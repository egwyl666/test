"""Очередь отправки на Prom.

Задача хранится в базе, поэтому переживает перезапуск сервера. Сбой сети или Prom = повтор с паузой,
а не потерянный товар. Если товар отредактировали, пока он отправлялся, он не будет помечен
как «на Prom» — он снова встанет в «готов к отправке».
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import config, db, feed, notify, phototunnel, products
from .prom_api import PromClient, PromError, import_state

log = logging.getLogger("promloader.sync")

BACKOFF_SECONDS = [10, 30, 60, 120, 300, 600, 900]
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
POLL_SECONDS = 5
MAX_WAIT = timedelta(hours=2)


def _at(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def import_settings() -> dict | None:
    raw = db.get_setting("import_settings")
    return json.loads(raw) if raw else None


def enqueue(product_ids: list[int]) -> dict:
    """Ставит товары в очередь. Товары с ошибками заполнения не берутся — возвращаются с причиной."""
    accepted, rejected = {}, []
    base_url = config.public_base_url()
    for pid in product_ids:
        try:
            p = products.get(pid)
        except KeyError:
            continue
        reasons = list(p["check"]["errors"])
        if p["status"] == "sending":
            reasons.append("Уже отправляется")
        if not base_url and not phototunnel.enabled() and any(not img["external"] for img in p["images"]):
            reasons.append("Фото загружены с компьютера, а временный доступ к фото выключен и публичный адрес не указан "
                           "(Настройки) — Prom не сможет их скачать")
        if reasons:
            rejected.append({"id": pid, "name": p["name"], "reasons": reasons})
        else:
            accepted[str(pid)] = p["revision"]

    job_id = None
    if accepted:
        quick_ids = {pid for pid in accepted if _quick_ok(int(pid))}
        groups = [("quick", {k: v for k, v in accepted.items() if k in quick_ids}),
                  ("import", {k: v for k, v in accepted.items() if k not in quick_ids})]
        ts = db.now()
        with db.tx() as c:
            for pid in accepted:
                c.execute("UPDATE products SET status = 'sending', last_error = '' WHERE id = ?", (int(pid),))
            for kind, group in groups:
                if not group:
                    continue
                cur = c.execute(
                    "INSERT INTO sync_jobs (status, kind, products, next_run_at, created_at, updated_at) "
                    "VALUES ('pending', ?, ?, ?, ?, ?)",
                    (kind, json.dumps(group), ts, ts, ts),
                )
                job_id = cur.lastrowid
    return {"job_id": job_id, "accepted": len(accepted), "rejected": rejected}


# Поля, изменение которых можно передать быстрым запросом (закупка/РРЦ на Prom не уходят вовсе).
QUICK_FIELDS = {"price", "presence", "quantity", "cost_price", "rrp"}
QUICK_CHUNK = 100


def quick_enabled() -> bool:
    return db.get_setting("quick_updates") != "0"


def _quick_ok(product_id: int) -> bool:
    """Товар уже на Prom и с тех пор поменялись только цена/наличие/количество."""
    if not quick_enabled():
        return False
    row = db.query_one("SELECT synced_at, pending_fields FROM products WHERE id = ?", (product_id,))
    if row is None or not row["synced_at"]:
        return False
    pending = set(json.loads(row["pending_fields"] or "[]"))
    return bool(pending) and pending <= QUICK_FIELDS


def list_jobs(limit: int = 20) -> list[dict]:
    rows = db.query("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT ?", (limit,))
    jobs = []
    for r in rows:
        job = dict(r)
        job["products"] = json.loads(job["products"])
        job["count"] = len(job["products"])
        job["result"] = json.loads(job["result"]) if job["result"] else None
        jobs.append(job)
    return jobs


def retry_job(job_id: int) -> dict:
    """Ручной повтор упавшей задачи: товары, которые с тех пор не меняли, отправятся снова."""
    job = db.query_one("SELECT * FROM sync_jobs WHERE id = ?", (job_id,))
    if job is None or job["status"] != "failed":
        raise ValueError("Повторить можно только задачу с ошибкой")
    ids = [int(pid) for pid in json.loads(job["products"])]
    with db.tx() as c:
        c.execute("UPDATE sync_jobs SET status = 'retried', updated_at = ? WHERE id = ?", (db.now(), job_id))
    return enqueue(ids)


def _update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE sync_jobs SET {sets} WHERE id = ?", list(fields.values()) + [job_id])


def _finish_products(job_products: dict, ok: bool, message: str = "", per_product: dict | None = None) -> None:
    """Проставляет итог только тем товарам, которые не меняли за время отправки."""
    per_product = per_product or {}
    ts = db.now()
    with db.tx() as c:
        for pid, revision in job_products.items():
            row = c.execute("SELECT revision, external_id FROM products WHERE id = ?", (int(pid),)).fetchone()
            if row is None:
                continue
            if row["revision"] != revision:
                c.execute("UPDATE products SET status = 'ready' WHERE id = ? AND status = 'sending'", (int(pid),))
                continue
            own_error = per_product.get(row["external_id"])
            if ok and not own_error:
                c.execute(
                    "UPDATE products SET status = 'synced', last_error = '', synced_at = ?, pending_fields = '[]' "
                    "WHERE id = ?", (ts, int(pid))
                )
            else:
                c.execute(
                    "UPDATE products SET status = 'error', last_error = ? WHERE id = ?",
                    (own_error or message, int(pid)),
                )


def _per_product_errors(result: dict) -> dict:
    """Пытается сопоставить ошибки импорта с артикулами. Формат ошибок Prom может отличаться — берём что узнаём."""
    found = {}
    errors = result.get("errors") or []
    if isinstance(errors, dict):
        errors = [{"id": k, "message": v} for k, v in errors.items()]
    for item in errors if isinstance(errors, list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("external_id") or item.get("offer_id") or item.get("id")
        message = item.get("message") or item.get("error") or json.dumps(item, ensure_ascii=False)
        if key is not None:
            found[str(key)] = str(message)
    return found


def _fail_or_retry(job: dict, err: PromError) -> None:
    attempts = job["attempts"] + 1
    if err.retryable and attempts < MAX_ATTEMPTS:
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        log.warning("Задача %s: %s — повтор через %s с", job["id"], err, delay)
        _update_job(job["id"], attempts=attempts, last_error=str(err), next_run_at=_at(delay))
        return
    log.error("Задача %s не выполнена: %s", job["id"], err)
    _update_job(job["id"], status="failed", attempts=attempts, last_error=str(err))
    count = len(json.loads(job["products"]))
    _finish_products(json.loads(job["products"]), ok=False, message=str(err))
    notify.send("sync_failed", f"❗ <b>Отправка на Prom не удалась</b> (товаров: {count})\n{notify.esc(err)}")


def _has_local_photos(product_ids: list[int]) -> bool:
    marks = ",".join("?" * len(product_ids))
    row = db.query_one(f"SELECT 1 FROM images WHERE file IS NOT NULL AND product_id IN ({marks}) LIMIT 1", product_ids)
    return row is not None


async def _photo_base_url(product_ids: list[int]) -> str:
    """Адрес, по которому Prom скачает фото: постоянный из настроек или временный туннель."""
    base = config.public_base_url()
    if base or not phototunnel.enabled():
        return base
    has_local = any(_has_local_photos(product_ids[i:i + 500]) for i in range(0, len(product_ids), 500))
    if not has_local:
        return ""
    try:
        return await asyncio.to_thread(phototunnel.tunnel.ensure_url)
    except phototunnel.TunnelError as exc:
        raise PromError(str(exc), retryable=True)


def _quick_items(ids: list[int]) -> list[dict]:
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT external_id, price, presence, quantity FROM products WHERE id IN ({marks})", ids)
    items = []
    for r in rows:
        item = {"id": r["external_id"], "price": r["price"], "presence": r["presence"]}
        if r["quantity"] is not None:
            item["quantity_in_stock"] = r["quantity"]
        items.append(item)
    return items


def _quick_errors(body: dict) -> dict:
    errors = body.get("errors") or {}
    if isinstance(errors, dict):
        return {str(k): str(v) for k, v in errors.items()}
    found = {}
    for item in errors if isinstance(errors, list) else []:
        if isinstance(item, dict):
            key = item.get("id") or item.get("external_id")
            if key is not None:
                found[str(key)] = str(item.get("message") or item.get("error") or item)
    return found


FORMAT_ERRORS = {400, 404, 405, 422}  # Prom не понимает запрос — а не «нет доступа» или «сервер лежит»


def _requeue_as_import(job_products: dict) -> None:
    """Товары, которые быстрый запрос не обработал, — отдельной задачей полного импорта."""
    if not job_products:
        return
    ts = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO sync_jobs (status, kind, products, next_run_at, created_at, updated_at) "
                  "VALUES ('pending', 'import', ?, ?, ?, ?)", (json.dumps(job_products), ts, ts, ts))


async def _start_quick(job: dict, client: PromClient) -> None:
    """Быстрое обновление цены/наличия через /products/edit_by_external_id (пачками по 100)."""
    job_products = json.loads(job["products"])
    ids = [int(pid) for pid in job_products]
    errors, processed, fallback = {}, 0, []
    try:
        for start in range(0, len(ids), QUICK_CHUNK):
            chunk = ids[start:start + QUICK_CHUNK]
            body = await client.edit_by_external_id(_quick_items(chunk))
            body = body if isinstance(body, dict) else {}
            chunk_errors = _quick_errors(body)
            errors.update(chunk_errors)
            if "processed_ids" in body:
                done = len(body.get("processed_ids") or [])
                processed += done
                if done < len(chunk) - len(chunk_errors):
                    # Prom обработал не всё и не сказал, что именно, — надёжнее отправить пачку полным импортом
                    fallback += chunk
            else:
                processed += len(chunk) - len(chunk_errors)
    except PromError as err:
        if err.retryable or err.status not in FORMAT_ERRORS:
            raise  # нет связи, токен, лимит — обычные повторы/ошибка, быстрый режим не трогаем
        log.warning("Быстрое обновление отклонено Prom (%s) — переключаюсь на обычный импорт", err)
        db.set_setting("quick_updates", "0")
        db.set_setting("quick_updates_error", str(err))
        _update_job(job["id"], kind="import", attempts=0, last_error="", next_run_at=db.now())
        return
    fallback_products = {str(pid): job_products[str(pid)] for pid in fallback}
    finished = {k: v for k, v in job_products.items() if k not in fallback_products}
    result = {"mode": "quick", "processed": processed, "errors": errors, "requeued": len(fallback_products)}
    _update_job(job["id"], status="done", result=json.dumps(result, ensure_ascii=False))
    _finish_products(finished, ok=True, per_product=errors)
    _requeue_as_import(fallback_products)
    if finished:
        notify.send("sync_done", f"✅ <b>Цены и наличие обновлены на Prom</b>: товаров {len(finished) - len(errors)}"
                    + (f", с ошибками {len(errors)}" if errors else ""))


async def _start(job: dict, client: PromClient) -> None:
    if job.get("kind") == "quick":
        return await _start_quick(job, client)
    job_products = json.loads(job["products"])
    ids = [int(pid) for pid in job_products]
    content = feed.build(ids, await _photo_base_url(ids))
    import_id = await client.import_file(content, import_settings())
    _update_job(job["id"], status="waiting", import_id=import_id, attempts=0, last_error="", next_run_at=_at(POLL_SECONDS))


async def _poll(job: dict, client: PromClient) -> None:
    result = await client.import_status(job["import_id"])
    state = import_state(result)
    if state == "running":
        started = datetime.fromisoformat(job["created_at"])
        if datetime.now(timezone.utc) - started > MAX_WAIT:
            raise PromError("Prom слишком долго обрабатывает импорт — проверьте раздел «Импорт» в кабинете")
        _update_job(job["id"], result=json.dumps(result, ensure_ascii=False), next_run_at=_at(POLL_SECONDS), attempts=0)
        return
    ok = state == "ok"
    message = "" if ok else "Prom не принял импорт: " + json.dumps(result, ensure_ascii=False)[:500]
    _update_job(job["id"], status="done" if ok else "failed", result=json.dumps(result, ensure_ascii=False), last_error=message)
    per_product = _per_product_errors(result)
    if ok:
        count = len(json.loads(job["products"]))
        notify.send("sync_done", f"✅ <b>Выгрузка на Prom выполнена</b>: товаров {max(0, count - len(per_product))}"
                    + (f", с ошибками {len(per_product)} (подробности — в программе)" if per_product else ""))
    if not ok:
        notify.send("sync_failed", f"❗ <b>Prom не принял импорт</b>\n{notify.esc(message[:300])}")
    _finish_products(json.loads(job["products"]), ok=ok, message=message, per_product=per_product)


def make_client() -> PromClient:
    return PromClient(config.get("prom_token"), config.get("prom_api_base"))


async def run_once(client_factory: Callable[[], PromClient] = make_client) -> int:
    """Обрабатывает все задачи, у которых подошло время. Возвращает число обработанных."""
    due = db.query(
        "SELECT * FROM sync_jobs WHERE status IN ('pending', 'waiting') AND next_run_at <= ? ORDER BY id",
        (db.now(),),
    )
    if not due:
        return 0
    try:
        client = client_factory()
    except PromError as err:
        for job in due:
            _fail_or_retry(dict(job), err)
        return len(due)
    async with client:
        for row in due:
            job = dict(row)
            try:
                if job["status"] == "pending":
                    await _start(job, client)
                else:
                    await _poll(job, client)
            except PromError as err:
                _fail_or_retry(job, err)
            except Exception as err:  # не даём одной задаче остановить очередь
                log.exception("Задача %s: непредвиденная ошибка", job["id"])
                _fail_or_retry(job, PromError(f"Внутренняя ошибка: {err}", retryable=True))
    return len(due)


async def worker(stop: asyncio.Event, interval: float = 2.0) -> None:
    while not stop.is_set():
        try:
            await run_once()
        except Exception:
            log.exception("Сбой очереди отправки")
        try:
            busy = db.query_one("SELECT 1 FROM sync_jobs WHERE status IN ('pending', 'waiting') LIMIT 1") is not None
            await asyncio.to_thread(phototunnel.tunnel.maybe_close, busy)
        except Exception:
            log.exception("Сбой временного доступа к фото")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
