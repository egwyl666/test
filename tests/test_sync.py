import asyncio
import json

import httpx

from promloader import db, products, sync
from promloader.prom_api import PromClient

from .conftest import make_image


class FakeProm:
    """Имитация Prom: список ответов на import_file и import/status."""

    def __init__(self, upload=None, statuses=None):
        self.upload = list(upload or [(200, {"id": "imp-1"})])
        self.statuses = list(statuses or [(200, {"status": "SUCCESS", "imported": 1})])
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer tkn"
        if request.url.path.endswith("/products/import_file"):
            code, body = self.upload.pop(0) if len(self.upload) > 1 else self.upload[0]
        elif "/products/import/status/" in request.url.path:
            code, body = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        else:
            code, body = 404, {"error": "unknown"}
        if isinstance(body, Exception):
            raise body
        return httpx.Response(code, json=body)

    def factory(self):
        return lambda: PromClient("tkn", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


def run(fake):
    return asyncio.run(sync.run_once(fake.factory()))


def make_due():
    """Промотать время: все задачи — «пора выполнять»."""
    with db.tx() as c:
        c.execute("UPDATE sync_jobs SET next_run_at = '2000-01-01T00:00:00+00:00'")


def ready_product(**extra):
    return products.create({"name": "Кружка", "price": 100, **extra})


def job():
    return sync.list_jobs()[0]


def test_happy_path():
    pid = ready_product()
    res = sync.enqueue([pid])
    assert res["accepted"] == 1 and not res["rejected"]
    assert products.get(pid)["status"] == "sending"

    fake = FakeProm(statuses=[(200, {"status": "PROCESSING"}), (200, {"status": "SUCCESS", "imported": 1})])
    run(fake)
    assert job()["status"] == "waiting" and job()["import_id"] == "imp-1"
    upload = fake.requests[0]
    assert b"products.xml" in upload.content and b"mark_missing_product_as" in upload.content

    make_due()
    run(fake)
    assert job()["status"] == "waiting"  # Prom ещё обрабатывает

    make_due()
    run(fake)
    assert job()["status"] == "done"
    p = products.get(pid)
    assert p["status"] == "synced" and p["synced_at"]


def test_invalid_products_are_rejected():
    bad = products.create({"name": ""})
    local_photo = ready_product()
    products.add_image_file(local_photo, make_image())
    res = sync.enqueue([bad, local_photo])
    assert res["accepted"] == 0
    reasons = {r["id"]: " ".join(r["reasons"]) for r in res["rejected"]}
    assert "Нет названия" in reasons[bad]
    assert "публичный адрес" in reasons[local_photo]


def test_retry_on_server_error_then_success():
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm(upload=[(503, {"error": "busy"}), (200, {"id": "imp-2"})])
    run(fake)
    j = job()
    assert j["status"] == "pending" and j["attempts"] == 1 and "503" in j["last_error"]
    assert products.get(pid)["status"] == "sending"  # не потерян и не в ошибке

    make_due()
    run(fake)
    assert job()["status"] == "waiting"


def test_network_error_is_retried():
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm(upload=[(0, httpx.ConnectError("boom"))])
    run(fake)
    assert job()["status"] == "pending" and "Нет связи" in job()["last_error"]


def test_bad_token_fails_without_retry():
    pid = ready_product()
    sync.enqueue([pid])
    run(FakeProm(upload=[(401, {"error": "unauthorized"})]))
    assert job()["status"] == "failed"
    p = products.get(pid)
    assert p["status"] == "error" and "токен" in p["last_error"]

    # ручной повтор ставит товар обратно в очередь
    res = sync.retry_job(job()["id"])
    assert res["accepted"] == 1
    assert products.get(pid)["status"] == "sending"


def test_gives_up_after_max_attempts():
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm(upload=[(500, {"error": "x"})])
    for _ in range(sync.MAX_ATTEMPTS):
        make_due()
        run(fake)
    assert job()["status"] == "failed"
    assert products.get(pid)["status"] == "error"


def test_product_edited_while_sending_is_not_marked_synced():
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm()
    run(fake)
    products.update(pid, {"price": 120})  # правка во время отправки
    make_due()
    run(fake)
    assert job()["status"] == "done"
    assert products.get(pid)["status"] == "ready"


def test_per_product_errors_from_import():
    a, b = ready_product(external_id="A"), ready_product(external_id="B")
    sync.enqueue([a, b])
    fake = FakeProm(statuses=[(200, {"status": "PARTIAL", "errors": [{"external_id": "B", "message": "Плохое фото"}]})])
    run(fake)
    make_due()
    run(fake)
    assert products.get(a)["status"] == "synced"
    pb = products.get(b)
    assert pb["status"] == "error" and pb["last_error"] == "Плохое фото"


def test_fatal_import_marks_error():
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm(statuses=[(200, {"status": "FATAL", "message": "bad file"})])
    run(fake)
    make_due()
    run(fake)
    assert job()["status"] == "failed"
    assert products.get(pid)["status"] == "error"


def test_missing_token_fails_job():
    pid = ready_product()
    sync.enqueue([pid])
    asyncio.run(sync.run_once())  # токен не задан
    assert job()["status"] == "failed"
    assert "токен" in products.get(pid)["last_error"]


def test_import_settings_override():
    db.set_setting("import_settings", json.dumps({"mark_missing_product_as": "none", "force_update": True}))
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm()
    run(fake)
    assert b'"force_update": true' in fake.requests[0].content
