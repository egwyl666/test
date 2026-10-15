"""Приложение на телефоне: манифест и service worker без входа, push-уведомления (VAPID, шифрование RFC 8291,
подписки, удаление умерших подписок, связь с событиями уведомлений)."""

import base64
import hashlib
import hmac
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from promloader import db, main, notify, phone, runtime, webpush

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Phone:
    """Браузер телефона: свои ключи подписки, умеет расшифровать пришедшее сообщение."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)

    def subscription(self, endpoint=ENDPOINT):
        return {"endpoint": endpoint, "keys": {"p256dh": b64(self.public), "auth": b64(self.auth)}}

    def decrypt(self, body: bytes) -> bytes:
        salt, rs, idlen = body[:16], body[16:20], body[20]
        as_public = body[21:21 + idlen]
        cipher = body[21 + idlen:]
        server = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
        shared = self.key.exchange(ec.ECDH(), server)

        def hkdf(salt, ikm, info, n):
            prk = hmac.new(salt, ikm, hashlib.sha256).digest()
            return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]
        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.public + as_public, 32)
        cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, cipher, None)
        assert plain.endswith(b"\x02") and int.from_bytes(rs, "big") == 4096
        return plain[:-1]


def test_encryption_is_readable_only_by_the_phone():
    ph = Phone()
    body = webpush.encrypt(b'{"title": "Prom"}', b64(ph.public), b64(ph.auth))
    assert ph.decrypt(body) == b'{"title": "Prom"}'
    with pytest.raises(Exception):
        Phone().decrypt(body)  # чужой телефон не прочитает


def test_vapid_signature_verifies_with_public_key():
    header = webpush.vapid_header(ENDPOINT)
    token = header.split("t=")[1].split(",")[0]
    key = header.split("k=")[1]
    head, claims, sig = token.split(".")
    assert json.loads(unb64(claims))["aud"] == "https://fcm.googleapis.com"
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(key))
    raw = unb64(sig)
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert key == webpush.public_key()  # ключ один и тот же, пока его не удалили


def test_events_reach_subscribed_phones_and_dead_ones_are_removed(monkeypatch):
    ph, other = Phone(), Phone()
    webpush.subscribe(ph.subscription(), ["order"])
    webpush.subscribe(other.subscription("https://web.push.apple.com/gone"), ["order", "chat"])
    got = []

    def handler(request: httpx.Request):
        if "gone" in str(request.url):
            return httpx.Response(410)
        assert request.headers["Content-Encoding"] == "aes128gcm" and request.headers["Authorization"].startswith("vapid t=")
        got.append(json.loads(ph.decrypt(request.read())))
        return httpx.Response(201)

    transport = httpx.MockTransport(handler)
    assert webpush.send_event("chat", "<b>Новое сообщение</b>\nОлена: Є в наявності?", transport) == 0  # ph не подписан на чат
    assert webpush.send_event("order", "🛒 <b>Новый заказ №1001</b>\nІван, 460 грн", transport) == 1
    assert got == [{"title": "🛒 Новый заказ №1001", "body": "Іван, 460 грн", "url": "/orders", "tag": "order"}]
    assert webpush.count() == 1  # подписка, на которую сервис ответил 410, удалена


def test_notify_send_also_pushes(monkeypatch):
    seen = []
    monkeypatch.setattr(webpush, "send_event_background", lambda event, text: seen.append(event))
    notify.send("order", "Новый заказ", background=False)
    assert seen == ["order"]


def test_api_subscribe_and_test(client, monkeypatch):
    ph = Phone()
    r = client.get("/api/push").json()
    assert r["available"] and len(unb64(r["public_key"])) == 65
    r = client.post("/api/push/subscribe", json={"subscription": ph.subscription(), "events": ["chat", "nope"]})
    assert r.status_code == 200 and r.json()["events"] == ["chat"]
    assert client.get("/api/push", params={"endpoint": ENDPOINT}).json()["subscription"]["events"] == ["chat"]
    monkeypatch.setattr(webpush, "push", lambda sub, message, transport=None: True)
    assert client.post("/api/push/test", json={"endpoint": ENDPOINT}).json()["ok"]
    assert client.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://x"}}).status_code == 400
    client.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT})
    assert client.post("/api/push/test", json={"endpoint": ENDPOINT}).status_code == 404


def test_manifest_and_sw_open_without_login_and_on_every_page(monkeypatch, client):
    monkeypatch.setattr(runtime, "lan_bound", True)
    phone.update({"password": "секрет-1234", "lan": True})
    lan = TestClient(main.app, base_url="http://192.168.1.5:8000", client=("192.168.1.20", 5000), follow_redirects=False)
    r = lan.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    assert json.loads(r.text)["display"] == "standalone"
    sw = lan.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "push" in sw.text
    assert lan.get("/api/push").status_code == 401  # подписка — только после входа
    for route in [*main.PAGES, "/login"]:
        html = client.get(route).text
        assert 'rel="manifest" href="/manifest.webmanifest"' in html and "apple-touch-icon" in html, route
    db.set_setting("phone_lan", "0")
