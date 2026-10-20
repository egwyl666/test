"""Чат с покупателями Prom: копия комнат и сообщений в программе, ответ из программы, черновик ИИ.

Prom отдаёт комнаты (`/chat/rooms`) и историю комнаты (`/chat/messages_history?room_ident=`). Сообщение — это либо
текст (`type = message`), либо «контекст»: карточка товара или заказа, о котором пишет покупатель (`context_item_*`).
`is_sender = true` — сообщение магазина. Ответ уходит на `/chat/send_message` с `user_id` из `room_ident`
(`{user_id}_{company_id}_buyer`).

Опрос раз в минуту: историю тянем только у комнат, где поменялось последнее сообщение. Новое сообщение покупателя —
уведомление в Telegram (событие «chat»). При самом первом опросе уведомлений нет: это старая переписка.
"""

import asyncio
import json
import logging
import re
import threading
from datetime import datetime

from . import config, db, notify

log = logging.getLogger("promloader.chat")

POLL_SECONDS = 60
HISTORY_LIMIT = 100
MAX_BODY = 4000          # Prom режет длинные сообщения; больше — признак вставленного по ошибке текста
TEXT_TYPES = ("message", "attachment")  # то, что написал человек; context — карточка товара или заказа
_lock = threading.Lock()


class ChatError(Exception):
    pass


def enabled() -> bool:
    return bool(config.get("prom_token"))


def _user_id(room_ident: str) -> str:
    return (room_ident or "").split("_")[0]


def _product_by_prom(c, prom_id) -> int | None:
    if not prom_id:
        return None
    row = c.execute("SELECT id FROM products WHERE prom_id = ?", (int(prom_id),)).fetchone()
    return row["id"] if row else None


# ---------- запись ----------

def _files_text(m: dict) -> str:
    """У сообщения-вложения текста нет — в списке диалогов показываем «📎 имя файла»."""
    names = [a.get("attachment_name") or "" for a in (m.get("attachments") or []) if isinstance(a, dict)]
    return ("📎 " + ", ".join(n for n in names if n)) if names else ""


