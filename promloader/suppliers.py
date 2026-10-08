"""Поставщики: источник прайса (ссылка или файл) + настройки колонок + обновление по расписанию.

Обновление:
- новые позиции создают товары (черновики или сразу «готов»), существующие обновляются;
- поля, которые пользователь правил руками (locked_fields), не перезаписываются;
- цена считается по правилам наценки от закупки/РРЦ;
- пропавшие из прайса позиции получают «нет в наличии», вернувшиеся — снова наличие поставщика;
- если прайс «похудел» больше чем вдвое, обновление останавливается: скорее всего, у поставщика сбой.
"""

import hashlib
import json
import logging
import math
import re
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import changes, db, excel, notify, pricing, products, rates

log = logging.getLogger("promloader.suppliers")

MISSING_ACTIONS = {"not_available": "ставить «нет в наличии»", "keep": "ничего не менять"}
NEW_STATUSES = ("draft", "ready")
# поля товара, которыми управляет прайс поставщика
SYNC_FIELDS = (
    "name", "name_ua", "description", "description_ua", "price", "old_price", "cost_price", "rrp", "currency",
    "unit", "quantity", "presence", "group_name", "vendor", "country", "keywords", "barcode",
    "vendor_code", "cost_currency",
)
EDITABLE = ("name", "url", "sheet", "header_row", "rows", "mapping", "defaults", "prefix", "new_status",
            "missing_action", "auto_sync", "interval_hours", "merge_by_barcode",
            "rate_mode", "rate_currency", "rate_value", "rate_add", "clean_names")

BROKEN_FEED_MIN = 20       # защита включается, если у поставщика было хотя бы столько товаров
BROKEN_FEED_RATIO = 0.5    # ...и в новом прайсе осталось меньше этой доли
CHUNK = 500                # товаров на транзакцию: интерфейс не подвисает во время большого обновления
SYNC_CHUNK = 2000          # товаров в одной задаче выгрузки на Prom
MAX_DOWNLOAD = 200 * 1024 * 1024

_running: set[int] = set()
_running_lock = threading.Lock()


class SupplierError(ValueError):
    pass


# ---------- чтение ----------

def _row(supplier_id: int):
    row = db.query_one("SELECT * FROM suppliers WHERE id = ?", (supplier_id,))
    if row is None:
        raise KeyError(supplier_id)
    return row


def _parse(row) -> dict:
    s = dict(row)
    s["mapping"] = json.loads(s["mapping"] or "{}")
    s["defaults"] = json.loads(s["defaults"] or "{}")
    s["auto_sync"] = bool(s["auto_sync"])
    s["merge_by_barcode"] = bool(s.get("merge_by_barcode", 1))
    s["clean_names"] = bool(s.get("clean_names", 0))
    s["running"] = s["id"] in _running
    return s


def _run_dict(row) -> dict:
    r = dict(row)
    r["stats"] = json.loads(r["stats"] or "{}")
    return r


def list_suppliers() -> list[dict]:
    result = []
    for row in db.query("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE"):
        s = _parse(row)
        counts = db.query_one(
            """SELECT COUNT(*) AS total, SUM(missing = 0 AND ignored = 0) AS active, SUM(product_id IS NOT NULL) AS linked
               FROM supplier_items WHERE supplier_id = ?""", (s["id"],))
        s["items_total"] = counts["total"] or 0
        s["items_active"] = counts["active"] or 0
        s["products"] = counts["linked"] or 0
        last = db.query_one("SELECT * FROM supplier_runs WHERE supplier_id = ? ORDER BY id DESC LIMIT 1", (s["id"],))
        s["last_run"] = _run_dict(last) if last else None
        result.append(s)
    return result


def failed() -> list[dict]:
    """Поставщики, у которых последнее обновление не удалось (для предупреждения на всех страницах)."""
    rows = db.query("""SELECT s.id, s.name, r.message, r.finished_at FROM suppliers s
                       JOIN supplier_runs r ON r.id = (SELECT MAX(id) FROM supplier_runs WHERE supplier_id = s.id)
                       WHERE r.status = 'failed' ORDER BY s.name COLLATE NOCASE""")
    return [{"id": r["id"], "name": r["name"], "message": r["message"], "at": r["finished_at"]} for r in rows]


def get(supplier_id: int) -> dict:
    s = _parse(_row(supplier_id))
    s["runs"] = [_run_dict(r) for r in db.query(
        "SELECT * FROM supplier_runs WHERE supplier_id = ? ORDER BY id DESC LIMIT 15", (supplier_id,))]
    counts = db.query_one(
        "SELECT COUNT(*) AS total, SUM(missing = 0 AND ignored = 0) AS active FROM supplier_items WHERE supplier_id = ?",
        (supplier_id,))
    s["items_total"] = counts["total"] or 0
    s["items_active"] = counts["active"] or 0
    s["items_deleted"] = db.query_one(
        "SELECT COUNT(*) AS n FROM supplier_items WHERE supplier_id = ? AND ignored = 1 AND product_id IS NULL",
        (supplier_id,))["n"]
    return s


