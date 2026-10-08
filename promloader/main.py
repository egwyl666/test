"""Веб-сервер: API + статические страницы."""

import asyncio
import base64
import re
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import (ai, aibulk, autostart, backup, changes, config, db, diagnose, excel, feed, rates, notify, orders, phototunnel, pricing, products, r2,
               promcatalog, promdelete, runtime, schedule, suppliers, support, sync, updater)
from .prom_api import DEFAULT_IMPORT_SETTINGS, PromError

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
PAGES = {
    "/": "index.html", "/product": "product.html", "/import": "import.html", "/settings": "settings.html",
    "/suppliers": "suppliers.html", "/supplier": "supplier.html", "/pricing": "pricing.html", "/orders": "orders.html",
    "/support": "support.html", "/diagnose": "diagnose.html", "/changes": "changes.html",
}
SUPPLIER_CHECK_SECONDS = 30
MAINTENANCE_SECONDS = 3600
UPDATE_CHECK_HOURS = 12
log = logging.getLogger("promloader")
_background: set[asyncio.Task] = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # восстановление копии, выбранное в настройках: START.bat делает это сам, а в Docker программа запускается
    # напрямую — без этой строки копия там никогда не применялась
    if backup.apply_pending(db.default_dir()):
        log.warning("Данные восстановлены из резервной копии")
    db.init()
    suppliers.recover()
    fixed = sync.repair_false_success()
    if fixed:
        log.warning("Выгрузки, в которых Prom не нашёл товаров, помечены как неудачные: %d", fixed)
    stuck = sync.repair_stuck_sending()
    if stuck:
        log.warning("Товары, застрявшие в «Отправляется» после выключения, возвращены в «Готов»: %d", stuck)
    if promcatalog.recover():
        log.warning("Загрузка каталога с Prom была прервана выключением")
    stop = asyncio.Event()
    tasks = []
    if os.environ.get("PROMLOADER_WORKER", "1") != "0":
        tasks = [asyncio.create_task(sync.worker(stop)), asyncio.create_task(supplier_worker(stop)),
                 asyncio.create_task(maintenance_worker(stop)), asyncio.create_task(schedule_worker(stop)),
                 asyncio.create_task(ai_worker(stop)), asyncio.create_task(orders_worker(stop)),
                 asyncio.create_task(support_worker(stop)), asyncio.create_task(r2_worker(stop)),
                 asyncio.create_task(rates_worker(stop))]
    try:
        yield
    finally:
        stop.set()
        # ошибка одного воркера не должна мешать остановке остальных и закрытию туннеля (иначе cloudflared остаётся)
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                log.error("Воркер завершился с ошибкой: %r", result)
        phototunnel.tunnel.close()


