import json
from xml.etree import ElementTree as ET

import httpx
import pytest

from promloader import db, excel, feed, pricing, products, rates, suppliers

from .conftest import REAL_NBU


def nbu_ok(rate=41.25):
    return httpx.MockTransport(lambda r: httpx.Response(200, json=[{"r030": 840, "cc": "USD", "rate": rate,
                                                                    "exchangedate": "04.10.2026"}]))


def nbu_down():
    return httpx.MockTransport(lambda r: httpx.Response(503))


def test_nbu_rate_cached_and_stale():
    assert REAL_NBU("USD", nbu_ok())["rate"] == 41.25
    assert REAL_NBU("USD", nbu_down())["rate"] == 41.25  # тот же день — из кеша, без запроса
    db.set_setting("nbu_rate_USD", json.dumps({"rate": 40.0, "date": "2026-01-01"}))
    r = REAL_NBU("USD", nbu_down())
    assert r["rate"] == 40.0 and r["stale"]
    with pytest.raises(rates.RateError, match="свой курс"):
        REAL_NBU("EUR", nbu_down())


def test_general_rate_modes_and_supplier_override():
    t = rates.Table()
    assert t.rate("USD") == 40 and t.rate("UAH") is None and t.rate(None) is None
    rates.save_settings({"add": "2,5"})
    assert rates.Table().rate("USD") == pytest.approx(41.0)
    rates.save_settings({"mode": "manual", "manual": {"USD": "42"}})
    assert rates.Table().rate("USD") == 42
    with pytest.raises(rates.RateError, match="EUR"):
        rates.Table().rate("EUR")
    sid = suppliers.create("Опт")
    suppliers.update(sid, {"rate_mode": "manual", "rate_value": "41,8"})
    assert rates.Table().rate("USD", sid) == pytest.approx(41.8)  # свой курс поставщика
    with pytest.raises(rates.RateError):
        rates.save_settings({"mode": "bank"})


def test_cost_stays_in_dollars_price_in_hryvnia():
    pricing.save_rules([{"markup_percent": 30, "rounding": "int"}])
    rows = [["@id", "price", "currencyId", "name"], ["1", "2.5", "USD", "Гачок"], ["2", "100", "UAH", "Тарілка"]]
    items = excel.build_products(rows, 1, [2, 3], {"A": "external_id", "B": "cost_price", "C": "currency", "D": "name"},
                                 None, None, pricing.Pricer(), None)
    a, b = items[0]["data"], items[1]["data"]
    assert a["cost_price"] == 2.5 and a["cost_currency"] == "USD" and a["price"] == 130 and a["currency"] == "UAH"
    assert b["cost_price"] == 100 and b["cost_currency"] == "UAH" and b["price"] == 130


def test_retail_price_in_dollars_becomes_rrp():
    rows = [["id", "price", "currency", "name"], ["1", "10", "USD", "Ліхтар"]]
    d = excel.build_products(rows, 1, [2], {"A": "external_id", "B": "price", "C": "currency", "D": "name"},
                             None, None, pricing.Pricer(), None)[0]["data"]
    assert d["rrp"] == 10 and d["cost_currency"] == "USD" and d["price"] == 400 and d["currency"] == "UAH"


def test_rate_change_recalculates_and_sends(monkeypatch):
    from promloader import sync
    pricing.save_rules([{"markup_percent": 0, "rounding": "none"}])
    pid = products.create({"name": "Гачок", "cost_price": 1, "cost_currency": "USD", "price": 40, "currency": "UAH"})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?, pending_fields = '[]' WHERE id = ?", (db.now(), pid))
    sent = []
    monkeypatch.setattr(sync, "enqueue", lambda ids: sent.extend(ids) or {"accepted": len(ids), "rejected": []})
    rates.save_settings({"mode": "manual", "manual": {"USD": 45}})
    res = suppliers.recalc_prices()
    assert res["changed"] == 1 and sent == [pid] and products.get(pid)["price"] == 45
    rates.save_settings({"manual": {"USD": 46}, "auto_send": False})
    sent.clear()
    assert suppliers.recalc_prices()["changed"] == 1 and sent == []


def test_missing_rate_leaves_price_and_warns():
    rates.save_settings({"mode": "manual", "manual": {}})
    pid = products.create({"name": "Гачок", "cost_price": 1, "cost_currency": "USD", "price": 40})
    res = suppliers.recalc_prices()
    assert res["changed"] == 0 and res["warnings"] and products.get(pid)["price"] == 40


