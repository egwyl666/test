"""Массовый импорт из Excel/CSV/XML: пользователь выбирает лист, строки и что лежит в каждой колонке.

Любой источник (Excel, CSV, XML/YML поставщика) сначала превращается в таблицу строк —
дальше сопоставление колонок и разбор одинаковые.
"""

import csv
import io
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import openpyxl
from openpyxl.utils import get_column_letter

from . import products

# Куда можно направить колонку. "param" — характеристика с именем из заголовка колонки.
TARGETS = {
    "": "— не импортировать —",
    "external_id": "Артикул / код",
    "name": "Название",
    "name_ua": "Название (укр.)",
    "price": "Цена (розничная)",
    "cost_price": "Цена закупки (к ней применяется наценка)",
    "rrp": "РРЦ (рекомендованная цена)",
    "old_price": "Старая цена (до скидки)",
    "currency": "Валюта",
    "quantity": "Количество",
    "presence": "Наличие",
    "group_name": "Группа / категория",
    "vendor": "Производитель",
    "country": "Страна",
    "description": "Описание",
    "description_ua": "Описание (укр.)",
    "keywords": "Ключевые слова",
    "unit": "Единица измерения",
    "images": "Фото (ссылки)",
    "param": "Характеристика",
}

# Подсказки для автоматического сопоставления по заголовкам колонок.
HINTS = [
    ("name_ua", r"назв\w*.*(укр|ua)|найменування|name_(ua|uk)"),
    ("description_ua", r"опис\w*.*(укр|ua)|^опис$|description_(ua|uk)"),
    ("old_price", r"стар\w* цен|цена до|стара ціна|old.?price"),
    ("cost_price", r"закуп|вход|опт|дроп|drop|cost|purchase|собіварт|себестоим"),
    ("rrp", r"ррц|rrp|рекоменд|роздр|розн"),
    ("external_id", r"^@id$|артикул|vendor.?code|код|sku|external|ідентиф|идентиф|^id$"),
    ("name", r"назв|наимен|товар|name"),
    ("price", r"цен|ціна|price|стоим|вартість"),
    ("currency", r"валют|currency"),
    ("quantity", r"кол-?во|колич|кільк|остат|залиш|qty|quantity|stock"),
    ("presence", r"налич|наявн|presence|available"),
    ("group_name", r"групп|груп|катег|розділ|раздел|^category$|category.?name|^group$"),
    ("vendor", r"произв|виробн|бренд|brand|^vendor$|марка"),
    ("country", r"стран|країн|country"),
    ("description", r"опис|описан|description"),
    ("keywords", r"ключ|keyword|теги|tags"),
    ("unit", r"един|одиниц|unit"),
    ("images", r"фото|изображ|зображ|картин|image|photo|picture"),
]

MAX_PREVIEW_ROWS = 2000


class ImportError_(ValueError):
    pass


# ---------- чтение файла ----------

def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M") if value.time() != datetime.min.time() else value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportError_("Не удалось определить кодировку CSV — сохраните файл в UTF-8")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


