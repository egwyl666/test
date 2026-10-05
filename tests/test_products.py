import pytest

from promloader import db, products

from .conftest import make_image


@pytest.mark.parametrize("raw, expected", [
    ("1 299,50 грн", 1299.5),
    ("1,299.50", 1299.5),
    ("1.299,50", 1299.5),
    ("450", 450.0),
    (" ", None),
    (None, None),
    (12, 12.0),
])
def test_parse_number(raw, expected):
    assert products.parse_number(raw) == expected


def test_parse_presence():
    assert products.parse_presence("В наличии") == "available"
    assert products.parse_presence("під замовлення") == "order"
    assert products.parse_presence("-") == "not_available"
    with pytest.raises(products.ProductError):
        products.parse_presence("может быть")


def test_create_generates_external_id_and_validates():
    pid = products.create({"name": "Кружка", "price": "150"})
    p = products.get(pid)
    assert p["external_id"] == f"PL-{pid:06d}"
    assert p["status"] == "draft"
    assert p["check"]["ok"]
    assert "Нет фото — такие товары почти не покупают" in p["check"]["warnings"]

    empty = products.get(products.create({}))
    assert set(empty["check"]["errors"]) == {"Нет названия", "Не указана цена"}


def test_duplicate_external_id_rejected():
    products.create({"name": "A", "external_id": "SKU-1"})
    with pytest.raises(products.ProductError):
        products.create({"name": "B", "external_id": "SKU-1"})
    other = products.create({"name": "C"})
    with pytest.raises(products.ProductError):
        products.update(other, {"external_id": "SKU-1"})


def test_edit_of_synced_product_requeues_it():
    pid = products.create({"name": "A", "price": 10})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced' WHERE id = ?", (pid,))
    before = products.get(pid)["revision"]
    p = products.update(pid, {"price": 12})
    assert p["status"] == "ready"
    assert p["revision"] == before + 1


def test_params_are_cleaned():
    pid = products.create({"name": "A", "params": [{"name": " Цвет ", "value": "Белый"}, {"name": "", "value": ""}]})
    assert products.get(pid)["params"] == [{"name": "Цвет", "value": "Белый"}]


def test_images_upload_order_delete(data_dir):
    pid = products.create({"name": "A"})
    a = products.add_image_file(pid, make_image("PNG"))
    b = products.add_image_file(pid, make_image("WEBP"))  # WEBP переводится в JPEG
    c = products.add_image_url(pid, "https://example.com/x.jpg")
    assert b["src"].endswith(".jpg")
    products.reorder_images(pid, [c["id"], a["id"], b["id"]])
    assert [i["id"] for i in products.get(pid)["images"]] == [c["id"], a["id"], b["id"]]

    products.delete_image(pid, a["id"])
    assert len(products.get(pid)["images"]) == 2
    assert len(list((data_dir / "uploads").iterdir())) == 1

    with pytest.raises(products.ProductError):
        products.add_image_file(pid, b"not an image")
    with pytest.raises(products.ProductError):
        products.add_image_url(pid, "ftp://nope")


def test_image_limit():
    pid = products.create({"name": "A"})
    for i in range(products.MAX_IMAGES):
        products.add_image_url(pid, f"https://example.com/{i}.jpg")
    with pytest.raises(products.ProductError):
        products.add_image_url(pid, "https://example.com/extra.jpg")


def test_delete_removes_files(data_dir):
    pid = products.create({"name": "A"})
    products.add_image_file(pid, make_image())
    products.delete([pid])
    assert list((data_dir / "uploads").iterdir()) == []


def test_search_ignores_case_for_cyrillic_and_finds_codes(client):
    from promloader import changes, products
    a = products.create({"name": "Кроссовки Найк", "price": 1, "external_id": "KR-1"})
    products.create({"name": "Ліхтар", "price": 1, "external_id": "L-2", "vendor_code": "GT-354"})
    products.create({"name": "Чашка 100%", "price": 1, "external_id": "C_3"})
    names = lambda q: [p["name"] for p in client.get("/api/products", params={"q": q}).json()["items"]]  # noqa: E731
    assert names("кросс") == ["Кроссовки Найк"] and names("КРОСС") == ["Кроссовки Найк"]
    assert names("ліх") == ["Ліхтар"] and names("gt-354") == ["Ліхтар"]
    assert names("100%") == ["Чашка 100%"] and names("c_3") == ["Чашка 100%"] and names("_") == ["Чашка 100%"]
    assert changes.search(period="all", q="кросс")["items"][0]["product_id"] == a


def test_status_counts_follow_search(client):
    from promloader import products
    products.create({"name": "Кроссовки", "price": 1})
    products.create({"name": "Тарілка", "price": 1})
    data = client.get("/api/products", params={"q": "кросс"}).json()
    assert data["total"] == 1 and sum(data["counts"].values()) == 1


