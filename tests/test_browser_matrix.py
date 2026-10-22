"""Обход всех страниц в настоящем браузере: роли × экраны (компьютер 1366 и телефон 390) × тема.

На каждой странице (tests/browser_audit.js): нет ошибок JS и неожиданных отказов сервера, ничего не вылезает вбок,
не обрезано и не закрыто другим, на телефоне кнопки не мельче пальца, текст читается (контраст WCAG). Роль без
права видит запертые кнопки, а не кнопки, на которые сервер ответит «Недостаточно прав». Ссылка на закрытый раздел —
понятная страница «Нет доступа» с выходом на главную.

Так в 2.29.3 нашлись: бледные бейджи статусов и серый текст, мелкие кнопки на телефоне, незапертые кнопки заказов
у «Только просмотр», тупиковая страница отказа. Нет Playwright или Chromium — тест пропускается."""

import io
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from PIL import Image  # noqa: E402

from promloader import branding, db, orders, products, suppliers, users  # noqa: E402

from .conftest import make_image  # noqa: E402
from .test_browser import PASSWORD, _browser, _free_port  # noqa: E402
from .test_novaposhta import prom_order  # noqa: E402

AUDIT = (Path(__file__).parent / "browser_audit.js").read_text(encoding="utf-8")
ROLES = {"admin": "admin", "operator": "operator", "viewer": "viewer"}
PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
DESKTOP = dict(viewport={"width": 1366, "height": 768})
# закрытые разделы по прямой ссылке: страница «Нет доступа», а не ошибка
REFUSED = {"operator": {"/import", "/settings", "/support"}, "viewer": {"/import", "/settings", "/support"}}


def _jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def shop(data_dir):
    """Магазин со всем, что бывает: товары во всех статусах и с ошибкой, фото, поставщик, заказы, чат, логотип."""
    import threading
    import time

    import uvicorn

    from promloader.main import app

    for login in ROLES:
        users.create(login, PASSWORD, ROLES[login])
    db.set_setting("prom_token", "fake")
    sup = suppliers.create("Склад")
    ids = []
    for n in range(12):
        pid = products.create({"name": f"Ялинка штучна {n + 1}", "price": 300 + n, "external_id": f"M-{n:02d}",
                               "group_name": "Ялинки", "quantity": n % 4})
        if n % 2:
            products.add_image_file(pid, _jpeg("#2e7d32"))
        ids.append(pid)
    with db.tx() as c:
        for i, pid in enumerate(ids):
            st = ["draft", "ready", "synced", "error"][i % 4]
            c.execute("UPDATE products SET status = ?, supplier_id = ?, synced_at = CASE WHEN ? = 'synced' THEN ? END "
                      "WHERE id = ?", (st, sup, st, db.now(), pid))
        c.execute("UPDATE products SET last_error = 'Prom: не вказана категорія' WHERE status = 'error'")
        c.execute("INSERT INTO chat_rooms (room_ident, buyer_name, last_message, last_at, unread, updated_at) "
                  "VALUES ('r1', 'Олена', 'Добрий день!', ?, 1, ?)", (db.now(), db.now()))
        c.execute("INSERT INTO chat_messages (id, room_ident, sender, body, is_my, created_at, type, status) "
                  "VALUES (1, 'r1', 'buyer', 'Добрий день!', 0, ?, 'message', 'unread')", (db.now(),))
    orders.store([prom_order(101), prom_order(102, status="received"), prom_order(103, status="delivered")],
                 notify_new=False)
    branding.save_logo(make_image(size=(300, 120), color="#e6a700"))  # жёлтый логотип — цвет кнопок должен читаться
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    pages = ["/today", "/", "/?status=error", f"/product?id={ids[1]}", "/orders", "/chat", "/suppliers",
             f"/supplier?id={sup}", "/reports", "/pricing", "/categories", "/changes", "/import", f"/diagnose?id={ids[2]}",
             "/support", "/settings"]
    yield f"http://127.0.0.1:{port}", pages
    srv.should_exit = True
    thread.join(5)


def _visit(ctx, url, path, problems, where, touch):
    """Каждая страница — в новой вкладке: эмуляция Chromium после переходов забывает, что экран сенсорный."""
    page = ctx.new_page()
    errors, refused = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: refused.append(f"{r.status} {r.request.method} {r.url.replace(url, '')}")
            if r.status >= 400 and r.url.startswith(url) else None)
    page.goto(url + path)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(250)
    a = page.evaluate(AUDIT, {"touch": touch, "hit": 40, "uk": False})
    tag = f"{where} {path}"
    for kind, items in (("ошибка JS", errors), ("ответ сервера", refused), ("обрезано", a["clipped"]),
                        ("закрыто другим", a["covered"]), ("мелко для пальца", a["smallTargets"]),
                        ("нечитаемо", a["lowContrast"]), ("битая картинка", a["brokenImg"])):
        problems += [f"{tag}: {kind}: {x}" for x in items]
    if a["overflowX"]:
        problems.append(f"{tag}: шире экрана на {a['overflowX']} px")
    return page


def _login(ctx, url, login):
    page = ctx.new_page()
    page.goto(url + "/login")
    page.fill("#login-name", login)
    page.fill("#password", PASSWORD)
    page.click("#go")
    page.wait_for_url(url + "/today")
    page.close()


@pytest.mark.parametrize("login", list(ROLES))
def test_every_page_every_role_desktop_and_phone(shop, login):
    url, pages = shop
    problems: list[str] = []
    with playwright.sync_playwright() as p:
        browser = _browser(p)
        for device, opts, theme in (("компьютер", DESKTOP, "light"), ("телефон", PHONE, "light"),
                                    ("компьютер тёмная", DESKTOP, "dark")):
            if theme == "dark" and login != "admin":
                continue
            ctx = browser.new_context(**opts)
            ctx.add_init_script(f"try{{localStorage.setItem('promloader-theme','{theme}')}}catch(e){{}}")
            _login(ctx, url, login)
            for path in pages:
                if path in REFUSED.get(login, ()):
                    page = ctx.new_page()
                    page.goto(url + path)
                    assert "Нет доступа" in page.inner_text("main"), path
                    assert page.locator('main a[href="/today"]').count() == 1  # выход с тупиковой страницы
                    page.close()
                    continue
                _visit(ctx, url, path, problems, f"{login}/{device}", "has_touch" in opts).close()
            ctx.close()
        browser.close()
    assert problems == [], "\n".join(problems[:40])


def test_view_only_role_sees_locked_order_and_chat_actions(shop):
    """«Только просмотр»: смена статуса заказа, ТТН и «Отправить» в чате — с замком, нажатие объясняет, а не 403."""
    url, _ = shop
    with playwright.sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context(**DESKTOP)
        _login(ctx, url, "viewer")
        page = ctx.new_page()
        refused = []
        page.on("response", lambda r: refused.append(r.url) if r.status == 403 else None)
        page.goto(url + "/orders")
        page.wait_for_selector("[data-set]")
        for sel in ("[data-set]", "[data-ttn-create]"):
            btns = page.locator(sel)
            assert btns.count() and all(btns.nth(i).get_attribute("data-perm") == "orders" for i in range(btns.count()))
        page.locator("[data-set]").first.click()
        page.wait_for_selector("text=Нет права")
        page.goto(url + "/chat")
        page.click("#rooms button >> nth=0")
        page.wait_for_selector("#send")
        assert page.get_attribute("#send", "data-perm") == "chat"
        page.goto(url + "/pricing")  # «Проверить расчёт» только считает — работает и без права менять
        page.wait_for_selector("#t-result:not(:empty)")
        assert refused == []
        browser.close()
