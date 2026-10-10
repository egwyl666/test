"""Загрузка своего каталога с Prom в программу (GET /products/list, постранично через last_id).

Товары сопоставляются по внешнему ID (external_id), затем по ID Prom. Загруженные товары получают статус
«На Prom». Поля, которых нет в ответе Prom, не трогаются.
"""

import asyncio
import json
import logging
import threading

from . import changes, db, products
from .prom_api import PromError

log = logging.getLogger("promloader.promcatalog")

PAGE = 100
SKIP_STATUSES = {"deleted", "deleted_by_moderator"}
PRESENCE = {"available": "available", "not_available": "not_available", "order": "order", "service": "available"}

_lock = threading.Lock()
STATE_KEY = "prom_catalog_state"


def state() -> dict:
    raw = db.get_setting(STATE_KEY)
    return json.loads(raw) if raw else {"running": False}


def _set_state(**fields) -> None:
    current = state()
    current.update(fields)
    db.set_setting(STATE_KEY, json.dumps(current, ensure_ascii=False))


def recover() -> bool:
    """При запуске: загрузка каталога (или запись ID), прерванная выключением, не должна «идти» вечно.
    True — была прервана."""
    if state().get("ext_running"):
        _set_state(ext_running=False, ext_error="Запись ID прервана: программа была выключена. Запустите её снова",
                   ext_finished_at=db.now())
    if not state().get("running"):
        return False
    _set_state(running=False, error="Загрузка прервана: программа была выключена. Запустите её снова",
               finished_at=db.now())
    return True


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def convert(p: dict) -> tuple[dict, list[str]]:
    """Товар из ответа Prom -> поля программы и ссылки на фото."""
    names = p.get("name_multilang") or {}
    descriptions = p.get("description_multilang") or {}
    data = {
        "name": _text(p.get("name") or names.get("ru") or names.get("uk")),
        "name_ua": _text(names.get("uk")),
        "description": _text(p.get("description") or descriptions.get("ru")),
        "description_ua": _text(descriptions.get("uk")),
        "keywords": _text(p.get("keywords")),
        "currency": _text(p.get("currency")) or "UAH",
        "presence": PRESENCE.get(_text(p.get("presence")), "available"),
    }
    if p.get("price") not in (None, ""):
        try:
            data["price"] = products.parse_number(p["price"])
        except products.ProductError:
            pass
    if p.get("quantity_in_stock") not in (None, ""):
        try:
            data["quantity"] = products.parse_int(p["quantity_in_stock"])
        except products.ProductError:
            pass
    group = p.get("group") or {}
    if isinstance(group, dict) and group.get("name"):
        data["group_name"] = _text(group["name"])
    images = [img.get("url") for img in p.get("images") or [] if isinstance(img, dict) and img.get("url")]
    if not images and p.get("main_image"):
        images = [p["main_image"]]
    return {k: v for k, v in data.items() if v not in ("", None) or k in ("name",)}, images[: products.MAX_IMAGES]


def _upsert(p: dict, counts: dict) -> None:
    if _text(p.get("status")) in SKIP_STATUSES:
        counts["skipped"] += 1
        return
    prom_id = p.get("id")
    external = _text(p.get("external_id"))
    data, images = convert(p)
    if not external:
        counts["no_external_id"] += 1
    external_id = external or _own_id(_text(p.get("sku")), prom_id)
    existing = products.find_by_external_id(external_id)
    if existing is None and prom_id is not None:
        row = db.query_one("SELECT id FROM products WHERE prom_id = ?", (prom_id,))
        existing = row["id"] if row else None
    if existing:
        status = db.query_one("SELECT status FROM products WHERE id = ?", (existing,))["status"]
        if status in ("ready", "error", "sending", "deleting"):
            # в программе есть неотправленные правки — не затираем их данными с Prom, только связываем
            with db.tx() as c:
                c.execute("UPDATE products SET prom_id = ?, prom_no_ext = ? WHERE id = ?",
                          (prom_id, 0 if external else 1, existing))
            counts["kept_local"] += 1
            return
        products.update(existing, data, lock=False)
        pid = existing
        counts["updated"] += 1
    else:
        pid = products.create({**data, "external_id": external_id})
        counts["created"] += 1
    if images and not products.image_rows(pid):
        products.replace_images(pid, images, [])
    with db.tx() as c:
        c.execute("UPDATE products SET prom_id = ?, status = 'synced', synced_at = ?, last_error = '', "
                  "pending_fields = '[]', prom_no_ext = ? WHERE id = ?", (prom_id, db.now(), 0 if external else 1, pid))


