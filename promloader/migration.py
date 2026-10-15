"""Переезд на другой компьютер или сервер: всё в одном файле, ничего не вводить заново.

Пакет переезда — обычная резервная копия (база, прайсы поставщиков, по желанию фото), но:
- ключи и токены (`SECRETS`: Prom, ИИ, Telegram, R2, ключ фида, пароль входа с телефона…) из базы стёрты и лежат
  рядом зашифрованными паролем копии (`secrets.enc`): файл можно спокойно хранить в облаке или передать;
- `migration_manifest.json` — что внутри (товары, поставщики, заказы, чаты, фото) — показываем перед восстановлением.

Шифрование — только стандартная библиотека Python (ставить ничего не нужно, и на Windows, и в Docker):
- ключ — PBKDF2-HMAC-SHA256 из пароля, соль 16 байт, ITERATIONS проходов;
- шифр — HMAC-SHA256 в режиме счётчика (поток ключа = HMAC(ключ, nonce ‖ номер блока));
- целостность — HMAC-SHA256 от всего зашифрованного (encrypt-then-MAC): неверный пароль или изменённый файл
  обнаруживаются до расшифровки, а не дают «мусор».

«Код подключения к облаку» — те же данные R2 (и папка копий), зашифрованные паролем: на новом компьютере вставить код
и пароль — программа сама подключится к R2 и покажет облачные копии.
"""

import base64
import hashlib
import hmac
import json
import logging
import platform
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import backup, cloudbackup, db, r2, updater

log = logging.getLogger("promloader.migration")

FORMAT = "promloader-secrets-1"
ITERATIONS = 300_000
MIN_PASSWORD = 8
SECRETS = cloudbackup.SECRET_SETTINGS  # что стирается из облачной копии — то и переносится зашифрованным
CODE_PREFIX = "PLK1-"
INCOMING = "migration-incoming.zip"  # выбранная копия ждёт пароля и подтверждения


class MigrationError(Exception):
    pass


# ---------- шифрование ----------

def _keys(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=64)
    return key[:32], key[32:]


def _stream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    block = 0
    while len(out) < size:
        out += hmac.new(key, nonce + block.to_bytes(8, "big"), hashlib.sha256).digest()
        block += 1
    return bytes(out[:size])


def _check_password(password: str) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD:
        raise MigrationError(f"Пароль копии слишком короткий — нужно не меньше {MIN_PASSWORD} символов")
    return password


def encrypt(data: bytes, password: str) -> bytes:
    password = _check_password(password)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    enc_key, mac_key = _keys(password, salt, ITERATIONS)
    cipher = bytes(a ^ b for a, b in zip(data, _stream(enc_key, nonce, len(data))))
    head = f"{FORMAT}|{ITERATIONS}|".encode() + salt + nonce
    tag = hmac.new(mac_key, head + cipher, hashlib.sha256).digest()
    b64 = base64.b64encode
    return json.dumps({"format": FORMAT, "kdf": "pbkdf2-sha256", "iterations": ITERATIONS, "salt": b64(salt).decode(),
                       "nonce": b64(nonce).decode(), "ciphertext": b64(cipher).decode(), "tag": b64(tag).decode()}).encode()


def decrypt(blob: bytes, password: str) -> bytes:
    try:
        box = json.loads(blob)
        if box.get("format") != FORMAT:
            raise ValueError
        salt, nonce, cipher, tag = (base64.b64decode(box[k]) for k in ("salt", "nonce", "ciphertext", "tag"))
        iterations = int(box["iterations"])
    except (ValueError, KeyError, TypeError):
        raise MigrationError("Файл ключей повреждён или сделан не Prom Loader")
    enc_key, mac_key = _keys(str(password or ""), salt, iterations)
    head = f"{FORMAT}|{iterations}|".encode() + salt + nonce
    if not hmac.compare_digest(tag, hmac.new(mac_key, head + cipher, hashlib.sha256).digest()):
        raise MigrationError("Неверный пароль копии (или файл изменён) — данные на этом компьютере не тронуты")
    return bytes(a ^ b for a, b in zip(cipher, _stream(enc_key, nonce, len(cipher))))


# ---------- пакет ----------

def current_secrets() -> dict:
    keys = ",".join("?" * len(SECRETS))
    return {r["key"]: r["value"] for r in db.query(f"SELECT key, value FROM settings WHERE key IN ({keys})", SECRETS)
            if r["value"]}


