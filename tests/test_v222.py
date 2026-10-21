"""2.22.0: «📷 Заполнить по фото», конфликты с кабинетом Prom, возможные дубли, «первые N по выгоде»."""

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from promloader import ai, aibulk, db, duplicates, products, promcatalog
from promloader.prom_api import PromClient


def ai_on(provider="gemini"):
    db.set_setting("ai_provider", provider)
    db.set_setting("gemini_key" if provider == "gemini" else "anthropic_key", "k")


def png(size=(2000, 1500), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def answer(fields: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(fields)}]}}]})


FILLED = {"name": "Чохол силіконовий червоний", "description": "<p>Червоний силіконовий чохол.</p>",
          "name_ua": "Чохол силіконовий червоний", "description_ua": "<p>Червоний силіконовий чохол.</p>",
          "keywords": "чохол, чехол, чехол, силиконовый чехол"}


# ---------- 1. по фото ----------

def test_from_photo_sends_image_to_gemini_and_fills_card(monkeypatch):
    ai_on()
    pid = products.create({"name": "Чехол 123", "price": 100})
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://cdn.example.com/1.png')", (pid,))
    seen = []

    def handler(r):
        if r.url.host == "cdn.example.com":
            return httpx.Response(200, content=png())
        if r.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]})
        seen.append(json.loads(r.content))
        return answer(FILLED)
    transport = httpx.MockTransport(handler)
    p = products.get(pid)
    assert ai.too_little_data(p)
    changes = ai.run(p, "from_photo", transport=transport)
    parts = seen[0]["contents"][0]["parts"]
    img = parts[0]["inline_data"]
    assert img["mime_type"] == "image/jpeg"
    picture = Image.open(io.BytesIO(base64.b64decode(img["data"])))
    assert max(picture.size) <= ai.PHOTO_SIDE                        # уменьшено до 1024 px
    assert "Не придумывай" in parts[1]["text"]
    assert changes["keywords"] == "чохол, чехол, силиконовый чехол"  # повторы убраны
    products.update(pid, changes)
    assert not ai.too_little_data(products.get(pid))                 # дальше товар может идти в «Готов»


def test_from_photo_local_file_and_claude_image_block():
    ai_on("claude")
    db.set_setting("claude_model", "claude-opus-5-5")
    pid = products.create({"name": "Ч 1", "price": 100})
    name = products.store_image_bytes(png((300, 300)))
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, file) VALUES (?, ?)", (pid, name))
    seen = []

    def handler(r):
        body = json.loads(r.content)
        seen.append(body)
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(FILLED)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})
    import httpx2  # клиент Anthropic — на своём httpx2
    ai.run(products.get(pid), "from_photo", transport=httpx2.MockTransport(handler))
    content = seen[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert content[1]["type"] == "text"


def test_from_photo_errors_without_photo_or_too_big():
    ai_on()
    pid = products.create({"name": "Ч 2", "price": 100})
    with pytest.raises(ai.AIError, match="нет фото"):
        ai.product_photo(products.get(pid))
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://cdn.example.com/big.jpg')", (pid,))
    big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (ai.PHOTO_MAX_BYTES + 10)))
    with pytest.raises(ai.AIError, match="больше 4 МБ"):
        ai.product_photo(products.get(pid), big)


def test_bulk_from_photo_one_per_request_and_skips_without_photo():
    ai_on()
    with_photo = products.create({"name": "Ч 3", "price": 100})
    without = products.create({"name": "Ч 4", "price": 100})
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://x/1.jpg')", (with_photo,))
    assert ai.batch_size("from_photo") == 1
    called = []
    job = aibulk.create([with_photo, without], "from_photo", auto_promote=True)
    while aibulk.process_one(lambda p, a, i="": (called.append(p["id"]), FILLED)[1]) != "idle":
        pass
    assert called == [with_photo]
    assert aibulk.get(job["id"])["counts"] == {"done": 1, "skipped": 1}
    p = products.get(with_photo)
    assert p["description"] == FILLED["description"] and p["status"] == "ready"   # фото есть, цена есть — «Готов»


# ---------- 2. конфликты с кабинетом Prom ----------

def prom_item(**extra):
    return {"id": 501, "external_id": "EXT-1", "name": "Кружка", "price": 100, "currency": "UAH",
            "presence": "available", "description": "<p>Кружка</p>", "keywords": "кружка", "status": "on_display", **extra}


def load(*items):
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"products": list(items) if not r.url.params.get("last_id") else []}))
    return asyncio.run(promcatalog.run(lambda: PromClient("t", "https://my.prom.ua/api/v1", transport=t)))


def test_conflict_when_both_sides_changed_and_resolution(client):
    load(prom_item())                                   # первая синхронизация — снимок того, что на Prom
    pid = products.find_by_external_id("EXT-1")
    products.update(pid, {"price": 150, "name": "Кружка белая"})   # у нас правки, ещё не отправлены
    assert products.get(pid)["status"] == "ready"
    counts = load(prom_item(price=120, keywords="кружка, чашка"))  # а в кабинете поменяли цену
    assert counts["conflicts"] == 1
    p = client.get(f"/api/products/{pid}").json()
    # ключевые слова поменяли только в кабинете — но наша неотправленная версия при отправке их затёрла бы: тоже конфликт
    assert p["prom_conflict"] == {"price": 120.0, "keywords": "кружка, чашка"}
    assert p["price"] == 150 and p["name"] == "Кружка белая"       # наши правки целы
    assert client.get("/api/products/alerts").json()["conflicts"] == 1
    assert [x["id"] for x in client.get("/api/products?conflict=1").json()["items"]] == [pid]
    r = client.post(f"/api/products/{pid}/prom-conflict", json={"action": "take", "fields": ["price"]}).json()
    assert r["price"] == 120 and r["prom_conflict"] == {"keywords": "кружка, чашка"} and r["name"] == "Кружка белая"
    r = client.post(f"/api/products/{pid}/prom-conflict", json={"action": "take", "fields": ["keywords"]}).json()
    assert r["keywords"] == "кружка, чашка" and r["prom_conflict"] == {}
    assert client.get("/api/products/alerts").json()["conflicts"] == 0


