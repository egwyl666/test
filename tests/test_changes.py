from promloader import changes, db, pricing, products, rates, suppliers


def rows(pid=None, **filters):
    return changes.search(period="all", product_id=pid, **filters)["items"]


def test_manual_edit_recorded_with_old_and_new(client):
    pid = products.create({"name": "Гачок", "price": 100})
    client.patch(f"/api/products/{pid}", json={"price": "120", "name": "Гачок карповий", "presence": "order"})
    got = {(r["field"], r["old"], r["new"], r["source"]) for r in rows(pid)}
    assert ("price", "100", "120", "Вручную") in got
    assert ("name", "Гачок", "Гачок карповий", "Вручную") in got
    assert ("presence", "В наличии", "Под заказ", "Вручную") in got
    assert any(r["field"] == "created" for r in rows(pid))
    client.patch(f"/api/products/{pid}", json={"price": "120"})  # без изменений — без записи
    assert sum(r["field"] == "price" for r in rows(pid)) == 1
    hist = client.get(f"/api/products/{pid}/changes").json()
    assert hist["items"][0]["product_name"] == "Гачок карповий" and hist["labels"]["price"] == "Цена"


def test_rate_change_is_attributed_to_rate():
    pricing.save_rules([{"markup_percent": 0, "rounding": "none"}])
    pid = products.create({"name": "Гачок", "cost_price": 1, "cost_currency": "USD", "price": 40, "currency": "UAH"})
    rates.check_changed()
    rates.save_settings({"mode": "manual", "manual": {"USD": 44}})
    rates.check_changed()
    price = [r for r in rows(pid) if r["field"] == "price"]
    assert price[0]["old"] == "40" and price[0]["new"] == "44" and price[0]["source"].startswith("Курс валют (1 $ = 44.00")


def test_bulk_price_source_and_filters(client):
    pricing.save_rules([{"markup_percent": 0, "rounding": "none"}])
    a = products.create({"name": "Гачок", "price": 100})
    b = products.create({"name": "Ліхтар", "price": 200})
    client.post("/api/products/prices", json={"ids": [a, b], "action": "percent", "value": 10})
    data = client.get("/api/changes", params={"period": "today", "field": "price"}).json()
    assert data["total"] == 2 and data["summary"] == {"price": 2}
    assert all(r["source"] == "Массово «💲 Цены»: +10%" for r in data["items"])
    assert client.get("/api/changes", params={"q": "Ліхтар", "field": "price"}).json()["total"] == 1
    who = data["items"][0]["source"]
    assert who in data["sources"]
    assert client.get("/api/changes", params={"who": "Вручную", "field": "price"}).json()["total"] == 0
    csv = client.get("/api/changes.csv", params={"field": "price"})
    assert csv.status_code == 200 and csv.content.startswith("﻿".encode())
    text = csv.content.decode("utf-8-sig")
    assert "Ліхтар" in text and "200;220" in text


def test_supplier_run_recorded_as_supplier():
    from .test_suppliers import BASE, make_supplier, make_yml, run

    sid = make_supplier(make_yml(BASE))
    run(sid)
    pid = db.query_one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
    assert rows(pid)[0]["source"] == "Поставщик «Посуда-опт»"


def test_deleted_product_still_in_journal(client):
    pid = products.create({"name": "Гачок", "price": 100})
    client.post("/api/products/delete", json={"ids": [pid]})
    data = client.get("/api/changes", params={"period": "all"}).json()
    assert {r["field"] for r in data["items"]} >= {"created", "deleted"}
    assert client.get("/").status_code == 200 and client.get("/changes").status_code == 200


def test_cleanup_drops_old_records():
    pid = products.create({"name": "Гачок", "price": 100})
    with db.tx() as c:
        c.execute("UPDATE product_changes SET at = '2020-01-01T00:00:00+00:00'")
    assert changes.cleanup() >= 1 and rows(pid) == []


def test_revert_price_and_presence_from_journal(client):
    pid = products.create({"name": "Гачок", "price": 100})
    client.patch(f"/api/products/{pid}", json={"price": "150", "presence": "order"})
    items = {r["field"]: r for r in client.get(f"/api/products/{pid}/changes").json()["items"]}
    assert items["price"]["revertable"] and items["presence"]["revertable"] and not items["created"]["revertable"]
    p = client.post(f"/api/changes/{items['price']['id']}/revert").json()
    assert p["price"] == 100
    p = client.post(f"/api/changes/{items['presence']['id']}/revert").json()
    assert p["presence"] == "available"       # в журнале «В наличии» — возвращается код наличия
    last = client.get(f"/api/products/{pid}/changes").json()["items"][0]
    assert last["source"].startswith("Откат") and last["new"] == "В наличии"


def test_revert_refuses_truncated_text_and_deleted_product(client):
    pid = products.create({"name": "Гачок", "price": 1, "description": "x" * 1000})
    client.patch(f"/api/products/{pid}", json={"description": "коротко"})
    ch = [r for r in client.get(f"/api/products/{pid}/changes").json()["items"] if r["field"] == "description"][0]
    assert not ch["revertable"]
    assert client.post(f"/api/changes/{ch['id']}/revert").status_code == 400
    client.patch(f"/api/products/{pid}", json={"price": "5"})
    price = [r for r in client.get(f"/api/products/{pid}/changes").json()["items"] if r["field"] == "price"][0]
    client.post("/api/products/delete", json={"ids": [pid]})
    r = client.post(f"/api/changes/{price['id']}/revert")
    assert r.status_code == 400 and "нет" in r.json()["detail"]
