"""Веб-сервер: API + статические страницы."""

import asyncio
import base64
import importlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import (
    ai,
    aibulk,
    autostart,
    backup,
    changes,
    cloudbackup,
    chat,
    config,
    db,
    excel,
    feed,
    housekeeping,
    notify,
    novaposhta,
    orders,
    phone,
    phototunnel,
    products,
    promcatalog,
    r2,
    rates,
    reports,
    schedule,
    stock,
    suppliers,
    support,
    sync,
    updater,
    users,
)
from .prom_api import PromError
from .routes import ROUTERS
from .web import (  # noqa: F401 — _page_html и MAX_UPLOAD нужны тестам
    MAX_UPLOAD,
    STATIC,
    _background,
    _page,
    _page_html,
    log,
)

PAGES = {
    "/": "index.html", "/product": "product.html", "/import": "import.html", "/settings": "settings.html",
    "/suppliers": "suppliers.html", "/supplier": "supplier.html", "/pricing": "pricing.html", "/orders": "orders.html",
    "/support": "support.html", "/diagnose": "diagnose.html", "/changes": "changes.html",
    "/categories": "categories.html", "/reports": "reports.html", "/chat": "chat.html",
}
SUPPLIER_CHECK_SECONDS = 30
MAINTENANCE_SECONDS = 3600
UPDATE_CHECK_HOURS = 12

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_LIMIT = 5 * 1024 * 1024
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def setup_log_file(data_dir: Path) -> logging.Handler:
    """Журнал программы — data/logs/promloader.log, не больше 5 МБ × 3 файла (раньше рос, пока программа работала).
    Под значком у часов вывод в консоль не нужен: он уходил бы в тот же журнал второй раз."""
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "promloader_file", False):
            root.removeHandler(h)
            h.close()
    folder = data_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "promloader.log", maxBytes=LOG_LIMIT, backupCount=2, encoding="utf-8",
                                  delay=True)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(config.RedactFilter())  # токен бота, ключи в ссылках — не в журнал
    handler.promloader_file = True
    root.addHandler(handler)
    if os.environ.get("PROMLOADER_TRAY"):
        for h in root.handlers:
            if type(h) is logging.StreamHandler:
                h.setLevel(logging.WARNING)
    return handler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # восстановление копии, выбранное в настройках: START.bat делает это сам, а в Docker программа запускается
    # напрямую — без этой строки копия там никогда не применялась
    setup_log_file(db.default_dir())
    if backup.apply_pending(db.default_dir()):
        log.warning("Данные восстановлены из резервной копии")
    db.init()
    users.service_token()  # ключ для значка у часов («Перезапустить», «Выключить») — до первого его запроса
    for name in ("photo-tunnel", "phone-tunnel"):  # cloudflared, оставшийся от прошлого запуска, держал бы старый адрес
        await asyncio.to_thread(phototunnel.kill_stale, name)
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
                 asyncio.create_task(rates_worker(stop)), asyncio.create_task(notify_worker(stop)),
                 asyncio.create_task(chat_worker(stop)), asyncio.create_task(phone_worker(stop)),
                 asyncio.create_task(np_worker(stop)), asyncio.create_task(catalog_worker(stop))]
    try:
        yield
    finally:
        stop.set()
        # ошибка одного воркера не должна мешать остановке остальных и закрытию туннеля (иначе cloudflared остаётся)
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                log.error("Воркер завершился с ошибкой: %r", result)
        phototunnel.tunnel.close()
        phone.tunnel.close()


