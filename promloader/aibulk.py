"""Массовый ИИ: очередь заданий по многим товарам с учётом лимитов бесплатного тарифа.

- товары обрабатываются в фоне — по одному или пачкой в одном запросе (ai.BATCH: ключевые слова, названия, переводы), темп — не больше RATE запросов в минуту (настраивается);
- при превышении лимита у провайдера (429) — пауза на минуту и повтор того же товара;
- старые значения полей сохраняются: все изменения задания можно откатить одной кнопкой;
- «только где пусто»: перевод/ключевые слова не трогают товары, где это уже заполнено.
"""

import json
import logging
import time

from . import ai, aicategory, aiprice, db, products
from . import changes as changes_log

log = logging.getLogger("promloader.aibulk")

BULK_ACTIONS = ("prepare_prom", "from_photo", "match_category", "improve", "shorten", "translate_ua", "translate_ru",
                "name", "keywords", "custom")
ACTION_LABEL = {k: v[0] for k, v in ai.ACTIONS.items()} | {"match_category": aicategory.LABEL}
# какие поля должны быть пустыми, чтобы товар обрабатывался в режиме «только где пусто»
# (что пропускать целиком — ai.already_done; эти поля при «только где пусто» не перезаписываются, если уже заполнены)
EMPTY_CHECK = {"translate_ua": ("name_ua", "description_ua"), "keywords": ("keywords",), "improve": ("description",),
               "prepare_prom": ("name_ua", "description_ua", "keywords"),
               # по фото: название можно улучшить (оно короткое и непонятное), остальное — только если пусто
               "from_photo": ("description", "name_ua", "description_ua", "keywords")}
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


def create(product_ids: list[int], action: str, instruction: str = "", only_empty: bool = True,
           auto_promote: bool = False, limit: int | None = None) -> dict:
    """limit — только первые N по выгоде (пробный прогон на 20–50 лучших товарах, а не на всех 6000)."""
    if action not in BULK_ACTIONS:
        raise BulkError("Неизвестное действие")
    if action == "custom" and not instruction.strip():
        raise BulkError("Напишите, что сделать с товарами")
    if not ai.enabled():
        raise BulkError("ИИ не подключён: выберите провайдера и вставьте ключ в «Настройках»")
    if action == "match_category":
        choices = aicategory.known_choices()
        if not choices["groups"] and not choices["categories"]:
            raise BulkError("Выбирать не из чего: у товаров нет групп, а категории Prom ещё не известны. Нажмите "
                            "«⬇ Каталог с Prom» или задайте группы нескольким товарам")
    # очередь по выгоде: сначала в наличии, с фото и с большей наценкой — бесплатный лимит ИИ тратится на них первыми
    ids = products.profit_order(sorted(set(product_ids)))
    if limit:
        ids = ids[:max(1, int(limit))]
    if not ids:
        raise BulkError("Не выбрано ни одного товара")
    ts = db.now()
    with db.tx() as c:
        job_id = c.execute(
            "INSERT INTO ai_jobs (action, instruction, only_empty, auto_promote, created_at, updated_at, user) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action, instruction.strip(), 1 if only_empty else 0, 1 if auto_promote else 0, ts, ts,
             changes_log.current_user())).lastrowid
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
    job["label"] = ACTION_LABEL.get(job["action"], job["action"])
    job["errors"] = [dict(r) for r in db.query(
        "SELECT product_id, message FROM ai_items WHERE job_id = ? AND status = 'error' LIMIT 10", (job_id,))]
    spent = aiprice.job_spent(job_id)
    job["spent"] = {**spent, "cost_uah": aiprice.to_uah(spent["cost_usd"])}
    return job


