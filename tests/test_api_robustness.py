"""Ни один адрес API не падает с 500 на пустой или кривой запрос (проверка при поиске ошибок, 2.19.1).

Сеть отключена: внешние сервисы (Prom, Gemini, НП, GitHub…) отвечают «нет связи» — это 4xx/502, но не 500."""

import re

import httpx
import pytest

from promloader import orders, products

SKIP = re.compile(r"restart|update/install|shutdown|migration/(apply|import)|backup.*restore|/quit")
BODIES = [{}, {"ids": "x"}, {"ids": [None]}, [], {"filter": "x"},
          {"ids": [1, 2], "status": 5, "action": [], "fields": "x", "data": "x", "events": 5}]


@pytest.fixture
def offline(monkeypatch):
    """Никакой живой сети — ни обычных, ни асинхронных запросов (клиент Prom асинхронный); без пауз перед повторами."""
    from promloader import ai, novaposhta
    real = httpx.HTTPTransport.handle_request

    def handle(self, request):
        if request.url.host == "testserver":
            return real(self, request)
        raise httpx.ConnectError("offline", request=request)

    async def handle_async(self, request):
        raise httpx.ConnectError("offline", request=request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)
    monkeypatch.setattr(novaposhta, "_sleep", lambda s: None)
    monkeypatch.setattr(ai, "_sleep", lambda s: None)


def test_no_route_crashes_on_bad_input(client, offline):
    from promloader.main import all_routes
    from tests.test_novaposhta import prom_order
    pid = products.create({"name": "Кружка", "price": 10})
    orders.store([prom_order(1001)], notify_new=False)
    values = {"product_id": pid, "order_id": 1001, "tab": "sales", "room": "555_x", "room_ident": "555_x"}
    crashes = []
    for route in all_routes():
        path = getattr(route, "path", "")
        if not path.startswith("/api") or SKIP.search(path):
            continue
        url = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: str(values.get(m.group(1), 1)), path)
        for method in getattr(route, "methods", None) or ():
            if method in ("GET", "DELETE"):
                responses = [client.request(method, url), client.request(method, url, params={"limit": "abc", "q": "%"})]
            else:
                responses = [client.request(method, url, json=b) for b in BODIES]
            crashes += [f"{method} {url} → {r.status_code}" for r in responses if r.status_code >= 500 and r.status_code != 502]
    assert not crashes, "\n".join(crashes[:20])