def deleted_items(supplier_id: int) -> list[dict]:
    """Строки прайса, товары которых вы удалили: поставщик их не создаёт заново, пока не вернёте."""
    _row(supplier_id)
    out = []
    for r in db.query("SELECT sku, data, missing, seen_at FROM supplier_items WHERE supplier_id = ? AND ignored = 1 "
                      "AND product_id IS NULL ORDER BY sku", (supplier_id,)):
        try:
            data = json.loads(r["data"]).get("data", {})
        except ValueError:
            data = {}
        # цена поставщика (закупка в его валюте), а не уже посчитанная розничная
        if data.get("cost_price") is not None:
            price, currency = data["cost_price"], data.get("cost_currency") or "UAH"
        elif data.get("rrp") is not None:
            price, currency = data["rrp"], data.get("cost_currency") or "UAH"
        else:
            price, currency = data.get("price"), data.get("currency") or "UAH"
        out.append({"sku": r["sku"], "name": data.get("name") or "", "price": price, "currency": currency,
                    "missing": bool(r["missing"]), "seen_at": r["seen_at"]})
    return out


def restore_deleted(supplier_id: int, skus: list[str] | None = None) -> int:
    """Вернуть удалённые вами товары поставщика (все или выбранные артикулы) — они создадутся при обновлении прайса."""
    _row(supplier_id)
    where, args = "supplier_id = ? AND ignored = 1 AND product_id IS NULL", [supplier_id]
    if skus is not None:
        if not skus:
            return 0
        where += f" AND sku IN ({','.join('?' * len(skus))})"
        args += [str(x) for x in skus]
    with db.tx() as c:
        return c.execute(f"UPDATE supplier_items SET ignored = 0 WHERE {where}", args).rowcount


# ---------- настройки ----------

def create(name: str) -> int:
    name = (name or "").strip() or "Новый поставщик"
    ts = db.now()
    with db.tx() as c:
        cur = c.execute("INSERT INTO suppliers (name, created_at, updated_at) VALUES (?, ?, ?)", (name, ts, ts))
    return cur.lastrowid


def _next_run(interval_hours: float) -> str | None:
    if not interval_hours or interval_hours <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(hours=interval_hours)).isoformat(timespec="seconds")


