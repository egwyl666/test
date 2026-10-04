"""Товары: хранение, нормализация, проверка перед отправкой, фото."""

import io
import json
import re
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from . import db

PRESENCE = {
    "available": "В наличии",
    "order": "Под заказ",
    "not_available": "Нет в наличии",
}

STATUSES = {
    "draft": "Черновик",
    "ready": "Готов к отправке",
    "sending": "Отправляется",
    "synced": "На Prom",
    "error": "Ошибка",
}

TEXT_FIELDS = (
    "external_id", "name", "name_ua", "description", "description_ua", "currency",
    "unit", "presence", "group_name", "vendor", "country", "keywords", "barcode",
    "vendor_code", "cost_currency",
)
NUMBER_FIELDS = ("price", "old_price", "cost_price", "rrp")
INT_FIELDS = ("quantity",)
EDITABLE = TEXT_FIELDS + NUMBER_FIELDS + INT_FIELDS + ("params",)
# Поля, которые при ручной правке закрепляются: обновление прайса поставщика их больше не перезапишет.
LOCKABLE = tuple(f for f in EDITABLE if f not in ("external_id", "cost_price", "rrp")) + ("images",)

MAX_IMAGES = 10
MAX_IMAGE_SIDE = 4000
KEEP_FORMATS = {"JPEG": "jpg", "PNG": "png", "GIF": "gif"}


