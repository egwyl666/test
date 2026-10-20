"""Новая Почта: ТТН из заказа Prom в один клик, этикетка, отслеживание посылок, тревога «посылку не забирают».

API Новой Почты 2.0 (`https://api.novaposhta.ua/v2.0/json/`): один POST {apiKey, modelName, calledMethod,
methodProperties} → {success, data, errors, warnings}.

Что проверено на живом API (без ключа, только чтение):
- номер отделения в заказе Prom (`delivery_provider_data.recipient_warehouse_id`) — это `Ref` отделения НП, по нему
  `AddressGeneral.getWarehouses` отдаёт город (`CityRef`) и название отделения → получатель подставляется сам;
- `TrackingDocument.getStatusDocuments` работает и без ключа → программа следит за всеми посылками магазина, в том
  числе с ТТН из кабинета Prom (номер приходит в заказе: `declaration_number`).

**Создание ТТН на живом API не проверялось**: настоящая ТТН приходит покупателю SMS. Поля — по документации НП;
первая настоящая ТТН — ваша, из «Заказов».
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, notify

log = logging.getLogger("promloader.novaposhta")

API = "https://api.novaposhta.ua/v2.0/json/"
LABEL_URL = "https://my.novaposhta.ua/orders/printMarking100x100/orders[]/{number}/type/pdf/apiKey/{key}"
TIMEOUT = 30
BATCH = 100  # getStatusDocuments принимает до 100 номеров за раз
TRACK_MINUTES = 30
NP_DEFAULTS = {"np_weight": "0.5", "np_description": "Товари", "np_payer": "Recipient", "np_storage_days": "5"}
# как Prom называет оплату при получении (рус./укр.) — распознавание, не текст для экрана
COD_WORDS = ("налож", "післяплат", "оплата при отриманні", "оплата при получении")
PAYERS = {"Recipient": "Покупатель", "Sender": "Отправитель (вы)"}

# коды статусов НП → этап для значка в «Заказах»
STAGES = {"1": "created", "2": "deleted", "3": "not_found", "4": "on_the_way", "41": "on_the_way", "5": "on_the_way",
          "6": "on_the_way", "101": "on_the_way", "104": "on_the_way", "112": "on_the_way",
          "7": "arrived", "8": "arrived", "9": "received", "10": "received", "11": "received", "106": "received",
          "102": "refused", "103": "refused", "108": "refused", "105": "refused", "111": "failed"}
STAGE_LABELS = {"created": "ТТН создана", "on_the_way": "В пути", "arrived": "На отделении", "received": "Получена",
                "refused": "Отказ / возврат", "failed": "Не доставлена", "deleted": "ТТН удалена",
                "not_found": "ТТН не найдена"}
FINAL = {"received", "refused", "deleted", "failed"}


class NPError(Exception):
    pass


# ---------- запросы ----------

def api_key() -> str:
    return db.get_setting("np_api_key")


def setting(key: str) -> str:
    return db.get_setting(key) or NP_DEFAULTS.get(key, "")


NET_RETRIES = 2          # повторов при сбое сети (getaddrinfo failed, WinError 10013 — бывает на Windows кратко)
NET_DELAY = 1.5          # секунд между попытками
# методы, которые что-то создают или удаляют: их повторяем, только если запрос точно не ушёл (не удалось
# соединиться), — иначе после обрыва ответа можно создать вторую ТТН
MUTATING = {"save", "delete", "update"}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _network_error(exc: Exception) -> NPError:
    """Понятный текст вместо «[WinError 10013] An attempt was made…» — без адресов и ключа."""
    text = str(exc)
    if "getaddrinfo" in text or "11001" in text or "Name or service not known" in text:
        return NPError("Нет связи с Новой Почтой: компьютер не нашёл её сервер (нет интернета или DNS). "
                       "Программа повторит позже")
    if "10013" in text or "forbidden by its access permissions" in text:
        return NPError("Нет связи с Новой Почтой: Windows не пустила программу в интернет — проверьте антивирус "
                       "или брандмауэр. Программа повторит позже")
    if isinstance(exc, httpx.TimeoutException):
        return NPError("Нет связи с Новой Почтой: сервер не ответил вовремя. Программа повторит позже")
    return NPError("Нет связи с Новой Почтой: нет подключения к интернету или сервер недоступен. Программа повторит позже")


def _request(send, safe: bool = True):
    """send() — один HTTP-запрос. Сетевой сбой повторяем NET_RETRIES раз; safe=False — только сбой соединения."""
    for attempt in range(NET_RETRIES + 1):
        try:
            return send()
        except (httpx.TransportError, OSError) as exc:
            retryable = safe or isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, OSError))
            if attempt == NET_RETRIES or not retryable:
                raise _network_error(exc) from None
            log.info("Нова Пошта: сбой сети, повтор %s из %s", attempt + 1, NET_RETRIES)
            _sleep(NET_DELAY)


def call(model: str, method: str, props: dict | None = None, key: str | None = None, transport=None) -> list:
    body = {"apiKey": api_key() if key is None else key, "modelName": model, "calledMethod": method,
            "methodProperties": props or {}}

    def send():
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            return client.post(API, json=body)
    r = _request(send, safe=method not in MUTATING)
    try:
        data = r.json()
    except ValueError:
        raise NPError(f"Нова Пошта ответила не по форме (HTTP {r.status_code}) — попробуйте позже")
    if not data.get("success"):
        errors = "; ".join(str(e) for e in (data.get("errors") or [])) or f"ошибка {r.status_code}"
        raise NPError(f"Нова Пошта: {errors}")
    return data.get("data") or []


# ---------- настройки отправителя ----------

def check(key: str, transport=None) -> dict:
    """Проверить ключ: отправитель (ваш кабинет НП) и его контактные лица."""
    key = key.strip()
    senders = call("Counterparty", "getCounterparties", {"CounterpartyProperty": "Sender", "Page": "1"}, key, transport)
    if not senders:
        raise NPError("Ключ принят, но в кабинете Новой Почты нет отправителя")
    sender = senders[0]
    contacts = call("Counterparty", "getCounterpartyContactPersons", {"Ref": sender["Ref"], "Page": "1"}, key, transport)
    return {"sender": {"ref": sender["Ref"], "name": sender.get("Description", ""), "city_ref": sender.get("City", ""),
                       "city": sender.get("CityDescription", "")},
            "contacts": [{"ref": c["Ref"], "name": c.get("Description", ""), "phone": c.get("Phones", "")} for c in contacts]}


def cities(q: str, transport=None) -> list[dict]:
    rows = call("Address", "getCities", {"FindByString": q.strip(), "Limit": "20"}, transport=transport)
    return [{"ref": r["Ref"], "name": r.get("Description", ""), "area": r.get("AreaDescription", "")} for r in rows]


def warehouses(city_ref: str, q: str = "", transport=None) -> list[dict]:
    props = {"CityRef": city_ref, "Limit": "50"}
    if q.strip():
        props["FindByString"] = q.strip()
    rows = call("AddressGeneral", "getWarehouses", props, transport=transport)
    return [{"ref": r["Ref"], "name": r.get("Description", ""), "number": r.get("Number", "")} for r in rows]


def sender_ready() -> bool:
    return all(db.get_setting(k) for k in ("np_api_key", "np_sender_ref", "np_contact_ref", "np_sender_phone",
                                           "np_city_ref", "np_warehouse_ref"))


# ---------- черновик ТТН из заказа ----------

def _order(order_id: int) -> dict:
    row = db.query_one("SELECT data FROM orders WHERE id = ?", (order_id,))
    if not row:
        raise NPError("Нет такого заказа")
    return json.loads(row["data"])


def _money(value) -> float:
    m = re.search(r"\d[\d\s]*(?:[.,]\d+)?", str(value or "").replace("\xa0", " "))
    return float(m.group(0).replace(" ", "").replace(",", ".")) if m else 0.0


def is_cod(order: dict) -> bool:
    """Наложенный платёж: заказ ещё не оплачен — без обратной доставки денег посылку отдали бы бесплатно."""
    name = str((order.get("payment_option") or {}).get("name") or "").lower()
    return any(w in name for w in COD_WORDS)


def prom_ttn(order: dict) -> str:
    return str((order.get("delivery_provider_data") or {}).get("declaration_number") or "")


def draft(order_id: int, transport=None) -> dict:
    """Готовая форма ТТН: получатель, город и отделение из заказа Prom; вес, описание, плательщик — по умолчанию."""
    order = _order(order_id)
    if prom_ttn(order) or db.query_one("SELECT 1 FROM shipments WHERE order_id = ?", (order_id,)):
        raise NPError("У этого заказа уже есть ТТН")
    rec = order.get("delivery_recipient") or {}
    first = rec.get("first_name") or order.get("client_first_name") or ""
    last = rec.get("last_name") or order.get("client_last_name") or ""
    middle = rec.get("second_name") or order.get("client_second_name") or ""
    phone = re.sub(r"\D", "", str(rec.get("phone") or order.get("phone") or ""))
    provider = order.get("delivery_provider_data") or {}
    city = {"ref": "", "name": ""}
    warehouse = {"ref": "", "name": ""}
    wh_ref = provider.get("recipient_warehouse_id") or (provider.get("recipient_address") or {}).get("warehouse_id")
    if wh_ref:
        try:
            found = call("AddressGeneral", "getWarehouses", {"Ref": wh_ref, "Limit": "1"}, transport=transport)
        except NPError:
            found = []
        if found:
            warehouse = {"ref": found[0]["Ref"], "name": found[0].get("Description", "")}
            city = {"ref": found[0].get("CityRef", ""), "name": found[0].get("CityDescription", "")}
    total = _money(order.get("full_price") or order.get("price"))
    cod = is_cod(order)
    return {
        "order_id": order_id, "first_name": first, "last_name": last, "middle_name": middle, "phone": phone,
        "city_ref": city["ref"], "city": city["name"], "warehouse_ref": warehouse["ref"], "warehouse": warehouse["name"],
        "weight": float(setting("np_weight") or 0.5), "description": setting("np_description"), "cost": total,
        "payer": setting("np_payer"), "cod": cod, "cod_amount": total if cod else 0.0,
        "address_hint": order.get("delivery_address") or "",
    }


# ---------- создание, удаление, этикетка ----------

def _phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "38" + digits
    if not re.fullmatch(r"380\d{9}", digits):
        raise NPError("Телефон получателя — в формате 380XXXXXXXXX (12 цифр)")
    return digits


def _validate(form: dict) -> dict:
    out = dict(form)
    for field, label in (("last_name", "фамилию"), ("first_name", "имя"), ("city_ref", "город"),
                         ("warehouse_ref", "отделение")):
        if not str(form.get(field) or "").strip():
            raise NPError(f"Укажите {label} получателя")
    out["phone"] = _phone(str(form.get("phone") or ""))
    try:
        out["weight"] = float(form.get("weight") or 0)
        out["cost"] = float(form.get("cost") or 0)
        out["cod_amount"] = float(form.get("cod_amount") or 0)
    except (TypeError, ValueError):
        raise NPError("Вес и суммы — числа")
    if not 0 < out["weight"] <= 1000:
        raise NPError("Вес посылки — от 0,1 до 1000 кг")
    if out["cost"] <= 0:
        raise NPError("Оценочная стоимость — больше нуля")
    if form.get("cod") and out["cod_amount"] <= 0:
        raise NPError("Сумма наложенного платежа — больше нуля")
    if form.get("payer") not in PAYERS:
        raise NPError("Кто платит за доставку — покупатель или вы")
    return out


def create(order_id: int, form: dict, transport=None) -> dict:
    """Создать ТТН. Номер сохраняется у заказа; в заказ на Prom его передаёт routes/orders.py (там клиент Prom)."""
    if not sender_ready():
        raise NPError("Сначала подключите Новую Почту в «Настройках»: ключ API, отправитель и отделение отправки")
    order = _order(order_id)
    if prom_ttn(order) or db.query_one("SELECT 1 FROM shipments WHERE order_id = ?", (order_id,)):
        raise NPError("У этого заказа уже есть ТТН")
    f = _validate(form)
    if is_cod(order) and not f.get("cod"):
        raise NPError("Заказ с наложенным платежом: включите «Наложенный платёж», иначе посылку отдадут без оплаты")
    recipient = call("Counterparty", "save", {
        "FirstName": f["first_name"].strip(), "LastName": f["last_name"].strip(),
        "MiddleName": str(f.get("middle_name") or "").strip(), "Phone": f["phone"], "Email": "",
        "CounterpartyType": "PrivatePerson", "CounterpartyProperty": "Recipient"}, transport=transport)[0]
    contact = ((recipient.get("ContactPerson") or {}).get("data") or [{}])[0].get("Ref", "")
    props = {
        "PayerType": f["payer"], "PaymentMethod": "Cash", "CargoType": "Parcel", "ServiceType": "WarehouseWarehouse",
        "DateTime": datetime.now().strftime("%d.%m.%Y"), "SeatsAmount": "1", "Weight": str(f["weight"]),
        "Description": str(f.get("description") or setting("np_description"))[:100], "Cost": str(round(f["cost"])),
        "CitySender": setting("np_city_ref"), "Sender": setting("np_sender_ref"),
        "SenderAddress": setting("np_warehouse_ref"), "ContactSender": setting("np_contact_ref"),
        "SendersPhone": setting("np_sender_phone"),
        "CityRecipient": f["city_ref"], "Recipient": recipient["Ref"], "RecipientAddress": f["warehouse_ref"],
        "ContactRecipient": contact, "RecipientsPhone": f["phone"],
        "InfoRegClientBarcodes": str(order_id),  # номер заказа Prom в ТТН — видно в кабинете НП
    }
    if f.get("cod"):
        props["BackwardDeliveryData"] = [{"PayerType": "Recipient", "CargoType": "Money",
                                          "RedeliveryString": str(round(f["cod_amount"], 2))}]
    doc = call("InternetDocument", "save", props, transport=transport)[0]
    ttn = str(doc.get("IntDocNumber") or "")
    if not ttn:
        raise NPError("Нова Пошта не вернула номер ТТН")
    with db.tx() as c:
        c.execute("""INSERT INTO shipments (order_id, ttn, ref, own, stage, status, cost, created_at)
                     VALUES (?, ?, ?, 1, 'created', ?, ?, ?)""",
                  (order_id, ttn, doc.get("Ref", ""), STAGE_LABELS["created"], str(doc.get("CostOnSite") or ""), db.now()))
    log.info("ТТН %s для заказа %s", ttn, order_id)
    return shipment(order_id)


def delete(order_id: int, transport=None) -> None:
    s = shipment(order_id)
    if not s:
        raise NPError("У заказа нет ТТН")
    if not s["own"]:
        raise NPError("Эта ТТН создана не программой — удалите её там, где создавали (кабинет Prom или Новой Почты)")
    if s["stage"] not in ("created", ""):
        raise NPError("Посылка уже отправлена — удалить ТТН нельзя")
    call("InternetDocument", "delete", {"DocumentRefs": s["ref"]}, transport=transport)
    with db.tx() as c:
        c.execute("DELETE FROM shipments WHERE order_id = ?", (order_id,))


def label_pdf(order_id: int, transport=None) -> bytes:
    """Этикетка 100×100 (PDF) — через программу, чтобы ключ НП не попадал в адрес в браузере."""
    s = shipment(order_id)
    if not s:
        raise NPError("У заказа нет ТТН")
    if not api_key():
        raise NPError("Для печати нужен ключ API Новой Почты в «Настройках»")
    def send():
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True, transport=transport) as client:
            return client.get(LABEL_URL.format(number=s["ttn"], key=api_key()))
    r = _request(send)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise NPError("Нова Пошта не отдала этикетку — откройте ТТН в кабинете Новой Почты")
    return r.content


def shipment(order_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM shipments WHERE order_id = ?", (order_id,))
    if not row:
        return None
    s = dict(row)
    s["alerts"] = json.loads(s["alerts"])
    s["stage_label"] = STAGE_LABELS.get(s["stage"], s["status"])
    return s


# ---------- отслеживание ----------

def _sync_from_prom() -> int:
    """ТТН, созданные в кабинете Prom, приходят в заказе — берём их под отслеживание."""
    added = 0
    rows = db.query("""SELECT id, json_extract(data, '$.delivery_provider_data.declaration_number') AS ttn,
                              json_extract(data, '$.delivery_provider_data.provider') AS provider
                       FROM orders WHERE status != 'canceled'
                         AND json_extract(data, '$.delivery_provider_data.declaration_number') IS NOT NULL
                         AND id NOT IN (SELECT order_id FROM shipments)""")
    with db.tx() as c:
        for r in rows:
            if r["provider"] == "nova_poshta" and re.fullmatch(r"\d{14}", str(r["ttn"] or "")):
                c.execute("INSERT OR IGNORE INTO shipments (order_id, ttn, own, created_at) VALUES (?, ?, 0, ?)",
                          (r["id"], str(r["ttn"]), db.now()))
                added += 1
    return added


def _alert(c, s: dict, kind: str, text: str) -> None:
    if kind in s["alerts"]:
        return
    s["alerts"].append(kind)
    c.execute("UPDATE shipments SET alerts = ? WHERE order_id = ?", (json.dumps(s["alerts"]), s["order_id"]))
    notify.send(kind, text)


def track(transport=None) -> int:
    """Обновить статусы посылок в пути. Возвращает, сколько проверено."""
    _sync_from_prom()
    active = [dict(r) for r in db.query("SELECT * FROM shipments WHERE stage NOT IN ('received', 'refused', 'deleted', 'failed')")]
    phone = setting("np_sender_phone")
    now = datetime.now(timezone.utc)
    days = int(setting("np_storage_days") or 5)
    checked = 0
    for i in range(0, len(active), BATCH):
        part = active[i:i + BATCH]
        docs = [{"DocumentNumber": s["ttn"], "Phone": phone} for s in part]
        try:
            rows = call("TrackingDocument", "getStatusDocuments", {"Documents": docs}, transport=transport)
        except NPError as exc:
            log.warning("Отслеживание посылок: %s", exc)
            return checked
        by_number = {str(r.get("Number")): r for r in rows}
        with db.tx() as c:
            for s in part:
                r = by_number.get(s["ttn"])
                if not r:
                    continue
                checked += 1
                s["alerts"] = json.loads(s["alerts"])
                code = str(r.get("StatusCode") or "")
                stage = STAGES.get(code, s["stage"] or "on_the_way")
                arrived = s["arrived_at"]
                if stage == "arrived" and not arrived:
                    arrived = now.isoformat(timespec="seconds")
                c.execute("""UPDATE shipments SET status_code = ?, status = ?, stage = ?, arrived_at = ?, checked_at = ?
                             WHERE order_id = ?""", (code, str(r.get("Status") or ""), stage, arrived, db.now(), s["order_id"]))
                who = f"заказ №{s['order_id']}, ТТН {s['ttn']}"
                if stage == "arrived":
                    _alert(c, s, "parcel_arrived", f"📦 Посылка прибыла в отделение ({who})\n{r.get('WarehouseRecipient') or ''}")
                    if arrived and now - datetime.fromisoformat(arrived) > timedelta(days=days):
                        _alert(c, s, "parcel_waiting", f"⏳ Посылку не забирают больше {days} дн. ({who}) — напишите или "
                                                       f"позвоните покупателю, пока не начался платный срок хранения")
                elif stage == "refused":
                    _alert(c, s, "parcel_refused", f"↩ Отказ от посылки или возврат ({who}): {r.get('Status') or ''}")
    return checked


def enabled() -> bool:
    """Отслеживать есть что: любые посылки (ключ для отслеживания не нужен)."""
    return db.query_one("SELECT 1 AS x FROM orders LIMIT 1") is not None


def settings_view() -> dict:
    return {"key_set": bool(api_key()), "key": config.mask(api_key()), "ready": sender_ready(), "payers": PAYERS,
            **{k: setting(k) for k in ("np_sender_name", "np_contact_name", "np_sender_phone", "np_city_name",
                                       "np_city_ref", "np_warehouse_name", "np_warehouse_ref", "np_weight",
                                       "np_description", "np_payer", "np_storage_days")}}
