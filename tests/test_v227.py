"""2.27.0: «↩ Отменить» быстрых действий со статусами и сквозные проверки логики — цена от закупки в валюте через
наценку и курс, переходы статусов товара, повторный и «рваный» прайс поставщика."""

import pytest

from promloader import db, pricing, products, rates, suppliers, undo

from .test_suppliers import BASE, make_supplier, make_yml, products_by_sku, run


def _status(pid):
    return db.query_one("SELECT status FROM products WHERE id = ?", (pid,))["status"]


def _make(status="draft", synced=False, **fields):
    pid = products.create({"name": f"Товар {status}", "price": 100, **fields})
    with db.tx() as c:
        c.execute("UPDATE products SET status = ?, synced_at = ? WHERE id = ?", (status, db.now() if synced else None, pid))
    return pid


# ---------- «↩ Отменить» ----------

def test_undo_returns_each_product_to_its_own_status(client):
    a, b, c = _make("draft"), _make("ready"), _make("synced", synced=True)
    r = client.post("/api/products/status", json={"ids": [a, b, c], "status": "draft"}).json()
    assert r["changed"] == 3 and r["undo"]
    assert [_status(x) for x in (a, b, c)] == ["draft"] * 3
    assert client.post(f"/api/products/undo/{r['undo']}").json() == {"restored": 3}
    assert [_status(x) for x in (a, b, c)] == ["draft", "ready", "synced"]
    last = db.query_one("SELECT source, new FROM product_changes WHERE product_id = ? ORDER BY id DESC LIMIT 1", (c,))
    assert last["source"] == "Отменено: В черновики" and last["new"] == products.STATUSES["synced"]


def test_undo_is_single_use_and_unknown_token_is_gone(client):
    a = _make("ready")
    token = client.post("/api/products/status", json={"ids": [a], "status": "draft"}).json()["undo"]
    assert client.post(f"/api/products/undo/{token}").status_code == 200
    second = client.post(f"/api/products/undo/{token}")
    assert second.status_code == 410 and "нельзя" in second.json()["detail"]
    assert client.post("/api/products/undo/nonsense").status_code == 410


def test_undo_expires(client, monkeypatch):
    a = _make("ready")
    token = client.post("/api/products/status", json={"ids": [a], "status": "draft"}).json()["undo"]
    real = undo.time.monotonic
    monkeypatch.setattr(undo.time, "monotonic", lambda: real() + undo.TTL + 1)
    assert client.post(f"/api/products/undo/{token}").status_code == 410
    assert _status(a) == "draft"


def test_undo_does_not_overwrite_a_later_edit_or_a_product_being_sent(client):
    """Отмена не затирает то, что случилось после действия: товар поправили или он уже уходит на Prom."""
    edited, sending, untouched = _make("ready"), _make("ready"), _make("ready")
    token = client.post("/api/products/status", json={"ids": [edited, sending, untouched], "status": "draft"}).json()["undo"]
    products.update(edited, {"name": "Поправили сразу после"})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'sending' WHERE id = ?", (sending,))
    assert client.post(f"/api/products/undo/{token}").json() == {"restored": 1}
    assert (_status(edited), _status(sending), _status(untouched)) == ("draft", "sending", "ready")


def test_nothing_changed_means_nothing_to_undo(client):
    never_sent = _make("draft")
    r = client.post("/api/products/status", json={"ids": [never_sent], "status": "synced"}).json()
    assert r["changed"] == 0 and r["undo"] is None


def test_undo_keep_best_duplicates_brings_hidden_back(client):
    best = products.create({"name": "Павербанк 10000", "barcode": "4820000000017", "price": 900, "cost_price": 500})
    other = products.create({"name": "Павербанк 10000 копія", "barcode": "4820000000017", "price": 900, "cost_price": 700})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'ready' WHERE id = ?", (other,))
    r = client.post("/api/products/duplicates/keep-best").json()
    assert r["hidden"] == 1 and "before" not in r
    assert db.query_one("SELECT dup_of FROM products WHERE id = ?", (other,))["dup_of"] == best
    assert client.post(f"/api/products/undo/{r['undo']}").json() == {"restored": 1}
    row = db.query_one("SELECT status, dup_of FROM products WHERE id = ?", (other,))
    assert (row["status"], row["dup_of"]) == ("ready", None)


