import pytest

from promloader import ai, aibulk, db, products


@pytest.fixture(autouse=True)
def ai_on():
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")


def translate(p, action, instruction=""):
    return {"name_ua": p["name"] + " (укр)", "description_ua": "<p>Опис</p>"}


def run_all(runner=translate):
    results = []
    while True:
        r = aibulk.process_one(runner)
        if r == "idle":
            return results
        results.append(r)


def test_translate_only_empty_and_revert():
    a = products.create({"name": "Кружка", "price": 1})
    b = products.create({"name": "Чашка", "price": 1, "name_ua": "Чашка укр", "description_ua": "<p>є</p>"})
    job = aibulk.create([a, b], "translate_ua")
    assert job["total"] == 2 and job["label"] == "Перевести на украинский"
    assert sorted(run_all()) == ["done", "skipped"]
    assert products.get(a)["name_ua"] == "Кружка (укр)"
    assert products.get(b)["name_ua"] == "Чашка укр"
    job = aibulk.get(job["id"])
    assert job["status"] == "done" and job["counts"] == {"done": 1, "skipped": 1}

    assert aibulk.revert(job["id"]) == 1
    assert products.get(a)["name_ua"] == ""
    assert aibulk.get(job["id"])["status"] == "reverted"


def test_overwrite_when_not_only_empty():
    b = products.create({"name": "Чашка", "price": 1, "name_ua": "старое", "description_ua": "<p>старое</p>"})
    aibulk.create([b], "translate_ua", only_empty=False)
    run_all()
    assert products.get(b)["name_ua"] == "Чашка (укр)"


def test_rate_limit_keeps_item_pending():
    a = products.create({"name": "Кружка", "price": 1})
    aibulk.create([a], "keywords")

    def limited(*args):
        raise ai.AIError("Gemini: превышен лимит запросов", temporary=True)

    assert aibulk.process_one(limited) == "rate_limited"
    assert aibulk.has_work()
    assert aibulk.process_one(lambda p, a, i="": {"keywords": "кружка, чашка"}) == "done"
    assert products.get(a)["keywords"] == "кружка, чашка"


def test_errors_pause_cancel():
    ids = [products.create({"name": f"T{i}", "price": 1}) for i in range(3)]
    job = aibulk.create(ids, "custom", "короче")

    def boom(*args):
        raise ai.AIError("ИИ не предложил изменений")

    assert aibulk.process_one(boom) == "error"
    aibulk.set_status(job["id"], "paused")
    assert aibulk.process_one(translate) == "idle"  # на паузе ничего не делает
    aibulk.set_status(job["id"], "cancelled")
    job = aibulk.get(job["id"])
    assert job["counts"] == {"error": 1, "skipped": 2} and job["errors"][0]["message"].startswith("ИИ")
    with pytest.raises(aibulk.BulkError):
        aibulk.set_status(job["id"], "running")


def test_validation():
    with pytest.raises(aibulk.BulkError):
        aibulk.create([1], "custom", "  ")
    with pytest.raises(aibulk.BulkError):
        aibulk.create([], "keywords")
    db.set_setting("ai_provider", "")
    with pytest.raises(aibulk.BulkError, match="не подключён"):
        aibulk.create([1], "keywords")


def test_rate_setting():
    assert aibulk.rate_per_minute() == 8
    db.set_setting("ai_rate", "30")
    assert aibulk.rate_per_minute() == 30


def test_api(client):
    pid = products.create({"name": "Кружка", "price": 1})
    r = client.post("/api/ai/bulk", json={"ids": [pid], "action": "keywords"}).json()
    assert r["total"] == 1
    assert client.get("/api/ai/bulk").json()["items"][0]["id"] == r["id"]
    assert client.post(f"/api/ai/bulk/{r['id']}/status", json={"status": "paused"}).json()["status"] == "paused"
    assert client.post("/api/ai/bulk", json={"ids": [pid], "action": "hack"}).status_code == 400


# ---------- 2.17.4: пачки товаров в одном запросе ----------

def _batch_server(answer, calls):
    import json as _json

    import httpx

    def handler(r):
        if r.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash",
                                                          "supportedGenerationMethods": ["generateContent"]}]})
        body = _json.loads(r.content)
        props = body["generationConfig"]["responseSchema"]["properties"]
        calls.append(sorted(props))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": _json.dumps(answer(props))}]}}]})
    return httpx.MockTransport(handler)


def test_keywords_go_in_batches_one_request_per_20(monkeypatch):
    from promloader import ai, aibulk, db, products
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    ids = [products.create({"name": f"Кружка {n}", "price": 10}) for n in range(25)]
    calls = []
    transport = _batch_server(lambda props: {k: f"слово {k}" for k in props}, calls)
    real = ai._gemini_post
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None: real(key, model, body, transport))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    aibulk.create(ids, "keywords")
    while aibulk.process_one() != "idle":
        pass
    assert len(calls) == 2 and len(calls[0]) == 20 and len(calls[1]) == 5   # 25 товаров — 2 запроса
    assert products.get(ids[0])["keywords"] == "слово i0_keywords"
    assert products.get(ids[24])["keywords"] == "слово i4_keywords"     # 25-й товар — 5-й во второй пачке
    est = aibulk.estimate(25, "keywords")
    assert est["requests"] == 2 and est["batch"] == 20


def test_batch_item_without_answer_is_error_others_done(monkeypatch):
    from promloader import ai, aibulk, db, products
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    ids = [products.create({"name": f"Лампа {n}", "price": 10}) for n in range(3)]
    calls = []
    transport = _batch_server(lambda props: {k: ("" if k.startswith("i1_") else "лампа, светильник") for k in props}, calls)
    real = ai._gemini_post
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None: real(key, model, body, transport))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    job = aibulk.create(ids, "keywords")
    while aibulk.process_one() != "idle":
        pass
    assert len(calls) == 1
    counts = aibulk.get(job["id"])["counts"]
    assert counts == {"done": 2, "error": 1}
    assert products.get(ids[1])["keywords"] in (None, "")


def test_broken_batch_answer_falls_back_to_one_by_one(monkeypatch):
    """Ответ на пачку оборвался (не JSON) — товары не теряются, а идут по одному."""
    import httpx

    from promloader import ai, aibulk, db, products
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    ids = [products.create({"name": f"Зонт {n}", "price": 10}) for n in range(3)]
    calls = []

    def handler(r):
        import json as _json
        props = _json.loads(r.content)["generationConfig"]["responseSchema"]["properties"]
        calls.append(len(props))
        text = '{"i0_keywords": "обор' if "i0_keywords" in props else _json.dumps({"keywords": "зонт"} | {k: "" for k in props if k != "keywords"})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    transport = httpx.MockTransport(handler)
    real = ai._gemini_post
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None: real(key, model, body, transport))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    job = aibulk.create(ids, "keywords")
    while aibulk.process_one() != "idle":
        pass
    assert calls[0] == 3 and calls[1:] == [5, 5, 5]   # пачка → потом по одному (схема одного товара — 5 полей)
    assert aibulk.get(job["id"])["counts"] == {"done": 3}, aibulk.get(job["id"])["errors"]
    assert all(products.get(i)["keywords"] == "зонт" for i in ids)
