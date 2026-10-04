"""Журнал изменений товаров: что поменялось, было → стало, кто поменял и когда.

Запись делает products._touch (через него проходят все изменения товара) и несколько мест, где товар
создаётся, выгружается или удаляется. «Кто» берётся из контекста: по умолчанию «Вручную», поставщик,
пересчёт по курсу, ИИ и т. д. оборачивают свою работу в with changes.source("…").
"""

import contextvars
import csv
import io
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import db

KEEP_DAYS = 180
MAX_TEXT = 400
_source: contextvars.ContextVar[str] = contextvars.ContextVar("change_source", default="Вручную")

LABELS = {
    "price": "Цена", "old_price": "Старая цена", "cost_price": "Закупка", "cost_currency": "Валюта закупки",
    "rrp": "РРЦ", "currency": "Валюта", "presence": "Наличие", "quantity": "Количество", "name": "Название",
    "name_ua": "Название (укр.)", "description": "Описание", "description_ua": "Описание (укр.)",
    "group_name": "Группа", "keywords": "Ключевые слова", "params": "Характеристики", "images": "Фото",
    "vendor": "Производитель", "country": "Страна", "barcode": "Штрихкод", "vendor_code": "Артикул поставщика",
    "external_id": "Артикул", "unit": "Ед. изм.", "created": "Создан", "deleted": "Удалён", "prom": "Prom",
}
# не показываем в журнале: служебные поля
SKIP = {"status", "supplier_id", "locked_fields", "last_error", "pending_fields", "revision", "updated_at",
        "synced_at", "prom_id", "created_at"}


@contextmanager
def source(text: str):
    token = _source.set(text)
    try:
        yield
    finally:
        _source.reset(token)


def current_source() -> str:
    return _source.get()


def _text(value) -> str:
    if value is None:
        return ""
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def record(c, product_id: int, field: str, old="", new="", who: str | None = None) -> None:
    if field == "presence":  # «В наличии» вместо available
        from .products import PRESENCE

        old, new = PRESENCE.get(old, old), PRESENCE.get(new, new)
    c.execute("INSERT INTO product_changes (product_id, at, source, field, old, new) VALUES (?, ?, ?, ?, ?, ?)",
              (product_id, db.now(), who or _source.get(), field, _text(old), _text(new)))


def record_diff(c, product_id: int, before, fields: dict) -> None:
    """before — строка товара до изменения, fields — что записывается."""
    if before is None:
        return
    if not fields:
        record(c, product_id, "images", "", "фото изменены")
        return
    for key, value in fields.items():
        if key in SKIP or key not in before.keys():
            continue
        if before[key] != value and not (before[key] in (None, "") and value in (None, "")):
            record(c, product_id, key, before[key], value)


def cleanup() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    with db.tx() as c:
        return c.execute("DELETE FROM product_changes WHERE at < ?", (cutoff,)).rowcount


# ---------- просмотр ----------

def _where(period: str = "", who: str = "", field: str = "", q: str = "", product_id: int | None = None):
    where, args = [], []
    days = {"today": 0, "7d": 7, "30d": 30}.get(period)
    if days is not None:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        where.append("ch.at >= ?")
        args.append(start.astimezone(timezone.utc).isoformat(timespec="seconds"))
    if who:
        where.append("ch.source = ?")
        args.append(who)
    if field:
        if field == "other":
            where.append(f"ch.field NOT IN ({','.join('?' * len(MAIN_FIELDS))})")
            args += MAIN_FIELDS
        else:
            where.append("ch.field = ?")
            args.append(field)
    if q:
        where.append("(p.name LIKE ? OR p.external_id LIKE ? OR ch.new LIKE ?)")
        args += [f"%{q}%"] * 3
    if product_id is not None:
        where.append("ch.product_id = ?")
        args.append(product_id)
    return ("WHERE " + " AND ".join(where)) if where else "", args


MAIN_FIELDS = ["price", "presence", "quantity", "name", "description", "images", "created", "deleted", "prom"]


def search(period: str = "7d", who: str = "", field: str = "", q: str = "", product_id: int | None = None,
           limit: int = 200, offset: int = 0) -> dict:
    clause, args = _where(period, who, field, q, product_id)
    base = f"FROM product_changes ch LEFT JOIN products p ON p.id = ch.product_id {clause}"
    rows = db.query(f"SELECT ch.*, p.name AS product_name, p.external_id AS product_code {base} "
                    f"ORDER BY ch.id DESC LIMIT ? OFFSET ?", args + [limit, offset])
    total = db.query_one(f"SELECT COUNT(*) AS n {base}", args)["n"]
    summary = {r["field"]: r["n"] for r in db.query(
        f"SELECT ch.field, COUNT(DISTINCT ch.product_id) AS n {base} GROUP BY ch.field", args)}
    sources = [r["source"] for r in db.query("SELECT DISTINCT source FROM product_changes ORDER BY source")]
    return {"items": [dict(r) for r in rows], "total": total, "summary": summary, "sources": sources, "labels": LABELS}


def to_csv(**filters) -> bytes:
    data = search(limit=100000, **filters)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Когда (UTC)", "Артикул", "Товар", "Что", "Было", "Стало", "Кто"])
    for r in data["items"]:
        w.writerow([r["at"], r["product_code"] or "", r["product_name"] or r["new"], LABELS.get(r["field"], r["field"]),
                    r["old"], r["new"], r["source"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу
