"""Свойства, которые должны выполняться всегда, а не в одном придуманном примере: права на каждом адресе для каждой
роли, база старой версии, правки одновременно из нескольких потоков, откат журнала по каждому полю, поиск с
символами SQL, копия «туда и обратно» без потерь."""

import hashlib
import re
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient

from promloader import backup, changes, db, orders, products, users
from promloader.main import PUBLIC_PATHS, all_routes, app

from .conftest import sign_in
from .test_api_robustness import SKIP, offline  # noqa: F401 — фикстура «без сети»
from .test_novaposhta import prom_order

# ---------- права: каждый адрес × каждая роль ----------

ROLES = ["viewer", "operator", "manager"]


@pytest.mark.parametrize("role", ROLES)
def test_every_route_enforces_role_permissions(role, data_dir, offline):  # noqa: F811
    """Каждый адрес API для каждой роли: если право роли не позволяет — 403 и ничего не меняется; если позволяет —
    не 500. Ловит новый обработчик, который проскочил мимо таблицы прав или сломался для «неадминов»."""
    pid = products.create({"name": "Кружка", "price": 10, "external_id": "R-1"})
    orders.store([prom_order(1001)], notify_new=False)
    users.create("boss", "admin-pass-1", "admin")
    c = sign_in(TestClient(app), f"u-{role}", "пароль-1234", role)
    perms = set(users.get(users.authenticate(f"u-{role}", "пароль-1234")["id"])["perms"])
    values = {"product_id": pid, "order_id": 1001, "tab": "sales", "room_ident": "555_x", "user_id": 1, "role_id": "viewer"}
    before = db.query_one("SELECT price, name, status FROM products WHERE id = ?", (pid,))
    wrong = []
    for route in all_routes():
        path = getattr(route, "path", "")
        if not path.startswith("/api") or SKIP.search(path) or path in PUBLIC_PATHS:  # вход и сброс — для всех
            continue
        url = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: str(values.get(m.group(1), 1)), path)
        for method in getattr(route, "methods", None) or ():
            need = users.required(method, path if "{" not in path else url)
            allowed = need is None or all(p in perms for p in ((need,) if isinstance(need, str) else need))
            r = c.request(method, url, json={} if method not in ("GET", "DELETE") else None)
            if not allowed and r.status_code != 403:
                wrong.append(f"{method} {url}: {r.status_code}, а нужно 403 (право {need})")
            if allowed and r.status_code >= 500 and r.status_code != 502:
                wrong.append(f"{method} {url}: {r.status_code}")
    assert wrong == []
    assert dict(db.query_one("SELECT price, name, status FROM products WHERE id = ?", (pid,))) == dict(before) \
        or "catalog" in perms


# ---------- база старой версии ----------

def test_database_without_any_later_columns_upgrades_and_keeps_data(data_dir):
    """База, где нет ни одной колонки из MIGRATIONS (как у самых первых версий): программа запускается, колонки
    появляются с правильными значениями по умолчанию, данные целы — и повторный запуск ничего не меняет."""
    pid = products.create({"name": "Стара кружка", "price": 99, "external_id": "OLD-1"})
    orders.store([prom_order(5001)], notify_new=False)
    dropped = []
    with db.tx() as c:
        for table, cols in db.MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col in have:
                    try:
                        c.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                        dropped.append((table, col))
                    except Exception:
                        pass  # колонка в индексе — удалить нельзя, и у старой базы её всё равно не было бы в индексе
    assert len(dropped) > 20
    for _ in range(3):
        db.init(data_dir)
    for table, col in dropped:
        assert col in {r["name"] for r in db.query(f"PRAGMA table_info({table})")}, (table, col)
    p = products.get(pid)
    assert p["name"] == "Стара кружка" and p["price"] == 99 and p["dup_of"] is None and p["locked_fields"] == []
    assert db.query_one("SELECT prepay_state, packed_at FROM orders WHERE id = 5001")["prepay_state"] == ""


# ---------- правки из нескольких потоков ----------

def test_parallel_edits_do_not_lose_updates_or_crash(data_dir):
    """Автосохранение карточки, массовое изменение, поставщик и ИИ пишут одновременно: ни одна правка не теряется,
    «database is locked» не вылетает, журнал совпадает с итогом."""
    ids = [products.create({"name": f"Товар {n}", "price": 100, "external_id": f"P-{n}"}) for n in range(8)]
    errors = []

    def worker(n):
        try:
            for k in range(25):
                pid = ids[(n + k) % len(ids)]
                with changes.user(f"потік {n}"):
                    products.update(pid, {"quantity": n * 1000 + k})
        except Exception as exc:  # pragma: no cover — сам факт исключения и есть провал
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    for pid in ids:
        last = db.query_one("SELECT new FROM product_changes WHERE product_id = ? AND field = 'quantity' "
                            "ORDER BY id DESC LIMIT 1", (pid,))
        assert str(products.get(pid)["quantity"]) == last["new"]  # журнал и товар согласны
    assert db.query_one("SELECT COUNT(*) AS n FROM product_changes WHERE field = 'quantity'")["n"] == 200