def _own_id(sku: str, prom_id) -> str:
    """ID для товара, у которого в кабинете Prom нет «Ідентифікатора_товару»: его код, если этот код не занят
    другим товаром Prom (раньше второй товар с тем же кодом затирал первый), иначе — PROM-<номер на Prom>."""
    if sku:
        other = db.query_one("SELECT prom_id FROM products WHERE external_id = ?", (sku,))
        if other is None or other["prom_id"] in (None, prom_id):
            return sku
    return f"PROM-{prom_id}"


# ---------- «Ідентифікатор_товару» на Prom ----------
# API Prom не умеет менять внешний ID у существующего товара (/products/edit его не принимает), но умеет импорт Excel:
# Prom находит товар по «Унікальний_ідентифікатор» и записывает «Ідентифікатор_товару». Проверено на живом кабинете
# (2026-10-10, на своих тестовых товарах):
# - без колонки названия Prom отклоняет каждую строку (ошибка 1001), и такой импорт висит «в процесі», не давая
#   запустить ни один новый, пока его не отменят в кабинете;
# - без updated_fields Prom стирает цену и наличие: их колонок нет — значит «пусто»;
# - «Ідентифікатор_товару» Prom пишет всегда (файл без этой колонки его стирает), а остальные поля — только из
#   updated_fields. Поэтому обновляем только код товара тем же значением, что на Prom: названия (в том числе
#   украинское — колонку «Назва_позиції_укр» такой импорт не понимает), цена, наличие и описание не меняются,
#   копий не появляется. Название в файле — только для проверки Prom.

EXT_SHEET = "Export Products Sheet"
EXT_HEADERS = ["Унікальний_ідентифікатор", "Назва_позиції", "Код_товару", "Ідентифікатор_товару"]
EXT_IMPORT_SETTINGS = {"mark_missing_product_as": "none", "force_update": False, "only_available": False,
                       "updated_fields": ["sku"]}
EXT_TRIAL = 2
EXT_WAIT_SECONDS = 1800  # Prom обрабатывает такой импорт 5–10 минут
EXT_POLL_SECONDS = 10
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_sleep = asyncio.sleep


def missing_ext_rows(limit: int | None = None) -> tuple[list, bool]:
    """Товары, которым нужно записать ID на Prom: помеченные при загрузке каталога как «без ID на Prom». Если пометок
    нет (каталог загружали версией до 2.2.3) — все товары с номером Prom: у тех, где ID на Prom уже есть, запись не
    нужна (это видно по живому каталогу). Второе значение — True, если это именно помеченные."""
    base = ("SELECT id, name, external_id, prom_id FROM products WHERE prom_id IS NOT NULL "
            "AND status != 'deleting' AND external_id != ''")
    flagged = db.query(base + " AND prom_no_ext = 1 ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))
    if flagged or db.query_one("SELECT 1 AS x FROM products WHERE prom_no_ext = 1 LIMIT 1"):
        return flagged, True
    return db.query(base + " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")), False


