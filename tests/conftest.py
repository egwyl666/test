import io
import os

import pytest
from PIL import Image

os.environ["PROMLOADER_WORKER"] = "0"
for name in ("PROM_API_TOKEN", "PUBLIC_BASE_URL", "PROM_API_BASE", "FEED_KEY", "APP_PASSWORD", "AI_PROVIDER",
             "GEMINI_API_KEY", "GEMINI_MODEL", "ANTHROPIC_API_KEY", "CLAUDE_MODEL"):
    os.environ.pop(name, None)

os.environ["PROMLOADER_ALLOWED_HOSTS"] = "testserver"  # адрес TestClient

from promloader import db, rates  # noqa: E402

REAL_NBU = rates.nbu


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMLOADER_DATA", str(tmp_path))
    db.init(tmp_path)
    db.set_setting("prom_token", "test-token")  # программа подключена к Prom (тест «без токена» убирает его сам)
    yield tmp_path


@pytest.fixture(autouse=True)
def offline_nbu(monkeypatch):
    """Тесты не ходят в НБУ: курс 40 грн за любую валюту (тесты самого НБУ используют REAL_NBU)."""
    monkeypatch.setattr(rates, "nbu", lambda code, transport=None: {"rate": 40.0, "date": "2026-10-04", "stale": False})


@pytest.fixture
def client(data_dir):
    from fastapi.testclient import TestClient

    from promloader.main import app

    with TestClient(app) as c:
        yield c


def make_image(fmt="PNG", size=(40, 30), color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    """Тесты не ходят в настоящий интернет (Prom, Gemini, НП, GitHub…): подменённые сервисы — через MockTransport,
    адрес TestClient — testserver. Случайный живой запрос — сразу ошибка соединения, а не обращение к Prom."""
    import httpx

    real = httpx.HTTPTransport.handle_request
    real_async = httpx.AsyncHTTPTransport.handle_async_request
    local = {"testserver", "localhost", "127.0.0.1"}

    def handle(self, request):
        if request.url.host in local:
            return real(self, request)
        raise httpx.ConnectError(f"тесты без интернета: {request.url.host}", request=request)

    async def handle_async(self, request):
        if request.url.host in local:
            return await real_async(self, request)
        raise httpx.ConnectError(f"тесты без интернета: {request.url.host}", request=request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)