# ---------- откат журнала по каждому полю ----------

SAMPLES = {
    "price": (100, 149.99), "old_price": (200, 0.5 + 249), "cost_price": (60, 61.25), "cost_currency": ("USD", "EUR"),
    "rrp": (180, 175), "currency": ("UAH", "USD"), "presence": ("available", "not_available"), "quantity": (5, 0),
    "name": ("Кружка «Ранок»", "Кружка 'Ніч' & <b>"), "name_ua": ("Кухоль", "Кухоль 😀"),
    "description": ("<p>Опис</p>", "<p>Новий\nопис</p>"), "description_ua": ("<p>A</p>", "<p>B</p>"),
    "group_name": ("Посуд", "Кухня / Посуд"), "keywords": ("кружка, чашка", "кухоль"), "vendor": ("ACME", "Ёлка"),
    "country": ("Україна", "Польща"), "barcode": ("4820000000017", "4820000000024"), "vendor_code": ("AB-1", "AB_2"),
    "unit": ("шт.", "компл."),
}


@pytest.mark.parametrize("field", sorted(changes.REVERTABLE - {"target_category_id"}))
def test_journal_revert_restores_every_field_exactly(field):
    assert field in SAMPLES, f"добавьте пример для нового откатываемого поля {field}"
    old, new = SAMPLES[field]
    pid = products.create({"name": "Товар", "price": 100, "external_id": f"RV-{field}", field: old})
    before = products.get(pid)[field]
    products.update(pid, {field: new})
    assert products.get(pid)[field] != before
    row = db.query_one("SELECT id FROM product_changes WHERE product_id = ? AND field = ? ORDER BY id DESC LIMIT 1",
                       (pid, field))
    changes.revert(row["id"])
    assert products.get(pid)[field] == before


# ---------- поиск с символами SQL ----------

@pytest.mark.parametrize("q", ["%", "_", "\\", "'", '"', "100%", "a_b", "'; DROP TABLE products; --", "[", "*"])
def test_search_treats_sql_characters_literally(client, q):
    products.create({"name": "Кружка біла", "price": 10, "external_id": "S-1"})
    products.create({"name": "Знижка 100% на a_b", "price": 10, "external_id": "S-2"})
    found = {p["external_id"] for p in client.get("/api/products", params={"q": q}).json()["items"]}
    expected = {"S-2"} if q in ("%", "_", "100%", "a_b") else set()
    assert found == expected
    assert client.get("/api/orders", params={"q": q}).status_code == 200
    assert client.get("/api/changes", params={"q": q}).status_code == 200
    assert db.query_one("SELECT COUNT(*) AS n FROM products")["n"] == 2


def test_search_ignores_case_for_ukrainian_letters(client):
    products.create({"name": "ЇЖАК ҐАНОК ЄНОТ", "price": 10, "external_id": "U-1"})
    for q in ("їжак", "ґанок", "єнот", "ЇжАк"):
        assert [p["external_id"] for p in client.get("/api/products", params={"q": q}).json()["items"]] == ["U-1"], q


# ---------- копия «туда и обратно» ----------

