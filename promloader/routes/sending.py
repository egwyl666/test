"""Отправка на Prom: очередь, задания выгрузки, удаление с Prom."""

import asyncio
import json

from fastapi import (
    APIRouter,
    Body,
    HTTPException,
)
from fastapi.responses import (
    Response,
)

from .. import (
    changes,
    config,
    db,
    diagnose,
    feed,
    phototunnel,
    products,
    r2,
    rates,
    suppliers,
    sync,
)
from ..prom_api import PromError
from ..web import _background
from .products import _selected

router = APIRouter()


@router.post("/api/sync")
def enqueue(ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return sync.enqueue(_selected(ids, filter))


@router.get("/api/sync/jobs")
def jobs():
    return sync.list_jobs()


@router.get("/api/sync/photos")
def photo_access():
    return {"enabled": phototunnel.enabled(), "r2": r2.active(), **phototunnel.tunnel.status()}


@router.post("/api/diagnose")
async def diagnose_start(product_id: int = Body(..., embed=True)):
    try:
        product = await asyncio.to_thread(products.get, product_id)
    except KeyError:
        raise HTTPException(404, "Товар не найден")
    if any(not r["done"] for r in diagnose.RUNS.values()):
        raise HTTPException(409, "Проверка уже идёт — дождитесь её окончания")
    run = diagnose.Run(product)
    task = asyncio.create_task(diagnose.execute(run, product_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return run.state


@router.get("/api/diagnose/{run_id}")
def diagnose_state(run_id: str):
    if run_id not in diagnose.RUNS:
        raise HTTPException(404)
    return diagnose.RUNS[run_id]


@router.get("/api/rates")
def rates_view():
    return {"settings": rates.settings(), "current": rates.current(), "coverage": rates.coverage()}


@router.put("/api/rates")
def rates_save(data: dict = Body(...)):
    """Сохранить курс и сразу пересчитать цены (изменённые цены товаров на Prom — отправить, если включено)."""
    try:
        rates.save_settings(data)
    except rates.RateError as exc:
        raise HTTPException(400, str(exc))
    with changes.source("Курс валют (изменён вручную)"):
        result = suppliers.recalc_prices()
    rates.mark_applied()
    return {"settings": rates.settings(), "current": rates.current(), "result": result,
            "coverage": rates.coverage()}


@router.get("/api/changes")
def changes_list(period: str = "7d", who: str = "", field: str = "", q: str = "", limit: int = 200,
                       offset: int = 0):
    """Журнал изменений товаров: что, было -> стало, кто и когда."""
    return changes.search(period, who, field, q, None, min(max(limit, 1), 1000),
                                   max(offset, 0))


@router.get("/api/changes.csv")
def changes_csv(period: str = "7d", who: str = "", field: str = "", q: str = ""):
    content = changes.to_csv(period=period, who=who, field=field, q=q)
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="promloader-izmeneniya.csv"'})


@router.post("/api/changes/{change_id}/revert")
def change_revert(change_id: int):
    try:
        return changes.revert(change_id)
    except changes.RevertError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/products/{product_id}/prom-link")
async def prom_link(product_id: int):
    """Номер товара на Prom: у выгруженных импортом он неизвестен — один раз спрашиваем у Prom по артикулу."""
    p = await asyncio.to_thread(products.get, product_id)
    if p["prom_id"] or not p["synced_at"]:
        return {"url": p["prom_url"]}
    try:
        async with sync.make_client() as client:
            found = await client.get_by_external_id(p["external_id"])
    except PromError as exc:
        raise HTTPException(400, str(exc))
    if not found or not found.get("id") or str(found.get("status", "")) in ("deleted", "deleted_by_moderator"):
        return {"url": ""}
    await asyncio.to_thread(_save_prom_id, product_id, int(found["id"]))
    return {"url": products.prom_url(found["id"])}


def _save_prom_id(product_id: int, prom_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE products SET prom_id = ? WHERE id = ?", (prom_id, product_id))


@router.get("/api/products/{product_id}/changes")
def product_changes(product_id: int, limit: int = 100):
    return changes.search("all", "", "", "", product_id, min(max(limit, 1), 1000), 0)


@router.get("/api/rates/{code}")
def rate(code: str):
    if code.upper() not in rates.CURRENCIES:
        raise HTTPException(404)
    try:
        return rates.nbu(code)
    except rates.RateError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/r2/check")
def r2_check():
    try:
        return {"ok": True, "message": r2.check()}
    except r2.R2Error as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/r2/stats")
def r2_stats():
    return {"active": r2.active(), **r2.stats()}


@router.get("/api/sync/jobs/{job_id}/file")
def job_file(job_id: int):
    """Файл выгрузки — чтобы загрузить его в кабинете Prom вручную и увидеть подробный отчёт."""
    job = db.query_one("SELECT products FROM sync_jobs WHERE id = ?", (job_id,))
    if job is None:
        raise HTTPException(404)
    wanted = [int(pid) for pid in json.loads(job["products"])]
    # одним запросом, а не по запросу на товар (в задании их бывают тысячи); порядок — как в задании
    alive = {r["id"] for r in db.query(f"SELECT id FROM products WHERE id {db.IN_LIST}", (db.as_list(wanted),))}
    ids = [pid for pid in wanted if pid in alive]
    base = (r2.base_url() if r2.active() else "") or config.public_base_url() or (phototunnel.tunnel.url or "")
    content = feed.build(ids, base)
    return Response(content, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="promloader-vygruzka-{job_id}.xml"'})


@router.post("/api/sync/jobs/{job_id}/retry")
def retry(job_id: int):
    try:
        return sync.retry_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
