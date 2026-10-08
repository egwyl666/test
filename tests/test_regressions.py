"""Ошибки, найденные проверкой кода (ОТЧЁТ-ПРОВЕРКИ.md). Каждый тест воспроизводит сценарий, который ломался."""

import asyncio
import json

import httpx
import pytest

from promloader import changes, db, pricing, products, promcatalog, promdelete, rates, suppliers, sync
from promloader.prom_api import PromClient

from .test_suppliers import BASE, make_supplier, make_yml, run


def on_prom(name="Гачок", ext="G-1", prom_id=501, price=100):
    pid = products.create({"name": name, "price": price, "external_id": ext})
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?, prom_id = ?, pending_fields = '[]' WHERE id = ?",
                  (db.now(), prom_id, pid))
    return pid


def status(pid):
    row = db.query_one("SELECT status FROM products WHERE id = ?", (pid,))
    return row["status"] if row else None


# ---------- статусы: удаление не теряется ----------

def test_touch_keeps_deleting_even_with_stale_status():
    """recalc/bulk передавали в _touch статус, прочитанный раньше: «удаляется» превращалось в «Готов»."""
    pid = on_prom()
    promdelete.request([pid])
    with db.tx() as c:
        products._touch(c, pid, {"price": 120}, "synced")   # устаревший статус
    assert status(pid) == "deleting"


def test_enqueue_does_not_overwrite_deleting(monkeypatch):
    """Между проверкой и записью товар успели начать удалять — отправка не должна его перехватить."""
    pid = on_prom()
    stale = products.get(pid)                       # прочитан ещё «На Prom»
    promdelete.request([pid])
    monkeypatch.setattr(products, "get", lambda i: {**stale, "status": "ready"})
    res = sync.enqueue([pid])
    assert status(pid) == "deleting" and res["accepted"] == 0


def test_finished_job_does_not_flip_product_of_newer_job():
    """Товар поправили во время отправки и отправили снова: конец первой выгрузки не должен сбить вторую."""
    pid = on_prom()
    products.update(pid, {"name": "Нова назва"})
    job1 = sync.enqueue([pid])["job_id"]
    products.update(pid, {"name": "Ще новіша"})       # пока job1 в работе
    job2 = sync.enqueue([pid])["job_id"]
    assert job2 != job1 and status(pid) == "sending"
    first = json.loads(db.query_one("SELECT products FROM sync_jobs WHERE id = ?", (job1,))["products"])
    sync._finish_products(first, ok=True, job_id=job1)
    assert status(pid) == "sending"                 # второй выгрузке ещё быть


def test_cancelled_deletion_is_not_executed():
    pid = on_prom()
    promdelete.request([pid])
    promdelete.cancel([pid])
    promdelete._done([pid])                          # подтверждение Prom пришло уже после отмены
    assert status(pid) == "synced"


def test_local_delete_refuses_product_being_sent():
    """«Только из программы» для товара в отправке ломал всю выгрузку (товар исчезал из файла)."""
    pid = on_prom()
    products.update(pid, {"name": "Нова назва"})
    sync.enqueue([pid])
    r = promdelete.request([pid], from_prom=False)
    assert r["deleted"] == 0 and r["rejected"] and status(pid) == "sending"


def test_supplier_run_does_not_resurrect_product_being_deleted():
    sid = make_supplier(make_yml(BASE))
    run(sid)
    pid = db.query_one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?, prom_id = 7 WHERE id = ?", (db.now(), pid))
    promdelete.request([pid])
    run(sid)                                         # обновление поставщика, пока Prom не подтвердил удаление
    assert status(pid) == "deleting"
    promdelete._done([pid])
    stats = run(sid)["stats"]
    assert stats["created"] == 0 and stats.get("ignored") == 1


def test_reconcile_does_not_turn_deleting_into_error():
    pid = on_prom(prom_id=None)

    class Slow:
        async def get_by_external_id(self, ext):
            promdelete.request([pid])                # пока ждали Prom, товар начали удалять
            return None
    asyncio.run(promcatalog.reconcile(Slow(), set()))
    assert status(pid) == "deleting"


# ---------- правка без изменений ----------

def test_saving_unchanged_values_keeps_status_and_locks():
    sid = make_supplier(make_yml(BASE))
    run(sid)
    pid = db.query_one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'synced', synced_at = ?, pending_fields = '[]' WHERE id = ?",
                  (db.now(), pid))
    p = products.get(pid)
    products.update(pid, {"name": p["name"], "group_name": p["group_name"]})
    after = products.get(pid)
    assert after["status"] == "synced" and after["locked_fields"] == [] and after["revision"] == p["revision"]
    products.bulk_edit([pid], {"group_name": p["group_name"]})
    assert products.get(pid)["status"] == "synced"


# ---------- «пробный прогон» ничего не трогает ----------

def test_preview_keeps_photo_files():
    """«👁 Что изменится» удалял файлы фото, которые поставщик заменил бы, — а база откатывалась к ним."""
    from .conftest import make_image
    sid = make_supplier(make_yml(BASE))
    pid = products.create({"name": "Кружка", "price": 1, "external_id": "A1"})
    products.add_image_file(pid, make_image())
    photo = db.uploads_dir() / db.query_one("SELECT file FROM images WHERE product_id = ?", (pid,))["file"]
    suppliers.preview(sid)
    assert photo.exists()
    assert db.query_one("SELECT file FROM images WHERE product_id = ?", (pid,))["file"] == photo.name


