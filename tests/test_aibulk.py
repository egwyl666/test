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
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None, retry=True: real(key, model, body, transport, retry))
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
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None, retry=True: real(key, model, body, transport, retry))
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
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None, retry=True: real(key, model, body, transport, retry))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    job = aibulk.create(ids, "keywords")
    while aibulk.process_one() != "idle":
        pass
    assert calls[0] == 3 and calls[1:] == [5, 5, 5]   # пачка → потом по одному (схема одного товара — 5 полей)
    assert aibulk.get(job["id"])["counts"] == {"done": 3}, aibulk.get(job["id"])["errors"]
    assert all(products.get(i)["keywords"] == "зонт" for i in ids)


def test_already_translated_products_are_skipped_without_requests():
    """2.17.5: украинский текст уже пришёл со второй ссылки поставщика — «Перевести на украинский» его пропускает
    и не тратит запрос; пачка из остальных товаров собирается как обычно."""
    from promloader import ai, aibulk, products
    done = products.create({"name": "Кружка", "name_ua": "Кухоль", "description": "<p>Белая</p>",
                            "description_ua": "<p>Біла</p>", "price": 10})
    no_desc = products.create({"name": "Лампа", "name_ua": "Лампа настільна", "price": 10})  # описания нет — нечего
    half = products.create({"name": "Зонт", "name_ua": "Парасоля", "description": "<p>Большой</p>", "price": 10})
    empty = products.create({"name": "Самокат", "price": 10})
    assert ai.already_done(products.get(done), "translate_ua") and ai.already_done(products.get(no_desc), "translate_ua")
    assert not ai.already_done(products.get(half), "translate_ua") and not ai.already_done(products.get(empty), "translate_ua")
    seen = []

    def runner(p, action, instruction=""):
        seen.append(p["id"])
        return {"description_ua": "<p>Переклад</p>"} | ({} if p.get("name_ua") else {"name_ua": "Самокат"})
    job = aibulk.create([done, no_desc, half, empty], "translate_ua")
    while aibulk.process_one(runner) != "idle":
        pass
    assert sorted(seen) == sorted([half, empty])
    assert aibulk.get(job["id"])["counts"] == {"skipped": 2, "done": 2}
    assert products.get(half)["name_ua"] == "Парасоля"   # заполненное поле не перезаписано


def test_translate_ru_skips_products_that_already_have_russian_text():
    from promloader import ai, products
    both = products.get(products.create({"name": "Кружка белая", "name_ua": "Кухоль білий", "price": 10}))
    ua_only = products.get(products.create({"name": "Кухоль білий", "price": 10}))
    assert ai.already_done(both, "translate_ru") and not ai.already_done(ua_only, "translate_ru")


def test_batch_skips_filled_items_without_breaking_batch(monkeypatch):
    import json as _json

    import httpx

    from promloader import ai, aibulk, db, products
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    ids = [products.create({"name": f"Товар {n}", "price": 10, **({"name_ua": "Є", "description_ua": "<p>Є</p>",
                                                                   "description": "<p>Есть</p>"} if n % 2 else {})})
           for n in range(6)]
    calls = []

    def handler(r):
        props = _json.loads(r.content)["generationConfig"]["responseSchema"]["properties"]
        calls.append(sorted(props))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": _json.dumps(
            {k: "Переклад" for k in props})}]}}]})
    transport = httpx.MockTransport(handler)
    real = ai._gemini_post
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None, retry=True: real(key, model, body, transport, retry))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    job = aibulk.create(ids, "translate_ua")
    while aibulk.process_one() != "idle":
        pass
    # 3 заполненных пропущены без запроса, 3 пустых — одной пачкой
    assert len(calls) == 1 and len(calls[0]) == 6
    assert aibulk.get(job["id"])["counts"] == {"skipped": 3, "done": 3}


# ---------- 2.18.0: «Подготовить для Prom», уведомление о завершении ----------

