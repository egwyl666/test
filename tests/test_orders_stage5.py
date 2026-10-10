"""Этап 5 — заказы: все заказы и догон после простоя, поиск, допустимые переходы, ошибки Prom, очередь Telegram."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from promloader import db, notify, orders
from promloader.prom_api import PromClient, PromError


def order(oid, status="pending", day=1, name=("Іван", "Петренко"), phone="+380501234567", product="Котушка Shimano",
          modified=None):
    return {"id": oid, "status": status, "date_created": f"2026-09-{day:02d}T10:00:00+03:00",
            "date_modified": modified or f"2026-09-{day:02d}T10:00:00+03:00",
            "client_first_name": name[0], "client_last_name": name[1], "phone": phone, "email": "buyer@example.com",
            "full_price": "100 грн", "products": [{"name": product, "quantity": 1, "price": "100 грн"}]}


class FakeProm:
    """Как настоящий /orders/list: новые сверху, limit до 100, last_id — заказы с меньшим номером,
    last_modified_from — изменённые не раньше."""

    def __init__(self, items):
        self.items = {o["id"]: o for o in items}
        self.requests = []
        self.status_reply = None

    def handler(self, request):
        path = request.url.path
        if path.endswith("/orders/list"):
            q = dict(request.url.params)
            self.requests.append(q)
            rows = sorted(self.items.values(), key=lambda o: -o["id"])
            if "last_id" in q:
                rows = [o for o in rows if o["id"] < int(q["last_id"])]
            if "last_modified_from" in q:
                since = q["last_modified_from"]
                rows = [o for o in rows if o["date_modified"][:19] >= since]
            return httpx.Response(200, json={"orders": rows[: min(int(q.get("limit", 100)), 100)]})
        if path.endswith("/orders/set_status"):
            body = json.loads(request.content)
            if self.status_reply is not None:
                return self.status_reply
            for oid in body["ids"]:
                self.items[oid]["status"] = body["status"]
            return httpx.Response(200, json={"processed_ids": body["ids"]})
        return httpx.Response(404, json={})

    def client(self):
        return PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


def poll(fake):
    return asyncio.run(orders.poll(fake.client()))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: out.append((event, text)))
    return out


def test_first_poll_loads_all_orders_quietly(sent):
    """Раньше программа брала только последние 100 заказов."""
    fake = FakeProm([order(i) for i in range(1, 251)])
    assert poll(fake) == 0                                  # старые заказы — не «новые»
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 250
    assert orders.list_orders()["unseen"] == 0 and sent == []
    assert [r.get("last_id") for r in fake.requests] == [None, "151", "51"]
    assert db.get_setting("orders_synced_at") and not db.get_setting("orders_cursor")


def test_catch_up_after_downtime_notifies_once_per_order(sent):
    """Компьютер был выключен: за это время пришло 130 заказов — ни один не теряется."""
    fake = FakeProm([order(i) for i in range(1, 6)])
    poll(fake)
    for i in range(6, 136):
        fake.items[i] = order(i, day=20, modified="2026-10-08T09:00:00+03:00")
    db.set_setting("orders_synced_at", "2026-10-08T08:00:00+00:00")
    assert poll(fake) == 130
    since = fake.requests[-1]["last_modified_from"]
    assert since == "2026-10-08T04:00:00"                  # с запасом: Prom ждёт время без пояса
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 135
    assert len(sent) == orders.NOTIFY_EACH + 1 and "ещё 120" in sent[-1][1]
    assert orders.list_orders()["unseen"] == 130


def test_status_changed_in_prom_cabinet_reaches_the_program(sent):
    fake = FakeProm([order(1)])
    poll(fake)
    fake.items[1] = order(1, status="delivered", modified="2099-01-01T10:00:00+03:00")
    poll(fake)
    assert orders.list_orders()["items"][0]["status"] == "delivered"


def test_huge_shop_continues_next_time(sent, monkeypatch):
    monkeypatch.setattr(orders, "MAX_PAGES", 2)
    fake = FakeProm([order(i) for i in range(1, 451)])
    poll(fake)
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 200
    assert orders.list_orders()["loading_all"]
    poll(fake)
    poll(fake)
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 450
    assert not orders.list_orders()["loading_all"] and sent == []   # вся первая загрузка — без уведомлений
    assert orders.list_orders()["unseen"] == 0


def test_search_and_dates():
    orders.store([order(1001, day=1), order(1002, day=5, name=("Олена", "Коваль"), phone="+380671112233",
                                                product="Воблер Kosadaka"),
                  order(1003, day=9, status="delivered")], notify_new=False)
    ids = lambda **f: [o["id"] for o in orders.list_orders(f)["items"]]
    assert ids(q="1002") == [1002]
    assert ids(q="067 111-22-33") == [1002]
    assert ids(q="коваль олена") == [1002]                 # регистр и порядок слов не важны
    assert ids(q="ВОБЛЕР") == [1002]                       # по товару
    assert ids(date_from="2026-09-05") == [1003, 1002]
    assert ids(date_from="2026-09-02", date_to="2026-09-05") == [1002]
    res = orders.list_orders({"status": "pending", "q": "петренко"})
    assert res["total"] == 1 and res["counts"] == {"pending": 1, "delivered": 1}
    with pytest.raises(ValueError):
        orders.list_orders({"date_from": "вчера"})


def test_paging_with_total(client):
    orders.store([order(i, day=1 + i % 28) for i in range(1, 121)], notify_new=False)
    first = client.get("/api/orders", params={"limit": 50}).json()
    assert first["total"] == 120 and len(first["items"]) == 50
    rest = client.get("/api/orders", params={"limit": 50, "offset": 100}).json()
    assert len(rest["items"]) == 20
    assert client.get("/api/orders", params={"date_from": "x"}).status_code == 400


def test_only_valid_transitions():
    orders.store([order(1, status="delivered"), order(2, status="pending")], notify_new=False)
    fake = FakeProm([order(1, status="delivered"), order(2)])
    with pytest.raises(PromError, match="нельзя перевести"):
        asyncio.run(orders.set_status(fake.client(), 1, "canceled", "another"))
    view = orders.list_orders({"q": "2"})["items"][0]
    assert view["actions"] == {"received": "Принят", "canceled": "Отменён"}
    assert orders.view(db.query_one("SELECT * FROM orders WHERE id = 1"))["actions"] == {}


def test_prom_refusal_is_an_error_not_silence():
    """Раньше ответ Prom не проверялся: заказ в программе становился «Принят», а на Prom оставался «Новым»."""
    orders.store([order(7)], notify_new=False)
    fake = FakeProm([order(7)])
    fake.status_reply = httpx.Response(200, json={"processed_ids": [], "errors": {"7": "Заказ уже обработан"}})
    with pytest.raises(PromError, match="Заказ уже обработан"):
        asyncio.run(orders.set_status(fake.client(), 7, "received"))
    assert db.query_one("SELECT status FROM orders WHERE id = 7")["status"] == "pending"
    fake.status_reply = httpx.Response(403, text="<html>403 Forbidden</html>")
    with pytest.raises(PromError, match="права"):
        asyncio.run(orders.set_status(fake.client(), 7, "received"))


def test_status_change_marks_seen_and_updates():
    orders.store([order(1)], notify_new=False)
    db.set_setting("orders_initialized", "1")
    orders.store([order(2)], notify_new=False)
    fake = FakeProm([order(1), order(2)])
    v = asyncio.run(orders.set_status(fake.client(), 2, "received"))
    assert v["status"] == "received" and not v["new"] and fake.items[2]["status"] == "received"


def test_seen_api(client):
    db.set_setting("orders_initialized", "1")
    orders.store([order(1), order(2)], notify_new=False)
    client.post("/api/orders/seen", json={"ids": [1]})
    assert orders.list_orders()["unseen"] == 1
    client.post("/api/orders/seen", json={})
    assert orders.list_orders()["unseen"] == 0


# ---------- очередь Telegram ----------

class Flaky:
    def __init__(self):
        self.down = True
        self.sent = []

    def handler(self, request):
        if self.down:
            raise httpx.ConnectError("нет интернета")
        body = json.loads(request.content)
        self.sent.append((body["chat_id"], body["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.fixture
def tg(monkeypatch):
    t = Flaky()
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw:
                        real(token, method, httpx.MockTransport(t.handler), files, **kw))
    db.set_setting("telegram_token", "123:ABC")
    notify.add_recipient("777", "Магазин")
    return t


def test_notification_survives_lost_connection(tg):
    """Раньше уведомление о заказе без интернета терялось навсегда."""
    notify.send("order", "Заказ №1", background=False)
    notify.send("order", "Заказ №2", background=False)
    state = notify.outbox_state()
    assert tg.sent == [] and state["pending"] == 2 and "нет связи" in state["error"].lower()
    tg.down = False
    notify.flush()                                           # пауза перед повтором ещё не прошла
    assert tg.sent == []
    with db.tx() as c:
        c.execute("UPDATE tg_outbox SET next_at = ?", (db.now(),))
    notify.flush()
    assert tg.sent == [("777", "Заказ №1"), ("777", "Заказ №2")]   # порядок сохранён
    assert notify.outbox_state()["pending"] == 0


def test_notification_gives_up_after_two_days(tg):
    notify.send("order", "Старый заказ", background=False)
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("UPDATE tg_outbox SET created_at = ?, next_at = ?", (old, db.now()))
    notify.flush()
    assert notify.outbox_state() == {"pending": 0, "failed": 1, "error": "", "next_at": ""}


def test_notification_inside_rolled_back_preview_is_not_sent(tg):
    from promloader import changes
    tg.down = False

    def run():
        notify.send("supplier_failed", "понарошку")
    changes.preview(run)
    notify.flush()
    assert tg.sent == [] and notify.outbox_state()["pending"] == 0


def test_retry_now_api(client, tg):
    notify.send("order", "Заказ №3", background=False)
    tg.down = False
    r = client.post("/api/telegram/retry").json()
    assert r["sent"] == 1 and r["outbox"]["pending"] == 0


def test_poll_broken_midway_still_notifies_found_orders(sent):
    """Prom оборвал проверку на второй странице: о заказах с первой всё равно сообщаем (потом они уже не «новые»)."""
    fake = FakeProm([order(1)])
    poll(fake)
    for i in range(2, 152):
        fake.items[i] = order(i, modified="2099-01-01T10:00:00+03:00")
    real = fake.handler

    def broken(request):
        if "last_id" in dict(request.url.params):
            return httpx.Response(503, text="busy")
        return real(request)
    fake.handler = broken
    with pytest.raises(PromError):
        poll(fake)
    assert len(sent) == orders.NOTIFY_EACH + 1 and "ещё 90" in sent[-1][1]
    assert db.get_setting("orders_error")


def test_upgrade_from_last_100_does_not_flood_old_orders(sent):
    """Версия до 2.1 знала только последние 100 заказов. Первая полная загрузка после обновления не должна
    объявить остальные старые заказы «новыми» и засыпать Telegram."""
    fake = FakeProm([order(i) for i in range(1, 301)])
    orders.store([order(i) for i in range(201, 301)], notify_new=False)   # как было в старой версии
    db.set_setting("orders_initialized", "1")
    fake.items[301] = order(301)                                           # пришёл, пока обновлялись
    assert poll(fake) == 1
    assert [t for _, t in sent] and "№301" in sent[0][1] and len(sent) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 301
    assert orders.list_orders()["unseen"] == 1


def test_shop_without_api_is_not_called_a_token_problem():
    """С 2026-10-09 Prom отвечает магазину 403 «Api is not available for free premium service» (пакет без API).
    Программа писала «Prom отклонил токен» / «у токена нет права менять заказы» — и человек искал не там."""
    orders.store([order(7)], notify_new=False)
    fake = FakeProm([order(7)])
    page = "<html><h1>403 Forbidden</h1>Access was denied to this resource.<br />Api is not available for free premium service</html>"
    fake.status_reply = httpx.Response(403, text=page)
    with pytest.raises(PromError, match="пакет") as exc:
        asyncio.run(orders.set_status(fake.client(), 7, "received"))
    assert exc.value.no_api and "токен" not in str(exc.value).split(".")[0]
    fake.handler = lambda request: httpx.Response(403, text=page)
    with pytest.raises(PromError, match="платных пакетах"):
        poll(fake)
    assert "платных пакетах" in db.get_setting("orders_error")
