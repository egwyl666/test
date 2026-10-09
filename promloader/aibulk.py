"""Массовый ИИ: очередь заданий по многим товарам с учётом лимитов бесплатного тарифа.

- товары обрабатываются по одному в фоне, темп — не больше RATE запросов в минуту (настраивается);
- при превышении лимита у провайдера (429) — пауза на минуту и повтор того же товара;
- старые значения полей сохраняются: все изменения задания можно откатить одной кнопкой;
- «только где пусто»: перевод/ключевые слова не трогают товары, где это уже заполнено.
"""

import json
import time

from . import ai, aiprice, db, products
from . import changes as changes_log

BULK_ACTIONS = ("improve", "shorten", "translate_ua", "name", "keywords", "custom")
ACTION_LABEL = {k: v[0] for k, v in ai.ACTIONS.items()}
# какие поля должны быть пустыми, чтобы товар обрабатывался в режиме «только где пусто»
EMPTY_CHECK = {"translate_ua": ("name_ua", "description_ua"), "keywords": ("keywords",), "improve": ("description",)}
DEFAULT_RATE = {"gemini": 8, "claude": 30}
RATE_LIMIT_PAUSE = 60
MAX_ATTEMPTS = 3


class BulkError(ValueError):
    pass


def rate_per_minute() -> int:
    custom = db.get_setting("ai_rate")
    if custom.isdigit() and int(custom) > 0:
        return int(custom)
    return DEFAULT_RATE.get(ai.settings()["provider"], 8)


