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
