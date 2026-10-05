"""Журнал изменений товаров: что поменялось, было → стало, кто поменял и когда.

Запись делает products._touch (через него проходят все изменения товара) и несколько мест, где товар
создаётся, выгружается или удаляется. «Кто» берётся из контекста: по умолчанию «Вручную», поставщик,
пересчёт по курсу, ИИ и т. д. оборачивают свою работу в with changes.source("…").
"""

import contextvars
import csv
import io
import json
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


# что можно вернуть одним нажатием: обычные поля товара (не фото, не характеристики — они хранятся в журнале не целиком)
REVERTABLE = {"price", "old_price", "cost_price", "cost_currency", "rrp", "currency", "presence", "quantity", "name",
              "name_ua", "description", "description_ua", "group_name", "keywords", "vendor", "country", "barcode",
              "vendor_code", "unit"}


class RevertError(Exception):
    pass


def revertable(row) -> bool:
    """Значение «было» сохранено целиком (длинные тексты в журнале обрезаны — их вернуть нельзя)."""
    old = row["old"] or ""
    return row["field"] in REVERTABLE and len(old) < MAX_TEXT and row["product_id"] is not None


def revert(change_id: int) -> dict:
    """«↩ Вернуть»: записать в товар значение «было» из журнала (как правку руками; в журнале — «Откат»)."""
    from . import products

    row = db.query_one("SELECT ch.*, p.status FROM product_changes ch JOIN products p ON p.id = ch.product_id "
                       "WHERE ch.id = ?", (change_id,))
    if row is None:
        raise RevertError("Товара уже нет или запись не найдена")
    if not revertable(row):
        raise RevertError("Это изменение нельзя вернуть автоматически — поправьте в карточке товара")
    if row["status"] in ("sending", "deleting"):
        raise RevertError("Товар сейчас отправляется или удаляется — попробуйте чуть позже")
    value = row["old"]
    if row["field"] == "presence":
        value = {v: k for k, v in products.PRESENCE.items()}.get(value, value)
    with source(f"Откат: {LABELS.get(row['field'], row['field'])}"):
        return products.update(row["product_id"], {row["field"]: value})


class _Rollback(Exception):
    pass


PREVIEW_SAMPLES = 50


def preview(fn, *args, **kwargs) -> dict:
    """«Что изменится»: выполнить fn по-настоящему внутри одной транзакции, собрать записи журнала, которые она
    сделала, и откатить всё (товары, журнал, очередь на Prom). Вложенные db.tx() входят во внешнюю транзакцию.

    Ответ: {created, price_changed, gone, changed (товаров вообще), to_prom, fields, samples, result}.
    Запросы в интернет внутри fn недопустимы (курс НБУ — заранее, rates.prefetch()).
    """
    out: dict = {}
    with db._lock:
        start = db.query_one("SELECT COALESCE(MAX(id), 0) AS n FROM product_changes")["n"]
        jobs_start = db.query_one("SELECT COALESCE(MAX(id), 0) AS n FROM sync_jobs")["n"]
        try:
            with db.tx():
                out["result"] = fn(*args, **kwargs)
                rows = db.query("""SELECT ch.*, p.name AS product_name, p.external_id AS product_code, p.currency
                                   FROM product_changes ch LEFT JOIN products p ON p.id = ch.product_id
                                   WHERE ch.id > ? ORDER BY ch.id""", (start,))
                to_prom = set()
                for j in db.query("SELECT products FROM sync_jobs WHERE id > ?", (jobs_start,)):
                    to_prom |= set(json.loads(j["products"]))
                out["rows"] = [dict(r) for r in rows]
                out["to_prom"] = len(to_prom)
                raise _Rollback
        except _Rollback:
            pass
    rows = out.pop("rows")
    created = [r for r in rows if r["field"] == "created"]
    created_ids = {r["product_id"] for r in created}
    prices = [r for r in rows if r["field"] == "price" and r["product_id"] not in created_ids]
    gone = [r for r in rows if r["field"] == "presence" and r["new"] == "Нет в наличии"
            and r["product_id"] not in created_ids]
    fields: dict = {}
    for r in rows:
        if r["field"] != "created" and r["product_id"] not in created_ids:
            fields.setdefault(r["field"], set()).add(r["product_id"])

    def sample(r):
        return {"product_id": r["product_id"], "name": r["product_name"] or r["new"], "code": r["product_code"] or "",
                "old": r["old"], "new": r["new"], "currency": r["currency"] or "UAH"}
    return {
        "created": len(created_ids), "price_changed": len({r["product_id"] for r in prices}),
        "gone": len({r["product_id"] for r in gone}),
        "changed": len({r["product_id"] for r in rows if r["product_id"] not in created_ids}),
        "to_prom": out["to_prom"],
        "fields": {LABELS.get(k, k): len(v) for k, v in fields.items()},
        "samples": {"created": [sample(r) for r in created[:PREVIEW_SAMPLES]],
                    "prices": [sample(r) for r in prices[:PREVIEW_SAMPLES]],
                    "gone": [sample(r) for r in gone[:PREVIEW_SAMPLES]]},
        "result": out.get("result"),
    }


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
        like = f"%{q.strip().lower()}%"
        where.append("(lower_u(p.name) LIKE ? OR lower_u(p.external_id) LIKE ? OR lower_u(ch.new) LIKE ?)")
        args += [like] * 3
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
    items = [{**dict(r), "revertable": revertable(r) and r["product_name"] is not None} for r in rows]
    return {"items": items, "total": total, "summary": summary, "sources": sources, "labels": LABELS}


def to_csv(**filters) -> bytes:
    data = search(limit=100000, **filters)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Когда (UTC)", "Артикул", "Товар", "Что", "Было", "Стало", "Кто"])
    for r in data["items"]:
        w.writerow([r["at"], r["product_code"] or "", r["product_name"] or r["new"], LABELS.get(r["field"], r["field"]),
                    r["old"], r["new"], r["source"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу
