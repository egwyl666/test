"""Оформление магазина: два логотипа и цвет программы.

- «Логотип» (малый, slot="logo") — в шапке программы и значком вкладки, PNG до 256 px с прозрачностью.
- «Большой логотип» (slot="big") — во весь фон страницы входа, до 1600 px: с прозрачностью — PNG, без неё — JPEG.
- Цвет программы: из малого логотипа берётся самый заметный насыщенный цвет (brand_accent); его можно выключить
  («Цвет из логотипа» в «Оформлении»), тогда — стандартный фиолетовый.

Хранится в настройках (base64), а не файлами: так всё само попадает в резервные копии и переносится на новый
компьютер. Принимаются только растровые картинки — их всегда пересохраняем; SVG не принимаем: в нём может быть
скрипт."""

import base64
import colorsys
import hashlib
import io
from collections import Counter

from PIL import Image, ImageOps, UnidentifiedImageError

from . import db

MAX_UPLOAD = 5 * 1024 * 1024
SLOTS = {"logo": ("shop_logo", 256), "big": ("shop_logo_big", 1600)}
MAX_SIDE = SLOTS["logo"][1]


class BrandingError(ValueError):
    pass


def _open(content: bytes) -> Image.Image:
    if len(content) > MAX_UPLOAD:
        raise BrandingError("Файл логотипа больше 5 МБ — уменьшите его")
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise BrandingError("Файл не похож на картинку: подойдёт PNG, JPEG или WebP") from None
    return ImageOps.exif_transpose(img).convert("RGBA")


def accent_from(img: Image.Image) -> str:
    """Самый заметный насыщенный цвет картинки (#rrggbb) или "" — логотип чёрно-белый или почти прозрачный.
    Яркость подгоняется, чтобы белый текст на кнопке этого цвета читался."""
    small = img.copy()
    small.thumbnail((64, 64))
    buckets: Counter = Counter()
    sums: dict = {}
    raw = small.tobytes()  # RGBA подряд (getdata устаревает в новых Pillow)
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i:i + 4]
        if a < 200:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.35 or not 0.2 < v < 0.98:
            continue  # серое, белое, чёрное — не «цвет бренда»
        key = int(h * 24)
        buckets[key] += 1
        sums.setdefault(key, [0, 0, 0])
        for ch, x in enumerate((r, g, b)):
            sums[key][ch] += x
    if not buckets or buckets.most_common(1)[0][1] < 8:
        return ""
    key, n = buckets.most_common(1)[0]
    r, g, b = (x / n / 255 for x in sums[key])
    h, light, s = colorsys.rgb_to_hls(r, g, b)
    light = min(max(light, 0.32), 0.45)  # не светлее 45%: белый текст на кнопке; не темнее 32%: видно, что цвет
    s = max(s, 0.55)
    r, g, b = colorsys.hls_to_rgb(h, light, s)
    return readable("#{:02x}{:02x}{:02x}".format(*(round(x * 255) for x in (r, g, b))))


MIN_CONTRAST = 5.5  # к белому: белый текст на кнопке и цветной текст на светлой подложке (бейдж «Готов») читаются


def _luminance(rgb) -> float:
    v = [x / 255 for x in rgb]
    v = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in v]
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]


def readable(color: str) -> str:
    """Тот же оттенок, но темнее, пока белый на нём не станет читаться (жёлтый, салатовый логотип)."""
    if not color or len(color) != 7:
        return color
    rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    h, light, s = colorsys.rgb_to_hls(*(x / 255 for x in rgb))
    while 1.05 / (_luminance(rgb) + 0.05) < MIN_CONTRAST and light > 0.05:
        light -= 0.01
        rgb = [round(x * 255) for x in colorsys.hls_to_rgb(h, light, s)]
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def save_logo(content: bytes, slot: str = "logo") -> str:
    """Сохранить логотип. Возвращает версию (для адреса ?v=, чтобы браузер взял новый)."""
    key, side = SLOTS[slot]
    img = _open(content)
    img.thumbnail((side, side))
    buf = io.BytesIO()
    if slot == "big" and img.getextrema()[3][0] == 255:
        img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)  # фото без прозрачности — JPEG легче
    else:
        img.save(buf, "PNG", optimize=True)
    version = hashlib.sha1(buf.getvalue()).hexdigest()[:10]
    with db.tx():
        db.set_setting(key, base64.b64encode(buf.getvalue()).decode())
        db.set_setting(f"{key}_version", version)
        if slot == "logo":
            db.set_setting("brand_accent", accent_from(img))
    return version


def logo(slot: str = "logo") -> bytes | None:
    data = db.get_setting(SLOTS[slot][0])
    return base64.b64decode(data) if data else None


def media_type(data: bytes) -> str:
    return "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"


def version(slot: str = "logo") -> str:
    key = SLOTS[slot][0]
    return (db.get_setting(f"{key}_version") or "") if db.get_setting(key) else ""


def delete_logo(slot: str = "logo") -> None:
    key = SLOTS[slot][0]
    with db.tx():
        db.set_setting(key, "")
        db.set_setting(f"{key}_version", "")
        if slot == "logo":
            db.set_setting("brand_accent", "")


def accent() -> str:
    """Цвет программы: из логотипа, если он есть и не выключен в «Оформлении»."""
    # readable() и при чтении: цвет, сохранённый версией до 2.29.3, мог быть слишком светлым
    return "" if db.get_setting("brand_accent_off") == "1" else readable(db.get_setting("brand_accent"))


def public() -> dict:
    """Для страницы входа (без входа): есть ли логотипы и цвет."""
    return {"logo": version("logo"), "big": version("big"), "accent": accent()}
