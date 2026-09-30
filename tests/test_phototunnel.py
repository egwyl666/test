import asyncio
import sys
import time
from xml.etree import ElementTree as ET

import httpx
import pytest

from promloader import db, phototunnel, products, sync
from promloader.prom_api import PromClient

from .conftest import make_image

FAKE = """import sys, time
print("2026-09-30T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
print("2026-09-30T10:00:01Z INF |  https://calm-river-demo.trycloudflare.com  |", flush=True)
time.sleep(600)
"""


@pytest.fixture
def fake_cloudflared(tmp_path, monkeypatch):
    script = tmp_path / "cloudflared"
    script.write_text(f"#!{sys.executable}\n" + FAKE)
    script.chmod(0o755)
    monkeypatch.setenv("PROMLOADER_CLOUDFLARED", str(script))
    t = phototunnel.Tunnel()
    t.reach_timeout = 0
    monkeypatch.setattr(phototunnel, "tunnel", t)
    yield t
    t.close()


def test_photo_server_serves_only_photos(fake_cloudflared):
    pid = products.create({"name": "A"})
    name = products.add_image_file(pid, make_image())["src"].split("/")[-1]
    (db.data_dir() / "promloader.sqlite3").exists()
    port = fake_cloudflared._ensure_server()
    base = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{base}/media/{name}").status_code == 200
    assert httpx.head(f"{base}/media/{name}").headers["content-type"] == "image/png"
    for bad in ("/", "/media/", "/api/products", "/media/../promloader.sqlite3", f"/media/{'a' * 32}.jpg",
                "/settings", "/media/x.jpg"):
        assert httpx.get(base + bad).status_code == 404, bad


def test_tunnel_url_and_idle_close(fake_cloudflared):
    url = fake_cloudflared.ensure_url()
    assert url == "https://calm-river-demo.trycloudflare.com"
    assert fake_cloudflared.status()["active"]
    assert fake_cloudflared.ensure_url() == url  # повторно не запускается
    fake_cloudflared.maybe_close(busy=True)
    assert fake_cloudflared.status()["active"]
    fake_cloudflared.last_used = time.monotonic() - phototunnel.IDLE_MINUTES * 60 - 1
    fake_cloudflared.maybe_close(busy=False)
    assert not fake_cloudflared.status()["active"]


def test_sync_uses_tunnel_for_local_photos(fake_cloudflared):
    pid = products.create({"name": "Кружка", "price": 100})
    products.add_image_file(pid, make_image())
    products.add_image_url(pid, "https://cdn.example.com/x.jpg")
    assert sync.enqueue([pid])["accepted"] == 1
    uploaded = []

    def handler(request):
        uploaded.append(request.content)
        return httpx.Response(200, json={"id": "imp-1"})

    factory = lambda: PromClient("tkn", "https://my.prom.ua/api/v1", transport=httpx.MockTransport(handler))  # noqa: E731
    asyncio.run(sync.run_once(factory))
    body = uploaded[0]
    xml = body[body.index(b"<?xml"):body.index(b"</yml_catalog>") + len(b"</yml_catalog>")]
    pictures = [p.text for p in ET.fromstring(xml).iter("picture")]
    assert pictures[0].startswith("https://calm-river-demo.trycloudflare.com/media/")
    assert pictures[1] == "https://cdn.example.com/x.jpg"


def test_no_tunnel_when_only_external_photos(fake_cloudflared):
    pid = products.create({"name": "Кружка", "price": 100})
    products.add_image_url(pid, "https://cdn.example.com/x.jpg")
    assert asyncio.run(sync._photo_base_url([pid])) == ""
    assert not fake_cloudflared.status()["active"]


def test_public_base_url_wins(fake_cloudflared, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://shop.example.com")
    pid = products.create({"name": "Кружка", "price": 100})
    products.add_image_file(pid, make_image())
    assert not phototunnel.enabled()
    assert asyncio.run(sync._photo_base_url([pid])) == "https://shop.example.com"


def test_tunnel_failure_is_retryable(tmp_path, monkeypatch):
    script = tmp_path / "cloudflared"
    script.write_text(f"#!{sys.executable}\nimport sys; print('ERR failed to connect', flush=True); sys.exit(1)\n")
    script.chmod(0o755)
    monkeypatch.setenv("PROMLOADER_CLOUDFLARED", str(script))
    monkeypatch.setattr(phototunnel, "START_TIMEOUT", 2)
    t = phototunnel.Tunnel()
    monkeypatch.setattr(phototunnel, "tunnel", t)
    pid = products.create({"name": "Кружка", "price": 100})
    products.add_image_file(pid, make_image())
    with pytest.raises(sync.PromError) as exc:
        asyncio.run(sync._photo_base_url([pid]))
    assert exc.value.retryable and "фото" in str(exc.value)
    t.close()
