import json

import httpx
import pytest

from promloader import db, excel, pricing, products, suppliers, sync

YML = """<?xml version="1.0" encoding="windows-1251"?>
<yml_catalog date="2026-09-01 10:00">
<shop>
  <categories>
    <category id="10">Кружки</category>
    <category id="20">Тарелки</category>
  </categories>
  <offers>
    {offers}
  </offers>
</shop>
</yml_catalog>"""

OFFER = """<offer id="{sku}" available="{available}">
  <name>{name}</name>
  <price>{price}</price>
  <categoryId>{cat}</categoryId>
  <picture>https://sup.example.com/{sku}-1.jpg</picture>
  <picture>https://sup.example.com/{sku}-2.jpg</picture>
  <vendor>Luminarc</vendor>
  <vendorCode>V-{sku}</vendorCode>
  <param name="Цвет">Белый</param>
  <description><![CDATA[<p>Описание {sku}</p>]]></description>
</offer>"""


def make_yml(items) -> bytes:
    offers = "\n".join(
        OFFER.format(sku=sku, name=name, price=price, cat=cat, available=avail)
        for sku, name, price, cat, avail in items
    )
    return YML.format(offers=offers).encode("cp1251")


BASE = [
    ("A1", "Кружка белая", "100", "10", "true"),
    ("A2", "Кружка синяя", "150.50", "10", "true"),
    ("B1", "Тарелка", "80", "20", "false"),
]


