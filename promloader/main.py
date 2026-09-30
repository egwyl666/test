"""Веб-сервер: API + статические страницы."""

import asyncio
import base64
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, db, excel, feed, products, sync
from .prom_api import DEFAULT_IMPORT_SETTINGS, PromError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
PAGES = {"/": "index.html", "/product": "product.html", "/import": "import.html", "/settings": "settings.html"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    stop = asyncio.Event()
    task = None
    if os.environ.get("PROMLOADER_WORKER", "1") != "0":
        task = asyncio.create_task(sync.worker(stop))
    yield
    stop.set()
    if task:
        await task


app = FastAPI(title="Prom Loader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- доступ ----------

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Если задан APP_PASSWORD — интерфейс закрыт паролем. Фото и фид остаются открытыми: их забирает Prom."""
    password = os.environ.get("APP_PASSWORD")
    path = request.url.path
    if password and not (path.startswith("/media/") or path.startswith("/feed/")):
        user = os.environ.get("APP_USER", "admin")
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                given_user, _, given_pass = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(given_user, user) and secrets.compare_digest(given_pass, password)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Prom Loader"'})
    return await call_next(request)


@app.exception_handler(products.ProductError)
@app.exception_handler(excel.ImportError_)
async def user_error(request: Request, exc: Exception):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def not_found(request: Request, exc: KeyError):
    return JSONResponse({"detail": "Не найдено"}, status_code=404)


# ---------- страницы ----------

def _page(name: str):
    async def handler():
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})
    return handler


for route, filename in PAGES.items():
    app.get(route, include_in_schema=False)(_page(filename))


@app.get("/media/{name}", include_in_schema=False)
async def media(name: str):
    path = db.uploads_dir() / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/feed/prom.yml")
async def feed_all(key: str = ""):
    """Постоянная ссылка для автоимпорта в кабинете Prom (все товары, кроме черновиков)."""
    if not secrets.compare_digest(key, config.get("feed_key")):
        raise HTTPException(403, "Неверный ключ фида")
    return Response(feed.build(None, config.public_base_url()), media_type="application/xml")


# ---------- товары ----------

@app.get("/api/meta")
async def meta():
    return {
        "presence": products.PRESENCE,
        "statuses": products.STATUSES,
        "targets": excel.TARGETS,
        "max_images": products.MAX_IMAGES,
        "shop_name": db.get_setting("shop_name"),
        "groups": [r["group_name"] for r in db.query(
            "SELECT DISTINCT group_name FROM products WHERE group_name != '' ORDER BY group_name")],
    }


@app.get("/api/products")
async def list_products(status: str = "", q: str = "", limit: int = 200, offset: int = 0):
    return products.list_products(status, q, min(limit, 1000), offset)


@app.post("/api/products")
async def create_product(data: dict = Body(default={})):
    return products.get(products.create(data))


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    return products.get(product_id)


@app.patch("/api/products/{product_id}")
async def update_product(product_id: int, data: dict = Body(...)):
    return products.update(product_id, data)


@app.post("/api/products/{product_id}/duplicate")
async def duplicate_product(product_id: int):
    src = products.get(product_id)
    data = {k: src[k] for k in products.EDITABLE if k != "external_id"}
    data["name"] = (src["name"] + " (копия)").strip()
    new_id = products.create(data)
    for img in products.image_rows(product_id):
        if img["url"]:
            products.add_image_url(new_id, img["url"])
        else:
            products.add_image_file(new_id, (db.uploads_dir() / img["file"]).read_bytes())
    return products.get(new_id)


@app.post("/api/products/delete")
async def delete_products(ids: list[int] = Body(..., embed=True)):
    return {"deleted": products.delete(ids)}


@app.post("/api/products/status")
async def set_status(ids: list[int] = Body(...), status: str = Body(...)):
    products.set_status(ids, status)
    return {"ok": True}


@app.post("/api/products/{product_id}/images")
async def upload_images(product_id: int, files: list[UploadFile] = File(...)):
    added, errors = [], []
    for f in files:
        content = await f.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            errors.append(f"{f.filename}: файл больше 25 МБ")
            continue
        try:
            added.append(products.add_image_file(product_id, content))
        except products.ProductError as exc:
            errors.append(f"{f.filename}: {exc}")
    return {"added": added, "errors": errors, "product": products.get(product_id)}


@app.post("/api/products/{product_id}/images/url")
async def add_image_url(product_id: int, url: str = Body(..., embed=True)):
    products.add_image_url(product_id, url)
    return products.get(product_id)


@app.delete("/api/products/{product_id}/images/{image_id}")
async def delete_image(product_id: int, image_id: int):
    products.delete_image(product_id, image_id)
    return products.get(product_id)


@app.post("/api/products/{product_id}/images/order")
async def reorder_images(product_id: int, ids: list[int] = Body(..., embed=True)):
    products.reorder_images(product_id, ids)
    return products.get(product_id)


# ---------- отправка на Prom ----------

@app.post("/api/sync")
async def enqueue(ids: list[int] = Body(..., embed=True)):
    return sync.enqueue(ids)


@app.get("/api/sync/jobs")
async def jobs():
    return sync.list_jobs()


@app.post("/api/sync/jobs/{job_id}/retry")
async def retry(job_id: int):
    try:
        return sync.retry_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------- настройки ----------

def _settings_view() -> dict:
    base = config.public_base_url()
    return {
        "prom_token": config.mask(config.get("prom_token")),
        "prom_token_set": bool(config.get("prom_token")),
        "public_base_url": base,
        "prom_api_base": config.get("prom_api_base"),
        "shop_name": db.get_setting("shop_name"),
        "import_settings": json.dumps(sync.import_settings() or DEFAULT_IMPORT_SETTINGS, ensure_ascii=False, indent=2),
        "feed_url": f"{base or ''}/feed/prom.yml?key={config.get('feed_key')}",
        "locked": {k: config.from_env(k) for k in config.ENV},
    }


@app.get("/api/settings")
async def get_settings():
    return _settings_view()


@app.post("/api/settings")
async def save_settings(data: dict = Body(...)):
    if data.get("prom_token"):
        db.set_setting("prom_token", data["prom_token"].strip())
    for key in ("public_base_url", "prom_api_base", "shop_name"):
        if key in data:
            db.set_setting(key, str(data[key] or "").strip())
    if "import_settings" in data:
        raw = (data["import_settings"] or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                assert isinstance(parsed, dict)
            except (ValueError, AssertionError):
                raise HTTPException(400, "Параметры импорта должны быть JSON-объектом")
            db.set_setting("import_settings", json.dumps(parsed, ensure_ascii=False))
        else:
            db.set_setting("import_settings", "")
    if data.get("regenerate_feed_key"):
        db.set_setting("feed_key", secrets.token_urlsafe(16))
    return _settings_view()


@app.post("/api/settings/check")
async def check_connection():
    try:
        async with sync.make_client() as client:
            await client.check()
    except PromError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "Подключение к Prom работает"}


# ---------- импорт из Excel ----------

def _import_path(token: str) -> Path:
    if not all(c in "0123456789abcdef" for c in token) or not token:
        raise HTTPException(404, "Файл импорта не найден")
    matches = list(db.imports_dir().glob(f"{token}.*"))
    if not matches:
        raise HTTPException(404, "Файл импорта не найден — загрузите его заново")
    return matches[0]


@lru_cache(maxsize=4)
def _sheet_cached(path: str, sheet: str, mtime: float):
    return excel.read_sheet(Path(path), sheet)


def _sheet(token: str, sheet: str):
    path = _import_path(token)
    return _sheet_cached(str(path), sheet, path.stat().st_mtime)


@app.post("/api/import/upload")
async def import_upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".csv"):
        raise HTTPException(400, "Поддерживаются .xlsx и .csv. Старый .xls откройте в Excel и сохраните как .xlsx")
    content = await file.read(MAX_UPLOAD * 2 + 1)
    if len(content) > MAX_UPLOAD * 2:
        raise HTTPException(400, "Файл больше 50 МБ")
    token = uuid.uuid4().hex
    path = db.imports_dir() / f"{token}{suffix}"
    path.write_bytes(content)
    try:
        sheets = excel.sheet_names(path)
    except excel.ImportError_:
        path.unlink(missing_ok=True)
        raise
    return {"token": token, "filename": file.filename, "sheets": sheets}


@app.get("/api/import/{token}/sheet")
async def import_sheet(token: str, sheet: str, header_row: int = 1):
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


@app.get("/api/import/{token}/image")
async def import_image(token: str, sheet: str, row: int, n: int = 0):
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
    return excel.build_products(rows, header_row, numbers, body.get("mapping") or {}, body.get("defaults"), embedded), images


@app.post("/api/import/{token}/preview")
async def import_preview(token: str, body: dict = Body(...)):
    items, _ = _build(token, body)
    for item in items:
        eid = item["data"].get("external_id")
        item["existing_id"] = products.find_by_external_id(eid) if eid else None
    return {"items": items}


@app.post("/api/import/{token}/commit")
async def import_commit(token: str, body: dict = Body(...)):
    items, images = _build(token, body)
    embedded = images if body.get("use_embedded_images", True) else {}
    status = body.get("status") if body.get("status") in ("draft", "ready") else "draft"
    update_existing = bool(body.get("update_existing", True))
    created = updated = skipped = 0
    failed = []
    for item in items:
        if item["errors"]:
            skipped += 1
            continue
        data = dict(item["data"])
        if item["params"]:
            data["params"] = item["params"]
        files = embedded.get(item["row"], [])
        try:
            existing = products.find_by_external_id(data["external_id"]) if data.get("external_id") else None
            if existing and not update_existing:
                skipped += 1
                continue
            if existing:
                products.update(existing, data)
                pid = existing
                updated += 1
            else:
                pid = products.create(data)
                created += 1
            if item["image_urls"] or files:
                products.replace_images(pid, item["image_urls"], files)
            if status == "ready":
                products.set_status([pid], "ready")
        except products.ProductError as exc:
            failed.append({"row": item["row"], "error": str(exc)})
    return {"created": created, "updated": updated, "skipped": skipped, "failed": failed}
