"""Импорт товаров из файла Excel/CSV/YML."""

import asyncio
import json
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import (
    APIRouter,
    Body,
    File,
    HTTPException,
    UploadFile,
)
from fastapi.responses import (
    Response,
)

from .. import (
    changes,
    db,
    excel,
    pricing,
    products,
    suppliers,
)
from ..web import MAX_UPLOAD

router = APIRouter()


def _import_path(token: str) -> Path:
    if not all(c in "0123456789abcdef" for c in token) or not token:
        raise HTTPException(404, "Файл импорта не найден")
    matches = list(db.imports_dir().glob(f"{token}.*"))
    if not matches:
        raise HTTPException(404, "Файл импорта не найден — загрузите его заново")
    return matches[0]


@lru_cache(maxsize=2)  # лист большого прайса — десятки МБ в памяти; держим не больше двух
def _sheet_cached(path: str, sheet: str, mtime: float):
    return excel.read_sheet(Path(path), sheet)


def _sheet(token: str, sheet: str):
    path = _import_path(token)
    return _sheet_cached(str(path), sheet, path.stat().st_mtime)


@router.post("/api/import/upload")
async def import_upload(file: UploadFile = File(...)):
    content = await _read_price_upload(file)
    return await asyncio.to_thread(_new_import_token, content, file.filename or "")


async def _read_price_upload(file: UploadFile) -> bytes:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix == ".xls":
        raise HTTPException(400, "Старый формат .xls не поддерживается: откройте файл в Excel и сохраните как .xlsx")
    if suffix not in (".xlsx", ".xlsm", ".csv", ".xml", ".yml", ".txt", ""):
        raise HTTPException(400, "Поддерживаются .xlsx, .csv и XML/YML")
    content = await file.read(MAX_UPLOAD * 8 + 1)
    if len(content) > MAX_UPLOAD * 8:
        raise HTTPException(400, "Файл больше 200 МБ")
    return content


def _new_import_token(content: bytes, filename: str) -> dict:
    """Кладёт файл во временную папку импорта: дальше работают общие шаги «строки и колонки»."""
    suffix = excel.detect_suffix(content, filename)
    token = uuid.uuid4().hex
    path = db.imports_dir() / f"{token}{suffix}"
    path.write_bytes(content)
    try:
        sheets = excel.sheet_names(path)
    except excel.ImportError_:
        path.unlink(missing_ok=True)
        raise
    return {"token": token, "filename": filename, "sheets": sheets}


@router.get("/api/import/{token}/sheet")
def import_sheet(token: str, sheet: str, header_row: int = 1):
    rows, images = _sheet(token, sheet)
    width = max((len(r) for r in rows), default=0)
    headers = excel.headers_for(rows, header_row)
    return {
        "rows": [r + [""] * (width - len(r)) for r in rows[: excel.MAX_PREVIEW_ROWS]],
        "total_rows": len(rows),
        "letters": excel.column_letters(width),
        "headers": headers,
        "mapping": excel.guess_mapping(headers),
        "image_rows": {str(k): len(v) for k, v in images.items()},
    }


@router.get("/api/import/{token}/image")
def import_image(token: str, sheet: str, row: int, n: int = 0):
    """Картинка, вставленная в ячейку Excel, — для предпросмотра карточек до импорта."""
    _, images = _sheet(token, sheet)
    try:
        content = images[row][n]
    except (KeyError, IndexError):
        raise HTTPException(404)
    kind = {b"\x89PNG": "image/png", b"GIF8": "image/gif"}.get(content[:4], "image/jpeg")
    return Response(content, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})


def _build(token: str, body: dict) -> tuple[list[dict], dict]:
    rows, images = _sheet(token, body["sheet"])
    header_row = int(body.get("header_row") or 0)
    numbers = excel.parse_row_spec(body.get("rows", ""), len(rows))
    if not numbers:
        raise excel.ImportError_("Не выбрано ни одной строки")
    embedded = images if body.get("use_embedded_images", True) else {}
    supplier_id = int(body["supplier_id"]) if body.get("supplier_id") else None
    clean = False
    if supplier_id:
        try:
            clean = body.get("clean_names", suppliers.get(supplier_id)["clean_names"])
        except KeyError:
            pass
    items = excel.build_products(rows, header_row, numbers, body.get("mapping") or {}, body.get("defaults"), embedded,
                                 pricing.Pricer(), supplier_id)
    if clean:
        excel.clean_names(items)
    return items, images


@router.post("/api/import/{token}/preview")
def import_preview(token: str, body: dict = Body(...)):
    return _import_preview(token, body)


def _import_preview(token: str, body: dict) -> dict:
    items, _ = _build(token, body)
    for item in items:
        eid = item["data"].get("external_id")
        item["existing_id"] = products.find_by_external_id(eid) if eid else None
    return {"items": items}


@router.post("/api/import/{token}/commit")
def import_commit(token: str, body: dict = Body(...)):
    return _import_commit(token, body)  # большой импорт не подвешивает интерфейс


def _import_commit(token: str, body: dict) -> dict:
    try:
        with changes.source("Импорт файла"):
            return _import_commit_inner(token, body)
    finally:
        _sheet_cached.cache_clear()  # импорт закончен — разобранный файл в памяти больше не нужен


def _link_imported(supplier: dict, sku: str, pid: int, item: dict) -> None:
    """Импорт с выбранным поставщиком: товар становится товаром поставщика (и перестаёт считаться удалённым вами)."""
    payload = json.dumps({"data": item["data"], "params": item["params"], "image_urls": item["image_urls"]},
                         ensure_ascii=False)
    with db.tx() as c:
        owner = c.execute("SELECT supplier_id FROM products WHERE id = ?", (pid,)).fetchone()
        if owner is None or owner["supplier_id"] not in (None, supplier["id"]):
            return  # товар другого поставщика — не перехватываем
        c.execute("UPDATE products SET supplier_id = ? WHERE id = ?", (supplier["id"], pid))
        suppliers.link_item(c, supplier["id"], sku, pid, payload, "", db.now(), 0)


def _import_commit_inner(token: str, body: dict) -> dict:
    items, images = _build(token, body)
    embedded = images if body.get("use_embedded_images", True) else {}
    status = body.get("status") if body.get("status") in ("draft", "ready") else "draft"
    update_existing = bool(body.get("update_existing", True))
    supplier = None
    if body.get("supplier_id"):
        try:
            supplier = suppliers.get(int(body["supplier_id"]))
        except KeyError:
            supplier = None
    created = updated = skipped = 0
    failed = []
    for item in items:
        if item["errors"]:
            skipped += 1
            continue
        data = dict(item["data"])
        sku = data.get("external_id") or ""
        if supplier and sku:
            data["external_id"] = supplier["prefix"] + sku  # как у товаров, созданных обновлением этого поставщика
        if item["params"]:
            data["params"] = item["params"]
        files = embedded.get(item["row"], [])
        try:
            existing = products.find_by_external_id(data["external_id"]) if data.get("external_id") else None
            if existing and not update_existing:
                skipped += 1
                continue
            if existing:
                products.update(existing, data, lock=False)
                pid = existing
                updated += 1
            else:
                pid = products.create(data)
                created += 1
            if item["image_urls"] or files:
                products.replace_images(pid, item["image_urls"], files)
            if status == "ready":
                products.set_status([pid], "ready")
            if supplier and sku:
                _link_imported(supplier, sku, pid, item)
        except products.ProductError as exc:
            failed.append({"row": item["row"], "error": str(exc)})
    return {"created": created, "updated": updated, "skipped": skipped, "failed": failed}
