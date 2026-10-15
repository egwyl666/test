import asyncio

import httpx

from promloader import db, products, promcatalog
from promloader.prom_api import PromClient


def prom_product(i, **extra):
    return {
        "id": 1000 + i, "external_id": f"EXT-{i}", "name": f"Товар {i}", "sku": f"SKU-{i}", "price": 100 + i,
        "currency": "UAH", "presence": "available", "keywords": "кружка", "description": f"<p>Описание {i}</p>",
        "group": {"id": 5, "name": "Кружки"}, "images": [{"id": 1, "url": f"https://images.prom.ua/{i}.jpg"}],
        "status": "on_display", **extra,
    }


def serve(pages):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        last_id = request.url.params.get("last_id")
        rest = [x for page in pages for x in page if not last_id or x["id"] > int(last_id)]
        size = int(request.url.params.get("limit", 100))
        return httpx.Response(200, json={"products": rest[:size]})

    factory = lambda: PromClient("tkn", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(handler))  # noqa: E731
    return factory, seen


def by_ext():
    return {p["external_id"]: p for p in products.list_products(limit=1000)["items"]}


def test_convert_multilang_and_presence():
    data, images = promcatalog.convert({
        "id": 1, "name": "", "name_multilang": {"ru": "Кружка", "uk": "Кухоль"},
        "description_multilang": {"uk": "<p>Опис</p>"}, "price": "1 299,50", "presence": "order",
        "quantity_in_stock": 3, "main_image": "https://images.prom.ua/main.jpg",
    })
    assert data["name"] == "Кружка" and data["name_ua"] == "Кухоль" and data["description_ua"] == "<p>Опис</p>"
    assert data["price"] == 1299.5 and data["presence"] == "order" and data["quantity"] == 3
    assert images == ["https://images.prom.ua/main.jpg"]


def test_paginated_load(monkeypatch):
    monkeypatch.setattr(promcatalog, "PAGE", 2)
    pages = [
        [prom_product(1), prom_product(2)],
        [prom_product(3, external_id=None, sku=""), prom_product(4, status="deleted")],
        [],
    ]
    factory, seen = serve(pages)
    counts = asyncio.run(promcatalog.run(factory))
    assert counts == {"seen": 4, "created": 3, "updated": 0, "skipped": 1, "no_external_id": 1, "kept_local": 0,
                      "missing_on_prom": 0}
    assert [s.get("last_id") for s in seen] == [None, "1002", "1004"]

    items = by_ext()
    assert set(items) == {"EXT-1", "EXT-2", "PROM-1003"}
    p1 = products.get(items["EXT-1"]["id"])
    assert p1["status"] == "synced" and p1["prom_id"] == 1001 and p1["group_name"] == "Кружки"
    assert [i["src"] for i in p1["images"]] == ["https://images.prom.ua/1.jpg"]
    state = promcatalog.state()
    assert state["running"] is False and state["created"] == 3


def test_updates_existing_and_keeps_own_photos(monkeypatch):
    from .conftest import make_image

    pid = products.create({"name": "Старое", "price": 1, "external_id": "EXT-1"})
    products.add_image_file(pid, make_image())
    factory, _ = serve([[prom_product(1)]])
    counts = asyncio.run(promcatalog.run(factory))
    assert counts["updated"] == 1 and counts["created"] == 0
    p = products.get(pid)
    assert p["name"] == "Товар 1" and p["price"] == 101
    assert len(p["images"]) == 1 and not p["images"][0]["external"]  # свои фото не заменены


def test_api(client, monkeypatch):
    db.set_setting("prom_token", "")  # проверяем поведение без токена
    assert client.post("/api/prom/catalog").status_code == 400  # нет токена
    db.set_setting("prom_token", "tkn")
    factory, _ = serve([[prom_product(1)]])
    from promloader import sync
    monkeypatch.setattr(sync, "make_client", factory)
    client.post("/api/prom/catalog")
    import time
    for _ in range(50):
        if not client.get("/api/prom/catalog").json().get("running"):
            break
        time.sleep(0.05)
    assert client.get("/api/prom/catalog").json()["created"] == 1


def test_unsent_local_edits_are_kept():
    pid = products.create({"name": "Моё название", "price": 1, "external_id": "EXT-1"})
    products.set_status([pid], "ready")
    counts = asyncio.run(promcatalog.run(serve([[prom_product(1)]])[0]))
    assert counts["kept_local"] == 1 and counts["updated"] == 0
    p = products.get(pid)
    assert p["name"] == "Моё название" and p["status"] == "ready" and p["prom_id"] == 1001


