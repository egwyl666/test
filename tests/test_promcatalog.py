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
