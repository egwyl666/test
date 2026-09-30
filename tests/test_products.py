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
