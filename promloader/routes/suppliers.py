"""Поставщики: прайсы, запуск обновления, предпросмотр."""

import asyncio

from fastapi import (
    APIRouter,
    Body,
    File,
    HTTPException,
    UploadFile,
)

from .. import (
    excel,
    rates,
    suppliers,
)
from ..web import _background, log
from .imports import _new_import_token, _read_price_upload

router = APIRouter()


@router.get("/api/suppliers")
def list_suppliers():
    return suppliers.list_suppliers()


@router.post("/api/suppliers")
def create_supplier(name: str = Body("", embed=True)):
    return suppliers.get(suppliers.create(name))


@router.get("/api/suppliers/{supplier_id}")
def get_supplier(supplier_id: int):
    return suppliers.get(supplier_id)


@router.patch("/api/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, data: dict = Body(...)):
    return suppliers.update(supplier_id, data)


@router.delete("/api/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int):
    suppliers.delete(supplier_id)
    return {"ok": True}


@router.post("/api/suppliers/{supplier_id}/source")
async def upload_supplier_source(supplier_id: int, file: UploadFile = File(...)):
    """Загрузить прайс файлом: он станет текущим прайсом поставщика."""
    content = await _read_price_upload(file)
    await asyncio.to_thread(suppliers.store_source, supplier_id, content, file.filename or "price")
    return await asyncio.to_thread(_new_import_token, content, file.filename or "price")


@router.post("/api/suppliers/{supplier_id}/open")
def open_supplier_source(supplier_id: int, refetch: bool = Body(False, embed=True)):
    """Открыть прайс поставщика для настройки колонок (refetch — скачать свежий по ссылке)."""
    path = suppliers.source_path(supplier_id, refetch)
    row = suppliers.get(supplier_id)
    return _new_import_token(path.read_bytes(), row["source_name"] or path.name)


@router.post("/api/suppliers/{supplier_id}/preview")
def preview_supplier(supplier_id: int, force: bool = Body(False, embed=True)):
    """«👁 Что изменится» при обновлении прайса — ничего не записывая."""
    try:
        return suppliers.preview(supplier_id, force)
    except (suppliers.SupplierError, excel.ImportError_, rates.RateError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/suppliers/{supplier_id}/run")
async def run_supplier(supplier_id: int, force: bool = Body(False, embed=True)):
    """Запуск обновления в фоне; ход виден в истории запусков поставщика."""
    s = await asyncio.to_thread(suppliers.get, supplier_id)
    if s["running"]:
        raise HTTPException(409, "Этот поставщик уже обновляется")
    task = asyncio.create_task(_run_supplier_bg(supplier_id, force))
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.sleep(0.05)  # дать запуску появиться в истории
    return {"started": True}


async def _run_supplier_bg(supplier_id: int, force: bool) -> None:
    try:
        await asyncio.to_thread(suppliers.run, supplier_id, "manual", force)
    except suppliers.SupplierError:
        pass
    except Exception:
        log.exception("Сбой обновления поставщика %s", supplier_id)