def test_no_conflict_without_snapshot_or_after_own_send():
    load(prom_item())
    pid = products.find_by_external_id("EXT-1")
    with db.tx() as c:
        c.execute("UPDATE products SET prom_seen = '' WHERE id = ?", (pid,))   # только что отправили сами
    products.update(pid, {"price": 150})
    load(prom_item(price=130))
    assert products.get(pid)["prom_conflict"] == {}


def test_keep_local_and_take_all(client):
    load(prom_item())
    pid = products.find_by_external_id("EXT-1")
    products.update(pid, {"price": 150, "description": "<p>Наша</p>"})
    load(prom_item(price=120, description="<p>Из кабинета</p>"))
    assert set(products.get(pid)["prom_conflict"]) == {"price", "description"}
    r = client.post(f"/api/products/{pid}/prom-conflict", json={"action": "keep", "fields": ["price"]}).json()
    assert r["price"] == 150 and set(r["prom_conflict"]) == {"description"}
    r = client.post(f"/api/products/{pid}/prom-conflict", json={"action": "take"}).json()
    assert r["description"] == "<p>Из кабинета</p>" and r["prom_conflict"] == {}
    assert client.post(f"/api/products/{pid}/prom-conflict", json={"action": "x"}).status_code == 400


# ---------- 3. дубли ----------

def test_duplicates_grouped_by_barcode_code_and_name(client):
    a = products.create({"name": "Кружка белая 300 мл", "price": 200, "cost_price": 100, "barcode": "4820000000017"})
    b = products.create({"name": "Кружка біла", "price": 200, "cost_price": 150, "barcode": "4820000000017"})
    c1 = products.create({"name": "Лампа", "price": 100, "vendor_code": "LMP-01"})
    c2 = products.create({"name": "Лампа настольная", "price": 100, "vendor_code": "lmp 01"})
    n1 = products.create({"name": "Зонт складной, чёрный", "price": 100})
    n2 = products.create({"name": "зонт  складной чёрный", "price": 100})
    products.create({"name": "Одиночка", "price": 100})
    groups = {g["kind"]: set(g["ids"]) for g in duplicates.find()}
    assert groups == {"barcode": {a, b}, "vendor_code": {c1, c2}, "name": {n1, n2}}
    items = client.get("/api/products?duplicates=1").json()["items"]
    ids = [x["id"] for x in items]
    assert set(ids) == {a, b, c1, c2, n1, n2}
    assert ids.index(a) < ids.index(b)                  # в группе сначала выгодный
    assert items[0]["dup"]["kind"] == "штрихкод" and items[0]["margin_uah"] == 100
    assert client.get("/api/products/alerts").json()["duplicates"] == 6


# ---------- 4. первые N по выгоде ----------

def test_bulk_limit_first_n_by_profit(client):
    ai_on()
    ids = []
    for i in range(30):
        pid = products.create({"name": f"Товар номер {i}", "price": 100 + i * 10, "cost_price": 100, "vendor": "V"})
        with db.tx() as c:
            c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://x/1.jpg')", (pid,))
        ids.append(pid)
    est = client.post("/api/ai/bulk/estimate", json={"action": "keywords", "filter": {}, "limit": 20}).json()
    assert est["count"] == 20
    job = client.post("/api/ai/bulk", json={"action": "keywords", "filter": {}, "limit": 20}).json()
    assert job["total"] == 20
    chosen = {r["product_id"] for r in db.query("SELECT product_id FROM ai_items WHERE job_id = ?", (job["id"],))}
    assert chosen == set(ids[10:])                      # 20 с самой большой наценкой
    assert client.post("/api/ai/bulk", json={"action": "keywords", "filter": {}, "limit": 0}).status_code == 422


# ---------- цена 0,1 грн: не «готов» и не уходит на Prom ----------

def test_price_below_one_uah_is_an_error_and_not_ready(client):
    pid = products.create({"name": "Кружка белая керамическая", "price": 0.1, "name_ua": "Кухоль", "keywords": "к",
                           "vendor": "V"})
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, url) VALUES (?, 'https://x/1.jpg')", (pid,))
    assert "Цена меньше 1 грн — похоже на ошибку, проверьте цену" in products.get(pid)["check"]["errors"]
    r = client.get("/api/products/readiness").json()
    assert r["ready_to_promote"] == 0 and r["missing_price_or_photo"] == 1
    assert products.validate({"name": "x", "price": 0.5, "currency": "USD"})["ok"]   # в долларах — бывает


def test_ai_keywords_never_touch_price():
    """Жалоба «ключевые слова переписали цену на 0,1»: ответ ИИ с ценой игнорируется, цена не меняется."""
    ai_on()
    pid = products.create({"name": "Кружка белая", "price": 250, "cost_price": 100})
    aibulk.create([pid], "keywords")
    while aibulk.process_one(lambda p, a, i="": ai._clean({"keywords": "кружка", "price": "0,1"}, p)) != "idle":
        pass
    p = products.get(pid)
    assert p["keywords"] == "кружка" and p["price"] == 250
