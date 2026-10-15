"""Категории каталога Prom («Зарядные станции», «Праздничное освещение»…) — не путать с группами магазина.

Prom сам ставит товару категорию по названию и не всегда угадывает. Программа хранит две вещи: что стоит на Prom
сейчас (`products.prom_category_id`, пишет только загрузка каталога) и что нужно нам (`products.target_category_id`:
по правилу группы или своё у товара, 🔒). Расхождение видно на странице «Категории».

Официального списка категорий в API Prom нет. Известные категории копятся в `prom_categories`: из своего каталога,
из поиска prom.ua (Prom сам называет самую подходящую категорию запроса — `topHitsCategory`) и со страницы категории
по ссылке (`categoryId` в её данных).
"""

import collections
import json
import re
from urllib.parse import urlparse

import httpx

from . import changes, db, products

PROM_SITE = "https://prom.ua"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36"}
TIMEOUT = 20
_LABEL_ID = re.compile(r"\[(\d+)\]\s*$")


class CategoryError(Exception):
    pass


# ---------- справочник ----------

def remember(c, cat_id, caption: str = "", path: str = "", url: str = "") -> None:
    """Запомнить категорию. Пустые поля не затирают уже известные (из каталога приходит только название)."""
    if not cat_id:
        return
    c.execute("""INSERT INTO prom_categories (id, caption, path, url, updated_at) VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET
                   caption = CASE WHEN excluded.caption != '' THEN excluded.caption ELSE caption END,
                   path = CASE WHEN excluded.path != '' THEN excluded.path ELSE path END,
                   url = CASE WHEN excluded.url != '' THEN excluded.url ELSE url END,
                   updated_at = excluded.updated_at""",
              (int(cat_id), caption or "", path or "", url or "", db.now()))


def info(cat_id) -> dict | None:
    if not cat_id:
        return None
    row = db.query_one("SELECT * FROM prom_categories WHERE id = ?", (int(cat_id),))
    return dict(row) if row else {"id": int(cat_id), "caption": "", "path": "", "url": ""}


def label(value) -> str:
    """Для журнала: «Зарядные станции [500901]» — по номеру в скобках работает «↩ Вернуть»."""
    if value in (None, ""):
        return ""
    cat = info(value)
    return f"{cat['caption'] or 'Категория'} [{cat['id']}]"


def id_from_label(text) -> int | None:
    if not text:
        return None
    m = _LABEL_ID.search(str(text))
    return int(m.group(1)) if m else products.parse_category(text)


def listing() -> list[dict]:
    """Все известные категории: сколько товаров в них сейчас на Prom и сколько мы хотим туда поставить."""
    rows = db.query("""SELECT pc.*,
                              (SELECT COUNT(*) FROM products p WHERE p.prom_category_id = pc.id) AS on_prom,
                              (SELECT COUNT(*) FROM products p WHERE p.target_category_id = pc.id) AS target
                       FROM prom_categories pc ORDER BY lower_u(pc.caption), pc.id""")
    return [dict(r) for r in rows]


# ---------- товар и группы ----------

def for_product(p: dict) -> dict:
    """Что показать в карточке: нужная категория, что на Prom, правило группы."""
    rule = db.query_one("SELECT category_id FROM group_categories WHERE group_name = ?", (p.get("group_name") or "",))
    target, prom = p.get("target_category_id"), p.get("prom_category_id")
    return {
        "target": info(target), "prom": info(prom), "rule": info(rule["category_id"]) if rule else None,
        "locked": "target_category_id" in (p.get("locked_fields") or []),
        # расхождение — только если мы что-то хотим и Prom уже ответил своей категорией
        "mismatch": bool(target and prom and int(target) != int(prom)),
    }


def overview() -> list[dict]:
    """Страница «Категории»: по каждой группе — правило, какие категории у её товаров на Prom, расхождения."""
    rules = {r["group_name"]: r["category_id"] for r in db.query("SELECT * FROM group_categories")}
    groups: dict[str, dict] = {}
    for r in db.query("SELECT group_name, prom_category_id, target_category_id, synced_at FROM products"):
        g = groups.setdefault(r["group_name"] or "", {"count": 0, "on_prom": 0, "prom": collections.Counter(),
                                                      "mismatch": 0, "own": 0})
        g["count"] += 1
        g["on_prom"] += 1 if r["synced_at"] else 0
        if r["prom_category_id"]:
            g["prom"][r["prom_category_id"]] += 1
        if r["target_category_id"] and r["prom_category_id"] and r["target_category_id"] != r["prom_category_id"]:
            g["mismatch"] += 1
        rule = rules.get(r["group_name"] or "")
        if r["target_category_id"] and r["target_category_id"] != rule:
            g["own"] += 1
    known = {r["id"]: dict(r) for r in db.query("SELECT * FROM prom_categories")}

    def cat(cat_id):
        return known.get(cat_id) or {"id": cat_id, "caption": "", "path": "", "url": ""}

    out = []
    for name, g in sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0].lower())):
        out.append({
            "group": name, "count": g["count"], "on_prom": g["on_prom"],
            "rule": cat(rules[name]) if name in rules else None,
            "prom": [{**cat(cid), "count": n} for cid, n in g["prom"].most_common()],
            # Prom разнёс товары одной группы по разным категориям — повод проверить
            "split": len(g["prom"]) > 1, "mismatch": g["mismatch"], "own": g["own"],
        })
    return out


