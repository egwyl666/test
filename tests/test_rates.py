import json
from xml.etree import ElementTree as ET

import httpx
import pytest

from promloader import db, excel, feed, pricing, products, rates, suppliers


def nbu_ok(rate=41.25):
    return httpx.MockTransport(lambda r: httpx.Response(200, json=[{"r030": 840, "cc": "USD", "rate": rate,
                                                                    "exchangedate": "04.10.2026"}]))


def nbu_down():
    return httpx.MockTransport(lambda r: httpx.Response(503))


def test_nbu_rate_cached_and_stale():
    assert rates.nbu("USD", nbu_ok())["rate"] == 41.25
    assert rates.nbu("USD", nbu_down())["rate"] == 41.25  # тот же день — из кеша, без запроса
    db.set_setting("nbu_rate_USD", json.dumps({"rate": 40.0, "date": "2026-01-01"}))
    r = rates.nbu("USD", nbu_down())
    assert r["rate"] == 40.0 and r["stale"]
    db.set_setting("nbu_rate_EUR", "")
    with pytest.raises(rates.RateError, match="вручную"):
        rates.nbu("EUR", nbu_down())


def test_converter_modes():
    assert rates.converter("", "USD", 0, 0) is None
    conv = rates.converter("manual", "USD", 40, 2.5)
    assert conv("USD") == pytest.approx(41.0) and conv(None) == pytest.approx(41.0) and conv("UAH") is None
    with pytest.raises(rates.RateError):
        rates.converter("manual", "USD", 0, 0)
    assert rates.converter("nbu", "USD", 0, 0, nbu_ok(40))("USD") == 40


def test_supplier_prices_converted_before_markup():
    sid = suppliers.create("Gold")
    pricing.save_rules([{"markup_percent": 30, "rounding": "int"}])
    suppliers.update(sid, {"rate_mode": "manual", "rate_value": "40", "rate_add": "0"})
    rows = [["@id", "price", "currencyId", "name"], ["1", "2.5", "USD", "Гачок"], ["2", "100", "UAH", "Тарілка"]]
    items = excel.build_products(rows, 1, [2, 3], {"A": "external_id", "B": "cost_price", "C": "currency", "D": "name"},
                                 None, None, pricing.Pricer(), sid, suppliers.converter_for(suppliers.get(sid)))
    a, b = items[0]["data"], items[1]["data"]
    assert a["cost_price"] == 100 and a["currency"] == "UAH" and a["price"] == 130  # 2.5$ × 40 = 100 грн + 30%
    assert b["cost_price"] == 100 and b["price"] == 130  # уже в гривнах — не трогаем


def test_supplier_rate_validation(client):
    sid = suppliers.create("X")
    assert client.patch(f"/api/suppliers/{sid}", json={"rate_mode": "bank"}).status_code == 400
    assert client.patch(f"/api/suppliers/{sid}", json={"rate_value": "abc"}).status_code == 400
    s = client.patch(f"/api/suppliers/{sid}", json={"rate_mode": "manual", "rate_value": "41,5", "rate_add": 2}).json()
    assert s["rate_mode"] == "manual" and s["rate_value"] == 41.5 and s["rate_add"] == 2


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
    assert "Нет названия на украинском" in products.get(ru)["check"]["warnings"]


def test_zero_supplier_price_is_not_a_price():
    pricing.save_rules([{"markup_percent": 40, "rounding": "end9"}])
    rows = [["@id", "price", "name"], ["1900", "0.00", "Форма для льоду"], ["2", "10", "Гачок"]]
    items = excel.build_products(rows, 1, [2, 3], {"A": "external_id", "B": "cost_price", "C": "name"},
                                 None, None, pricing.Pricer(), None)
    assert items[0]["data"].get("price") is None and items[0]["errors"]  # не 9 грн
    assert items[1]["data"]["price"] == 19