def _counts(conn) -> dict:
    def count(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.DatabaseError:
            return 0
    return {"products": count("SELECT COUNT(*) FROM products"), "suppliers": count("SELECT COUNT(*) FROM suppliers"),
            "orders": count("SELECT COUNT(*) FROM orders"), "chats": count("SELECT COUNT(*) FROM chat_rooms"),
            "images": count("SELECT COUNT(*) FROM images WHERE file != ''")}


def seal(source: Path, target: Path, password: str | None) -> dict:
    """Копия → копия без ключей в базе (+ зашифрованные ключи, если задан пароль) и описание пакета."""
    cloudbackup.strip_secrets(source, target)
    with zipfile.ZipFile(source) as z, tempfile.TemporaryDirectory() as tmp:
        counts = {}
        if backup.DB_NAME in z.namelist():
            z.extract(backup.DB_NAME, tmp)
            conn = sqlite3.connect(Path(tmp) / backup.DB_NAME)
            try:
                counts = _counts(conn)
            finally:
                conn.close()
        photos = sum(1 for n in z.namelist() if n.startswith("uploads/") and not n.endswith("/"))
    found = current_secrets() if password else {}
    manifest = {
        "version": updater.current_version(), "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hostname": platform.node(), **counts, "photos_in_file": photos, "r2": r2.configured(),
        "secrets_encrypted": bool(password), "secrets": sorted(found),
    }
    with zipfile.ZipFile(target, "a", zipfile.ZIP_DEFLATED) as z:
        if password:
            z.writestr("secrets.enc", encrypt(json.dumps(found, ensure_ascii=False).encode("utf-8"), password))
        z.writestr("migration_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def create_bundle(password: str, with_photos: bool = True) -> dict:
    """Пакет переезда на диске (в списке копий, причина «для переезда»)."""
    _check_password(password)
    info = backup.create("migration", with_photos=with_photos)
    path = backup.path_of(info["name"])
    with tempfile.TemporaryDirectory() as tmp:
        sealed = Path(tmp) / path.name
        manifest = seal(path, sealed, password)
        shutil.move(str(sealed), path)
    return {**backup.describe(path.name), "size": path.stat().st_size, "manifest": manifest}


def upload_bundle(name: str, transport=None) -> None:
    """Пакет уже без открытых ключей — отправляем в облако как есть."""
    try:
        r2.put_file(cloudbackup.prefix() + name, backup.path_of(name), transport=transport)
    except r2.R2Error as exc:
        raise MigrationError(f"Пакет не отправлен в облако: {exc}")


# ---------- на новом компьютере ----------

def incoming_path() -> Path:
    return db.data_dir() / INCOMING


def inspect(path: Path | None = None) -> dict:
    """Что в копии: из описания пакета, а у обычной копии — посчитать по её базе."""
    path = path or incoming_path()
    try:
        backup._validate(path)
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "migration_manifest.json" in names:
                manifest = json.loads(z.read("migration_manifest.json"))
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    z.extract(backup.DB_NAME, tmp)
                    conn = sqlite3.connect(Path(tmp) / backup.DB_NAME)
                    try:
                        manifest = _counts(conn)
                    finally:
                        conn.close()
                manifest["photos_in_file"] = sum(1 for n in names if n.startswith("uploads/") and not n.endswith("/"))
            manifest["secrets_encrypted"] = "secrets.enc" in names
    except backup.BackupError as exc:
        raise MigrationError(str(exc))
    except (ValueError, zipfile.BadZipFile):
        raise MigrationError("Файл повреждён или это не копия Prom Loader")
    return manifest


def receive(source) -> dict:
    """Загруженный файл (пакет переезда или обычная копия): сохранить и показать, что внутри."""
    path = incoming_path()
    tmp = path.with_suffix(".part")
    with open(tmp, "wb") as out:
        shutil.copyfileobj(source, out, 1024 * 1024)
    tmp.replace(path)
    try:
        return inspect(path)
    except MigrationError:
        path.unlink(missing_ok=True)
        raise


def receive_cloud(name: str, transport=None) -> dict:
    if not backup.NAME_RE.match(name):
        raise MigrationError("Нет такой копии")
    path = incoming_path()
    try:
        with open(path.with_suffix(".part"), "wb") as out:
            r2.download(cloudbackup.prefix() + name, out, transport=transport)
    except r2.R2Error as exc:
        raise MigrationError(f"Не удалось скачать копию из облака: {exc}")
    path.with_suffix(".part").replace(path)
    return inspect(path)


def apply(password: str = "") -> None:
    """Проверить пароль и поставить копию на восстановление (само восстановление — при перезапуске).
    Неверный пароль — ошибка до любых изменений."""
    path = incoming_path()
    if not path.exists():
        raise MigrationError("Сначала выберите файл или копию в облаке")
    pending_secrets = db.data_dir() / backup.SECRETS_PENDING
    with zipfile.ZipFile(path) as z:
        if "secrets.enc" in z.namelist():
            if not password:
                raise MigrationError("В копии зашифрованы ключи и токены — введите пароль копии")
            values = json.loads(decrypt(z.read("secrets.enc"), password))
        else:
            values = None
    with open(path, "rb") as f:
        backup.stage_restore_stream(f)
    if values is None:
        pending_secrets.unlink(missing_ok=True)
    else:
        pending_secrets.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    path.unlink(missing_ok=True)


# ---------- код подключения к облаку ----------

R2_FIELDS = ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket", "r2_public_url")


def connection_code(password: str) -> str:
    if not r2.configured():
        raise MigrationError("Хранилище R2 не подключено — код подключения не из чего сделать")
    data = {k: db.get_setting(k) for k in R2_FIELDS}
    data["backup_cloud_prefix"] = cloudbackup.prefix().split("/")[1]
    blob = encrypt(json.dumps(data).encode(), password)
    return CODE_PREFIX + base64.urlsafe_b64encode(blob).decode().rstrip("=")


def connect(code: str, password: str) -> None:
    """Новый компьютер: код + пароль → подключиться к R2 и к папке копий старого компьютера."""
    code = "".join(str(code or "").split())
    if not code.startswith(CODE_PREFIX):
        raise MigrationError(f"Это не код подключения Prom Loader — он начинается с {CODE_PREFIX}")
    body = code[len(CODE_PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        raise MigrationError("Код подключения скопирован не полностью")
    data = json.loads(decrypt(blob, password))
    with db.tx():
        for key in (*R2_FIELDS, "backup_cloud_prefix"):
            if data.get(key):
                db.set_setting(key, data[key])
        db.set_setting("backup_cloud", "1")
