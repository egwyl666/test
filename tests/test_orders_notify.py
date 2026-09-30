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
                {"update_id": 1, "message": {"chat": {"id": 777, "first_name": "Магазин"}, "text": "/start"}}]})
        if method == "sendMessage":
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False, "description": "nope"})


@pytest.fixture
def tg(monkeypatch):
    t = Telegram()
    real = notify._client
    monkeypatch.setattr(notify, "_client", lambda transport=None: real(httpx.MockTransport(t.handler)))
    db.set_setting("telegram_token", "123:ABC")
    notify.find_chat()
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


def test_find_chat_and_send(tg):
    assert db.get_setting("telegram_chat_id") == "777" and db.get_setting("telegram_chat_name") == "Магазин"
    notify.send_now("привет")
    assert tg.sent[0] == {"chat_id": "777", "text": "привет", "parse_mode": "HTML", "disable_web_page_preview": True}


def test_events_filter(tg):
    db.set_setting("telegram_events", "sync_failed")
    notify.send("order", "x", background=False)
    notify.send("sync_failed", "y", background=False)
    assert [m["text"] for m in tg.sent] == ["y"]


def test_bad_token_message(monkeypatch):
    real = notify._client
    monkeypatch.setattr(notify, "_client", lambda transport=None: real(
        httpx.MockTransport(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))))
    db.set_setting("telegram_token", "bad")
    with pytest.raises(notify.NotifyError, match="токен"):
        notify.find_chat()


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
    r = client.get("/api/orders").json()
    assert r["enabled"] is False and r["items"] == []
    assert client.post("/api/orders/refresh").status_code == 400
    s = client.post("/api/settings", json={"telegram_token": "123456:ABCDEFGHIJ", "telegram_events": ["order"]}).json()
    assert s["telegram_token"].startswith("1234") and s["telegram_events"] == ["order"]
    assert client.get("/orders").status_code == 200