def test_preview_blocks_parallel_run(monkeypatch):
    sid = make_supplier(make_yml(BASE))
    seen = {}

    def during(fn, *a, **k):
        with pytest.raises(suppliers.SupplierError, match="обновляется"):
            suppliers.run(sid)
        seen["running"] = suppliers.get(sid)["running"]
        return {"result": ({"total": 0, "valid": 0, "errors": 0, "error_samples": [], "unchanged": 0}, [])}
    monkeypatch.setattr(changes, "preview", during)
    suppliers.preview(sid)
    assert seen["running"] is True and suppliers.get(sid)["running"] is False


def test_preview_counts_gone_items_like_real_run(client):
    """Строки, привязанные импортом файла, предпросмотр не считал «пропавшими», а настоящее обновление — считало."""
    from promloader import main
    sid = make_supplier(make_yml(BASE))
    pid = products.create({"name": "Тарілка з імпорту", "price": 1, "external_id": "Z9", "presence": "available"})
    main._link_imported(suppliers.get(sid), "Z9", pid, {"data": {"external_id": "Z9"}, "params": [], "image_urls": []})
    suppliers.update(sid, {"missing_action": "not_available"})
    p = suppliers.preview(sid)
    real = run(sid)["stats"]
    assert real["missing"] == 1 and p["gone"] == 1


def test_rate_preview_fetches_nbu_outside_the_transaction(client, monkeypatch):
    """Запрос к НБУ (до 20 с) не должен идти внутри пробного прогона — вся программа ждала бы его."""
    from .conftest import REAL_NBU
    http = []

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, *a, **k):
            http.append(db.in_transaction())
            return httpx.Response(200, json=[{"rate": 41.0}], request=httpx.Request("GET", "https://bank.gov.ua"))
    monkeypatch.setattr(rates, "nbu", REAL_NBU)
    monkeypatch.setattr(rates.httpx, "Client", Client)
    monkeypatch.setattr(rates, "_failed", {})
    rates.save_settings({"mode": "manual", "manual": {"USD": 40}})
    products.create({"name": "Гачок", "cost_price": 1, "cost_currency": "USD", "price": 40, "currency": "UAH"})
    r = client.post("/api/rates/preview", json={"mode": "nbu"})
    assert r.status_code == 200 and http == [False]


# ---------- числа и валюты ----------

@pytest.mark.parametrize("bad", ["nan", "inf", "1e309", "-inf"])
def test_non_finite_numbers_rejected(client, bad):
    """«nan»/«inf» сохранялись: цена становилась бесконечной, список товаров и курс отдавали ошибку 500."""
    assert client.put("/api/rates", json={"mode": "manual", "manual": {"USD": bad}}).status_code == 400
    sid = suppliers.create("Опт")
    assert client.patch(f"/api/suppliers/{sid}", json={"rate_value": bad}).status_code == 400
    assert client.patch(f"/api/suppliers/{sid}", json={"interval_hours": bad}).status_code == 400
    assert client.put("/api/pricing", json={"rules": [{"markup_percent": bad}]}).status_code == 400
    pid = products.create({"name": "Гачок", "price": 1})
    r = client.patch(f"/api/products/{pid}", json={"price": bad})
    assert r.status_code == 400 and r.json().get("field") == "price"
    assert client.get("/api/products").status_code == 200 and client.get("/api/rates").status_code == 200


def test_journal_keeps_exact_numbers_for_revert(client):
    """Журнал хранил числа с 6 знаками: «↩ Вернуть» 10999.99 возвращал 11000."""
    pid = products.create({"name": "Гачок", "price": 10999.99})
    client.patch(f"/api/products/{pid}", json={"price": "12000"})
    ch = [r for r in client.get(f"/api/products/{pid}/changes").json()["items"] if r["field"] == "price"][0]
    assert ch["old"] == "10999.99"
    assert client.post(f"/api/changes/{ch['id']}/revert").json()["price"] == 10999.99
    big = products.create({"name": "Котушка", "price": 1234567})
    client.patch(f"/api/products/{big}", json={"price": "1"})
    ch = [r for r in client.get(f"/api/products/{big}/changes").json()["items"] if r["field"] == "price"][0]
    assert ch["old"] == "1234567"


def test_text_of_exactly_400_chars_is_revertable(client):
    pid = products.create({"name": "Гачок", "price": 1, "description": "д" * 400})
    client.patch(f"/api/products/{pid}", json={"description": "коротко"})
    ch = [r for r in client.get(f"/api/products/{pid}/changes").json()["items"] if r["field"] == "description"][0]
    assert ch["revertable"]


@pytest.mark.parametrize("text,code", [("грн", "UAH"), ("грн.", "UAH"), ("₴", "UAH"), ("Гривня", "UAH"),
                                       ("$", "USD"), ("usd", "USD"), ("€", "EUR"), ("євро", "EUR"), ("zł", "PLN")])
def test_currency_synonyms(text, code):
    """«грн.», «₴», «$» из прайсов не узнавались: товар уходил в РРЦ, курс НБУ не находился, строка с ошибкой."""
    assert products.normalize({"currency": text})["currency"] == code
    assert products.normalize({"cost_currency": text})["cost_currency"] == code


def test_unknown_currency_is_an_error():
    with pytest.raises(products.ProductError) as exc:
        products.normalize({"currency": "тугрики"})
    assert exc.value.field == "currency"


def test_unicode_minus_and_numbers():
    assert products.parse_number("−100") == -100      # U+2212 из Excel/сайтов
    assert products.parse_number("1 299,50 грн") == 1299.5
    with pytest.raises(products.ProductError):
        products.parse_number("9" * 400)
