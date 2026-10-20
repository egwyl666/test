"""Загрузка своего каталога с Prom в программу (GET /products/list, постранично через last_id).

Товары сопоставляются по внешнему ID (external_id), затем по ID Prom. Загруженные товары получают статус
«На Prom». Поля, которых нет в ответе Prom, не трогаются.
"""

import asyncio
import json
import logging
import re
import threading

from . import categories, changes, db, products
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


def live_state() -> dict:
    """Состояние для страницы. «Нет на Prom» — сколько таких товаров в программе СЕЙЧАС, а не число из прошлой загрузки
    каталога: иначе после удаления этих товаров сообщение про «199 товаров в фильтре «Ошибка»» висело бы вечно."""
    st = state()
    st["missing_on_prom"] = db.query_one("SELECT COUNT(*) AS n FROM products WHERE status = 'error' AND last_error = ?",
                                         (MISSING,))["n"]
    return st


def _set_state(**fields) -> None:
    current = state()
    current.update(fields)
    db.set_setting(STATE_KEY, json.dumps(current, ensure_ascii=False))


def recover() -> bool:
    """При запуске: загрузка каталога (или запись ID), прерванная выключением, не должна «идти» вечно.
    True — была прервана."""
    if state().get("cat_running"):
        _set_state(cat_running=False, cat_error="Исправление категорий прервано: программа была выключена. Запустите его снова",
                   cat_finished_at=db.now())
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
        cur = db.query_one("SELECT * FROM products WHERE id = ?", (existing,))
        if cur["status"] in ("ready", "error", "sending", "deleting"):
            # в программе есть неотправленные правки — не затираем их данными с Prom, только связываем; а то, что за это
            # время поменяли и в кабинете Prom, — в «конфликт» (решает владелец в карточке), а не молча теряется
            conflict = _conflicts(cur, data)
            if len(conflict) > len(json.loads(cur["prom_conflict"] or "{}")):
                counts["conflicts"] = counts.get("conflicts", 0) + 1
            with db.tx() as c:
                c.execute("UPDATE products SET prom_id = ?, prom_no_ext = ?, prom_category_id = ?, prom_conflict = ?, "
                          "prom_seen = ? WHERE id = ?",
                          (prom_id, 0 if external else 1, categories.from_catalog(c, p),
                           json.dumps(conflict, ensure_ascii=False) if conflict else "", _seen(data), existing))
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
        # категория Prom — только «что стоит на Prom»; нужную категорию (target) загрузка каталога не трогает
        c.execute("UPDATE products SET prom_id = ?, status = 'synced', synced_at = ?, last_error = '', "
                  "pending_fields = '[]', prom_no_ext = ?, prom_category_id = ?, prom_seen = ?, prom_conflict = '' "
                  "WHERE id = ?",
                  (prom_id, db.now(), 0 if external else 1, categories.from_catalog(c, p), _seen(data), pid))


# ---------- конфликты: поменяли и в программе (не отправлено), и в кабинете Prom ----------
CONFLICT_FIELDS = ("name", "name_ua", "description", "description_ua", "price", "keywords")


def _norm(field: str, value):
    if field == "price":
        try:
            return round(float(value), 2) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    text = str(value or "")
    if field.startswith("description"):  # Prom переформатирует HTML — сравниваем текст
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _seen(data: dict) -> str:
    """Что сейчас стоит на Prom по полям конфликта — чтобы в следующий раз понять, меняли ли их в кабинете."""
    return json.dumps({f: data[f] for f in CONFLICT_FIELDS if f in data}, ensure_ascii=False)