async def rates_worker(stop: asyncio.Event) -> None:
    """Раз в час: изменился курс (НБУ обновляется раз в день) — пересчитать цены и отправить новые на Prom."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=30)
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            if not db.paused():
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


async def notify_worker(stop: asyncio.Event) -> None:
    """Досылает уведомления Telegram, которые не ушли сразу (нет связи, Telegram недоступен)."""
    while not stop.is_set():
        try:
            if not db.paused():  # после переезда уведомления шлёт новый компьютер — без дублей
                await asyncio.to_thread(notify.flush)
                await asyncio.to_thread(reports.maybe_daily_summary)
                await asyncio.to_thread(stock.maybe_notify)
        except Exception:
            log.exception("Сбой отправки уведомлений Telegram")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def chat_worker(stop: asyncio.Event) -> None:
    """Раз в минуту — новые сообщения покупателей в чате Prom (если указан токен)."""
    while not stop.is_set():
        try:
            if chat.enabled() and not db.paused():
                async with sync.make_client() as client:
                    await chat.poll(client)
        except PromError:
            pass  # ошибка сохранена (chat_error) и видна на странице чата
        except Exception:
            log.exception("Сбой опроса чата")
        try:
            await asyncio.wait_for(stop.wait(), timeout=chat.POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def phone_worker(stop: asyncio.Event) -> None:
    """Держит открытым адрес для телефона через Cloudflare, пока включён доступ из интернета."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(phone.keep_tunnel)
        except Exception:
            log.exception("Сбой туннеля для телефона")
        try:
            users.cleanup_reset()  # код сброса пароля просрочен — файл с кодом не лежит дольше нужного
        except Exception:
            log.exception("Не удалось убрать просроченный код сброса пароля")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


async def np_worker(stop: asyncio.Event) -> None:
    """Раз в 30 минут — статусы посылок Новой Почты (ключ не нужен): прибыла, не забирают, отказ."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=60)
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            if not db.paused() and novaposhta.enabled():
                await asyncio.to_thread(novaposhta.track)
        except Exception:
            log.exception("Сбой отслеживания посылок")
        try:
            await asyncio.wait_for(stop.wait(), timeout=novaposhta.TRACK_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


async def catalog_worker(stop: asyncio.Event) -> None:
    """Автосинхронизация с кабинетом Prom: раз в N часов («Настройки») — загрузка каталога, как по кнопке."""
    while not stop.is_set():
        try:
            if await asyncio.to_thread(promcatalog.autosync_due):
                await promcatalog.run_auto(sync.make_client)
        except Exception:
            log.exception("Сбой автосинхронизации с Prom")
            promcatalog._set_state(running=False)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def orders_worker(stop: asyncio.Event) -> None:
    """Раз в 5 минут забирает заказы с Prom (если указан токен)."""
    while not stop.is_set():
        try:
            if orders.enabled() and not db.paused():
                async with sync.make_client() as client:
                    await orders.poll(client)
        except PromError:
            pass  # ошибка сохранена и видна на странице заказов
        except Exception:
            log.exception("Сбой опроса заказов")
        # загружаются все заказы магазина (первый раз или после долгого простоя) — продолжаем без долгой паузы
        catching_up = bool(db.get_setting("orders_cursor"))
        try:
            await asyncio.wait_for(stop.wait(), timeout=20 if catching_up else orders.POLL_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


async def ai_worker(stop: asyncio.Event) -> None:
    """Массовый ИИ: по одному товару, не чаще лимита провайдера; при 429 — пауза на минуту."""
    pacer = aibulk.Pacer()
    while not stop.is_set():
        delay = 3.0
        try:
            if not db.paused() and await asyncio.to_thread(aibulk.has_work):
                wait = pacer.wait_time()
                if wait > 0:
                    delay = wait
                else:
                    pacer.mark()
                    result = await asyncio.to_thread(aibulk.process_one)
                    if result == "rate_limited":
                        # лимит на сутки ждём до сброса (но проверяем раз в полчаса), перегрузку — минуту
                        err = aibulk.last_limit
                        delay = min(max(aibulk.RATE_LIMIT_PAUSE, (err.retry_after or 0) if err else 0), 1800)
                        aibulk.pause_running(f"{err or 'ИИ-сервис занят'} — продолжу сам через {ai.human_wait(delay)}")
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
            for event in ([] if db.paused() else await asyncio.to_thread(schedule.tick)):
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
                info = await asyncio.to_thread(backup.create, "daily")
                await asyncio.to_thread(cloudbackup.after_backup, info)  # если включено — копия и в облако R2
                await asyncio.to_thread(housekeeping.run)  # после копии: удалённое останется в ней
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
            due = [] if db.paused() else await asyncio.to_thread(suppliers.due)
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


class StaticNoCache(StaticFiles):
    """Скрипты и стили: браузер каждый раз сверяет их с программой (ответ 304 — на localhost мгновенно). Без этого
    после обновления он часами брал старые common.js и app.css из кеша к новому HTML — например, пропадало меню."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Prom Loader", lifespan=lifespan)