def update(supplier_id: int, data: dict) -> dict:
    current = _row(supplier_id)
    fields = {}
    for key in EDITABLE:
        if key not in data:
            continue
        value = data[key]
        if key in ("mapping", "defaults"):
            if not isinstance(value, dict):
                raise SupplierError(f"{key}: ожидается объект")
            value = json.dumps(value, ensure_ascii=False)
        elif key == "header_row":
            try:
                value = max(0, int(value or 0))
            except (TypeError, ValueError):
                raise SupplierError("Строка с заголовками — это номер строки, например 1")
        elif key == "interval_hours":
            try:
                value = float(value or 0)
            except (TypeError, ValueError):
                raise SupplierError("Интервал обновления — число часов")
            value = max(0.0, value) if math.isfinite(value) else -1
            if value < 0 or value > 24 * 366:
                raise SupplierError("Интервал обновления — число часов, не больше года")
        elif key in ("auto_sync", "merge_by_barcode", "clean_names"):
            value = 1 if value else 0
        elif key == "rate_mode":
            if value not in ("", "manual", "nbu"):
                raise SupplierError("Курс поставщика: общий или свой")
        elif key == "rate_currency":
            value = str(value or "USD").strip().upper()
            if value not in rates.CURRENCIES:
                raise SupplierError("Валюта прайса: " + ", ".join(rates.CURRENCIES))
        elif key in ("rate_value", "rate_add"):
            try:
                value = float(str(value or 0).replace(",", "."))
            except ValueError:
                raise SupplierError("Курс и надбавка к курсу — числа, например 41.5 и 2")
            if not math.isfinite(value) or value > 1e6:
                raise SupplierError("Курс и надбавка к курсу — обычные числа, например 41.5 и 2")
            if key == "rate_value" and value < 0 or key == "rate_add" and not -50 <= value <= 100:
                raise SupplierError("Курс не может быть отрицательным, надбавка — от -50 до 100%")
        elif key == "new_status" and value not in NEW_STATUSES:
            raise SupplierError("Статус новых товаров: draft или ready")
        elif key == "missing_action" and value not in MISSING_ACTIONS:
            raise SupplierError("Неизвестное действие для пропавших товаров")
        else:
            value = str(value or "").strip()
        fields[key] = value
    if "rows" in fields and fields["rows"]:
        excel.parse_row_spec(fields["rows"], 10)  # проверка синтаксиса
    if "url" in fields and fields["url"] and not re.match(r"^https?://", fields["url"]):
        raise SupplierError("Ссылка на прайс должна начинаться с http:// или https://")
    if "interval_hours" in fields and fields["interval_hours"] != current["interval_hours"]:
        fields["next_run_at"] = _next_run(fields["interval_hours"])
    if fields:
        fields["updated_at"] = db.now()
        with db.tx() as c:
            c.execute(f"UPDATE suppliers SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      list(fields.values()) + [supplier_id])
    return get(supplier_id)


def delete(supplier_id: int) -> None:
    """Поставщик удаляется, его товары остаются как обычные (без привязки)."""
    row = _row(supplier_id)
    with db.tx() as c:
        c.execute("""UPDATE products SET supplier_id = (
                         SELECT si.supplier_id FROM supplier_items si
                         WHERE si.product_id = products.id AND si.supplier_id != ? ORDER BY si.supplier_id LIMIT 1)
                     WHERE supplier_id = ?""", (supplier_id, supplier_id))
        c.execute("UPDATE products SET locked_fields = '[]' WHERE supplier_id IS NULL AND locked_fields != '[]' "
                  "AND id IN (SELECT product_id FROM supplier_items WHERE supplier_id = ?)", (supplier_id,))
        c.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
    if row["source_file"]:
        (db.suppliers_dir() / row["source_file"]).unlink(missing_ok=True)


# ---------- источник ----------

def _direct_url(url: str) -> str:
    """Ссылку на Google Таблицу превращаем в ссылку на скачивание .xlsx."""
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    return url


def download(url: str, transport: httpx.BaseTransport | None = None) -> tuple[bytes, str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=180, transport=transport,
                          headers={"User-Agent": "PromLoader/1.0"}) as client:
            with client.stream("GET", _direct_url(url)) as r:
                if r.status_code >= 400:
                    raise SupplierError(f"Поставщик ответил ошибкой HTTP {r.status_code}")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise SupplierError("Прайс больше 200 МБ")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise SupplierError(f"Не удалось скачать прайс: {exc}")
    content = b"".join(chunks)
    if not content.strip():
        raise SupplierError("По ссылке пустой файл")
    return content, url.rsplit("/", 1)[-1].split("?")[0] or "price"


def store_source(supplier_id: int, content: bytes, filename: str) -> Path:
    """Сохраняет прайс как текущий файл поставщика."""
    _row(supplier_id)
    suffix = excel.detect_suffix(content, filename)
    folder = db.suppliers_dir()
    for old in folder.glob(f"{supplier_id}.*"):
        old.unlink()
    path = folder / f"{supplier_id}{suffix}"
    path.write_bytes(content)
    with db.tx() as c:
        c.execute("UPDATE suppliers SET source_file = ?, source_name = ?, updated_at = ? WHERE id = ?",
                  (path.name, filename, db.now(), supplier_id))
    return path


def source_path(supplier_id: int, refetch: bool = False, transport=None) -> Path:
    row = _row(supplier_id)
    if refetch and row["url"]:
        content, name = download(row["url"], transport)
        return store_source(supplier_id, content, name)
    if row["source_file"]:
        path = db.suppliers_dir() / row["source_file"]
        if path.exists():
            return path
    if row["url"]:
        content, name = download(row["url"], transport)
        return store_source(supplier_id, content, name)
    raise SupplierError("У поставщика нет ни ссылки на прайс, ни загруженного файла")


# ---------- обновление ----------

def _images_hash(urls: list[str], files: list[bytes]) -> str:
    h = hashlib.sha1(json.dumps(urls).encode())
    for b in files:
        h.update(hashlib.sha1(b).digest())
    return h.hexdigest()


def _replace_images(c, product_id: int, urls: list[str], files: list[bytes], trash: list[str]) -> None:
    products.mark_pending(c, product_id, ["images"])  # фото изменились: быстрым обновлением цены не обойтись
    trash += [r["file"] for r in c.execute("SELECT file FROM images WHERE product_id = ? AND file IS NOT NULL", (product_id,))]
    c.execute("DELETE FROM images WHERE product_id = ?", (product_id,))
    pos = 0
    for content in files:
        try:
            name = products.store_image_bytes(content)
        except products.ProductError:
            continue
        c.execute("INSERT INTO images (product_id, file, position) VALUES (?, ?, ?)", (product_id, name, pos))
        pos += 1
    for url in urls:
        if pos >= products.MAX_IMAGES:
            break
        c.execute("INSERT INTO images (product_id, url, position) VALUES (?, ?, ?)", (product_id, url, pos))
        pos += 1


def _item_fields(item: dict) -> dict:
    fields = {k: item["data"][k] for k in SYNC_FIELDS if k in item["data"]}
    if item["params"]:
        fields["params"] = json.dumps(products.parse_params(item["params"]), ensure_ascii=False)
    return fields


def valid_barcode(code: str) -> bool:
    """Штрихкод, по которому можно объединять: 8–14 цифр и не «заглушка» из одинаковых цифр."""
    code = (code or "").strip()
    return code.isdigit() and 8 <= len(code) <= 14 and len(set(code)) > 1


def recompute_offer(c, product_id: int, pricer: pricing.Pricer) -> bool:
    """Цена и наличие товара с несколькими поставщиками — по лучшему предложению. True, если товар изменился."""
    rows = c.execute("SELECT supplier_id, sku, missing, data FROM supplier_items WHERE product_id = ?", (product_id,)).fetchall()
    if len(rows) < 2:
        return False
    items = [{"supplier_id": r["supplier_id"], "missing": r["missing"], "data": json.loads(r["data"])["data"]} for r in rows]
    product = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    best = products.choose_offer(items, pricer.rates)
    if best is None:
        target = {"presence": "not_available", "quantity": 0 if product["quantity"] is not None else None}
    else:
        data = dict(best["data"])
        data["group_name"] = product["group_name"]
        pricer.apply(data, best["supplier_id"])
        target = {k: data[k] for k in products.COMMERCIAL if k in data}
        if not any(not i["missing"] and i["data"].get("presence", "available") != "not_available" for i in items):
            target["presence"] = "not_available"
    locked = set(json.loads(product["locked_fields"] or "[]"))
    nothing_in_stock = target.get("presence") == "not_available"
    # ручные правки уважаем; исключение — товара нет ни у одного поставщика (как и для одного поставщика)
    updates = {k: v for k, v in target.items()
               if (k not in locked or (nothing_in_stock and k in ("presence", "quantity"))) and product[k] != v}
    if updates:
        products._touch(c, product_id, updates, product["status"])
    return bool(updates)


def _apply_one(c, s, item: dict, files: list[bytes], ts: str, run_id: int, stats: dict, changed: list[int],
               trash: list[str], multi: set[int]) -> None:
    sku = item["data"]["external_id"]
    fields = _item_fields(item)
    urls = item["image_urls"]
    img_hash = _images_hash(urls, files)
    existing = c.execute("SELECT * FROM supplier_items WHERE supplier_id = ? AND sku = ?", (s["id"], sku)).fetchone()
    payload = json.dumps({"data": item["data"], "params": item["params"], "image_urls": urls}, ensure_ascii=False)

    product = None
    if existing and existing["product_id"]:
        product = c.execute("SELECT * FROM products WHERE id = ?", (existing["product_id"],)).fetchone()
    if product is None:
        external_id = s["prefix"] + sku
        other = c.execute("SELECT * FROM products WHERE external_id = ?", (external_id,)).fetchone()
        if other is not None:
            if other["supplier_id"] not in (None, s["id"]):
                stats["errors"] += 1
                _sample(stats, f"{sku}: артикул {external_id} уже занят товаром другого поставщика")
                return
            # товар уже был (создан руками, импортом или снова появился после удаления) — привязываем его к поставщику
            product = other
    if (product is None and existing is not None and existing["ignored"]) or \
            (product is not None and product["status"] == "deleting"):
        # товар удалили вы — не создаём его заново (вернуть: страница поставщика → «Удалённые вами товары»)
        c.execute("UPDATE supplier_items SET data = ?, images_hash = ?, missing = 0, seen_at = ?, seen_run = ? WHERE id = ?",
                  (payload, img_hash, ts, run_id, existing["id"]))
        stats["ignored"] = stats.get("ignored", 0) + 1
        return
    if product is None and s["merge_by_barcode"] and valid_barcode(fields.get("barcode", "")):
        # тот же товар у ДРУГОГО поставщика — добавляем как ещё одно предложение
        product = c.execute(
            """SELECT * FROM products WHERE barcode = ? AND id NOT IN
                   (SELECT product_id FROM supplier_items WHERE supplier_id = ? AND product_id IS NOT NULL)
               ORDER BY id LIMIT 1""", (fields["barcode"], s["id"])).fetchone()
        if product is not None:
            stats["joined"] = stats.get("joined", 0) + 1

    if product is None:
        cols = ["external_id", "supplier_id", "status", "created_at", "updated_at"] + list(fields)
        cur = c.execute(
            f"INSERT INTO products ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [s["prefix"] + sku, s["id"], s["new_status"], ts, ts] + list(fields.values()),
        )
        product_id = cur.lastrowid
        changes.record(c, product_id, "created", "", fields.get("name") or "")
        if urls or files:
            _replace_images(c, product_id, urls, files, trash)
        stats["created"] += 1
        changed.append(product_id)
    else:
        product_id = product["id"]
        locked = set(json.loads(product["locked_fields"] or "[]"))
        returned = bool(existing and existing["missing"])
        others = c.execute("SELECT COUNT(*) FROM supplier_items WHERE product_id = ? AND NOT (supplier_id = ? AND sku = ?)",
                           (product_id, s["id"], sku)).fetchone()[0]
        primary = product["supplier_id"] in (None, s["id"])
        if not primary:
            fields = {}  # описание и фото ведёт основной поставщик; цену и наличие считаем по лучшему предложению
        elif others:
            fields = {k: v for k, v in fields.items() if k not in products.COMMERCIAL}
        if others or not primary:
            multi.add(product_id)
            returned = False
        updates = {k: v for k, v in fields.items() if k not in locked and product[k] != v}
        if returned:
            # вернулся в прайс: наличие берём от поставщика, даже если его правили руками
            updates["presence"] = fields.get("presence", "available")
            if "quantity" in fields:
                updates["quantity"] = fields["quantity"]
            updates = {k: v for k, v in updates.items() if product[k] != v}
            stats["returned"] += 1
        if product["supplier_id"] is None:
            updates["supplier_id"] = s["id"]
        images_changed = primary and (
            "images" not in locked and (urls or files)
            and (existing is None or existing["images_hash"] != img_hash)
        )
        if images_changed:
            _replace_images(c, product_id, urls, files, trash)
        if updates or images_changed:
            products._touch(c, product_id, updates, product["status"])
            stats["updated"] += 1
            if "price" in updates:
                stats["price_changed"] += 1
            changed.append(product_id)
        else:
            stats["unchanged"] += 1

    link_item(c, s["id"], sku, product_id, payload, img_hash, ts, run_id)


def link_item(c, supplier_id: int, sku: str, product_id: int, payload: str, img_hash: str, ts: str, run_id: int) -> None:
    """Строка прайса ↔ товар. Привязанная строка больше не считается удалённой вами."""
    c.execute(
        """INSERT INTO supplier_items (supplier_id, sku, product_id, data, images_hash, missing, seen_at, seen_run)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)
           ON CONFLICT(supplier_id, sku) DO UPDATE SET product_id = excluded.product_id, data = excluded.data,
               images_hash = excluded.images_hash, missing = 0, seen_at = excluded.seen_at, seen_run = excluded.seen_run,
               ignored = 0""",
        (supplier_id, sku, product_id, payload, img_hash, ts, run_id),
    )


def _sample(stats: dict, message: str) -> None:
    if len(stats["error_samples"]) < 30:
        stats["error_samples"].append(message)


def apply_items(s: dict, items: list[dict], embedded: dict, run_id: int, force: bool = False,
                changed: list[int] | None = None) -> tuple[dict, list[int]]:
    """changed — список, куда складываются изменённые товары по ходу (он остаётся у вызывающего даже при сбое)."""
    stats = {"total": len(items), "valid": 0, "created": 0, "updated": 0, "unchanged": 0, "price_changed": 0,
             "missing": 0, "returned": 0, "errors": 0, "error_samples": []}
    valid, seen = [], set()
    for item in items:
        sku = item["data"].get("external_id")
        if not sku:
            stats["errors"] += 1
            _sample(stats, f"строка {item['row']}: нет артикула поставщика")
            continue
        if sku in seen:
            stats["errors"] += 1
            _sample(stats, f"строка {item['row']}: артикул {sku} повторяется")
            continue
        if item["errors"]:
            stats["errors"] += 1
            _sample(stats, f"строка {item['row']} ({sku}): {'; '.join(item['errors'])}")
            continue
        seen.add(sku)
        valid.append(item)
    stats["valid"] = len(valid)

    before = db.query_one("SELECT COUNT(*) AS n FROM supplier_items WHERE supplier_id = ? AND missing = 0", (s["id"],))["n"]
    if not force and before >= BROKEN_FEED_MIN and len(valid) < before * BROKEN_FEED_RATIO:
        raise SupplierError(
            f"В прайсе {len(valid)} годных товаров, а в прошлый раз было {before}. Похоже, прайс поставщика сломан — "
            "обновление остановлено, ничего не изменено. Если товары действительно убрали, нажмите «Обновить принудительно»."
        )

    ts = db.now()
    changed = changed if changed is not None else []
    multi: set[int] = set()
    for start in range(0, len(valid), CHUNK):
        trash: list[str] = []
        with db.tx() as c:
            for item in valid[start:start + CHUNK]:
                _apply_one(c, s, item, embedded.get(item["row"], []), ts, run_id, stats, changed, trash, multi)
        _drop_files(trash)

    with db.tx() as c:
        gone = c.execute(
            "SELECT id, product_id FROM supplier_items WHERE supplier_id = ? AND missing = 0 AND seen_run != ?",
            (s["id"], run_id)).fetchall()
        for item in gone:
            c.execute("UPDATE supplier_items SET missing = 1 WHERE id = ?", (item["id"],))
            stats["missing"] += 1
            if not item["product_id"]:
                continue
            other_offers = c.execute("SELECT COUNT(*) FROM supplier_items WHERE product_id = ? AND id != ?",
                                     (item["product_id"], item["id"])).fetchone()[0]
            if other_offers:
                multi.add(item["product_id"])  # у другого поставщика товар может быть — решит пересчёт
                continue
            if s["missing_action"] != "not_available":
                continue
            p = c.execute("SELECT status, presence, quantity FROM products WHERE id = ?", (item["product_id"],)).fetchone()
            if p and (p["presence"] != "not_available" or p["quantity"] not in (None, 0)):
                qty = 0 if p["quantity"] is not None else None
                products._touch(c, item["product_id"], {"presence": "not_available", "quantity": qty}, p["status"])
                changed.append(item["product_id"])

    if multi:
        pricer = pricing.Pricer()
        ids = sorted(multi)
        for start in range(0, len(ids), CHUNK):
            with db.tx() as c:
                for pid in ids[start:start + CHUNK]:
                    if recompute_offer(c, pid, pricer):
                        changed.append(pid)
                        stats["price_changed"] += 1
        stats["multi_offer"] = len(multi)
    return stats, changed


def _queue_changed(changed: list[int]) -> int:
    from . import sync

    if not changed:
        return 0
    ready = {r["id"] for r in db.query("SELECT id FROM products WHERE status = 'ready'")}
    ids = sorted(set(changed) & ready)
    queued = 0
    for start in range(0, len(ids), SYNC_CHUNK):
        queued += sync.enqueue(ids[start:start + SYNC_CHUNK])["accepted"]
    return queued


def run(supplier_id: int, trigger: str = "manual", force: bool = False, transport=None) -> dict:
    """Полное обновление поставщика. Возвращает запись о запуске."""
    with _busy(supplier_id):
        s = _parse(_row(supplier_id))
        with changes.source(f"Поставщик «{s['name']}»"):  # в журнале изменений — кто поменял товары
            return _run(s, trigger, force, transport)


def _drop_files(names: list[str]) -> None:
    """Удалить заменённые файлы фото — только если изменения действительно записаны. Внутри «пробного прогона»
    (внешняя транзакция, которая откатится) файлы должны остаться: база вернётся к ним."""
    if db.in_transaction():
        return
    for name in names:
        (db.uploads_dir() / name).unlink(missing_ok=True)


@contextmanager
def _busy(supplier_id: int):
    """Один поставщик — одно действие за раз: обновление и «Что изменится» не идут параллельно."""
    with _running_lock:
        if supplier_id in _running:
            raise SupplierError("Этот поставщик сейчас обновляется — дождитесь окончания")
        _running.add(supplier_id)
    try:
        yield
    finally:
        with _running_lock:
            _running.discard(supplier_id)


def _fetch_source(s: dict, transport=None) -> tuple[Path, bytes | None, str, Path | None]:
    """Свежий прайс по ссылке — сначала во временный файл: текущим он станет только после успешного обновления,
    чтобы битый или пустой прайс не затёр последний рабочий. Без ссылки — сохранённый файл.
    Возвращает (путь для разбора, содержимое, имя, временный файл для удаления)."""
    if not s["url"]:
        return source_path(s["id"]), None, "", None
    content, name = download(s["url"], transport)
    tmp = db.suppliers_dir() / f"{s['id']}.new-{uuid.uuid4().hex[:8]}{excel.detect_suffix(content, name)}"
    tmp.write_bytes(content)
    return tmp, content, name, tmp


def _parse_items(s: dict, path: Path, images: bool = True) -> tuple[list[dict], dict]:
    sheets = excel.sheet_names(path)
    sheet = s["sheet"] if s["sheet"] in sheets else sheets[0]
    rows, embedded = excel.read_sheet(path, sheet)
    if not images:
        embedded = {}
    spec = s["rows"] or f"{s['header_row'] + 1}-"
    numbers = excel.parse_row_spec(spec, len(rows))
    items = excel.build_products(rows, s["header_row"], numbers, s["mapping"], s["defaults"], embedded,
                                 pricing.Pricer(), s["id"])
    if s["clean_names"]:
        excel.clean_names(items)
    return items, embedded


PREVIEW_RUN = -1


def preview(supplier_id: int, force: bool = False, transport=None) -> dict:
    """«👁 Что изменится»: обновление прайса понарошку — посчитать новые товары, изменения цен и пропавшие,
    ничего не записав (и не затронув сохранённый прайс)."""
    s = _parse(_row(supplier_id))
    if not any(t == "external_id" for t in s["mapping"].values()):
        raise SupplierError("Не выбрана колонка «Артикул / код» — по ней товары узнаются при следующих обновлениях")
    tmp = None
    with _busy(supplier_id):
        try:
            path, _, _, tmp = _fetch_source(s, transport)
            rates.prefetch()
            items, _ = _parse_items(s, path, images=False)

            def apply_and_queue():
                changed: list[int] = []
                # run_id = -1: ни одна строка прайса не «видена» в этом прогоне — пропавшие считаются как в настоящем
                stats, _ = apply_items(s, items, {}, PREVIEW_RUN, force, changed)
                if s["auto_sync"]:
                    _queue_changed(changed)  # сколько ушло бы на Prom (очередь тоже откатится)
                return stats, changed

            result = changes.preview(apply_and_queue)
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
    stats = result.pop("result")[0]
    result.update(total=stats["total"], valid=stats["valid"], errors=stats["errors"],
                  error_samples=stats["error_samples"][:10], unchanged=stats["unchanged"], ignored=stats.get("ignored", 0))
    return result


def _run(s: dict, trigger: str, force: bool, transport) -> dict:
    supplier_id = s["id"]
    with db.tx() as c:
        run_id = c.execute(
            "INSERT INTO supplier_runs (supplier_id, status, trigger, started_at) VALUES (?, 'running', ?, ?)",
            (supplier_id, trigger, db.now())).lastrowid
    status, stats, message = "ok", {}, ""
    changed: list[int] = []
    tmp = None
    try:
        if not any(t == "external_id" for t in s["mapping"].values()):
            raise SupplierError("Не выбрана колонка «Артикул / код» — по ней товары узнаются при следующих обновлениях")
        path, content, name, tmp = _fetch_source(s, transport)
        rates.prefetch()  # курс — до записи в базу
        items, embedded = _parse_items(s, path)
        stats, _ = apply_items(s, items, embedded, run_id, force, changed)
        if content is not None:
            store_source(supplier_id, content, name)  # прайс разобран и применён — теперь он текущий
    except (SupplierError, excel.ImportError_, rates.RateError) as exc:
        status, message = "failed", str(exc)
    except Exception as exc:
        log.exception("Поставщик %s: сбой обновления", supplier_id)
        status, message = "failed", f"Внутренняя ошибка: {exc}"
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    if s["auto_sync"]:
        # даже если обновление оборвалось на середине: уже изменённое (записано частями) должно уйти на Prom,
        # иначе при следующем обновлении эти товары выглядят «без изменений» и остались бы со старой ценой
        try:
            stats["queued"] = _queue_changed(changed)
        except Exception:
            log.exception("Поставщик %s: не удалось поставить изменения в очередь", supplier_id)
    if status == "failed":
        notify.send("supplier_failed", f"⚠️ <b>Поставщик «{notify.esc(s['name'])}» не обновился</b>\n{notify.esc(message)}")
    with db.tx() as c:
        c.execute("UPDATE supplier_runs SET status = ?, stats = ?, message = ?, finished_at = ? WHERE id = ?",
                  (status, json.dumps(stats, ensure_ascii=False), message, db.now(), run_id))
        c.execute("UPDATE suppliers SET next_run_at = ? WHERE id = ?", (_next_run(s["interval_hours"]), supplier_id))
    return _run_dict(db.query_one("SELECT * FROM supplier_runs WHERE id = ?", (run_id,)))


def due() -> list[int]:
    rows = db.query(
        "SELECT id FROM suppliers WHERE interval_hours > 0 AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY next_run_at",
        (db.now(),))
    return [r["id"] for r in rows if r["id"] not in _running]


def recover() -> None:
    """После перезапуска сервера незавершённые запуски помечаются как прерванные."""
    with db.tx() as c:
        c.execute("UPDATE supplier_runs SET status = 'failed', message = 'Прервано перезапуском сервера', finished_at = ? "
                  "WHERE status = 'running'", (db.now(),))


# ---------- правки и цены ----------

def reapply(product_id: int) -> None:
    """После снятия закрепления возвращает данные поставщика в незакреплённые поля."""
    # данные основного поставщика (того, кто ведёт описание и фото)
    item = db.query_one("SELECT si.* FROM supplier_items si JOIN products p ON p.id = si.product_id "
                        "WHERE si.product_id = ? ORDER BY si.supplier_id = p.supplier_id DESC, si.id LIMIT 1", (product_id,))
    if item is None:
        return
    multi = db.query_one("SELECT COUNT(*) AS n FROM supplier_items WHERE product_id = ?", (product_id,))["n"] > 1
    stored = json.loads(item["data"])
    data = dict(stored["data"])
    pricing.Pricer().apply(data, item["supplier_id"])
    fields = _item_fields({"data": data, "params": stored["params"]})
    if multi:
        fields = {k: v for k, v in fields.items() if k not in products.COMMERCIAL}
    trash: list[str] = []
    with db.tx() as c:
        p = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        locked = set(json.loads(p["locked_fields"] or "[]"))
        updates = {k: v for k, v in fields.items() if k not in locked and p[k] != v}
        if item["missing"]:
            updates.pop("presence", None)
            updates.pop("quantity", None)
        images = "images" not in locked and stored["image_urls"]
        if images:
            _replace_images(c, product_id, stored["image_urls"], [], trash)
        elif "images" not in locked:
            c.execute("UPDATE supplier_items SET images_hash = '' WHERE id = ?", (item["id"],))
        if updates or images:
            products._touch(c, product_id, updates, p["status"])
        if multi:
            recompute_offer(c, product_id, pricing.Pricer())
    _drop_files(trash)


def recalc_prices(ids: list[int] | None = None, send: bool | None = None) -> dict:
    """Пересчёт цен по текущим правилам наценки и курсу (кроме цен, закреплённых вручную).

    ids — только эти товары (None — все с закупкой/РРЦ). send — отправить изменённые цены товаров, которые
    уже на Prom (None — по настройке «Наценка» → «сразу отправлять новые цены»).
    """
    rates.prefetch()
    pricer = pricing.Pricer()
    where = "WHERE (cost_price IS NOT NULL OR rrp IS NOT NULL)"
    args: list = []
    if ids is not None:
        where += f" AND id IN ({','.join('?' * len(ids))})" if ids else " AND 0"
        args = list(ids)
    rows = db.query("SELECT id, supplier_id, cost_price, rrp, cost_currency, price, currency, group_name, status, "
                    f"locked_fields, synced_at FROM products {where}", args)
    changed, warnings = [], Counter()
    multi = {r["product_id"] for r in db.query(
        "SELECT product_id FROM supplier_items WHERE product_id IS NOT NULL GROUP BY product_id HAVING COUNT(*) > 1")}
    for start in range(0, len(rows), CHUNK):
        with db.tx() as c:
            for old in rows[start:start + CHUNK]:
                # перечитать в транзакции: за время пересчёта цену могли закрепить руками или товар — удалить
                p = c.execute("SELECT id, supplier_id, cost_price, rrp, cost_currency, price, currency, group_name, "
                              "status, locked_fields, synced_at FROM products WHERE id = ?", (old["id"],)).fetchone()
                if p is None or "price" in json.loads(p["locked_fields"] or "[]"):
                    continue
                if p["id"] in multi:  # несколько поставщиков — цена по лучшему предложению и его правилу
                    if recompute_offer(c, p["id"], pricer):
                        changed.append(p["id"])
                    continue
                data = {"cost_price": p["cost_price"], "rrp": p["rrp"], "group_name": p["group_name"],
                        "cost_currency": p["cost_currency"], "currency": p["currency"], "price": p["price"]}
                for w in pricer.apply(data, p["supplier_id"]):
                    if w.startswith("Цена не пересчитана"):
                        warnings[w] += 1
                updates = {k: data[k] for k in ("price", "currency") if data.get(k) is not None and data[k] != p[k]}
                if updates:
                    products._touch(c, p["id"], updates, p["status"])
                    changed.append(p["id"])
    if send is None:
        send = rates.settings()["auto_send"]
    on_prom = {p["id"] for p in rows if p["synced_at"]}
    auto = {r["id"] for r in db.query("SELECT id FROM suppliers WHERE auto_sync = 1")}
    by_id = {p["id"]: p["supplier_id"] for p in rows}
    to_queue = [pid for pid in changed if (send and pid in on_prom) or by_id.get(pid) in auto]
    return {"changed": len(changed), "queued": _queue_changed(to_queue),
            "warnings": [f"{w} (товаров: {n})" for w, n in warnings.items()]}

PRICE_ACTIONS = ("as_cost", "percent", "recalc")


def bulk_prices(ids: list[int], action: str, value: float = 0, currency: str = "") -> dict:
    """«💲 Цены» для выделенных товаров.

    as_cost — текущая цена это опт: переносим её в закупку (в её валюте или в currency, если цену загрузили
              с неверной валютой — например, доллары как гривны) и считаем розничную по наценке и курсу;
    percent — поднять/снизить цену на value % (цена закрепляется как ручная);
    recalc  — снять ручное закрепление цены и пересчитать по наценке и курсу.
    Изменённые цены товаров, которые уже на Prom, отправляются сразу, если так настроено на странице «Наценка».
    """
    from . import rates

    if action not in PRICE_ACTIONS:
        raise SupplierError("Неизвестное действие с ценами")
    currency = (currency or "").upper()
    if currency and currency != "UAH" and currency not in rates.CURRENCIES:
        raise SupplierError(f"Неизвестная валюта {currency}")
    ids = [int(i) for i in ids]
    if not ids:
        return {"changed": 0, "queued": 0, "warnings": []}
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT * FROM products WHERE id IN ({marks})", ids)
    warnings = []
    if action == "percent":
        if not -90 <= value <= 1000:
            raise SupplierError("Изменение цены — от -90 до 1000%")
        changed = []
        with db.tx() as c:
            for old in rows:
                p = c.execute("SELECT id, price, status FROM products WHERE id = ?", (old["id"],)).fetchone()
                if p is None or not p["price"]:
                    continue
                new = round(p["price"] * (1 + value / 100), 2)
                if new != p["price"]:
                    products._lock(c, p["id"], ["price"])
                    products._touch(c, p["id"], {"price": new}, p["status"])
                    changed.append(p["id"])
        send = rates.settings()["auto_send"]
        on_prom = [p["id"] for p in rows if p["synced_at"] and p["id"] in changed]
        return {"changed": len(changed), "queued": _queue_changed(on_prom) if send else 0, "warnings": warnings}

    with db.tx() as c:
        for p in rows:
            locked = [f for f in json.loads(p["locked_fields"] or "[]") if f != "price"]
            fields = {"locked_fields": json.dumps(locked)}
            if action == "as_cost":
                if p["cost_price"] is not None:
                    # закупка уже есть (кнопку нажали второй раз) — розничную цену в закупку не превращаем,
                    # только поправляем валюту, если её выбрали
                    if not currency or currency == (p["cost_currency"] or "UAH").upper():
                        continue
                    fields.update(cost_currency=currency)
                elif p["rrp"] is not None:   # из прайса в $ пришла только цена — она в РРЦ
                    fields.update(cost_price=p["rrp"], cost_currency=currency or p["cost_currency"] or p["currency"], rrp=None)
                elif p["price"]:
                    fields.update(cost_price=p["price"], cost_currency=currency or p["currency"] or "UAH")
                else:
                    continue
            changes.record_diff(c, p["id"], p, {k: v for k, v in fields.items() if k != "locked_fields"})
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE products SET {sets} WHERE id = ?", list(fields.values()) + [p["id"]])
    if not pricing.list_rules():
        warnings.append("Правил наценки нет — цена равна закупке по курсу. Добавьте правило на странице «Наценка»")
    result = recalc_prices(ids)
    result["warnings"] = warnings + result["warnings"]
    return result
