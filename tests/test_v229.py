"""2.29.0: выполненный заказ уменьшает остаток; дизайн (тема, логотипы, права)."""

import pytest

from promloader import db, orders, products, stock, sync

from .test_novaposhta import prom_order


def _order(oid, items, status="received"):
    o = prom_order(oid, status=status)
    o["products"] = [{"id": None, "external_id": code, "name": code, "quantity": q, "price": "100 грн"} for code, q in items]
    return o


@pytest.fixture
def shop(monkeypatch):
    """Товары с остатком; заказы магазина уже загружены (не первая загрузка)."""
    sent = []
    monkeypatch.setattr(sync, "enqueue", lambda ids: sent.append(list(ids)) or {"accepted": len(ids)})
    ids = {}
    for code, qty in (("A", 5), ("B", 1), ("C", None)):
        ids[code] = products.create({"name": f"Товар {code}", "price": 100, "external_id": code, "quantity": qty})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?", (db.now(),))
    orders.store([], notify_new=False, first_run=True)  # магазин подключён раньше
    db.set_setting("orders_initialized", "1")
    return ids, sent


def _qty(pid):
    r = db.query_one("SELECT quantity, presence, status FROM products WHERE id = ?", (pid,))
    return r["quantity"], r["presence"], r["status"]


def test_delivered_order_decreases_stock_once_and_goes_to_prom(shop):
    ids, sent = shop
    orders.store([_order(10, [("A", 2), ("B", 1), ("C", 3)])], notify_new=False)
    assert _qty(ids["A"])[0] == 5  # принят — ещё не продан
    orders.store([_order(10, [("A", 2), ("B", 1), ("C", 3)], status="delivered")], notify_new=False)
    assert _qty(ids["A"]) == (3, "available", "ready")
    assert _qty(ids["B"]) == (0, "not_available", "ready")  # кончился — «нет в наличии»
    assert _qty(ids["C"])[0] is None  # остаток не ведётся — не трогаем
    assert sorted(sent[-1]) == sorted([ids["A"], ids["B"]])  # сразу на Prom
    orders.store([_order(10, [("A", 2)], status="delivered")], notify_new=False)  # повторный опрос
    assert _qty(ids["A"])[0] == 3  # второй раз не списывает
    last = db.query_one("SELECT source FROM product_changes WHERE product_id = ? AND field = 'quantity' "
                        "ORDER BY id DESC LIMIT 1", (ids["A"],))
    assert last["source"] == "Заказ №10 выполнен"
    assert "quantity" not in products.get(ids["A"])["locked_fields"]  # не ручная правка — поставщик пришлёт своё


def test_first_connection_and_update_do_not_touch_old_orders(monkeypatch):
    monkeypatch.setattr(sync, "enqueue", lambda ids: {"accepted": 0})
    pid = products.create({"name": "Старый", "price": 100, "external_id": "OLD", "quantity": 4})
    orders.store([_order(1, [("OLD", 3)], status="delivered")], notify_new=False, first_run=True)  # история магазина
    assert _qty(pid)[0] == 4
    # обновление программы: выполненный ещё до новой версии заказ не списывается
    db.set_setting("stock_applied_since", "")
    with db.tx() as c:
        c.execute("UPDATE orders SET stock_applied_at = ''")
    stock.apply_delivered()
    assert _qty(pid)[0] == 4
    orders.store([_order(2, [("OLD", 1)], status="delivered")], notify_new=False, first_run=False)
    assert _qty(pid)[0] == 3


def test_odd_and_canceled_orders_do_not_change_stock(shop):
    ids, _ = shop
    orders.store([_order(20, [("A", 1)], status="canceled")], notify_new=False)
    odd = prom_order(21, status="delivered")
    odd["products"] = "не список"
    orders.store([odd], notify_new=False)
    assert _qty(ids["A"])[0] == 5


# ---------- «Сегодня», логотипы, цвет ----------

def _png(size, color, alpha=255):
    import io

    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", size, (*color, alpha)).save(b, "PNG")
    return b.getvalue()


def test_today_shows_only_what_needs_attention(client):
    r = client.get("/api/today").json()
    assert set(r["sales"]) == {"today", "yesterday", "7d", "month"} and r["attention"] == []
    pid = products.create({"name": "Кружка", "price": 100})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'error' WHERE id = ?", (pid,))
    orders.store([prom_order(5, status="received")], notify_new=False, first_run=False)
    keys = {a["key"]: a for a in client.get("/api/today").json()["attention"]}
    assert keys["sync_errors"]["count"] == 1 and keys["sync_errors"]["href"] == "/?status=error"
    assert "orders_unpacked" not in keys  # упаковку не отмечают — не шумим «не упакованы»
    with db.tx() as c:
        c.execute("UPDATE orders SET packed_at = 'x' WHERE id = 5")
    orders.store([prom_order(6, status="received")], notify_new=False)
    assert {a["key"]: a for a in client.get("/api/today").json()["attention"]}["orders_unpacked"]["count"] == 1


def test_brand_accent_from_logo_and_big_logo_for_login(client, anon):
    from promloader import branding
    v = client.post("/api/settings/logo", files={"file": ("l.png", _png((80, 80), (14, 116, 144)), "image/png")}).json()
    assert v["accent"].startswith("#") and v["accent"] != "#000000"
    grey = branding.accent_from(__import__("PIL.Image", fromlist=["Image"]).new("RGBA", (40, 40), (120, 120, 120, 255)))
    assert grey == ""  # серый логотип — цвет стандартный
    big = client.post("/api/settings/logo?slot=big",
                      files={"file": ("b.png", _png((2000, 1000), (200, 50, 50)), "image/png")}).json()
    assert big["big"]
    pub = anon.get("/api/brand").json()  # страница входа — без входа
    assert pub == {"logo": v["logo"], "big": big["big"], "accent": v["accent"]}
    img = anon.get("/shop-logo-big")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"  # без прозрачности — JPEG
    assert client.post("/api/settings/brand-accent", json={"on": False}).json()["accent"] == ""
    assert client.get("/api/meta").json()["brand_accent"] == ""
    assert client.post("/api/settings/logo?slot=nope", files={"file": ("l.png", b"x", "image/png")}).status_code == 400
    client.delete("/api/settings/logo?slot=big")
    assert anon.get("/shop-logo-big").status_code == 404
