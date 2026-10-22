"""Проверка в настоящем браузере (Chromium через Playwright): то, что видно только на странице — заготовка при
загрузке, «↩ Отменить», горячие клавиши, «← Пред. / След. →», список помнит место, обновление без мигания,
телефон без прокрутки вбок, украинский интерфейс. Без интернета: сервер программы — на этом же компьютере.

Нужен пакет playwright и Chromium (в CI — отдельный шаг). Нет их — тест пропускается."""

import socket
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from promloader import branding, db, orders, products, users  # noqa: E402

from .conftest import make_image  # noqa: E402

from .test_novaposhta import prom_order  # noqa: E402

PASSWORD = "browser-password-1"
PAGES = ["/today", "/", "/orders", "/chat", "/reports", "/suppliers", "/changes", "/categories", "/import", "/pricing",
         "/settings", "/product?id=1"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(data_dir):
    import uvicorn

    from promloader.main import app

    users.create("admin", PASSWORD, "admin")
    users.create("viewer", PASSWORD, "viewer")
    ids = [products.create({"name": f"Товар {n:02d}", "price": 100 + n, "external_id": f"T-{n:02d}"}) for n in range(1, 31)]
    with db.tx() as c:
        c.execute("UPDATE products SET status = 'ready'")
    orders.store([prom_order(101), prom_order(102, status="received")], notify_new=False)
    branding.save_logo(make_image(size=(300, 120)))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", ids
    srv.should_exit = True
    thread.join(5)


def _browser(p):
    try:
        return p.chromium.launch()
    except Exception as exc:  # браузер не установлен (обычный прогон без Playwright)
        pytest.skip(f"нет Chromium: {exc}")


def _login(ctx, url, errors):
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
    page.goto(url + "/login")
    page.fill("#login-name", "admin")
    page.fill("#password", PASSWORD)
    page.click("#go")
    page.wait_for_url(url + "/today")  # после входа — главная «Сегодня»
    page.goto(url + "/")
    return page


def _hit(page, selector) -> bool:
    """Элемент действительно под мышью (не обрезан родителем и не закрыт другим) — is_visible этого не видит."""
    return page.evaluate("""s => { const el = document.querySelector(s); const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !!hit && (hit === el || el.contains(hit)); }""", selector)


def _contrast(page, selector) -> float:
    """Контраст текста и фона элемента (WCAG: 4.5 — читается)."""
    return page.evaluate("""s => { const cs = getComputedStyle(document.querySelector(s));
        const rgb = (c) => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
        const lum = (c) => { const [r, g, b] = rgb(c).map((v) => { v /= 255;
            return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
        const [a, b] = [lum(cs.color), lum(cs.backgroundColor)].sort((x, y) => y - x);
        return (a + 0.05) / (b + 0.05); }""", selector)


def _status(pid):
    return db.query_one("SELECT status FROM products WHERE id = ?", (pid,))["status"]


def test_interface_in_a_real_browser(server):
    url, ids = server
    errors: list[str] = []
    with playwright.sync_playwright() as p:
        browser = _browser(p)

        # ---------- компьютер ----------
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        login = ctx.new_page()
        login.goto(url + "/login")
        login.wait_for_selector("img.login-logo")  # логотип магазина — уже на странице входа, над формой
        login.close()
        page = _login(ctx, url, errors)
        page.wait_for_selector("tr.item")
        assert page.locator("tr.item").count() == 30
        page.wait_for_selector(".topbar .logo.with-logo img")
        assert "/shop-logo.png" in page.get_attribute('link[rel="icon"]', "href")

        # эмодзи в кнопках — значки Lucide; «Сегодня» показывает, что требует внимания
        assert page.locator("button svg.i, a svg.i").count() > 3
        page.goto(url + "/today")
        page.wait_for_selector(".attention-item")
        assert "Готовы к отправке на Prom" in page.inner_text("#attention")
        page.goto(url + "/")
        page.wait_for_selector("tr.item")
        # тёмная тема из меню пользователя — сразу, без перезагрузки, и запоминается
        page.click("#user-menu summary")
        page.click('[data-theme-pick="dark"]')
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        page.reload()
        page.wait_for_selector("tr.item")
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        # выбранный период в «Отчётах» читается: текст не того же цвета, что фон
        page.goto(url + "/reports")
        page.wait_for_selector("#periods .chip.active")
        assert _contrast(page, "#periods .chip.active") > 4.5
        page.goto(url + "/")
        page.wait_for_selector("tr.item")
        page.click("#user-menu summary")
        page.click('[data-theme-pick="light"]')
        page.mouse.click(5, 400)

        # конвейер вместо статусов: этап «Готов к отправке» → своя кнопка; массовые действия — только при выборе
        assert not page.locator('#bulk [data-action="send"]').is_visible()
        page.click('#pipeline [data-stage="ready"]')
        page.wait_for_selector("#send-stage")
        assert "(30)" in page.inner_text("#send-stage")
        page.click('#pipeline [data-stage=""]')
        page.wait_for_selector("#stage-action.hidden", state="attached")
        # редкие разделы — в «Ещё ▾»
        assert not page.locator('.topbar nav a[href="/changes"]').is_visible()
        page.click(".nav-more summary")
        assert page.locator('.topbar nav a[href="/changes"]').is_visible()
        # не обрезан меню, которое в окне до 1440 px прокручивается вбок: пункт виден и нажимается на самом деле
        assert _hit(page, '.topbar nav a[href="/changes"]')
        page.mouse.click(5, 400)  # клик мимо — меню закрылось
        assert not page.locator('.topbar nav a[href="/changes"]').is_visible()

        # заготовка, пока список в пути (ответ задерживаем)
        page.route("**/api/products?*", lambda route: (time.sleep(0.6), route.continue_()))
        page.goto(url + "/?q=Товар")
        page.wait_for_selector("tr.skeleton")
        page.wait_for_selector("tr.item")
        assert page.locator("tr.skeleton").count() == 0
        page.unroute("**/api/products?*")
        page.goto(url + "/")
        page.wait_for_selector("tr.item")

        # фоновое обновление не пересоздаёт строки, которые не изменились
        page.evaluate("document.querySelectorAll('tr.item').forEach((tr) => (tr.__kept = true))")
        page.evaluate("loadProducts()")
        page.wait_for_timeout(500)
        assert page.evaluate("[...document.querySelectorAll('tr.item')].every((tr) => tr.__kept)")

        # «В черновики» сразу, а «↩ Отменить» возвращает как было
        first = [int(x) for x in page.eval_on_selector_all("tr.item", "rows => rows.slice(0, 2).map(r => r.dataset.id)")]
        for pid in first:
            page.check(f'tr.item[data-id="{pid}"] .sel')
        # на компьютере «Ещё ▾» раскрыто и без заголовка — кнопки видны сразу
        page.click('#bulk [data-action="draft"]')
        page.wait_for_selector(".toast-undo")
        assert [_status(x) for x in first] == ["draft", "draft"]
        page.click(".toast-undo")
        page.wait_for_selector("text=Отменено: 2")
        assert [_status(x) for x in first] == ["ready", "ready"]

        # горячие клавиши
        page.keyboard.press("/")
        assert page.evaluate("document.activeElement.id") == "search"
        page.keyboard.press("Escape")
        page.evaluate("document.activeElement.blur()")
        page.keyboard.press("?")
        assert page.locator("#keys-help").is_visible()
        page.keyboard.press("Escape")
        assert not page.locator("#keys-help").is_visible()

        # список помнит место; в карточке — соседние товары из списка
        row_ids = [int(x) for x in page.eval_on_selector_all("tr.item", "rows => rows.map(r => r.dataset.id)")]
        page.check(f'tr.item[data-id="{row_ids[0]}"] .sel')
        page.evaluate("window.scrollTo(0, 700)")
        page.wait_for_timeout(200)
        scrolled = page.evaluate("scrollY")
        page.click(f'tr.item[data-id="{row_ids[10]}"] td:nth-child(4)')
        page.wait_for_url(f"**/product?id={row_ids[10]}")
        page.wait_for_selector("#card-nav:not(.hidden)")
        assert page.inner_text("#card-pos") == "11 / 30"
        page.click(".actions-menu.menu-always summary")  # «Ещё ▾» карточки раскрывается, и кнопки в нём нажимаются
        assert _hit(page, ".actions-menu.menu-always .actions-box .btn:not([hidden])")
        page.mouse.click(5, 400)
        page.click("#next-link")
        page.wait_for_url(f"**/product?id={row_ids[11]}")
        page.wait_for_selector("#card-nav:not(.hidden)")
        page.keyboard.press("ArrowLeft")
        page.wait_for_url(f"**/product?id={row_ids[10]}")
        page.wait_for_selector("#card-nav:not(.hidden)")
        page.click("#back-link")
        page.wait_for_selector("tr.item")
        page.wait_for_timeout(300)
        assert abs(page.evaluate("scrollY") - scrolled) < 60
        assert page.is_checked(f'tr.item[data-id="{row_ids[0]}"] .sel')

        # заказы: карточки вместо заготовки
        page.goto(url + "/orders")
        page.wait_for_selector('[data-order="101"]')
        assert page.locator(".panel.skeleton").count() == 0
        ctx.close()

        # ---------- телефон ----------
        for width in (390, 360):
            ctx = browser.new_context(viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True)
            page = _login(ctx, url, errors)
            for path in PAGES:
                page.goto(url + path)
                page.wait_for_timeout(400)
                assert page.evaluate("document.documentElement.scrollWidth") <= width, f"{path} шире экрана {width}"
            page.goto(url + "/")
            page.wait_for_selector("tr.item")
            assert not page.locator(".help-fab-text").is_visible()  # на телефоне — только «🆘»
            assert page.locator(".bottom-nav").is_visible()  # нижняя панель вместо меню сверху
            assert not page.locator(".topbar nav").is_visible()
            page.click(".bottom-nav .bn-more")
            assert page.locator('.topbar nav a[href="/changes"]').is_visible()
            page.check("tr.item .sel >> nth=0")
            assert page.evaluate("getComputedStyle(document.querySelector('#bulk')).position") == "sticky"
            ctx.close()

        # ---------- роль без права «товары»: кнопки видны, но заперты, и объяснено почему ----------
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"{page.url}: {e}"))
        page.goto(url + "/login")
        page.fill("#login-name", "viewer")
        page.fill("#password", PASSWORD)
        page.click("#go")
        page.wait_for_url(url + "/today")
        page.wait_for_selector(".role-banner")
        assert "нельзя" in page.inner_text(".role-banner")
        btn = page.locator('.today-links [data-perm="catalog"]')
        assert btn.is_visible() and float(btn.evaluate("e => getComputedStyle(e).opacity")) < 1
        btn.click()
        page.wait_for_selector("text=Нет права")
        assert page.url.endswith("/today")  # не перешли
        ctx.close()

        # ---------- украинский ----------
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        ctx.add_init_script("localStorage.setItem('promloader-lang', 'uk')")
        page = _login(ctx, url, errors)
        page.wait_for_selector("tr.item")
        page.check("tr.item .sel >> nth=0")
        # на компьютере «Ещё ▾» раскрыто и без заголовка — кнопки видны сразу
        page.click('#bulk [data-action="draft"]')
        page.wait_for_selector(".toast-undo")
        assert page.inner_text(".toast-undo") == "↩ Скасувати"
        page.keyboard.press("?")
        assert "Гарячі клавіші" in page.inner_text("#keys-help")
        ctx.close()
        browser.close()
    assert errors == []
