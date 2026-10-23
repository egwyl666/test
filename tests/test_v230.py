"""2.30.0: несколько ключей ИИ с переключением по лимиту и многопоточный массовый ИИ.

- ключ упёрся в лимит (429) или не принят — запрос сразу повторяется со следующим ключом, этот отдыхает;
- все ключи в лимите — «подождать» (массовый ИИ ставит паузу), а не ошибка товара;
- в многопоточном режиме каждый поток начинает со своего ключа, один товар не уходит в ИИ дважды;
- запасные ключи — секреты: не в облачной копии и вырезаются из журнала."""

import asyncio
import json
import threading
import time

import httpx
import pytest

from promloader import ai, aibulk, cloudbackup, config, db, products

from .conftest import sign_in


@pytest.fixture(autouse=True)
def clean_holds(monkeypatch):
    ai._key_hold.clear()
    ai._model_hold.clear()
    ai._auto_cache.clear()
    aibulk._claimed.clear()
    monkeypatch.setattr(ai, "_sleep", lambda s: None)
    yield
    ai._key_hold.clear()
    ai._model_hold.clear()


def use_keys(provider="gemini", *keys):
    db.set_setting("ai_provider", provider)
    primary, extra = ai.KEY_SETTINGS[provider]
    db.set_setting(primary, keys[0])
    db.set_setting(extra, json.dumps(list(keys[1:])))


def test_keys_primary_first_without_duplicates(data_dir):
    use_keys("gemini", "AIza-one", "AIza-two", "AIza-one", "AIza-three")
    assert ai.keys("gemini") == ["AIza-one", "AIza-two", "AIza-three"]
    assert ai.settings()["key"] == "AIza-one" and len(ai.settings()["keys"]) == 3
    db.set_setting("gemini_key", "")  # основной убрали — работает запасной
    assert ai.settings()["key"] == "AIza-two" and ai.enabled()


def test_limit_on_one_key_switches_to_next_and_rests(data_dir):
    use_keys("gemini", "k1", "k2")
    calls = []

    def one(s, prompt):
        calls.append(s["key"])
        if s["key"] == "k1":
            raise ai.AIError("лимит", temporary=True, limit=True, retry_after=120)
        return {"ok": s["key"]}, {}

    assert ai._with_keys(one, ai.settings(), "p")[0] == {"ok": "k2"}
    assert calls == ["k1", "k2"]
    assert not ai.key_ready("k1") and ai.key_ready("k2")
    calls.clear()
    ai._with_keys(one, ai.settings(), "p")  # k1 отдыхает — сразу второй
    assert calls == ["k2"]
    states = {x["mask"]: x for x in ai.key_states("gemini")}
    assert [x["state"] for x in ai.key_states("gemini")] == ["limit", "ok"] and states


def test_rejected_key_is_skipped_but_other_errors_are_not_retried(data_dir):
    use_keys("gemini", "bad", "good")

    def one(s, prompt):
        if s["key"] == "bad":
            raise ai.AIError("ключ не принят", key_error=True)
        raise ai.AIError("ответ не в том формате")  # ошибка запроса — другой ключ её не исправит

    with pytest.raises(ai.AIError, match="формате"):
        ai._with_keys(one, ai.settings(), "p")
    assert ai.key_states("gemini")[0]["state"] == "key"


def test_all_keys_limited_is_temporary_with_nearest_reset(data_dir):
    use_keys("gemini", "k1", "k2", "k3")

    def one(s, prompt):
        raise ai.AIError("лимит", temporary=True, limit=True, retry_after={"k1": 300, "k2": 40, "k3": 90}[s["key"]])

    with pytest.raises(ai.AIError) as e:
        ai._with_keys(one, ai.settings(), "p")
    assert e.value.temporary and e.value.limit and e.value.retry_after == 40
    assert "Все ключи (3)" in str(e.value)


def test_each_stream_starts_with_its_own_key(data_dir):
    use_keys("gemini", "k1", "k2", "k3")
    first = []

    def one(s, prompt):
        first.append(s["key"])
        return {}, {}

    for slot in range(4):
        with ai.prefer_key(slot):
            ai._with_keys(one, ai.settings(), "p")
    assert first == ["k1", "k2", "k3", "k1"]


def gemini_server(limited_keys):
    seen = []

    def handler(request):
        key = request.headers["x-goog-api-key"]
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash",
                                                         "supportedGenerationMethods": ["generateContent"]}]})
        seen.append(key)
        if key in limited_keys:
            return httpx.Response(429, json={"error": {"code": 429, "message": "quota", "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "600s"}]}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(
            {f: "" for f in ai.FIELDS} | {"keywords": "кружка"})}]}, "finishReason": "STOP"}]})
    return httpx.MockTransport(handler), seen