def test_synced_after_load_has_no_pending():
    counts = asyncio.run(promcatalog.run(serve([[prom_product(1)]])[0]))
    p = products.get(by_ext()["EXT-1"]["id"])
    assert counts["created"] == 1 and p["pending_fields"] == "[]"


def test_short_page_in_the_middle_is_not_the_end(monkeypatch):
    """Живой Prom: страница с удалёнными товарами приходит неполной (99 из 100), а дальше товары ещё есть."""
    monkeypatch.setattr(promcatalog, "PAGE", 3)
    calls = []

    def handler(request):
        last_id = request.url.params.get("last_id")
        calls.append(last_id)
        pages = {None: [prom_product(1), prom_product(2)],          # 2 из 3 — удалённый пропущен
                 "1002": [prom_product(3), prom_product(4), prom_product(5)],
                 "1005": []}
        return httpx.Response(200, json={"products": pages[last_id]})

    factory = lambda: PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(handler))  # noqa: E731
    counts = asyncio.run(promcatalog.run(factory))
    assert counts["seen"] == 5 and calls == [None, "1002", "1005"]


# ---------- «Ідентифікатор_товару» для товаров без внешнего ID ----------

def load(pages):
    factory, _ = serve(pages)
    asyncio.run(promcatalog.load(factory()))


def test_same_code_without_external_id_does_not_merge_two_products():
    """Два товара Prom без внешнего ID с одинаковым кодом: второй затирал первый — в программе оставался один."""
    load([[prom_product(1, external_id="", sku="A-1"), prom_product(2, external_id="", sku="A-1"),
           prom_product(3, external_id="", sku="")]])
    items = by_ext()
    assert set(items) == {"A-1", "PROM-1002", "PROM-1003"}
    assert items["A-1"]["name"] == "Товар 1" and items["PROM-1002"]["name"] == "Товар 2"
    load([[prom_product(1, external_id="", sku="A-1"), prom_product(2, external_id="", sku="A-1"),
           prom_product(3, external_id="", sku="")]])
    assert set(by_ext()) == {"A-1", "PROM-1002", "PROM-1003"}          # повторная загрузка — те же ID


def test_external_id_info(client):
    load([[prom_product(1), prom_product(2, external_id="", sku="B-2"), prom_product(3, external_id="", sku="")]])
    info = client.get("/api/prom/external-ids").json()
    assert info["count"] == 2 and info["flagged"]
    assert [s["external_id"] for s in info["sample"]] == ["B-2", "PROM-1003"]
    assert info["sample"][0]["prom_url"].endswith("/1002")
    assert promcatalog.state()["no_external_id"] == 2
    assert client.get("/api/prom/external-ids.xlsx").status_code in (404, 405)  # файла для кабинета больше нет


# ---------- запись ID на Prom через API ----------