def create(product_ids: list[int], action: str, instruction: str = "", only_empty: bool = True) -> dict:
    if action not in BULK_ACTIONS:
        raise BulkError("Неизвестное действие")
    if action == "custom" and not instruction.strip():
        raise BulkError("Напишите, что сделать с товарами")
    if not ai.enabled():
        raise BulkError("ИИ не подключён: выберите провайдера и вставьте ключ в «Настройках»")
    ids = sorted(set(product_ids))
    if not ids:
        raise BulkError("Не выбрано ни одного товара")
    ts = db.now()
    with db.tx() as c:
        job_id = c.execute(
            "INSERT INTO ai_jobs (action, instruction, only_empty, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (action, instruction.strip(), 1 if only_empty else 0, ts, ts)).lastrowid
        c.executemany("INSERT INTO ai_items (job_id, product_id) VALUES (?, ?)", [(job_id, pid) for pid in ids])
    return get(job_id)


def get(job_id: int) -> dict:
    row = db.query_one("SELECT * FROM ai_jobs WHERE id = ?", (job_id,))
    if row is None:
        raise KeyError(job_id)
    job = dict(row)
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM ai_items WHERE job_id = ? GROUP BY status", (job_id,))}
    job["counts"] = counts
    job["total"] = sum(counts.values())
    job["label"] = ai.ACTIONS[job["action"]][0]
    job["errors"] = [dict(r) for r in db.query(
        "SELECT product_id, message FROM ai_items WHERE job_id = ? AND status = 'error' LIMIT 10", (job_id,))]
    spent = aiprice.job_spent(job_id)
    job["spent"] = {**spent, "cost_uah": aiprice.to_uah(spent["cost_usd"])}
    return job


def estimate(count: int, action: str) -> dict:
    """Сколько примерно обойдётся задание: средняя цена последних запросов этой модели, иначе — типичный запрос."""
    s = ai.settings()
    model = s["model"] or (db.get_setting("gemini_model_used") if s["provider"] == "gemini" else "")
    per = aiprice.average_cost(s["provider"], model, action) if model else None
    measured = per is not None
    if per is None:
        # авто-модель Gemini ещё ни разу не выбиралась — считаем по цене свежей Flash (её программа и выберет)
        per = aiprice.typical_cost(s["provider"], model or ("gemini-flash" if s["provider"] == "gemini" else ""))
    total = per * count if per is not None else None
    minutes = round(count / max(1, rate_per_minute()))
    return {"count": count, "model": model, "per_request_usd": per, "per_request_uah": aiprice.to_uah(per),
            "total_usd": total, "total_uah": aiprice.to_uah(total), "measured": measured,
            "free": s["provider"] == "gemini" and aiprice.free_tier(), "minutes": minutes}


def list_jobs(limit: int = 5) -> list[dict]:
    return [get(r["id"]) for r in db.query("SELECT id FROM ai_jobs ORDER BY id DESC LIMIT ?", (limit,))]


def set_status(job_id: int, status: str) -> dict:
    if status not in ("running", "paused", "cancelled"):
        raise BulkError("Неизвестный статус")
    job = get(job_id)
    if job["status"] in ("done", "cancelled", "reverted"):
        raise BulkError("Задание уже завершено")
    with db.tx() as c:
        c.execute("UPDATE ai_jobs SET status = ?, message = '', updated_at = ? WHERE id = ?", (status, db.now(), job_id))
        if status == "cancelled":
            c.execute("UPDATE ai_items SET status = 'skipped', message = 'отменено' WHERE job_id = ? AND status = 'pending'",
                      (job_id,))
    return get(job_id)


def revert(job_id: int) -> int:
    """Вернуть прежние значения полей у всех товаров задания."""
    rows = db.query("SELECT product_id, old FROM ai_items WHERE job_id = ? AND status = 'done' AND old IS NOT NULL",
                    (job_id,))
    restored = 0
    for r in rows:
        try:
            with changes_log.source("ИИ: откат"):
                products.update(r["product_id"], json.loads(r["old"]), lock=False)
            restored += 1
        except KeyError:
            continue
    with db.tx() as c:
        c.execute("UPDATE ai_jobs SET status = 'reverted', message = ?, updated_at = ? WHERE id = ?",
                  (f"Откачено изменений: {restored}", db.now(), job_id))
        c.execute("UPDATE ai_items SET status = 'skipped', message = 'отменено' WHERE job_id = ? AND status = 'pending'",
                  (job_id,))
    return restored


def _next_item():
    return db.query_one(
        """SELECT i.*, j.action, j.instruction, j.only_empty FROM ai_items i JOIN ai_jobs j ON j.id = i.job_id
           WHERE j.status = 'running' AND i.status = 'pending' ORDER BY j.id, i.id LIMIT 1""")


def _finish_jobs() -> None:
    with db.tx() as c:
        c.execute("""UPDATE ai_jobs SET status = 'done', updated_at = ? WHERE status = 'running' AND NOT EXISTS
                     (SELECT 1 FROM ai_items i WHERE i.job_id = ai_jobs.id AND i.status = 'pending')""", (db.now(),))


def _mark(item_id: int, status: str, message: str = "", old=None, new=None) -> None:
    with db.tx() as c:
        c.execute("UPDATE ai_items SET status = ?, message = ?, old = ?, new = ? WHERE id = ?",
                  (status, message, json.dumps(old, ensure_ascii=False) if old is not None else None,
                   json.dumps(new, ensure_ascii=False) if new is not None else None, item_id))


def process_one(runner=ai.run) -> str:
    """Обработать один товар. Возвращает 'idle' | 'done' | 'skipped' | 'error' | 'rate_limited'."""
    item = _next_item()
    if item is None:
        _finish_jobs()
        return "idle"
    try:
        p = products.get(item["product_id"])
    except KeyError:
        _mark(item["id"], "skipped", "товар удалён")
        return "skipped"
    fields = EMPTY_CHECK.get(item["action"])
    if item["only_empty"] and fields and all((p.get(f) or "").strip() for f in fields):
        _mark(item["id"], "skipped", "уже заполнено")
        return "skipped"
    try:
        with ai.usage_source("bulk", item["job_id"]):
            changes = runner(p, item["action"], item["instruction"])
    except ai.AIError as exc:
        if exc.temporary:
            return "rate_limited"  # лимит или перегрузка у провайдера — подождём и повторим этот же товар
        _mark(item["id"], "error", str(exc))
        return "error"
    if item["only_empty"] and fields:
        changes = {k: v for k, v in changes.items() if k not in fields or not (p.get(k) or "").strip()}
    old = {k: p.get(k) for k in changes}
    if changes:
        with changes_log.source(f"ИИ: {ACTION_LABEL.get(item['action'], item['action'])}"):
            products.update(p["id"], changes)
    _mark(item["id"], "done", old=old, new=changes)
    return "done"


def pause_running(message: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE ai_jobs SET message = ?, updated_at = ? WHERE status = 'running'", (message, db.now()))


def has_work() -> bool:
    return _next_item() is not None


class Pacer:
    """Не чаще rate запросов в минуту."""

    def __init__(self):
        self.last = 0.0

    def wait_time(self) -> float:
        return max(0.0, self.last + 60.0 / rate_per_minute() - time.monotonic())

    def mark(self) -> None:
        self.last = time.monotonic()
