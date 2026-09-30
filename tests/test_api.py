import base64
import io

import openpyxl

from .conftest import make_image


def test_pages_served(client):
    for path in ("/", "/product", "/import", "/settings"):
        r = client.get(path)
        assert r.status_code == 200 and "<html" in r.text


def test_product_lifecycle(client):
    p = client.post("/api/products", json={"name": "Кружка"}).json()
    pid = p["id"]
    p = client.patch(f"/api/products/{pid}", json={"price": "1 200,50", "presence": "Под заказ"}).json()
    assert p["price"] == 1200.5 and p["presence"] == "order"

    r = client.post(f"/api/products/{pid}/images", files=[
        ("files", ("a.png", make_image(), "image/png")),
        ("files", ("bad.txt", b"hello", "text/plain")),
    ])
    body = r.json()
    assert len(body["added"]) == 1 and len(body["errors"]) == 1
    src = body["product"]["images"][0]["src"]
    assert client.get(src).status_code == 200

    listing = client.get("/api/products").json()
    assert listing["total"] == 1 and listing["items"][0]["thumb"] == src

    copy = client.post(f"/api/products/{pid}/duplicate").json()
    assert copy["name"] == "Кружка (копия)" and len(copy["images"]) == 1

    assert client.patch(f"/api/products/{pid}", json={"price": "abc"}).status_code == 400
    assert client.get("/api/products/9999").status_code == 404

    client.post("/api/products/delete", json={"ids": [pid, copy["id"]]})
    assert client.get("/api/products").json()["total"] == 0


def test_media_path_traversal(client):
    assert client.get("/media/..%2Fpromloader.sqlite3").status_code == 404


def test_excel_import_flow(client):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Прайс магазина"])
    ws.append(["Артикул", "Название", "Цена", "Цвет"])
    ws.append(["S1", "Кружка", 150, "Белый"])
    ws.append(["S2", "Тарелка", 90, "Синий"])
    ws.append(["S3", "", 10, ""])
    buf = io.BytesIO()
    wb.save(buf)

    up = client.post("/api/import/upload", files={"file": ("price.xlsx", buf.getvalue())}).json()
    sheet = client.get(f"/api/import/{up['token']}/sheet", params={"sheet": up["sheets"][0], "header_row": 2}).json()
    assert sheet["total_rows"] == 5
    assert sheet["mapping"] == {"A": "external_id", "B": "name", "C": "price"}

    body = {
        "sheet": up["sheets"][0], "header_row": 2, "rows": "3-5",
        "mapping": {**sheet["mapping"], "D": "param"}, "defaults": {"group_name": "Посуда"},
    }
    preview = client.post(f"/api/import/{up['token']}/preview", json=body).json()["items"]
    assert [bool(i["errors"]) for i in preview] == [False, False, True]

    res = client.post(f"/api/import/{up['token']}/commit", json={**body, "status": "ready"}).json()
    assert res == {"created": 2, "updated": 0, "skipped": 1, "failed": []}
    items = {p["external_id"]: p for p in client.get("/api/products").json()["items"]}
    assert items["S1"]["params"] == [{"name": "Цвет", "value": "Белый"}]
    assert items["S1"]["status"] == "ready" and items["S1"]["group_name"] == "Посуда"

    # повторный импорт обновляет по артикулу, а не создаёт дубли
    res = client.post(f"/api/import/{up['token']}/commit", json={**body, "rows": "3"}).json()
    assert res["updated"] == 1 and res["created"] == 0


def test_import_rejects_xls(client):
    r = client.post("/api/import/upload", files={"file": ("old.xls", b"junk")})
    assert r.status_code == 400 and ".xlsx" in r.json()["detail"]


def test_settings_and_feed(client):
    s = client.post("/api/settings", json={"prom_token": "abcdef1234567890", "public_base_url": "https://s.example.com/"}).json()
    assert s["prom_token"] == "abcd…7890" and s["public_base_url"] == "https://s.example.com"
    key = s["feed_url"].split("key=")[1]
    assert client.get("/feed/prom.yml", params={"key": "wrong"}).status_code == 403
    r = client.get("/feed/prom.yml", params={"key": key})
    assert r.status_code == 200 and b"<yml_catalog" in r.content

    assert client.post("/api/settings", json={"import_settings": "[1]"}).status_code == 400


def test_basic_auth(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret")
    assert client.get("/").status_code == 401
    good = "Basic " + base64.b64encode(b"admin:secret").decode()
    assert client.get("/", headers={"Authorization": good}).status_code == 200
    # фото и фид должны оставаться доступными для Prom
    assert client.get("/media/none.jpg").status_code == 404


def test_import_embedded_image_preview(client, tmp_path):
    from openpyxl.drawing.image import Image as XLImage

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Название", "Цена"])
    ws.append(["Кружка", 150])
    pic = tmp_path / "p.png"
    pic.write_bytes(make_image())
    ws.add_image(XLImage(str(pic)), "C2")
    buf = io.BytesIO()
    wb.save(buf)
    up = client.post("/api/import/upload", files={"file": ("p.xlsx", buf.getvalue())}).json()
    sheet = up["sheets"][0]
    r = client.get(f"/api/import/{up['token']}/image", params={"sheet": sheet, "row": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get(f"/api/import/{up['token']}/image", params={"sheet": sheet, "row": 3}).status_code == 404

    body = {"sheet": sheet, "header_row": 1, "rows": "2", "mapping": {"A": "name", "B": "price"}}
    assert client.post(f"/api/import/{up['token']}/commit", json=body).json()["created"] == 1
    product = client.get("/api/products").json()["items"][0]
    assert product["image_count"] == 1
