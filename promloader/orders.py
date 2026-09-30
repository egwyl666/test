"""Заказы с Prom: периодический опрос /orders/list, хранение у себя, смена статуса, уведомление о новых.

Структура заказа Prom разбирается бережно: неизвестные поля не ломают страницу, исходные данные сохраняются целиком.
"""

import json
import logging
import threading

from . import config, db, notify
from .prom_api import PromError

log = logging.getLogger("promloader.orders")

POLL_MINUTES = 5
PAGE = 100
STATUSES = {
    "pending": "Новый",
    "received": "Принят",
    "delivered": "Выполнен",
    "canceled": "Отменён",
    "paid": "Оплачен",
    "draft": "Черновик",
}
# что пользователь может поставить из программы
SETTABLE = {"received": "Принят", "delivered": "Выполнен", "canceled": "Отменён"}
CANCEL_REASONS = {
    "not_available": "Нет в наличии",
    "price_changed": "Изменилась цена",
    "buyers_request": "По просьбе покупателя",
    "duplicate": "Дубликат заказа",
    "invalid_phone_number": "Неверный телефон",
    "another": "Другое",
}

_lock = threading.Lock()


def _money(value) -> str:
    return "" if value in (None, "") else str(value)


def view(row) -> dict:
    """Заказ для страницы: основные поля + исходные данные."""
    data = json.loads(row["data"])
    client = " ".join(filter(None, [data.get("client_last_name"), data.get("client_first_name"),
                                    data.get("client_second_name")])) or data.get("client_name") or ""
    delivery = data.get("delivery_option") or {}
    payment = data.get("payment_option") or {}
    items = []
    for p in data.get("products") or []:
        ext = p.get("external_id") or ""
        local = db.query_one("SELECT id FROM products WHERE external_id = ?", (ext,)) if ext else None
        items.append({
            "name": p.get("name") or (p.get("name_multilang") or {}).get("ru") or "",
            "quantity": p.get("quantity"), "price": _money(p.get("price")), "total": _money(p.get("total_price")),
            "image": p.get("image") or "", "external_id": ext, "sku": p.get("sku") or "",
            "local_id": local["id"] if local else None,
        })
    status = row["status"]
    return {
        "id": row["id"], "status": status, "status_label": data.get("status_name") or STATUSES.get(status, status),
        "date": row["date_created"], "client": client, "phone": data.get("phone") or "",
        "email": data.get("email") or "", "price": _money(data.get("full_price") or data.get("price")),
        "delivery": delivery.get("name") if isinstance(delivery, dict) else str(delivery or ""),
        "address": data.get("delivery_address") or "",
        "payment": payment.get("name") if isinstance(payment, dict) else str(payment or ""),
        "notes": data.get("client_notes") or "", "source": data.get("source") or "",
        "items": items, "new": not row["seen"],
    }


def list_orders(status: str = "", limit: int = 100, offset: int = 0) -> dict:
    where, args = ("WHERE status = ?", [status]) if status else ("", [])
    rows = db.query(f"SELECT * FROM orders {where} ORDER BY date_created DESC, id DESC LIMIT ? OFFSET ?",
                    args + [limit, offset])
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
    unseen = db.query_one("SELECT COUNT(*) AS n FROM orders WHERE seen = 0")["n"]
    return {"items": [view(r) for r in rows], "counts": counts, "unseen": unseen,
            "last_poll": db.get_setting("orders_last_poll"), "error": db.get_setting("orders_error")}


def mark_seen(ids: list[int] | None = None) -> None:
    with db.tx() as c:
        if ids:
            c.executemany("UPDATE orders SET seen = 1 WHERE id = ?", [(i,) for i in ids])
        else:
            c.execute("UPDATE orders SET seen = 1 WHERE seen = 0")


def _notify_text(v: dict) -> str:
    lines = [f"🛒 <b>Новый заказ №{v['id']}</b> на {notify.esc(v['price'])}"]
    if v["client"] or v["phone"]:
        lines.append(f"{notify.esc(v['client'])} {notify.esc(v['phone'])}".strip())
    for item in v["items"][:5]:
        lines.append(f"• {notify.esc(item['name'])} × {item['quantity']}")
    if v["delivery"]:
        lines.append(f"Доставка: {notify.esc(v['delivery'])}")
    return "\n".join(lines)


def store(orders: list[dict], notify_new: bool = True) -> int:
    """Сохраняет заказы. Возвращает, сколько новых. first_run — старые заказы не считаются «новыми»."""
    first_run = db.get_setting("orders_initialized") != "1"
    new = []
    ts = db.now()
    with db.tx() as c:
        for o in orders:
            if not isinstance(o, dict) or o.get("id") is None:
                continue
            exists = c.execute("SELECT 1 FROM orders WHERE id = ?", (o["id"],)).fetchone()
            c.execute(
                """INSERT INTO orders (id, status, date_created, data, seen, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data, updated_at = excluded.updated_at""",
                (o["id"], str(o.get("status") or ""), str(o.get("date_created") or ""),
                 json.dumps(o, ensure_ascii=False), 1 if first_run else 0, ts),
            )
            if not exists and not first_run:
                new.append(o["id"])
    db.set_setting("orders_initialized", "1")
    if notify_new:
        for oid in new:
            notify.send("order", _notify_text(view(db.query_one("SELECT * FROM orders WHERE id = ?", (oid,)))))
    return len(new)


async def poll(client) -> int:
    """Забирает последние заказы (одна-две страницы достаточно для опроса раз в 5 минут)."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        body = await client.list_orders(limit=PAGE)
        new = store(body.get("orders") or [])
        db.set_setting("orders_last_poll", db.now())
        db.set_setting("orders_error", "")
        return new
    except PromError as exc:
        db.set_setting("orders_error", str(exc))
        raise
    finally:
        _lock.release()


async def set_status(client, order_id: int, status: str, reason: str = "", text: str = "") -> dict:
    if status not in SETTABLE:
        raise PromError("Такой статус из программы поставить нельзя")
    if status == "canceled" and reason not in CANCEL_REASONS:
        raise PromError("Укажите причину отмены")
    await client.set_order_status([order_id], status, reason if status == "canceled" else "",
                                  text if status == "canceled" else "")
    row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if row is not None:
        data = json.loads(row["data"])
        data["status"] = status
        data.pop("status_name", None)
        with db.tx() as c:
            c.execute("UPDATE orders SET status = ?, data = ?, seen = 1, updated_at = ? WHERE id = ?",
                      (status, json.dumps(data, ensure_ascii=False), db.now(), order_id))
        row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return view(row)
    return {"id": order_id, "status": status}


def enabled() -> bool:
    return bool(config.get("prom_token")) and db.get_setting("orders_enabled") != "0"