def estimate(count: int, action: str, limit: int | None = None) -> dict:
    """Сколько примерно обойдётся задание: средняя цена последних запросов этой модели, иначе — типичный запрос."""
    if limit:
        count = min(count, max(1, int(limit)))
    s = ai.settings()
    model = s["model"] or (db.get_setting("gemini_model_used") if s["provider"] == "gemini" else "")
    per = aiprice.average_cost(s["provider"], model, action) if model else None
    measured = per is not None
    if per is None:
        # авто-модель Gemini ещё ни разу не выбиралась — считаем по цене свежей Flash (её программа и выберет)
        per = aiprice.typical_cost(s["provider"], model or ("gemini-flash" if s["provider"] == "gemini" else ""))
    size = ai.batch_size(action)
    requests = -(-count // size)  # пачками: запросов в size раз меньше, чем товаров
    # замеренная цена — уже за запрос-пачку; типичная — за один товар (пачка дешевле: инструкция одна на всех)
    total = (per * requests if measured and size > 1 else per * count) if per is not None else None
    minutes = round(requests / max(1, rate_per_minute()))
    return {"count": count, "requests": requests, "batch": size, "model": model, "per_request_usd": per, "per_request_uah": aiprice.to_uah(per),
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
    _unpromote(job_id)
    with db.tx() as c:
        c.execute("UPDATE ai_jobs SET status = 'reverted', message = ?, updated_at = ? WHERE id = ?",
                  (f"Откачено изменений: {restored}", db.now(), job_id))
        c.execute("UPDATE ai_items SET status = 'skipped', message = 'отменено' WHERE job_id = ? AND status = 'pending'",
                  (job_id,))
    return restored


def _unpromote(job_id: int) -> int:
    """Откат задания с «сразу в «Готов к отправке»»: товары, которые задание перевело в «Готов» и которые с тех пор
    никуда не ушли, — обратно в черновики (без текстов ИИ они могут быть уже не готовы)."""
    from . import changes
    rows = db.query("""SELECT p.id FROM products p JOIN ai_items i ON i.product_id = p.id
                       WHERE i.job_id = ? AND p.status = 'ready' AND (
                         SELECT ch.source FROM product_changes ch WHERE ch.product_id = p.id AND ch.field = 'status'
                         ORDER BY ch.id DESC LIMIT 1) = ?""", (job_id, AUTO_PROMOTE_SOURCE))
    with db.tx() as c:
        for r in rows:
            if c.execute("UPDATE products SET status = 'draft', updated_at = ? WHERE id = ? AND status = 'ready'",
                         (db.now(), r["id"])).rowcount:
                changes.record(c, r["id"], "status", products.STATUSES["ready"], products.STATUSES["draft"],
                               who="ИИ: откат")
    return len(rows)


def _next_item():
    return db.query_one(
        """SELECT i.*, j.action, j.instruction, j.only_empty, j.auto_promote, j.user FROM ai_items i
           JOIN ai_jobs j ON j.id = i.job_id
           WHERE j.status = 'running' AND i.status = 'pending' ORDER BY j.id, i.id LIMIT 1""")


SINGLE = "single"  # пачка не разобралась (ответ оборвался) — эти товары дальше идут по одному


def _next_items(limit: int) -> list:
    first = _next_item()
    if first is None or limit <= 1 or first["message"] == SINGLE:
        return [first] if first else []
    return db.query(
        """SELECT i.*, j.action, j.instruction, j.only_empty, j.auto_promote, j.user FROM ai_items i
           JOIN ai_jobs j ON j.id = i.job_id
           WHERE i.job_id = ? AND i.status = 'pending' AND i.message != ? ORDER BY i.id LIMIT ?""",
        (first["job_id"], SINGLE, limit))


NOTIFY_MIN_ITEMS = 10  # о маленьких заданиях не уведомляем: их результат и так видно сразу


def _notify_job(job_id: int, stopped: str = "") -> None:
    """Telegram и push: «Массовый ИИ завершён» (или остановлен) — итоги задания. Не бросает исключений."""
    from . import notify
    try:
        job = get(job_id)
        if job["total"] < NOTIFY_MIN_ITEMS:
            return
        n = job["counts"]
        head = (f"⛔ <b>Массовый ИИ остановлен: {notify.esc(job['label'])}</b>\n{notify.esc(stopped)}" if stopped
                else f"✅ <b>Массовый ИИ завершён: {notify.esc(job['label'])}</b>")
        promoted = f"\nВ «Готов к отправке»: {job['promoted']}" if job.get("auto_promote") else ""
        notify.send("ai_bulk", f"{head}\nТоваров: {job['total']} · готово: {n.get('done', 0)} · "
                               f"пропущено: {n.get('skipped', 0)} · ошибок: {n.get('error', 0)}{promoted}")
    except Exception:
        log.exception("Уведомление о задании ИИ %s не отправлено", job_id)


def _finish_jobs() -> None:
    with db.tx() as c:
        finished = [r["id"] for r in c.execute(
            """SELECT id FROM ai_jobs WHERE status = 'running' AND NOT EXISTS
               (SELECT 1 FROM ai_items i WHERE i.job_id = ai_jobs.id AND i.status = 'pending')""").fetchall()]
        if finished:
            c.execute(f"UPDATE ai_jobs SET status = 'done', updated_at = ? WHERE id {db.IN_LIST}",
                      (db.now(), db.as_list(finished)))
    for job_id in finished:
        _notify_job(job_id)


def _stop_job(job_id: int, message: str) -> None:
    """Ошибка, которая не пройдёт сама (ключ ИИ не принят): ставим задание на паузу — иначе каждый следующий товар
    получал бы ту же ошибку — и сообщаем."""
    with db.tx() as c:
        c.execute("UPDATE ai_jobs SET status = 'paused', message = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                  (message, db.now(), job_id))
    _notify_job(job_id, message)


def _mark(item_id: int, status: str, message: str = "", old=None, new=None) -> None:
    with db.tx() as c:
        c.execute("UPDATE ai_items SET status = ?, message = ?, old = ?, new = ? WHERE id = ?",
                  (status, message, json.dumps(old, ensure_ascii=False) if old is not None else None,
                   json.dumps(new, ensure_ascii=False) if new is not None else None, item_id))


last_limit: ai.AIError | None = None  # последний лимит/перегрузка — для паузы и сообщения у задания


def _prepare(item):
    """Товар для запроса или None (пропущен: удалён, уже заполнено, переводить нечего — запрос не нужен)."""
    try:
        p = products.get(item["product_id"])
    except KeyError:
        _mark(item["id"], "skipped", "товар удалён")
        return None
    if item["only_empty"] and ai.already_done(p, item["action"]):
        _mark(item["id"], "skipped", "уже заполнено")  # например, украинский текст пришёл со второй ссылки поставщика
        # «сразу в «Готов»»: товар без ИИ уже готов — его тоже переводим (иначе он остался бы черновиком)
        _auto_promote(item, [p["id"]])
        return None
    if item["action"] == "from_photo" and not products.image_rows(p["id"]):
        _mark(item["id"], "skipped", "нет фото")  # без фото описывать нечего — запрос не тратим
        return None
    if item["action"] == "prepare_prom" and ai.too_little_data(p):
        # не выдумываем по названию «Чехол 123»: товар остаётся в «мало данных — заполнить вручную»
        _mark(item["id"], "skipped", MANUAL)
        return None
    if ai.nothing_to_translate(p, item["action"]):
        _mark(item["id"], "skipped", "нет украинского текста")  # запрос к ИИ не нужен и не оплачивается
        return None
    return p


def _apply(item, p: dict, changes: dict) -> None:
    """p — товар, каким его видел ИИ. Пока шёл запрос (секунды, а в очереди с лимитом — минуты), человек мог
    поправить поле в карточке: такое поле ответ ИИ не перезаписывает — правка человека новее."""
    fields = EMPTY_CHECK.get(item["action"])
    if item["only_empty"] and fields:
        changes = {k: v for k, v in changes.items() if k not in fields or not (p.get(k) or "").strip()}
    kept = []
    with db.tx():
        try:
            now = products.get(p["id"])
        except KeyError:
            _mark(item["id"], "skipped", "товар удалён")
            return
        kept = [k for k in changes if now.get(k) != p.get(k)]
        changes = {k: v for k, v in changes.items() if k not in kept}
        old = {k: p.get(k) for k in changes}
        if changes:
            with changes_log.source(f"ИИ: {ACTION_LABEL.get(item['action'], item['action'])}"), \
                    changes_log.user(item["user"]):
                products.update(p["id"], changes)
    names = ", ".join(changes_log.LABELS.get(k, k) for k in kept)
    message = f"не перезаписано — поправили вручную, пока работал ИИ: {names}" if kept else ""
    _mark(item["id"], "done", message, old=old, new=changes)


def process_one(runner=None) -> str:
    """Обработать следующий товар — или пачку товаров одним запросом (ключевые слова, названия, переводы: так
    бесплатного лимита хватает в разы на большее число товаров). runner — свой вызов ИИ для одного товара (тесты).
    Возвращает 'idle' | 'done' | 'skipped' | 'error' | 'rate_limited'."""
    global last_limit
    first = _next_item()
    if first is None:
        _finish_jobs()
        return "idle"
    size = ai.batch_size(first["action"]) if runner is None else 1
    batch = [(item, p) for item in _next_items(size) if (p := _prepare(item)) is not None]
    if not batch:
        return "skipped"
    try:
        with ai.usage_source("bulk", first["job_id"]):
            only_missing = bool(first["only_empty"]) and first["action"] in ai.ONLY_MISSING
            if first["action"] == "match_category" and runner is None:
                results, _ = aicategory.match([p for _, p in batch], only_empty=bool(first["only_empty"]))
            elif len(batch) == 1 and (runner is not None or not only_missing):
                item, p = batch[0]
                results = [(runner or ai.run)(p, item["action"], item["instruction"])]
            else:
                results, _ = ai.run_batch([p for _, p in batch], first["action"], only_missing=only_missing)
    except ai.AIError as exc:
        if exc.temporary:
            last_limit = exc
            return "rate_limited"  # лимит или перегрузка у провайдера — подождём и повторим эти же товары
        if exc.key_error:
            _stop_job(first["job_id"], f"{exc}. Исправьте ключ и нажмите «Продолжить»")
            return "error"
        if len(batch) > 1 and not exc.key_error:
            # ответ на пачку не разобрался (например, оборвался) — эти товары пойдут по одному, ничего не теряем
            with db.tx() as c:
                c.executemany("UPDATE ai_items SET message = ? WHERE id = ?", [(SINGLE, item["id"]) for item, _ in batch])
            return "error"
        for item, _ in batch:
            _mark(item["id"], "error", str(exc))
        return "error"
    status = "error"
    applied = []
    for (item, p), result in zip(batch, results):
        if isinstance(result, ai.AIError):
            _mark(item["id"], "error", str(result))
        else:
            _apply(item, p, result)
            applied.append(p["id"])
            status = "done"
    _auto_promote(first, applied)
    return status


AUTO_PROMOTE_SOURCE = "Массовый ИИ: готов к отправке"
MANUAL = "мало данных — заполнить вручную"


def start_pipeline(supplier_id: int) -> dict:
    """Автоконвейер поставщика: черновики в наличии, которые уже готовы, — сразу в «Готов к отправке»; остальным —
    задание «Подготовить для Prom» с «сразу в «Готов»». Товары, уже стоящие в очереди ИИ, второй раз не ставим.
    Возвращает {pipeline_promoted, pipeline_queued, pipeline_manual}."""
    # одним запросом и только нужные поля: у поставщика бывают тысячи черновиков, конвейер идёт после каждого прайса
    rows = db.query("""SELECT p.id, p.name, p.name_ua, p.description, p.description_ua, p.keywords, p.params, p.vendor
                       FROM products p WHERE p.supplier_id = ? AND p.status = 'draft'
                       AND p.presence = 'available' AND p.dup_of IS NULL AND NOT EXISTS (
                         SELECT 1 FROM ai_items i JOIN ai_jobs j ON j.id = i.job_id
                         WHERE i.product_id = p.id AND i.status = 'pending' AND j.status IN ('running', 'paused'))""",
                    (supplier_id,))
    queue, manual, done = [], 0, []
    for r in rows:
        p = dict(r)
        if ai.already_done(p, "prepare_prom"):
            done.append(p["id"])
        elif ai.too_little_data(p):
            manual += 1
        else:
            queue.append(p["id"])
    promoted = products.promote_if_ready(done, AUTO_PROMOTE_SOURCE) if done else 0
    if queue and ai.enabled():
        create(queue, "prepare_prom", only_empty=True, auto_promote=True)
    return {"pipeline_promoted": promoted, "pipeline_queued": len(queue) if ai.enabled() else 0, "pipeline_manual": manual}


def _auto_promote(job_item, product_ids: list[int]) -> None:
    """Задание с «сразу в «Готов к отправке»»: черновики, которые после ИИ стали готовы для Prom, — в «Готов»."""
    if not job_item["auto_promote"] or not product_ids:
        return
    with changes_log.user(job_item["user"]):
        moved = products.promote_if_ready(product_ids, AUTO_PROMOTE_SOURCE)
    if moved:
        with db.tx() as c:
            c.execute("UPDATE ai_jobs SET promoted = promoted + ? WHERE id = ?", (moved, job_item["job_id"]))


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
