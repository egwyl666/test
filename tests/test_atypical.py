"""Нетипичное использование: странные данные не должны давать ошибку 500 или тихо портить товар."""
from xml.etree import ElementTree as ET

import pytest

from promloader import feed, pricing, products, schedule, suppliers


@pytest.mark.parametrize("raw, expected", [("1e10", 1e10), ("1,2E+03", 1200.0), ("100 USD", 100.0),
                                           ("12 345,67 грн", 12345.67)])
def test_numbers_are_not_silently_mangled(raw, expected):
    assert products.parse_number(raw) == expected


@pytest.mark.parametrize("raw", ["abc10", "дорого", "∞", "NaN", "1.5.3"])
def test_bad_numbers_rejected(raw):
    with pytest.raises(products.ProductError):
        products.parse_number(raw)


def test_field_name_in_error():
    pid = products.create({"name": "x"})
    with pytest.raises(products.ProductError) as exc:
        products.update(pid, {"vendor": "ok", "price": "дорого"})
    assert exc.value.field == "price"


def test_excel_control_chars_keep_feed_valid():
    pid = products.create({"name": "Кружка\x0bбелая\x00", "price": 1, "description": "a\x1fb",
                           "params": [{"name": "Цвет\x08", "value": "бел\x0bый"}]})
    p = products.get(pid)
    assert p["name"] == "Кружка\nбелая" and p["params"] == [{"name": "Цвет", "value": "бел\nый"}]
    ET.fromstring(feed.build([pid], ""))


@pytest.mark.parametrize("params", ["не json", 5, {"a": 1}])
def test_bad_params(params):
    with pytest.raises(products.ProductError):
        products.normalize({"params": params})
    assert products.parse_params(["строка", None, {"name": "Цвет", "value": "Белый"}]) == [{"name": "Цвет", "value": "Белый"}]


def test_misc_validation():
    with pytest.raises(ValueError):
        pricing.save_rules([{"supplier_id": 999, "markup_percent": 1}])
    with pytest.raises(ValueError):
        pricing.save_rules("не список")
    sid = suppliers.create("")
    with pytest.raises(suppliers.SupplierError):
        suppliers.update(sid, {"header_row": "abc"})
    with pytest.raises(schedule.ScheduleError):
        schedule.create({"days": "пн"})


def test_api_edge_cases(client):
    pid = client.post("/api/products", json={"name": "x"}).json()["id"]
    r = client.patch(f"/api/products/{pid}", json={"price": "дорого"})
    assert r.status_code == 400 and r.json()["field"] == "price"
    assert client.patch(f"/api/products/{pid}", json={"params": "строка"}).status_code == 400
    up = client.post("/api/import/upload", files={"file": ("a.csv", "Название;Цена\nA;1\n".encode())}).json()
    r = client.post(f"/api/import/{up['token']}/preview", json={"sheet": "CSV", "header_row": 1, "rows": "2-",
                                                                "mapping": {"1": "name"}})
    assert r.status_code == 400
    assert client.post("/api/settings", json={"public_base_url": "shop.example.com"}).json()["public_base_url"] == \
        "https://shop.example.com"
    assert client.post("/api/settings", json={"public_base_url": "not a url"}).status_code == 400
    for _ in range(3):
        client.post("/api/products", json={"name": "y"})
    assert len(client.get("/api/products", params={"limit": -5}).json()["items"]) == 1
    assert client.get("/api/products", params={"offset": -10}).status_code == 200
