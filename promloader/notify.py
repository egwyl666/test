"""Уведомления в Telegram (по желанию).

Несколько получателей: у каждого свой набор событий (например, владелец получает заказы и ошибки,
менеджер — только отчёты о выгрузке). Подключение: бот у @BotFather, его токен — в «Настройках»,
получатель пишет боту любое сообщение, программа находит чат через getUpdates.
Отдельный бот поддержки (см. support.py) используется только для обращений к разработчику.
"""

import html
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db

log = logging.getLogger("promloader.notify")

API = "https://api.telegram.org"
EVENTS = {
    "order": "новые заказы с Prom",
    "sync_done": "выгрузка товаров на Prom выполнена",
    "sync_failed": "ошибки отправки на Prom",
    "supplier_failed": "сбои обновления поставщиков",
    "schedule_missed": "пропущенная выгрузка по расписанию",
    "chat": "новые сообщения покупателей в чате Prom",
    "phone": "новый адрес для входа с телефона",
    "parcel_arrived": "посылка прибыла в отделение Новой Почты",
    "parcel_waiting": "посылку долго не забирают из отделения",
    "parcel_refused": "отказ от посылки или возврат",
    "daily_summary": "сводка за день: заказы, выручка, прибыль",
    "ai_bulk": "массовый ИИ завершён или остановлен",
    "margin_alert": "товар продаётся в минус или ниже минимальной наценки",
    "prom_sync": "синхронизация с Prom: товары добавили или удалили прямо в кабинете",
    "parcel_waiting_alert": "посылка ждёт на отделении 3+ дня, а покупателю ещё не напомнили",
    "low_stock": "товар заканчивается: остаток не больше порога",
}
DEFAULT_EVENTS = ["order", "chat", "sync_failed", "supplier_failed", "schedule_missed", "phone", "parcel_waiting",
                  "parcel_refused", "daily_summary", "ai_bulk", "margin_alert", "prom_sync", "parcel_waiting_alert", "low_stock"]


class NotifyError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def permanent(self) -> bool:
        """Telegram не примет это сообщение никогда (400 — плохой текст): повторять бессмысленно, а повторы держали
        бы всю очередь получателя — следующие уведомления приходили через двое суток пачкой."""
        return self.status == 400


def token() -> str:
    return config.get("telegram_token")


# ---------- низкоуровневые вызовы (общие с ботом поддержки) ----------

def call(bot_token: str, method: str, transport=None, files=None, **payload) -> dict:
    if not bot_token:
        raise NotifyError("Не указан токен бота Telegram")
    try:
        with httpx.Client(base_url=f"{API}/bot{bot_token}", timeout=60, transport=transport) as client:
            if files:
                r = client.post(f"/{method}", data={k: str(v) for k, v in payload.items()}, files=files)
            else:
                r = client.post(f"/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise NotifyError(f"Нет связи с Telegram: {exc}")
    try:
        body = r.json()
    except ValueError:
        raise NotifyError(f"Telegram ответил странно (HTTP {r.status_code})")
    if not body.get("ok"):
        if r.status_code == 401:
            raise NotifyError("Telegram не принял токен бота — проверьте, что скопировали его целиком")
        if r.status_code == 403:
            raise NotifyError("Получатель заблокировал бота или не нажал «Старт»")
        raise NotifyError(f"Telegram: {body.get('description', 'ошибка')}", r.status_code,
                          (body.get("parameters") or {}).get("retry_after"))
    return body


def _chat_name(chat: dict) -> str:
    return chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
        or (("@" + chat["username"]) if chat.get("username") else "") or str(chat.get("id"))


def recent_chats(bot_token: str, transport=None) -> list[dict]:
    """Чаты, которые недавно писали боту (кандидаты в получатели)."""
    body = call(bot_token, "getUpdates", transport)
    found = {}
    for update in body.get("result") or []:
        message = update.get("message") or update.get("my_chat_member") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            found[str(chat["id"])] = {"chat_id": str(chat["id"]), "name": _chat_name(chat)}
    return list(found.values())


# ---------- получатели ----------

def _migrate_single_chat() -> None:
    """Версии до 1.2 хранили одного получателя — переносим его в список."""
    old = db.get_setting("telegram_chat_id")
    if old:
        events = db.get_setting("telegram_events")
        chosen = [e for e in events.split(",") if e in EVENTS] if events else DEFAULT_EVENTS
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO tg_recipients (chat_id, name, events, created_at) VALUES (?, ?, ?, ?)",
                      (old, db.get_setting("telegram_chat_name") or old, json.dumps(chosen), db.now()))
        db.set_setting("telegram_chat_id", "")