XML_ITEM_TAGS = ("offer", "item", "product", "good", "goods", "tovar", "товар")
XML_PARAM_TAGS = ("param", "attribute", "characteristic", "feature", "property")
XML_SUFFIXES = (".xml", ".yml", ".yaml_xml")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _read_xml(path: Path) -> list[list[str]]:
    """XML/YML поставщика -> таблица: строка на каждый товар, колонка на каждый тег.

    Атрибуты становятся колонками «@имя», повторяющиеся теги (picture) склеиваются через перевод строки,
    <param name="Цвет"> превращается в колонку «param:Цвет». Для YML подставляется название категории.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ImportError_(f"XML повреждён или это не XML: {exc}")

    categories = {}
    for el in root.iter():
        if _local(el.tag) == "category" and el.get("id"):
            categories[el.get("id")] = (el.text or "").strip()

    counts = Counter(_local(el.tag) for el in root.iter() if len(el))
    if not counts:
        raise ImportError_("В XML не нашлось товаров")
    known = [t for t in XML_ITEM_TAGS if counts.get(t)]
    item_tag = known[0] if known else counts.most_common(1)[0][0]

    columns: list[str] = []
    records = []
    for el in root.iter():
        if _local(el.tag) != item_tag or not len(el):
            continue
        rec: dict[str, str] = {}

        def add(key: str, value: str):
            value = (value or "").strip()
            if key not in columns:
                columns.append(key)
            rec[key] = f"{rec[key]}\n{value}" if rec.get(key) and value else (value or rec.get(key, ""))

        for key, value in el.attrib.items():
            add("@" + _local(key), value)
        for child in el:
            name = _local(child.tag)
            if name in XML_PARAM_TAGS and child.get("name"):
                add(f"param:{child.get('name').strip()}", "".join(child.itertext()))
            elif len(child):
                add(name, "\n".join(t.strip() for t in child.itertext() if t.strip()))
            else:
                add(name, child.text or "")
        category_id = rec.get("categoryId") or rec.get("category_id")
        if categories and category_id in categories:
            add("category", categories[category_id])
        records.append(rec)

    if not records:
        raise ImportError_("В XML не нашлось товаров")
    return [columns] + [[rec.get(c, "") for c in columns] for rec in records]


def detect_suffix(content: bytes, filename: str = "") -> str:
    """Определяет формат по содержимому: ссылки поставщиков часто без расширения или с неверным."""
    head = content[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"PK"):
        return ".xlsx"
    if head.startswith(b"<"):
        if b"<html" in head.lower() or b"<!doctype html" in head.lower():
            raise ImportError_("По ссылке открылась веб-страница, а не прайс. Нужна прямая ссылка на файл")
        return ".xml"
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        raise ImportError_("Файл повреждён: это не Excel")
    return ".csv"


def _load(path: Path):
    try:
        return openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise ImportError_(f"Не удалось открыть файл как Excel (.xlsx): {exc}")


def sheet_names(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        return ["CSV"]
    if path.suffix.lower() in XML_SUFFIXES:
        _read_xml(path)  # проверить, что читается
        return ["XML"]
    return _load(path).sheetnames


def read_sheet(path: Path, sheet: str) -> tuple[list[list[str]], dict[int, list[bytes]]]:
    """Все строки листа как текст + картинки, вставленные в ячейки: {номер строки: [байты]}."""
    if path.suffix.lower() == ".csv":
        return _read_csv(path), {}
    if path.suffix.lower() in XML_SUFFIXES:
        return _read_xml(path), {}
    wb = _load(path)
    if sheet not in wb.sheetnames:
        raise ImportError_(f"В файле нет листа «{sheet}»")
    ws = wb[sheet]
    rows = [[_cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
    # хвост из пустых строк Excel любит оставлять — отрезаем
    while rows and not any(rows[-1]):
        rows.pop()
    images: dict[int, list[bytes]] = {}
    for img in getattr(ws, "_images", []):
        try:
            row_number = img.anchor._from.row + 1
            images.setdefault(row_number, []).append(img._data())
        except Exception:
            continue
    return rows, images


# ---------- выбор строк ----------

def parse_row_spec(spec: str, max_row: int) -> list[int]:
    """'2-50, 55, 60-' -> номера строк (как в Excel, с единицы)."""
    spec = (spec or "").strip()
    if not spec:
        return []
    selected: set[int] = set()
    for part in re.split(r"[,;\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?|(\d+)", part)
        if not m:
            raise ImportError_(f"Не понял диапазон «{part}». Пример: 2-50, 55, 60-")
        if m.group(3):
            start = end = int(m.group(3))
        else:
            start = int(m.group(1) or 1)
            end = int(m.group(2) or max_row)
        if start > end:
            start, end = end, start
        selected.update(range(max(start, 1), min(end, max_row) + 1))
    return sorted(selected)


# ---------- сопоставление колонок ----------

def guess_mapping(headers: list[str]) -> dict[str, str]:
    mapping, used = {}, set()
    for idx, header in enumerate(headers):
        text = (header or "").strip().lower()
        if not text:
            continue
        if text.startswith("param:"):
            mapping[get_column_letter(idx + 1)] = "param"
            continue
        for target, pattern in HINTS:
            if target not in used and re.search(pattern, text):
                mapping[get_column_letter(idx + 1)] = target
                used.add(target)
                break
    return mapping


def _split_urls(text: str) -> list[str]:
    return [u for u in re.split(r"[\s,;|]+", text or "") if re.match(r"^https?://", u)]


def build_products(
    rows: list[list[str]],
    header_row: int,
    row_numbers: list[int],
    mapping: dict[str, str],
    defaults: dict | None = None,
    embedded_images: dict[int, list[bytes]] | None = None,
    pricer=None,
    supplier_id: int | None = None,
) -> list[dict]:
    """Строки -> товары. Для каждой строки: данные, фото, ошибки и предупреждения."""
    defaults = {k: v for k, v in (defaults or {}).items() if v not in (None, "")}
    headers = rows[header_row - 1] if 0 < header_row <= len(rows) else []
    columns = []
    for letter, target in mapping.items():
        if not target:
            continue
        if target not in TARGETS and not target.startswith("param:"):
            raise ImportError_(f"Неизвестное поле «{target}»")
        idx = openpyxl.utils.column_index_from_string(letter) - 1
        if target == "param":
            header = headers[idx] if idx < len(headers) else ""
            if header.lower().startswith("param:"):
                header = header[6:].strip()
            target = "param:" + (header or f"Колонка {letter}")
        columns.append((idx, target))

    result = []
    for number in row_numbers:
        if number < 1 or number > len(rows) or number == header_row:
            continue
        row = rows[number - 1]
        if not any(row):
            continue
        raw, params, urls, errors = dict(defaults), [], [], []
        for idx, target in columns:
            value = row[idx] if idx < len(row) else ""
            if target.startswith("param:"):
                if value:
                    params.append({"name": target[6:], "value": value})
            elif target == "images":
                urls += _split_urls(value)
            elif value != "":
                raw[target] = value
        if params:
            raw["params"] = params
        data = {}
        for key, value in raw.items():
            # по одному полю: кривое значение в одной колонке не выбрасывает всю строку
            try:
                data.update(products.normalize({key: value}))
            except products.ProductError as exc:
                errors.append(f"{TARGETS.get(key, key)}: {exc}")
        # наличие по количеству, если колонки «наличие» нет
        if "presence" not in raw and data.get("quantity") is not None:
            data["presence"] = "available" if data["quantity"] > 0 else "not_available"
        price_warnings = pricer.apply(data, supplier_id) if pricer else []
        files = (embedded_images or {}).get(number, [])
        check = products.validate({**data, "params": params}, image_count=len(urls) + len(files))
        result.append({
            "row": number,
            "data": data,
            "params": params,
            "image_urls": urls[: products.MAX_IMAGES],
            "embedded_images": len(files),
            "errors": errors + check["errors"],
            "warnings": price_warnings + check["warnings"],
        })
    return result


def headers_for(rows: list[list[str]], header_row: int) -> list[str]:
    width = max((len(r) for r in rows), default=0)
    headers = rows[header_row - 1] if 0 < header_row <= len(rows) else []
    return [headers[i] if i < len(headers) else "" for i in range(width)]


def column_letters(width: int) -> list[str]:
    return [get_column_letter(i + 1) for i in range(width)]
