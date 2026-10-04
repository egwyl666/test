import io
import os

import pytest
from PIL import Image

os.environ["PROMLOADER_WORKER"] = "0"
for name in ("PROM_API_TOKEN", "PUBLIC_BASE_URL", "PROM_API_BASE", "FEED_KEY", "APP_PASSWORD", "AI_PROVIDER",
             "GEMINI_API_KEY", "GEMINI_MODEL", "ANTHROPIC_API_KEY", "CLAUDE_MODEL"):
    os.environ.pop(name, None)

from promloader import db, rates  # noqa: E402

REAL_NBU = rates.nbu


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMLOADER_DATA", str(tmp_path))
    db.init(tmp_path)
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