# события, появившиеся после того, как получатели уже были добавлены: включаем их всем один раз
# (иначе новый адрес для телефона не приходил тем, кто подключил Telegram раньше)
NEW_EVENTS = {1: ["phone", "parcel_waiting", "parcel_refused"], 2: ["daily_summary"], 3: ["ai_bulk"], 4: ["margin_alert"], 5: ["prom_sync"],
              6: ["parcel_waiting_alert"], 7: ["low_stock"]}


def _add_new_events() -> None:
    done = int(db.get_setting("tg_events_version") or 0)
    latest = max(NEW_EVENTS)
    if done >= latest:
        return
    with db.tx() as c:
        for r in c.execute("SELECT id, events FROM tg_recipients").fetchall():
            events = json.loads(r["events"])
            for version, names in NEW_EVENTS.items():
                if version > done:
                    events += [e for e in names if e not in events]
            c.execute("UPDATE tg_recipients SET events = ? WHERE id = ?", (json.dumps(events), r["id"]))
        db.set_setting("tg_events_version", str(latest))


def recipients() -> list[dict]:
    _migrate_single_chat()
    _add_new_events()
    return [{**dict(r), "events": json.loads(r["events"])}
            for r in db.query("SELECT * FROM tg_recipients ORDER BY id")]


def add_recipient(chat_id: str, name: str = "", events: list[str] | None = None) -> dict:
    chat_id = str(chat_id).strip()
    if not chat_id.lstrip("-").isdigit():
        raise NotifyError("ID чата — это число (например 123456789 или -100123… для группы)")
    chosen = [e for e in (events if events is not None else DEFAULT_EVENTS) if e in EVENTS]
    with db.tx() as c:
        db.set_setting("tg_events_version", str(max(NEW_EVENTS)))  # у нового получателя события уже свежие
        c.execute("""INSERT INTO tg_recipients (chat_id, name, events, created_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name""",
                  (chat_id, name.strip() or chat_id, json.dumps(chosen), db.now()))
    return next(r for r in recipients() if r["chat_id"] == chat_id)