def _ext_xlsx(items: list[tuple[dict, dict]]) -> bytes:
    """Файл для импорта: номер на Prom, название и код — как сейчас на Prom (чтобы ничего не изменилось), и ID."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = EXT_SHEET
    ws.append(EXT_HEADERS)
    for row, live in items:
        ws.append([str(row["prom_id"]), _text(live.get("name")), _text(live.get("sku")), row["external_id"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ext_state(**fields) -> None:
    _set_state(**{f"ext_{k}": v for k, v in fields.items()})


def _confirm(prom_ids: list[int]) -> None:
    if not prom_ids:
        return
    with db.tx() as c:
        c.execute(f"UPDATE products SET prom_no_ext = 0 WHERE prom_id {db.IN_LIST}", (db.as_list(prom_ids),))
    left = db.query_one("SELECT COUNT(*) AS n FROM products WHERE prom_no_ext = 1")["n"]
    _set_state(no_external_id=left)


async def _live_catalog(client) -> dict[int, dict]:
    out, last_id = {}, None
    while True:
        page = (await client.list_products(limit=PAGE, last_id=last_id)).get("products") or []
        if not page:
            return out
        out.update({int(p["id"]): p for p in page if p.get("id") and _text(p.get("status")) not in SKIP_STATUSES})
        next_id = page[-1].get("id")
        if not next_id or next_id == last_id:
            return out
        last_id = next_id


async def _import_and_wait(client, content: bytes) -> dict:
    from .prom_api import error_positions, import_state

    try:
        import_id = await client.import_file(content, EXT_IMPORT_SETTINGS, "prom-id.xlsx", XLSX_TYPE)
    except PromError as exc:
        if exc.busy:
            from .sync import CABINET_HINT
            raise PromError(f"Prom занят другим импортом. {CABINET_HINT}")
        raise
    waited = 0
    while True:
        result = await client.import_status(import_id)
        state = import_state(result)
        if state == "failed":
            raise PromError("Prom не принял файл с ID: " + json.dumps(result, ensure_ascii=False)[:300])
        if state == "ok":
            errors = error_positions(result)
            if errors:
                from .sync import CABINET_HINT
                first = next(iter(errors.values()))
                raise PromError(f"Prom нашёл ошибки в {len(errors)} строках ({first}). {CABINET_HINT}")
            return result
        if waited >= EXT_WAIT_SECONDS:
            raise PromError("Prom слишком долго обрабатывает файл с ID — проверьте «Товари» → «Імпорт» в кабинете")
        await _sleep(EXT_POLL_SECONDS)
        waited += EXT_POLL_SECONDS


async def _verify(client, items: list[tuple[dict, dict]]) -> tuple[list[int], str]:
    """Какие товары получили ID именно на своём номере Prom. Если у товара изменились цена или наличие — стоп:
    импорт должен был поменять только ID."""
    ok = []
    for row, live in items:
        found = await client.get_by_external_id(row["external_id"])
        if not found or int(found.get("id") or 0) != int(row["prom_id"]):
            continue
        for field in ("price", "presence", "name", "sku"):
            if str(found.get(field)) != str(live.get(field)):
                return ok, (f"Prom изменил «{field}» у товара «{_text(live.get('name'))}» (было {live.get(field)}, "
                            f"стало {found.get(field)}) — запись остановлена, проверьте товар в кабинете")
        ok.append(int(row["prom_id"]))
    return ok, ""


async def write_ext_ids(client) -> dict:
    """Записывает на Prom ID товаров, у которых там его нет: сначала 2 товара с проверкой, потом остальные."""
    rows, _ = missing_ext_rows()
    live = await _live_catalog(client)
    used = {_text(p.get("external_id")): pid for pid, p in live.items() if _text(p.get("external_id"))}
    todo, already, skipped = [], [], 0
    for r in rows:
        p = live.get(int(r["prom_id"]))
        current = _text(p.get("external_id")) if p else ""
        if p is None:
            skipped += 1  # товара на Prom уже нет
        elif current == r["external_id"]:
            already.append(int(r["prom_id"]))
        elif current or used.get(r["external_id"]) not in (None, int(r["prom_id"])):
            skipped += 1  # на Prom уже другой ID, или этот ID занят другим товаром — не трогаем
        else:
            todo.append((r, p))
    _confirm(already)
    done = len(already)
    _ext_state(total=len(todo) + len(already), done=done, skipped=skipped)
    for stage, items in (("проба на 2 товарах", todo[:EXT_TRIAL]), ("остальные товары", todo[EXT_TRIAL:])):
        if not items:
            continue
        _ext_state(stage=f"{stage}: Prom обрабатывает файл ({len(items)})")
        await _import_and_wait(client, _ext_xlsx(items))
        ok, problem = await _verify(client, items)
        _confirm(ok)
        done += len(ok)
        _ext_state(done=done)
        if problem:
            raise PromError(problem)
        if len(ok) < len(items):
            raise PromError(f"Prom записал ID только {len(ok)} из {len(items)} товаров ({stage}) — дальше не продолжаю. "
                            "Загрузите каталог с Prom заново и попробуйте ещё раз")
    return {"done": done, "skipped": skipped}


async def run_ext_ids(client_factory) -> dict:
    if not _lock.acquire(blocking=False):
        raise PromError("Программа уже работает с каталогом Prom — подождите, пока закончит")
    try:
        _ext_state(running=True, error="", stage="читаю каталог Prom", done=0, total=0, skipped=0, finished_at=None)
        async with client_factory() as client:
            counts = await write_ext_ids(client)
        _ext_state(running=False, stage="", finished_at=db.now())
        return counts
    except PromError as exc:
        _ext_state(running=False, error=str(exc), finished_at=db.now())
        raise
    finally:
        _lock.release()


def _upsert_page(page: list[dict], counts: dict) -> None:
    for p in page:
        try:
            _upsert(p, counts)
        except products.ProductError as exc:
            log.warning("Товар Prom %s пропущен: %s", p.get("id"), exc)
            counts["skipped"] += 1


async def load(client) -> dict:
    """Проходит весь каталог. client — PromClient."""
    counts = {"seen": 0, "created": 0, "updated": 0, "skipped": 0, "no_external_id": 0, "kept_local": 0,
              "missing_on_prom": 0}
    seen: set[int] = set()
    last_id = None
    while True:
        body = await client.list_products(limit=PAGE, last_id=last_id)
        page = body.get("products") or []
        seen |= {int(p["id"]) for p in page if p.get("id") and _text(p.get("status")) not in SKIP_STATUSES}
        await asyncio.to_thread(_upsert_page, page, counts)  # база — в отдельном потоке, интерфейс не подвисает
        counts["seen"] += len(page)
        _set_state(running=True, **counts)
        if not page:
            break  # неполная страница — ещё не конец: Prom пропускает в ней удалённые/скрытые товары
        next_id = page[-1].get("id")
        if not next_id or next_id == last_id:
            break
        last_id = next_id
    counts["missing_on_prom"] = await reconcile(client, seen)
    return counts


MISSING = "Товара нет на Prom (удалён в кабинете Prom?). Отправьте его заново или удалите из программы"
CHECK_LIMIT = 500


async def reconcile(client, seen: set[int]) -> int:
    """Товары, которые программа считает выгруженными, но которых в каталоге Prom не оказалось.

    Каждый перепроверяем по артикулу (одна неполная страница каталога не должна ничего ломать). Если Prom его
    действительно не знает — товар получает статус «Ошибка» с понятной причиной, а не висит как «На Prom».
    """
    rows = db.query("SELECT id, external_id, prom_id FROM products WHERE synced_at IS NOT NULL "
                    "AND status IN ('synced', 'ready', 'error')")
    candidates = [r for r in rows if not r["prom_id"] or int(r["prom_id"]) not in seen][:CHECK_LIMIT]
    missing = []
    for r in candidates:
        try:
            found = await client.get_by_external_id(r["external_id"])
        except PromError as exc:
            log.warning("Сверка с Prom прервана: %s", exc)  # каталог уже загружен; сверка повторится в следующий раз
            break
        if found is None or _text(found.get("status")) in SKIP_STATUSES:
            missing.append(r["id"])
        elif found.get("id"):
            with db.tx() as c:
                c.execute("UPDATE products SET prom_id = ? WHERE id = ?", (int(found["id"]), r["id"]))
    if missing:
        with db.tx() as c:
            for pid in missing:
                # пока ждали ответы Prom, товар могли начать удалять или отправлять — такие не трогаем
                if c.execute("""UPDATE products SET status = 'error', last_error = ?, synced_at = NULL, prom_id = NULL,
                                pending_fields = '["*"]' WHERE id = ? AND status IN ('synced', 'ready', 'error')""",
                             (MISSING, pid)).rowcount:
                    changes.record(c, pid, "prom", "на Prom", "нет на Prom")
        log.warning("Нет на Prom, хотя считались выгруженными: %d", len(missing))
    return len(missing)


async def run(client_factory) -> dict:
    with changes.source("Каталог с Prom"):
        return await _run(client_factory)


async def _run(client_factory) -> dict:
    if not _lock.acquire(blocking=False):
        raise PromError("Каталог уже загружается")
    try:
        _set_state(running=True, error="", seen=0, created=0, updated=0, skipped=0, no_external_id=0, kept_local=0,
                   missing_on_prom=0, finished_at=None)
        async with client_factory() as client:
            counts = await load(client)
        _set_state(running=False, finished_at=db.now(), **counts)
        return counts
    except PromError as exc:
        _set_state(running=False, error=str(exc), finished_at=db.now())
        raise
    finally:
        _lock.release()
