"""Хранилище фото Cloudflare R2: у каждого фото с компьютера или из Excel — постоянная ссылка.

Prom скачивает фото по ссылкам. С R2 программа один раз загружает фото в ваше хранилище, и ссылка вида
https://pub-….r2.dev/media/<имя>.jpg работает всегда: компьютер может быть выключен, ссылка не меняется
при следующих выгрузках (Prom не скачивает фото заново). Бесплатно до 10 ГБ.

R2 совместим с Amazon S3: запросы подписываются AWS Signature V4 (без тяжёлой библиотеки boto3).
Имена фото уникальны и не меняются, поэтому каждое фото загружается ровно один раз (учёт — таблица r2_objects).
"""

import hashlib
import hmac
import logging
import mimetypes
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from . import config, db

log = logging.getLogger("promloader.r2")

REGION = "auto"
SERVICE = "s3"
PREFIX = "media/"
UPLOAD_THREADS = 4
SWEEP_BATCH = 300
FIELDS = ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket", "r2_public_url")
SECRET_FIELDS = ("r2_access_key_id", "r2_secret_access_key")


class R2Error(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# ---------- настройки ----------

def account_id(value: str) -> str:
    """Принимает и сам ID, и адрес вида https://<id>.r2.cloudflarestorage.com, который показывает Cloudflare."""
    value = (value or "").strip()
    m = re.search(r"([0-9a-f]{32})", value.lower())
    return m.group(1) if m else value


def public_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value and not re.match(r"^https?://", value):
        value = "https://" + value
    return value


def settings() -> dict:
    s = {key: config.get(key) for key in FIELDS}
    s["r2_account_id"] = account_id(s["r2_account_id"])
    s["r2_public_url"] = public_url(s["r2_public_url"])
    return s


def configured() -> bool:
    return all(settings().values())


def active() -> bool:
    """Фото отправляются через R2: выбрано в настройках и всё заполнено."""
    return config.get("photo_storage") == "r2" and configured()


def validate(s: dict) -> None:
    if s["r2_account_id"] and not re.fullmatch(r"[0-9a-f]{32}", s["r2_account_id"]):
        raise R2Error("Account ID — 32 символа из цифр и букв a–f. Его видно на главной странице R2 справа")
    if s["r2_bucket"] and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", s["r2_bucket"]):
        raise R2Error("Имя бакета: строчные латинские буквы, цифры и дефис, например promloader-photos")
    if s["r2_public_url"] and not re.match(r"^https?://[^\s/]+\.[^\s/]+(/\S*)?$", s["r2_public_url"]):
        raise R2Error("Публичный адрес бакета — вида https://pub-xxxx.r2.dev или ваш домен")
    if s["r2_public_url"].endswith(".r2.cloudflarestorage.com"):
        raise R2Error("Это адрес для программ (S3 API), а нужен публичный адрес: в настройках бакета → "
                      "Public access → r2.dev subdomain → Allow, затем скопируйте «Public R2.dev Bucket URL»")


# ---------- подпись AWS Signature V4 ----------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: dict, headers: dict, payload_hash: str,
         access_key: str, secret_key: str, when: datetime, region: str = REGION, service: str = SERVICE) -> dict:
    """Возвращает заголовки запроса с подписью (Authorization, x-amz-date, x-amz-content-sha256)."""
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    out = {**headers, "host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    canon = {k.lower().strip(): " ".join(str(v).split()) for k, v in out.items()}
    signed = ";".join(sorted(canon))
    canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in sorted(canon))
    canonical_query = "&".join(f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}"
                               for k, v in sorted(query.items()))
    canonical_request = "\n".join([method, quote(path, safe="/-_.~"), canonical_query, canonical_headers,
                                   signed, payload_hash])
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), day), region), service), "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out.pop("host")
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


# ---------- запросы к хранилищу ----------

_ERRORS = {
    "InvalidAccessKeyId": "R2 не узнал Access Key ID — скопируйте ключ из «Manage R2 API Tokens» заново",
    "SignatureDoesNotMatch": "R2 не принял Secret Access Key — скопируйте его заново (он показывается один раз при создании токена)",
    "NoSuchBucket": "Бакет с таким именем не найден — проверьте имя бакета",
    "AccessDenied": "У ключа нет прав на этот бакет — при создании API-токена выберите «Object Read & Write» и нужный бакет",
    "Unauthorized": "R2 не принял ключи — проверьте Access Key ID и Secret Access Key",
}


def _error(r: httpx.Response) -> R2Error:
    code = re.search(r"<Code>([^<]+)</Code>", r.text or "")
    code = code.group(1) if code else ""
    if code in _ERRORS:
        return R2Error(_ERRORS[code])
    if r.status_code in (401, 403):
        return R2Error(_ERRORS["AccessDenied"])
    retryable = r.status_code == 429 or r.status_code >= 500
    return R2Error(f"R2 ответил ошибкой {r.status_code} {code}".strip(), retryable=retryable)


