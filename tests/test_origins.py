"""2.20.1: у поля карточки видно, откуда значение — ИИ, поставщик или вручную (по журналу изменений)."""

from promloader import aibulk, changes, products, suppliers


def test_origin_kinds():
    k = products.origin_kind
    assert k("ИИ: Ключевые слова") == k("ИИ (в карточке)") == k("Массовый ИИ: готов к отправке") == "ai"
    assert k("Поставщик «Опт»") == "supplier"
    assert k("Вручную") == k("Массово «✏ Изменить»") == k("ИИ: откат") == "manual"
    assert k("Импорт файла") == "import" and k("Каталог с Prom") == "prom" and k("Пересчёт по наценке") == "auto"


def test_card_shows_where_each_value_came_from(client):
    from tests.test_suppliers import BASE, make_supplier, make_yml
    sid = make_supplier(make_yml(BASE))
    suppliers.run(sid)
    pid = products.list_products({"q": "Кружка белая"})["items"][0]["id"]
    o = client.get(f"/api/products/{pid}").json()["origins"]
    assert o["name"]["kind"] == "supplier" and o["price"]["kind"] == "supplier" and "Поставщик" in o["name"]["source"]
    assert "keywords" not in o                                     # пустое поле — без метки
    # ИИ из карточки («Применить») — помечен как ИИ, а не «вручную»
    r = client.patch(f"/api/products/{pid}", json={"keywords": "кружка, чашка", "_ai_fields": ["keywords"]}).json()
    assert r["origins"]["keywords"]["kind"] == "ai" and r["keywords"] == "кружка, чашка"
    # потом поправили руками — уже «вручную»
    r = client.patch(f"/api/products/{pid}", json={"keywords": "кружка"}).json()
    assert r["origins"]["keywords"]["kind"] == "manual"
    # смешанное сохранение: часть из ИИ, часть — своя правка
    r = client.patch(f"/api/products/{pid}", json={"name_ua": "Кухоль", "vendor": "Philips", "_ai_fields": ["name_ua"]}).json()
    assert r["origins"]["name_ua"]["kind"] == "ai" and r["origins"]["vendor"]["kind"] == "manual"
    assert client.get("/api/meta").json()["origin_labels"]["ai"] == "✨ ИИ"


def test_bulk_ai_marks_fields_as_ai():
    from promloader import db
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    pid = products.create({"name": "Кружка белая керамическая", "price": 10, "vendor": "Luminarc"})
    with changes.source("Вручную"):
        products.update(pid, {"description": "<p>Кружка</p>"})
    aibulk.create([pid], "keywords")
    while aibulk.process_one(lambda p, a, i="": {"keywords": "кружка"}) != "idle":
        pass
    o = products.get(pid)["origins"]
    assert o["keywords"]["kind"] == "ai" and o["description"]["kind"] == "manual" and o["name"]["kind"] == "manual"