def _store(room: dict, messages: list[dict], first_run: bool) -> list[dict]:
    """Записать комнату и её сообщения. Вернуть новые сообщения покупателя (для уведомлений)."""
    ident = room.get("ident") or room.get("room_ident") or ""
    new_from_buyer = []
    with db.tx() as c:
        product_id = prom_product = order_id = None
        buyer, phone = "", ""
        for m in sorted(messages, key=lambda x: (x.get("date_sent") or "", x.get("id") or 0)):
            if m.get("id") is None:
                continue
            mine = bool(m.get("is_sender"))
            if not mine and not buyer:
                buyer, phone = m.get("user_name") or "", m.get("user_phone") or ""
            ctx_type, ctx_id = m.get("context_item_type") or "", m.get("context_item_id")
            msg_product = None
            if ctx_type == "order" and ctx_id:
                order_id = int(ctx_id)
            elif ctx_type and ctx_id:  # карточка товара
                prom_product = int(ctx_id)
                msg_product = product_id = _product_by_prom(c, ctx_id) or product_id
            exists = c.execute("SELECT 1 FROM chat_messages WHERE id = ?", (m["id"],)).fetchone()
            c.execute("""INSERT INTO chat_messages (id, room_ident, sender, body, is_my, created_at, type, status, product_id,
                                                    attachments, data)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(id) DO UPDATE SET body = excluded.body, status = excluded.status, data = excluded.data""",
                      (m["id"], ident, m.get("user_name") or "", m.get("body") or _files_text(m), 1 if mine else 0,
                       m.get("date_sent") or "", m.get("type") or "message", m.get("status") or "", msg_product,
                       json.dumps(m.get("attachments") or [], ensure_ascii=False), json.dumps(m, ensure_ascii=False)))
            if not exists and not mine and not first_run and (m.get("type") or "message") in TEXT_TYPES:
                new_from_buyer.append(m)
        # порядок — по номеру сообщения Prom: время Prom приходит без пояса, номера растут
        last = c.execute("""SELECT body, created_at, id FROM chat_messages WHERE room_ident = ? AND type IN ('message', 'attachment')
                            ORDER BY id DESC LIMIT 1""", (ident,)).fetchone()
        unread = c.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE room_ident = ? AND is_my = 0 "
                           "AND type IN ('message', 'attachment') "
                           "AND status != 'read'", (ident,)).fetchone()["n"]
        old = c.execute("SELECT buyer_name, buyer_phone, product_id, prom_product_id, order_id FROM chat_rooms "
                        "WHERE room_ident = ?", (ident,)).fetchone()
        old = dict(old) if old else {}
        c.execute("""INSERT INTO chat_rooms (room_ident, room_id, project, buyer_name, buyer_phone, last_message, last_at,
                                             last_message_id, unread, product_id, prom_product_id, order_id, data, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(room_ident) DO UPDATE SET room_id = excluded.room_id, project = excluded.project,
                       buyer_name = excluded.buyer_name, buyer_phone = excluded.buyer_phone,
                       last_message = excluded.last_message, last_at = excluded.last_at,
                       last_message_id = excluded.last_message_id, unread = excluded.unread,
                       product_id = excluded.product_id, prom_product_id = excluded.prom_product_id,
                       order_id = excluded.order_id, data = excluded.data, updated_at = excluded.updated_at""",
                  (ident, room.get("id") or "", room.get("project") or "promua",
                   buyer or old.get("buyer_name") or "", phone or old.get("buyer_phone") or "",
                   (last["body"] if last else "")[:300], (last["created_at"] if last else room.get("date_sent") or ""),
                   room.get("last_message_id"), unread, product_id or old.get("product_id"),
                   prom_product or old.get("prom_product_id"), order_id or old.get("order_id"),
                   json.dumps(room, ensure_ascii=False), db.now()))
    return new_from_buyer


def _notify(room_ident: str, messages: list[dict]) -> None:
    room = db.query_one("SELECT buyer_name FROM chat_rooms WHERE room_ident = ?", (room_ident,))
    who = notify.esc((room["buyer_name"] if room else "") or "Покупатель")
    for m in messages[:3]:
        notify.send("chat", f"💬 <b>{who}</b> пишет в чате Prom:\n{notify.esc((m.get('body') or '')[:500])}")
    if len(messages) > 3:
        notify.send("chat", f"💬 <b>{who}</b>: ещё {len(messages) - 3} сообщений — откройте «Чат» в программе.")


