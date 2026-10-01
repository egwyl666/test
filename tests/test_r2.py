import asyncio
import hashlib
import re
from datetime import datetime, timezone

import httpx
import pytest

from promloader import db, phototunnel, products, r2, sync

from .conftest import make_image
from .test_sync import FakeProm

ACCOUNT = "0123456789abcdef0123456789abcdef"
PUBLIC = "https://pub-1234.r2.dev"


class FakeR2:
    """Хранилище R2: проверяет подпись так же, как настоящий S3, и отдаёт файлы по публичному адресу."""

    def __init__(self, fail=None):
        self.objects = {}
        self.calls = []
        self.fail = fail

    def api(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail:
            return httpx.Response(self.fail[0], text=f"<Error><Code>{self.fail[1]}</Code></Error>")
        assert request.url.host == f"{ACCOUNT}.r2.cloudflarestorage.com"
        auth = request.headers["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/") and "/auto/s3/aws4_request" in auth
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(request.content).hexdigest()
        # пересчитываем подпись по тем же правилам, что и сервер
        when = datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        signed = re.search(r"SignedHeaders=([^,]+)", auth).group(1).split(";")
        hdrs = {h: request.headers[h] for h in signed if h not in ("host", "x-amz-date", "x-amz-content-sha256")}
        expect = r2.sign(request.method, request.url.host, request.url.path, {}, hdrs,
                         request.headers["x-amz-content-sha256"], "AKID", "SECRET", when)
        if expect["Authorization"] != auth:
            return httpx.Response(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")
        bucket, key = request.url.path.lstrip("/").split("/", 1)
        assert bucket == "photos"
        if request.method == "PUT":
            self.objects[key] = (request.content, request.headers["content-type"])
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)

    def public(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if key in self.objects:
            return httpx.Response(200, content=self.objects[key][0])
        return httpx.Response(404)


@pytest.fixture
def storage(monkeypatch):
    fake = FakeR2()
    for key, value in {"r2_account_id": f"https://{ACCOUNT}.r2.cloudflarestorage.com", "r2_access_key_id": "AKID",
                       "r2_secret_access_key": "SECRET", "r2_bucket": "photos", "r2_public_url": "pub-1234.r2.dev",
                       "photo_storage": "r2"}.items():
        db.set_setting(key, value)
    real = r2.request
    monkeypatch.setattr(r2, "request", lambda method, key, body=b"", headers=None, s=None, transport=None:
                        real(method, key, body, headers, s, httpx.MockTransport(fake.api)))
    return fake


def product_with_photo(name="Кружка"):
    pid = products.create({"name": name, "price": 100})
    products.add_image_file(pid, make_image())
    return pid


def test_signature_matches_aws_example():
    h = r2.sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"},
                hashlib.sha256(b"").hexdigest(), "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                datetime(2013, 5, 24, tzinfo=timezone.utc), region="us-east-1")
    assert h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_settings_normalized(storage):
    s = r2.settings()
    assert s["r2_account_id"] == ACCOUNT and s["r2_public_url"] == PUBLIC
    assert r2.active() and not phototunnel.enabled()


def test_upload_once(storage):
    pid = product_with_photo()
    files = r2.local_files([pid])
    assert r2.upload(files) == 1
    data, ctype = storage.objects[f"media/{files[0]}"]
    assert ctype == "image/png" and data == (db.uploads_dir() / files[0]).read_bytes()
    assert r2.upload(files) == 0 and len(storage.calls) == 1  # повторно не загружаем
    assert r2.stats() == {"uploaded": 1, "waiting": 0}


def test_sync_sends_permanent_links(storage, monkeypatch):
    monkeypatch.setattr(phototunnel.tunnel, "ensure_url", lambda: pytest.fail("туннель не нужен"))
    pid = product_with_photo()
    sent = {}
    fake = FakeProm()
    real = fake.handler

    def handler(request):
        if request.url.path.endswith("/products/import_file"):
            sent["yml"] = request.content.decode("utf-8", "replace")
        return real(request)
    fake.handler = handler
    assert sync.enqueue([pid])["accepted"] == 1
    asyncio.run(sync.run_once(fake.factory()))
    file = r2.local_files([pid])[0]
    assert f"<picture>{PUBLIC}/media/{file}</picture>" in sent["yml"]
    assert f"media/{file}" in storage.objects


def test_bad_keys_stop_sending_with_clear_message(storage):
    storage.fail = (403, "SignatureDoesNotMatch")
    pid = product_with_photo()
    sync.enqueue([pid])
    asyncio.run(sync.run_once(FakeProm().factory()))
    p = products.get(pid)
    assert p["status"] == "error" and "Secret Access Key" in p["last_error"]


def test_network_error_is_retried(storage):
    storage.fail = (503, "ServiceUnavailable")
    pid = product_with_photo()
    sync.enqueue([pid])
    asyncio.run(sync.run_once(FakeProm().factory()))
    assert products.get(pid)["status"] == "sending" and sync.list_jobs()[0]["status"] == "pending"


def test_sweep_uploads_in_background(storage):
    product_with_photo("A"), product_with_photo("B")
    assert r2.sweep() == 2 and r2.sweep() == 0
    db.set_setting("photo_storage", "")
    product_with_photo("C")
    assert r2.sweep() == 0  # R2 не выбран — ничего не делаем


def test_check(storage):
    msg = r2.check(public_transport=httpx.MockTransport(storage.public))
    assert "работает" in msg and storage.objects == {}  # пробный файл удалён
    with pytest.raises(r2.R2Error, match="r2.dev subdomain"):
        r2.check(public_transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert storage.objects == {}


def test_settings_api(client, storage):
    s = client.get("/api/settings").json()
    assert s["photo_storage"] == "r2" and s["r2_active"] and s["r2_account_id"] == ACCOUNT
    assert "SECRET" not in s["r2_secret_access_key"] and s["r2_bucket"] == "photos"
    # пустой секрет при сохранении = «не менять»
    client.post("/api/settings", json={"r2_bucket": "photos", "r2_secret_access_key": ""})
    assert r2.settings()["r2_secret_access_key"] == "SECRET"
    assert client.post("/api/settings", json={"r2_bucket": "Мои фото"}).status_code == 400
    r = client.post("/api/settings", json={"r2_public_url": f"https://{ACCOUNT}.r2.cloudflarestorage.com"})
    assert r.status_code == 400 and "публичный адрес" in r.json()["detail"]
    assert client.post("/api/settings", json={"photo_storage": "tunnel"}).json()["photo_storage"] == "tunnel"
    assert not r2.active() and phototunnel.enabled()
    db.set_setting("r2_bucket", "")
    assert client.post("/api/settings", json={"photo_storage": "r2"}).status_code == 400
    assert client.post("/api/settings", json={"photo_storage": "off"}).json()["photo_storage"] == "off"
    assert not phototunnel.enabled()
