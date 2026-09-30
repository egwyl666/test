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

from . import ai, backup, config, db, excel, feed, phototunnel, pricing, products, runtime, suppliers, sync, updater
from .prom_api import DEFAULT_IMPORT_SETTINGS, PromError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
PAGES = {
    "/": "index.html", "/product": "product.html", "/import": "import.html", "/settings": "settings.html",
    "/suppliers": "suppliers.html", "/supplier": "supplier.html", "/pricing": "pricing.html",
}
SUPPLIER_CHECK_SECONDS = 30
MAINTENANCE_SECONDS = 3600
UPDATE_CHECK_HOURS = 12
log = logging.getLogger("promloader")
_background: set[asyncio.Task] = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    suppliers.recover()
    stop = asyncio.Event()
    tasks = []
    if os.environ.get("PROMLOADER_WORKER", "1") != "0":
        tasks = [asyncio.create_task(sync.worker(stop)), asyncio.create_task(supplier_worker(stop)),
                 asyncio.create_task(maintenance_worker(stop))]
    yield
    stop.set()
    for task in tasks:
        await task
    phototunnel.tunnel.close()


async def maintenance_worker(stop: asyncio.Event) -> None:
    """Раз в час: суточная резервная копия и (раз в 12 часов) проверка обновлений."""
    last_update_check = 0.0
    while not stop.is_set():
        try:
            if backup.due():
                await asyncio.to_thread(backup.create, "daily")
        except Exception:
            log.exception("Не удалось сделать резервную копию")
        now = asyncio.get_running_loop().time()
        if not updater.is_dev_checkout() and now - last_update_check > UPDATE_CHECK_HOURS * 3600:
            last_update_check = now
            try:
                await asyncio.to_thread(updater.check)
            except Exception:
                pass  # нет интернета — проверим в следующий раз
        try:
            await asyncio.wait_for(stop.wait(), timeout=MAINTENANCE_SECONDS)
        except asyncio.TimeoutError:
            pass


async def supplier_worker(stop: asyncio.Event) -> None:
    """Обновляет поставщиков по расписанию. Разбор прайса идёт в отдельном потоке, чтобы не тормозить интерфейс."""
    while not stop.is_set():
        for supplier_id in suppliers.due():
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(suppliers.run, supplier_id, "schedule")
            except suppliers.SupplierError:
                pass
            except Exception:
                log.exception("Сбой обновления поставщика %s", supplier_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SUPPLIER_CHECK_SECONDS)
        except asyncio.TimeoutError:
            pass


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
@app.exception_handler(suppliers.SupplierError)
@app.exception_handler(ai.AIError)
@app.exception_handler(updater.UpdateError)
@app.exception_handler(backup.BackupError)
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
        "version": updater.current_version(),
        "update_available": db.get_setting("update_latest"),
        "ai": {"enabled": ai.enabled(), "provider": ai.settings()["provider"],
               "actions": {k: v[0] for k, v in ai.ACTIONS.items()}},
        "suppliers": [{"id": r["id"], "name": r["name"]} for r in db.query(
            "SELECT id, name FROM suppliers ORDER BY name COLLATE NOCASE")],
        "groups": [r["group_name"] for r in db.query(
            "SELECT DISTINCT group_name FROM products WHERE group_name != '' ORDER BY group_name")],
    }


@app.get("/api/products")
async def list_products(status: str = "", q: str = "", limit: int = 200, offset: int = 0, supplier: str = ""):
    """supplier: '' — все, 'none' — без поставщика, число — товары поставщика."""
    supplier_id = 0 if supplier == "none" else (int(supplier) if supplier.isdigit() else None)
    return products.list_products(status, q, min(limit, 1000), offset, supplier_id)


@app.post("/api/products")
async def create_product(data: dict = Body(default={})):
    return products.get(products.create(data))


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    return products.get(product_id)


@app.patch("/api/products/{product_id}")
async def update_product(product_id: int, data: dict = Body(...)):
    return products.update(product_id, data)


