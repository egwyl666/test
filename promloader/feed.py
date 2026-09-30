"""YML-фид в формате, который принимает импорт Prom.ua.

Формат тегов собран в одном месте, чтобы его было легко сверить с документацией Prom
(https://support.prom.ua — «Импорт из YML»).
"""

import html
import re
import zlib
from datetime import datetime
from xml.etree import ElementTree as ET

from . import db, products

# Prom: available="true" — в наличии, "false" — под заказ, пусто — нет в наличии.
AVAILABLE_ATTR = {"available": "true", "order": "false", "not_available": ""}

_has_tags = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")


def description_html(text: str) -> str:
    """Обычный текст превращаем в абзацы; уже размеченный HTML оставляем как есть."""
    text = (text or "").strip()
    if not text or _has_tags.search(text):
        return text
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


def category_id(group_name: str) -> str:
    """Стабильный числовой id группы — один и тот же при каждой выгрузке."""
    return str(zlib.crc32(group_name.strip().lower().encode()) % 10_000_000 + 1)


def _sub(parent, tag, text, **attrs):
    el = ET.SubElement(parent, tag, attrs)
    el.text = text
    return el


def _fmt_price(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def build(product_ids: list[int] | None, base_url: str, shop_name: str = "") -> bytes:
    """Фид по списку товаров; None — все товары, кроме черновиков."""
    if product_ids is None:
        rows = db.query("SELECT id FROM products WHERE status != 'draft' ORDER BY id")
        product_ids = [r["id"] for r in rows]
    items = [products.get(pid) for pid in product_ids]

    root = ET.Element("yml_catalog", date=datetime.now().strftime("%Y-%m-%d %H:%M"))
    shop = ET.SubElement(root, "shop")
    if shop_name:
        _sub(shop, "name", shop_name)
    currencies = ET.SubElement(shop, "currencies")
    for cur in sorted({p["currency"] or "UAH" for p in items} or {"UAH"}):
        ET.SubElement(currencies, "currency", id=cur, rate="1")

    categories = ET.SubElement(shop, "categories")
    for group in sorted({p["group_name"] for p in items if p["group_name"]}):
        _sub(categories, "category", group, id=category_id(group))

    offers = ET.SubElement(shop, "offers")
    for p in items:
        offer = ET.SubElement(offers, "offer", id=p["external_id"], available=AVAILABLE_ATTR.get(p["presence"], "true"))
        _sub(offer, "name", p["name"])
        if p["name_ua"]:
            _sub(offer, "name_ua", p["name_ua"])
        if p["price"] is not None:
            _sub(offer, "price", _fmt_price(p["price"]))
        if p["old_price"] and p["price"] and p["old_price"] > p["price"]:
            _sub(offer, "oldprice", _fmt_price(p["old_price"]))
        _sub(offer, "currencyId", p["currency"] or "UAH")
        if p["group_name"]:
            _sub(offer, "categoryId", category_id(p["group_name"]))
        for img in products.image_rows(p["id"])[: products.MAX_IMAGES]:
            _sub(offer, "picture", products.image_src(img, base_url))
        _sub(offer, "vendorCode", p["external_id"])
        if p.get("barcode"):
            _sub(offer, "barcode", p["barcode"])
        if p["vendor"]:
            _sub(offer, "vendor", p["vendor"])
        if p["country"]:
            _sub(offer, "country_of_origin", p["country"])
        if p["quantity"] is not None:
            _sub(offer, "quantity_in_stock", str(p["quantity"]))
        if p["keywords"]:
            _sub(offer, "keywords", p["keywords"])
        if p["description"]:
            _sub(offer, "description", description_html(p["description"]))
        if p["description_ua"]:
            _sub(offer, "description_ua", description_html(p["description_ua"]))
        for param in p["params"]:
            if param["name"] and param["value"]:
                _sub(offer, "param", param["value"], name=param["name"])

    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