def test_prepare_prom_already_done_and_missing_fields():
    from promloader import ai
    full = {"name_ua": "Кухоль", "description": "<p>Кружка</p>", "description_ua": "<p>Кухоль</p>", "keywords": "кружка"}
    assert ai.already_done(full, "prepare_prom")
    assert ai.already_done({**full, "description": "", "description_ua": ""}, "prepare_prom")  # описания нет — не нужно
    assert not ai.already_done({**full, "keywords": ""}, "prepare_prom")
    assert not ai.already_done({**full, "description_ua": ""}, "prepare_prom")
    assert ai.missing_fields({**full, "keywords": ""}, "prepare_prom") == ("keywords",)
    assert ai.missing_fields({"name": "Кружка"}, "prepare_prom") == ("name_ua", "keywords")
    assert ai.missing_fields({"name": "Кружка", "description": "<p>x</p>"}, "prepare_prom") == (
        "name_ua", "description_ua", "keywords")
    assert ai.batch_size("prepare_prom") == 5


def test_prepare_prom_batch_requests_only_missing_fields_and_keeps_existing(monkeypatch):
    """Товар поставщика с украинской ссылкой уже переведён — просим у ИИ только ключевые слова, перевод не трогаем."""
    import json as _json

    import httpx

    from promloader import ai, aibulk, db, products
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    translated = products.create({"name": "Кружка", "name_ua": "Кухоль", "description": "<p>Белая</p>",
                                  "description_ua": "<p>Біла</p>", "price": 10})
    raw = products.create({"name": "Лампа", "description": "<p>Настольная</p>", "price": 10})
    done = products.create({"name": "Зонт", "name_ua": "Парасоля", "keywords": "зонт", "price": 10})
    calls = []

    def handler(r):
        props = _json.loads(r.content)["generationConfig"]["responseSchema"]["properties"]
        calls.append(sorted(props))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": _json.dumps(
            {k: f"ШІ {k}" for k in props})}]}}]})
    transport = httpx.MockTransport(handler)
    real = ai._gemini_post
    monkeypatch.setattr(ai, "_gemini_post", lambda key, model, body, t=None, retry=True: real(key, model, body, transport, retry))
    monkeypatch.setattr(ai, "gemini_models", lambda key, t=None: ["gemini-3.8-flash"])
    job = aibulk.create([translated, raw, done], "prepare_prom")
    while aibulk.process_one() != "idle":
        pass
    # один запрос на пачку; у переведённого товара — только keywords, у нового — все три поля
    assert calls == [sorted(["i0_keywords", "i1_name_ua", "i1_description_ua", "i1_keywords"])]
    t = products.get(translated)
    assert (t["name_ua"], t["description_ua"], t["keywords"]) == ("Кухоль", "<p>Біла</p>", "ШІ i0_keywords")
    r = products.get(raw)
    assert (r["name_ua"], r["description_ua"], r["keywords"]) == ("ШІ i1_name_ua", "ШІ i1_description_ua", "ШІ i1_keywords")
    assert aibulk.get(job["id"])["counts"] == {"done": 2, "skipped": 1}


def _fill_job(n, action="keywords"):
    from promloader import aibulk, products
    ids = [products.create({"name": f"Товар {i}", "price": 10}) for i in range(n)]
    return aibulk.create(ids, action)


def test_notify_when_big_job_finishes(monkeypatch):
    from promloader import aibulk, notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    job = _fill_job(aibulk.NOTIFY_MIN_ITEMS)
    while aibulk.process_one(lambda p, a, i="": {"keywords": "слово"}) != "idle":
        pass
    assert aibulk.get(job["id"])["status"] == "done"
    assert len(sent) == 1 and sent[0][0] == "ai_bulk"
    assert "завершён" in sent[0][1] and f"готово: {aibulk.NOTIFY_MIN_ITEMS}" in sent[0][1]


def test_no_notify_for_small_job(monkeypatch):
    from promloader import aibulk, notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    _fill_job(aibulk.NOTIFY_MIN_ITEMS - 1)
    while aibulk.process_one(lambda p, a, i="": {"keywords": "слово"}) != "idle":
        pass
    assert sent == []


def test_key_error_stops_job_and_notifies(monkeypatch):
    from promloader import ai, aibulk, notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    job = _fill_job(12)

    def bad_key(p, a, i=""):
        raise ai.AIError("Google не принял ключ Gemini", key_error=True)
    assert aibulk.process_one(bad_key) == "error"
    j = aibulk.get(job["id"])
    assert j["status"] == "paused" and "ключ" in j["message"] and j["counts"] == {"pending": 12}
    assert sent and sent[0][0] == "ai_bulk" and "остановлен" in sent[0][1]
    assert aibulk.process_one(bad_key) == "idle"  # на паузе дальше не тратит запросы


