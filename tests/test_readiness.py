"""2.18.0: готовность черновиков для Prom — счётчики, фильтры списка и перевод готовых в «Готов к отправке»."""

from promloader import db, products


def make(name="Кружка", photo=True, **fields):
    pid = products.create({"name": name, "price": 100, **fields})
    if photo:
        with db.tx() as c:
            c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://x/1.jpg')", (pid,))
    return pid


def test_readiness_counts_and_filters(client):
    full = dict(name_ua="Кухоль", keywords="кружка, чашка")
    ready = make(**full)
    ready_no_desc = make(name="Лампа", **full)                                     # описания нет — UA-описание не нужно
    no_desc_ua = make(name="Зонт", description="<p>Большой</p>", **full)          # нет украинского описания
    no_kw = make(name="Самокат", name_ua="Самокат")
    no_photo = make(name="Ролики", photo=False, **full)
    zero_price = make(name="Скейт", **full)
    with db.tx() as c:
        c.execute("UPDATE products SET price = 0 WHERE id = ?", (zero_price,))
    not_draft = make(name="Готовый", **full)
    products.set_status([not_draft], "ready")

    r = client.get("/api/products/readiness").json()
    assert r["drafts"] == 6 and r["ready_to_promote"] == 2
    assert r["missing_ua"] == 1 and r["missing_keywords"] == 1 and r["missing_price_or_photo"] == 2

    def ids(key):
        return {p["id"] for p in client.get(f"/api/products?status=draft&readiness={key}").json()["items"]}
    assert ids("ready_to_promote") == {ready, ready_no_desc}
    assert ids("missing_ua") == {no_desc_ua}  # у «Самоката» есть укр. название, а описания нет — украинского хватает
    assert ids("missing_keywords") == {no_kw}
    assert ids("missing_price_or_photo") == {no_photo, zero_price}
    # с другим фильтром списка (поиск) — счётчики по нему
    assert client.get("/api/products/readiness?q=Лампа").json()["ready_to_promote"] == 1


def test_promote_ready_moves_only_ready_drafts_and_logs(client):
    full = dict(name_ua="Кухоль", keywords="кружка")
    a, b = make(**full), make(name="Лампа", **full)
    not_ready = make(name="Без ключевых", name_ua="Без")
    r = client.post("/api/products/promote-ready", json={"filter": {}}).json()
    assert r["moved"] == 2 and r["ready_to_promote"] == 0
    assert products.get(a)["status"] == products.get(b)["status"] == "ready"
    assert products.get(not_ready)["status"] == "draft"
    log = db.query("SELECT field, old, new, source FROM product_changes WHERE product_id = ? AND field = 'status'", (a,))
    assert [(x["old"], x["new"]) for x in log] == [("Черновик", "Готов к отправке")]
    assert "Готовность" in log[0]["source"]


def test_promote_ready_respects_list_filter(client):
    full = dict(name_ua="Кухоль", keywords="кружка")
    a, b = make(name="Кружка белая", **full), make(name="Лампа", **full)
    r = client.post("/api/products/promote-ready", json={"filter": {"q": "Лампа"}}).json()
    assert r["moved"] == 1
    assert products.get(a)["status"] == "draft" and products.get(b)["status"] == "ready"


def test_too_long_name_is_not_ready(client):
    """Найдено при проверке: название длиннее 255 символов Prom не примет — такой черновик не «готов»."""
    long = make(name="Кружка " * 50, name_ua="Кухоль", keywords="кружка")
    ok = make(name="Кружка", name_ua="Кухоль", keywords="кружка")
    r = client.post("/api/products/promote-ready", json={"filter": {}}).json()
    assert r["moved"] == 1
    assert products.get(ok)["status"] == "ready" and products.get(long)["status"] == "draft"
