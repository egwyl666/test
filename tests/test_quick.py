import asyncio
import json

import httpx

from promloader import db, products, sync
from promloader.prom_api import PromClient


def synced(**extra):
    pid = products.create({"name": "Кружка", "price": 100, "quantity": 5, **extra})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?, pending_fields = '[]' WHERE id = ?",
                  (db.now(), pid))
    return pid


class Prom:
    def __init__(self, edit=(200, {"processed_ids": [1]})):
        self.edit = edit
        self.calls = []

    def handler(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/products/edit_by_external_id"):
            code, body = self.edit
            return httpx.Response(code, json=body)
        if path.endswith("/products/import_file"):
            return httpx.Response(200, json={"id": "imp-1"})
        return httpx.Response(200, json={"status": "SUCCESS"})

    def run(self):
        factory = lambda: PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))  # noqa: E731
        return asyncio.run(sync.run_once(factory))

    def paths(self):
        return [c.url.path.rsplit("/", 1)[-1] for c in self.calls]


def kinds():
    return [j["kind"] for j in sync.list_jobs()]


def test_pending_fields_tracking():
    pid = synced()
    products.update(pid, {"price": 120}, lock=False)
    assert json.loads(products.get(pid)["pending_fields"]) == ["price"]
    products.add_image_url(pid, "https://x.example.com/1.jpg")
    assert json.loads(products.get(pid)["pending_fields"]) == ["images", "price"]


def test_price_only_goes_quick():
    pid = synced()
    products.update(pid, {"price": 120, "presence": "order"}, lock=False)
    sync.enqueue([pid])
    assert kinds() == ["quick"]
    prom = Prom()
    prom.run()
    assert prom.paths() == ["edit_by_external_id"]
    body = json.loads(prom.calls[0].content)
    p = products.get(pid)
    assert body == [{"id": p["external_id"], "price": 120.0, "presence": "order", "quantity_in_stock": 5}]
    assert p["status"] == "synced" and p["pending_fields"] == "[]"
    assert sync.list_jobs()[0]["result"]["mode"] == "quick"


def test_name_change_goes_full_import_and_mixed_split():
    a, b = synced(), synced()
    products.update(a, {"price": 90}, lock=False)
    products.update(b, {"name": "Кружка большая"}, lock=False)
    sync.enqueue([a, b])
    assert sorted(kinds()) == ["import", "quick"]


def test_never_synced_product_is_full_import():
    pid = products.create({"name": "Новая", "price": 10})
    products.update(pid, {"price": 11})
    sync.enqueue([pid])
    assert kinds() == ["import"]


def test_rejected_format_falls_back_to_import():
    pid = synced()
    products.update(pid, {"price": 120}, lock=False)
    sync.enqueue([pid])
    prom = Prom(edit=(400, {"error": "bad request"}))
    prom.run()
    assert db.get_setting("quick_updates") == "0"
    assert kinds() == ["import"] and sync.list_jobs()[0]["status"] == "pending"
    prom.run()
    assert prom.paths()[-1] == "import_file"
    # быстрый способ больше не пробуется
    products.update(pid, {"price": 130}, lock=False)
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced' WHERE id = ?", (pid,))
    assert sync._quick_ok(pid) is False


def test_per_product_error():
    pid = synced()
    products.update(pid, {"price": 120}, lock=False)
    ext = products.get(pid)["external_id"]
    sync.enqueue([pid])
    Prom(edit=(200, {"processed_ids": [], "errors": {ext: "Товар не найден"}})).run()
    p = products.get(pid)
    assert p["status"] == "error" and p["last_error"] == "Товар не найден"


def test_photos_and_price_together_is_full_import():
    from promloader import suppliers
    pid = synced()
    with db.tx() as c:
        suppliers._replace_images(c, pid, ["https://new.example.com/1.jpg"], [], [])
        products._touch(c, pid, {"price": 150.0}, "synced")
    assert sorted(json.loads(products.get(pid)["pending_fields"])) == ["images", "price"]
    sync.enqueue([pid])
    assert kinds() == ["import"]


def test_auth_error_does_not_disable_quick_mode():
    pid = synced()
    products.update(pid, {"price": 120}, lock=False)
    sync.enqueue([pid])
    Prom(edit=(401, {"error": "unauthorized"})).run()
    assert db.get_setting("quick_updates") != "0"
    assert sync.list_jobs()[0]["status"] == "failed"


def test_unprocessed_products_requeued_as_import():
    a, b = synced(), synced()
    for pid in (a, b):
        products.update(pid, {"price": 120}, lock=False)
    sync.enqueue([a, b])
    Prom(edit=(200, {"processed_ids": [1]})).run()  # Prom обработал только один из двух
    jobs = sync.list_jobs()
    assert jobs[0]["kind"] == "import" and jobs[0]["status"] == "pending" and jobs[0]["count"] == 2
    assert jobs[1]["result"]["requeued"] == 2


def test_migration_marks_unsent_products(tmp_path):
    import sqlite3
    pid = synced()
    products.update(pid, {"name": "правка до обновления программы"}, lock=False)
    conn = db._conn
    conn.execute("ALTER TABLE products DROP COLUMN pending_fields")
    db._migrate(conn)
    assert products.get(pid)["pending_fields"] == '["*"]'
    products.update(pid, {"price": 1}, lock=False)
    assert sync._quick_ok(pid) is False
