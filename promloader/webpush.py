"""Push-уведомления на телефон (приложение Prom Loader на главном экране) — без Telegram.

Стандарт Web Push, всё своё, без тяжёлых библиотек:
- **VAPID** (RFC 8292): программа подписывает каждый запрос к push-сервису (Google, Apple, Mozilla) своим ключом
  ECDSA P-256 — ключ создаётся один раз и хранится в настройках (`push_vapid_private` — секрет, в облачную копию без
  пароля не попадает);
- **шифрование сообщения** (RFC 8291, aes128gcm): текст уведомления видит только телефон, push-сервис — нет.

Нужен компонент `cryptography` (ставится вместе с программой). Если его нет — push просто недоступен, остальное
работает. Подписка, у которой push-сервис ответил 404/410 (приложение удалено, разрешение снято), удаляется сама.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlparse

import httpx

from . import db

log = logging.getLogger("promloader.webpush")

SUBJECT = "mailto:promloader@localhost"  # push-сервисы требуют контакт отправителя; программа — у пользователя дома
TTL = 24 * 3600
URLS = {"order": "/orders", "chat": "/chat", "supplier_failed": "/suppliers", "sync_failed": "/",
        "schedule_missed": "/", "sync_done": "/", "phone": "/settings", "parcel_arrived": "/orders",
        "parcel_waiting": "/orders", "parcel_refused": "/orders"}
DEFAULT_EVENTS = ["order", "chat", "supplier_failed", "sync_failed", "ai_bulk"]


class PushError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_available: bool | None = None


def available() -> bool:
    """Есть ли рабочий компонент cryptography. Сломанная сборка падает не ImportError, а исключением от Rust
    (PanicException наследует BaseException) — его тоже ловим: без push программа работает дальше."""
    global _available
    if _available is None:
        try:
            from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
            _available = True
        except KeyboardInterrupt:
            raise
        except BaseException:
            log.warning("Компонент cryptography недоступен — push-уведомления выключены")
            _available = False
    return _available


# ---------- ключи VAPID ----------

def _private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = db.get_setting("push_vapid_private")
    if pem:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    db.set_setting("push_vapid_private", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    return key


def _raw_public(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key() -> str:
    """Открытый ключ для браузера (applicationServerKey)."""
    return _b64(_raw_public(_private_key()))


def vapid_header(endpoint: str, key=None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = key or _private_key()
    url = urlparse(endpoint)
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": SUBJECT}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={_b64(_raw_public(key))}"


# ---------- шифрование (RFC 8291) ----------

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None, server_key=None) -> bytes:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ua_public = _unb64(p256dh)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(server_key)
    shared = server_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(_unb64(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02 — последняя (единственная) запись
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


# ---------- подписки ----------

def subscribe(sub: dict, events: list[str] | None = None, device: str = "") -> dict:
    try:
        endpoint = str(sub["endpoint"])
        p256dh, auth = str(sub["keys"]["p256dh"]), str(sub["keys"]["auth"])
    except (KeyError, TypeError):
        raise PushError("Браузер прислал неполную подписку на уведомления")
    if urlparse(endpoint).scheme != "https":
        raise PushError("Адрес подписки должен быть https")
    from . import notify
    events = [e for e in (events if events is not None else DEFAULT_EVENTS) if e in notify.EVENTS]
    with db.tx() as c:
        c.execute("""INSERT INTO push_subscriptions (endpoint, p256dh, auth, events, device, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,
                       events = excluded.events, device = excluded.device, last_error = ''""",
                  (endpoint, p256dh, auth, json.dumps(events), device[:200], db.now()))
    return get(endpoint)


def get(endpoint: str) -> dict | None:
    row = db.query_one("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    return {**dict(row), "events": json.loads(row["events"])} if row else None


def unsubscribe(endpoint: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def count() -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM push_subscriptions")["n"]


def push(sub: dict, message: dict, transport=None) -> bool:
    """Отправить одно уведомление. False — подписка больше не действует и удалена."""
    body = encrypt(json.dumps(message, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    headers = {"TTL": str(TTL), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "Urgency": "high", "Authorization": vapid_header(sub["endpoint"])}
    try:
        with httpx.Client(timeout=20, transport=transport) as client:
            r = client.post(sub["endpoint"], content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise PushError(f"Нет связи с сервисом уведомлений: {exc}")
    if r.status_code in (404, 410):
        unsubscribe(sub["endpoint"])
        return False
    if r.status_code >= 300:
        raise PushError(f"Сервис уведомлений ответил {r.status_code}: {r.text[:200]}")
    return True


def _plain(text: str) -> str:
    """Текст Telegram (HTML) → обычный текст уведомления."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def send_event(event: str, text: str, transport=None) -> int:
    """Всем подписанным на событие. Не бросает исключений: уведомление — не повод ломать опрос заказов."""
    if not available():
        return 0
    sent = 0
    lines = _plain(text).split("\n", 1)
    message = {"title": lines[0][:120] or "Prom Loader", "body": (lines[1] if len(lines) > 1 else "")[:400],
               "url": URLS.get(event, "/"), "tag": event}
    for row in db.query("SELECT * FROM push_subscriptions"):
        if event not in json.loads(row["events"]):
            continue
        try:
            sent += 1 if push(dict(row), message, transport) else 0
        except Exception as exc:  # одна сломанная подписка не мешает остальным
            log.warning("Push-уведомление не отправлено: %s", exc)
            with db.tx() as c:
                c.execute("UPDATE push_subscriptions SET last_error = ? WHERE id = ?", (str(exc)[:300], row["id"]))
    return sent


def send_event_background(event: str, text: str) -> None:
    if db.query_one("SELECT 1 AS x FROM push_subscriptions LIMIT 1"):
        threading.Thread(target=send_event, args=(event, text), daemon=True).start()
