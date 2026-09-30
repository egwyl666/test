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

from . import config, db, feed, products
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
        if not base_url and any(not img["external"] for img in p["images"]):
            reasons.append("Фото загружены сюда, но не указан публичный адрес сайта (Настройки) — Prom не сможет их скачать")
        if reasons:
            rejected.append({"id": pid, "name": p["name"], "reasons": reasons})
        else:
            accepted[str(pid)] = p["revision"]

    job_id = None
    if accepted:
        ts = db.now()
        with db.tx() as c:
            for pid in accepted:
                c.execute("UPDATE products SET status = 'sending', last_error = '' WHERE id = ?", (int(pid),))
            cur = c.execute(
                "INSERT INTO sync_jobs (status, products, next_run_at, created_at, updated_at) VALUES ('pending', ?, ?, ?, ?)",
                (json.dumps(accepted), ts, ts, ts),
            )
            job_id = cur.lastrowid
    return {"job_id": job_id, "accepted": len(accepted), "rejected": rejected}


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
                    "UPDATE products SET status = 'synced', last_error = '', synced_at = ? WHERE id = ?", (ts, int(pid))
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
    _finish_products(json.loads(job["products"]), ok=False, message=str(err))


async def _start(job: dict, client: PromClient) -> None:
    job_products = json.loads(job["products"])
    content = feed.build([int(pid) for pid in job_products], config.public_base_url())
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
    _finish_products(json.loads(job["products"]), ok=ok, message=message, per_product=_per_product_errors(result))


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
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