# ---------- несовпадающие: файл для кабинета Prom ----------
# Prom не принимает категорию ни из выгрузки, ни через API (проверено на живом магазине, 2026-10-15) — исправлять
# приходится в кабинете. Файл — список таких товаров в колонках импорта Prom плюс справочные колонки.

MISMATCH_SHEET = "Export Products Sheet"
MISMATCH_HEADERS = ["Унікальний_ідентифікатор", "Ідентифікатор_товару", "Назва_позиції", "Ідентифікатор_підрозділу",
                    "Посилання_підрозділу", "Нужная категория", "Сейчас на Prom", "Изменить в кабинете"]
CABINET_EDIT = "https://my.prom.ua/cms/product/edit/{}"


def mismatch_rows() -> list[dict]:
    """Товары на Prom, у которых категория не та, что нужна. Одним запросом, вместе с названиями категорий."""
    rows = db.query("""SELECT p.id, p.name, p.external_id, p.prom_id, p.group_name, p.prom_category_id,
                              p.target_category_id, t.caption AS target_caption, t.url AS target_url,
                              n.caption AS prom_caption
                       FROM products p
                       LEFT JOIN prom_categories t ON t.id = p.target_category_id
                       LEFT JOIN prom_categories n ON n.id = p.prom_category_id
                       WHERE p.prom_id IS NOT NULL AND p.status != 'deleting'
                         AND p.target_category_id IS NOT NULL AND p.prom_category_id IS NOT NULL
                         AND p.target_category_id != p.prom_category_id
                       ORDER BY lower_u(p.group_name), lower_u(p.name), p.id""")
    return [dict(r) for r in rows]


def mismatch_xlsx() -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(MISMATCH_SHEET)
    ws.append(MISMATCH_HEADERS)
    for r in mismatch_rows():
        target = r["target_category_id"]
        ws.append([str(r["prom_id"]), r["external_id"] or None, r["name"], str(target),
                   # ссылку знаем, только если категорию брали со страницы prom.ua: адреса «по номеру» у Prom нет
                   r["target_url"] or None,
                   f"{r['target_caption'] or 'Категория'} [{target}]",
                   f"{r['prom_caption'] or 'Категория'} [{r['prom_category_id']}]",
                   CABINET_EDIT.format(r["prom_id"])])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def set_group(group: str, category_id: int | None) -> int:
    """Правило «группа → категория» и сразу его применение к товарам группы без своей (🔒) категории.

    Товары, которые уже на Prom, становятся «Готов к отправке» — категория уйдёт полным импортом.
    Возвращает, у скольких товаров поменялась категория.
    """
    category_id = products.parse_category(category_id)
    changed = 0
    with changes.source("Категории Prom"), db.tx() as c:
        if category_id:
            c.execute("""INSERT INTO group_categories (group_name, category_id, updated_at) VALUES (?, ?, ?)
                         ON CONFLICT(group_name) DO UPDATE SET category_id = excluded.category_id,
                                                               updated_at = excluded.updated_at""",
                      (group or "", category_id, db.now()))
            remember(c, category_id)
        else:
            c.execute("DELETE FROM group_categories WHERE group_name = ?", (group or "",))
        rows = c.execute("SELECT id, target_category_id, locked_fields, status FROM products WHERE group_name = ?",
                         (group or "",)).fetchall()
        for r in rows:
            if "target_category_id" in json.loads(r["locked_fields"] or "[]") or r["target_category_id"] == category_id:
                continue
            if r["status"] in ("sending", "deleting"):
                continue  # уйдёт со следующей правкой; трогать товар посреди отправки нельзя
            products._touch(c, r["id"], {"target_category_id": category_id})
            changed += 1
    return changed


def preview_group(group: str, category_id) -> dict:
    """«👁 Что изменится» до сохранения правила: сколько товаров получат категорию и сколько уйдёт на Prom."""
    return changes.preview(set_group, group, category_id)


# ---------- с Prom ----------