class ProductError(ValueError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


# ---------- нормализация ----------

_num_junk = re.compile(r"[^\d,.\-]")
_sci = re.compile(r"^[+-]?\d+(?:[.,]\d+)?[eE][+-]?\d+$")
_currency_words = re.compile(r"грн\.?|uah|usd|eur|руб\.?|₴|\$|€", re.I)
# символы, недопустимые в XML (из-за них Prom отклонит весь файл импорта); \x0b — перенос строки из Excel
_xml_bad = re.compile("[\x00-\x08\x0c\x0e-\x1f\ufffe\uffff]")


def clean_text(value) -> str:
    """Убирает невидимые управляющие символы; перенос строки Excel (Alt+Enter) превращает в обычный."""
    return _xml_bad.sub("", str(value).replace("\x0b", "\n"))


def parse_number(value) -> float | None:
    """'1 299,50 грн' -> 1299.5; пустое -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    original = str(value).strip()
    if original in {"", "-", "—", "–"}:
        return None
    if _sci.match(original):  # 1,2E+03 — так Excel иногда сохраняет числа в CSV
        number = float(original.replace(",", "."))
        if number != number or abs(number) > 1e12:
            raise ProductError(f"Не похоже на число: {value!r}")
        return number
    if re.search(r"[A-Za-zА-Яа-яЁёІіЇїЄє∞]", _currency_words.sub("", original)):
        raise ProductError(f"Не похоже на число: {value!r}")
    text = _num_junk.sub("", original.replace(" ", ""))
    if not any(ch.isdigit() for ch in text):
        raise ProductError(f"Не похоже на число: {value!r}")
    if "," in text and "." in text:
        # последний разделитель — десятичный
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        raise ProductError(f"Не похоже на число: {value!r}")


def parse_int(value) -> int | None:
    number = parse_number(value)
    return None if number is None else int(round(number))


_presence_words = {
    "available": {"available", "в наличии", "в наявності", "есть", "є", "да", "так", "+", "true", "1", "yes"},
    "order": {"order", "под заказ", "під замовлення", "заказ", "замовлення"},
    "not_available": {"not_available", "нет в наличии", "немає в наявності", "нет", "ні", "немає", "-", "false", "0", "no"},
}


def parse_presence(value) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    for key, words in _presence_words.items():
        if text in words:
            return key
    raise ProductError(f"Неизвестное значение наличия: {value!r}")


def parse_params(value) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            raise ProductError("Характеристики переданы в неверном формате")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProductError("Характеристики должны быть списком «название — значение»")
    params = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name", "")).strip()
        val = clean_text(item.get("value", "")).strip()
        if name or val:
            params.append({"name": name, "value": val})
    return params


def normalize(data: dict) -> dict:
    """Приводит пришедшие поля к типам базы. Незнакомые поля игнорируются."""
    out = {}
    for key, value in data.items():
        if key not in EDITABLE:
            continue
        try:
            if key in TEXT_FIELDS:
                out[key] = "" if value is None else clean_text(value).strip()
            elif key in NUMBER_FIELDS:
                out[key] = parse_number(value)
            elif key in INT_FIELDS:
                out[key] = parse_int(value)
            elif key == "params":
                out[key] = json.dumps(parse_params(value), ensure_ascii=False)
            if key == "presence":
                out[key] = parse_presence(out[key]) or "available"
        except ProductError as exc:
            raise ProductError(str(exc), field=key)  # интерфейс подсветит именно это поле
    if "presence" in out:
        out["presence"] = parse_presence(out["presence"]) or "available"
    if "currency" in out:
        out["currency"] = (out["currency"] or "UAH").upper()
    if "cost_currency" in out:
        out["cost_currency"] = (out["cost_currency"] or "").upper()
        if out["cost_currency"] not in ("", "UAH", "USD", "EUR", "PLN", "GBP"):
            raise ProductError("Валюта закупки: UAH, USD, EUR, PLN или GBP", "cost_currency")
    if out.get("external_id") == "":
        out.pop("external_id")
    return out


# ---------- чтение ----------

def _row_to_dict(row) -> dict:
    p = dict(row)
    p["params"] = json.loads(p["params"] or "[]")
    p["locked_fields"] = json.loads(p.get("locked_fields") or "[]")
    return p


def image_rows(product_id: int) -> list[dict]:
    rows = db.query("SELECT * FROM images WHERE product_id = ? ORDER BY position, id", (product_id,))
    return [dict(r) for r in rows]


def image_src(img: dict, base_url: str = "") -> str:
    """Ссылка на фото. base_url нужен для фида: Prom скачивает фото по абсолютному адресу."""
    if img.get("url"):
        return img["url"]
    return f"{base_url}/media/{img['file']}"


def get(product_id: int) -> dict:
    row = db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
    if row is None:
        raise KeyError(product_id)
    p = _row_to_dict(row)
    p["images"] = [{"id": i["id"], "src": image_src(i), "external": bool(i["url"])} for i in image_rows(product_id)]
    p["check"] = validate(p)
    p["offers"] = offers(product_id)
    p["cost_uah"] = None
    if p["cost_price"] is not None and (p["cost_currency"] or "UAH") != "UAH":
        from . import rates
        try:
            rate = rates.Table().rate(p["cost_currency"], p["supplier_id"])
            p["cost_uah"] = round(p["cost_price"] * rate, 2) if rate else None
        except rates.RateError:
            pass
    p["supplier"] = None
    if p["supplier_id"]:
        s = db.query_one("SELECT id, name FROM suppliers WHERE id = ?", (p["supplier_id"],))
        item = db.query_one("SELECT missing FROM supplier_items WHERE product_id = ?", (product_id,))
        if s:
            p["supplier"] = {"id": s["id"], "name": s["name"], "missing": bool(item and item["missing"])}
    return p


COMMERCIAL = ("price", "old_price", "cost_price", "rrp", "presence", "quantity", "currency", "cost_currency")


def choose_offer(items: list[dict]) -> dict | None:
    """Лучшее предложение: самая низкая закупка среди тех, у кого товар есть; иначе — среди оставшихся в прайсах."""
    def cost(i):
        d = i["data"]
        value = d.get("cost_price") if d.get("cost_price") is not None else d.get("price")
        return float("inf") if value is None else value

    live = [i for i in items if not i["missing"]]
    in_stock = [i for i in live if i["data"].get("presence", "available") != "not_available"]
    pool = in_stock or live
    return min(pool, key=lambda i: (cost(i), i["supplier_id"])) if pool else None


def offer_items(product_id: int) -> list[dict]:
    rows = db.query("""SELECT si.supplier_id, si.sku, si.missing, si.data, s.name AS supplier_name
                       FROM supplier_items si JOIN suppliers s ON s.id = si.supplier_id
                       WHERE si.product_id = ? ORDER BY si.supplier_id""", (product_id,))
    return [{**dict(r), "data": json.loads(r["data"])["data"]} for r in rows]


def offers(product_id: int) -> list[dict]:
    """Предложения поставщиков по товару (для карточки)."""
    items = offer_items(product_id)
    best = choose_offer(items)
    return [{
        "supplier_id": i["supplier_id"], "supplier_name": i["supplier_name"], "sku": i["sku"],
        "cost_price": i["data"].get("cost_price"), "price": i["data"].get("price"), "rrp": i["data"].get("rrp"),
        "presence": i["data"].get("presence", "available"), "quantity": i["data"].get("quantity"),
        "missing": bool(i["missing"]), "active": best is not None and i is best,
    } for i in items]


def list_products(status: str = "", q: str = "", limit: int = 200, offset: int = 0, supplier_id: int | None = None) -> dict:
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if supplier_id == 0:
        where.append("supplier_id IS NULL")
    elif supplier_id:
        where.append("supplier_id = ?")
        args.append(supplier_id)
    if q:
        where.append("(name LIKE ? OR name_ua LIKE ? OR external_id LIKE ? OR group_name LIKE ?)")
        args += [f"%{q}%"] * 4
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one(f"SELECT COUNT(*) AS n FROM products {clause}", args)["n"]
    rows = db.query(
        f"""SELECT p.*, (SELECT COALESCE(i.url, '/media/' || i.file) FROM images i
                         WHERE i.product_id = p.id ORDER BY i.position, i.id LIMIT 1) AS thumb,
                        (SELECT COUNT(*) FROM images i WHERE i.product_id = p.id) AS image_count,
                        (SELECT s.name FROM suppliers s WHERE s.id = p.supplier_id) AS supplier_name
            FROM products p {clause} ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?""",
        args + [limit, offset],
    )
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM products GROUP BY status")}
    items = []
    for r in rows:
        p = _row_to_dict(r)
        p["check"] = validate(p, image_count=p["image_count"])
        items.append(p)
    return {"items": items, "total": total, "counts": counts}


def find_by_external_id(external_id: str) -> int | None:
    row = db.query_one("SELECT id FROM products WHERE external_id = ?", (external_id,))
    return row["id"] if row else None


# ---------- проверка ----------

_UA_ONLY = re.compile(r"[іїєґІЇЄҐ]")
_RU_ONLY = re.compile(r"[ыэъёЫЭЪЁ]")


def looks_ukrainian(text: str) -> bool:
    """Текст на украинском: есть і/ї/є/ґ и нет ы/э/ъ/ё (русский текст их почти всегда содержит)."""
    return bool(_UA_ONLY.search(text or "")) and not _RU_ONLY.search(text or "")


def validate(p: dict, image_count: int | None = None) -> dict:
    """errors — отправлять нельзя; warnings — можно, но карточка будет слабой."""
    errors, warnings = [], []
    if not (p.get("name") or "").strip():
        errors.append("Нет названия")
    elif len(p["name"]) > 255:
        errors.append("Название длиннее 255 символов")
    price = p.get("price")
    if price is None:
        errors.append("Не указана цена")
    elif price <= 0:
        errors.append("Цена должна быть больше нуля")
    cost = p.get("cost_price")
    if cost is not None and price is not None and price < cost:
        warnings.append("Цена ниже закупочной")
    old_price = p.get("old_price")
    if old_price is not None and price is not None and old_price <= price:
        warnings.append("Старая цена не больше текущей — скидка не покажется")
    if image_count is None:
        image_count = len(p.get("images") or [])
    if image_count == 0:
        warnings.append("Нет фото — такие товары почти не покупают")
    if not (p.get("description") or p.get("description_ua") or "").strip():
        warnings.append("Нет описания")
    if not (p.get("group_name") or "").strip():
        warnings.append("Не указана группа — на Prom товар попадёт в группу «Без группы»")
    if not (p.get("name_ua") or "").strip() and not looks_ukrainian(p.get("name") or ""):
        warnings.append("Нет названия на украинском")
    return {"errors": errors, "warnings": warnings, "ok": not errors}


# ---------- запись ----------

def create(data: dict) -> int:
    fields = normalize(data)
    external_id = fields.pop("external_id", None)
    ts = db.now()
    with db.tx() as c:
        if external_id and c.execute("SELECT 1 FROM products WHERE external_id = ?", (external_id,)).fetchone():
            raise ProductError(f"Артикул {external_id} уже занят другим товаром")
        cols = ["created_at", "updated_at"] + list(fields)
        cur = c.execute(
            f"INSERT INTO products ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [ts, ts] + list(fields.values()),
        )
        product_id = cur.lastrowid
        from . import changes
        changes.record(c, product_id, "created", "", fields.get("name") or "")
        c.execute(
            "UPDATE products SET external_id = ? WHERE id = ?",
            (external_id or f"PL-{product_id:06d}", product_id),
        )
    return product_id


def update(product_id: int, data: dict, lock: bool = True) -> dict:
    """lock=True — правка руками: у товара поставщика изменённые поля закрепляются."""
    fields = normalize(data)
    with db.tx() as c:
        row = c.execute("SELECT status FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise KeyError(product_id)
        if "external_id" in fields:
            clash = c.execute(
                "SELECT 1 FROM products WHERE external_id = ? AND id != ?", (fields["external_id"], product_id)
            ).fetchone()
            if clash:
                raise ProductError(f"Артикул {fields['external_id']} уже занят другим товаром")
        if lock:
            _lock(c, product_id, [f for f in fields if f in LOCKABLE])
        if {"cost_price", "rrp", "cost_currency"} & set(fields) and "price" not in fields:
            fields.update(_priced(c, product_id, fields))
        _touch(c, product_id, fields, row["status"])
    return get(product_id)


def _priced(c, product_id: int, fields: dict) -> dict:
    """Закупку поменяли руками — розничная цена сразу по наценке и курсу (если цена не закреплена вручную)."""
    from . import pricing

    row = dict(c.execute("SELECT cost_price, rrp, cost_currency, currency, price, group_name, supplier_id, locked_fields "
                         "FROM products WHERE id = ?", (product_id,)).fetchone())
    if "price" in json.loads(row.pop("locked_fields") or "[]"):
        return {}
    data = {**row, **fields}
    if not data.get("cost_currency"):
        data["cost_currency"] = data.get("currency") or "UAH"
    pricing.Pricer().apply(data, row["supplier_id"])
    out = {"cost_currency": data["cost_currency"]}
    if data.get("price") is not None and data["price"] != row["price"]:
        out.update(price=data["price"], currency=data["currency"])
    return out


def _lock(c, product_id: int, names: list[str]) -> None:
    if not names:
        return
    row = c.execute("SELECT supplier_id, cost_price, rrp, locked_fields FROM products WHERE id = ?", (product_id,)).fetchone()
    # закрепление нужно только товарам, которые что-то может перезаписать: прайс поставщика или пересчёт наценки
    if row is None or (row["supplier_id"] is None and row["cost_price"] is None and row["rrp"] is None):
        return
    locked = json.loads(row["locked_fields"] or "[]")
    merged = sorted(set(locked) | set(names))
    if merged != sorted(locked):
        c.execute("UPDATE products SET locked_fields = ? WHERE id = ?", (json.dumps(merged), product_id))


def unlock(product_id: int, names: list[str]) -> None:
    with db.tx() as c:
        row = c.execute("SELECT locked_fields FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise KeyError(product_id)
        locked = [f for f in json.loads(row["locked_fields"] or "[]") if f not in names]
        c.execute("UPDATE products SET locked_fields = ? WHERE id = ?", (json.dumps(locked), product_id))


NOT_TRACKED = {"status", "supplier_id", "locked_fields", "last_error", "pending_fields"}


def _touch(c, product_id: int, fields: dict, status: str) -> None:
    """Любое изменение поднимает ревизию. Уже выгруженный товар снова ждёт отправки.

    Запоминаем, какие поля поменялись с последней отправки: если только цена/наличие —
    хватит быстрого обновления вместо полного импорта. Пустой fields — это изменение фото.
    """
    from . import changes

    fields = dict(fields)
    changes.record_diff(c, product_id, c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone(), fields)
    changed = [f for f in fields if f not in NOT_TRACKED] or (["images"] if not fields else [])
    if changed:
        row = c.execute("SELECT pending_fields FROM products WHERE id = ?", (product_id,)).fetchone()
        pending = set(json.loads(row["pending_fields"] or "[]")) if row else set()
        fields["pending_fields"] = json.dumps(sorted(pending | set(changed)))
    if status in ("synced", "error", "sending"):
        fields["status"] = "ready"
    sets = ", ".join(f"{k} = ?" for k in fields)
    c.execute(
        f"UPDATE products SET {sets + ', ' if sets else ''}revision = revision + 1, updated_at = ? WHERE id = ?",
        list(fields.values()) + [db.now(), product_id],
    )


def mark_pending(c, product_id: int, names: list[str]) -> None:
    """Добавить поля в список «изменилось с последней отправки» (например, фото, заменённые поставщиком)."""
    row = c.execute("SELECT pending_fields FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        return
    pending = set(json.loads(row["pending_fields"] or "[]")) | set(names)
    c.execute("UPDATE products SET pending_fields = ? WHERE id = ?", (json.dumps(sorted(pending)), product_id))


def delete(ids: list[int]) -> int:
    files = []
    with db.tx() as c:
        from . import changes
        for pid in ids:
            files += [r["file"] for r in c.execute("SELECT file FROM images WHERE product_id = ? AND file IS NOT NULL", (pid,))]
            row = c.execute("SELECT name, external_id FROM products WHERE id = ?", (pid,)).fetchone()
            if row:
                changes.record(c, pid, "deleted", "", f"{row['name']} ({row['external_id']})")
            c.execute("DELETE FROM products WHERE id = ?", (pid,))
    for name in files:
        (db.uploads_dir() / name).unlink(missing_ok=True)
    return len(ids)


def set_status(ids: list[int], status: str) -> int:
    """draft / ready — как раньше; synced — «Вернуть «На Prom»»: только товарам, которые уже выгружались.
    Неотправленные изменения при этом не теряются — уйдут со следующей отправкой."""
    if status == "synced":
        with db.tx() as c:
            marks = ",".join("?" * len(ids))
            return c.execute(f"UPDATE products SET status = 'synced', last_error = '', updated_at = ? "
                             f"WHERE id IN ({marks}) AND synced_at IS NOT NULL AND status != 'sending'",
                             [db.now(), *ids]).rowcount if ids else 0
    if status not in ("draft", "ready"):
        raise ProductError("Вручную можно ставить только «черновик» или «готов»")
    with db.tx() as c:
        for pid in ids:
            c.execute(
                "UPDATE products SET status = ?, updated_at = ? WHERE id = ? AND status != 'sending'",
                (status, db.now(), pid),
            )
    return len(ids)


# ---------- фото ----------

def store_image_bytes(content: bytes) -> str:
    """Проверяет, что это картинка, при необходимости пережимает в JPEG. Возвращает имя файла."""
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ProductError("Файл не похож на изображение")
    fmt = img.format or ""
    too_big = max(img.size) > MAX_IMAGE_SIDE
    if fmt in KEEP_FORMATS and not too_big:
        ext, data = KEEP_FORMATS[fmt], content
    else:
        img = ImageOps.exif_transpose(img)
        if too_big:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        if img.mode not in ("RGB", "L"):
            background = Image.new("RGB", img.size, "white")
            background.paste(img, mask=img.convert("RGBA").split()[-1])
            img = background
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        ext, data = "jpg", buf.getvalue()
    name = f"{uuid.uuid4().hex}.{ext}"
    (db.uploads_dir() / name).write_bytes(data)
    return name


def _next_position(c, product_id: int) -> int:
    count = c.execute("SELECT COUNT(*) FROM images WHERE product_id = ?", (product_id,)).fetchone()[0]
    if count >= MAX_IMAGES:
        raise ProductError(f"У товара уже {MAX_IMAGES} фото — это максимум для Prom")
    row = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM images WHERE product_id = ?", (product_id,)).fetchone()
    return row[0]


def _product_status(c, product_id: int) -> str:
    row = c.execute("SELECT status FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise KeyError(product_id)
    return row["status"]


def add_image_file(product_id: int, content: bytes) -> dict:
    name = store_image_bytes(content)
    try:
        with db.tx() as c:
            status = _product_status(c, product_id)
            pos = _next_position(c, product_id)
            cur = c.execute("INSERT INTO images (product_id, file, position) VALUES (?, ?, ?)", (product_id, name, pos))
            _lock(c, product_id, ["images"])
            _touch(c, product_id, {}, status)
    except BaseException:
        (db.uploads_dir() / name).unlink(missing_ok=True)
        raise
    return {"id": cur.lastrowid, "src": f"/media/{name}", "external": False}


def add_image_url(product_id: int, url: str) -> dict:
    url = url.strip()
    if not re.match(r"^https?://\S+$", url):
        raise ProductError("Ссылка на фото должна начинаться с http:// или https://")
    with db.tx() as c:
        status = _product_status(c, product_id)
        pos = _next_position(c, product_id)
        cur = c.execute("INSERT INTO images (product_id, url, position) VALUES (?, ?, ?)", (product_id, url, pos))
        _lock(c, product_id, ["images"])
        _touch(c, product_id, {}, status)
    return {"id": cur.lastrowid, "src": url, "external": True}


def delete_image(product_id: int, image_id: int) -> None:
    with db.tx() as c:
        row = c.execute("SELECT file FROM images WHERE id = ? AND product_id = ?", (image_id, product_id)).fetchone()
        if row is None:
            raise KeyError(image_id)
        c.execute("DELETE FROM images WHERE id = ?", (image_id,))
        _lock(c, product_id, ["images"])
        _touch(c, product_id, {}, _product_status(c, product_id))
    if row["file"]:
        (db.uploads_dir() / row["file"]).unlink(missing_ok=True)


def reorder_images(product_id: int, image_ids: list[int]) -> None:
    with db.tx() as c:
        existing = {r["id"] for r in c.execute("SELECT id FROM images WHERE product_id = ?", (product_id,))}
        if set(image_ids) != existing:
            raise ProductError("Список фото устарел — обновите страницу")
        for pos, image_id in enumerate(image_ids):
            c.execute("UPDATE images SET position = ? WHERE id = ?", (pos, image_id))
        _lock(c, product_id, ["images"])
        _touch(c, product_id, {}, _product_status(c, product_id))


def replace_images(product_id: int, urls: list[str], files: list[bytes]) -> None:
    """Используется массовым импортом: фото из Excel заменяют прежние."""
    names = [store_image_bytes(b) for b in files]
    with db.tx() as c:
        status = _product_status(c, product_id)
        old = [r["file"] for r in c.execute("SELECT file FROM images WHERE product_id = ? AND file IS NOT NULL", (product_id,))]
        c.execute("DELETE FROM images WHERE product_id = ?", (product_id,))
        pos = 0
        for name in names:
            c.execute("INSERT INTO images (product_id, file, position) VALUES (?, ?, ?)", (product_id, name, pos))
            pos += 1
        for url in urls:
            c.execute("INSERT INTO images (product_id, url, position) VALUES (?, ?, ?)", (product_id, url, pos))
            pos += 1
        _lock(c, product_id, ["images"])
        _touch(c, product_id, {}, status)
    for name in old:
        (db.uploads_dir() / name).unlink(missing_ok=True)
