"""2.20.0: выгода — очередь по выгоде, сортировки и фильтры; сторож наценки; автоконвейер; «мало данных»."""

import json

import httpx

from promloader import ai, aibulk, db, notify, products, suppliers


def ai_on():
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")


def mk(name, price=100, cost=None, currency="UAH", photo=True, presence="available", status="draft", **f):
    pid = products.create({"name": name, "price": price, "cost_price": cost, "cost_currency": currency,
                           "presence": presence, "vendor": "Luminarc", **f})
    if photo:
        with db.tx() as c:
            c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://x/1.jpg')", (pid,))
    if status != "draft":
        with db.tx() as c:
            c.execute("UPDATE products SET status = ? WHERE id = ?", (status, pid))
    return pid


def names(client, query):
    return [p["name"] for p in client.get(f"/api/products?{query}").json()["items"]]


# ---------- 1. выгода ----------

def test_margin_in_uah_with_currency():
    pid = mk("Инвертор", price=600, cost=10, currency="USD")   # курс в тестах — 40: закупка 400 грн
    r = db.query_one(f"{products.LIST_SELECT} WHERE p.id = ?", (pid,))
    from promloader import rates
    assert products.margin(r, rates.Table()) == (200.0, 50.0)


def test_profit_sorts_and_filters(client):
    big = mk("Большая наценка", price=500, cost=100)
    small = mk("Маленькая наценка", price=105, cost=100)
    loss = mk("В минус", price=90, cost=100)
    no_cost = mk("Без закупки", price=100)
    out = mk("Нет в наличии", price=900, cost=100, presence="not_available")
    no_photo = mk("Без фото", price=900, cost=100, photo=False)
    desc = names(client, "sort=profit_desc")
    assert desc[0] == "Большая наценка"                       # в наличии, с фото, наценка больше всех
    assert desc.index("Без фото") > desc.index("В минус") and desc.index("Нет в наличии") > desc.index("Без фото")
    asc = names(client, "sort=profit_asc")
    assert asc[0] == "Без закупки" and asc[1] == "В минус"    # сначала самые непонятные и невыгодные
    assert names(client, "profit=high_profit") == ["Большая наценка"]          # минимум по умолчанию 10%
    assert set(names(client, "profit=low_or_negative")) == {"Маленькая наценка", "В минус"}
    assert names(client, "profit=no_cost_or_rrp") == ["Без закупки"]
    assert set(products.ids_for_filter({"profit": "low_or_negative"})) == {small, loss}
    assert {big, out, no_cost, no_photo}  # все созданы


def test_bulk_ai_queue_goes_by_profit():
    ai_on()
    poor = mk("Копейки", price=101, cost=100)
    rich = mk("Выгодный", price=900, cost=100)
    no_photo = mk("Без фото", price=900, cost=100, photo=False)
    job = aibulk.create([poor, no_photo, rich], "keywords")
    order = [r["product_id"] for r in db.query("SELECT product_id FROM ai_items WHERE job_id = ? ORDER BY id", (job["id"],))]
    assert order == [rich, poor, no_photo]


# ---------- 3. сторож наценки ----------

def test_low_margin_watchdog_and_setting(client):
    mk("Черновик в минус", price=50, cost=100)                         # черновики не продаются — не следим
    loss = mk("Продаётся в минус", price=90, cost=100, status="synced")
    mk("Нормальный", price=200, cost=100, status="synced")
    r = client.get("/api/pricing/low-margin").json()
    assert [x["id"] for x in r["items"]] == [loss] and r["items"][0]["loss"] and r["min_margin_pct"] == 10
    thin = mk("Тонкая наценка", price=115, cost=100, status="ready")
    r = client.post("/api/pricing/min-margin", json={"value": 20}).json()
    assert {x["id"] for x in r["items"]} == {loss, thin}
    assert client.post("/api/pricing/min-margin", json={"value": 5000}).status_code == 400


def _supplier_with_price(cost):
    from tests.test_suppliers import make_supplier, make_yml
    return make_supplier(make_yml([("A1", "Кружка белая керамическая", str(cost), "10", "true")]))


