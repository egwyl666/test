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
    db.set_setting("photo_tunnel", "0")  # временный доступ к фото выключен
    res = sync.enqueue([bad, local_photo])
    assert res["accepted"] == 0
    reasons = {r["id"]: " ".join(r["reasons"]) for r in res["rejected"]}
    assert "Нет названия" in reasons[bad]
    assert "Фото для Prom" in reasons[local_photo]

    db.set_setting("photo_tunnel", "1")  # по умолчанию фото уйдут через временный туннель
    assert sync.enqueue([local_photo])["accepted"] == 1


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


def test_one_prom_import_at_a_time_and_queued_ones_merge():
    """Пока Prom обрабатывает импорт, новый не запускаем; всё накопившееся уходит одним файлом."""
    fake = FakeProm(statuses=[(200, {"status": "PROCESSING"})])
    first = ready_product(name="Первый")
    sync.enqueue([first])
    run(fake)
    assert len([r for r in fake.requests if r.url.path.endswith("/import_file")]) == 1
    # пока первый импорт идёт: ручная отправка и обновление поставщика
    second, third = ready_product(name="Второй"), ready_product(name="Третий")
    sync.enqueue([second])
    sync.enqueue([third])
    make_due()
    run(fake)
    uploads = [r for r in fake.requests if r.url.path.endswith("/import_file")]
    assert len(uploads) == 1
    waiting = [j for j in sync.list_jobs() if j["status"] == "pending"]
    assert waiting and all("Ждёт, пока Prom закончит" in j["last_error"] for j in waiting)
    # первый импорт закончился — второй и третий уходят одним файлом
    fake.statuses = [(200, {"status": "SUCCESS"})]
    make_due()
    run(fake)
    make_due()
    run(fake)
    uploads = [r for r in fake.requests if r.url.path.endswith("/import_file")]
    assert len(uploads) == 2
    body = uploads[1].content.decode("utf-8", "replace")
    assert "Второй" in body and "Третий" in body and "Первый" not in body
    assert len(sync.list_jobs()) == 2
    make_due()
    run(fake)
    assert {products.get(pid)["status"] for pid in (first, second, third)} == {"synced"}


def test_success_with_zero_products_is_not_success():
    """Prom отвечает SUCCESS с total: 0, если не узнал в файле товаров — товар не должен стать «На Prom»."""
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm(statuses=[(200, {"status": "SUCCESS", "total": 0, "imported": 0, "created": 0, "errors": []})])
    run(fake)
    make_due()
    run(fake)
    p = products.get(pid)
    assert p["status"] == "error" and "не нашёл в нём товаров" in p["last_error"]
    assert job()["status"] == "failed"


def test_old_false_successes_are_repaired():
    pid = ready_product()
    sync.enqueue([pid])
    run(FakeProm())
    make_due()
    run(FakeProm())
    assert products.get(pid)["status"] == "synced"
    with db.tx() as c:  # так выглядела выгрузка в 1.3.2 и раньше
        c.execute("""UPDATE sync_jobs SET result = '{"status": "SUCCESS", "total": 0}'""")
    assert sync.repair_false_success() == 1 and sync.repair_false_success() == 0
    p = products.get(pid)
    assert p["status"] == "error" and p["synced_at"] is None
    assert sync.retry_job(job()["id"])["accepted"] == 1
    assert not sync._quick_ok(pid)  # повтор — полным импортом


def test_job_file_download(client):
    pid = ready_product()
    sync.enqueue([pid])
    r = client.get(f"/api/sync/jobs/{job()['id']}/file")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "<yml_catalog" in r.text and "Кружка" in r.text


def test_import_sends_fields_to_update_and_falls_back():
    pid = ready_product()
    sync.enqueue([pid])
    fake = FakeProm()
    run(fake)
    upload = next(r for r in fake.requests if r.url.path.endswith("/import_file"))
    assert b'"updated_fields": ["name", "sku", "price"' in upload.content
    # Prom отклонил список полей — повтор без него, и дальше без него
    pid2 = ready_product(name="Второй")
    make_due()
    run(fake)
    fake2 = FakeProm(upload=[(400, {"error": "updated_fields: unknown value"}), (200, {"id": "imp-2"})])
    sync.enqueue([pid2])
    run(fake2)
    uploads = [r for r in fake2.requests if r.url.path.endswith("/import_file")]
    assert len(uploads) == 2 and b"updated_fields" not in uploads[1].content
    assert db.get_setting("import_plain") == "1"
