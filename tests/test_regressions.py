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