@app.post("/api/products/{product_id}/ai")
async def ai_edit(product_id: int, action: str = Body(...), instruction: str = Body("")):
    """Предложение ИИ по карточке. Ничего не сохраняет: пользователь сам решает, применять ли."""
    product = products.get(product_id)
    changes = await asyncio.to_thread(ai.run, product, action, instruction)
    return {"changes": changes}


@app.post("/api/ai/check")
async def ai_check():
    try:
        return await asyncio.to_thread(ai.check)
    except ai.AIError as exc:
        return {"ok": False, "models": [], "message": str(exc)}


@app.post("/api/products/{product_id}/unlock")
async def unlock_fields(product_id: int, fields: list[str] = Body(..., embed=True)):
    """Снять закрепление: поля снова берутся у поставщика / из правил наценки."""
    products.unlock(product_id, fields)
    suppliers.reapply(product_id)
    if "price" in fields:
        await asyncio.to_thread(_recalc_one, product_id)
    return products.get(product_id)


def _recalc_one(product_id: int) -> None:
    p = products.get(product_id)
    if p["supplier_id"] or (p["cost_price"] is None and p["rrp"] is None):
        return
    data = {"cost_price": p["cost_price"], "rrp": p["rrp"], "group_name": p["group_name"]}
    pricing.Pricer().apply(data)
    if data.get("price") is not None and data["price"] != p["price"]:
        products.update(product_id, {"price": data["price"]}, lock=False)


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


@app.get("/api/sync/photos")
async def photo_access():
    return {"enabled": phototunnel.enabled(), **phototunnel.tunnel.status()}


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
        "ai_provider": config.get("ai_provider"),
        "ai_providers": ai.PROVIDERS,
        "gemini_key": config.mask(config.get("gemini_key")),
        "anthropic_key": config.mask(config.get("anthropic_key")),
        "gemini_model": config.get("gemini_model") or ai.DEFAULT_MODELS["gemini"],
        "claude_model": config.get("claude_model") or ai.DEFAULT_MODELS["claude"],
        "auto_update": config.get("auto_update") != "0",
        "photo_tunnel": config.get("photo_tunnel") != "0",
        "github_token": config.mask(config.get("github_token")),
        "update_repo": config.get("update_repo") or updater.DEFAULT_REPO,
        "version": updater.current_version(),
        "dev_checkout": updater.is_dev_checkout(),
        "can_restart": runtime.can_restart(),
    }


@app.get("/api/settings")
async def get_settings():
    return _settings_view()


@app.post("/api/settings")
async def save_settings(data: dict = Body(...)):
    if data.get("prom_token"):
        db.set_setting("prom_token", data["prom_token"].strip())
    if "photo_tunnel" in data:
        db.set_setting("photo_tunnel", "1" if data["photo_tunnel"] else "0")
    if "auto_update" in data:
        db.set_setting("auto_update", "1" if data["auto_update"] else "0")
    if data.get("github_token"):
        db.set_setting("github_token", data["github_token"].strip())
    if "update_repo" in data:
        db.set_setting("update_repo", str(data["update_repo"] or "").strip())
    for key in ("gemini_key", "anthropic_key"):
        if data.get(key):
            db.set_setting(key, data[key].strip())
    if "ai_provider" in data:
        if data["ai_provider"] not in ("", *ai.PROVIDERS):
            raise HTTPException(400, "Неизвестный провайдер ИИ")
        db.set_setting("ai_provider", data["ai_provider"])
    for key in ("gemini_model", "claude_model"):
        if key in data:
            db.set_setting(key, str(data[key] or "").strip().removeprefix("models/"))
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
    content = await _read_price_upload(file)
    return _new_import_token(content, file.filename or "")


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
    supplier_id = int(body["supplier_id"]) if body.get("supplier_id") else None
    items = excel.build_products(rows, header_row, numbers, body.get("mapping") or {}, body.get("defaults"), embedded,
                                 pricing.Pricer(), supplier_id)
    return items, images


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
        except products.ProductError as exc:
            failed.append({"row": item["row"], "error": str(exc)})
    return {"created": created, "updated": updated, "skipped": skipped, "failed": failed}


# ---------- поставщики ----------

@app.get("/api/suppliers")
async def list_suppliers():
    return suppliers.list_suppliers()