def _conflicts(cur, incoming: dict) -> dict:
    """Поля, которые поменяли в кабинете Prom с прошлой синхронизации и которые расходятся с программой.
    Без прошлого снимка (первая синхронизация, только что отправили) конфликта нет — сравнивать не с чем."""
    conflict = json.loads(cur["prom_conflict"] or "{}")
    seen = json.loads(cur["prom_seen"] or "{}")
    for f in CONFLICT_FIELDS:
        if f not in incoming or f not in seen:
            continue
        new, was, local = _norm(f, incoming[f]), _norm(f, seen[f]), _norm(f, cur[f])
        if new != was and new != local:
            conflict[f] = incoming[f]
        elif new == local:
            conflict.pop(f, None)  # в кабинете сделали так же, как у нас — конфликта нет
    return conflict


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


async def _import_and_wait(client, content: bytes, what: str = "с ID") -> dict:
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
            raise PromError(f"Prom не принял файл {what}: " + json.dumps(result, ensure_ascii=False)[:300])
        if state == "ok":
            errors = error_positions(result)
            if errors:
                from .sync import CABINET_HINT
                first = next(iter(errors.values()))
                raise PromError(f"Prom нашёл ошибки в {len(errors)} строках ({first}). {CABINET_HINT}")
            return result
        if waited >= EXT_WAIT_SECONDS:
            raise PromError(f"Prom слишком долго обрабатывает файл {what} — проверьте «Товари» → «Імпорт» в кабинете")
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


# ---------- категория каталога Prom ----------
# Prom меняет категорию только импортом Excel с «Ідентифікатор_підрозділу» (проверено 2026-10-15 на своём тестовом
# товаре: категория сменилась, цена и наличие — нет; YML, /products/edit и updated_fields её не меняют). В файле —
# текущие название, код и ID с Prom: «Ідентифікатор_товару» Prom пишет всегда, а без кода стёр бы артикул.

CAT_HEADERS = ["Унікальний_ідентифікатор", "Назва_позиції", "Код_товару", "Ідентифікатор_товару", "Ідентифікатор_підрозділу"]


def _cat_state(**fields) -> None:
    _set_state(**{f"cat_{k}": v for k, v in fields.items()})


def _cat_xlsx(items: list[tuple[dict, dict]]) -> bytes:
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = EXT_SHEET
    ws.append(CAT_HEADERS)
    for row, live in items:
        ws.append([str(row["prom_id"]), _text(live.get("name")), _text(live.get("sku")) or None,
                   _text(live.get("external_id")) or None, str(row["target_category_id"])])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cat_confirm(done: dict[int, int]) -> None:
    if not done:
        return
    with db.tx() as c:
        for prom_id, cat_id in done.items():
            c.execute("UPDATE products SET prom_category_id = ? WHERE prom_id = ?", (cat_id, prom_id))


async def fix_categories(client) -> dict:
    """Ставит на Prom нужную категорию товарам, у которых она не та: сначала 2 товара с проверкой, потом остальные."""
    from . import categories

    rows = categories.mismatch_rows()
    live = await _live_catalog(client)
    todo, done, skipped = [], {}, 0
    for r in rows:
        p = live.get(int(r["prom_id"]))
        current = int(((p or {}).get("category") or {}).get("id") or 0)
        if p is None:
            skipped += 1
        elif current == int(r["target_category_id"]):
            done[int(r["prom_id"])] = current  # уже исправили в кабинете
        else:
            todo.append((r, p))
    _cat_confirm(done)
    _cat_state(total=len(todo) + len(done), done=len(done), skipped=skipped)
    for stage, items in (("проба на 2 товарах", todo[:EXT_TRIAL]), ("остальные товары", todo[EXT_TRIAL:])):
        if not items:
            continue
        _cat_state(stage=f"{stage}: Prom обрабатывает файл ({len(items)})")
        await _import_and_wait(client, _cat_xlsx(items), "с категориями")
        after = await _live_catalog(client)
        ok = {}
        for row, before in items:
            now = after.get(int(row["prom_id"])) or {}
            for field in ("price", "presence", "name", "sku"):
                if str(now.get(field)) != str(before.get(field)):
                    _cat_confirm(ok)
                    raise PromError(f"Prom изменил «{field}» у товара «{_text(before.get('name'))}» (было "
                                    f"{before.get(field)}, стало {now.get(field)}) — остановлено, проверьте товар в кабинете")
            if int((now.get("category") or {}).get("id") or 0) == int(row["target_category_id"]):
                ok[int(row["prom_id"])] = int(row["target_category_id"])
        _cat_confirm(ok)
        done.update(ok)
        _cat_state(done=len(done))
        if len(ok) < len(items):
            raise PromError(f"Prom поменял категорию только у {len(ok)} из {len(items)} товаров ({stage}) — дальше не "
                            "продолжаю. Проверьте эти товары в кабинете")
    return {"done": len(done), "skipped": skipped}