def test_undo_promote_ready_drafts(client, monkeypatch):
    monkeypatch.setattr(products, "ready_drafts", lambda flt=None: [a, b])
    a, b = _make("draft"), _make("draft")
    r = client.post("/api/products/promote-ready", json={"filter": {}}).json()
    assert r["moved"] == 2 and _status(a) == "ready"
    client.post(f"/api/products/undo/{r['undo']}")
    assert (_status(a), _status(b)) == ("draft", "draft")


# ---------- цена: закупка в валюте → наценка → курс (без округления) ----------

def test_price_follows_cost_markup_and_exchange_rate():
    pricing.save_rules([{"markup_percent": 50, "rounding": "none"}])
    pid = products.create({"name": "Кабель", "cost_price": 10, "cost_currency": "USD"})
    locked = products.create({"name": "Кабель свій", "cost_price": 10, "cost_currency": "USD"})
    products.update(locked, {"price": 777})  # цена вручную — 🔒
    assert products.get(pid)["price"] == 600  # 10 $ × 40 × 1.5

    rates.save_settings({"mode": "manual", "manual": {"USD": "42"}})
    suppliers.recalc_prices()
    assert products.get(pid)["price"] == 630
    assert products.get(locked)["price"] == 777

    rates.save_settings({"mode": "nbu", "add": "2.5"})  # надбавка к курсу НБУ: 40 → 41
    suppliers.recalc_prices()
    assert products.get(pid)["price"] == pytest.approx(615)

    pricing.save_rules([])  # правило убрали — цена остаётся, какой была (не превращается в закупку)
    suppliers.recalc_prices()
    assert products.get(pid)["price"] == pytest.approx(615)


def test_rate_change_does_not_touch_hryvnia_products():
    pricing.save_rules([{"markup_percent": 20, "rounding": "none"}])
    uah = products.create({"name": "Чашка", "cost_price": 100})
    assert products.get(uah)["price"] == 120
    rates.save_settings({"mode": "manual", "manual": {"USD": "50"}})
    assert suppliers.recalc_prices()["changed"] == 0
    assert products.get(uah)["price"] == 120


# ---------- статусы товара ----------

STATES = [("draft", False), ("ready", False), ("synced", True), ("error", True), ("sending", True), ("deleting", True)]


@pytest.mark.parametrize("start, synced", STATES)
@pytest.mark.parametrize("target", ["draft", "ready", "synced"])
def test_manual_status_change_rules(start, synced, target):
    """Вручную: товар, который прямо сейчас отправляется или удаляется, не трогается никогда; «На Prom» — только
    тому, что уже выгружался; остальное — как попросили."""
    pid = _make(start, synced=synced)
    products.set_status([pid], target)
    if start in ("sending", "deleting"):
        expected = start
    elif target == "synced":
        expected = "synced" if synced else start
    else:
        expected = target
    assert _status(pid) == expected


@pytest.mark.parametrize("start, after_edit", [("draft", "draft"), ("ready", "ready"), ("synced", "ready"),
                                               ("error", "ready")])
def test_editing_a_product_moves_it_back_to_the_queue_only_if_it_was_sent(start, after_edit):
    pid = _make(start, synced=start in ("synced", "error"))
    products.update(pid, {"name": "Новое название"})
    assert _status(pid) == after_edit
    products.update(pid, {"name": "Новое название"})  # то же значение — не изменение
    assert _status(pid) == after_edit


def test_unknown_manual_status_is_refused():
    pid = _make("draft")
    with pytest.raises(products.ProductError):
        products.set_status([pid], "sending")
    assert _status(pid) == "draft"


# ---------- прайс поставщика: повтор, «рваный», пустой ----------

def _state():
    return {sku: (p["price"], p["cost_price"], p["presence"], p["status"]) for sku, p in products_by_sku().items()}


def test_same_price_list_twice_changes_nothing():
    pricing.save_rules([{"markup_percent": 30, "rounding": "none"}])
    sid = make_supplier(make_yml(BASE))
    run(sid)
    before, journal = _state(), db.query_one("SELECT COUNT(*) AS n FROM product_changes")["n"]
    r = run(sid)
    assert r["status"] == "ok" and r["stats"]["created"] == 0 and r["stats"]["updated"] == 0
    assert _state() == before
    assert db.query_one("SELECT COUNT(*) AS n FROM product_changes")["n"] == journal  # в журнале — ни строчки


