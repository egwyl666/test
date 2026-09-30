"""Уведомления в Telegram (по желанию): новые заказы и проблемы, о которых нужно знать сразу.

Подключение: бот создаётся у @BotFather, его токен вставляется в «Настройках», пользователь пишет боту
любое сообщение — программа находит чат через getUpdates. Ошибки отправки уведомлений не мешают работе.
"""

import html
import logging
import threading

import httpx

from . import config, db

log = logging.getLogger("promloader.notify")

API = "https://api.telegram.org"
EVENTS = {
    "order": "новые заказы с Prom",
    "sync_failed": "ошибки отправки на Prom",
    "supplier_failed": "сбои обновления поставщиков",
    "schedule_missed": "пропущенная выгрузка по расписанию",
}


class NotifyError(Exception):
    pass


def token() -> str:
    return config.get("telegram_token")


def chat_id() -> str:
    return db.get_setting("telegram_chat_id")


def enabled_events() -> set[str]:
    raw = db.get_setting("telegram_events")
    return set(raw.split(",")) if raw else set(EVENTS)


def ready() -> bool:
    return bool(token() and chat_id())


def _client(transport=None) -> httpx.Client:
    return httpx.Client(base_url=f"{API}/bot{token()}", timeout=20, transport=transport)


def _call(method: str, transport=None, **payload) -> dict:
    if not token():
        raise NotifyError("Не указан токен бота Telegram")
    try:
        with _client(transport) as client:
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
        raise NotifyError(f"Telegram: {body.get('description', 'ошибка')}")
    return body


def find_chat(transport=None) -> dict:
    """Ищет последний чат, где пользователь написал боту. Сохраняет его."""
    body = _call("getUpdates", transport)
    for update in reversed(body.get("result") or []):
        message = update.get("message") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            db.set_setting("telegram_chat_id", str(chat["id"]))
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                or chat.get("username") or str(chat["id"])
            db.set_setting("telegram_chat_name", name)
            return {"id": chat["id"], "name": name}
    raise NotifyError("Бот пока не получил ни одного сообщения. Откройте своего бота в Telegram, нажмите «Старт» "
                      "или напишите ему что-нибудь — и нажмите «Найти чат» ещё раз")


def send_now(text: str, transport=None) -> None:
    if not chat_id():
        raise NotifyError("Сначала нажмите «Найти чат»")
    _call("sendMessage", transport, chat_id=chat_id(), text=text, parse_mode="HTML", disable_web_page_preview=True)


def send(event: str, text: str, background: bool = True) -> None:
    """Отправить уведомление, если Telegram подключён и это событие включено. Никогда не бросает исключений."""
    if not ready() or event not in enabled_events():
        return

    def go():
        try:
            send_now(text)
        except NotifyError as exc:
            log.warning("Уведомление Telegram не отправлено: %s", exc)

    if background:
        threading.Thread(target=go, daemon=True).start()
    else:
        go()


def esc(value) -> str:
    return html.escape(str(value or ""))