def _fingerprint() -> dict:
    out = {}
    for t in [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%'")]:
        rows = db.query(f"SELECT * FROM {t} ORDER BY 1")
        out[t] = hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()
    return out


def test_backup_restore_roundtrip_is_lossless(data_dir):
    """Копия и восстановление возвращают базу байт-в-байт по каждой таблице (кроме служебного: время копии)."""
    for n in range(5):
        pid = products.create({"name": f"Товар {n} 😀", "price": 10 + n, "external_id": f"B-{n}",
                               "params": [{"name": "Колір", "value": "чорний"}]})
        products.update(pid, {"price": 20 + n})
    orders.store([prom_order(7001), prom_order(7002, status="canceled")], notify_new=False)
    users.create("anna", "пароль-анни", "manager")
    users.create_role("Склад", ["orders"])
    before = _fingerprint()
    info = backup.create("manual")
    products.create({"name": "Після копії", "price": 1, "external_id": "AFTER"})
    with open(backup.path_of(info["name"]), "rb") as f:
        backup.stage_restore_stream(f)
    db._conn.close()
    db._conn = None
    assert backup.apply_pending(data_dir)
    db.init(data_dir)
    after = _fingerprint()
    skip = {"settings"}  # время последней копии и т. п. — меняются самой копией
    assert {k: v for k, v in after.items() if k not in skip} == {k: v for k, v in before.items() if k not in skip}


# ---------- постраничная выдача: каждая сортировка, каждый товар — ровно один раз ----------

@pytest.mark.parametrize("sort", sorted({*products.SORTS, *products.PROFIT_SORTS}))
def test_paging_returns_every_product_exactly_once(client, sort):
    """«Показать ещё» по страницам: при любой сортировке ни один товар не теряется и не повторяется — даже когда у
    многих одинаковые цена, статус и время изменения (без последнего ключа «по номеру» порядок между страницами
    у SQLite не гарантирован)."""
    for n in range(23):
        products.create({"name": f"Товар {n % 3}", "price": 100 if n % 2 else None, "cost_price": 50 if n % 4 else None,
                         "external_id": f"PG-{n}"})
    with db.tx() as c:
        c.execute("UPDATE products SET updated_at = '2026-10-01T00:00:00+00:00', created_at = '2026-10-01T00:00:00+00:00'")
    seen = []
    for offset in range(0, 30, 5):
        page = client.get("/api/products", params={"sort": sort, "limit": 5, "offset": offset}).json()["items"]
        seen += [p["id"] for p in page]
    assert sorted(seen) == sorted(r["id"] for r in db.query("SELECT id FROM products")) and len(seen) == len(set(seen))


def test_orders_paging_returns_every_order_exactly_once(client):
    orders.store([prom_order(9000 + n) for n in range(17)], notify_new=False)  # у всех одно и то же время
    seen = []
    for offset in range(0, 20, 4):
        seen += [o["id"] for o in client.get("/api/orders", params={"limit": 4, "offset": offset}).json()["items"]]
    assert sorted(seen) == list(range(9000, 9017))


# ---------- «кривые» заказы от Prom ----------

ODD_ORDERS = [
    {"id": 8001},  # почти пустой
    {"id": 8002, "client_first_name": None, "phone": None, "products": None, "delivery_option": None,
     "payment_option": None, "price": None, "date_created": None, "status": None},
    {"id": 8003, "client_first_name": "<script>alert(1)</script>", "client_last_name": "О'Брайен & Ко",
     "phone": "+38 (050) 111-22-33 доб. 5", "products": [{"name": None, "quantity": "2", "price": "1 299,50 грн"}, "мусор", 5],
     "delivery_option": "Укрпошта", "payment_option": 7, "price": "не число", "status": "weird_new_status",
     "date_created": "2026-13-45T99:99:99"},
    {"id": 8004, "products": [{"name_multilang": {"uk": "Кухоль"}, "quantity": None}], "delivery_provider_data": "nova_poshta",
     "cancellation": "строка вместо объекта", "cpa_commission": "12.5"},
]


def test_odd_prom_orders_never_break_pages_reports_or_notifications(client, monkeypatch):
    from promloader import notify, reports
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append(text))
    db.set_setting("orders_initialized", "1")
    orders.store(ODD_ORDERS)  # как новые: с уведомлениями и проверкой «рисковый покупатель»
    assert len(sent) == len(ODD_ORDERS)
    assert "<script>" not in "".join(sent)  # имя покупателя экранировано для Telegram (там HTML-разметка)
    items = client.get("/api/orders").json()["items"]
    assert {o["id"] for o in items} == {o["id"] for o in ODD_ORDERS}
    for oid in (8001, 8002, 8003, 8004):
        assert client.get("/api/orders/scan", params={"code": str(oid)}).status_code == 200
        assert client.get(f"/api/orders/{oid}/ttn/draft").status_code in (200, 400)
    for tab in reports.TABS:
        assert client.get(f"/api/reports/{tab}", params={"period": "30d"}).status_code == 200, tab
    assert client.get("/api/orders/summary").status_code == 200
    assert client.get("/api/orders/stats").status_code == 200
    assert isinstance(reports.daily_summary_text(date.today()), str)


# ---------- граница суток в отчётах ----------

@pytest.fixture
def kyiv_time(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("iso, day", [
    ("2026-10-20T23:59:59+03:00", 20),  # Prom отдаёт киевское время — заказ поздно вечером остаётся в своём дне
    ("2026-10-20T21:30:00Z", 21),  # 00:30 по Киеву — уже следующий день, а не «вчера по UTC»
    ("2026-10-25T03:30:00+02:00", 25),  # ночь перевода часов
    ("2026-10-20T12:00:00", 20),  # без пояса — как есть
])
def test_order_lands_on_the_local_day(kyiv_time, iso, day):
    from promloader import reports
    assert reports._local(iso).day == day


def test_period_edges_include_first_and_last_day():
    from promloader import reports
    first_of_month = date(2026, 10, 1)
    assert reports.period_bounds("month", today=first_of_month) == (first_of_month, first_of_month)
    assert reports.period_bounds("today", today=first_of_month) == (first_of_month, first_of_month)
    start, end = reports.period_bounds("7d", today=date(2026, 3, 2))
    assert (end - start).days == 6 and start == date(2026, 2, 24)
    # даты перепутаны местами — период тот же, а не пустой
    assert reports.period_bounds("custom", "2026-10-20", "2026-10-01") == (date(2026, 10, 1), date(2026, 10, 20))
