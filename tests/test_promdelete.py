import asyncio
import json

import httpx

from promloader import changes, db, products, promcatalog, promdelete, suppliers, sync
from promloader.prom_api import PromClient

from .test_suppliers import BASE, make_supplier, make_yml, run


class FakeProm:
    """Prom: каталог товаров {prom_id: external_id}; удаление через /products/edit."""

    def __init__(self, items=None, down=False):
        self.items = dict(items or {})
        self.down = down
        self.calls = []

    def handler(self, request):
        path = request.url.path
        self.calls.append(path)
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        if path.endswith("/products/edit"):
            processed, errors = [], {}
            for item in json.loads(request.content):
                if item["id"] in self.items:
                    del self.items[item["id"]]
                    processed.append(item["id"])
                else:
                    errors[str(item["id"])] = {"id": "Продукт не найден"}
            return httpx.Response(200, json={"processed_ids": processed, "errors": errors, "warnings": {}})
        if "/products/by_external_id/" in path:
            ext = path.rsplit("/", 1)[1]
            for prom_id, e in self.items.items():
                if e == ext:
                    return httpx.Response(200, json={"product": {"id": prom_id, "external_id": e, "status": "on_display"}})
            return httpx.Response(404, json={"error": "not found"})
        if path.endswith("/products/list"):
            last = int(request.url.params.get("last_id") or 0)
            rest = [{"id": i, "external_id": e, "name": f"Товар {e}", "price": 10, "status": "on_display"}
                    for i, e in sorted(self.items.items()) if i > last]
            return httpx.Response(200, json={"products": rest[:100]})
        return httpx.Response(404, json={})

    def factory(self):
        return PromClient("tkn", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


def on_prom(name, ext, prom_id=None):
    pid = products.create({"name": name, "price": 100, "external_id": ext})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?, prom_id = ?, pending_fields = '[]' WHERE id = ?",
                  (db.now(), prom_id, pid))
    return pid


def exists(pid):
    return db.query_one("SELECT status, last_error, delete_attempts FROM products WHERE id = ?", (pid,))


def process(prom):
    return asyncio.run(promdelete.process(prom.factory))


def test_not_on_prom_deleted_right_away(client):
    pid = products.create({"name": "Чернетка", "price": 1})
    assert client.post("/api/products/delete/check", json={"ids": [pid]}).json()["on_prom"] == 0
    r = client.post("/api/products/delete", json={"ids": [pid]}).json()
    assert r["deleted"] == 1 and r["deleting"] == 0 and exists(pid) is None


def test_deleted_on_prom_then_in_program(client):
    a = on_prom("Гачок", "G-1", prom_id=501)
    b = on_prom("Ліхтар", "L-2")                 # выгружен импортом — id на Prom неизвестен
    prom = FakeProm({501: "G-1", 502: "L-2", 503: "OTHER"})
    r = client.post("/api/products/delete", json={"ids": [a, b]}).json()
    assert r["deleting"] == 2 and exists(a)["status"] == "deleting"
    assert client.get("/api/products", params={"status": "deleting"}).json()["total"] == 2
    process(prom)
    assert exists(a) is None and exists(b) is None
    assert prom.items == {503: "OTHER"}         # чужой товар не тронут
    log = changes.search(period="all", field="deleted")["items"]
    assert all("удалён на Prom" in x["new"] and x["source"] == "Prom" for x in log)


def test_missing_on_prom_counts_as_deleted():
    a = on_prom("Гачок", "G-1", prom_id=501)    # уже удалили в кабинете
    b = on_prom("Ліхтар", "L-2")
    promdelete.request([a, b])
    prom = FakeProm({})
    process(prom)
    assert exists(a) is None and exists(b) is None


def test_prom_down_retries_and_can_be_cancelled(client):
    a = on_prom("Гачок", "G-1", prom_id=501)
    promdelete.request([a])
    prom = FakeProm({501: "G-1"}, down=True)
    process(prom)
    row = exists(a)
    assert row["status"] == "deleting" and row["delete_attempts"] == 1 and "повторит сама" in row["last_error"]
    calls = len(prom.calls)
    process(prom)
    assert len(prom.calls) == calls              # ждёт паузу, не долбит Prom
    prom.down = False
    client.post("/api/products/delete/retry", json={"ids": [a]})
    process(prom)
    assert exists(a) is None

    b = on_prom("Ліхтар", "L-2", prom_id=502)
    promdelete.request([b])
    assert client.post("/api/products/delete/cancel", json={"ids": [b]}).json()["count"] == 1
    assert exists(b)["status"] == "synced"


def test_sending_product_not_deleted_and_deleting_not_sent():
    a = on_prom("Гачок", "G-1", prom_id=501)
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'sending' WHERE id = ?", (a,))
    r = promdelete.request([a])
    assert r["rejected"] and exists(a)["status"] == "sending"
    b = on_prom("Ліхтар", "L-2", prom_id=502)
    promdelete.request([b])
    res = sync.enqueue([b])
    assert res["accepted"] == 0 and "Удаляется" in res["rejected"][0]["reasons"][0]


def test_keep_on_prom_only_local():
    a = on_prom("Гачок", "G-1", prom_id=501)
    r = promdelete.request([a], from_prom=False)
    assert r == {"deleted": 1, "deleting": 0, "kept_on_prom": 1, "rejected": []}
    assert "на Prom оставлен" in changes.search(period="all", field="deleted")["items"][0]["new"]


def test_supplier_does_not_recreate_deleted_product(client):
    sid = make_supplier(make_yml(BASE))
    total = run(sid)["stats"]["created"]
    pid = db.query_one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
    promdelete.request([pid])                     # не на Prom — удаляется сразу
    stats = run(sid, force=True)["stats"]
    assert stats.get("ignored") == 1 and stats["created"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM products")["n"] == total - 1
    assert client.get(f"/api/suppliers/{sid}").json()["items_deleted"] == 1
    assert client.post(f"/api/suppliers/{sid}/restore-deleted").json()["count"] == 1
    assert run(sid, force=True)["stats"]["created"] == 1


def test_catalog_marks_products_missing_on_prom():
    gone = on_prom("Видалений в кабінеті", "GONE-1", prom_id=777)
    found = on_prom("Є на Prom", "HERE-1")         # id на Prom неизвестен, но товар там есть
    prom = FakeProm({900: "HERE-1"})
    counts = asyncio.run(promcatalog.run(prom.factory))
    assert counts["missing_on_prom"] == 1
    g = db.query_one("SELECT * FROM products WHERE id = ?", (gone,))
    assert g["status"] == "error" and g["synced_at"] is None and "нет на prom" in g["last_error"].lower()
    f = db.query_one("SELECT * FROM products WHERE id = ?", (found,))
    assert f["status"] == "synced" and f["prom_id"] == 900


def test_status_buttons_do_not_interrupt_deletion():
    a = on_prom("Гачок", "G-1", prom_id=501)
    promdelete.request([a])
    products.set_status([a], "ready")
    products.set_status([a], "synced")
    assert exists(a)["status"] == "deleting"