app.mount("/static", StaticNoCache(directory=STATIC), name="static")


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


_REFUSED_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Prom Loader</title>
<style>body{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2129;margin:0;padding:40px 20px}
.box{max-width:440px;margin:0 auto;background:#fff;border-radius:10px;padding:24px}</style></head>
<body><div class="box"><h2>Prom Loader</h2><p>{text}.</p></div></body></html>"""


# без входа: страницы входа и первого запуска, их запросы, значок и файлы приложения на телефоне
PUBLIC_PATHS = {"/login", "/setup", "/api/login", "/api/logout", "/api/setup", "/api/password/reset-code",
                "/api/password/reset", "/favicon.ico", "/manifest.webmanifest", "/sw.js", "/shop-logo.png"}


def _refused(request: Request, text: str, status: int = 403, **extra):
    if request.method == "GET" and not request.url.path.startswith("/api/"):
        return HTMLResponse(_REFUSED_PAGE.replace("{text}", text), status_code=status)
    # с кодировкой: без неё Safari показывал русский текст кракозябрами
    return JSONResponse({"detail": text, **extra}, status_code=status, media_type="application/json; charset=utf-8")


def _next_url(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _sign_in_gate(request: Request, access: str):
    """Вход по логину и паролю — для всех, в том числе на самом компьютере. Пока пользователей нет (первый запуск
    или обновление со старой версии), программа просит создать администратора — но только на самом компьютере
    (или за паролем APP_PASSWORD на сервере): иначе любой в домашней сети успел бы стать администратором первым."""
    path = request.url.path
    user = users.session_user(request.cookies.get(users.COOKIE))
    request.state.user = user
    if user is None:
        if path in PUBLIC_PATHS or path.startswith("/static/") or users.service_ok(request):
            return None
        no_users = not users.any_users()
        if no_users and access != "local":
            return _refused(request, "Программу ещё не настроили: откройте её на компьютере, где она установлена, и "
                                     "создайте пароль администратора", setup=True)
        if request.method == "GET" and not path.startswith("/api/"):
            return RedirectResponse(("/setup?" if no_users else "/login?") + urlencode({"next": _next_url(request)}), 303)
        text = "Сначала создайте пароль администратора" if no_users else "Нужно войти — введите логин и пароль"
        return JSONResponse({"detail": text, "login": True, "setup": no_users}, status_code=401)
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return None  # вход, выход, сброс пароля — и тем, кто уже вошёл (любой роли)
    perm = users.required(request.method, path)
    if not users.can(user, perm):
        log.info("Нет прав (%s): %s %s — %s", perm, request.method, path, user["login"])
        needed = ", ".join(users.PERM_LABELS.get(x, x) for x in ((perm,) if isinstance(perm, str) else perm))
        return _refused(request, f"Недостаточно прав ({needed}). Ваша роль — "
                                 f"«{user['role_label']}». Обратитесь к администратору программы", forbidden=True)
    return None


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    """Защита от чужих сайтов, открытых в том же браузере, и вход по логину и паролю.

    - Программа отвечает только по своему адресу (localhost). Это закрывает «DNS rebinding»: чужой сайт не может
      прочитать, например, резервную копию с токенами. Если интерфейс открыт по сети и закрыт паролем
      (APP_PASSWORD), адрес не проверяется; другие адреса можно разрешить в PROMLOADER_ALLOWED_HOSTS.
    - Изменяющие запросы (POST/PUT/PATCH/DELETE) принимаются только со страниц самой программы: браузер
      сообщает, откуда запрос (Origin, Sec-Fetch-Site), и запрос с чужого сайта отклоняется.
    - Всё, кроме фото и фида для Prom, — только после входа, и только то, что разрешает роль пользователя.
    """
    path = request.url.path
    if path.startswith(OPEN_PATHS):
        return await call_next(request)
    if os.environ.get("APP_PASSWORD"):
        access = "local"  # сервер за паролем APP_PASSWORD (basic_auth ниже): дальше — вход пользователя
    else:
        access = phone.client_kind(request, _allowed_hosts())
        if access is None:
            return _refused(request, "Программа открывается только по адресу http://localhost. Для доступа с телефона "
                                     "включите его в «Настройках» → «Доступ с телефона»")
    request.state.access = access
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site", "")
        if site == "cross-site" or (origin is not None and not _same_origin(origin, request)):
            log.warning("Отклонён запрос %s %s с чужой страницы (%s)", request.method, path, origin or site)
            return JSONResponse({"detail": "Запрос отклонён: он пришёл не со страницы программы"}, status_code=403)
    refused = _sign_in_gate(request, access)
    if refused is not None:
        return refused
    user = request.state.user
    token = changes._user.set((user["name"] or user["login"]) if user else "")
    try:
        return await call_next(request)
    finally:
        changes._user.reset(token)


# перебор пароля APP_PASSWORD (сервер в сети, Docker): 5 неверных с одного адреса за 5 минут — пауза
basic_limiter = phone.LoginLimiter(per_client=5, total=50, window=300, what="APP_PASSWORD")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Если задан APP_PASSWORD — интерфейс закрыт паролем. Фото и фид остаются открытыми: их забирает Prom."""
    password = os.environ.get("APP_PASSWORD")
    path = request.url.path
    if password and not (path.startswith("/media/") or path.startswith("/feed/")):
        who = phone.client_key(request)
        if basic_limiter.blocked(who):
            return JSONResponse({"detail": "Слишком много неверных паролей — подождите 5 минут"}, status_code=429)
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
            if header:  # браузер сначала спрашивает без пароля — это не попытка
                basic_limiter.failed(who)
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
@app.exception_handler(phone.PhoneError)
@app.exception_handler(users.UserError)
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

for route, filename in PAGES.items():
    app.get(route, include_in_schema=False)(_page(filename))


# приложение на телефоне: манифест и service worker — с корня сайта (иначе service worker не охватит все страницы);
# открыты без входа: браузер запрашивает манифест без куки, секретов в них нет
@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


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
    expected = config.get("feed_key") or ""
    # пустой ключ не открывает фид никогда: compare_digest(b"", b"") — это True
    if not key or not expected or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(403, "Неверный ключ фида")
    return Response(feed.build(None, config.public_base_url()), media_type="application/xml")


# ---------- обработчики API по разделам (routes/) ----------
# Порядок подключения = прежний порядок в файле: от него зависит, какой путь совпадёт первым
# (например, /api/reports/{tab}.xlsx раньше /api/reports/{tab}).

for _name in ROUTERS:
    app.include_router(importlib.import_module(f".routes.{_name}", __package__).router)


def all_routes(routes=None) -> list:
    """Все пути программы, включая подключённые из routes/ (FastAPI держит их во вложенных роутерах)."""
    out = []
    for r in app.routes if routes is None else routes:
        if hasattr(r, "original_router"):
            out += all_routes(r.original_router.routes)
        else:
            out.append(r)
    return out
