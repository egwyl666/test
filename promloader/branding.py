"""Логотип магазина: в шапке программы, на странице входа и значком вкладки браузера.

Хранится в настройках (PNG в base64, до 256 px по большей стороне), а не файлом: так он сам попадает в резервные
копии и переносится на новый компьютер вместе с остальными настройками. Принимаются только растровые картинки
(PNG, JPEG, WebP, GIF…) — их всегда пересохраняем в PNG; SVG не принимаем: в нём может быть скрипт."""

import base64
import hashlib
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from . import db

MAX_SIDE = 256
MAX_UPLOAD = 5 * 1024 * 1024


class BrandingError(ValueError):
    pass


def save_logo(content: bytes) -> str:
    """Картинка → PNG ≤ 256 px с прозрачностью. Возвращает версию (для адреса ?v=, чтобы браузер взял новый)."""
    if len(content) > MAX_UPLOAD:
        raise BrandingError("Файл логотипа больше 5 МБ — уменьшите его")
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise BrandingError("Файл не похож на картинку: подойдёт PNG, JPEG или WebP") from None
    img = ImageOps.exif_transpose(img).convert("RGBA")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    version = hashlib.sha1(buf.getvalue()).hexdigest()[:10]
    with db.tx():
        db.set_setting("shop_logo", base64.b64encode(buf.getvalue()).decode())
        db.set_setting("shop_logo_version", version)
    return version


def logo() -> bytes | None:
    data = db.get_setting("shop_logo")
    return base64.b64decode(data) if data else None


def version() -> str:
    return db.get_setting("shop_logo_version") or "" if db.get_setting("shop_logo") else ""


def delete_logo() -> None:
    with db.tx():
        db.set_setting("shop_logo", "")
        db.set_setting("shop_logo_version", "")