async def run_fix_categories(client_factory) -> dict:
    if not _lock.acquire(blocking=False):
        raise PromError("Программа уже работает с каталогом Prom — подождите, пока закончит")
    try:
        _cat_state(running=True, error="", stage="читаю каталог Prom", done=0, total=0, skipped=0, finished_at=None)
        async with client_factory() as client:
            counts = await fix_categories(client)
        _cat_state(running=False, stage="", finished_at=db.now())
        return counts
    except PromError as exc:
        _cat_state(running=False, error=str(exc), finished_at=db.now())
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
              "missing_on_prom": 0, "conflicts": 0}
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


# ---------- автосинхронизация с кабинетом Prom ----------
# Товары, которые выставили или поменяли прямо в кабинете Prom (минуя программу), сами подтягиваются в программу
# раз в N часов: та же загрузка каталога, что по кнопке «⬇ Каталог с Prom».

AUTO_HOURS = (0, 1, 2, 3, 6, 12, 24)  # 0 — выключено


def autosync_hours() -> int:
    try:
        hours = int(db.get_setting("prom_autosync_hours") or 0)
    except ValueError:
        return 0
    return hours if hours in AUTO_HOURS else 0


def set_autosync_hours(hours) -> int:
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = -1
    if hours not in AUTO_HOURS:
        raise ValueError("Синхронизация с Prom: выберите выкл. или 1, 2, 3, 6, 12, 24 часа")
    db.set_setting("prom_autosync_hours", str(hours))
    return hours


def autosync_due(now=None) -> bool:
    """Пора ли синхронизироваться: включено, есть токен, нет паузы, каталог сейчас не загружается, ничего не
    отправляется на Prom (иначе товары «в пути» выглядели бы расходящимися) и прошло N часов с прошлой загрузки."""
    from datetime import datetime, timezone

    from . import config
    hours = autosync_hours()
    if not hours or not config.get("prom_token") or db.paused() or _lock.locked():
        return False
    if db.query_one("SELECT 1 FROM products WHERE status = 'sending' LIMIT 1"):
        return False
    last = state().get("finished_at")
    if not last:
        return True
    try:
        last_at = datetime.fromisoformat(last)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return (now - last_at).total_seconds() >= hours * 3600


async def run_auto(client_factory) -> dict | None:
    """Автосинхронизация: загрузить каталог и сообщить, если на Prom вручную добавили или удалили товары."""
    from . import notify
    try:
        counts = await run(client_factory)
    except PromError as exc:
        log.warning("Автосинхронизация с Prom: %s", exc)
        return None
    _set_state(auto=True)
    created, missing, conflicts = counts.get("created", 0), counts.get("missing_on_prom", 0), counts.get("conflicts", 0)
    if created or missing or conflicts:
        lines = ["🔄 <b>Синхронизация с Prom</b>"]
        if created:
            lines.append(f"На Prom вручную добавили товаров: {created} — теперь они и в программе (статус «На Prom»).")
        if missing:
            lines.append(f"Пропали с Prom (удалены в кабинете?): {missing} — в программе у них статус «Ошибка».")
        if conflicts:
            lines.append(f"Конфликты: {conflicts} — товар поменяли и в кабинете Prom, и в программе. Решите в карточке "
                         "товара («Товары» → «⚡ Конфликты с кабинетом Prom»).")
        notify.send("prom_sync", "\n".join(lines))
    return counts

