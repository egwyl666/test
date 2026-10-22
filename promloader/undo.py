"""«↩ Отменить» для быстрых действий со статусами товаров: «В черновики», «Пометить готов», «Вернуть На Prom»,
«В «Готов к отправке»» для готовых черновиков, «Оставить выгодный» у дублей.

Вместо вопроса «Вы уверены?» действие выполняется сразу, а несколько секунд можно нажать «↩ Отменить». Что было до
действия, хранится в памяти программы TTL секунд (после перезапуска отменить нельзя — и не нужно). Отмена
возвращает товар, только если с тех пор его никто не менял: у него тот же статус и то же время изменения, что
поставило действие."""

import secrets
import threading
import time
from datetime import datetime, timezone

from . import changes, db

TTL = 120
_LOCK = threading.Lock()
_STORE: dict[str, dict] = {}


def stamp() -> str:
    """Время изменения для действия с отменой — с микросекундами: правка товара в ту же секунду после действия даёт
    другое время, и отмена её не затрёт."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def snapshot(ids) -> list[dict]:
    """Статус и пометка дубля товаров до действия."""
    if not ids:
        return []
    return [dict(r) for r in db.query(f"SELECT id, status, dup_of FROM products WHERE id {db.IN_LIST}",
                                      (db.as_list(list(ids)),))]


def remember(before: list[dict], stamp: str, label: str) -> str | None:
    """Запомнить «как было» → номер для отмены (None — отменять нечего)."""
    if not before:
        return None
    token = secrets.token_urlsafe(12)
    now = time.monotonic()
    with _LOCK:
        for k in [k for k, v in _STORE.items() if v["expires"] < now]:
            del _STORE[k]
        _STORE[token] = {"before": before, "stamp": stamp, "label": label, "expires": now + TTL}
    return token


def apply(token: str) -> int:
    """Вернуть как было. KeyError — отменять уже поздно (или уже отменили)."""
    with _LOCK:
        entry = _STORE.pop(token, None)
    if entry is None or entry["expires"] < time.monotonic():
        raise KeyError(token)
    from . import products
    restored = 0
    with db.tx() as c:
        for row in entry["before"]:
            now = c.execute("SELECT status FROM products WHERE id = ? AND updated_at = ?",
                            (row["id"], entry["stamp"])).fetchone()
            if now is None or now["status"] in ("sending", "deleting"):
                continue  # товар с тех пор меняли или он уже уходит на Prom — не трогаем
            c.execute("UPDATE products SET status = ?, dup_of = ?, updated_at = ? WHERE id = ?",
                      (row["status"], row["dup_of"], db.now(), row["id"]))
            if now["status"] != row["status"]:
                changes.record(c, row["id"], "status", products.STATUSES.get(now["status"], now["status"]),
                               products.STATUSES.get(row["status"], row["status"]), who=f"Отменено: {entry['label']}")
            restored += 1
    return restored