def test_ukrainian_text_also_sent_as_ukrainian():
    assert products.looks_ukrainian("Ліхтарик брелок з карабіном")
    assert not products.looks_ukrainian("Паровая швабра 12в1 с насадками") and not products.looks_ukrainian("Powerbank")
    ua = products.create({"name": "Ліхтарик брелок", "price": 10, "description": "Зручний ліхтарик."})
    ru = products.create({"name": "Паровая швабра", "price": 10})
    own = products.create({"name": "Ліхтар", "name_ua": "Ліхтар великий", "price": 10})
    offers = {o.findtext("name"): o for o in ET.fromstring(feed.build([ua, ru, own], "")).iter("offer")}
    assert offers["Ліхтарик брелок"].findtext("name_ua") == "Ліхтарик брелок"
    assert "ліхтарик" in offers["Ліхтарик брелок"].findtext("description_ua")
    assert offers["Паровая швабра"].find("name_ua") is None
    assert offers["Ліхтар"].findtext("name_ua") == "Ліхтар великий"
    assert "Нет названия на украинском" not in products.get(ua)["check"]["warnings"]


def test_zero_supplier_price_is_not_a_price():
    pricing.save_rules([{"markup_percent": 40, "rounding": "end9"}])
    rows = [["@id", "price", "name"], ["1900", "0.00", "Форма для льоду"], ["2", "10", "Гачок"]]
    items = excel.build_products(rows, 1, [2, 3], {"A": "external_id", "B": "cost_price", "C": "name"},
                                 None, None, pricing.Pricer(), None)
    assert items[0]["data"].get("price") is None and items[0]["errors"]
    assert items[1]["data"]["price"] == 19


def test_hourly_check_recalculates_only_on_change():
    pricing.save_rules([{"markup_percent": 0, "rounding": "none"}])
    pid = products.create({"name": "Гачок", "cost_price": 1, "cost_currency": "USD", "price": 40, "currency": "UAH"})
    assert rates.check_changed() is None  # первый раз — только запомнить курс
    assert rates.check_changed() is None  # курс тот же
    rates.save_settings({"mode": "manual", "manual": {"USD": 44}})
    res = rates.check_changed()
    assert res["changed"] == 1 and products.get(pid)["price"] == 44
    assert rates.check_changed() is None


def test_editing_cost_in_card_recalculates_price(client):
    pricing.save_rules([{"markup_percent": 50, "rounding": "int"}])
    pid = products.create({"name": "Гачок", "price": 1})
    p = client.patch(f"/api/products/{pid}", json={"cost_price": "2", "cost_currency": "USD"}).json()
    assert p["price"] == 120 and p["currency"] == "UAH" and p["cost_uah"] == 80  # 2 $ × 40 × 1.5
    client.patch(f"/api/products/{pid}", json={"price": "150"})  # цена руками — закреплена
    p = client.patch(f"/api/products/{pid}", json={"cost_price": "3"}).json()
    assert p["price"] == 150


def test_bulk_wholesale_price_becomes_cost(client):
    pricing.save_rules([{"markup_percent": 50, "rounding": "int"}])
    a = products.create({"name": "Гачок", "price": 2.5, "currency": "USD"})        # опт загрузили как цену
    b = products.create({"name": "Ліхтар", "rrp": 10, "cost_currency": "USD", "price": 400})  # из прайса в $ — РРЦ
    res = client.post("/api/products/prices", json={"ids": [a, b], "action": "as_cost"}).json()
    pa, pb = products.get(a), products.get(b)
    assert pa["cost_price"] == 2.5 and pa["cost_currency"] == "USD" and pa["price"] == 150 and pa["currency"] == "UAH"
    assert pb["cost_price"] == 10 and pb["rrp"] is None and pb["price"] == 600
    assert res["changed"] == 2 and not res["warnings"]


def test_bulk_percent_and_recalc(client):
    pricing.save_rules([{"markup_percent": 0, "rounding": "none"}])
    pid = products.create({"name": "Гачок", "cost_price": 1, "cost_currency": "USD", "price": 40, "currency": "UAH"})
    client.post("/api/products/prices", json={"ids": [pid], "action": "percent", "value": 10})
    assert products.get(pid)["price"] == 44
    assert suppliers.recalc_prices()["changed"] == 0  # ручная цена не пересчитывается
    client.post("/api/products/prices", json={"ids": [pid], "action": "recalc"})
    assert products.get(pid)["price"] == 40
    assert client.post("/api/products/prices", json={"ids": [pid], "action": "x"}).status_code == 400


def test_return_to_prom_status(client):
    on = products.create({"name": "Був на Prom", "price": 10})
    never = products.create({"name": "Не був", "price": 10})
    with db.tx() as c:
        c.execute("UPDATE products SET synced_at = ?, status = 'ready' WHERE id = ?", (db.now(), on))
    res = client.post("/api/products/status", json={"ids": [on, never], "status": "synced"}).json()
    assert res["changed"] == 1 and products.get(on)["status"] == "synced" and products.get(never)["status"] == "draft"
