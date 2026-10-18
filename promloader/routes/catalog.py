"""Каталог с Prom и запись внешних ID."""

import asyncio

from fastapi import (
    APIRouter,
    HTTPException,
)

from .. import (
    config,
    products,
    promcatalog,
    sync,
)
from ..prom_api import PromError
from ..web import _background, log

router = APIRouter()


@router.get("/api/prom/catalog")
def prom_catalog_state():
    return promcatalog.live_state()


@router.post("/api/prom/external-ids/dismiss")
def prom_external_ids_dismiss():
    """Скрыть старую ошибку записи ID (например, «нет связи» — сеть с тех пор вернулась)."""
    promcatalog._ext_state(error="")
    return promcatalog.live_state()


@router.get("/api/prom/external-ids")
def prom_external_ids():
    """Скольким товарам записать «Ідентифікатор_товару» на Prom и какие два пойдут в пробу."""
    rows, flagged = promcatalog.missing_ext_rows()
    sample = rows[:2]
    return {"count": len(rows), "flagged": flagged,
            "sample": [{"name": r["name"], "external_id": r["external_id"], "prom_id": r["prom_id"],
                        "prom_url": products.prom_url(r["prom_id"])} for r in sample]}


@router.post("/api/prom/external-ids/write")
async def prom_external_ids_write():
    """Записать ID на Prom через API (в фоне): сначала 2 товара с проверкой, потом остальные."""
    if promcatalog._lock.locked():
        raise HTTPException(409, "Программа уже работает с каталогом Prom — подождите, пока закончит")
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    task = asyncio.create_task(_ext_ids_bg())
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.sleep(0.05)
    return promcatalog.live_state()


async def _ext_ids_bg() -> None:
    try:
        await promcatalog.run_ext_ids(sync.make_client)
    except PromError:
        pass
    except Exception as exc:
        log.exception("Сбой записи ID на Prom")
        promcatalog._ext_state(running=False, error=f"Внутренняя ошибка: {exc}")


@router.post("/api/prom/catalog")
async def prom_catalog_load():
    if promcatalog._lock.locked():  # идёт загрузка каталога или запись ID
        raise HTTPException(409, "Программа уже работает с каталогом Prom — подождите, пока закончит")
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    task = asyncio.create_task(_catalog_bg())
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.sleep(0.05)
    return promcatalog.live_state()


async def _catalog_bg() -> None:
    try:
        await promcatalog.run(sync.make_client)
    except PromError:
        pass
    except Exception as exc:
        log.exception("Сбой загрузки каталога Prom")
        promcatalog._set_state(running=False, error=f"Внутренняя ошибка: {exc}")
