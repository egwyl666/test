import json

import httpx
import httpx2
import pytest

from promloader import ai, db, products

PRODUCT = {
    "name": "Кружка белая", "description": "Кружка.", "group_name": "Кружки", "price": 149.0, "currency": "UAH",
    "params": [{"name": "Объём", "value": "350 мл"}], "name_ua": "", "description_ua": "",
}


def use(provider, key="k-123", model=""):
    db.set_setting("ai_provider", provider)
    db.set_setting("gemini_key" if provider == "gemini" else "anthropic_key", key)
    if model:
        db.set_setting(f"{provider}_model", model)


def gemini_reply(payload: dict, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": status, "message": "boom"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]},
                                                         "finishReason": "STOP"}]})
    return httpx.MockTransport(handler), seen


def test_prompt_contains_product_as_data():
    prompt = ai.build_prompt(PRODUCT, "improve")
    assert "<product>" in prompt and "350 мл" in prompt and "Перепиши поле description" in prompt
    with pytest.raises(ai.AIError):
        ai.build_prompt(PRODUCT, "custom", "   ")
    with pytest.raises(ai.AIError):
        ai.build_prompt(PRODUCT, "nope")


def test_not_configured():
    assert not ai.enabled()
    with pytest.raises(ai.AIError, match="не подключён"):
        ai.run(PRODUCT, "improve")


def test_gemini_request_and_cleanup():
    use("gemini", model="gemini-test")
    transport, seen = gemini_reply({
        "name": "", "description": "<p>Отличная <i>кружка</i></p><script>x</script><ul><li>350 мл</li></ul>",
        "name_ua": "", "description_ua": "", "keywords": "",
    })
    changes = ai.run(PRODUCT, "improve", transport=transport)
    assert changes == {"description": "<p>Отличная кружка</p>x<ul><li>350 мл</li></ul>"}

    req = seen[0]
    assert req.url.path == "/v1beta/models/gemini-test:generateContent"
    assert req.headers["x-goog-api-key"] == "k-123"
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert set(body["generationConfig"]["responseSchema"]["properties"]) == set(ai.FIELDS)
    assert "Prom.ua" in body["systemInstruction"]["parts"][0]["text"]


def test_gemini_unchanged_result_is_error():
    use("gemini")
    transport, _ = gemini_reply({f: "" for f in ai.FIELDS} | {"name": "Кружка белая"})
    with pytest.raises(ai.AIError, match="не предложил"):
        ai.run(PRODUCT, "name", transport=transport)


@pytest.mark.parametrize("status, text", [(429, "лимит"), (403, "ключ"), (404, "модель не найдена"), (500, "HTTP 500")])
def test_gemini_errors(status, text):
    use("gemini")
    transport, _ = gemini_reply({}, status=status)
    with pytest.raises(ai.AIError, match=text):
        ai.run(PRODUCT, "improve", transport=transport)


def test_gemini_blocked():
    use("gemini")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(ai.AIError, match="SAFETY"):
        ai.run(PRODUCT, "improve", transport=transport)


def test_gemini_models_list():
    pages = [
        {"models": [{"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embedding-x", "supportedGenerationMethods": ["embedContent"]}],
         "nextPageToken": "p2"},
        {"models": [{"name": "models/gemini-b", "supportedGenerationMethods": ["generateContent", "countTokens"]}]},
    ]
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=pages[1 if r.url.params.get("pageToken") else 0]))
    assert ai.gemini_models("k", transport) == ["gemini-a", "gemini-b"]
    use("gemini", model="gemini-zzz")
    res = ai.check(transport)
    assert res["ok"] and "нет в списке" in res["message"]


def claude_reply(text: str, stop_reason="end_turn"):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        })
    return httpx2.MockTransport(handler), seen


def test_claude_request():
    use("claude", key="sk-test")
    transport, seen = claude_reply(json.dumps({f: "" for f in ai.FIELDS} | {
        "name_ua": "Кухоль білий", "description_ua": "<p>Кухоль.</p>"}))
    changes = ai.run(PRODUCT, "translate_ua", transport=transport)
    assert changes == {"name_ua": "Кухоль білий", "description_ua": "<p>Кухоль.</p>"}
    req = seen[0]
    body = json.loads(req.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert req.headers["x-api-key"] == "sk-test"


def test_claude_refusal():
    use("claude", key="sk-test")
    transport, _ = claude_reply("", stop_reason="refusal")
    with pytest.raises(ai.AIError, match="отказался"):
        ai.run(PRODUCT, "improve", transport=transport)


def test_api_endpoint(client, monkeypatch):
    pid = products.create({"name": "Кружка", "price": 10})
    r = client.post(f"/api/products/{pid}/ai", json={"action": "improve"})
    assert r.status_code == 400 and "Настройках" in r.json()["detail"]

    monkeypatch.setattr(ai, "run", lambda p, action, instruction="": {"description": f"<p>{p['name']} {action} {instruction}</p>"})
    r = client.post(f"/api/products/{pid}/ai", json={"action": "custom", "instruction": "короче"})
    assert r.json() == {"changes": {"description": "<p>Кружка custom короче</p>"}}
    assert products.get(pid)["description"] == ""  # ничего не сохраняется само

    s = client.post("/api/settings", json={"ai_provider": "gemini", "gemini_key": "AIzaSy-very-secret-key",
                                           "gemini_model": "models/gemini-x"}).json()
    assert s["gemini_key"] == "AIza…-key" and s["gemini_model"] == "gemini-x"
    assert client.get("/api/meta").json()["ai"]["enabled"] is True
    assert client.post("/api/settings", json={"ai_provider": "gpt"}).status_code == 400