def test_bulk_actions_by_filter_cover_all_matching(client):
    from promloader import products
    for i in range(5):
        products.create({"name": f"Гачок {i}", "price": 10, "external_id": f"G-{i}"})
    products.create({"name": "Тарілка", "price": 10, "external_id": "T-1"})
    flt = {"status": "", "q": "ГАЧОК", "supplier": ""}
    client.post("/api/products/status", json={"filter": flt, "status": "ready"})
    statuses = {p["name"]: p["status"] for p in products.list_products(limit=100)["items"]}
    assert sum(v == "ready" for v in statuses.values()) == 5 and statuses["Тарілка"] == "draft"
    assert client.post("/api/products/delete/check", json={"filter": flt}).json()["total"] == 5
    assert client.post("/api/products/status", json={"status": "ready"}).status_code == 400


def _seed_list():
    from promloader import db, products
    a = products.create({"name": "Котушка Shimano", "price": 1400, "external_id": "A", "group_name": "Котушки"})
    b = products.create({"name": "Вудилище", "price": 900, "external_id": "B", "group_name": "Вудилища",
                         "presence": "not_available"})
    c = products.create({"name": "", "price": 100, "external_id": "C"})   # без названия — ошибка заполнения
    products.add_image_url(a, "https://x.example.com/a.jpg")
    with db.tx() as cur:
        cur.execute("UPDATE products SET synced_at = ?, status = 'synced' WHERE id = ?", (db.now(), a))
    return a, b, c


def test_list_filters_and_sorting(client):
    a, b, c = _seed_list()
    get = lambda **p: [x["id"] for x in client.get("/api/products", params=p).json()["items"]]  # noqa: E731
    assert get(group="Котушки") == [a] and get(group="-") == [c]
    assert get(presence="not_available") == [b]
    assert get(on_prom="yes") == [a] and set(get(on_prom="no")) == {b, c}
    assert set(get(no_photo="1")) == {b, c}
    assert get(errors="1") == [c]
    data = client.get("/api/products", params={"errors": "1"}).json()
    assert data["total"] == 1 and data["counts"] == {"draft": 1}
    assert get(sort="price_asc") == [c, b, a] and get(sort="price_desc") == [a, b, c]
    assert get(sort="name")[0] == c and get(sort="nonsense")  # неизвестная сортировка — как обычно
    assert get(q="котушка", on_prom="yes") == [a]


def test_gone_from_supplier_filter(client):
    from promloader import db, products, suppliers
    a, b, _ = _seed_list()
    sid = suppliers.create("Опт")
    with db.tx() as c:
        c.execute("INSERT INTO supplier_items (supplier_id, sku, product_id, data, missing, seen_at) "
                  "VALUES (?, 'A', ?, '{}', 1, ?)", (sid, a, db.now()))
        c.execute("INSERT INTO supplier_items (supplier_id, sku, product_id, data, missing, seen_at) "
                  "VALUES (?, 'B', ?, '{}', 0, ?)", (sid, b, db.now()))
    assert [x["id"] for x in client.get("/api/products", params={"gone": "1"}).json()["items"]] == [a]


def test_bulk_edit_by_ids_and_filter(client):
    from promloader import changes, products
    a, b, c = _seed_list()
    r = client.post("/api/products/bulk-edit", json={"ids": [a, b], "fields": {"group_name": "Акція", "quantity": "",
                                                                                "presence": "order"}}).json()
    assert r == {"changed": 2, "errors": []}
    assert products.get(a)["group_name"] == "Акція" and products.get(b)["presence"] == "order"
    assert products.get(a)["status"] == "ready"           # был на Prom — снова ждёт отправки
    assert changes.search(period="all", field="group_name")["items"][0]["source"] == "Массово «✏ Изменить»"
    r = client.post("/api/products/bulk-edit", json={"filter": {"group": "-"}, "fields": {"vendor": "Owner"}}).json()
    assert r["changed"] == 1 and products.get(c)["vendor"] == "Owner"
    assert client.post("/api/products/bulk-edit", json={"ids": [a], "fields": {"quantity": ""}}).status_code == 400


def test_export_xlsx_follows_filter(client):
    import io

    from openpyxl import load_workbook
    _seed_list()
    r = client.get("/api/products.xlsx", params={"group": "Котушки"})
    assert r.status_code == 200 and "tovary-" in r.headers["content-disposition"]
    rows = list(load_workbook(io.BytesIO(r.content)).active.values)
    assert rows[0][:3] == ("Артикул", "Название", "Группа") and len(rows) == 2
    assert rows[1][1] == "Котушка Shimano" and rows[1][13] == "да"
