"""2.26.0: склад — скан ТТН / номера заказа, «📦 Упакован» (режим упаковки), поиск заказов по ТТН."""

from fastapi.testclient import TestClient

from promloader import db, orders
from promloader.main import app

from .conftest import sign_in
from .test_novaposhta import prom_order


def shop():
    orders.store([prom_order(201, status="received"), prom_order(202, ttn="20451560371674", status="received"),
                  prom_order(203, status="canceled"), prom_order(204, status="pending")], notify_new=False)
    with db.tx() as c:
        c.execute("INSERT INTO shipments (order_id, ttn, own, stage, created_at) VALUES (201, '20450000000201', 1, 'created', ?)",
                  (db.now(),))


def test_scan_finds_order_by_ttn_or_number(client):
    shop()
    r = client.get("/api/orders/scan", params={"code": "20450000000201"}).json()
    assert r["kind"] == "ttn" and r["order"]["id"] == 201 and r["order"]["shipment"]["ttn"] == "20450000000201"
    # сканер иногда даёт лишнее: пробелы, подпись — берём цифры
    assert client.get("/api/orders/scan", params={"code": "TTN: 2045 0000 0002 01"}).json()["order"]["id"] == 201
    # ТТН из кабинета Prom (в данных заказа, а не в shipments)
    assert client.get("/api/orders/scan", params={"code": "20451560371674"}).json()["order"]["id"] == 202
    r = client.get("/api/orders/scan", params={"code": "№204"}).json()
    assert r["kind"] == "order" and r["order"]["id"] == 204
    missing = client.get("/api/orders/scan", params={"code": "20459999999999"})
    assert missing.status_code == 404 and "не найдена" in missing.json()["detail"]
    assert client.get("/api/orders/scan", params={"code": "abc"}).status_code == 400
    assert client.get("/api/orders/scan").status_code == 400


def test_order_search_finds_ttn(client):
    shop()
    ids = [o["id"] for o in client.get("/api/orders", params={"q": "20450000000201"}).json()["items"]]
    assert ids == [201]
    ids = [o["id"] for o in client.get("/api/orders", params={"q": "20451560371674"}).json()["items"]]
    assert ids == [202]


def test_packing_flow_and_guards(client):
    shop()
    users_name = client.get("/api/me").json()
    r = client.post("/api/orders/201/packed", json={"packed": True})
    assert r.status_code == 200 and r.json()["warnings"] == []
    o = r.json()["order"]
    assert o["packed_at"] and o["packed_by"] == (users_name["name"] or users_name["login"])
    # второй скан той же посылки — не перезаписываем «кто и когда»
    again = client.post("/api/orders/201/packed", json={"packed": True})
    assert again.status_code == 409 and "уже упакован" in again.json()["detail"]
    # отменённый не упаковываем
    r = client.post("/api/orders/203/packed", json={"packed": True})
    assert r.status_code == 409 and "отменён" in r.json()["detail"]
    assert not db.query_one("SELECT packed_at FROM orders WHERE id = 203")["packed_at"]
    # без ТТН — можно, но с предупреждением
    r = client.post("/api/orders/204/packed", json={"packed": True}).json()
    assert r["warnings"] == ["У заказа ещё нет ТТН"]
    # снять отметку
    r = client.post("/api/orders/201/packed", json={"packed": False}).json()
    assert r["order"]["packed_at"] == "" and r["order"]["packed_by"] == ""
    items = {o["id"]: o for o in client.get("/api/orders").json()["items"]}
    assert items[204]["packed_at"] and not items[201]["packed_at"]
    assert client.post("/api/orders/9999/packed", json={"packed": True}).status_code == 404


def test_packing_warns_about_risky_buyer(client):
    shop()
    with db.tx() as c:
        c.execute("UPDATE orders SET prepay_state = 'required' WHERE id = 202")
    r = client.post("/api/orders/202/packed", json={"packed": True}).json()
    assert r["warnings"] == ["Рисковый покупатель: предоплата не отмечена — проверьте перед отправкой"]


def test_packing_permissions(client):
    shop()
    viewer = sign_in(TestClient(app), "ivan", "пароль-1234", "viewer")
    assert viewer.get("/api/orders/scan", params={"code": "204"}).status_code == 200  # найти — может каждый
    assert viewer.post("/api/orders/204/packed", json={"packed": True}).status_code == 403
    operator = sign_in(TestClient(app), "ira", "пароль-1234", "operator")
    r = operator.post("/api/orders/204/packed", json={"packed": True})
    assert r.status_code == 200 and r.json()["order"]["packed_by"] == "ira"
