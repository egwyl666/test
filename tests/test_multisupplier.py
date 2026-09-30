import json

from promloader import excel, pricing, products, suppliers

HEADER = ["Код", "Штрихкод", "Название", "Закупка", "Наличие", "Фото"]
MAPPING = {"A": "external_id", "B": "barcode", "C": "name", "D": "cost_price", "E": "presence", "F": "images"}


def csv(rows) -> bytes:
    lines = [";".join(HEADER)] + [";".join(map(str, r)) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def supplier(name, rows, **settings):
    sid = suppliers.create(name)
    suppliers.store_source(sid, csv(rows), "price.csv")
    suppliers.update(sid, {"mapping": MAPPING, "header_row": 1, "prefix": name[:1] + "-", **settings})
    return sid


def refresh(sid, rows):
    suppliers.store_source(sid, csv(rows), "price.csv")
    r = suppliers.run(sid)
    assert r["status"] == "ok", r["message"]
    return r["stats"]


def only_product():
    items = products.list_products()["items"]
    assert len(items) == 1, [i["external_id"] for i in items]
    return products.get(items[0]["id"])


def test_cheapest_in_stock_wins_and_content_stays():
    pricing.save_rules([{"markup_percent": 50, "rounding": "none"}])
    a = supplier("Альфа", [["A1", "4820000000017", "Кружка от Альфы", 100, "есть", "https://a.example.com/1.jpg"]])
    refresh(a, [["A1", "4820000000017", "Кружка от Альфы", 100, "есть", "https://a.example.com/1.jpg"]])
    b = supplier("Бета", [])
    stats = refresh(b, [["B7", "4820000000017", "КРУЖКА БЕТА", 80, "есть", "https://b.example.com/9.jpg"]])
    assert stats["joined"] == 1 and stats["created"] == 0

    p = only_product()
    assert p["name"] == "Кружка от Альфы" and p["supplier_id"] == a      # описание — от основного
    assert [i["src"] for i in p["images"]] == ["https://a.example.com/1.jpg"]
    assert p["cost_price"] == 80 and p["price"] == 120                     # цена — от более дешёвого
    active = [o for o in p["offers"] if o["active"]]
    assert len(p["offers"]) == 2 and active[0]["supplier_name"] == "Бета"

    # у Беты закончилось — продаём по цене Альфы
    refresh(b, [["B7", "4820000000017", "КРУЖКА БЕТА", 80, "нет", "https://b.example.com/9.jpg"]])
    p = only_product()
    assert p["cost_price"] == 100 and p["price"] == 150 and p["presence"] == "available"

    # у Альфы пропал из прайса, у Беты «нет» — нет в наличии
    refresh(a, [])  # пустой прайс: защита сработает только от 20 товаров
    p = only_product()
    assert p["presence"] == "not_available"

    # вернулся у Беты
    refresh(b, [["B7", "4820000000017", "КРУЖКА БЕТА", 85, "есть", "https://b.example.com/9.jpg"]])
    p = only_product()
    assert p["presence"] == "available" and p["cost_price"] == 85


def test_no_merge_when_disabled_or_no_barcode():
    a = supplier("Альфа", [])
    refresh(a, [["A1", "4820000000111", "Кружка", 100, "есть", ""]])
    b = supplier("Бета", [], merge_by_barcode=False)
    refresh(b, [["B1", "4820000000111", "Кружка", 90, "есть", ""]])
    assert products.list_products()["total"] == 2


def test_locked_price_respected_and_delete_primary():
    pricing.save_rules([{"markup_percent": 10}])
    a = supplier("Альфа", [])
    refresh(a, [["A1", "4820000000222", "Кружка", 100, "есть", ""]])
    b = supplier("Бета", [])
    refresh(b, [["B1", "4820000000222", "Кружка Б", 90, "есть", ""]])
    p = only_product()
    products.update(p["id"], {"price": 500})
    refresh(b, [["B1", "4820000000222", "Кружка Б", 70, "есть", ""]])
    assert only_product()["price"] == 500

    suppliers.delete(a)
    p = only_product()
    assert p["supplier_id"] == b  # основным стал оставшийся поставщик


def test_mapping_hint_for_barcode():
    assert excel.guess_mapping(["Штрихкод", "Код товара"]) == {"A": "barcode", "B": "external_id"}


def test_same_supplier_and_fake_barcodes_are_not_merged():
    a = supplier("Альфа", [])
    stats = refresh(a, [["A1", "4820000000017", "Кружка белая", 100, "есть", ""],
                        ["A2", "4820000000017", "Кружка чёрная", 100, "есть", ""],
                        ["A3", "0000000000000", "Тарелка", 50, "есть", ""]])
    assert stats["created"] == 3 and not stats.get("joined")
    b = supplier("Бета", [])
    stats = refresh(b, [["B1", "0000000000000", "Что-то", 10, "есть", ""]])
    assert stats["created"] == 1 and not stats.get("joined")  # «заглушка» из нулей не объединяет
    assert suppliers.valid_barcode("4820000000017") and not suppliers.valid_barcode("123")


def test_locked_presence_respected_unless_nothing_in_stock():
    a = supplier("Альфа", [])
    refresh(a, [["A1", "4820000000333", "Кружка", 100, "есть", ""]])
    b = supplier("Бета", [])
    refresh(b, [["B1", "4820000000333", "Кружка Б", 90, "есть", ""]])
    p = only_product()
    products.update(p["id"], {"presence": "not_available"})  # решили не продавать
    refresh(b, [["B1", "4820000000333", "Кружка Б", 80, "есть", ""]])
    assert only_product()["presence"] == "not_available"
