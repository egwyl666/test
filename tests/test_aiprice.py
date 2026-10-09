"""Цена запросов ИИ, учёт расходов, лимиты и доступность моделей (окно «✨ ИИ»)."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import httpx2
import pytest

from promloader import ai, aibulk, aiprice, db, products

PRODUCT = {"name": "Кружка белая", "description": "Кружка.", "price": 149.0, "params": []}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ai, "_sleep", lambda s: None)
    monkeypatch.setattr(ai, "CLAUDE_RETRIES", 0)  # без пауз SDK перед повтором
    ai._auto_cache.clear()
    # курс доллара для пересчёта в гривны — как будто НБУ уже ответил сегодня
    db.set_setting("nbu_rate_USD", json.dumps({"rate": 40.0, "date": date.today().isoformat()}))


def use(provider, key="k-123", model=""):
    db.set_setting("ai_provider", provider)
    db.set_setting("gemini_key" if provider == "gemini" else "anthropic_key", key)
    if model:
        db.set_setting(f"{provider}_model", model)


def usage_rows():
    return [dict(r) for r in db.query("SELECT * FROM ai_usage ORDER BY id")]


# ---------- цены ----------

def test_price_exact_family_unknown():
    assert aiprice.price("gemini-3.8-flash") == {"input": 0.75, "output": 3.75, "exact": True}
    assert aiprice.price("models/gemini-2.5-flash-001")["exact"]  # суффикс версии и префикс models/
    assert aiprice.price("claude-haiku-5-5")["input"] == 0.10
    family = aiprice.price("gemini-4.2-flash-lite")
    assert family == {"input": 0.30, "output": 2.50, "exact": False}
    assert aiprice.price("claude-sonnet-9")["input"] == 2.0 and not aiprice.price("claude-sonnet-9")["exact"]
    assert aiprice.price("gemma-3")["input"] is None


def test_cost_free_tier_and_unknown():
    assert aiprice.cost("gemini", "gemini-3.8-flash", 1000, 500) == 0.002625
    db.set_setting("gemini_free_tier", "1")
    assert aiprice.cost("gemini", "gemini-3.8-flash", 1000, 500) == 0.0
    assert aiprice.cost("claude", "claude-opus-5-5", 1000, 500) == 0.014  # бесплатный ключ — только у Gemini
    assert aiprice.cost("claude", "something-else", 1000, 500) is None


def test_to_uah_rate_sources():
    assert aiprice.to_uah(0.01) == 0.4
    db.set_setting("rate_add", "10")
    assert aiprice.to_uah(0.01) == 0.44
    db.set_setting("rate_mode", "manual")
    db.set_setting("rate_manual", json.dumps({"USD": 42}))
    assert aiprice.to_uah(0.01) == 0.42
    db.set_setting("rate_manual", "{}")
    assert aiprice.to_uah(0.01) is None  # курса нет — показываем только доллары
    assert aiprice.to_uah(None) is None


def test_stale_rate_used_while_refreshing():
    db.set_setting("nbu_rate_USD", json.dumps({"rate": 41.0, "date": "2026-01-01"}))
    assert aiprice.to_uah(1) == 41.0  # старый курс сразу, свежий — в фоне


# ---------- учёт запросов ----------

def gemini_ok(model_used=None, usage=None):
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash",
                                                         "supportedGenerationMethods": ["generateContent"]}]})
        seen.append(request)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps({"description": "<p>Новое</p>"})}]}}],
            "usageMetadata": usage or {"promptTokenCount": 1000, "candidatesTokenCount": 400, "thoughtsTokenCount": 100},
        })
    return httpx.MockTransport(handler), seen


def test_gemini_usage_recorded_with_thinking_as_output():
    use("gemini")
    transport, _ = gemini_ok()
    changes, used = ai.run_detailed(PRODUCT, "improve", transport=transport)
    assert changes == {"description": "<p>Новое</p>"}
    assert used == {"provider": "gemini", "model": "gemini-3.8-flash", "tokens_in": 1000, "tokens_out": 500,
                    "cost_usd": 0.002625, "free": False}
    row = usage_rows()[-1]
    assert (row["source"], row["action"], row["ok"], row["cost_usd"]) == ("card", "improve", 1, 0.002625)
    assert aiprice.model_statuses()["gemini:gemini-3.8-flash"]["status"] == "ok"


def claude_server(model="claude-opus-5-5", status=200, headers=None, error=None):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [
                {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-01-01T00:00:00Z"},
                {"type": "model", "id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5", "created_at": "2026-01-01T00:00:00Z"},
            ], "has_more": False, "first_id": "claude-opus-5-5", "last_id": "claude-haiku-5-5"})
        if status != 200:
            return httpx2.Response(status, headers=headers or {}, json={"type": "error", "error": error or {
                "type": "rate_limit_error", "message": "slow down"}})
        body = json.loads(request.content)
        return httpx2.Response(200, headers=headers or {}, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": model or body["model"],
            "content": [{"type": "text", "text": json.dumps({"name": "", "description": "", "name_ua": "",
                                                             "description_ua": "", "keywords": "кружка, чашка"})}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500},
        })
    return httpx2.MockTransport(handler), seen


LIMITS = {"anthropic-ratelimit-requests-limit": "50", "anthropic-ratelimit-requests-remaining": "49",
          "anthropic-ratelimit-requests-reset": "2026-10-09T12:00:30Z",
          "anthropic-ratelimit-input-tokens-limit": "30000", "anthropic-ratelimit-input-tokens-remaining": "28500",
          "anthropic-ratelimit-input-tokens-reset": "2026-10-09T12:00:10Z"}


def test_claude_usage_and_limits_from_headers():
    use("claude", key="sk-test")
    transport, seen = claude_server(headers=LIMITS)
    changes, used = ai.run_detailed(PRODUCT, "keywords", transport=transport)
    assert changes == {"keywords": "кружка, чашка"}
    # кеш считается входом (по верхней цене — честнее переоценить, чем недооценить)
    assert used["tokens_in"] == 1500 and used["tokens_out"] == 200
    assert used["cost_usd"] == round((1500 * 4 + 200 * 20) / 1e6, 6)
    lim = aiprice.limits()["claude"]
    assert lim["requests"] == {"limit": 50, "remaining": 49, "reset": "2026-10-09T12:00:30Z"}
    assert lim["input_tokens"]["remaining"] == 28500 and "output_tokens" not in lim


def test_claude_fallback_priced_by_model_that_answered():
    use("claude", key="sk-test")
    transport, _ = claude_server(model="claude-sonnet-5-5")
    _, used = ai.run_detailed(PRODUCT, "keywords", transport=transport)
    assert used["model"] == "claude-sonnet-5-5" and used["cost_usd"] == round((1500 * 2 + 200 * 10) / 1e6, 6)


@pytest.mark.parametrize("model, fallback, effort", [("claude-opus-5-5", True, True), ("claude-haiku-5-5", False, True),
                                                     ("claude-haiku-4-5", False, False)])
def test_claude_params_per_model(model, fallback, effort):
    use("claude", key="sk-test", model=model)
    transport, seen = claude_server(model=model)
    ai.run(PRODUCT, "keywords", transport=transport)
    body = json.loads(seen[0].content)
    assert ("fallbacks" in body) == fallback
    assert ("server-side-fallback-2026-07-01" in seen[0].headers.get("anthropic-beta", "")) == fallback
    assert ("effort" in body["output_config"]) == effort


def test_claude_rate_limit_message_and_status():
    use("claude", key="sk-test")
    headers = {**LIMITS, "anthropic-ratelimit-input-tokens-remaining": "0", "retry-after": "12"}
    transport, _ = claude_server(status=429, headers=headers)
    with pytest.raises(ai.AIError, match="входных токенов в минуту — повторить можно через 12 с") as err:
        ai.run(PRODUCT, "keywords", transport=transport)
    assert err.value.temporary
    assert aiprice.limits()["claude"]["retry_seconds"] == 12.0
    row = usage_rows()[-1]
    assert row["ok"] == 0 and row["cost_usd"] == 0 and "лимит" in row["error"]
    assert aiprice.model_statuses()["claude:claude-opus-5-5"]["status"] == "limit"


def quota_429(quota_id, value, retry="31s", model="gemini-3.8-flash"):
    return {"error": {"code": 429, "message": f"Quota exceeded ... limit: {value}", "status": "RESOURCE_EXHAUSTED",
                      "details": [
                          {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                              "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                              "quotaId": quota_id, "quotaDimensions": {"location": "global", "model": model},
                              "quotaValue": str(value)}]},
                          {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry}]}}


def test_gemini_free_tier_limit_parsed_and_remembered():
    use("gemini", model="gemini-3.8-flash")
    transport = httpx.MockTransport(lambda r: httpx.Response(
        429, json=quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 250)))
    with pytest.raises(ai.AIError, match="лимит 250 запросов в сутки для gemini-3.8-flash .бесплатный ключ. — "
                                         "снова можно через 31 с. Чтобы программа сама переходила") as err:
        ai.run(PRODUCT, "improve", transport=transport)
    assert err.value.temporary and err.value.retry_after == 31
    q = aiprice.limits()["gemini"]["last_limit"]
    assert (q["period"], q["kind"], q["limit"], q["free_tier"]) == ("day", "requests", 250, True)
    assert aiprice.free_tier()  # Google сам сказал, что ключ бесплатный — цена 0
    assert aiprice.model_statuses()["gemini:gemini-3.8-flash"]["status"] == "limit"


def test_gemini_free_tier_not_overridden_when_user_said_paid():
    use("gemini", model="gemini-3.8-flash")
    db.set_setting("gemini_free_tier", "0")
    transport = httpx.MockTransport(lambda r: httpx.Response(
        429, json=quota_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 10)))
    with pytest.raises(ai.AIError, match="в минуту"):
        ai.run(PRODUCT, "improve", transport=transport)
    assert not aiprice.free_tier()


def test_gemini_limit_zero_means_unavailable_on_key():
    use("gemini", model="gemini-3.1-pro-preview")
    transport = httpx.MockTransport(lambda r: httpx.Response(
        429, json=quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 0, model="gemini-3.1-pro-preview")))
    with pytest.raises(ai.AIError, match="недоступна на вашем ключе") as err:
        ai.run(PRODUCT, "improve", transport=transport)
    assert not err.value.temporary
    assert aiprice.model_statuses()["gemini:gemini-3.1-pro-preview"]["status"] == "unavailable"


def test_auto_skips_model_unavailable_on_free_key():
    use("gemini")
    calls = []

    def handler(r):
        if r.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                                                        for m in ("gemini-3.8-flash", "gemini-3.8-flash-lite")]})
        model = r.url.path.rsplit("/", 1)[-1].split(":")[0]
        calls.append(model)
        if model == "gemini-3.8-flash":
            return httpx.Response(429, json=quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 0))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"description": "<p>Да</p>"})}]}}]})
    transport = httpx.MockTransport(handler)
    assert ai.run(PRODUCT, "improve", transport=transport) == {"description": "<p>Да</p>"}
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash-lite"]
    ai.run(PRODUCT, "improve", transport=transport)  # во второй раз недоступную модель не пробует первой
    assert calls[2:] == ["gemini-3.8-flash-lite"]
    assert db.get_setting("gemini_model_used") == "gemini-3.8-flash-lite"


def test_gemini_limit_text_only_message():
    q = ai._gemini_quota({"error": {"message": "Quota exceeded, limit: 0, model: x"}}, "gemini-x")
    assert q == {"model": "gemini-x", "limit": 0}
    assert ai._gemini_quota({"error": {"message": "other"}}) is None


# ---------- список моделей и проверка ----------

def test_claude_models_list_with_prices():
    use("claude", key="sk-test")
    transport, seen = claude_server()
    aiprice.set_model_status("claude", "claude-haiku-5-5", "ok")
    items = {m["id"]: m for m in ai.list_models(transport)}
    assert items["claude-opus-5-5"]["name"] == "Claude Opus 5.5"
    assert items["claude-haiku-5-5"]["typical_usd"] == round((1500 * 0.10 + 900 * 0.50) / 1e6, 6)
    assert items["claude-haiku-5-5"]["status"] == "ok" and items["claude-opus-5-5"]["status"] == ""
    assert all(r.url.path.endswith("/models") for r in seen)  # список — без платных запросов


def test_claude_check_is_free():
    use("claude", key="sk-test", model="claude-opus-5-5")
    transport, seen = claude_server()
    res = ai.check(transport)
    assert res["ok"] and res["models"] == ["claude-opus-5-5", "claude-haiku-5-5"] and "Выбрана модель" in res["message"]
    assert all(r.method == "GET" for r in seen) and usage_rows() == []


def test_check_models_statuses_do_not_change_choice():
    use("gemini", model="gemini-3.8-flash")
    db.set_setting("gemini_model_used", "gemini-3.8-flash")
    replies = {
        "gemini-3.8-flash": httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps({"keywords": "кружка"})}]}}],
            "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 5}}),
        "gemini-x-denied": httpx.Response(403, json={"error": {"message": "denied"}}),
        "gemini-3.1-pro-preview": httpx.Response(429, json=quota_429("RequestsPerDay-FreeTier", 0)),
        "gemini-2.5-flash": httpx.Response(429, json=quota_429("GenerateRequestsPerMinutePerProjectPerModel", 15)),
        "gemini-busy": httpx.Response(503, json={"error": {"message": "overloaded"}}),
        "gemini-gone": httpx.Response(404, json={"error": {"message": "not found"}}),
    }
    transport = httpx.MockTransport(lambda r: replies[r.url.path.rsplit("/", 1)[-1].split(":")[0]])
    res = {x["model"]: x["status"] for x in ai.check_models(list(replies), transport)}
    assert res == {"gemini-3.8-flash": "ok", "gemini-x-denied": "unavailable", "gemini-3.1-pro-preview": "unavailable",
                   "gemini-2.5-flash": "limit", "gemini-busy": "busy", "gemini-gone": "unavailable"}
    # проверка не переключает выбранную модель и не трогает «использованную»
    assert db.get_setting("gemini_model") == "gemini-3.8-flash" and db.get_setting("gemini_model_used") == "gemini-3.8-flash"
    rows = usage_rows()
    assert {r["source"] for r in rows} == {"check"} and {r["action"] for r in rows} == {"check"}
    assert sum(r["cost_usd"] or 0 for r in rows) == round((60 * 0.75 + 5 * 3.75) / 1e6, 6)


def test_check_models_needs_key():
    with pytest.raises(ai.AIError, match="ключ"):
        ai.check_models(["gemini-3.8-flash"])


# ---------- сводка, массовый ИИ ----------

def add_usage(at, cost, model="gemini-3.8-flash", ok=1, job_id=None, action="improve"):
    with db.tx() as c:
        c.execute("""INSERT INTO ai_usage (at, provider, model, action, source, job_id, tokens_in, tokens_out, cost_usd, ok)
                     VALUES (?, 'gemini', ?, ?, 'card', ?, 100, 50, ?, ?)""", (at, model, action, job_id, cost, ok))


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def test_summary_today_month_by_model():
    now = datetime.now().astimezone()
    add_usage(iso(now), 0.01)
    add_usage(iso(now), 0.02, model="gemini-2.5-flash")
    add_usage(iso(now), 0.0, ok=0)
    add_usage(iso(now - timedelta(days=40)), 5.0)  # прошлый месяц — не в сводке
    add_usage(iso(now), None, model="mystery")  # цена неизвестна
    s = aiprice.summary()
    assert s["today"]["requests"] == 4 and s["today"]["cost_usd"] == 0.03 and s["today"]["unpriced"] == 1
    assert s["month"]["cost_usd"] == 0.03 and s["last_minute"] == 4
    assert [m["model"] for m in s["by_model"]][:2] == ["gemini-2.5-flash", "gemini-3.8-flash"]
    assert len(s["recent"]) == 5 and s["recent"][0]["model"] == "mystery"


def test_bulk_job_spent_and_estimate():
    use("gemini", model="gemini-3.8-flash")
    a = products.create({"name": "Кружка", "price": 1})
    b = products.create({"name": "Чашка", "price": 1})
    est = aibulk.estimate(10, "improve")
    assert not est["measured"] and est["per_request_usd"] == aiprice.typical_cost("gemini", "gemini-3.8-flash")
    assert est["total_uah"] == round(est["total_usd"] * 40, 4) and est["minutes"] == 1

    job = aibulk.create([a, b], "improve", only_empty=False)
    transport, _ = gemini_ok()
    while aibulk.process_one(lambda p, action, instr="": ai.run(p, action, instr, transport=transport)) != "idle":
        pass
    spent = aibulk.get(job["id"])["spent"]
    assert spent["requests"] == 2 and spent["cost_usd"] == 2 * 0.002625 and spent["cost_uah"] == round(2 * 0.002625 * 40, 4)
    assert {r["source"] for r in usage_rows()} == {"bulk"}

    add_usage(db.now(), 0.002625)  # третий замер — оценка по своим запросам, а не по «типичному»
    est = aibulk.estimate(4, "improve")
    assert est["measured"] and est["total_usd"] == pytest.approx(4 * 0.002625)


def test_estimate_free_key():
    use("gemini", model="gemini-3.8-flash")
    db.set_setting("gemini_free_tier", "1")
    est = aibulk.estimate(5, "improve")
    assert est["free"] and est["total_usd"] == 0


# ---------- API окна «ИИ» ----------

def test_status_and_choose_model(client):
    r = client.post("/api/ai/model", json={"provider": "claude"})
    assert r.status_code == 400 and "Настройках" in r.json()["detail"]  # ключи — только в «Настройках»
    use("gemini")
    db.set_setting("anthropic_key", "sk-test")
    add_usage(db.now(), 0.01)
    s = client.get("/api/ai/status").json()
    assert s["provider"] == "gemini" and s["keys"] == {"gemini": True, "claude": True} and s["model"] == ""
    assert s["summary"]["today"]["cost_uah"] == 0.4 and s["prices_checked"] == aiprice.PRICES_CHECKED

    s = client.post("/api/ai/model", json={"provider": "claude", "model": "claude-haiku-5-5"}).json()
    assert s["provider"] == "claude" and s["model"] == "claude-haiku-5-5"
    assert db.get_setting("claude_model") == "claude-haiku-5-5"
    s = client.post("/api/ai/model", json={"provider": "gemini", "model": "models/gemini-2.5-flash", "free_tier": True}).json()
    assert s["model"] == "gemini-2.5-flash" and s["free_tier"] is True
    assert client.post("/api/ai/model", json={"provider": "gpt"}).status_code == 400

    meta = client.get("/api/meta").json()["ai"]
    assert meta["model"] == "gemini-2.5-flash" and meta["free"] and meta["today_requests"] == 1
    assert meta["today_uah"] == 0.4


def test_meta_shows_auto_model(client):
    use("gemini")
    db.set_setting("gemini_model_used", "gemini-3.8-flash")
    meta = client.get("/api/meta").json()["ai"]
    assert meta["auto"] and meta["model"] == "gemini-3.8-flash"


def test_bulk_estimate_endpoint(client):
    use("gemini", model="gemini-3.8-flash")
    ids = [products.create({"name": f"Товар {i}", "price": 1}) for i in range(3)]
    r = client.post("/api/ai/bulk/estimate", json={"action": "improve", "ids": ids}).json()
    assert r["count"] == 3 and r["model"] == "gemini-3.8-flash" and r["total_usd"] > 0
    r = client.post("/api/ai/bulk/estimate", json={"action": "improve", "filter": {}}).json()
    assert r["count"] == 3


def test_models_check_endpoint_without_key(client):
    r = client.post("/api/ai/models/check", json={"models": ["gemini-3.8-flash"]})
    assert r.status_code == 400


def test_housekeeping_drops_year_old_usage():
    from promloader import housekeeping

    add_usage(iso(datetime.now().astimezone() - timedelta(days=400)), 1.0)
    add_usage(db.now(), 0.01)
    assert housekeeping.run()["ai_usage"] == 1
    assert len(usage_rows()) == 1


# ---------- ключ не принят, лимиты по моделям (2.3.1) ----------

BAD_KEY = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
                     "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}


def test_clean_key_and_warning():
    assert ai.clean_key(' "GEMINI_API_KEY=AQ.Ab8​RN 6 x" ') == "AQ.Ab8RN6x"
    assert ai.clean_key("«AIzaSyX»\n") == "AIzaSyX"
    assert ai.key_warning("gemini", "AIza" + "x" * 35) == "" and ai.key_warning("gemini", "AQ.Ab8RN6I8") == ""
    assert "токен Prom" in ai.key_warning("gemini", "a" * 40)
    assert "AIza" in ai.key_warning("gemini", "sk-ant-123")
    assert "sk-ant-" in ai.key_warning("claude", "AIza123")


def test_invalid_key_is_about_key_not_model(client):
    use("gemini", key="AIzaSy-wrong-key-1234", model="gemini-3.8-flash")
    aiprice.set_model_status("gemini", "gemini-3.8-flash", "ok")
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json=BAD_KEY))
    with pytest.raises(ai.AIError, match="Google не принял ключ Gemini .ключ программы заканчивается на «…1234».") as err:
        ai.run(PRODUCT, "improve", transport=transport)
    assert err.value.key_error and not err.value.temporary and "aistudio.google.com/apikey" in str(err.value)
    assert aiprice.model_statuses()["gemini:gemini-3.8-flash"]["status"] == "ok"  # модель ни при чём
    assert client.get("/api/meta").json()["ai"]["key_error"] is True
    assert "Google не принял" in client.get("/api/ai/status").json()["key_error"]["message"]

    transport, _ = gemini_ok()
    ai.run(PRODUCT, "improve", transport=transport)  # удачный запрос снимает «ключ не принят»
    assert client.get("/api/meta").json()["ai"]["key_error"] is False

    aiprice.set_key_error("gemini", "плохой ключ")
    s = client.post("/api/settings", json={"gemini_key": " AIzaSy​new-key-56789012345678901234567 "}).json()
    assert db.get_setting("gemini_key") == "AIzaSynew-key-56789012345678901234567" and s["ai_key_warning"] == ""
    assert aiprice.key_errors() == {}  # новый ключ — старая ошибка к нему не относится
    s = client.post("/api/settings", json={"gemini_key": "b" * 40}).json()
    assert "токен Prom" in s["ai_key_warning"]


def test_invalid_key_from_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-env-key-9876")
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_model", "gemini-3.8-flash")
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json=BAD_KEY))
    with pytest.raises(ai.AIError, match="переменной Windows GEMINI_API_KEY"):
        ai.run(PRODUCT, "improve", transport=transport)


def test_check_with_invalid_key_sets_and_clears():
    use("gemini")
    with pytest.raises(ai.AIError, match="не принял ключ"):
        ai.check(httpx.MockTransport(lambda r: httpx.Response(400, json=BAD_KEY)))
    assert "gemini" in aiprice.key_errors()
    ai.check(httpx.MockTransport(lambda r: httpx.Response(200, json={"models": []})))
    assert aiprice.key_errors() == {}


def test_claude_invalid_key():
    use("claude", key="sk-ant-wrong-abcd")
    transport, _ = claude_server(status=401, error={"type": "authentication_error", "message": "invalid x-api-key"})
    with pytest.raises(ai.AIError, match="Anthropic не принял ключ Claude .ключ программы заканчивается на «…abcd».") as err:
        ai.run(PRODUCT, "keywords", transport=transport)
    assert err.value.key_error and "claude" in aiprice.key_errors()
    assert "claude:claude-opus-5-5" not in aiprice.model_statuses()


def per_model_server(exhausted, retry="23385s"):
    calls = []

    def handler(r):
        if r.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                                                        for m in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash")]})
        model = r.url.path.rsplit("/", 1)[-1].split(":")[0]
        calls.append(model)
        if model in exhausted:
            return httpx.Response(429, json=quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 20, retry, model))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"description": "<p>Да</p>"})}]}}]})
    return httpx.MockTransport(handler), calls


def test_auto_moves_to_next_model_when_daily_limit_is_used_up():
    use("gemini")
    transport, calls = per_model_server({"gemini-3.8-flash", "gemini-3.7-flash"})
    assert ai.run(PRODUCT, "improve", transport=transport) == {"description": "<p>Да</p>"}
    assert calls == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    assert db.get_setting("gemini_model_used") == "gemini-3.6-flash"
    assert aiprice.limited_now("gemini", "gemini-3.8-flash") and not aiprice.limited_now("gemini", "gemini-3.6-flash")
    ai.run(PRODUCT, "improve", transport=transport)  # до сброса лимита исчерпанные модели не пробует первыми
    assert calls[3:] == ["gemini-3.6-flash"]


def test_auto_all_models_used_up():
    use("gemini")
    transport, calls = per_model_server({"gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"})
    with pytest.raises(ai.AIError, match="исчерпаны лимиты моделей gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash"
                                         " — снова можно через 6 ч 30 мин") as err:
        ai.run(PRODUCT, "improve", transport=transport)
    assert err.value.temporary and err.value.retry_after == 23385


def test_limit_status_expires():
    aiprice.set_model_status("gemini", "gemini-3.8-flash", "limit", "лимит", retry_after=3600)
    assert aiprice.limited_now("gemini", "gemini-3.8-flash")
    aiprice.set_model_status("gemini", "gemini-3.8-flash", "limit", "лимит", retry_after=-5)
    assert not aiprice.limited_now("gemini", "gemini-3.8-flash")


def test_bulk_remembers_limit_for_pause():
    use("gemini")
    pid = products.create({"name": "Кружка", "price": 1})
    aibulk.create([pid], "improve", only_empty=False)

    def limited(p, action, instruction=""):
        raise ai.AIError("Gemini: исчерпан лимит", temporary=True, retry_after=600)
    assert aibulk.process_one(limited) == "rate_limited"
    assert aibulk.last_limit.retry_after == 600
