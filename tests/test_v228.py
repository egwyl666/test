"""2.28.0: большой каталог (быстрые фильтры), конвейер черновиков, остатки и закупка."""

import itertools
from datetime import date, datetime

import pytest

from promloader import db, notify, orders, products, stock, suppliers

from .test_novaposhta import prom_order

NAMES = ["", "   ", "\n\t", "Кружка", " Кружка ", "К" * 255, "К" * 256, "a" * 256]
PRICES = [None, 0, -1, 0.5, 0.99, 1, 100]
CURRENCIES = ["UAH", "USD", "грн", "ГРН", "", "EUR", " uah"]


def test_errors_filter_in_sql_matches_card_validation():
    """Фильтр «с ошибками» считается в SQL (ERRORS_SQL), а карточка — в Python (validate). Они обязаны совпадать на
    любых значениях, иначе список покажет «ошибок нет», а отправка откажет."""
    cases = list(itertools.product(NAMES, PRICES, CURRENCIES))
    now = db.now()
    with db.tx() as c:
        for n, (name, price, cur) in enumerate(cases):
            c.execute("INSERT INTO products (external_id, name, price, currency, status, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, 'draft', ?, ?)", (f"E{n}", name, price, cur, now, now))
    sql = products.ERRORS_SQL.format(min_price=products.MIN_PRICE)
    rows = db.query(f"SELECT p.id, p.name, p.price, p.currency, {sql} AS bad FROM products p")
    mismatched = [(r["name"], r["price"], r["currency"]) for r in rows
                  if bool(r["bad"]) != bool(products.validate(dict(r), image_count=1)["errors"])]
    assert mismatched == []
    expected = sum(1 for r in rows if r["bad"])
    assert products.list_products({"errors": "1"}, limit=10_000)["total"] == expected
    assert len(products.ids_for_filter({"errors": "1"})) == expected


@pytest.mark.parametrize("flt", [{"errors": "1"}, {"errors": "1", "status": "draft"}, {"errors": "1", "q": "Кру"}])
def test_errors_filter_pages_without_gaps(flt):
    for n in range(30):
        products.create({"name": f"Кружка {n}", "price": 100})
    with db.tx() as c:
        c.execute("UPDATE products SET price = NULL WHERE id % 2 = 0")
    total = products.list_products(flt, limit=7)["total"]
    seen = []
    for offset in range(0, total, 7):
        seen += [p["id"] for p in products.list_products(flt, limit=7, offset=offset)["items"]]
    assert len(seen) == len(set(seen)) == total == 15


# ---------- остатки и закупка ----------

def _supplier_with_products():
    sid = suppliers.create("Опт")
    ids = {}
    for code, qty in (("A", 10), ("B", 1), ("C", None), ("D", 0)):
        pid = products.create({"name": f"Товар {code}", "price": 100, "external_id": f"T-{code}", "quantity": qty,
                               "presence": "available" if qty != 0 else "not_available"})
        with db.tx() as c:
            c.execute("UPDATE products SET supplier_id = ? WHERE id = ?", (sid, pid))
            c.execute("INSERT INTO supplier_items (supplier_id, sku, product_id, data, images_hash, missing, seen_at, seen_run) "
                      "VALUES (?, ?, ?, '{}', '', 0, ?, 1)", (sid, f"SUP-{code}", pid, db.now()))
        ids[code] = pid
    return sid, ids


def _order(oid, items, status="delivered", when="2026-10-15T10:00:00"):
    o = prom_order(oid, status=status)
    o["date_created"] = when
    o["products"] = [{"id": None, "external_id": code, "name": code, "quantity": q, "price": "100 грн"} for code, q in items]
    return o


def test_low_stock_filter_alert_and_threshold(client):
    _, ids = _supplier_with_products()
    assert stock.threshold() == 3
    low = {p["id"] for p in products.list_products({"low_stock": "1"})["items"]}
    assert low == {ids["B"]}  # 10 — много, неизвестно — не «мало», 0 и «нет в наличии» — уже не «заканчивается»
    assert client.get("/api/products/alerts").json()["low_stock"] == 1
    assert client.post("/api/products/low-stock-threshold", json={"value": "10"}).json() == {"threshold": 10}
    assert client.get("/api/products/alerts").json()["low_stock"] == 2
    assert client.post("/api/products/low-stock-threshold", json={"value": "-1"}).status_code == 400


def test_purchase_counts_sales_without_cancellations_and_subtracts_stock(client):
    sid, ids = _supplier_with_products()
    orders.store([_order(1, [("T-A", 12), ("T-B", 3)]), _order(2, [("T-C", 2)]),
                  _order(3, [("T-A", 100)], status="canceled"),  # отмена — не продажа
                  _order(4, [("T-A", 50)], when="2026-08-01T10:00:00")], notify_new=False)  # давно — вне периода
    r = stock.purchase(sid, days=30, cover=15, today=date(2026, 10, 20))
    rows = {x["supplier_sku"]: x for x in r["rows"]}
    assert rows["SUP-A"]["sold"] == 12 and rows["SUP-A"]["order"] == 0      # 12/30×15 = 6 − остаток 10 → не нужно
    assert rows["SUP-B"]["sold"] == 3 and rows["SUP-B"]["order"] == 1       # 1.5 − 1 → 1 (вверх)
    assert rows["SUP-C"]["order"] == 1 and rows["SUP-C"]["stock_known"] is False  # остаток неизвестен — как 0
    assert "SUP-D" not in rows  # не продавался и не «заканчивается»
    assert r["total_units"] == 2
    x = client.get(f"/api/suppliers/{sid}/purchase.xlsx?days=30&cover=15")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get(f"/api/suppliers/{sid}/purchase?days=0").status_code == 400
    assert client.get("/api/suppliers/99999/purchase").status_code == 404


def test_low_stock_telegram_once_per_day_and_again_after_restock(monkeypatch):
    _, ids = _supplier_with_products()
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    monkeypatch.setattr(notify, "recipients", lambda: [{"events": ["low_stock"]}])
    assert stock.maybe_notify(datetime(2026, 10, 20, 9)) == 1
    assert sent[0][0] == "low_stock" and "Товар B" in sent[0][1]
    assert stock.maybe_notify(datetime(2026, 10, 20, 18)) == 0  # в тот же день — нет
    assert stock.maybe_notify(datetime(2026, 10, 21, 9)) == 0  # уже сообщили — не повторяем
    products.update(ids["B"], {"quantity": 20})  # пополнили
    stock.maybe_notify(datetime(2026, 10, 22, 9))
    products.update(ids["B"], {"quantity": 2})  # снова мало
    assert stock.maybe_notify(datetime(2026, 10, 23, 9)) == 1 and len(sent) == 2
