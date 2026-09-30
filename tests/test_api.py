import base64
import io

import openpyxl

from .conftest import make_image


def test_pages_served(client):
    for path in ("/", "/product", "/import", "/settings", "/suppliers", "/supplier", "/pricing"):
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


def test_supplier_api_flow(client):
    from .test_suppliers import BASE, MAPPING_YML, make_yml

    s = client.post("/api/suppliers", json={"name": "Опт"}).json()
    sid = s["id"]
    up = client.post(f"/api/suppliers/{sid}/source", files={"file": ("feed.xml", make_yml(BASE))}).json()
    assert up["sheets"] == ["XML"]
    sheet = client.get(f"/api/import/{up['token']}/sheet", params={"sheet": "XML", "header_row": 1}).json()
    assert sheet["mapping"]["A"] == "external_id"

    client.put("/api/pricing", json={"rules": [{"markup_percent": 50, "rounding": "int"}]})
    mapping = {**MAPPING_YML}
    preview = client.post(f"/api/import/{up['token']}/preview", json={
        "sheet": "XML", "header_row": 1, "rows": "2-", "mapping": mapping, "supplier_id": sid}).json()["items"]
    assert preview[0]["data"]["price"] == 150 and preview[0]["data"]["cost_price"] == 100

    r = client.patch(f"/api/suppliers/{sid}", json={"mapping": mapping, "header_row": 1, "interval_hours": 12})
    assert r.status_code == 200 and r.json()["next_run_at"]
    assert client.patch(f"/api/suppliers/{sid}", json={"new_status": "bogus"}).status_code == 400

    assert client.post(f"/api/suppliers/{sid}/run", json={}).json() == {"started": True}
    import time
    for _ in range(50):
        s = client.get(f"/api/suppliers/{sid}").json()
        if not s["running"] and s["runs"] and s["runs"][0]["status"] != "running":
            break
        time.sleep(0.05)
    assert s["runs"][0]["status"] == "ok", s["runs"][0]
    assert s["items_active"] == 3

    listing = client.get("/api/products", params={"supplier": sid}).json()
    assert listing["total"] == 3 and listing["items"][0]["supplier_name"] == "Опт"
    assert client.get("/api/products", params={"supplier": "none"}).json()["total"] == 0

    pid = listing["items"][0]["id"]
    p = client.patch(f"/api/products/{pid}", json={"price": 1}).json()
    assert p["locked_fields"] == ["price"] and p["price"] == 1
    p = client.post(f"/api/products/{pid}/unlock", json={"fields": ["price"]}).json()
    assert p["locked_fields"] == [] and p["price"] == p["cost_price"] * 1.5

    test = client.post("/api/pricing/test", json={"cost": "200"}).json()
    assert test["price"] == 300
    assert client.put("/api/pricing", json={"rules": [{"rounding": "weird"}]}).status_code == 400

    assert client.delete(f"/api/suppliers/{sid}").json() == {"ok": True}
    assert client.get("/api/products").json()["total"] == 3


def test_import_accepts_xml(client):
    from .test_suppliers import BASE, make_yml

    up = client.post("/api/import/upload", files={"file": ("feed.yml", make_yml(BASE))}).json()
    assert up["sheets"] == ["XML"]
    assert client.post("/api/import/upload", files={"file": ("page.xml", b"<!DOCTYPE html><html></html>")}).status_code == 400


def test_launcher_helpers(client, monkeypatch):
    import socket

    from promloader import launcher

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        s.listen()
        assert launcher.port_free(busy) is False
    assert launcher.first_run("/nonexistent-dir")

    # «уже запущен» определяется по ответу /api/meta нашей программы
    class Resp:
        def __init__(self, code, text="", headers=None):
            self.status_code, self.text, self.headers = code, text, headers or {}

    monkeypatch.setattr(launcher.httpx, "get", lambda *a, **k: Resp(200, client.get("/api/meta").text))
    assert launcher.running_at(8000)
    monkeypatch.setattr(launcher.httpx, "get", lambda *a, **k: Resp(200, "<html>другой сайт</html>"))
    assert not launcher.running_at(8000)
    monkeypatch.setattr(launcher.httpx, "get", lambda *a, **k: Resp(401, "", {"www-authenticate": 'Basic realm="Prom Loader"'}))
    assert launcher.running_at(8000)