async def rates_worker(stop: asyncio.Event) -> None:
    """Раз в час: изменился курс (НБУ обновляется раз в день) — пересчитать цены и отправить новые на Prom."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=30)
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            await asyncio.to_thread(rates.check_changed)
        except Exception:
            log.exception("Сбой проверки курса")
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


async def r2_worker(stop: asyncio.Event) -> None:
    """Новые фото заранее уходят в хранилище R2, чтобы отправка на Prom не ждала загрузки."""
    while not stop.is_set():
        delay = 120
        try:
            if await asyncio.to_thread(r2.sweep) >= r2.SWEEP_BATCH:
                delay = 5  # фото много — продолжаем без долгой паузы
        except r2.R2Error as exc:
            log.warning("Фоновая загрузка фото в R2: %s", exc)
            delay = 600
        except Exception:
            log.exception("Сбой фоновой загрузки фото в R2")
            delay = 600
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def support_worker(stop: asyncio.Event) -> None:
    """Досылает обращения в поддержку, которые не ушли из-за отсутствия связи."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(support.send_pending)
        except Exception:
            log.exception("Сбой досылки обращений в поддержку")
        try:
            await asyncio.wait_for(stop.wait(), timeout=support.RETRY_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


async def orders_worker(stop: asyncio.Event) -> None:
    """Раз в 5 минут забирает заказы с Prom (если указан токен)."""
    while not stop.is_set():
        try:
            if orders.enabled():
                async with sync.make_client() as client:
                    await orders.poll(client)
        except PromError:
            pass  # ошибка сохранена и видна на странице заказов
        except Exception:
            log.exception("Сбой опроса заказов")
        try:
            await asyncio.wait_for(stop.wait(), timeout=orders.POLL_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


async def ai_worker(stop: asyncio.Event) -> None:
    """Массовый ИИ: по одному товару, не чаще лимита провайдера; при 429 — пауза на минуту."""
    pacer = aibulk.Pacer()
    while not stop.is_set():
        delay = 3.0
        try:
            if await asyncio.to_thread(aibulk.has_work):
                wait = pacer.wait_time()
                if wait > 0:
                    delay = wait
                else:
                    pacer.mark()
                    result = await asyncio.to_thread(aibulk.process_one)
                    if result == "rate_limited":
                        aibulk.pause_running("ИИ-сервис занят (лимит запросов или перегрузка) — продолжу через минуту")
                        delay = aibulk.RATE_LIMIT_PAUSE
                    elif result == "skipped":
                        pacer.last = 0.0  # пропуск не тратит лимит
                        delay = 0.05
                    else:
                        aibulk.pause_running("")
                        delay = 0.2
            else:
                await asyncio.to_thread(aibulk._finish_jobs)
        except Exception:
            log.exception("Сбой массового ИИ")
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def schedule_worker(stop: asyncio.Event) -> None:
    """Выгрузка по расписанию; после отключения света пропущенные запуски обрабатываются по настройке."""
    while not stop.is_set():
        try:
            for event in await asyncio.to_thread(schedule.tick):
                log.info("Расписание: %s", event)
        except Exception:
            log.exception("Сбой расписания выгрузки")
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass


async def maintenance_worker(stop: asyncio.Event) -> None:
    """Раз в час: суточная резервная копия и (раз в 12 часов, первый раз — сразу после запуска) проверка обновлений."""
    last_update_check = None  # было 0.0: часы цикла считаются от включения компьютера, и первая проверка ждала 12 ч
    while not stop.is_set():
        try:
            if backup.due():
                await asyncio.to_thread(backup.create, "daily")
                await asyncio.to_thread(changes.cleanup)  # журнал изменений — за полгода
        except Exception:
            log.exception("Не удалось сделать резервную копию")
        now = asyncio.get_running_loop().time()
        if last_update_check is None or now - last_update_check > UPDATE_CHECK_HOURS * 3600:
            last_update_check = now
            try:
                if not updater.is_dev_checkout():
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
        try:
            due = await asyncio.to_thread(suppliers.due)
        except Exception:
            log.exception("Сбой проверки расписания поставщиков")
            due = []
        for supplier_id in due:
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

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
OPEN_PATHS = ("/media/", "/feed/")  # фото и фид забирает Prom по публичному адресу


def _host_name(value: str) -> str:
    """'localhost:8765' -> 'localhost'; '[::1]:80' -> '[::1]'."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _allowed_hosts() -> set[str]:
    """Адреса, по которым интерфейс открывается без пароля. Публичного адреса (туннель, домен) здесь нет и быть не
    должно: по нему Prom забирает только фото и фид, а весь интерфейс без пароля был бы открыт всему интернету."""
    extra = os.environ.get("PROMLOADER_ALLOWED_HOSTS", "")
    return LOCAL_HOSTS | {_host_name(h) for h in extra.split(",") if h.strip()}


def _same_origin(origin: str, request: Request) -> bool:
    """Origin страницы совпадает с адресом программы. За обратным прокси (nginx) адрес в Host — внутренний,
    а адрес из браузера — в X-Forwarded-Host. Подделать эти заголовки чужая страница не может: браузер не даст
    отправить их без разрешения CORS, а его программа не выдаёт."""
    netloc = urlparse(origin).netloc.lower()
    hosts = [request.headers.get("host", "")] + request.headers.get("x-forwarded-host", "").split(",")
    return any(netloc == h.strip().lower() for h in hosts if h.strip())


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    """Защита от чужих сайтов, открытых в том же браузере.

    - Программа отвечает только по своему адресу (localhost). Это закрывает «DNS rebinding»: чужой сайт не может
      прочитать, например, резервную копию с токенами. Если интерфейс открыт по сети и закрыт паролем
      (APP_PASSWORD), адрес не проверяется; другие адреса можно разрешить в PROMLOADER_ALLOWED_HOSTS.
    - Изменяющие запросы (POST/PUT/PATCH/DELETE) принимаются только со страниц самой программы: браузер
      сообщает, откуда запрос (Origin, Sec-Fetch-Site), и запрос с чужого сайта отклоняется.
    """
    path = request.url.path
    if not path.startswith(OPEN_PATHS):
        host = request.headers.get("host", "")
        if not os.environ.get("APP_PASSWORD") and _host_name(host) not in _allowed_hosts():
            return JSONResponse({"detail": "Программа открывается только по адресу http://localhost. Для доступа по "
                                           "сети задайте APP_PASSWORD (см. README)"}, status_code=403)
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site", "")
            if site == "cross-site" or (origin is not None and not _same_origin(origin, request)):
                log.warning("Отклонён запрос %s %s с чужой страницы (%s)", request.method, path, origin or site)
                return JSONResponse({"detail": "Запрос отклонён: он пришёл не со страницы программы"}, status_code=403)
    return await call_next(request)


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
                raw = base64.b64decode(header[6:])
            except ValueError:
                raw = b""
            given_user, _, given_pass = raw.partition(b":")
            # байты, а не строки: compare_digest не принимает строки с кириллицей — такой пароль не подходил никогда
            ok = secrets.compare_digest(given_user, user.encode()) and secrets.compare_digest(given_pass, password.encode())
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Prom Loader"'})
    return await call_next(request)


@app.exception_handler(products.ProductError)
@app.exception_handler(excel.ImportError_)
@app.exception_handler(suppliers.SupplierError)
@app.exception_handler(ai.AIError)
@app.exception_handler(updater.UpdateError)
@app.exception_handler(backup.BackupError)
@app.exception_handler(schedule.ScheduleError)
@app.exception_handler(aibulk.BulkError)
@app.exception_handler(notify.NotifyError)
@app.exception_handler(support.SupportError)
@app.exception_handler(autostart.AutostartError)
async def user_error(request: Request, exc: Exception):
    body = {"detail": str(exc)}
    if getattr(exc, "field", None):
        body["field"] = exc.field
    return JSONResponse(body, status_code=400)


@app.exception_handler(KeyError)
async def not_found(request: Request, exc: KeyError):
    return JSONResponse({"detail": "Не найдено"}, status_code=404)


@app.exception_handler(ValueError)
@app.exception_handler(TypeError)
@app.exception_handler(OverflowError)
async def bad_input(request: Request, exc: Exception):
    """Число вместо текста, текст вместо списка, огромный номер страницы — это ошибка запроса (400), а не сбой
    программы (500). В журнал всё равно пишем: если это ошибка в коде, её будет видно."""
    log.warning("Неверные данные запроса %s %s: %r", request.method, request.url.path, exc, exc_info=exc)
    return JSONResponse({"detail": f"Неверные данные запроса: {exc}"}, status_code=400)


# ---------- страницы ----------

def _page(name: str):
    async def handler():
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})
    return handler


for route, filename in PAGES.items():
    app.get(route, include_in_schema=False)(_page(filename))


@app.get("/media/{name}", include_in_schema=False)
def media(name: str):
    path = db.uploads_dir() / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/feed/{name}.xml")
def feed_file(name: str):
    """Файл выгрузки для импорта Prom по ссылке (имя случайное, как у фото)."""
    if not re.fullmatch(r"[0-9a-f]{32}", name):
        raise HTTPException(404)
    path = phototunnel.feeds_dir() / f"{name}.xml"
    if not path.is_file():
        raise HTTPException(404)
    return Response(path.read_bytes(), media_type="text/xml; charset=utf-8")


@app.get("/feed/prom.yml")
def feed_all(key: str = ""):
    """Постоянная ссылка для автоимпорта в кабинете Prom (все товары, кроме черновиков)."""
    if not secrets.compare_digest(key.encode(), (config.get("feed_key") or "").encode()):
        raise HTTPException(403, "Неверный ключ фида")
    return Response(feed.build(None, config.public_base_url()), media_type="application/xml")


# ---------- товары ----------

@app.get("/api/meta")
def meta():
    return {
        "presence": products.PRESENCE,
        "statuses": products.STATUSES,
        "targets": excel.TARGETS,
        "max_images": products.MAX_IMAGES,
        "shop_name": db.get_setting("shop_name"),
        "prom_token_set": bool(config.get("prom_token")),
        "failed_suppliers": suppliers.failed(),
        "version": updater.current_version(),
        "update_available": db.get_setting("update_latest"),
        "missed_schedules": schedule.missed(),
        "orders_unseen": db.query_one("SELECT COUNT(*) AS n FROM orders WHERE seen = 0")["n"],
        "ai": {"enabled": ai.enabled(), "provider": ai.settings()["provider"],
               "actions": {k: v[0] for k, v in ai.ACTIONS.items()}},
        "suppliers": [{"id": r["id"], "name": r["name"]} for r in db.query(
            "SELECT id, name FROM suppliers ORDER BY name COLLATE NOCASE")],
        "groups": [r["group_name"] for r in db.query(
            "SELECT DISTINCT group_name FROM products WHERE group_name != '' ORDER BY group_name")],
    }


@app.get("/api/products")
def list_products(request: Request, limit: int = 200, offset: int = 0, sort: str = "updated"):
    """Фильтры (все необязательные): status, q, supplier ('' — все, 'none' — без поставщика, число), group ('-' —
    без группы), presence, on_prom (yes/no), no_photo, gone (пропал у поставщика), errors (ошибки заполнения)."""
    return products.list_products(_list_filter(request), sort, max(1, min(limit, 1000)), max(0, offset))


def _list_filter(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in products.FILTER_KEYS}


@app.get("/api/products.xlsx")
def export_products(request: Request, sort: str = "updated"):
    content = products.export_xlsx(_list_filter(request), sort)
    name = f"tovary-{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/products/bulk-edit")
def bulk_edit(fields: dict = Body(...), ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    with changes.source("Массово «✏ Изменить»"):
        return products.bulk_edit(_selected(ids, filter), fields)


@app.post("/api/products")
def create_product(data: dict = Body(default={})):
    return products.get(products.create(data))


@app.get("/api/products/{product_id}")
def get_product(product_id: int):
    return products.get(product_id)


@app.patch("/api/products/{product_id}")
def update_product(product_id: int, data: dict = Body(...)):
    return products.update(product_id, data)


@app.post("/api/products/{product_id}/ai")
def ai_edit(product_id: int, action: str = Body(...), instruction: str = Body("")):
    """Предложение ИИ по карточке. Ничего не сохраняет: пользователь сам решает, применять ли."""
    product = products.get(product_id)
    changes = ai.run(product, action, instruction)
    return {"changes": changes}


@app.post("/api/ai/check")
def ai_check():
    try:
        return ai.check()
    except ai.AIError as exc:
        return {"ok": False, "models": [], "message": str(exc)}


@app.post("/api/products/{product_id}/unlock")
def unlock_fields(product_id: int, fields: list[str] = Body(..., embed=True)):
    """Снять закрепление: поля снова берутся у поставщика / из правил наценки."""
    products.unlock(product_id, fields)
    suppliers.reapply(product_id)
    if "price" in fields:
        _recalc_one(product_id)
    return products.get(product_id)


def _recalc_one(product_id: int) -> None:
    p = products.get(product_id)
    if p["supplier_id"] or (p["cost_price"] is None and p["rrp"] is None):
        return
    data = {k: p[k] for k in ("cost_price", "rrp", "group_name", "price", "currency")}
    data["cost_currency"] = p["cost_currency"] or p["currency"] or "UAH"
    pricing.Pricer().apply(data)
    if data.get("price") is not None and data["price"] != p["price"]:
        products.update(product_id, {"price": data["price"]}, lock=False)


@app.post("/api/products/{product_id}/duplicate")
def duplicate_product(product_id: int):
    src = products.get(product_id)
    data = {k: src[k] for k in products.EDITABLE if k != "external_id"}
    data["name"] = (src["name"] + " (копия)").strip()
    new_id = products.create(data)
    for img in products.image_rows(product_id):
        if img["url"]:
            products.add_image_url(new_id, img["url"])
            continue
        try:
            content = (db.uploads_dir() / img["file"]).read_bytes()
        except OSError:
            continue  # файл фото пропал с диска — копия без него, а не ошибка посреди копирования
        products.add_image_file(new_id, content)
    return products.get(new_id)


@app.get("/api/suppliers/{supplier_id}/deleted")
def supplier_deleted(supplier_id: int):
    return suppliers.deleted_items(supplier_id)


@app.post("/api/suppliers/{supplier_id}/restore-deleted")
async def supplier_restore_deleted(supplier_id: int, body: dict = Body(default={})):
    """Вернуть удалённые вами товары поставщика: все или skus; run — сразу обновить прайс, чтобы они появились."""
    skus = body.get("skus")
    count = await asyncio.to_thread(suppliers.restore_deleted, supplier_id,
                                    [str(x) for x in skus] if skus is not None else None)
    started = False
    if count and body.get("run", True) and not (await asyncio.to_thread(suppliers.get, supplier_id))["running"]:
        task = asyncio.create_task(_run_supplier_bg(supplier_id, False))
        _background.add(task)
        task.add_done_callback(_background.discard)
        started = True
    return {"count": count, "started": started}


def _selected(ids: list[int] | None, flt: dict | None) -> list[int]:
    """Выбранные товары: список id или «все по фильтру списка» ({status, q, supplier})."""
    if flt is not None:
        return products.ids_for_filter(flt)
    if ids is None:
        raise HTTPException(400, "Не выбраны товары")
    return [int(i) for i in ids]


@app.post("/api/products/delete")
def delete_products(ids: list[int] | None = Body(None), prom: bool = Body(True), filter: dict | None = Body(None)):
    """Удалить товары. prom=True — и на Prom (товар уйдёт из программы, когда Prom подтвердит удаление)."""
    return promdelete.request(_selected(ids, filter), prom)


@app.post("/api/products/delete/check")
def delete_check(ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return promdelete.check(_selected(ids, filter))


@app.post("/api/products/delete/retry")
def delete_retry(ids: list[int] = Body(..., embed=True)):
    return {"count": promdelete.retry(ids)}


@app.post("/api/products/delete/cancel")
def delete_cancel(ids: list[int] = Body(..., embed=True)):
    return {"count": promdelete.cancel(ids)}


@app.post("/api/products/status")
def set_status(status: str = Body(...), ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return {"ok": True, "changed": products.set_status(_selected(ids, filter), status)}


@app.post("/api/products/{product_id}/images")
async def upload_images(product_id: int, files: list[UploadFile] = File(...)):
    added, errors = [], []
    for f in files:
        content = await f.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            errors.append(f"{f.filename}: файл больше 25 МБ")
            continue
        try:
            added.append(await asyncio.to_thread(products.add_image_file, product_id, content))
        except products.ProductError as exc:
            errors.append(f"{f.filename}: {exc}")
    return {"added": added, "errors": errors, "product": await asyncio.to_thread(products.get, product_id)}


@app.post("/api/products/{product_id}/images/url")
def add_image_url(product_id: int, url: str = Body(..., embed=True)):
    products.add_image_url(product_id, url)
    return products.get(product_id)


@app.delete("/api/products/{product_id}/images/{image_id}")
def delete_image(product_id: int, image_id: int):
    products.delete_image(product_id, image_id)
    return products.get(product_id)


@app.post("/api/products/{product_id}/images/order")
def reorder_images(product_id: int, ids: list[int] = Body(..., embed=True)):
    products.reorder_images(product_id, ids)
    return products.get(product_id)


# ---------- отправка на Prom ----------

@app.post("/api/sync")
def enqueue(ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return sync.enqueue(_selected(ids, filter))


@app.get("/api/sync/jobs")
def jobs():
    return sync.list_jobs()


@app.get("/api/sync/photos")
def photo_access():
    return {"enabled": phototunnel.enabled(), "r2": r2.active(), **phototunnel.tunnel.status()}


@app.post("/api/diagnose")
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


@app.get("/api/diagnose/{run_id}")
def diagnose_state(run_id: str):
    if run_id not in diagnose.RUNS:
        raise HTTPException(404)
    return diagnose.RUNS[run_id]


@app.get("/api/rates")
def rates_view():
    return {"settings": rates.settings(), "current": rates.current(), "coverage": rates.coverage()}


@app.put("/api/rates")
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


@app.get("/api/changes")
def changes_list(period: str = "7d", who: str = "", field: str = "", q: str = "", limit: int = 200,
                       offset: int = 0):
    """Журнал изменений товаров: что, было -> стало, кто и когда."""
    return changes.search(period, who, field, q, None, min(max(limit, 1), 1000),
                                   max(offset, 0))


@app.get("/api/changes.csv")
def changes_csv(period: str = "7d", who: str = "", field: str = "", q: str = ""):
    content = changes.to_csv(period=period, who=who, field=field, q=q)
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="promloader-izmeneniya.csv"'})


@app.post("/api/changes/{change_id}/revert")
def change_revert(change_id: int):
    try:
        return changes.revert(change_id)
    except changes.RevertError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/products/{product_id}/prom-link")
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


@app.get("/api/products/{product_id}/changes")
def product_changes(product_id: int, limit: int = 100):
    return changes.search("all", "", "", "", product_id, min(max(limit, 1), 1000), 0)


@app.get("/api/rates/{code}")
def rate(code: str):
    if code.upper() not in rates.CURRENCIES:
        raise HTTPException(404)
    try:
        return rates.nbu(code)
    except rates.RateError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/r2/check")
def r2_check():
    try:
        return {"ok": True, "message": r2.check()}
    except r2.R2Error as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/r2/stats")
def r2_stats():
    return {"active": r2.active(), **r2.stats()}


@app.get("/api/sync/jobs/{job_id}/file")
def job_file(job_id: int):
    """Файл выгрузки — чтобы загрузить его в кабинете Prom вручную и увидеть подробный отчёт."""
    job = db.query_one("SELECT products FROM sync_jobs WHERE id = ?", (job_id,))
    if job is None:
        raise HTTPException(404)
    ids = [int(pid) for pid in json.loads(job["products"]) if db.query_one("SELECT 1 FROM products WHERE id = ?", (int(pid),))]
    base = (r2.base_url() if r2.active() else "") or config.public_base_url() or (phototunnel.tunnel.url or "")
    content = feed.build(ids, base)
    return Response(content, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="promloader-vygruzka-{job_id}.xml"'})


@app.post("/api/sync/jobs/{job_id}/retry")
def retry(job_id: int):
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
        "gemini_model": config.get("gemini_model"),
        "gemini_model_used": db.get_setting("gemini_model_used"),
        "claude_model": config.get("claude_model") or ai.DEFAULT_MODELS["claude"],
        "ai_rate": db.get_setting("ai_rate"),
        "telegram_token": config.mask(notify.token()),
        "telegram_event_labels": notify.EVENTS,
        "support_token": config.mask(support.channel()["token"]),
        "support_chat_id": support.channel()["chat_id"],
        "auto_update": config.get("auto_update") != "0",
        "photo_tunnel": config.get("photo_tunnel") != "0",
        "photo_storage": "r2" if config.get("photo_storage") == "r2" else ("tunnel" if config.get("photo_tunnel") != "0" else "off"),
        "r2_active": r2.active(),
        **{k: r2.settings()[k] for k in r2.FIELDS if k not in r2.SECRET_FIELDS},
        **{k: config.mask(config.get(k)) for k in r2.SECRET_FIELDS},
        "quick_updates": db.get_setting("quick_updates") != "0",
        "quick_updates_error": db.get_setting("quick_updates_error"),
        "github_token": config.mask(config.get("github_token")),
        "update_repo": config.get("update_repo") or updater.DEFAULT_REPO,
        "version": updater.current_version(),
        "dev_checkout": updater.is_dev_checkout(),
        "can_restart": runtime.can_restart(),
    }


@app.get("/api/settings")
def get_settings():
    return _settings_view()


@app.post("/api/settings")
def save_settings(data: dict = Body(...)):
    """Всё или ничего: ошибка в одном поле не оставляет половину настроек сохранённой."""
    with db.tx():
        if data.get("prom_token"):
            db.set_setting("prom_token", data["prom_token"].strip())
        if data.get("telegram_token"):
            db.set_setting("telegram_token", data["telegram_token"].strip())
        if data.get("support_token"):
            db.set_setting("support_token", data["support_token"].strip())
        if data.get("support_token_clear"):
            db.set_setting("support_token", "")
        if "support_chat_id" in data:
            chat = str(data["support_chat_id"] or "").strip()
            if chat and not chat.lstrip("-").isdigit():
                raise HTTPException(400, "ID чата поддержки — это число")
            db.set_setting("support_chat_id", chat)
        if "quick_updates" in data:
            db.set_setting("quick_updates", "1" if data["quick_updates"] else "0")
            if data["quick_updates"]:
                db.set_setting("quick_updates_error", "")
        if "photo_tunnel" in data:
            db.set_setting("photo_tunnel", "1" if data["photo_tunnel"] else "0")
        if any(k in data for k in r2.FIELDS):
            fresh = {k: str(data.get(k) or "").strip() for k in r2.FIELDS}
            fresh["r2_account_id"] = r2.account_id(fresh["r2_account_id"])
            fresh["r2_public_url"] = r2.public_url(fresh["r2_public_url"])
            try:
                r2.validate(fresh)
            except r2.R2Error as exc:
                raise HTTPException(400, str(exc))
            for key, value in fresh.items():
                if key in data and (value or key not in r2.SECRET_FIELDS):  # пустой ключ = «не менять»
                    db.set_setting(key, value)
        if "photo_storage" in data:
            mode = data["photo_storage"]
            if mode not in ("r2", "tunnel", "off"):
                raise HTTPException(400, "Неизвестный способ передачи фото")
            if mode == "r2" and not r2.configured():
                raise HTTPException(400, "Чтобы выбрать хранилище R2, заполните все поля R2 и нажмите «Проверить»")
            db.set_setting("photo_storage", "r2" if mode == "r2" else "")
            if mode != "r2":
                db.set_setting("photo_tunnel", "1" if mode == "tunnel" else "0")
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
        if "ai_rate" in data:
            rate = str(data["ai_rate"] or "").strip()
            if rate and (not rate.isdigit() or not 1 <= int(rate) <= 1000):
                raise HTTPException(400, "Запросов в минуту: число от 1 до 1000")
            db.set_setting("ai_rate", rate)
        for key in ("gemini_model", "claude_model"):
            if key in data:
                db.set_setting(key, str(data[key] or "").strip().removeprefix("models/"))
        for key in ("public_base_url", "prom_api_base"):
            if key in data:
                url = str(data[key] or "").strip().rstrip("/")
                if url and not re.match(r"^https?://", url):
                    url = "https://" + url  # «shop.example.com» -> «https://shop.example.com»
                if url and not re.match(r"^https?://[^\s/]+\.[^\s]+$|^https?://(localhost|127\.0\.0\.1)(:\d+)?(/\S*)?$", url):
                    raise HTTPException(400, f"Не похоже на адрес сайта: {data[key]}")
                db.set_setting(key, url)
        if "shop_name" in data:
            db.set_setting("shop_name", str(data["shop_name"] or "").strip())
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


@app.get("/api/import/{token}/sheet")
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


@app.get("/api/import/{token}/image")
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


@app.post("/api/import/{token}/preview")
def import_preview(token: str, body: dict = Body(...)):
    return _import_preview(token, body)


def _import_preview(token: str, body: dict) -> dict:
    items, _ = _build(token, body)
    for item in items:
        eid = item["data"].get("external_id")
        item["existing_id"] = products.find_by_external_id(eid) if eid else None
    return {"items": items}


@app.post("/api/import/{token}/commit")
def import_commit(token: str, body: dict = Body(...)):
    return _import_commit(token, body)  # большой импорт не подвешивает интерфейс


def _import_commit(token: str, body: dict) -> dict:
    with changes.source("Импорт файла"):
        return _import_commit_inner(token, body)


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


# ---------- поставщики ----------

@app.get("/api/suppliers")
def list_suppliers():
    return suppliers.list_suppliers()


@app.post("/api/suppliers")
def create_supplier(name: str = Body("", embed=True)):
    return suppliers.get(suppliers.create(name))


@app.get("/api/suppliers/{supplier_id}")
def get_supplier(supplier_id: int):
    return suppliers.get(supplier_id)


@app.patch("/api/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, data: dict = Body(...)):
    return suppliers.update(supplier_id, data)


@app.delete("/api/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int):
    suppliers.delete(supplier_id)
    return {"ok": True}


@app.post("/api/suppliers/{supplier_id}/source")
async def upload_supplier_source(supplier_id: int, file: UploadFile = File(...)):
    """Загрузить прайс файлом: он станет текущим прайсом поставщика."""
    content = await _read_price_upload(file)
    await asyncio.to_thread(suppliers.store_source, supplier_id, content, file.filename or "price")
    return await asyncio.to_thread(_new_import_token, content, file.filename or "price")


@app.post("/api/suppliers/{supplier_id}/open")
def open_supplier_source(supplier_id: int, refetch: bool = Body(False, embed=True)):
    """Открыть прайс поставщика для настройки колонок (refetch — скачать свежий по ссылке)."""
    path = suppliers.source_path(supplier_id, refetch)
    row = suppliers.get(supplier_id)
    return _new_import_token(path.read_bytes(), row["source_name"] or path.name)


@app.post("/api/suppliers/{supplier_id}/preview")
def preview_supplier(supplier_id: int, force: bool = Body(False, embed=True)):
    """«👁 Что изменится» при обновлении прайса — ничего не записывая."""
    try:
        return suppliers.preview(supplier_id, force)
    except (suppliers.SupplierError, excel.ImportError_, rates.RateError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/suppliers/{supplier_id}/run")
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


# ---------- наценка ----------

@app.get("/api/pricing")
def get_pricing():
    return {"rules": pricing.list_rules(), "rounding": pricing.ROUNDING}


@app.put("/api/pricing")
def save_pricing(rules: list[dict] = Body(..., embed=True)):
    try:
        return {"rules": pricing.save_rules(rules), "rounding": pricing.ROUNDING}
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Ошибка в правилах: {exc}")


@app.post("/api/pricing/test")
def test_pricing(body: dict = Body(...)):
    cost = products.parse_number(body.get("cost"))
    rrp = products.parse_number(body.get("rrp"))
    supplier_id = int(body["supplier_id"]) if body.get("supplier_id") else None
    try:
        price, rule = pricing.Pricer().price(cost, rrp, supplier_id, body.get("category") or "", body.get("currency") or "UAH")
    except rates.RateError as exc:
        return {"error": str(exc)}
    return {"price": price, "rule": rule}


@app.post("/api/products/currencies")
def products_currencies(ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    """В какой валюте сейчас цена у выбранных товаров (закупка/РРЦ — в своей валюте, иначе — валюта цены)."""
    ids = _selected(ids, filter)
    if not ids:
        return {}
    rows = db.query(f"""SELECT UPPER(CASE WHEN (cost_price IS NOT NULL OR rrp IS NOT NULL) AND cost_currency != ''
                        THEN cost_currency ELSE COALESCE(NULLIF(currency, ''), 'UAH') END) AS c, COUNT(*) AS n
                        FROM products WHERE id {db.IN_LIST} GROUP BY c""", (db.as_list(int(i) for i in ids),))
    return {("UAH" if r["c"] == "ГРН" else r["c"]): r["n"] for r in rows}


@app.post("/api/products/prices")
def products_prices(action: str = Body(...), value: float = Body(0), currency: str = Body(""),
                          ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    ids = _selected(ids, filter)
    label = {"as_cost": "опт → закупка", "percent": f"{value:+g}%", "recalc": "пересчёт по наценке"}.get(action, "")
    with changes.source(f"Массово «💲 Цены»: {label}" if label else "Вручную"):
        try:
            return suppliers.bulk_prices(ids, action, value, currency)
        except suppliers.SupplierError as exc:
            raise HTTPException(400, str(exc))


@app.post("/api/pricing/preview")
def preview_pricing(rules: list[dict] | None = Body(None, embed=True)):
    """Сколько цен изменит пересчёт (с правилами из формы, если переданы) и сколько уйдёт на Prom."""
    rates.prefetch()

    def run():
        if rules is not None:
            pricing.save_rules(rules)
        return suppliers.recalc_prices()
    try:
        with changes.source("Пересчёт по наценке"):
            return changes.preview(run)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Ошибка в правилах: {exc}")


@app.post("/api/rates/preview")
def preview_rates(data: dict = Body(...)):
    """Сколько цен изменит новый курс (настройки из формы применяются понарошку)."""
    rates.prefetch(force=True)  # курс НБУ — до пробного прогона, даже если сейчас сохранён «свой» курс

    def run():
        rates.save_settings(data)
        return suppliers.recalc_prices()
    try:
        with changes.source("Курс валют"):
            return changes.preview(run)
    except rates.RateError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/pricing/apply")
def apply_pricing():
    with changes.source("Пересчёт по наценке"):
        result = suppliers.recalc_prices()
    rates.mark_applied()
    return result


# ---------- обновления и резервные копии ----------

@app.get("/api/update/check")
def update_check():
    return updater.check()


@app.post("/api/update/install")
def update_install():
    version = updater.update()
    restarting = runtime.request_restart()
    return {"version": version, "restarting": restarting}


@app.get("/api/backups")
def list_backups():
    return {"items": backup.list_backups(), "last": db.get_setting("last_backup_at"), "can_restart": runtime.can_restart()}


@app.post("/api/backups")
def create_backup():
    return backup.create("manual")


@app.get("/api/backups/{name}")
def download_backup(name: str):
    path = backup.path_of(name)
    return FileResponse(path, filename=name, media_type="application/zip")


@app.post("/api/backups/{name}/restore")
def restore_backup(name: str):
    backup.stage_restore(name)
    return {"restarting": runtime.request_restart()}


@app.post("/api/backups/upload")
def restore_uploaded_backup(file: UploadFile = File(...)):
    # копия может весить гигабайты (фото) — пишем на диск частями, а не читаем целиком в память
    backup.stage_restore_stream(file.file)
    return {"restarting": runtime.request_restart()}


@app.post("/api/shutdown")
def shutdown(request: Request):
    """Выключение из меню значка у часов. Только с этого компьютера."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403)
    return {"stopping": runtime.request_stop()}


@app.post("/api/restart")
def restart():
    if not runtime.request_restart():
        raise HTTPException(400, "Программа запущена не через START.bat — перезапустите её вручную")
    return {"restarting": True}


# ---------- расписание и автозапуск ----------

@app.get("/api/schedules")
def list_schedules():
    return {"items": schedule.list_schedules(), "missed_actions": schedule.MISSED_ACTIONS, "days": schedule.DAY_NAMES,
            "autostart": {"supported": autostart.supported(), "enabled": autostart.enabled()}}


@app.post("/api/schedules")
def create_schedule(data: dict = Body(default={})):
    return schedule.create(data)


@app.patch("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: int, data: dict = Body(...)):
    return schedule.update(schedule_id, data)


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: int):
    schedule.delete(schedule_id)
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/resolve")
def resolve_schedule(schedule_id: int, decision: str = Body(..., embed=True)):
    return schedule.resolve(schedule_id, decision)


@app.post("/api/autostart")
def set_autostart(enabled: bool = Body(..., embed=True)):
    return {"enabled": autostart.set_enabled(enabled)}


# ---------- каталог с Prom ----------

@app.get("/api/prom/catalog")
def prom_catalog_state():
    return promcatalog.state()


@app.post("/api/prom/catalog")
async def prom_catalog_load():
    if promcatalog.state().get("running") and promcatalog._lock.locked():
        raise HTTPException(409, "Каталог уже загружается")
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    task = asyncio.create_task(_catalog_bg())
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.sleep(0.05)
    return promcatalog.state()


async def _catalog_bg() -> None:
    try:
        await promcatalog.run(sync.make_client)
    except PromError:
        pass
    except Exception as exc:
        log.exception("Сбой загрузки каталога Prom")
        promcatalog._set_state(running=False, error=f"Внутренняя ошибка: {exc}")


# ---------- массовый ИИ ----------

@app.get("/api/ai/bulk")
def ai_bulk_jobs():
    return {"items": aibulk.list_jobs(), "rate": aibulk.rate_per_minute()}


@app.post("/api/ai/bulk")
def ai_bulk_create(action: str = Body(...), instruction: str = Body(""), only_empty: bool = Body(True),
                         ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return aibulk.create(_selected(ids, filter), action, instruction, only_empty)


@app.post("/api/ai/bulk/{job_id}/status")
def ai_bulk_status(job_id: int, status: str = Body(..., embed=True)):
    return aibulk.set_status(job_id, status)


@app.post("/api/ai/bulk/{job_id}/revert")
def ai_bulk_revert(job_id: int):
    return {"restored": aibulk.revert(job_id)}


# ---------- заказы и Telegram ----------

@app.get("/api/orders")
def list_orders(status: str = "", limit: int = 100, offset: int = 0):
    return {**orders.list_orders(status, max(1, min(limit, 500)), max(0, offset)), "statuses": orders.STATUSES,
            "settable": orders.SETTABLE, "cancel_reasons": orders.CANCEL_REASONS, "enabled": orders.enabled()}


@app.post("/api/orders/refresh")
async def refresh_orders():
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    try:
        async with sync.make_client() as client:
            new = await orders.poll(client)
    except PromError as exc:
        raise HTTPException(502, str(exc))
    return {"new": new}


@app.post("/api/orders/seen")
def orders_seen(ids: list[int] | None = Body(None, embed=True)):
    """ids — эти заказы; без ids — все. Пустой список — ни одного (раньше он помечал все)."""
    orders.mark_seen(ids)
    return {"ok": True}


@app.post("/api/orders/{order_id}/status")
async def order_status(order_id: int, status: str = Body(...), reason: str = Body(""), text: str = Body("")):
    try:
        async with sync.make_client() as client:
            return await orders.set_status(client, order_id, status, reason, text)
    except PromError as exc:
        raise HTTPException(400 if exc.status in (None, 400, 422) else 502, str(exc))


@app.get("/api/telegram/recipients")
def telegram_recipients():
    return {"items": notify.recipients(), "events": notify.EVENTS, "token_set": bool(notify.token())}


@app.post("/api/telegram/candidates")
def telegram_candidates():
    """Кто недавно писал боту — чтобы добавить в получатели одним нажатием."""
    known = {r["chat_id"] for r in notify.recipients()}
    chats = notify.recent_chats(notify.token())
    return {"items": [c for c in chats if c["chat_id"] not in known]}


@app.post("/api/telegram/recipients")
def telegram_add(chat_id: str = Body(...), name: str = Body(""), events: list[str] | None = Body(None)):
    return notify.add_recipient(chat_id, name, events)


@app.patch("/api/telegram/recipients/{rid}")
def telegram_update(rid: int, events: list[str] | None = Body(None), name: str | None = Body(None)):
    notify.update_recipient(rid, events, name)
    return {"items": notify.recipients()}


@app.delete("/api/telegram/recipients/{rid}")
def telegram_remove(rid: int):
    notify.remove_recipient(rid)
    return {"items": notify.recipients()}


@app.post("/api/telegram/recipients/{rid}/test")
def telegram_test(rid: int):
    r = next((x for x in notify.recipients() if x["id"] == rid), None)
    if r is None:
        raise HTTPException(404)
    notify.send_to(r["chat_id"], "✅ Prom Loader: уведомления подключены. "
                            "Подписки: " + ", ".join(notify.EVENTS[e] for e in r["events"]))
    return {"ok": True}


# ---------- поддержка ----------

@app.get("/api/support")
def support_list():
    return {"items": support.list_tickets(), "configured": support.configured()}


@app.get("/api/support/diagnostics")
def support_diagnostics():
    """Что именно уйдёт вместе с обращением (показываем пользователю)."""
    d = support.diagnostics()
    d["log_tail"] = d["log_tail"][-20:]
    return d


@app.post("/api/support")
async def support_create(description: str = Form(""), contact: str = Form(""), page: str = Form(""),
                         include_diagnostics: bool = Form(True), client: str = Form("{}"),
                         files: list[UploadFile] = File(default=[])):
    try:
        client_info = json.loads(client)
    except ValueError:
        client_info = {}
    payload = [(f.filename or "file", await f.read(support.MAX_FILE + 1)) for f in files]
    ticket = await asyncio.to_thread(support.create, description, contact, page, payload, include_diagnostics, client_info)
    message = "Обращение сохранено."
    if support.configured():
        try:
            ticket = await asyncio.to_thread(support.send, ticket["id"])
            message = "Обращение отправлено разработчику."
        except support.SupportError as exc:
            message = str(exc)
    else:
        message += " Отправка разработчику пока не настроена — скачайте архив обращения и перешлите его."
    return {"ticket": ticket, "message": message}


@app.post("/api/support/{ticket_id}/send")
def support_send(ticket_id: int):
    return support.send(ticket_id)


@app.get("/api/support/{ticket_id}/archive")
def support_archive(ticket_id: int):
    content = support.archive(ticket_id)
    return Response(content, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="promloader-obrashchenie-{ticket_id}.zip"'})


@app.delete("/api/support/{ticket_id}")
def support_delete(ticket_id: int):
    support.delete(ticket_id)
    return {"ok": True}


@app.post("/api/support/channel/chats")
def support_chats(token: str = Body("", embed=True)):
    """Для разработчика: найти свой чат, написав боту поддержки (можно до сохранения токена)."""
    return {"items": notify.recent_chats(token.strip() or support.channel()["token"])}