class FakeProm:
    """Как живой Prom (2026-10-10): импорт Excel находит товар по «Унікальний_ідентифікатор» и пишет ID."""

    def __init__(self, live, change_price=False, row_errors=False, busy=False):
        self.live = {p["id"]: dict(p) for p in live}
        self.imports, self.change_price, self.row_errors, self.busy = [], change_price, row_errors, busy

    def handler(self, request):
        import io
        import json

        import openpyxl
        path = request.url.path
        if path.endswith("/products/list"):
            last = request.url.params.get("last_id")
            items = sorted(self.live.values(), key=lambda p: p["id"])
            rest = [p for p in items if not last or p["id"] > int(last)]
            return httpx.Response(200, json={"products": rest[:100]})
        if path.endswith("/products/import_file"):
            if self.busy:
                return httpx.Response(400, json={"error": {"message": "действует ограничение на запуск одновременных импортов"}})
            boundary = request.headers["content-type"].split("boundary=")[1].encode()
            parts = {}
            for part in request.content.split(b"--" + boundary)[1:-1]:
                head, _, data = part.strip(b"\r\n").partition(b"\r\n\r\n")
                name = head.split(b'name="')[1].split(b'"')[0].decode()
                parts[name] = (head, data)
            ws = openpyxl.load_workbook(io.BytesIO(parts["file"][1])).active
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            settings = json.loads(parts["data"][1])
            self.imports.append({"rows": rows, "settings": settings, "type": parts["file"][0].decode()})
            for prom_id, _name, _sku, ext in rows[1:]:
                p = self.live[int(prom_id)]
                p["external_id"] = ext
                if self.change_price:
                    p["price"] = None
            return httpx.Response(200, json={"id": f"imp{len(self.imports)}"})
        if "/products/import/status/" in path:
            n = len(self.imports[-1]["rows"]) - 1
            if self.row_errors:
                return httpx.Response(200, json={"status": "PARTIAL", "total": n, "with_errors_count": 0, "errors": [
                    {"category": "validation", "errors": [{"code": 1001, "field_code": "name",
                                                           "positions": [r[0] for r in self.imports[-1]["rows"][1:]]}]}]})
            return httpx.Response(200, json={"status": "PARTIAL", "total": n, "imported": n, "updated": n})
        if "/products/by_external_id/" in path:
            ext = path.rsplit("/", 1)[-1]
            found = next((p for p in self.live.values() if p["external_id"] == ext), None)
            return httpx.Response(200, json={"product": found}) if found else httpx.Response(404, json={"error": "not found"})
        return httpx.Response(404, json={})

    def client(self):
        return PromClient("tkn", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


async def _no_sleep(s):
    return None


def write(fake):
    async def go():
        async with fake.client() as c:
            return await promcatalog.write_ext_ids(c)
    return asyncio.run(go())


def setup_ext(n=5):
    """n товаров Prom без внешнего ID, загруженных в программу (помечены prom_no_ext)."""
    live = [prom_product(i, external_id="", sku=f"S-{i}", name_multilang={"ru": f"Товар {i}", "uk": f"Товар {i} укр"})
            for i in range(1, n + 1)]
    load([live])
    return live


def test_write_ext_ids_trial_then_rest(monkeypatch):
    monkeypatch.setattr(promcatalog, "_sleep", _no_sleep)
    live = setup_ext(5)
    live[3]["external_id"] = "S-4"          # на Prom уже тот же ID — запись не нужна
    live[4]["external_id"] = "CHUZHOY"      # на Prom уже другой ID — не трогаем
    fake = FakeProm(live)
    counts = write(fake)
    assert counts == {"done": 4, "skipped": 1}
    assert [len(i["rows"]) - 1 for i in fake.imports] == [2, 1]  # проба на 2, потом остальные
    first = fake.imports[0]
    assert first["rows"][0] == promcatalog.EXT_HEADERS
    assert first["rows"][1] == ["1001", "Товар 1", "S-1", "S-1"]  # название и код — как на Prom, затем ID
    assert first["settings"]["updated_fields"] == ["sku"] and first["settings"]["mark_missing_product_as"] == "none"
    assert "spreadsheetml" in first["type"] and "prom-id.xlsx" in first["type"]
    assert fake.live[1005]["external_id"] == "CHUZHOY"
    flagged = {r["prom_id"] for r in db.query("SELECT prom_id FROM products WHERE prom_no_ext = 1")}
    assert flagged == {1005}
    st = promcatalog.state()
    assert st["no_external_id"] == 1 and st["ext_done"] == 4 and st["ext_skipped"] == 1


def test_write_ext_ids_stops_if_prom_changes_price(monkeypatch):
    monkeypatch.setattr(promcatalog, "_sleep", _no_sleep)
    live = setup_ext(4)
    fake = FakeProm(live, change_price=True)
    try:
        write(fake)
        raise AssertionError("должно остановиться")
    except promcatalog.PromError as exc:
        assert "изменил «price»" in str(exc) and "остановлена" in str(exc)
    assert len(fake.imports) == 1  # после пробы дальше не пошло
    assert db.query_one("SELECT COUNT(*) AS n FROM products WHERE prom_no_ext = 1")["n"] == 4


def test_write_ext_ids_row_errors_and_busy_point_to_cabinet(monkeypatch):
    monkeypatch.setattr(promcatalog, "_sleep", _no_sleep)
    live = setup_ext(3)
    for fake, text in ((FakeProm(live, row_errors=True), "ошибки в 2 строках"), (FakeProm(live, busy=True), "занят")):
        try:
            write(fake)
            raise AssertionError("должна быть ошибка")
        except promcatalog.PromError as exc:
            assert text in str(exc) and "«Товари» → «Імпорт»" in str(exc)
    assert db.query_one("SELECT COUNT(*) AS n FROM products WHERE prom_no_ext = 1")["n"] == 3


def test_run_ext_ids_state_and_lock(monkeypatch):
    monkeypatch.setattr(promcatalog, "_sleep", _no_sleep)
    live = setup_ext(2)
    fake = FakeProm(live)
    asyncio.run(promcatalog.run_ext_ids(fake.client))
    st = promcatalog.state()
    assert not st["ext_running"] and st["ext_finished_at"] and st["ext_error"] == "" and st["no_external_id"] == 0
    promcatalog._set_state(ext_running=True)
    promcatalog.recover()  # программу выключили посреди записи — при запуске это не «идёт» вечно
    st = promcatalog.state()
    assert st["ext_running"] is False and "прервана" in st["ext_error"]


def test_write_endpoint_needs_token(client):
    db.set_setting("prom_token", "")
    r = client.post("/api/prom/external-ids/write")
    assert r.status_code == 400 and "токен" in r.json()["detail"]
