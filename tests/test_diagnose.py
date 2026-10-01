import asyncio
import json

import httpx
import pytest

from promloader import diagnose, phototunnel, products
from promloader.prom_api import PromClient


class Prom:
    """Prom, который через import_file «не видит» товаров, а по ссылке — создаёт товар."""

    def __init__(self, busy=False, file_works=False):
        self.busy, self.file_works = busy, file_works
        self.created = False
        self.calls = []

    def handler(self, request):
        path = request.url.path
        self.calls.append(path.rsplit("/", 1)[-1] if "/status/" not in path else "status")
        if path.endswith("/products/list"):
            return httpx.Response(200, json={"products": []})
        if "/by_external_id/" in path:
            if self.created:
                return httpx.Response(200, json={"product": {"id": 77, "name": "Кружка", "price": 100}})
            return httpx.Response(404, json={"error": "not found"})
        if path.endswith("/import_file") or path.endswith("/import_url"):
            if self.busy:
                return httpx.Response(400, json={"status": 400, "message": "В данный момент действует ограничение на "
                                                 "запуск одновременных импортов."})
            kind = "file" if path.endswith("/import_file") else "url"
            return httpx.Response(200, json={"id": kind})
        if "/import/status/" in path:
            kind = path.rsplit("/", 1)[-1]
            if kind == "url" or self.file_works:
                self.created = True
                return httpx.Response(200, json={"status": "SUCCESS", "total": 1, "created": 1})
            return httpx.Response(200, json={"status": "SUCCESS", "total": 0, "created": 0})
        return httpx.Response(404, json={})

    def factory(self):
        return lambda: PromClient("t", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(diagnose, "POLL_SECONDS", 0)
    monkeypatch.setattr(phototunnel.tunnel, "ensure_url", lambda transport=None: "https://t.trycloudflare.com")


def run_check(prom):
    pid = products.create({"name": "Кружка", "price": 100})
    run = diagnose.Run(products.get(pid))
    asyncio.run(diagnose.execute(run, pid, prom.factory()))
    return run.state


def test_file_finds_nothing_but_url_works():
    st = run_check(Prom())
    titles = [(s["title"], s["status"]) for s in st["steps"]]
    assert titles[0] == ("Связь с Prom и токен", "ok")
    assert ("Отправка на Prom по ссылке (import_url)", "ok") in titles
    assert any(s["status"] == "warn" and "total: 0" in s["detail"] for s in st["steps"])
    assert st["done"] and "Всё работает" in st["verdict"] and "ссылка" in st["verdict"]
    from promloader import db
    assert db.get_setting("import_method") == "url"


def test_file_works():
    st = run_check(Prom(file_works=True))
    assert "Всё работает" in st["verdict"] and "файл" in st["verdict"]
    assert not any("по ссылке" in s["title"] for s in st["steps"])


def test_busy_prom_is_explained():
    st = run_check(Prom(busy=True))
    assert st["steps"][-1]["status"] == "fail" and "отмените" in st["verdict"]


def test_api(client, monkeypatch):
    pid = products.create({"name": "Кружка", "price": 100})

    async def fake_execute(run, product_id, client_factory=None):
        run.state["done"] = True
    monkeypatch.setattr(diagnose, "execute", fake_execute)
    st = client.post("/api/diagnose", json={"product_id": pid}).json()
    assert client.get(f"/api/diagnose/{st['id']}").json()["product"]["id"] == pid
    assert client.post("/api/diagnose", json={"product_id": 999999}).status_code == 404
    assert client.get("/diagnose").status_code == 200
