"""2.24.0: надёжность покупателя (отказы от посылок), напоминание забрать посылку, замок ТТН в базе (виден другому
процессу), дубли между поставщиками — выгодный и «Оставить выгодный»."""

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from promloader import aibulk, db, duplicates, novaposhta, orders, products, suppliers, sync
from promloader.main import app
from promloader.prom_api import PromClient

from .conftest import sign_in
from .test_novaposhta import FakeNP, _room, prom_order, ready_sender

# ---------- 1. надёжность покупателя ----------


def order(oid, phone, status="delivered", ttn=None):
    return prom_order(oid, ttn=ttn, status=status) | {"phone": phone}


def ship(order_id, stage, arrived_days=None):
    arrived = ((datetime.now(timezone.utc) - timedelta(days=arrived_days)).isoformat(timespec="seconds")
               if arrived_days is not None else None)
    with db.tx() as c:
        c.execute("INSERT INTO shipments (order_id, ttn, own, stage, arrived_at, created_at) VALUES (?, ?, 1, ?, ?, ?)",
                  (order_id, f"2045{order_id:010d}", stage, arrived, db.now()))


def test_buyer_history_counts_refusals_across_phone_formats(client):
    orders.store([order(1, "+380 (50) 111-22-33"), order(2, "0501112233", status="canceled"),
                  order(3, "380501112233", status="received"), order(4, "+380501112233", status="pending"),
                  order(5, "+380679998877", status="pending")], notify_new=False)
    ship(3, "refused")
    h = orders.buyer_history("050 111 22 33", exclude_order_id=4)
    assert h == {"orders": 3, "delivered": 1, "canceled": 1, "refused": 1, "risky": True}
    items = {o["id"]: o for o in client.get("/api/orders").json()["items"]}
    assert items[4]["buyer"]["refused"] == 1 and items[4]["buyer"]["risky"]
    assert items[3]["buyer"]["refused"] == 0  # свой отказ не считается «в прошлом» у этого же заказа
    assert items[5]["buyer"] == {"orders": 0, "delivered": 0, "canceled": 0, "refused": 0, "risky": False}
    assert not orders.buyer_history("12")["risky"]  # короткий «телефон» ни с кем не совпадает


def test_repeated_cancellations_are_risky_too():
    orders.store([order(11, "+380631234567", status="canceled"), order(12, "+380631234567", status="canceled"),
                  order(13, "+380631234567", status="pending")], notify_new=False)
    h = orders.buyer_history("+380631234567", exclude_order_id=13)
    assert h["canceled"] == 2 and h["refused"] == 0 and h["risky"]
    assert not orders.buyer_history("+380631234567", exclude_order_id=12)["risky"]  # одна отмена — ещё не риск


def test_ttn_draft_warns_about_buyer(np, client):
    ready_sender()
    orders.store([order(21, "+380671112233", status="canceled"), order(22, "+380671112233", status="pending")],
                 notify_new=False)
    ship(21, "refused")
    d = client.get("/api/orders/22/ttn/draft").json()
    assert d["buyer"]["refused"] == 1 and d["buyer"]["risky"] and d["cod"] is True


@pytest.fixture
def np(monkeypatch):
    fake = FakeNP()
    transport = httpx.MockTransport(fake.handler)
    real = httpx.Client
    monkeypatch.setattr(novaposhta.httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    return fake


# ---------- 1б. «🔔 Напомнить в чат» ----------

class KeepingNP(FakeNP):
    """Отслеживание с датой платного хранения — как отвечает Нова Пошта."""

    def handler(self, request):
        r = super().handler(request)
        body = json.loads(request.content)
        if body["calledMethod"] == "getStatusDocuments":
            data = r.json()
            for d in data["data"]:
                d["DatePayedKeeping"] = "2026-10-28 00:00:00"
            return httpx.Response(200, json=data)
        return r


@pytest.fixture
def prom_chat(monkeypatch):
    sent = []

    def prom(request):
        if request.url.path.endswith("/chat/send_message"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "success", "data": {"message_id": 1}})
        return httpx.Response(200, json={"status": "success"})
    monkeypatch.setattr(sync, "make_client", lambda: PromClient("t", "https://x", transport=httpx.MockTransport(prom)))
    return sent


