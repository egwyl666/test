"""Загрузка своего каталога с Prom в программу (GET /products/list, постранично через last_id).

Товары сопоставляются по внешнему ID (external_id), затем по ID Prom. Загруженные товары получают статус
«На Prom». Поля, которых нет в ответе Prom, не трогаются.
"""

import asyncio
import json
import logging
import threading

from . import db, products
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
    external_id = external or _text(p.get("sku")) or f"PROM-{prom_id}"
    existing = products.find_by_external_id(external_id)
    if existing is None and prom_id is not None:
        row = db.query_one("SELECT id FROM products WHERE prom_id = ?", (prom_id,))
        existing = row["id"] if row else None
    if existing:
        status = db.query_one("SELECT status FROM products WHERE id = ?", (existing,))["status"]
        if status in ("ready", "error", "sending"):
            # в программе есть неотправленные правки — не затираем их данными с Prom, только связываем
            with db.tx() as c:
                c.execute("UPDATE products SET prom_id = ? WHERE id = ?", (prom_id, existing))
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
                  "pending_fields = '[]' WHERE id = ?", (prom_id, db.now(), pid))


def _upsert_page(page: list[dict], counts: dict) -> None:
    for p in page:
        try:
            _upsert(p, counts)
        except products.ProductError as exc:
            log.warning("Товар Prom %s пропущен: %s", p.get("id"), exc)
            counts["skipped"] += 1


async def load(client) -> dict:
    """Проходит весь каталог. client — PromClient."""
    counts = {"seen": 0, "created": 0, "updated": 0, "skipped": 0, "no_external_id": 0, "kept_local": 0}
    last_id = None
    while True:
        body = await client.list_products(limit=PAGE, last_id=last_id)
        page = body.get("products") or []
        await asyncio.to_thread(_upsert_page, page, counts)  # база — в отдельном потоке, интерфейс не подвисает
        counts["seen"] += len(page)
        _set_state(running=True, **counts)
        if len(page) < PAGE:
            break
        next_id = page[-1].get("id")
        if not next_id or next_id == last_id:
            break
        last_id = next_id
    return counts


async def run(client_factory) -> dict:
    if not _lock.acquire(blocking=False):
        raise PromError("Каталог уже загружается")
    try:
        _set_state(running=True, error="", seen=0, created=0, updated=0, skipped=0, no_external_id=0, kept_local=0,
                   finished_at=None)
        async with client_factory() as client:
            counts = await load(client)
        _set_state(running=False, finished_at=db.now(), **counts)
        return counts
    except PromError as exc:
        _set_state(running=False, error=str(exc), finished_at=db.now())
        raise
    finally:
        _lock.release()
