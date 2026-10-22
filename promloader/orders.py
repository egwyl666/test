"""Заказы с Prom: периодический опрос /orders/list, хранение у себя, смена статуса, уведомление о новых.

Структура заказа Prom разбирается бережно: неизвестные поля не ломают страницу, исходные данные сохраняются целиком.

Опрос забирает не «последние 100», а всё, что изменилось с прошлой полной проверки (с запасом), листая страницы.
Поэтому заказы не теряются после простоя (выключенный компьютер, нет интернета), а смена статуса в кабинете Prom
(оплачен, выполнен) тоже доходит до программы. Первая проверка загружает все заказы магазина. Если заказов
очень много, загрузка продолжается со следующей проверки с того же места.
"""

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta

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


def _obj(value) -> dict:
    """Поле заказа Prom, которое должно быть объектом: строка, число или null вместо него — пустой объект
    (заказ из кабинета, старой версии API или с ошибкой у Prom не должен ронять страницу «Заказы»)."""
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def view(row) -> dict:
    """Заказ для страницы: основные поля + исходные данные."""
    data = _obj(json.loads(row["data"] or "{}"))
    client = " ".join(filter(None, [_text(data.get("client_last_name")), _text(data.get("client_first_name")),
                                    _text(data.get("client_second_name"))])) or _text(data.get("client_name"))
    delivery = data.get("delivery_option") or {}
    payment = data.get("payment_option") or {}
    provider = _obj(data.get("delivery_provider_data"))
    items = []
    goods = data.get("products")
    for p in goods if isinstance(goods, list) else []:
        if not isinstance(p, dict):
            continue
        ext = _text(p.get("external_id"))
        local = db.query_one("SELECT id FROM products WHERE external_id = ?", (ext,)) if ext else None
        items.append({
            "name": _text(p.get("name")) or _text(_obj(p.get("name_multilang")).get("ru"))
                    or _text(_obj(p.get("name_multilang")).get("uk")),
            "quantity": p.get("quantity"), "price": _money(p.get("price")), "total": _money(p.get("total_price")),
            "image": _text(p.get("image")), "external_id": ext, "sku": _text(p.get("sku")),
            "local_id": local["id"] if local else None,
        })
    status = row["status"]
    return {
        "id": row["id"], "status": status, "status_label": _text(data.get("status_name")) or STATUSES.get(status, status),
        "date": row["date_created"], "client": client, "phone": _text(data.get("phone")),
        "email": _text(data.get("email")), "price": _money(data.get("full_price") or data.get("price")),
        "delivery": delivery.get("name") if isinstance(delivery, dict) else str(delivery or ""),
        "address": _text(data.get("delivery_address")),
        "payment": payment.get("name") if isinstance(payment, dict) else str(payment or ""),
        "notes": _text(data.get("client_notes")), "source": _text(data.get("source")),
        "items": items, "new": not row["seen"],
        "nova_poshta": provider.get("provider") == "nova_poshta"
                       or "нова пошта" in str(delivery.get("name") if isinstance(delivery, dict) else delivery).lower()
                       or "новая почта" in str(delivery.get("name") if isinstance(delivery, dict) else delivery).lower(),
        "prom_ttn": _text(provider.get("declaration_number")),
        "actions": {k: SETTABLE[k] for k in TRANSITIONS.get(status, ())},
        "prepay_state": row["prepay_state"] if "prepay_state" in row.keys() else "",
        "prepay_requested_at": row["prepay_requested_at"] if "prepay_requested_at" in row.keys() else "",
        "packed_at": row["packed_at"] if "packed_at" in row.keys() else "",
        "packed_by": row["packed_by"] if "packed_by" in row.keys() else "",
    }


# Покупатель, почта и товары заказа — одной строкой для поиска (lower_u: регистр кириллицы)
_TEXT = ("lower_u(COALESCE(json_extract(data, '$.client_last_name'), '') || ' ' || "
         "COALESCE(json_extract(data, '$.client_first_name'), '') || ' ' || "
         "COALESCE(json_extract(data, '$.client_second_name'), '') || ' ' || COALESCE(json_extract(data, '$.email'), '') "
         "|| ' ' || COALESCE(json_extract(data, '$.products'), ''))")
_PHONE = "COALESCE(json_extract(data, '$.phone'), '')"
for _ch in "+ -()":
    _PHONE = f"replace({_PHONE}, '{_ch}', '')"
_PHONE10 = f"substr({_PHONE}, -10)"  # последние 10 цифр: «+380 50…», «050…» и «38050…» — один покупатель
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
        if digits and _NUMBER.match(q):  # «№1234», «+38 050 123-45-67», ТТН — номер заказа, телефон или ТТН
            sub += ["CAST(id AS TEXT) = ?", f"{_PHONE} LIKE ?", "id IN (SELECT order_id FROM shipments WHERE ttn = ?)",
                    "COALESCE(json_extract(data, '$.delivery_provider_data.declaration_number'), '') = ?"]
            args += [digits, f"%{digits}%", digits, digits]
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
    items = [view(r) for r in rows]
    # посылки Новой Почты — одним запросом на страницу заказов
    ships = {r["order_id"]: dict(r) for r in db.query(f"SELECT * FROM shipments WHERE order_id {db.IN_LIST}",
                                                      (db.as_list([o["id"] for o in items]),))}
    from . import novaposhta
    buyers = buyer_histories([(o["id"], o["phone"]) for o in items])
    for o in items:
        s = ships.get(o["id"])
        o["shipment"] = novaposhta.shipment_view(s) if s else None
        o["buyer"] = buyers.get(o["id"])
    flag_risky(items)  # заказ рискового покупателя, пришедший до включения настройки, — пометим при просмотре
    return {"items": items, "total": total, "counts": counts, "unseen": unseen,
            "last_poll": db.get_setting("orders_last_poll"), "error": db.get_setting("orders_error"),
            "loading_all": bool(db.get_setting("orders_cursor"))}


# ---------- надёжность покупателя ----------
# Отказ от посылки с наложенным платежом стоит продавцу доставки туда и обратно. Если этот телефон уже отказывался
# от посылки (или часто отменял заказы), оператор должен увидеть это до того, как отправит наложенным платежом.

RISKY_CANCELS = 2  # столько отменённых заказов в прошлом — тоже повод попросить предоплату


def phone_key(phone) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-10:] if len(digits) >= 9 else ""


def buyer_histories(pairs: list[tuple[int, str]]) -> dict[int, dict]:
    """{номер заказа: история покупателя по его телефону, без самого этого заказа} — одним запросом на страницу."""
    keys = {oid: phone_key(phone) for oid, phone in pairs}
    wanted = sorted({k for k in keys.values() if k})
    if not wanted:
        return {}
    rows = db.query(f"""SELECT o.id, o.status, {_PHONE10} AS ph, s.stage FROM orders o
                        LEFT JOIN shipments s ON s.order_id = o.id WHERE {_PHONE10} {db.IN_LIST}""",
                    (db.as_list(wanted),))
    by_phone: dict[str, list] = {}
    for r in rows:
        by_phone.setdefault(r["ph"], []).append(r)
    result = {}
    for oid, key in keys.items():
        others = [r for r in by_phone.get(key, []) if r["id"] != oid]
        h = {"orders": len(others),
             "delivered": sum(1 for r in others if r["status"] == "delivered" or r["stage"] == "received"),
             "canceled": sum(1 for r in others if r["status"] == "canceled"),
             "refused": sum(1 for r in others if r["stage"] == "refused")}
        h["risky"] = h["refused"] >= 1 or h["canceled"] >= RISKY_CANCELS
        result[oid] = h
    return result


ACTIVE = ("pending", "received", "paid")  # заказ ещё не отправлен и не закрыт — пометка о предоплате имеет смысл


def auto_flag_enabled() -> bool:
    """«Автоматически помечать заказы рисковых покупателей как требующие предоплаты» — по умолчанию включено."""
    return db.get_setting("auto_flag_risky_cod") != "0"


def flag_risky(items: list[dict]) -> list[int]:
    """Пометить «нужна предоплата» активные заказы рисковых покупателей (у кого пометки ещё нет). items — заказы
    списка (view + buyer). Возвращает номера, помеченные сейчас."""
    if not auto_flag_enabled():
        return []
    flagged = [o["id"] for o in items
               if o.get("buyer") and o["buyer"]["risky"] and not o.get("prepay_state") and o["status"] in ACTIVE]
    if flagged:
        with db.tx() as c:
            c.execute(f"UPDATE orders SET prepay_state = 'required' WHERE prepay_state = '' AND id {db.IN_LIST}",
                      (db.as_list(flagged),))
        for o in items:
            if o["id"] in flagged:
                o["prepay_state"] = "required"
    return flagged


def risk_text(b: dict) -> str:
    return f"⚠️ Рисковый покупатель (отказов: {b['refused']}, отмен: {b['canceled']}) — рекомендуется предоплата"


def set_prepay(order_id: int, state: str) -> dict:
    if state not in ("", "ok", "required"):
        raise ValueError("Неизвестное состояние пометки")
    with db.tx() as c:
        if not c.execute("UPDATE orders SET prepay_state = ? WHERE id = ?", (state, order_id)).rowcount:
            raise KeyError(order_id)
    return {"prepay_state": state}


def buyer_history(phone, exclude_order_id: int = 0) -> dict:
    """Заказы этого телефона: всего, выполнено, отменено, отказов от посылок (этап «Отказ / возврат» у ТТН)."""
    return buyer_histories([(exclude_order_id, phone)]).get(
        exclude_order_id, {"orders": 0, "delivered": 0, "canceled": 0, "refused": 0, "risky": False})


# ---------- склад: скан ТТН / номера заказа, «Упакован» ----------

TTN_MIN = 13  # ТТН Новой Почты — 14 цифр (2045…, 5900…); номер заказа Prom — до 12 цифр


class ScanError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def find_by_code(code) -> dict:
    """Что отсканировали или ввели: ТТН (штрихкод на этикетке Новой Почты) или номер заказа. Возвращает заказ так же,
    как в списке (с посылкой и историей покупателя). Сканер иногда добавляет лишнее («TTN: 2045…», пробелы) —
    берём цифры."""
    digits = re.sub(r"\D", "", str(code or ""))
    if not digits:
        raise ScanError("Не похоже на ТТН или номер заказа")
    order_id = None
    kind = "ttn" if len(digits) >= TTN_MIN else "order"
    if kind == "ttn":
        row = db.query_one("SELECT order_id FROM shipments WHERE ttn = ?", (digits,)) or db.query_one(
            "SELECT id AS order_id FROM orders WHERE "
            "json_extract(data, '$.delivery_provider_data.declaration_number') = ?", (digits,))
        order_id = row["order_id"] if row else None
    if order_id is None and len(digits) <= 12:
        row = db.query_one("SELECT id FROM orders WHERE id = ?", (int(digits),))
        order_id = row["id"] if row else None
        kind = "order"
    if order_id is None:
        raise ScanError(f"ТТН {digits} не найдена среди заказов программы" if kind == "ttn"
                        else f"Заказ №{digits} не найден среди заказов программы", 404)
    item = order_card(order_id)
    return {"kind": kind, "code": digits, "order": item}


def order_card(order_id: int) -> dict:
    """Один заказ — как в списке: посылка, история покупателя, пометка о предоплате."""
    from . import novaposhta
    row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if row is None:
        raise KeyError(order_id)
    o = view(row)
    s = db.query_one("SELECT * FROM shipments WHERE order_id = ?", (order_id,))
    o["shipment"] = novaposhta.shipment_view(s) if s else None
    o["buyer"] = buyer_history(o["phone"], exclude_order_id=order_id) if o["phone"] else None
    return o


