"""«Проверка выгрузки»: один товар проходит весь путь до Prom по шагам, и видно, на каком шаге что сломалось.

Шаги: токен → есть ли товар на Prom → файл → фото → отправка → обработка у Prom → появился ли товар.
Последний шаг спрашивает Prom о товаре по артикулу — это надёжнее, чем счётчики в ответе об импорте.
Это настоящая выгрузка одного товара (как кнопка «Отправить»), статус товара в программе не меняется.
"""

import asyncio
import json
import time
import uuid
from xml.etree import ElementTree as ET

import httpx

from . import db, feed, products, sync
from .prom_api import DEFAULT_IMPORT_SETTINGS, PromError, import_state

POLL_SECONDS = 3
WAIT_SECONDS = 180
RUNS: dict[str, dict] = {}


class Run:
    def __init__(self, product: dict):
        self.id = uuid.uuid4().hex[:12]
        self.state = {"id": self.id, "product": {"id": product["id"], "name": product["name"],
                                                 "external_id": product["external_id"]},
                      "steps": [], "done": False, "verdict": "", "started": time.time()}
        RUNS[self.id] = self.state

    def step(self, title: str) -> dict:
        s = {"title": title, "status": "running", "detail": "", "data": None}
        self.state["steps"].append(s)
        return s

    @staticmethod
    def finish(s: dict, status: str, detail: str = "", data=None) -> None:
        s.update(status=status, detail=detail, data=data)

    def verdict(self, text: str) -> None:
        self.state["verdict"] = text


def _short(data, limit: int = 1500) -> str:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


async def _wait_import(run: Run, client, import_id: str, title: str) -> dict | None:
    s = run.step(title)
    seen, deadline = [], time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            result = await client.import_status(import_id)
        except PromError as err:
            run.finish(s, "fail", f"Prom не ответил о статусе импорта: {err}")
            return None
        if not seen or seen[-1] != result:
            seen.append(result)
            s["detail"] = f"Статус: {result.get('status')}"
            s["data"] = seen
        state = import_state(result)
        if state != "running":
            if state == "failed":
                run.finish(s, "fail", "Prom завершил импорт с ошибкой", seen)
            elif sync._found_nothing(result):
                run.finish(s, "warn", "Prom ответил «успешно», но насчитал в файле 0 товаров (total: 0)", seen)
            else:
                n = lambda k: result.get(k) or 0  # noqa: E731
                run.finish(s, "ok", f"Готово: всего {n('total')}, создано {n('created')}, "
                                    f"обновлено {n('updated')}, без изменений {n('not_changed')}", seen)
            return result
        await asyncio.sleep(POLL_SECONDS)
    run.finish(s, "warn", f"Prom обрабатывает импорт дольше {WAIT_SECONDS // 60} минут — в кабинете он, скорее всего, "
                          "висит «В процесі»", seen)
    return None


async def _on_prom(run: Run, client, external_id: str, title: str) -> dict | None | bool:
    """Товар на Prom по артикулу: dict — есть, None — нет, False — не удалось узнать."""
    s = run.step(title)
    try:
        found = await client.get_by_external_id(external_id)
    except PromError as err:
        run.finish(s, "warn", f"Не удалось спросить Prom: {err}")
        return False
    if found:
        brief = {k: found.get(k) for k in ("id", "name", "sku", "price", "currency", "presence", "status") if k in found}
        run.finish(s, "ok", f"Да: «{found.get('name', '')}», id на Prom {found.get('id')}", brief)
    else:
        run.finish(s, "info", f"Нет товара с артикулом {external_id}")
    return found


async def execute(run: Run, product_id: int, client_factory=sync.make_client) -> None:
    try:
        await _execute(run, product_id, client_factory)
    except Exception as exc:  # проверка не должна молча падать
        s = run.step("Непредвиденная ошибка")
        run.finish(s, "fail", f"{type(exc).__name__}: {exc}")
        run.verdict("Проверка остановилась из-за ошибки программы — отправьте этот отчёт в «Поддержку»")
    finally:
        run.state["done"] = True


