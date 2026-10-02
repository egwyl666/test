"""Очередь отправки на Prom.

Задача хранится в базе, поэтому переживает перезапуск сервера. Сбой сети или Prom = повтор с паузой,
а не потерянный товар. Если товар отредактировали, пока он отправлялся, он не будет помечен
как «на Prom» — он снова встанет в «готов к отправке».
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import config, db, feed, notify, phototunnel, products, r2, updater
from .prom_api import BUSY_MARKERS, DEFAULT_IMPORT_SETTINGS, PromClient, PromError, import_state

log = logging.getLogger("promloader.sync")

BACKOFF_SECONDS = [10, 30, 60, 120, 300, 600, 900]
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
POLL_SECONDS = 5
MAX_WAIT = timedelta(hours=2)
NOTHING_WAIT = timedelta(minutes=15)  # «SUCCESS, total: 0» столько времени подряд — в файле правда нет товаров


def _at(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def import_settings() -> dict | None:
    raw = db.get_setting("import_settings")
    return json.loads(raw) if raw else None


def enqueue(product_ids: list[int]) -> dict:
    """Ставит товары в очередь. Товары с ошибками заполнения не берутся — возвращаются с причиной."""
    accepted, rejected = {}, []
    base_url = config.public_base_url() or (r2.base_url() if r2.active() else "")
    for pid in product_ids:
        try:
            p = products.get(pid)
        except KeyError:
            continue
        reasons = list(p["check"]["errors"])
        if p["status"] == "sending":
            reasons.append("Уже отправляется")
        if not base_url and not phototunnel.enabled() and any(not img["external"] for img in p["images"]):
            reasons.append("Фото загружены с компьютера, а способ передать их Prom не выбран "
                           "(Настройки → «Фото для Prom») — Prom не сможет их скачать")
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
        job["sent"] = json.loads(job["sent"]) if job.get("sent") else None
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


NOTHING_FOUND = ("Prom принял файл, но не нашёл в нём товаров (total: 0) — товар на Prom не появился. "
                 "Нажмите «Скачать файл» у этой выгрузки и загрузите его в кабинете Prom вручную "
                 "(Імпорт → Завантажити файл з комп'ютера): кабинет покажет, что ему не нравится. Пришлите это в «Поддержку»")


def _progress(result: dict) -> str:
    total = result.get("total") if isinstance(result, dict) else None
    if isinstance(total, int) and total:
        return f"Prom принял файл (товаров: {total}) и обрабатывает их — обычно 1–3 минуты"
    return "Prom принял файл и разбирает его"


def _found_nothing(result: dict) -> bool:
    """Prom отвечает «SUCCESS», даже если не узнал в файле ни одного товара."""
    total = result.get("total") if isinstance(result, dict) else None
    return isinstance(total, int) and total == 0


def repair_false_success() -> int:
    """Чиним следы старых версий: выгрузки «успешные» при total: 0 (до 1.3.3) и упавшие из-за того,
    что Prom был занят другим импортом (до 1.3.7) — вторые ставим в очередь заново."""
    fixed = 0
    for job in db.query("SELECT id, last_error FROM sync_jobs WHERE status = 'failed'"):
        if any(m in job["last_error"].lower() for m in BUSY_MARKERS):
            retry_job(job["id"])
            fixed += 1
    for job in db.query("SELECT * FROM sync_jobs WHERE status = 'done' AND kind = 'import' AND result IS NOT NULL"):
        try:
            result = json.loads(job["result"])
        except ValueError:
            continue
        if not _found_nothing(result):
            continue
        with db.tx() as c:
            c.execute("UPDATE sync_jobs SET status = 'failed', last_error = ? WHERE id = ?", (NOTHING_FOUND, job["id"]))
            for pid in json.loads(job["products"]):
                c.execute("""UPDATE products SET status = 'error', last_error = ?, synced_at = NULL, pending_fields = '["*"]'
                             WHERE id = ? AND status = 'synced'""", (NOTHING_FOUND, int(pid)))
        fixed += 1
    return fixed


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


BUSY_RETRY_SECONDS = 120
BUSY_GIVE_UP = timedelta(hours=12)


def _fail_or_retry(job: dict, err: PromError) -> None:
    if getattr(err, "busy", False) and datetime.now(timezone.utc) - datetime.fromisoformat(job["created_at"]) < BUSY_GIVE_UP:
        # Prom занят другим импортом — это не ошибка: ждём, попытки не тратим
        _update_job(job["id"], last_error=str(err), next_run_at=_at(BUSY_RETRY_SECONDS))
        return
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
    """Адрес, по которому Prom скачает фото: хранилище R2, постоянный адрес из настроек или временный туннель."""
    if r2.active():
        try:
            await asyncio.to_thread(r2.upload, r2.local_files(product_ids))
        except r2.R2Error as exc:
            raise PromError(f"Фото не загрузились в хранилище R2: {exc}", retryable=exc.retryable)
        return r2.base_url()
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


WAIT_PREVIOUS = "Ждёт, пока Prom закончит выгрузку №{}"


def _import_in_progress(job_id: int):
    """Prom обрабатывает импорты по одному: пока идёт предыдущий, новый не запускаем."""
    return db.query_one("SELECT id FROM sync_jobs WHERE kind = 'import' AND status = 'waiting' AND id != ? "
                        "ORDER BY id LIMIT 1", (job_id,))


def _absorb_queued(job: dict) -> dict:
    """Всё, что накопилось в очереди за время ожидания, — в этот же файл: один импорт вместо нескольких."""
    others = db.query("SELECT id, products FROM sync_jobs WHERE kind = 'import' AND status = 'pending' AND attempts = 0 "
                      "AND id != ? ORDER BY id", (job["id"],))
    job_products = json.loads(job["products"])
    if not others:
        return job_products
    for row in others:
        job_products.update(json.loads(row["products"]))
    with db.tx() as c:
        c.execute("UPDATE sync_jobs SET products = ?, updated_at = ? WHERE id = ?",
                  (json.dumps(job_products), db.now(), job["id"]))
        c.executemany("DELETE FROM sync_jobs WHERE id = ?", [(row["id"],) for row in others])
    job["products"] = json.dumps(job_products)
    return job_products


async def _start(job: dict, client: PromClient) -> None:
    if job.get("kind") == "quick":
        return await _start_quick(job, client)
    busy = _import_in_progress(job["id"])
    if busy:
        _update_job(job["id"], last_error=WAIT_PREVIOUS.format(busy["id"]), next_run_at=_at(POLL_SECONDS))
        return
    job_products = _absorb_queued(job)
    ids = [int(pid) for pid in job_products]
    content = feed.build(ids, await _photo_base_url(ids))
    settings = {**DEFAULT_IMPORT_SETTINGS, **(import_settings() or {})}
    if db.get_setting("import_plain_v2") == "1":
        settings.pop("updated_fields", None)
    method = "url" if db.get_setting("import_method") == "url" else "file"
    import_id, sent = await _send_import(client, content, settings, method)
    # что именно ушло на Prom — чтобы по /api/sync/jobs было видно версию, способ и настройки
    _update_job(job["id"], status="waiting", import_id=import_id, attempts=0, last_error="", started_at=db.now(),
                sent=json.dumps(sent, ensure_ascii=False), next_run_at=_at(POLL_SECONDS))


FEED_KEEP_SECONDS = 2 * 24 * 3600


async def _publish_feed(content: bytes) -> str:
    """Выкладывает файл выгрузки по публичной ссылке — для импорта Prom «по ссылке»."""
    name = f"{uuid.uuid4().hex}.xml"
    if r2.active():
        try:
            await asyncio.to_thread(r2.put, f"feed/{name}", content, "text/xml; charset=utf-8")
        except r2.R2Error as exc:
            raise PromError(f"Файл выгрузки не загрузился в R2: {exc}", retryable=exc.retryable)
        return f"{r2.base_url()}/feed/{name}"
    folder = phototunnel.feeds_dir()
    for old in folder.glob("*.xml"):
        if time.time() - old.stat().st_mtime > FEED_KEEP_SECONDS:
            old.unlink(missing_ok=True)
    (folder / name).write_bytes(content)
    base = config.public_base_url()
    if not base:
        try:
            base = await asyncio.to_thread(phototunnel.tunnel.ensure_url)
        except phototunnel.TunnelError as exc:
            raise PromError(str(exc), retryable=True)
    return f"{base}/feed/{name}"


async def _send_import(client: PromClient, content: bytes, settings: dict, method: str) -> tuple[str, dict]:
    """Отправляет файл на Prom: загрузкой файла или ссылкой на него. Возвращает id импорта и что ушло."""
    url = await _publish_feed(content) if method == "url" else ""

    async def send():
        return await (client.import_url(url, settings) if url else client.import_file(content, settings))

    try:
        import_id = await send()
    except PromError as err:
        if err.status not in (400, 422) or "updated_fields" not in settings or "updated_fields" not in str(err):
            raise
        # Prom не принял список полей — дальше отправляем без него
        log.warning("Prom не принял updated_fields (%s) — отправляю без списка полей", err)
        db.set_setting("import_plain_v2", "1")  # v2: до 1.3.7 ставилось по ошибке на любой 400
        settings = {k: v for k, v in settings.items() if k != "updated_fields"}
        import_id = await send()
    sent = {"version": updater.current_version(), "method": method, "bytes": len(content),
            "offers": content.count(b"<offer "), "settings": settings}
    if url:
        sent["url"] = url
    return import_id, sent


async def _poll(job: dict, client: PromClient) -> None:
    result = await client.import_status(job["import_id"])
    state = import_state(result)
    if state == "running":
        started = datetime.fromisoformat(job.get("started_at") or job["created_at"])
        waited = datetime.now(timezone.utc) - started
        if _found_nothing(result) and waited > NOTHING_WAIT:
            state = "ok"  # Prom так и не нашёл товаров в файле — разбираемся ниже
        elif waited > MAX_WAIT:
            raise PromError("Prom слишком долго обрабатывает импорт — проверьте раздел «Импорт» в кабинете")
        if state == "running":
            _update_job(job["id"], result=json.dumps(result, ensure_ascii=False), next_run_at=_at(POLL_SECONDS),
                        attempts=0, last_error=_progress(result))
            return
    ok = state == "ok"
    message = "" if ok else "Prom не принял импорт: " + json.dumps(result, ensure_ascii=False)[:500]
    sent = json.loads(job.get("sent") or "{}")
    if ok and _found_nothing(result) and sent.get("method", "file") == "file":
        # Prom не увидел товаров в загруженном файле — тот же файл по ссылке («Завантажити файл з сервера»)
        ids = [int(pid) for pid in json.loads(job["products"])]
        content = feed.build(ids, await _photo_base_url(ids))
        import_id, retry = await _send_import(client, content, sent.get("settings") or dict(DEFAULT_IMPORT_SETTINGS), "url")
        retry["after_file_import"] = job["import_id"]
        _update_job(job["id"], import_id=import_id, sent=json.dumps(retry, ensure_ascii=False), started_at=db.now(),
                    result=json.dumps(result, ensure_ascii=False), next_run_at=_at(POLL_SECONDS),
                    last_error="Prom не увидел товаров в загруженном файле — повторяю тот же файл по ссылке")
        return
    if ok and _found_nothing(result):
        ok = False
        message = NOTHING_FOUND
    if ok and sent.get("after_file_import") and db.get_setting("import_method") != "url":
        log.warning("Импорт по ссылке сработал, а загрузкой файла — нет: дальше отправляю по ссылке")
        db.set_setting("import_method", "url")
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
            fresh = db.query_one("SELECT * FROM sync_jobs WHERE id = ?", (row["id"],))
            if fresh is None or fresh["status"] not in ("pending", "waiting"):
                continue  # задачу уже объединили с другой (или она завершилась) в этом же проходе
            job = dict(fresh)
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