def pack(order_id: int, who: str, force: bool = False) -> dict:
    """«Упакован»: проверки, которые на складе легко пропустить. Возвращает {ok, order, warnings}; ошибки — ScanError.
    - отменённый заказ не упаковываем (409);
    - уже упакован — второй скан не перезаписывает «кто и когда» (409, если не force);
    - рисковый покупатель без снятой пометки, нет ТТН — упаковать можно, но с предупреждением."""
    o = order_card(order_id)
    if o["status"] == "canceled":
        raise ScanError(f"Заказ №{order_id} отменён — не упаковывайте его", 409)
    if o["packed_at"] and not force:
        raise ScanError(f"Заказ №{order_id} уже упакован ({o['packed_by'] or '—'})", 409)
    warnings = []
    if o["prepay_state"] in ("required", "requested"):
        warnings.append("Рисковый покупатель: предоплата не отмечена — проверьте перед отправкой")
    if not o["shipment"] and not o["prom_ttn"]:
        warnings.append("У заказа ещё нет ТТН")
    with db.tx() as c:
        c.execute("UPDATE orders SET packed_at = ?, packed_by = ? WHERE id = ?", (db.now(), who or "", order_id))
    return {"ok": True, "order": order_card(order_id), "warnings": warnings}


def unpack(order_id: int) -> dict:
    with db.tx() as c:
        if not c.execute("UPDATE orders SET packed_at = '', packed_by = '' WHERE id = ?", (order_id,)).rowcount:
            raise KeyError(order_id)
    return order_card(order_id)


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
    if v.get("prepay_state") == "required" and v.get("buyer"):
        lines.append(f"<b>{notify.esc(risk_text(v['buyer']))}</b>")
    return "\n".join(lines)


def store(orders: list[dict], notify_new: bool = True, first_run: bool | None = None) -> int:
    """Сохраняет заказы. Возвращает, сколько новых. first_run — заказы, которые уже были в магазине до подключения
    программы: не «новые» и без уведомлений."""
    if first_run is None:
        first_run = db.get_setting("orders_initialized") != "1"
    new = _store(orders, first_run)
    from . import stock
    if first_run:
        stock.mark_legacy()  # история магазина при первом подключении — уже учтена в остатках
    else:
        stock.apply_delivered()  # заказ стал «Выполнен» в кабинете Prom — уменьшить остаток
        db.set_setting("orders_initialized", "1")
    _flag_new(new)
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


def _flag_new(new_ids: list) -> None:
    """Новый заказ рискового покупателя — сразу «нужна предоплата» (и строка об этом в уведомлении о заказе)."""
    if not new_ids or not auto_flag_enabled():
        return
    rows = db.query(f"SELECT * FROM orders WHERE id {db.IN_LIST}", (db.as_list(new_ids),))
    items = [view(r) for r in rows]
    buyers = buyer_histories([(o["id"], o["phone"]) for o in items])
    for o in items:
        o["buyer"] = buyers.get(o["id"])
    flag_risky(items)


def _notify(new_ids: list) -> None:
    """После простоя новых заказов может быть много — о первых по сообщению, об остальных одним."""
    rows = [db.query_one("SELECT * FROM orders WHERE id = ?", (oid,)) for oid in new_ids]
    rows.sort(key=lambda r: (r["date_created"], r["id"]))
    for row in rows[:NOTIFY_EACH]:
        v = view(row)
        if v["prepay_state"] == "required":
            v["buyer"] = buyer_history(v["phone"], exclude_order_id=v["id"])
        notify.send("order", _notify_text(v))
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
        if exc.status == 403 and not exc.no_api:
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
    if status == "delivered":
        from . import stock
        await asyncio.to_thread(stock.apply_delivered)  # «Выполнен» из программы — так же уменьшает остаток
    return view(db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,)))


def enabled() -> bool:
    return bool(config.get("prom_token")) and db.get_setting("orders_enabled") != "0"
