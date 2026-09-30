"""Уведомления в Telegram (по желанию).

Несколько получателей: у каждого свой набор событий (например, владелец получает заказы и ошибки,
менеджер — только отчёты о выгрузке). Подключение: бот у @BotFather, его токен — в «Настройках»,
получатель пишет боту любое сообщение, программа находит чат через getUpdates.
Отдельный бот поддержки (см. support.py) используется только для обращений к разработчику.
"""

import html
import json
import logging
import threading

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
}
DEFAULT_EVENTS = ["order", "sync_failed", "supplier_failed", "schedule_missed"]


class NotifyError(Exception):
    pass


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
        raise NotifyError(f"Telegram: {body.get('description', 'ошибка')}")
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


def recipients() -> list[dict]:
    _migrate_single_chat()
    return [{**dict(r), "events": json.loads(r["events"])}
            for r in db.query("SELECT * FROM tg_recipients ORDER BY id")]


def add_recipient(chat_id: str, name: str = "", events: list[str] | None = None) -> dict:
    chat_id = str(chat_id).strip()
    if not chat_id.lstrip("-").isdigit():
        raise NotifyError("ID чата — это число (например 123456789 или -100123… для группы)")
    chosen = [e for e in (events if events is not None else DEFAULT_EVENTS) if e in EVENTS]
    with db.tx() as c:
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
    call(token(), "sendMessage", transport, chat_id=chat_id, text=text, parse_mode="HTML",
         disable_web_page_preview=True)


def send(event: str, text: str, background: bool = True) -> None:
    """Отправить уведомление всем, кто подписан на событие. Никогда не бросает исключений."""
    if not token():
        return
    targets = [r["chat_id"] for r in recipients() if event in r["events"]]
    if not targets:
        return

    def go():
        for chat_id in targets:
            try:
                send_to(chat_id, text)
            except NotifyError as exc:
                log.warning("Уведомление Telegram для %s не отправлено: %s", chat_id, exc)

    if background:
        threading.Thread(target=go, daemon=True).start()
    else:
        go()


def esc(value) -> str:
    return html.escape(str(value or ""))