def test_remind_buyer_about_waiting_parcel(monkeypatch, client, prom_chat):
    fake = KeepingNP()
    real = httpx.Client
    monkeypatch.setattr(novaposhta.httpx, "Client",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(fake.handler)}))
    orders.store([prom_order(31, ttn="20451560371673", status="received"),
                  prom_order(32, ttn="20451560371674", status="received")], notify_new=False)
    _room("555_abc", order_id=31)
    novaposhta.track()
    assert client.post("/api/orders/31/remind").status_code == 400  # ещё в пути — напоминать не о чем
    fake.status = {"20451560371673": "7"}
    novaposhta.track()
    with db.tx() as c:  # лежит на отделении 4 дня
        c.execute("UPDATE shipments SET arrived_at = ? WHERE order_id = 31",
                  ((datetime.now(timezone.utc) - timedelta(days=4)).isoformat(timespec="seconds"),))
    s = next(o for o in client.get("/api/orders").json()["items"] if o["id"] == 31)["shipment"]
    assert s["stage"] == "arrived" and s["days_waiting"] == 4 and s["can_remind"] and s["keep_until"] == "2026-10-28"

    operator = sign_in(TestClient(app), "ira", "пароль-1234", "operator")
    r = operator.post("/api/orders/31/remind")
    assert r.status_code == 200
    assert prom_chat[0]["user_id"] == "555"
    assert prom_chat[0]["body"] == ("Вітаємо! Ваше замовлення #31 вже чекає на вас у відділенні Нової Пошти "
                                    "(Відділення №1). Номер ТТН: 20451560371673. Будь ласка, заберіть її до 28.10, поки "
                                    "зберігання безкоштовне. Дякуємо, що обрали наш магазин!")
    # второй оператор нажал ещё раз — не отправляем дважды
    again = client.post("/api/orders/31/remind")
    assert again.status_code == 409 and len(prom_chat) == 1
    s = next(o for o in client.get("/api/orders").json()["items"] if o["id"] == 31)["shipment"]
    assert s["reminded_at"] and not s["can_remind"]
    # через сутки — снова можно
    with db.tx() as c:
        c.execute("UPDATE shipments SET reminded_at = ? WHERE order_id = 31",
                  ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"),))
    assert client.post("/api/orders/31/remind").status_code == 200 and len(prom_chat) == 2
    # «Только просмотр» напоминать не может
    viewer = sign_in(TestClient(app), "ivan", "пароль-1234", "viewer")
    assert viewer.post("/api/orders/31/remind").status_code == 403


def test_remind_without_chat_room_explains(client, prom_chat):
    orders.store([prom_order(41, status="received")], notify_new=False)
    ship(41, "arrived", arrived_days=2)
    r = client.post("/api/orders/41/remind")
    assert r.status_code == 400 and "нет чата" in r.json()["detail"] and prom_chat == []
    assert not db.query_one("SELECT reminded_at FROM shipments WHERE order_id = 41")["reminded_at"]
    assert db.query_one("SELECT COUNT(*) AS n FROM locks")["n"] == 0


# ---------- 3. замок ТТН в базе ----------

def test_db_lock_basics(monkeypatch):
    owner = db.acquire("ttn:1", 60)
    assert owner and db.acquire("ttn:1", 60) is None
    db.release("ttn:1", "чужой")  # снять может только владелец
    assert db.acquire("ttn:1", 60) is None
    db.release("ttn:1", owner)
    assert db.acquire("ttn:1", 60)
    real = time.time
    monkeypatch.setattr(db.time, "time", lambda: real() + 61)  # истёк (процесс упал посреди запроса)
    assert db.acquire("ttn:1", 60)


def test_ttn_lock_is_seen_by_another_process(np, data_dir):
    """Вторая копия программы на тех же данных (или программа после перезапуска) держит замок — эта ТТН не создаёт."""
    ready_sender()
    orders.store([prom_order(51)], notify_new=False)
    form = novaposhta.draft(51)
    code = ("import sys; sys.path.insert(0, '.'); from promloader import db; db.init(sys.argv[1]); "
            "print(db.acquire('ttn:51', 600))")
    out = subprocess.run([sys.executable, "-c", code, str(data_dir)], capture_output=True, text=True, timeout=60,
                         cwd=str(__import__("pathlib").Path(__file__).parent.parent))
    assert out.returncode == 0 and out.stdout.strip() not in ("", "None"), out.stderr
    with pytest.raises(novaposhta.TTNConflict, match="уже создаётся"):
        novaposhta.create(51, form)
    assert not [c for c in np.calls if c[0] == "InternetDocument.save"]  # в Новую Почту не ходили


# ---------- 2. дубли между поставщиками ----------

def two_suppliers_same_item():
    cheap = suppliers.create("Дешёвий")
    pricey = suppliers.create("Дорогий")
    a = products.create({"name": "Павербанк X 10000", "barcode": "4820000000017", "price": 1000, "cost_price": 600,
                         "external_id": "A-1"})
    b = products.create({"name": "Павербанк X10000 чорний", "barcode": "4820000000017", "price": 1000,
                         "cost_price": 750, "external_id": "B-1"})
    c = products.create({"name": "Павербанк X10000 (нема)", "barcode": "4820000000017", "price": 1000,
                         "cost_price": 500, "external_id": "C-1", "presence": "not_available"})
    with db.tx() as conn:
        conn.execute("UPDATE products SET supplier_id = ? WHERE id = ?", (cheap, a))
        conn.execute("UPDATE products SET supplier_id = ? WHERE id IN (?, ?)", (pricey, b, c))
    return a, b, c


def test_best_offer_is_first_and_marked(client):
    a, b, c = two_suppliers_same_item()
    g, = duplicates.find()
    assert g["best"] == a and g["ids"] == [a, b, c]  # в наличии и наценка больше; «нема в наявності» — последним
    items = client.get("/api/products", params={"duplicates": "1"}).json()["items"]
    assert [i["id"] for i in items] == [a, b, c]
    assert items[0]["dup"]["best"] and not items[1]["dup"]["best"]


def test_keep_best_hides_others_and_keeps_them_out_of_pipeline(client):
    a, b, c = two_suppliers_same_item()
    with db.tx() as conn:  # один дубль уже выгружен на Prom
        conn.execute("UPDATE products SET status = 'synced', synced_at = ? WHERE id = ?", (db.now(), b))
    r = client.post("/api/products/duplicates/keep-best").json()
    assert r == {"groups": 1, "hidden": 2, "on_prom": 1, "busy": 0}
    assert products.get(a)["status"] == "draft" and products.get(a)["dup_of"] is None
    for pid in (b, c):
        p = products.get(pid)
        assert p["status"] == "draft" and p["dup_of"] == a and p["dup_of_product"]["id"] == a
    assert client.get("/api/products/alerts").json()["duplicates"] == 0
    row = db.query_one("SELECT source, new FROM product_changes WHERE product_id = ? AND field = 'status' "
                       "ORDER BY id DESC LIMIT 1", (b,))
    assert row["source"] == duplicates.DUP_SOURCE and row["new"] == products.STATUSES["draft"]
    # скрытые не «готовы» и не уходят в «В «Готов к отправке»» и автоконвейер, даже если всё заполнено
    for pid in (a, b, c):
        products.update(pid, {"name_ua": "Павербанк", "keywords": "павербанк"})
        with db.tx() as conn:
            conn.execute("INSERT INTO images (product_id, file, url, position) VALUES (?, '', 'https://x/1.jpg', 0)", (pid,))
    assert products.promote_ready({}) == 1 and products.get(a)["status"] == "ready"
    assert products.get(b)["status"] == "draft"
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    sid = products.get(b)["supplier_id"]
    assert aibulk.start_pipeline(sid) == {"pipeline_promoted": 0, "pipeline_queued": 0, "pipeline_manual": 0}


def test_unhide_and_manual_ready_bring_duplicate_back(client):
    a, b, c = two_suppliers_same_item()
    client.post("/api/products/duplicates/keep-best")
    r = client.post(f"/api/products/{b}/dup-unhide")
    assert r.status_code == 200 and r.json()["dup_of"] is None
    assert client.post("/api/products/status", json={"ids": [c], "status": "ready"}).status_code == 200
    assert products.get(c)["dup_of"] is None and products.get(c)["status"] == "ready"
    assert client.post("/api/products/999999/dup-unhide").status_code == 404
    viewer = sign_in(TestClient(app), "ivan", "пароль-1234", "viewer")
    assert viewer.post("/api/products/duplicates/keep-best").status_code == 403


def test_keep_best_skips_product_being_sent(client):
    a, b, c = two_suppliers_same_item()
    with db.tx() as conn:
        conn.execute("UPDATE products SET status = 'sending' WHERE id = ?", (b,))
    r = client.post("/api/products/duplicates/keep-best").json()
    assert r["busy"] == 1 and r["hidden"] == 1 and products.get(b)["status"] == "sending"