def test_supplier_cost_rise_alerts_once_and_is_in_history(monkeypatch):
    from tests.test_suppliers import make_yml
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    sid = _supplier_with_price(100)
    suppliers.run(sid)
    pid = db.query_one("SELECT id FROM products WHERE supplier_id = ?", (sid,))["id"]
    with db.tx() as c:  # цена закреплена вручную, товар уже на Prom
        c.execute("UPDATE products SET price = 150, status = 'synced', locked_fields = '[\"price\"]' WHERE id = ?", (pid,))
    suppliers.store_source(sid, make_yml([("A1", "Кружка белая керамическая", "160", "10", "true")]), "feed.xml")
    suppliers.run(sid)
    alerts = [t for e, t in sent if e == "margin_alert"]
    assert len(alerts) == 1 and "наценка упала у 1" in alerts[0]
    hist = db.query("SELECT old, new, source FROM product_changes WHERE product_id = ? AND field = 'cost_price'", (pid,))
    assert hist[-1]["new"] == "160" and "Поставщик" in hist[-1]["source"]
    suppliers.run(sid)                                       # ничего нового — без повторного уведомления
    assert len([t for e, t in sent if e == "margin_alert"]) == 1


# ---------- 2. автоконвейер и «мало данных» ----------

def test_too_little_data_rule():
    assert ai.too_little_data({"name": "Чехол 123"})
    assert not ai.too_little_data({"name": "Чехол 123", "vendor": "Baseus"})
    assert not ai.too_little_data({"name": "Чехол для iPhone 15 силиконовый"})
    assert not ai.too_little_data({"name": "Чехол", "params": [{"name": "Цвет", "value": "чёрный"}]})
    assert not ai.too_little_data({"name": "Чехол", "description": "<p>Силиконовый чехол, чёрный, для iPhone</p>"})


def test_needs_manual_pill_and_ai_skips_without_request(client):
    ai_on()
    cryptic = products.create({"name": "Чехол 123", "price": 100})
    ok = mk("Чехол для iPhone 15 силиконовый")
    r = client.get("/api/products/readiness").json()
    assert r["needs_manual"] == 1 and r["missing_ai"] == 1
    called = []
    job = aibulk.create([cryptic, ok], "prepare_prom")

    def runner(p, a, i=""):
        called.append(p["id"])
        return {"description": "<p>Чехол</p>", "name_ua": "Чохол", "description_ua": "<p>Чохол</p>", "keywords": "чехол"}
    while aibulk.process_one(runner) != "idle":
        pass
    assert called == [ok]
    j = aibulk.get(job["id"])
    assert j["counts"] == {"done": 1, "skipped": 1}
    assert db.query_one("SELECT message FROM ai_items WHERE product_id = ?", (cryptic,))["message"] == aibulk.MANUAL


def test_prepare_prom_writes_missing_description_in_batch(monkeypatch):
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    a = mk("Чехол для iPhone 15 силиконовый")
    b = mk("Кружка керамическая белая 300 мл")
    seen = []

    def handler(r):
        body = json.loads(r.content)
        props = body["generationConfig"]["responseSchema"]["properties"]
        seen.append((sorted(props), body["contents"][0]["parts"][0]["text"]))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(
            {k: f"ШІ {k}" for k in props})}]}}]})
    transport = httpx.MockTransport(handler)
    real = ai._gemini_post
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None, retry=True: real(key, model, body, transport, retry))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    aibulk.create([a, b], "prepare_prom")
    while aibulk.process_one() != "idle":
        pass
    props, prompt = seen[0]
    assert "i0_description" in props and "i1_description" in props
    assert "ничего не придумывая" in prompt
    assert products.get(a)["description"] == "ШІ i0_description" or products.get(a)["description"] == "ШІ i1_description"


def test_pipeline_after_supplier_run(monkeypatch):
    from tests.test_suppliers import make_supplier, make_yml
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    sid = make_supplier(make_yml([("A1", "Кружка белая керамическая 300 мл", "100", "10", "true"),
                                  ("B1", "Тарелка", "80", "20", "false")]))   # нет в наличии — конвейер не трогает
    suppliers.update(sid, {"auto_pipeline": True})
    assert suppliers.get(sid)["auto_pipeline"] is True
    run = suppliers.run(sid)
    assert run["status"] == "ok" and run["stats"].get("pipeline_queued") == 1
    jobs = db.query("SELECT action, auto_promote, only_empty FROM ai_jobs")
    assert [dict(j) for j in jobs] == [{"action": "prepare_prom", "auto_promote": 1, "only_empty": 1}]
    # второй прогон прайса не ставит те же товары в очередь повторно
    suppliers.run(sid)
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_jobs")["n"] == 1


def test_pipeline_off_by_default():
    from tests.test_suppliers import make_supplier, make_yml
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    sid = make_supplier(make_yml([("A1", "Кружка белая керамическая 300 мл", "100", "10", "true")]))
    suppliers.run(sid)
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_jobs")["n"] == 0