def from_catalog(c, prom_product: dict) -> int | None:
    """Категория товара из ответа /products/list: запомнить в справочнике, вернуть номер."""
    cat = prom_product.get("category") or {}
    if not isinstance(cat, dict) or not cat.get("id"):
        return None
    remember(c, cat["id"], str(cat.get("caption") or ""))
    return int(cat["id"])


async def refresh(client) -> dict:
    """Только категории: пройти каталог Prom и обновить, что у наших товаров стоит на Prom. Остальное не трогает."""
    import asyncio

    seen = updated = 0
    last_id = None
    while True:
        page = (await client.list_products(limit=100, last_id=last_id)).get("products") or []
        if not page:
            break
        seen += len(page)
        updated += await asyncio.to_thread(_apply_page, page)
        next_id = page[-1].get("id")
        if not next_id or next_id == last_id:
            break
        last_id = next_id
    return {"seen": seen, "updated": updated}


def _apply_page(page: list[dict]) -> int:
    n = 0
    with db.tx() as c:
        for p in page:
            cat_id = from_catalog(c, p)
            if p.get("id") and cat_id:
                n += c.execute("UPDATE products SET prom_category_id = ? WHERE prom_id = ? "
                               "AND COALESCE(prom_category_id, 0) != ?", (cat_id, p["id"], cat_id)).rowcount
    return n


def _get(url: str, **params) -> str:
    try:
        r = httpx.get(url, params=params or None, headers=UA, timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise CategoryError(f"Нет связи с prom.ua: {exc}") from None
    if r.status_code == 404:
        raise CategoryError("Такой страницы на prom.ua нет — проверьте ссылку")
    if r.status_code != 200:
        raise CategoryError(f"prom.ua ответил ошибкой HTTP {r.status_code}")
    return r.text


def _path(items: list[dict]) -> str:
    names = [str(x.get("caption") or x.get("name") or "") for x in items]
    return " › ".join(n for n in names if n and n not in ("Товари", "Товары", "Каталог товарів", "Каталог товаров"))


def suggest(text: str) -> list[dict]:
    """Подобрать категорию по словам (название группы или товара): поиск prom.ua называет самую подходящую
    категорию запроса с полным путём, остальные — по товарам в выдаче."""
    text = (text or "").strip()
    if not text:
        return []
    html = _get(f"{PROM_SITE}/ua/search", search_term=text[:100])
    found: list[dict] = []
    m = re.search(r'"topHitsCategory":\{"id":(\d+),[^{}]*"path":(\[.*?\])\}', html)
    if m:
        try:
            path = json.loads(m.group(2))
            found.append({"id": int(m.group(1)), "caption": str(path[-1].get("caption") or ""), "path": _path(path)})
        except (ValueError, IndexError, AttributeError):
            pass
    known = {int(cid): cap for cid, cap in re.findall(r'\{"id":(\d+),"caption":"([^"]*)","__typename":"Category"\}', html)}
    for cid, _ in collections.Counter(int(x) for x in re.findall(r'"category":\{"id":(\d+)', html)).most_common(5):
        if cid and all(f["id"] != cid for f in found):
            found.append({"id": cid, "caption": known.get(cid, ""), "path": ""})
    with db.tx() as c:
        for f in found:
            remember(c, f["id"], f["caption"], f["path"])
    return [info(f["id"]) | {k: v for k, v in f.items() if v} for f in found]


def from_url(url: str) -> dict:
    """Категория по ссылке на её страницу prom.ua (https://prom.ua/ua/Zaryadnye-stantsii) или на товар."""
    url = (url or "").strip()
    host = urlparse(url).hostname or ""
    if not (host == "prom.ua" or host.endswith(".prom.ua")):
        raise CategoryError("Нужна ссылка на страницу категории prom.ua, например https://prom.ua/ua/Zaryadnye-stantsii")
    html = _get(url)
    ids = collections.Counter(int(x) for x in re.findall(r'"categoryId":(\d+)', html))
    if not ids:
        raise CategoryError("На этой странице prom.ua не нашлось категории")
    cat_id = ids.most_common(1)[0][0]
    path = ""
    m = re.search(r'"@type":"BreadcrumbList","itemListElement":(\[.*?\])\}', html)
    if m:
        try:
            path = _path(json.loads(m.group(1)))
        except ValueError:
            pass
    caption = path.split(" › ")[-1] if path else ""
    if "/p" in urlparse(url).path and re.search(r"/p\d+", urlparse(url).path):
        url = ""  # это ссылка на товар, а не на категорию — как ссылку категории не храним
    with db.tx() as c:
        remember(c, cat_id, caption, path, url)
    return info(cat_id)
