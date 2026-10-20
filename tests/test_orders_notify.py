import asyncio
import json

import httpx
import pytest

from promloader import db, notify, orders, products, schedule, suppliers, sync
from promloader.prom_api import PromClient

ORDER = {
    "id": 501, "date_created": "2026-09-30T10:15:00", "client_first_name": "Иван", "client_last_name": "Петренко",
    "phone": "+380501234567", "email": "ivan@example.com", "full_price": "398 грн", "status": "pending",
    "status_name": "Новый", "delivery_option": {"id": 1, "name": "Нова Пошта"}, "delivery_address": "Киев, отд. 12",
    "payment_option": {"id": 2, "name": "Наложенный платёж"}, "client_notes": "Позвонить после 18",
    "products": [{"id": 9, "external_id": "PL-000001", "name": "Кружка", "quantity": 2, "price": "199 грн",
                  "total_price": "398 грн", "image": "https://images.prom.ua/1.jpg"}],
}


class Telegram:
    def __init__(self):
        self.sent = []

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 777, "first_name": "Магазин"}, "text": "/start"}},
                {"update_id": 2, "message": {"chat": {"id": 888, "username": "manager"}, "text": "привет"}}]})
        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") \
                else {"multipart": True, "size": len(request.content)}
            self.sent.append({"method": method, "bot": request.url.path.split("/")[1], **body})
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False, "description": "nope"})


@pytest.fixture
def tg(monkeypatch):
    t = Telegram()
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw:
                        real(token, method, httpx.MockTransport(t.handler), files, **kw))
    db.set_setting("telegram_token", "123:ABC")
    return t


def prom(orders_list, status_calls=None):
    def handler(request):
        if request.url.path.endswith("/orders/list"):
            return httpx.Response(200, json={"orders": orders_list})
        if request.url.path.endswith("/orders/set_status"):
            if status_calls is not None:
                status_calls.append(json.loads(request.content))
            return httpx.Response(200, json={"processed_ids": [501]})
        return httpx.Response(404, json={})
    return PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(handler))


def test_recipients_and_events(tg):
    chats = notify.recent_chats(notify.token())
    assert {c["chat_id"]: c["name"] for c in chats} == {"777": "Магазин", "888": "@manager"}
    owner = notify.add_recipient("777", "Магазин")
    manager = notify.add_recipient("888", "Менеджер", ["sync_done"])
    assert owner["events"] == notify.DEFAULT_EVENTS
    notify.send("order", "заказ", background=False)
    notify.send("sync_done", "выгрузка", background=False)
    got = [(m["chat_id"], m["text"]) for m in tg.sent]
    assert got == [("777", "заказ"), ("888", "выгрузка")]
    notify.update_recipient(manager["id"], events=["sync_done", "order"])
    notify.remove_recipient(owner["id"])
    assert [r["chat_id"] for r in notify.recipients()] == ["888"]
    with pytest.raises(notify.NotifyError):
        notify.add_recipient("не число")


def test_old_single_chat_is_migrated():
    db.set_setting("telegram_chat_id", "555")
    db.set_setting("telegram_chat_name", "Старый")
    db.set_setting("telegram_events", "order")
    # события, появившиеся позже (адрес для телефона, посылки), получатель получает автоматически — один раз
    assert [(r["chat_id"], r["name"], r["events"]) for r in notify.recipients()] == [
        ("555", "Старый", ["order", "phone", "parcel_waiting", "parcel_refused", "daily_summary", "ai_bulk", "margin_alert", "prom_sync"])]


def test_bad_token_message(monkeypatch):
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw: real(
        token, method, httpx.MockTransport(lambda r: httpx.Response(401, json={"ok": False, "description": "x"})), files, **kw))
    with pytest.raises(notify.NotifyError, match="токен"):
        notify.recent_chats("bad")


def test_orders_first_run_not_new_then_notify(tg, monkeypatch):
    monkeypatch.setattr(notify, "send", lambda e, t, background=True: tg.sent.append({"event": e, "text": t}))
    products.create({"name": "Кружка", "price": 199, "external_id": "PL-000001"})
    assert asyncio.run(orders.poll(prom([ORDER]))) == 0  # первый запуск: старые заказы не «новые»
    assert tg.sent[1:] == []
    newer = {**ORDER, "id": 502, "date_created": "2026-09-30T12:00:00"}
    assert asyncio.run(orders.poll(prom([newer, ORDER]))) == 1
    note = tg.sent[-1]
    assert note["event"] == "order" and "№502" in note["text"] and "Кружка × 2" in note["text"]

    data = orders.list_orders()
    assert data["unseen"] == 1 and [o["id"] for o in data["items"]] == [502, 501]
    v = data["items"][1]
    assert v["client"] == "Петренко Иван" and v["delivery"] == "Нова Пошта" and v["items"][0]["local_id"]
    orders.mark_seen()
    assert orders.list_orders()["unseen"] == 0