async def _execute(run: Run, product_id: int, client_factory) -> None:
    p = products.get(product_id)
    ext = p["external_id"]

    # 1. Токен
    s = run.step("Связь с Prom и токен")
    try:
        client = client_factory()
    except PromError as err:
        run.finish(s, "fail", str(err))
        run.verdict("Не задан или неверный токен Prom — «Настройки» → «Подключение к Prom.ua»")
        return
    async with client:
        try:
            await client.check()
            run.finish(s, "ok", "Prom принимает токен")
        except PromError as err:
            run.finish(s, "fail", str(err))
            run.verdict("Prom не принимает токен или недоступен")
            return

        # 2. Есть ли товар на Prom заранее
        before = await _on_prom(run, client, ext, f"Есть ли товар «{ext}» на Prom до выгрузки")

        # 3. Файл
        s = run.step("Файл выгрузки")
        errors = p["check"]["errors"]
        if errors:
            run.finish(s, "fail", "Товар не прошёл проверку программы: " + "; ".join(errors))
            run.verdict("Исправьте ошибки в карточке товара")
            return
        try:
            base = await sync._photo_base_url([product_id])
        except PromError as err:
            run.finish(s, "fail", f"Не получилось подготовить ссылки на фото: {err}")
            run.verdict("Проблема с передачей фото — «Настройки» → «Фото для Prom»")
            return
        content = feed.build([product_id], base)
        root = ET.fromstring(content)
        offer = root.find("shop/offers/offer")
        fields = {child.tag: (child.text or "")[:80] for child in offer} if offer is not None else {}
        pictures = [el.text for el in offer.findall("picture")] if offer is not None else []
        run.finish(s, "ok", f"{len(content)} байт, товаров в файле: {len(root.findall('shop/offers/offer'))}, "
                            f"фото: {len(pictures)}", {"offer_id": offer.get("id") if offer is not None else None,
                                                       "fields": fields, "file_start": content[:1200].decode("utf-8", "replace")})

        # 4. Фото
        s = run.step("Фото открываются по ссылкам")
        if not pictures:
            run.finish(s, "info", "У товара нет фото")
        else:
            results = {}
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
                for url in pictures[:5]:
                    try:
                        r = await http.head(url)
                        if r.status_code == 405:
                            r = await http.get(url)
                        results[url] = f"HTTP {r.status_code}"
                    except httpx.HTTPError as exc:
                        results[url] = f"не открывается: {exc}"
            bad = [u for u, v in results.items() if v != "HTTP 200"]
            run.finish(s, "warn" if bad else "ok", f"не открываются: {len(bad)} из {len(results)}" if bad
                       else "все проверенные фото открываются", results)

        # 5–7. Отправка и обработка
        settings = {**DEFAULT_IMPORT_SETTINGS, **(sync.import_settings() or {})}
        result = None
        for method in ("file", "url"):
            s = run.step("Отправка на Prom " + ("файлом (import_file)" if method == "file" else "по ссылке (import_url)"))
            try:
                import_id, sent = await sync._send_import(client, content, dict(settings), method)
            except PromError as err:
                run.finish(s, "fail", str(err))
                if err.busy:
                    run.verdict("Prom занят другим импортом. В кабинете вверху откройте список импортов: дождитесь "
                                "или отмените тот, что висит «В процесі», и запустите проверку снова")
                else:
                    run.verdict(f"Prom не принял выгрузку: {err}")
                return
            run.finish(s, "ok", f"Prom принял, номер импорта {import_id}", sent)
            result = await _wait_import(run, client, import_id, "Prom обрабатывает импорт")
            if result is None:
                run.verdict("Prom не закончил импорт за 3 минуты. Посмотрите в кабинете, не висит ли там импорт "
                            "«В процесі», и отмените его")
                return
            after = await _on_prom(run, client, ext, f"Появился ли товар «{ext}» на Prom")
            if after is False:
                run.verdict("Prom обработал импорт (см. шаг выше), но спросить его о товаре не удалось — "
                            "проверьте товар в кабинете")
                return
            if after:
                changed = "обновлён" if before else "создан"
                if method == "url":
                    db.set_setting("import_method", "url")  # обычные выгрузки — тоже по ссылке
                run.verdict(f"Всё работает: товар {changed} на Prom способом «{'файл' if method == 'file' else 'ссылка'}»")
                return
            if not sync._found_nothing(result):
                break  # Prom что-то посчитал, но товара нет — по ссылке пробовать бессмысленно

        run.verdict("Prom принимает файл, но товар не появляется. Нажмите «Скопировать отчёт» и пришлите его — "
                    "по шагам выше видно, что именно ответил Prom" if result is not None else "")
