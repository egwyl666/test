import pytest

from promloader import excel, suppliers

CASES = [
    ("Подрібнювач ручний (50шт) А 777-251", "А777-251", "Подрібнювач ручний"),
    ("Тонометр(50)GT-652", "GT-652", "Тонометр"),
    ("Лампочка з акумулятором (100)AR-0145", "AR-0145", "Лампочка з акумулятором"),
    ("Перкусійний масажер LM-130 з насадками та регулюванням(30)AR-00103 швидкості", "AR-00103",
     "Перкусійний масажер LM-130 з насадками та регулюванням швидкості"),
    ("Клітина для плахів ,переносна (1шт)    А777-58", "А777-58", "Клітина для плахів, переносна"),
    ("Акумуляторна газонокосарка-тример синій 2 акб AR-0082 (5)", "AR-0082-1", "Акумуляторна газонокосарка-тример синій 2 акб"),
    ("Кухонний блендер подрібнювач 2л (24) GT 613", "GT613", "Кухонний блендер подрібнювач 2л"),
    ("Уличный настенный светильник 30LED GT 355 100шт в 📦", "GT-355", "Уличный настенный светильник 30LED"),
    ("Гавайський гамак із палицею A777-57 (200×150 см)", "", "Гавайський гамак із палицею A777-57 (200×150 см)"),
    ("Набір ножів (6 шт) кухонних", "", "Набір ножів (6 шт) кухонних"),
    ("Тример для стрижки Vintage T99 ∙ (100шт) GT-220", "GT-220", "Тример для стрижки Vintage T99"),
]


@pytest.mark.parametrize("name, code, expected", CASES)
def test_clean_name(name, code, expected):
    assert excel.clean_name(name, code) == expected


def test_frequent_prefix_is_supplier_code_rare_is_model():
    items = [{"data": {"name": f"Товар {i} (10) GT-{100 + i}", "vendor_code": f"GT-{100 + i}"}} for i in range(20)]
    items.append({"data": {"name": "Повербанк Remax 80000 mAh RPP-118 (12)", "vendor_code": "RPP-118"}})
    excel.clean_names(items)
    assert items[0]["data"]["name"] == "Товар 0"
    assert items[-1]["data"]["name"] == "Повербанк Remax 80000 mAh RPP-118"  # модель осталась, упаковка ушла


def test_vendor_code_column_guessed_and_supplier_option(client):
    assert excel.guess_mapping(["@id", "name", "vendorCode"])["C"] == "vendor_code"
    sid = suppliers.create("X")
    assert client.patch(f"/api/suppliers/{sid}", json={"clean_names": True}).json()["clean_names"] is True