async def poll(client) -> int:
    """Забрать новое с Prom. Возвращает, сколько новых сообщений покупателей."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        first_run = not db.get_setting("chat_initialized")
        known = {r["room_ident"]: r["last_message_id"] for r in db.query("SELECT room_ident, last_message_id FROM chat_rooms")}
        total = 0
        for room in await client.chat_rooms(limit=100):
            ident = room.get("ident") or ""
            if not ident or (ident in known and known[ident] == room.get("last_message_id")):
                continue  # в комнате ничего не поменялось — историю не запрашиваем
            messages = await client.chat_messages(ident, HISTORY_LIMIT)
            new = await asyncio.to_thread(_store, room, messages, first_run)
            if new:
                total += len(new)
                await asyncio.to_thread(_notify, ident, new)
        db.set_setting("chat_initialized", "1")
        db.set_setting("chat_error", "")
        db.set_setting("chat_last_poll", db.now())
        return total
    except Exception as exc:
        db.set_setting("chat_error", str(exc))
        raise
    finally:
        _lock.release()


# ---------- чтение ----------

def unseen() -> int:
    return db.query_one("SELECT COALESCE(SUM(unread), 0) AS n FROM chat_rooms")["n"]


def _product_card(product_id) -> dict | None:
    if not product_id:
        return None
    p = db.query_one("""SELECT p.id, p.name, p.price, p.currency, p.presence, p.external_id,
                               (SELECT COALESCE(i.url, '/media/' || i.file) FROM images i WHERE i.product_id = p.id
                                ORDER BY i.position, i.id LIMIT 1) AS thumb
                        FROM products p WHERE p.id = ?""", (product_id,))
    return dict(p) if p else None


def rooms(q: str = "", unread_only: bool = False) -> list[dict]:
    where, args = [], []
    if unread_only:
        where.append("unread > 0")
    if q.strip():
        where.append("(lower_u(buyer_name) LIKE ? OR lower_u(last_message) LIKE ? OR buyer_phone LIKE ? "
                     "OR room_ident IN (SELECT room_ident FROM chat_messages WHERE lower_u(body) LIKE ?))")
        like = f"%{q.strip().lower()}%"
        args += [like, like, f"%{q.strip()}%", like]
    rows = db.query(f"SELECT * FROM chat_rooms {'WHERE ' + ' AND '.join(where) if where else ''} "
                    "ORDER BY last_at DESC", args)
    out = []
    for r in rows:
        d = {k: r[k] for k in ("room_ident", "buyer_name", "buyer_phone", "last_message", "last_at", "unread",
                               "product_id", "order_id")}
        d["product"] = _product_card(r["product_id"])
        out.append(d)
    return out


def messages(room_ident: str) -> dict:
    room = db.query_one("SELECT * FROM chat_rooms WHERE room_ident = ?", (room_ident,))
    if room is None:
        raise KeyError(room_ident)
    msgs = []
    for m in db.query("SELECT * FROM chat_messages WHERE room_ident = ? ORDER BY id", (room_ident,)):
        d = {k: m[k] for k in ("id", "sender", "body", "is_my", "created_at", "type", "status", "product_id")}
        d["attachments"] = json.loads(m["attachments"] or "[]")
        data = json.loads(m["data"] or "{}")
        d["context_type"], d["context_id"] = data.get("context_item_type") or "", data.get("context_item_id")
        d["context_image"] = data.get("context_item_image_url") or ""
        d["product"] = _product_card(m["product_id"]) if m["type"] == "context" else None
        msgs.append(d)
    order = db.query_one("SELECT id, status FROM orders WHERE id = ?", (room["order_id"],)) if room["order_id"] else None
    return {"room": {k: room[k] for k in ("room_ident", "buyer_name", "buyer_phone", "unread", "order_id", "product_id")},
            "product": _product_card(room["product_id"]), "order": dict(order) if order else None, "messages": msgs,
            "quick": quick_replies(room_ident)}


# ---------- факты для ответа: товар, заказ, посылка ----------

QUICK_LABELS = {"stock": "✅ Наличие и цена", "ttn": "📦 Номер ТТН и статус", "order": "📋 Статус заказа",
                "check": "⏳ Уточню и напишу"}


def _order_for_room(room) -> dict | None:
    """Заказ этого покупателя: привязанный к чату, иначе — последний заказ с тем же телефоном."""
    if room["order_id"]:
        row = db.query_one("SELECT id, status, data FROM orders WHERE id = ?", (room["order_id"],))
        if row:
            return dict(row)
    phone = _digits(room["buyer_phone"])
    if len(phone) < 9:
        return None
    for row in db.query("SELECT id, status, data FROM orders ORDER BY date_created DESC LIMIT 500"):
        if _digits(json.loads(row["data"] or "{}").get("phone")) == phone:
            return dict(row)
    return None


def _facts(room_ident: str) -> dict:
    """Что программа точно знает: товар (наличие, цена), заказ (статус), посылка (ТТН, где она)."""
    from . import novaposhta, orders
    room = db.query_one("SELECT * FROM chat_rooms WHERE room_ident = ?", (room_ident,))
    if room is None:
        raise KeyError(room_ident)
    facts: dict = {}
    if room["product_id"]:
        p = db.query_one("SELECT name, name_ua, price, currency, presence, quantity FROM products WHERE id = ?",
                         (room["product_id"],))
        if p:
            facts["product"] = dict(p)
    order = _order_for_room(room)
    if order:
        facts["order"] = {"id": order["id"], "status": order["status"],
                          "status_label": orders.STATUSES.get(order["status"], order["status"])}
        s = novaposhta.shipment(order["id"])
        if s:
            facts["shipment"] = {"ttn": s["ttn"], "stage": s.get("stage") or "", "status": s.get("status") or ""}
    return facts


STAGE_UA = {"created": "ТТН створено, посилка готується до відправлення", "on_the_way": "посилка в дорозі",
            "arrived": "посилка вже у відділенні", "received": "посилку отримано", "refused": "відмова / повернення"}
ORDER_UA = {"pending": "отримали, скоро підтвердимо", "received": "прийнято, готуємо до відправлення",
            "delivered": "виконано", "canceled": "скасовано", "paid": "оплачено, готуємо до відправлення"}


def quick_replies(room_ident: str) -> list[dict]:
    """Готовые ответы без ИИ — только из настоящих данных и всегда по-украински (решение продавца)."""
    try:
        f = _facts(room_ident)
    except KeyError:
        return []
    out = []
    p = f.get("product")
    if p:
        name = p["name_ua"] or p["name"] or ""
        if p["presence"] == "available":
            price = f", ціна {p['price']:g} грн." if p["price"] else "."
            out.append({"key": "stock", "text": f"Вітаємо! «{name}» є в наявності{price}"})
        else:
            out.append({"key": "stock", "text": f"Вітаємо! На жаль, «{name}» зараз немає в наявності."})
    o, s = f.get("order"), f.get("shipment")
    if s:
        stage = STAGE_UA.get(s["stage"], s["status"])
        out.append({"key": "ttn", "text": f"Ваше замовлення #{o['id']} відправлено Новою Поштою. Номер ТТН: {s['ttn']}"
                                          + (f" ({stage})." if stage else ".")})
    elif o:
        out.append({"key": "order", "text": f"Ваше замовлення #{o['id']}: {ORDER_UA.get(o['status'], o['status_label'])}."})
    out.append({"key": "check", "text": "Зараз уточню і напишу вам."})
    return [{**x, "label": QUICK_LABELS[x["key"]]} for x in out]


# ---------- действия ----------

async def send(client, room_ident: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ChatError("Напишите текст ответа")
    if len(text) > MAX_BODY:
        raise ChatError(f"Слишком длинное сообщение — не больше {MAX_BODY} символов")
    room = db.query_one("SELECT room_ident, project FROM chat_rooms WHERE room_ident = ?", (room_ident,))
    if room is None:
        raise KeyError(room_ident)
    user_id = _user_id(room_ident)
    if not user_id.isdigit():
        raise ChatError("Не удалось определить покупателя этого чата — обновите страницу")
    result = await client.chat_send(user_id, text, room["project"] or "promua")
    message_id = result.get("message_id")
    # время — как у Prom: местное, без пояса (иначе свой ответ встал бы в списке не на своё место)
    now = datetime.now().isoformat(timespec="seconds")
    with db.tx() as c:
        if message_id:  # своё сообщение видно сразу, не дожидаясь следующего опроса
            c.execute("""INSERT OR IGNORE INTO chat_messages (id, room_ident, sender, body, is_my, created_at, type, status)
                         VALUES (?, ?, '', ?, 1, ?, 'message', '')""", (int(message_id), room_ident, text, now))
        c.execute("UPDATE chat_rooms SET last_message = ?, last_at = ?, updated_at = ? WHERE room_ident = ?",
                  (text[:300], now, db.now(), room_ident))
    return {"message_id": message_id}


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]  # 0501234567 и +380501234567 — один номер


def room_for_order(order_id: int) -> str | None:
    """Чат покупателя этого заказа: комната, привязанная к заказу, иначе — комната с тем же телефоном.
    Написать покупателю первым Prom не даёт: без чата сообщение не отправить."""
    row = db.query_one("SELECT room_ident FROM chat_rooms WHERE order_id = ? ORDER BY last_at DESC LIMIT 1", (order_id,))
    if row:
        return row["room_ident"]
    order = db.query_one("SELECT data FROM orders WHERE id = ?", (order_id,))
    phone = _digits(json.loads(order["data"] or "{}").get("phone")) if order else ""
    if len(phone) < 9:
        return None
    for r in db.query("SELECT room_ident, buyer_phone FROM chat_rooms WHERE buyer_phone != '' ORDER BY last_at DESC"):
        if _digits(r["buyer_phone"]) == phone:
            return r["room_ident"]
    return None


def ttn_message(order_id: int, ttn: str) -> str:
    """Текст покупателю — по-украински (язык покупателей Prom)."""
    return f"Вітаємо! Ваше замовлення #{order_id} відправлено Новою Поштою. Номер ТТН: {ttn}"


async def send_ttn(client, order_id: int, ttn: str) -> dict:
    room = room_for_order(order_id)
    if not room:
        raise ChatError("У покупателя этого заказа нет чата с магазином — Prom не даёт написать первым")
    return await send(client, room, ttn_message(order_id, ttn))


async def mark_read(client, room_ident: str) -> int:
    """Отметить прочитанными сообщения покупателя в комнате (на Prom — последнее, этого достаточно)."""
    room = db.query_one("SELECT room_id FROM chat_rooms WHERE room_ident = ?", (room_ident,))
    if room is None:
        raise KeyError(room_ident)
    last = db.query_one("""SELECT id FROM chat_messages WHERE room_ident = ? AND is_my = 0 AND type IN ('message', 'attachment')
                           AND status != 'read' ORDER BY id DESC LIMIT 1""", (room_ident,))
    if last and room["room_id"]:
        await client.chat_mark_read(room["room_id"], last["id"])
    with db.tx() as c:
        n = c.execute("UPDATE chat_messages SET status = 'read' WHERE room_ident = ? AND is_my = 0 AND status != 'read'",
                      (room_ident,)).rowcount
        c.execute("UPDATE chat_rooms SET unread = 0 WHERE room_ident = ?", (room_ident,))
    return n


def ai_draft(room_ident: str) -> dict:
    """Черновик ответа ИИ по переписке и товару. Ничего не отправляет."""
    from . import ai, aiprice

    data = messages(room_ident)
    lines = []
    p = data["product"]
    if p:
        full = db.query_one("SELECT name, price, currency, presence, quantity, params, description FROM products "
                            "WHERE id = ?", (p["id"],))
        params = "; ".join(f"{x.get('name')}: {x.get('value')}" for x in json.loads(full["params"] or "[]") if x.get("name"))
        qty = f", {full['quantity']} шт." if full["quantity"] else "."
        lines.append(f"Товар: {full['name']}. Цена: {full['price']} {full['currency']}. Наличие: {full['presence']}{qty}"
                     + (f" Характеристики: {params}" if params else ""))
    facts = _facts(room_ident)
    if facts.get("order"):
        o = facts["order"]
        lines.append(f"Заказ покупателя №{o['id']}: статус «{o['status_label']}».")
    if facts.get("shipment"):
        s = facts["shipment"]
        lines.append(f"Посылка Новой Почтой: ТТН {s['ttn']}, статус: {s['status'] or s['stage']}.")
    elif facts.get("order"):
        lines.append("ТТН по заказу ещё нет.")
    lines.append("Переписка (последние сообщения):")
    for m in [m for m in data["messages"] if m["type"] == "message"][-12:]:
        lines.append(f"{'Продавец' if m['is_my'] else 'Покупатель'}: {m['body']}")
    reply, used = ai.chat_reply("\n".join(lines))
    used = dict(used or {})
    if used.get("cost_usd") is not None:
        used["cost_uah"] = aiprice.to_uah(used["cost_usd"])
    return {"reply": reply, "usage": used}