def test_gemini_daily_limit_on_first_key_answers_with_second(data_dir):
    use_keys("gemini", "AIza-first-key-000", "AIza-second-key-00")
    transport, seen = gemini_server({"AIza-first-key-000"})
    result = ai.run({"name": "Кружка", "description": "", "params": []}, "keywords", transport=transport)
    assert result["keywords"] == "кружка"
    assert seen == ["AIza-first-key-000", "AIza-second-key-00"]
    # лучшая модель на втором ключе не «испорчена» лимитом первого: второй ключ начинает с неё же
    assert ai._usable(["gemini-3.8-flash", "gemini-3.8-flash-lite"], "AIza-second-key-00")[0] == "gemini-3.8-flash"


def test_extra_keys_are_secrets(data_dir):
    use_keys("gemini", "AIza-main-key-123", "AIza-extra-key-456")
    assert "gemini_keys_extra" in cloudbackup.SECRET_SETTINGS
    assert "AIza-extra-key-456" in config.known_secrets()
    assert "AIza-extra-key-456" not in config.redact("ошибка ключа AIza-extra-key-456")


def test_keys_api_admin_only(anon):
    admin = sign_in(anon)
    db.set_setting("ai_provider", "gemini")
    r = admin.post("/api/settings/ai-keys", json={"provider": "gemini", "key": " AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1 "})
    assert r.status_code == 200 and [x["mask"] for x in r.json()["gemini"]] == ["AIza…AAA1"]
    assert admin.post("/api/settings/ai-keys", json={"provider": "gemini", "key": "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"}).status_code == 400
    admin.post("/api/settings/ai-keys", json={"provider": "gemini", "key": "AIzaSyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"})
    assert ai.keys("gemini") == ["AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1", "AIzaSyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"]
    r = admin.delete("/api/settings/ai-keys?provider=gemini&n=0")  # убрали основной — запасной стал основным
    assert ai.keys("gemini") == ["AIzaSyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"] and r.json()["gemini"][0]["primary"]
    assert "AIzaSy" not in json.dumps(r.json())  # ключ целиком не отдаётся
    viewer = sign_in(anon, login="v1", role="viewer")
    assert viewer.get("/api/settings/ai-keys").status_code == 403


def test_parallel_switch_and_streams(anon):
    c = sign_in(anon)
    use_keys("gemini", "k1")
    assert aibulk.streams() == 1
    st = c.post("/api/ai/parallel", json={"multi": True}).json()
    assert st["parallel"] and st["streams"] == 2  # один ключ — два потока делят его темп
    use_keys("gemini", *[f"k{n}" for n in range(9)])
    assert aibulk.streams() == aibulk.MAX_STREAMS
    assert aibulk.speed_per_minute() == aibulk.rate_per_minute() * aibulk.MAX_STREAMS
    c.post("/api/ai/parallel", json={"multi": False})
    assert aibulk.streams() == 1 and aibulk.speed_per_minute() == aibulk.rate_per_minute()
    viewer = sign_in(anon, login="v2", role="viewer")
    assert viewer.post("/api/ai/parallel", json={"multi": True}).status_code == 403


def test_threads_never_take_the_same_product(data_dir):
    use_keys("gemini", "k1", "k2", "k3")
    ids = [products.create({"name": f"Товар {n}", "price": 100}) for n in range(12)]
    aibulk.create(ids, "custom", instruction="допиши")
    seen, lock = [], threading.Lock()

    def slow(p, action, instruction=""):
        with lock:
            seen.append(p["id"])
        time.sleep(0.05)
        return {"description": "готово"}

    def stream(slot):
        while aibulk.process_one(slow, slot) != "idle":
            pass
    threads = [threading.Thread(target=stream, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(seen) == sorted(ids)  # каждый товар — ровно один раз
    assert aibulk.list_jobs()[0]["status"] == "done"


def test_worker_runs_one_or_several_requests_at_once(data_dir, monkeypatch):
    from promloader import main

    use_keys("gemini", "k1", "k2", "k3")
    db.set_setting("ai_rate", "1000")
    state = {"now": 0, "max": 0, "calls": 0}
    lock = threading.Lock()

    def fake(runner=None, slot=0):
        with lock:
            state["now"] += 1
            state["calls"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
        return "done"
    monkeypatch.setattr(aibulk, "process_one", fake)
    monkeypatch.setattr(aibulk, "has_work", lambda: True)

    async def run_for(seconds):
        stop = asyncio.Event()
        task = asyncio.create_task(main.ai_worker(stop))
        await asyncio.sleep(seconds)
        stop.set()
        await asyncio.wait_for(task, 5)

    aibulk.set_parallel(False)
    asyncio.run(run_for(0.8))
    assert state["max"] == 1
    state.update(max=0, calls=0)
    aibulk.set_parallel(True)
    asyncio.run(run_for(0.8))
    assert state["max"] == 3  # по потоку на каждый из трёх ключей