def test_set_status_and_cancel_reason():
    orders.store([ORDER], notify_new=False)
    calls = []
    v = asyncio.run(orders.set_status(prom([], calls), 501, "received"))
    assert calls[0] == {"status": "received", "ids": [501]} and v["status"] == "received"
    with pytest.raises(sync.PromError):
        asyncio.run(orders.set_status(prom([], calls), 501, "canceled"))
    asyncio.run(orders.set_status(prom([], calls), 501, "canceled", "not_available", "Закончился"))
    assert calls[-1] == {"status": "canceled", "ids": [501], "cancellation_reason": "not_available",
                         "cancellation_text": "Закончился"}
    with pytest.raises(sync.PromError):
        asyncio.run(orders.set_status(prom([], calls), 501, "paid"))


def test_failures_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda e, t, background=True: sent.append((e, t)))
    # отправка на Prom окончательно не удалась
    pid = products.create({"name": "Кружка", "price": 10})
    sync.enqueue([pid])
    sync._fail_or_retry(dict(db.query_one("SELECT * FROM sync_jobs")), sync.PromError("токен не принят"))
    assert sent[-1][0] == "sync_failed" and "токен не принят" in sent[-1][1]
    # поставщик не обновился
    sid = suppliers.create("Опт")
    suppliers.run(sid)
    assert sent[-1][0] == "supplier_failed" and "Опт" in sent[-1][1]


def test_missed_schedule_notifies(monkeypatch):
    from datetime import datetime
    sent = []
    monkeypatch.setattr(notify, "send", lambda e, t, background=True: sent.append((e, t)))
    monkeypatch.setattr(schedule, "_now", lambda: datetime(2026, 9, 28, 8, 0))
    schedule.create({"time": "09:00"})
    schedule.tick(datetime(2026, 9, 29, 12, 0))
    assert sent[-1][0] == "schedule_missed"


def test_api(client):
    db.set_setting("prom_token", "")  # проверяем поведение без токена
    r = client.get("/api/orders").json()
    assert r["enabled"] is False and r["items"] == []
    assert client.post("/api/orders/refresh").status_code == 400
    s = client.post("/api/settings", json={"telegram_token": "123456:ABCDEFGHIJ"}).json()
    assert s["telegram_token"].startswith("1234")
    r = client.post("/api/telegram/recipients", json={"chat_id": "42", "name": "Я", "events": ["order", "hack"]}).json()
    assert r["events"] == ["order"]
    assert client.patch(f"/api/telegram/recipients/{r['id']}", json={"events": ["sync_done"]}).json()["items"][0]["events"] == ["sync_done"]
    assert client.delete(f"/api/telegram/recipients/{r['id']}").json()["items"] == []
    assert client.get("/orders").status_code == 200



def test_bad_message_does_not_hold_the_queue(monkeypatch):
    """Сообщение, которое Telegram не примет никогда (400), раньше повторялось двое суток и держало все следующие
    уведомления получателя — потом они приходили пачкой. Теперь: разметка не разобралась — уходит простым текстом;
    иначе — помечается «не доставлено», а следующие идут сразу."""
    db.set_setting("telegram_token", "123:abc")
    notify.add_recipient("555", "Я", ["order"])
    sent = []

    def handler(request):
        body = json.loads(request.content)
        if "<broken" in body["text"] and body.get("parse_mode"):
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        if "слишком" in body["text"]:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        sent.append(body["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw: real(
        token, method, httpx.MockTransport(handler), files, **kw))
    notify.send("order", "Отделение <broken №1", background=False)
    notify.send("order", "сломанное: слишком", background=False)
    notify.send("order", "<b>Новый заказ №7</b>", background=False)
    assert sent == ["Отделение <broken №1", "<b>Новый заказ №7</b>"]  # простым текстом — как есть
    state = notify.outbox_state()
    assert state["pending"] == 0 and state["failed"] == 1


def test_retry_after_from_telegram(monkeypatch):
    db.set_setting("telegram_token", "123:abc")
    notify.add_recipient("555", "Я", ["order"])
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw: real(
        token, method, httpx.MockTransport(lambda r: httpx.Response(429, json={
            "ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}})), files, **kw))
    notify.send("order", "Заказ", background=False)
    row = db.query_one("SELECT * FROM tg_outbox")
    from datetime import datetime, timezone
    left = (datetime.fromisoformat(row["next_at"]) - datetime.now(timezone.utc)).total_seconds()
    assert row["failed"] == 0 and 0 < left <= 8
