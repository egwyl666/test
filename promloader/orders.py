"""Заказы с Prom: периодический опрос /orders/list, хранение у себя, смена статуса, уведомление о новых.

Структура заказа Prom разбирается бережно: неизвестные поля не ломают страницу, исходные данные сохраняются целиком.

Опрос забирает не «последние 100», а всё, что изменилось с прошлой полной проверки (с запасом), листая страницы.
Поэтому заказы не теряются после простоя (выключенный компьютер, нет интернета), а смена статуса в кабинете Prom
(оплачен, выполнен) тоже доходит до программы. Первая проверка загружает все заказы магазина. Если заказов
очень много, загрузка продолжается со следующей проверки с того же места.
"""

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from . import config, db, notify
from .prom_api import PromError

log = logging.getLogger("promloader.orders")

POLL_MINUTES = 5
PAGE = 100            # больше Prom за раз не отдаёт
MAX_PAGES = 50        # за одну проверку; остальное — со следующей
OVERLAP = timedelta(hours=4)  # Prom ждёт время без пояса (киевское?) — берём с запасом, повтор заказа не страшен
NOTIFY_EACH = 10      # после простоя: о первых 10 новых — по сообщению, об остальных — одним
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
# из какого статуса куда можно перейти: выполненный и отменённый заказ кнопками не меняются
TRANSITIONS = {
    "pending": ("received", "canceled"),
    "received": ("delivered", "canceled"),
    "paid": ("received", "delivered", "canceled"),
}
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
        "actions": {k: SETTABLE[k] for k in TRANSITIONS.get(status, ())},
    }


# Покупатель, почта и товары заказа — одной строкой для поиска (lower_u: регистр кириллицы)
_TEXT = ("lower_u(COALESCE(json_extract(data, '$.client_last_name'), '') || ' ' || "
         "COALESCE(json_extract(data, '$.client_first_name'), '') || ' ' || "
         "COALESCE(json_extract(data, '$.client_second_name'), '') || ' ' || COALESCE(json_extract(data, '$.email'), '') "
         "|| ' ' || COALESCE(json_extract(data, '$.products'), ''))")
_PHONE = "COALESCE(json_extract(data, '$.phone'), '')"
for _ch in "+ -()":
    _PHONE = f"replace({_PHONE}, '{_ch}', '')"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER = re.compile(r"^[\s№#+\-()\d]+$")


def _where(flt: dict, with_status: bool = True) -> tuple[str, list]:
    """Фильтр заказов: статус, поиск (номер, телефон, имя, почта, товар), даты «с» и «по» включительно."""
    parts, args = [], []
    if with_status and flt.get("status"):
        parts.append("status = ?")
        args.append(flt["status"])
    q = str(flt.get("q") or "").strip()
    if q:
        words = q.lower().split()
        sub = ["(" + " AND ".join([f"{_TEXT} LIKE ?"] * len(words)) + ")"]
        args += [f"%{w}%" for w in words]
        digits = re.sub(r"\D", "", q)
        if digits and _NUMBER.match(q):  # «№1234», «+38 050 123-45-67» — номер заказа или телефон
            sub += ["CAST(id AS TEXT) = ?", f"{_PHONE} LIKE ?"]
            args += [digits, f"%{digits}%"]
        parts.append("(" + " OR ".join(sub) + ")")
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        value = str(flt.get(key) or "")
        if value:
            if not _DATE.match(value):
                raise ValueError("Дата — в виде ГГГГ-ММ-ДД")
            parts.append(f"substr(date_created, 1, 10) {op} ?")
            args.append(value)
    return ("WHERE " + " AND ".join(parts)) if parts else "", args


def list_orders(flt: dict | None = None, limit: int = 50, offset: int = 0) -> dict:
    flt = flt or {}
    where, args = _where(flt)
    rows = db.query(f"SELECT * FROM orders {where} ORDER BY date_created DESC, id DESC LIMIT ? OFFSET ?",
                    args + [limit, offset])
    total = db.query_one(f"SELECT COUNT(*) AS n FROM orders {where}", args)["n"]
    # счётчики статусов — с тем же поиском и датами, чтобы чипы показывали, сколько найдётся
    cwhere, cargs = _where(flt, with_status=False)
    counts = {r["status"]: r["n"] for r in db.query(f"SELECT status, COUNT(*) AS n FROM orders {cwhere} GROUP BY status",
                                                    cargs)}
    unseen = db.query_one("SELECT COUNT(*) AS n FROM orders WHERE seen = 0")["n"]
    return {"items": [view(r) for r in rows], "total": total, "counts": counts, "unseen": unseen,
            "last_poll": db.get_setting("orders_last_poll"), "error": db.get_setting("orders_error"),
            "loading_all": bool(db.get_setting("orders_cursor"))}


def mark_seen(ids: list[int] | None = None) -> None:
    """ids — эти заказы; None — все."""
    with db.tx() as c:
        if ids is not None:
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


def store(orders: list[dict], notify_new: bool = True, first_run: bool | None = None) -> int:
    """Сохраняет заказы. Возвращает, сколько новых. first_run — заказы, которые уже были в магазине до подключения
    программы: не «новые» и без уведомлений."""
    if first_run is None:
        first_run = db.get_setting("orders_initialized") != "1"
    new = _store(orders, first_run)
    if not first_run:
        db.set_setting("orders_initialized", "1")
    if notify_new and new:
        _notify(new)
    return len(new)