def test_broken_rows_do_not_take_good_rows_down():
    pricing.save_rules([{"markup_percent": 30, "rounding": "none"}])
    sid = make_supplier(make_yml(BASE))
    run(sid)
    ragged = [("A1", "Кружка белая", "не число", "10", "true"),  # цена не читается — строка не ломает остальные
              ("A2", "Кружка синяя", "160", "10", "true"), ("B1", "Тарелка", "80", "20", "false")]
    suppliers.store_source(sid, make_yml(ragged), "feed.xml")
    r = run(sid)
    assert r["status"] == "ok", r["message"]
    items = products_by_sku()
    assert items["A2"]["cost_price"] == 160
    assert items["A1"]["cost_price"] == 100 and items["A1"]["presence"] == "available"  # прежняя цена, не «нет в наличии»
    assert r["stats"]["missing"] == 0 and "оставлен как был" in r["stats"]["error_samples"][0]


def test_empty_price_list_does_not_wipe_the_catalogue():
    sid = make_supplier(make_yml(BASE))
    run(sid)
    before = _state()
    suppliers.store_source(sid, make_yml([]), "feed.xml")
    r = run(sid)
    assert r["status"] == "failed" and "ни одного годного" in r["message"]
    assert _state() == before  # защита от битого прайса и у маленького поставщика: ничего не снято с продажи
    assert run(sid, force=True)["stats"]["missing"] == 3  # «Обновить принудительно» — если товары правда убрали


def test_gone_and_back_keeps_manual_edits():
    sid = make_supplier(make_yml(BASE))
    run(sid)
    a1 = products_by_sku()["A1"]["id"]
    products.update(a1, {"name": "Своё название"})
    suppliers.store_source(sid, make_yml(BASE[1:] + [("C1", "Чашка", "60", "10", "true")]), "feed.xml")
    run(sid)
    assert products.get(a1)["presence"] == "not_available"
    suppliers.store_source(sid, make_yml(BASE + [("C1", "Чашка", "60", "10", "true")]), "feed.xml")
    assert run(sid)["stats"]["returned"] == 1
    p = products.get(a1)
    assert p["presence"] == "available" and p["name"] == "Своё название"


# ---------- логотип магазина ----------

def _png(size=(600, 300), mode="RGBA"):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 0) if mode == "RGBA" else "red").save(buf, "PNG")
    return buf.getvalue()


def test_logo_upload_is_shrunk_kept_transparent_and_shown_without_login(client, anon):
    from io import BytesIO

    from PIL import Image
    assert anon.get("/shop-logo.png").status_code == 404
    r = client.post("/api/settings/logo", files={"file": ("logo.png", _png(), "image/png")})
    assert r.status_code == 200
    version = r.json()["version"]
    assert client.get("/api/meta").json()["shop_logo"] == version
    got = anon.get(f"/shop-logo.png?v={version}")  # страница входа показывает его до входа
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    img = Image.open(BytesIO(got.content))
    assert max(img.size) == 256 and img.mode == "RGBA"  # уменьшен, прозрачность цела


def test_logo_rejects_svg_and_garbage(client):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    for name, content in (("logo.svg", svg), ("logo.png", b"not an image")):
        r = client.post("/api/settings/logo", files={"file": (name, content, "image/png")})
        assert r.status_code == 400 and "картинку" in r.json()["detail"]
    assert client.get("/api/meta").json()["shop_logo"] == ""


def test_logo_replace_and_remove(client):
    v1 = client.post("/api/settings/logo", files={"file": ("a.png", _png((100, 100)), "image/png")}).json()["version"]
    v2 = client.post("/api/settings/logo", files={"file": ("b.png", _png((50, 80), "RGB"), "image/png")}).json()["version"]
    assert v1 != v2  # новый адрес ?v= — браузер не покажет старый из кеша
    assert client.delete("/api/settings/logo").json()["ok"] is True
    assert client.get("/shop-logo.png").status_code == 404 and client.get("/api/meta").json()["shop_logo"] == ""
