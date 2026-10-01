from xml.etree import ElementTree as ET

from promloader import feed, products

from .conftest import make_image


def test_feed_contents():
    pid = products.create({
        "name": "Кружка <белая>", "name_ua": "Кухоль", "price": 150, "old_price": 200,
        "group_name": "Посуда", "presence": "order", "quantity": 4, "vendor": "Luminarc",
        "description": "Первый абзац\nстрока\n\nВторой", "params": [{"name": "Объём", "value": "300 мл"}],
    })
    products.add_image_file(pid, make_image())
    products.add_image_url(pid, "https://cdn.example.com/a.jpg")

    xml = feed.build([pid], "https://shop.example.com")
    root = ET.fromstring(xml)
    offer = root.find("shop/offers/offer")
    p = products.get(pid)

    assert offer.get("id") == p["external_id"]
    assert offer.get("available") == "false"  # под заказ
    assert offer.findtext("name") == "Кружка <белая>"
    assert offer.findtext("name_ua") == "Кухоль"
    assert offer.findtext("price") == "150"
    assert offer.findtext("oldprice") == "200"
    assert offer.findtext("quantity_in_stock") == "4"
    assert offer.findtext("description") == "<p>Первый абзац<br>строка</p><p>Второй</p>"
    pictures = [e.text for e in offer.findall("picture")]
    assert pictures[0].startswith("https://shop.example.com/media/")
    assert pictures[1] == "https://cdn.example.com/a.jpg"
    assert offer.find("param").get("name") == "Объём"
    category = root.find("shop/categories/category")
    assert category.text == "Посуда"
    assert offer.findtext("categoryId") == category.get("id")
    assert xml.count(b"<?xml") == 1


def test_feed_all_skips_drafts():
    draft = products.create({"name": "Черновик", "price": 1})
    ready = products.create({"name": "Готов", "price": 1})
    products.set_status([ready], "ready")
    root = ET.fromstring(feed.build(None, ""))
    ids = [o.get("id") for o in root.iter("offer")]
    assert ids == [products.get(ready)["external_id"]]
    assert products.get(draft)["external_id"] not in ids


def test_no_oldprice_without_discount():
    pid = products.create({"name": "A", "price": 100, "old_price": 90})
    offer = ET.fromstring(feed.build([pid], "")).find("shop/offers/offer")
    assert offer.find("oldprice") is None


def test_required_yml_fields_present():
    """name/company/url у магазина и categoryId у каждого товара — даже без группы."""
    from promloader import feed as feedmod
    pid = products.create({"name": "Без группы товар", "price": 10})
    root = ET.fromstring(feedmod.build([pid], "https://pub-1.r2.dev"))
    shop = root.find("shop")
    assert shop.findtext("name") and shop.findtext("company") and shop.findtext("url") == "https://pub-1.r2.dev"
    offer = shop.find("offers/offer")
    cat_id = offer.findtext("categoryId")
    assert cat_id and shop.find(f"categories/category[@id='{cat_id}']").text == feedmod.NO_GROUP