def update_recipient(rid: int, events: list[str] | None = None, name: str | None = None) -> None:
    fields = {}
    if events is not None:
        fields["events"] = json.dumps([e for e in events if e in EVENTS])
    if name is not None:
        fields["name"] = name.strip()
    if fields:
        with db.tx() as c:
            c.execute(f"UPDATE tg_recipients SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      list(fields.values()) + [rid])


def remove_recipient(rid: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM tg_recipients WHERE id = ?", (rid,))


def ready() -> bool:
    return bool(token() and recipients())


# ---------- отправка ----------

def send_to(chat_id: str, text: str, transport=None) -> None:
    try:
        call(token(), "sendMessage", transport, chat_id=chat_id, text=text[:4000], parse_mode="HTML",
             disable_web_page_preview=True)
    except NotifyError as exc:
        if not exc.permanent:
            raise
        # разметка не разобралась (символ «<» в имени, адресе) — то же сообщение простым текстом, без разметки
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
        call(token(), "sendMessage", transport, chat_id=chat_id, text=plain, disable_web_page_preview=True)


RETRY_SECONDS = (60, 120, 300, 900, 1800, 3600)
GIVE_UP_HOURS = 48  # дольше двух суток уведомление о заказе уже бесполезно
KEEP_DAYS = 30
_flush_lock = threading.Lock()


def _later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def send(event: str, text: str, background: bool = True) -> None:
    """Уведомление всем, кто подписан на событие: в очередь и сразу попытка отправить. Не дошло (нет интернета,
    Telegram недоступен) — программа повторит сама (раньше такое уведомление терялось). Не бросает исключений.

    Внутри пробного прогона («Что изменится») запись в очередь откатывается вместе со всем остальным.
    Заодно — push-уведомление на телефоны с приложением Prom Loader (webpush), если они подписаны на событие."""
    try:
        from . import webpush
        webpush.send_event_background(event, text)
    except Exception:
        log.exception("Push-уведомление «%s» не отправлено", event)
    try:
        if not token():
            return
        targets = [r["chat_id"] for r in recipients() if event in r["events"]]
        if not targets:
            return
        now = db.now()
        with db.tx() as c:
            c.executemany("INSERT INTO tg_outbox (chat_id, event, text, created_at, next_at) VALUES (?, ?, ?, ?, ?)",
                          [(chat_id, event, text, now, now) for chat_id in targets])
    except Exception:
        log.exception("Уведомление «%s» не поставлено в очередь", event)
        return
    if background:
        threading.Thread(target=flush, daemon=True).start()
    else:
        flush()


def flush(transport=None) -> int:
    """Отправить очередь. Сообщения одному получателю уходят по порядку: если одно не дошло, следующие ему ждут.
    Возвращает, сколько отправлено."""
    if not _flush_lock.acquire(blocking=False):
        return 0  # уже отправляет другой поток
    try:
        sent = 0
        for _ in range(5):  # что поставили в очередь, пока отправляли, — следующим кругом, а не через минуту
            done = _flush_round(transport)
            sent += done
            if not done:
                break
        old = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        with db.tx() as c:
            c.execute("DELETE FROM tg_outbox WHERE (sent_at IS NOT NULL OR failed = 1) AND created_at < ?", (old,))
        return sent
    finally:
        _flush_lock.release()


def _flush_round(transport=None) -> int:
    now = db.now()
    rows = db.query("SELECT * FROM tg_outbox WHERE sent_at IS NULL AND failed = 0 ORDER BY id LIMIT 500")
    waiting, sent = set(), 0
    for r in rows:
        if r["chat_id"] in waiting:
            continue
        if r["next_at"] > now:
            waiting.add(r["chat_id"])
            continue
        try:
            send_to(r["chat_id"], r["text"], transport)
        except NotifyError as exc:
            if exc.permanent:  # не держим очередь: это сообщение — в «не доставлено», следующие идут дальше
                log.warning("Уведомление Telegram для %s отклонено: %s", r["chat_id"], exc)
                with db.tx() as c:
                    c.execute("UPDATE tg_outbox SET failed = 1, last_error = ?, attempts = attempts + 1 WHERE id = ?",
                              (str(exc), r["id"]))
                continue
            waiting.add(r["chat_id"])
            _retry_later(r, str(exc), exc.retry_after)
            continue
        with db.tx() as c:
            c.execute("UPDATE tg_outbox SET sent_at = ?, last_error = '' WHERE id = ?", (db.now(), r["id"]))
        sent += 1
    return sent


def _retry_later(row, error: str, retry_after: int | None = None) -> None:
    attempts = row["attempts"] + 1
    created = datetime.fromisoformat(row["created_at"])
    give_up = datetime.now(timezone.utc) - created > timedelta(hours=GIVE_UP_HOURS)
    log.warning("Уведомление Telegram для %s не отправлено (попытка %d): %s", row["chat_id"], attempts, error)
    with db.tx() as c:
        c.execute("UPDATE tg_outbox SET attempts = ?, next_at = ?, last_error = ?, failed = ? WHERE id = ?",
                  (attempts, _later(retry_after or RETRY_SECONDS[min(attempts - 1, len(RETRY_SECONDS) - 1)]), error,
                   1 if give_up else 0, row["id"]))


def outbox_state() -> dict:
    """Для «Настроек»: сколько уведомлений ждут повтора и почему."""
    pending = db.query_one("SELECT COUNT(*) AS n FROM tg_outbox WHERE sent_at IS NULL AND failed = 0")["n"]
    stuck = db.query_one("SELECT last_error, next_at FROM tg_outbox WHERE sent_at IS NULL AND failed = 0 "
                         "AND attempts > 0 ORDER BY id LIMIT 1")
    failed = db.query_one("SELECT COUNT(*) AS n FROM tg_outbox WHERE failed = 1")["n"]
    return {"pending": pending, "failed": failed, "error": stuck["last_error"] if stuck else "",
            "next_at": stuck["next_at"] if stuck else ""}


def esc(value) -> str:
    return html.escape(str(value or ""))