def test_yml_becomes_table(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_bytes(make_yml(BASE))
    assert excel.sheet_names(path) == ["XML"]
    rows, _ = excel.read_sheet(path, "XML")
    header = rows[0]
    assert header[:2] == ["@id", "@available"]
    assert "param:Цвет" in header and "category" in header
    first = dict(zip(header, rows[1]))
    assert first["picture"] == "https://sup.example.com/A1-1.jpg\nhttps://sup.example.com/A1-2.jpg"
    assert first["category"] == "Кружки"
    assert first["description"] == "<p>Описание A1</p>"

    mapping = excel.guess_mapping(header)
    by_target = {v: header[excel.openpyxl.utils.column_index_from_string(k) - 1] for k, v in mapping.items() if v != "param"}
    assert by_target["external_id"] == "@id"
    assert by_target["presence"] == "@available"
    assert by_target["name"] == "name"
    assert by_target["price"] == "price"
    assert by_target["images"] == "picture"
    assert by_target["group_name"] == "category"
    assert by_target["vendor"] == "vendor"


def test_detect_suffix():
    assert excel.detect_suffix(b"\xef\xbb\xbf<?xml version='1.0'?><a/>") == ".xml"
    assert excel.detect_suffix(b"PK\x03\x04....") == ".xlsx"
    assert excel.detect_suffix(b"name;price\n") == ".csv"
    with pytest.raises(excel.ImportError_):
        excel.detect_suffix(b"<!DOCTYPE html><html>")


# ---------- наценка ----------

@pytest.mark.parametrize("value, mode, expected", [
    (123.456, "none", 123.46), (123.2, "int", 124), (1231, "end9", 1239), (1240, "end9", 1249),
    (1239, "end9", 1239), (121, "tens", 130), (1234, "end99", 1299), (120, "tens", 120),
])
def test_rounding(value, mode, expected):
    assert pricing.round_price(value, mode) == expected


def test_rules_order_and_matching():
    sid = suppliers.create("S")
    pricing.save_rules([
        {"supplier_id": sid, "category": "кружк", "markup_percent": 50, "rounding": "int"},
        {"cost_from": 1000, "markup_percent": 10},
        {"markup_percent": 30, "markup_fixed": 5, "rounding": "end9"},
        {"supplier_id": sid, "use_rrp": True, "markup_percent": 99},
    ])
    p = pricing.Pricer()
    assert p.price(100, None, sid, "Кружки")[0] == 150
    assert p.price(2000, None, sid, "Тарелки")[0] == 2200
    assert p.price(100, None, None, "")[0] == 139  # 135 -> ...9
    assert p.price(None)[0] is None

    pricing.save_rules([{"use_rrp": True, "markup_percent": 20}])
    assert pricing.Pricer().price(100, 175)[0] == 175
    assert pricing.Pricer().price(100, None)[0] == 120

    data = {"cost_price": 50.0}
    assert pricing.Pricer([]).apply(data) == ["Нет правила наценки — цена равна закупочной"]
    assert data["price"] == 50.0


def test_markup_never_below_cost():
    pricing.save_rules([{"markup_percent": -50}])
    assert pricing.Pricer().price(100)[0] == 100


# ---------- обновление поставщика ----------

MAPPING_YML = {
    "A": "external_id", "B": "presence", "C": "name", "D": "cost_price", "E": "",
    "F": "images", "G": "vendor", "H": "", "I": "param", "J": "description", "K": "group_name",
}


def make_supplier(content: bytes, **settings) -> int:
    sid = suppliers.create("Посуда-опт")
    suppliers.store_source(sid, content, "feed.xml")
    suppliers.update(sid, {"mapping": MAPPING_YML, "header_row": 1, **settings})
    return sid


def run(sid, force=False):
    return suppliers.run(sid, force=force)


def products_by_sku():
    return {p["external_id"]: p for p in products.list_products(limit=1000)["items"]}


def test_supplier_full_cycle():
    rows, _ = excel.read_sheet(_tmp_feed(make_yml(BASE)), "XML")
    assert rows[0][:11] == ["@id", "@available", "name", "price", "categoryId", "picture", "vendor",
                            "vendorCode", "param:Цвет", "description", "category"]

    pricing.save_rules([{"markup_percent": 30, "rounding": "end9"}])
    sid = make_supplier(make_yml(BASE), prefix="PO-")
    r = run(sid)
    assert r["status"] == "ok", r["message"]
    assert r["stats"]["created"] == 3

    items = products_by_sku()
    a1 = products.get(items["PO-A1"]["id"])
    assert a1["cost_price"] == 100 and a1["price"] == 139
    assert a1["group_name"] == "Кружки" and a1["vendor"] == "Luminarc"
    assert a1["params"] == [{"name": "Цвет", "value": "Белый"}]
    assert [i["src"] for i in a1["images"]] == ["https://sup.example.com/A1-1.jpg", "https://sup.example.com/A1-2.jpg"]
    assert a1["status"] == "draft" and a1["supplier"]["name"] == "Посуда-опт"
    assert items["PO-B1"]["presence"] == "not_available"

    # пользователь поправил название и фото
    products.update(a1["id"], {"name": "Кружка белая 300 мл — хит"})
    products.delete_image(a1["id"], a1["images"][1]["id"])
    assert set(products.get(a1["id"])["locked_fields"]) == {"name", "images"}

    # новый прайс: цена A1 выросла, A2 пропал, появился C1
    changed = [("A1", "Кружка белая", "120", "10", "true"), ("B1", "Тарелка", "80", "20", "false"),
               ("C1", "Чашка", "60", "10", "true")]
    suppliers.store_source(sid, make_yml(changed), "feed.xml")
    r = run(sid)
    assert r["status"] == "ok", r["message"]
    s = r["stats"]
    assert (s["created"], s["updated"], s["missing"], s["price_changed"]) == (1, 1, 1, 1)
    assert s["unchanged"] == 1

    a1 = products.get(a1["id"])
    assert a1["name"] == "Кружка белая 300 мл — хит"  # правка сохранилась
    assert a1["price"] == 159 and a1["cost_price"] == 120
    assert len(a1["images"]) == 1  # фото не вернулись
    a2 = products_by_sku()["PO-A2"]
    assert a2["presence"] == "not_available"
    assert products.get(a2["id"])["supplier"]["missing"] is True

    # A2 вернулся
    suppliers.store_source(sid, make_yml(changed + [BASE[1]]), "feed.xml")
    r = run(sid)
    assert r["stats"]["returned"] == 1
    assert products_by_sku()["PO-A2"]["presence"] == "available"

    # снятие закрепления возвращает данные поставщика
    products.unlock(a1["id"], ["name", "images"])
    suppliers.reapply(a1["id"])
    a1 = products.get(a1["id"])
    assert a1["name"] == "Кружка белая" and len(a1["images"]) == 2


def _tmp_feed(content: bytes):
    path = db.data_dir() / "tmp_feed.xml"
    path.write_bytes(content)
    return path


def test_broken_feed_protection():
    many = [(f"S{i}", f"Товар {i}", "10", "10", "true") for i in range(30)]
    sid = make_supplier(make_yml(many))
    assert run(sid)["stats"]["created"] == 30

    suppliers.store_source(sid, make_yml(many[:5]), "feed.xml")
    r = run(sid)
    assert r["status"] == "failed" and "сломан" in r["message"]
    assert all(p["presence"] == "available" for p in products_by_sku().values())

    r = run(sid, force=True)
    assert r["status"] == "ok" and r["stats"]["missing"] == 25


def test_requires_key_column():
    sid = make_supplier(make_yml(BASE))
    suppliers.update(sid, {"mapping": {"C": "name", "D": "price"}})
    r = run(sid)
    assert r["status"] == "failed" and "Артикул" in r["message"]


def test_adopts_existing_product_and_rejects_foreign():
    own = products.create({"name": "Уже был", "price": 1, "external_id": "A1"})
    other_sid = suppliers.create("Другой")
    foreign = products.create({"name": "Чужой", "price": 1, "external_id": "A2"})
    with db.tx() as c:
        c.execute("UPDATE products SET supplier_id = ? WHERE id = ?", (other_sid, foreign))

    sid = make_supplier(make_yml(BASE))
    r = run(sid)
    assert r["stats"]["created"] == 1 and r["stats"]["updated"] == 1 and r["stats"]["errors"] == 1
    assert "другого поставщика" in r["stats"]["error_samples"][0]
    assert products.get(own)["supplier_id"] == sid
    assert products.get(own)["name"] == "Кружка белая"


def test_auto_sync_queues_only_ready(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x.example.com")
    sid = make_supplier(make_yml(BASE), new_status="ready", auto_sync=True)
    r = run(sid)
    assert r["stats"]["queued"] == 3
    job = sync.list_jobs()[0]
    assert job["count"] == 3

    # следующий прогон без изменений ничего не ставит
    with db.tx() as c:
        c.execute("UPDATE sync_jobs SET status = 'done'")
        c.execute("UPDATE products SET status = 'synced'")
    assert run(sid)["stats"]["queued"] == 0


def test_download_and_google_sheets_link():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=make_yml(BASE))

    sid = make_supplier(b"<a><offer><name>x</name></offer></a>")
    suppliers.update(sid, {"url": "https://supplier.example.com/feed.xml"})
    r = suppliers.run(sid, transport=httpx.MockTransport(handler))
    assert r["status"] == "ok" and r["stats"]["created"] == 3
    assert seen == ["https://supplier.example.com/feed.xml"]

    assert suppliers._direct_url("https://docs.google.com/spreadsheets/d/abc-1_2/edit#gid=0") == \
        "https://docs.google.com/spreadsheets/d/abc-1_2/export?format=xlsx"

    bad = httpx.MockTransport(lambda r: httpx.Response(404))
    r = suppliers.run(sid, transport=bad)
    assert r["status"] == "failed" and "404" in r["message"]


def test_recalc_prices_respects_locked_price():
    pricing.save_rules([{"markup_percent": 10}])
    sid = make_supplier(make_yml(BASE))
    run(sid)
    items = products_by_sku()
    products.update(items["A1"]["id"], {"price": 999})
    pricing.save_rules([{"markup_percent": 100}])
    res = suppliers.recalc_prices()
    assert res["changed"] == 2
    items = products_by_sku()
    assert items["A1"]["price"] == 999
    assert items["A2"]["price"] == 301


def test_due_and_settings_validation():
    sid = suppliers.create("S")
    with pytest.raises(suppliers.SupplierError):
        suppliers.update(sid, {"url": "ftp://x"})
    with pytest.raises(excel.ImportError_):
        suppliers.update(sid, {"rows": "abc"})
    s = suppliers.update(sid, {"interval_hours": 6})
    assert s["next_run_at"]
    assert suppliers.due() == []
    with db.tx() as c:
        c.execute("UPDATE suppliers SET next_run_at = '2000-01-01T00:00:00+00:00'")
    assert suppliers.due() == [sid]


def test_delete_supplier_keeps_products():
    sid = make_supplier(make_yml(BASE))
    run(sid)
    suppliers.delete(sid)
    items = products_by_sku()
    assert len(items) == 3 and all(p["supplier_id"] is None for p in items.values())
    assert json.loads(db.query_one("SELECT COUNT(*) AS n FROM supplier_items")["n"].__str__()) == 0


def test_changes_queued_even_if_run_fails_midway(monkeypatch):
    """Обновление оборвалось на середине — уже записанные изменения всё равно уходят на Prom."""
    from promloader import sync
    sid = make_supplier(make_yml(BASE), auto_sync=1, new_status="ready")
    sent = []
    monkeypatch.setattr(sync, "enqueue", lambda ids: sent.extend(ids) or {"accepted": len(ids), "rejected": []})
    orig_apply_one = suppliers._apply_one
    count = {"n": 0}

    def apply_then_fail(*a, **k):
        count["n"] += 1
        if count["n"] == 3:
            raise RuntimeError("сбой посередине")
        return orig_apply_one(*a, **k)
    monkeypatch.setattr(suppliers, "CHUNK", 2)
    monkeypatch.setattr(suppliers, "_apply_one", apply_then_fail)
    r = run(sid)
    assert r["status"] == "failed" and len(sent) == 2   # первая часть записана — и ушла в очередь


def snapshot():
    return (db.query_one("SELECT COUNT(*) n FROM products")["n"],
            [(p["external_id"], p["price"], p["presence"]) for p in db.query("SELECT * FROM products ORDER BY id")],
            db.query_one("SELECT COUNT(*) n FROM product_changes")["n"],
            db.query_one("SELECT COUNT(*) n FROM sync_jobs")["n"],
            db.query_one("SELECT COUNT(*) n FROM supplier_items")["n"])


def test_preview_shows_changes_and_writes_nothing():
    sid = make_supplier(make_yml(BASE), auto_sync=1, new_status="ready")
    run(sid)
    changed = [("A1", "Кружка белая", "120", "10", "true"),       # цена выросла
               ("A2", "Кружка синяя", "150.50", "10", "true"),
               ("C1", "Новая миска", "70", "20", "true")]          # новая; B1 пропала
    suppliers.store_source(sid, make_yml(changed), "feed.xml")
    before = snapshot()
    p = suppliers.preview(sid)
    assert snapshot() == before                                     # ничего не записано
    assert p["created"] == 1 and p["price_changed"] == 1 and p["gone"] == 0   # B1 и так «нет в наличии»
    assert p["samples"]["created"][0]["name"] == "Новая миска"
    price = p["samples"]["prices"][0]
    assert price["code"] == "A1" and price["old"] == "100" and price["new"] == "120"
    assert p["to_prom"] >= 1 and p["total"] == 3
    r = run(sid)                                                    # «Применить» — то же самое по-настоящему
    assert r["stats"]["created"] == 1 and r["stats"]["price_changed"] == 1


def test_broken_download_keeps_last_good_price_list():
    sid = make_supplier(make_yml(BASE))
    suppliers.update(sid, {"url": "https://supplier.example.com/feed.xml"})
    good = httpx.MockTransport(lambda r: httpx.Response(200, content=make_yml(BASE)))
    assert suppliers.run(sid, transport=good)["status"] == "ok"
    kept = suppliers.source_path(sid).read_bytes()
    broken = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>500 Internal Server Error</html>"))
    r = suppliers.run(sid, transport=broken)
    assert r["status"] == "failed"
    assert suppliers.source_path(sid).read_bytes() == kept          # рабочий прайс на месте
    assert not list(db.suppliers_dir().glob(f"{sid}.new.*"))         # временный файл убран
    with pytest.raises(excel.ImportError_):
        suppliers.preview(sid, transport=broken)