def test_ai_bulk_event_added_to_existing_recipients():
    from promloader import db, notify
    with db.tx() as c:
        c.execute("INSERT INTO tg_recipients (chat_id, name, events, created_at) VALUES ('5', 'x', '[\"order\"]', ?)", (db.now(),))
    db.set_setting("tg_events_version", "2")
    assert "ai_bulk" in notify.recipients()[0]["events"]


# ---------- 2.19.0: «сразу в «Готов к отправке»» ----------

def _draft(name, **f):
    from promloader import db, products
    pid = products.create({"name": name, "price": 100, **f})
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://x/1.jpg')", (pid,))
    return pid


def test_auto_promote_moves_ready_drafts_and_counts(monkeypatch):
    from promloader import aibulk, db, notify, products
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append(text))
    ids = [_draft(f"Кружка {i}") for i in range(10)]
    no_photo = products.create({"name": "Без фото", "price": 100})
    job = aibulk.create(ids + [no_photo], "prepare_prom", auto_promote=True)
    while aibulk.process_one(lambda p, a, i="": {"name_ua": "Кухоль", "keywords": "кружка"}) != "idle":
        pass
    j = aibulk.get(job["id"])
    assert j["promoted"] == 10 and j["auto_promote"] == 1
    assert all(products.get(i)["status"] == "ready" for i in ids)
    assert products.get(no_photo)["status"] == "draft"          # не готов — остаётся черновиком
    log = db.query("SELECT source FROM product_changes WHERE product_id = ? AND field = 'status'", (ids[0],))
    assert log[0]["source"] == aibulk.AUTO_PROMOTE_SOURCE
    assert "В «Готов к отправке»: 10" in sent[-1]
    # откат задания: тексты ИИ убраны — и товары снова черновики
    aibulk.revert(job["id"])
    assert products.get(ids[0])["status"] == "draft" and not products.get(ids[0])["name_ua"]


def test_without_auto_promote_status_untouched():
    from promloader import aibulk, products
    pid = _draft("Кружка")
    aibulk.create([pid], "prepare_prom")
    while aibulk.process_one(lambda p, a, i="": {"name_ua": "Кухоль", "keywords": "кружка"}) != "idle":
        pass
    assert products.get(pid)["status"] == "draft"


def test_readiness_ai_target_filter(client):
    """«✨ Заполнить через ИИ»: выборка по фильтру missing_ai — черновики без перевода или ключевых слов."""
    from promloader import products
    a = _draft("Без перевода", keywords="k")
    b = _draft("Без ключевых", name_ua="Є")
    _draft("Готовый", name_ua="Є", keywords="k")
    r = client.get("/api/products/readiness").json()
    assert r["missing_ai"] == 2
    assert set(products.ids_for_filter({"status": "draft", "readiness": "missing_ai"})) == {a, b}
    est = client.post("/api/ai/bulk/estimate", json={"action": "prepare_prom",
                                                     "filter": {"status": "draft", "readiness": "missing_ai"}}).json()
    assert est["count"] == 2


def test_auto_promote_also_moves_already_complete_skipped_drafts():
    """Найдено при проверке: товар, у которого всё уже было (пропущен без ИИ), оставался черновиком,
    хотя задание «сразу в «Готов к отправке»»."""
    from promloader import aibulk, products
    complete = _draft("Готовый", name_ua="Кухоль", keywords="кружка")
    empty = _draft("Пустой")
    job = aibulk.create([complete, empty], "prepare_prom", auto_promote=True)
    while aibulk.process_one(lambda p, a, i="": {"name_ua": "Лампа", "keywords": "лампа"}) != "idle":
        pass
    assert products.get(complete)["status"] == "ready" and products.get(empty)["status"] == "ready"
    assert aibulk.get(job["id"])["promoted"] == 2


def test_bulk_pauses_not_errors_when_limit_then_unavailable(monkeypatch):
    from promloader import ai, aibulk
    pid = _draft("Кружка")
    job = aibulk.create([pid], "keywords")

    def limited(p, a, i=""):
        raise ai.AIError("лимит", temporary=True)
    assert aibulk.process_one(limited) == "rate_limited"
    assert aibulk.get(job["id"])["counts"] == {"pending": 1}