def _store(orders: list[dict], first_run: bool, newer_than: int | None = None) -> list:
    """Записать заказы; вернуть номера новых. newer_than — новыми считаются только заказы с номером больше
    (догрузка старых заказов после обновления программы: они не «новые» и без уведомлений)."""
    new = []
    ts = db.now()
    with db.tx() as c:
        for o in orders:
            if not isinstance(o, dict) or o.get("id") is None:
                continue
            exists = c.execute("SELECT 1 FROM orders WHERE id = ?", (o["id"],)).fetchone()
            old = first_run or (newer_than is not None and int(o["id"]) <= newer_than)
            c.execute(
                """INSERT INTO orders (id, status, date_created, data, seen, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data, updated_at = excluded.updated_at""",
                (o["id"], str(o.get("status") or ""), str(o.get("date_created") or ""),
                 json.dumps(o, ensure_ascii=False), 1 if old else 0, ts),
            )
            if not exists and not old:
                new.append(o["id"])
    return new


def _notify(new_ids: list) -> None:
    """После простоя новых заказов может быть много — о первых по сообщению, об остальных одним."""
    rows = [db.query_one("SELECT * FROM orders WHERE id = ?", (oid,)) for oid in new_ids]
    rows.sort(key=lambda r: (r["date_created"], r["id"]))
    for row in rows[:NOTIFY_EACH]:
        notify.send("order", _notify_text(view(row)))
    rest = rows[NOTIFY_EACH:]
    if rest:
        notify.send("order", f"🛒 <b>И ещё {len(rest)} новых заказов</b> — пока программа не проверяла Prom. "
                             "Откройте «Заказы» в программе.")


def _prom_time(iso: str) -> str:
    return (datetime.fromisoformat(iso) - OVERLAP).strftime("%Y-%m-%dT%H:%M:%S")


async def poll(client) -> int:
    """Забирает заказы, изменившиеся с прошлой полной проверки, — все страницы. Возвращает, сколько новых."""
    if not _lock.acquire(blocking=False):
        return 0
    new = []
    try:
        cursor = json.loads(db.get_setting("orders_cursor") or "{}")  # незаконченная загрузка: откуда продолжить
        started = cursor.get("started") or db.now()
        since = cursor.get("since") if cursor else db.get_setting("orders_synced_at")
        first_run = db.get_setting("orders_initialized") != "1"
        # программа уже работала с заказами (версия до 2.1 брала последние 100), но полной загрузки ещё не было:
        # догружаемые старые заказы — не «новые», новыми считаем только заказы с номером больше известных
        newer_than = cursor.get("newer_than")
        if not since and not first_run and newer_than is None:
            newer_than = db.query_one("SELECT MAX(id) AS m FROM orders")["m"] or 0
        params = {"last_modified_from": _prom_time(since)} if since else {}
        last_id, complete = cursor.get("last_id"), False
        for _ in range(MAX_PAGES):
            body = await client.list_orders(limit=PAGE, last_id=last_id, **params)
            page = [o for o in body.get("orders") or [] if isinstance(o, dict) and o.get("id") is not None]
            new += _store(page, first_run, newer_than)
            ids = [int(o["id"]) for o in page]
            if len(page) < PAGE or (last_id is not None and min(ids, default=last_id) >= last_id):
                complete = True
                break
            last_id = min(ids)
        if complete:
            db.set_setting("orders_synced_at", started)  # следующая проверка — с начала этой (с запасом)
            db.set_setting("orders_cursor", "")
            if first_run:
                db.set_setting("orders_initialized", "1")
        else:
            db.set_setting("orders_cursor", json.dumps({"started": started, "since": since, "last_id": last_id,
                                                        "newer_than": newer_than}))
        db.set_setting("orders_last_poll", db.now())
        db.set_setting("orders_error", "")
        return len(new)
    except PromError as exc:
        db.set_setting("orders_error", str(exc))
        raise
    finally:
        _lock.release()
        if new:
            _notify(new)  # одним списком за всю проверку (и если Prom оборвал её посередине — о том, что успели)


async def set_status(client, order_id: int, status: str, reason: str = "", text: str = "") -> dict:
    """Сменить статус на Prom. Только допустимые переходы; ответ Prom проверяется — «не изменён» это ошибка."""
    if status not in SETTABLE:
        raise PromError("Такой статус из программы поставить нельзя")
    row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if row is None:
        raise PromError("Заказ не найден — нажмите «↻ Проверить новые»")
    if status not in TRANSITIONS.get(row["status"], ()):
        current = STATUSES.get(row["status"], row["status"])
        raise PromError(f"Заказ в статусе «{current}» нельзя перевести в «{SETTABLE[status]}»")
    if status == "canceled" and reason not in CANCEL_REASONS:
        raise PromError("Укажите причину отмены")
    try:
        body = await client.set_order_status([order_id], status, reason if status == "canceled" else "",
                                             text.strip() if status == "canceled" else "")
    except PromError as exc:
        if exc.status == 403:
            raise PromError("Prom не дал изменить заказ: у API-токена нет права менять заказы (кабинет Prom → "
                            "Настройки → Управление API-токенами → права «Заказы»)", status=403)
        raise
    if isinstance(body, dict) and "processed_ids" in body:
        processed = {int(x) for x in body.get("processed_ids") or [] if str(x).isdigit()}
        if order_id not in processed:
            errors = body.get("errors") or {}
            reason_text = errors.get(str(order_id)) if isinstance(errors, dict) else errors
            raise PromError(f"Prom не изменил статус заказа №{order_id}" + (f": {reason_text}" if reason_text else ""))
    data = json.loads(row["data"])
    data["status"] = status
    data.pop("status_name", None)
    with db.tx() as c:
        c.execute("UPDATE orders SET status = ?, data = ?, seen = 1, updated_at = ? WHERE id = ?",
                  (status, json.dumps(data, ensure_ascii=False), db.now(), order_id))
    return view(db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,)))


def enabled() -> bool:
    return bool(config.get("prom_token")) and db.get_setting("orders_enabled") != "0"
