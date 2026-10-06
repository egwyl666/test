import openpyxl
import pytest
from openpyxl.drawing.image import Image as XLImage

from promloader import excel

from .conftest import make_image


def test_row_spec():
    assert excel.parse_row_spec("2-4, 7; 9-", 10) == [2, 3, 4, 7, 9, 10]
    assert excel.parse_row_spec("5-3", 10) == [3, 4, 5]
    assert excel.parse_row_spec("", 10) == []
    assert excel.parse_row_spec("8-100", 9) == [8, 9]
    with pytest.raises(excel.ImportError_):
        excel.parse_row_spec("abc", 10)


def test_guess_mapping():
    headers = ["Артикул", "Название товара", "Назва (укр)", "Цена, грн", "Старая цена", "Остаток", "Фото", "Описание", "Цвет"]
    mapping = excel.guess_mapping(headers)
    assert mapping == {
        "A": "external_id", "B": "name", "C": "name_ua", "D": "price", "E": "old_price",
        "F": "quantity", "G": "images", "H": "description",
    }


def test_build_products():
    rows = [
        ["Код", "Название", "Цена", "Кол-во", "Цвет", "Фото"],
        ["A1", "Кружка", "150,00", "3", "Белый", "https://x.com/1.jpg, https://x.com/2.jpg"],
        ["A2", "", "abc", "", "", ""],
        ["", "", "", "", "", ""],
        ["A3", "Чашка", "99", "0", "", ""],
    ]
    mapping = {"A": "external_id", "B": "name", "C": "price", "D": "quantity", "E": "param", "F": "images"}
    items = excel.build_products(rows, 1, [2, 3, 4, 5], mapping, {"group_name": "Посуда"})
    assert [i["row"] for i in items] == [2, 3, 5]  # пустая строка пропущена

    first = items[0]
    assert first["data"]["price"] == 150.0
    assert first["data"]["presence"] == "available"
    assert first["data"]["group_name"] == "Посуда"
    assert first["params"] == [{"name": "Цвет", "value": "Белый"}]
    assert first["image_urls"] == ["https://x.com/1.jpg", "https://x.com/2.jpg"]
    assert first["errors"] == []

    assert items[1]["errors"]  # нет названия и цена не число
    assert items[2]["data"]["presence"] == "not_available"


def test_xlsx_with_embedded_image(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(["Название", "Цена"])
    ws.append(["Кружка", 150])
    ws.append(["Ложка", 20.5])
    img_path = tmp_path / "pic.png"
    img_path.write_bytes(make_image())
    ws.add_image(XLImage(str(img_path)), "C2")
    path = tmp_path / "in.xlsx"
    wb.save(path)

    assert excel.sheet_names(path) == ["Товары"]
    rows, images = excel.read_sheet(path, "Товары")
    assert rows[1][:2] == ["Кружка", "150"]
    assert rows[2][1] == "20.5"
    assert list(images) == [2]
    items = excel.build_products(rows, 1, [2, 3], {"A": "name", "B": "price"}, embedded_images=images)
    assert items[0]["embedded_images"] == 1
    assert "Нет фото — такие товары почти не покупают" not in items[0]["warnings"]


def test_csv_cp1251(tmp_path):
    path = tmp_path / "in.csv"
    path.write_bytes("Название;Цена\nКружка;150\n".encode("cp1251"))
    rows, _ = excel.read_sheet(path, "CSV")
    assert rows == [["Название", "Цена"], ["Кружка", "150"]]


def test_photo_urls_with_spaces_are_not_cut():
    """Имя файла поставщика с пробелом: «LU991black_warm_white (2)500x500.jpg» — ссылка целиком, пробел → %20."""
    from promloader import excel
    cell = ("https://karaman.com.ua/image/catalog/cont168/LU991black_warm_white/LU991black_warm_white (2)500x500.jpg\n"
            "https://karaman.com.ua/image/catalog/cont168/LU991black_warm_white/LU991black_warm_white (5)500x500.jpg")
    assert excel._split_urls(cell) == [
        "https://karaman.com.ua/image/catalog/cont168/LU991black_warm_white/LU991black_warm_white%20(2)500x500.jpg",
        "https://karaman.com.ua/image/catalog/cont168/LU991black_warm_white/LU991black_warm_white%20(5)500x500.jpg",
    ]
    assert excel._split_urls("https://a.ua/1.jpg, https://a.ua/2.jpg;https://a.ua/3.jpg | https://a.ua/4%20x.jpg") == [
        "https://a.ua/1.jpg", "https://a.ua/2.jpg", "https://a.ua/3.jpg", "https://a.ua/4%20x.jpg"]
    assert excel._split_urls("https://a.ua/фото 1.jpg") == ["https://a.ua/%D1%84%D0%BE%D1%82%D0%BE%201.jpg"]
    assert excel._split_urls("нет фото") == []


def test_supplier_photo_with_cyrillic_x_and_space():
    """Реальный адрес поставщика: пробел и кириллическая «х» в «500х500» — ссылка должна совпасть с рабочей."""
    from promloader import excel
    raw = "https://karaman.com.ua/image/catalog/cont168/LU991black_warm_white/LU991black_warm_white (2)500х500.jpg"
    assert excel._split_urls(raw) == [
        "https://karaman.com.ua/image/catalog/cont168/LU991black_warm_white/LU991black_warm_white%20(2)500%D1%85500.jpg"]