@app.post("/api/suppliers")
async def create_supplier(name: str = Body("", embed=True)):
    return suppliers.get(suppliers.create(name))


@app.get("/api/suppliers/{supplier_id}")
async def get_supplier(supplier_id: int):
    return suppliers.get(supplier_id)


@app.patch("/api/suppliers/{supplier_id}")
async def update_supplier(supplier_id: int, data: dict = Body(...)):
    return suppliers.update(supplier_id, data)


@app.delete("/api/suppliers/{supplier_id}")
async def delete_supplier(supplier_id: int):
    suppliers.delete(supplier_id)
    return {"ok": True}


@app.post("/api/suppliers/{supplier_id}/source")
async def upload_supplier_source(supplier_id: int, file: UploadFile = File(...)):
    """Загрузить прайс файлом: он станет текущим прайсом поставщика."""
    content = await _read_price_upload(file)
    suppliers.store_source(supplier_id, content, file.filename or "price")
    return _new_import_token(content, file.filename or "price")


@app.post("/api/suppliers/{supplier_id}/open")
async def open_supplier_source(supplier_id: int, refetch: bool = Body(False, embed=True)):
    """Открыть прайс поставщика для настройки колонок (refetch — скачать свежий по ссылке)."""
    path = await asyncio.to_thread(suppliers.source_path, supplier_id, refetch)
    row = suppliers.get(supplier_id)
    return _new_import_token(path.read_bytes(), row["source_name"] or path.name)


@app.post("/api/suppliers/{supplier_id}/run")
async def run_supplier(supplier_id: int, force: bool = Body(False, embed=True)):
    """Запуск обновления в фоне; ход виден в истории запусков поставщика."""
    s = suppliers.get(supplier_id)
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


# ---------- наценка ----------

@app.get("/api/pricing")
async def get_pricing():
    return {"rules": pricing.list_rules(), "rounding": pricing.ROUNDING}


@app.put("/api/pricing")
async def save_pricing(rules: list[dict] = Body(..., embed=True)):
    try:
        return {"rules": pricing.save_rules(rules), "rounding": pricing.ROUNDING}
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Ошибка в правилах: {exc}")


@app.post("/api/pricing/test")
async def test_pricing(body: dict = Body(...)):
    cost = products.parse_number(body.get("cost"))
    rrp = products.parse_number(body.get("rrp"))
    supplier_id = int(body["supplier_id"]) if body.get("supplier_id") else None
    price, rule = pricing.Pricer().price(cost, rrp, supplier_id, body.get("category") or "")
    return {"price": price, "rule": rule}


@app.post("/api/pricing/apply")
async def apply_pricing():
    return await asyncio.to_thread(suppliers.recalc_prices)


# ---------- обновления и резервные копии ----------

@app.get("/api/update/check")
async def update_check():
    return await asyncio.to_thread(updater.check)


@app.post("/api/update/install")
async def update_install():
    version = await asyncio.to_thread(updater.update)
    restarting = runtime.request_restart()
    return {"version": version, "restarting": restarting}


@app.get("/api/backups")
async def list_backups():
    return {"items": backup.list_backups(), "last": db.get_setting("last_backup_at"), "can_restart": runtime.can_restart()}


@app.post("/api/backups")
async def create_backup():
    return await asyncio.to_thread(backup.create, "manual")


@app.get("/api/backups/{name}")
async def download_backup(name: str):
    path = backup.path_of(name)
    return FileResponse(path, filename=name, media_type="application/zip")


@app.post("/api/backups/{name}/restore")
async def restore_backup(name: str):
    backup.stage_restore(name)
    return {"restarting": runtime.request_restart()}


@app.post("/api/backups/upload")
async def restore_uploaded_backup(file: UploadFile = File(...)):
    backup.stage_restore_upload(await file.read())
    return {"restarting": runtime.request_restart()}


@app.post("/api/shutdown")
async def shutdown(request: Request):
    """Выключение из меню значка у часов. Только с этого компьютера."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403)
    return {"stopping": runtime.request_stop()}


@app.post("/api/restart")
async def restart():
    if not runtime.request_restart():
        raise HTTPException(400, "Программа запущена не через START.bat — перезапустите её вручную")
    return {"restarting": True}