def request(method: str, key: str, body: bytes = b"", headers: dict | None = None, s: dict | None = None,
            transport=None) -> httpx.Response:
    s = s or settings()
    host = f"{s['r2_account_id']}.r2.cloudflarestorage.com"
    path = f"/{s['r2_bucket']}/{key}"
    signed = sign(method, host, path, {}, headers or {}, hashlib.sha256(body).hexdigest(),
                  s["r2_access_key_id"], s["r2_secret_access_key"], datetime.now(timezone.utc))
    try:
        with httpx.Client(timeout=60, transport=transport) as client:
            r = client.request(method, f"https://{host}{quote(path, safe='/-_.~')}", content=body or None, headers=signed)
    except httpx.HTTPError as exc:
        raise R2Error(f"Нет связи с хранилищем R2: {exc}", retryable=True)
    if r.status_code >= 300 and not (method == "HEAD" and r.status_code == 404):
        raise _error(r)
    return r


def put(key: str, data: bytes, content_type: str, s: dict | None = None, transport=None) -> None:
    request("PUT", key, data, {"content-type": content_type, "cache-control": "public, max-age=31536000, immutable"},
            s, transport)


def delete(key: str, s: dict | None = None, transport=None) -> None:
    request("DELETE", key, s=s, transport=transport)


# ---------- фото ----------

def _target(s: dict) -> str:
    return f"{s['r2_account_id']}/{s['r2_bucket']}"


def base_url() -> str:
    """Адрес для фида: <public_url>/media/<файл> — та же форма ссылок, что и у самой программы."""
    return settings()["r2_public_url"]


def missing(files: list[str], s: dict | None = None) -> list[str]:
    s = s or settings()
    if not files:
        return []
    done = set()
    for i in range(0, len(files), 500):
        chunk = files[i:i + 500]
        marks = ",".join("?" * len(chunk))
        done |= {r["file"] for r in db.query(
            f"SELECT file FROM r2_objects WHERE target = ? AND file IN ({marks})", [_target(s), *chunk])}
    return [f for f in dict.fromkeys(files) if f not in done]


def upload(files: list[str], transport=None) -> int:
    """Загружает в R2 фото, которых там ещё нет. Возвращает, сколько загружено."""
    s = settings()
    todo = missing(files, s)
    if not todo:
        return 0
    uploads = db.uploads_dir()

    def one(name: str) -> str | None:
        path = uploads / name
        if not path.exists():
            return None  # фото удалили — нечего загружать
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        put(PREFIX + name, path.read_bytes(), ctype, s, transport)
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO r2_objects (target, file, uploaded_at) VALUES (?, ?, ?)",
                      (_target(s), name, db.now()))
        return name

    with ThreadPoolExecutor(UPLOAD_THREADS) as pool:
        results = list(pool.map(one, todo))  # первая ошибка прерывает: остальное загрузится при повторе
    count = sum(1 for r in results if r)
    log.info("Загружено фото в R2: %d", count)
    return count


def local_files(product_ids: list[int] | None = None) -> list[str]:
    if product_ids is None:
        return [r["file"] for r in db.query("SELECT DISTINCT file FROM images WHERE file IS NOT NULL")]
    files = []
    for i in range(0, len(product_ids), 500):
        chunk = product_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        files += [r["file"] for r in db.query(
            f"SELECT file FROM images WHERE file IS NOT NULL AND product_id IN ({marks})", chunk)]
    return files


def sweep(transport=None) -> int:
    """Фоновая загрузка: новые фото уходят в R2 заранее, чтобы отправка на Prom не ждала."""
    if not active():
        return 0
    return upload(missing(local_files())[:SWEEP_BATCH], transport)


def stats() -> dict:
    if not configured():
        return {"uploaded": 0, "waiting": 0}
    files = local_files()
    waiting = len(missing(files))
    return {"uploaded": len(set(files)) - waiting, "waiting": waiting}


def check(transport=None, public_transport=None) -> str:
    """Пробная загрузка: ключи работают, бакет есть, файл открывается по публичному адресу. Затем удаляем."""
    s = settings()
    empty = [label for key, label in zip(FIELDS, ("Account ID", "Access Key ID", "Secret Access Key",
                                                  "имя бакета", "публичный адрес")) if not s[key]]
    if empty:
        raise R2Error("Не заполнено: " + ", ".join(empty))
    validate(s)
    key = f"{PREFIX}promloader-check-{secrets.token_hex(8)}.txt"
    marker = secrets.token_hex(16).encode()
    put(key, marker, "text/plain", s, transport)
    try:
        try:
            with httpx.Client(timeout=30, follow_redirects=True, transport=public_transport) as client:
                r = client.get(f"{s['r2_public_url']}/{key}")
        except httpx.HTTPError as exc:
            raise R2Error(f"Ключи работают, но публичный адрес не открывается: {exc}")
        if r.status_code != 200 or r.content != marker:
            raise R2Error(f"Ключи работают, но по публичному адресу файл не открывается (HTTP {r.status_code}). "
                          "Включите в настройках бакета Public access → r2.dev subdomain → Allow "
                          "и вставьте адрес «Public R2.dev Bucket URL»")
    finally:
        try:
            delete(key, s, transport)
        except R2Error as exc:
            log.warning("Не удалось удалить пробный файл %s: %s", key, exc)
    return "Хранилище R2 работает: фото будут доступны Prom по постоянным ссылкам"
