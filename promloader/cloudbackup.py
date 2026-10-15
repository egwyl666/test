"""Резервные копии ещё и в облаке Cloudflare R2 — на случай, если пропадёт сам компьютер или диск.

Безопасность — главное:
- **в облачную копию не попадают токены, ключи и пароль** (`SECRET_SETTINGS`): из снимка базы они стираются, а
  файл базы пересобирается (VACUUM), чтобы стёртые значения не остались в свободных страницах файла;
- **имя в облаке случайное** (`backups/<32 hex>/…`, папка — настройка этого компьютера): бакет R2 для фото открыт на
  чтение по прямой ссылке, но список файлов там не виден, а такое имя не угадать;
- при восстановлении токены и ключи этого компьютера сохраняются (`backup.apply_pending`), их не нужно вводить
  заново. На новом компьютере их придётся ввести — так и задумано.
"""

import logging
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import backup, db, r2

log = logging.getLogger("promloader.cloudbackup")

SECRET_SETTINGS = ("prom_token", "feed_key", "gemini_key", "anthropic_key", "github_token", "telegram_token",
                   "support_token", "r2_access_key_id", "r2_secret_access_key", "phone_password_hash", "phone_secret",
                   "backup_password", "push_vapid_private")
CLOUD_KEEP = 10
REASONS = ("daily", "manual")  # перед обновлением и восстановлением — только на диске: они нужны «здесь и сейчас»


class CloudBackupError(Exception):
    pass


def enabled() -> bool:
    return db.get_setting("backup_cloud") == "1" and r2.configured()


def prefix() -> str:
    value = db.get_setting("backup_cloud_prefix")
    if not value:
        value = secrets.token_hex(16)
        db.set_setting("backup_cloud_prefix", value)
    return f"backups/{value}/"


def strip_secrets(source: Path, target: Path) -> None:
    """Та же копия, но в базе нет токенов, ключей и пароля."""
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(source) as src, \
            zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename != backup.DB_NAME:
                with src.open(info) as f, dst.open(info, "w") as out:
                    while chunk := f.read(1024 * 1024):
                        out.write(chunk)
                continue
            db_path = Path(src.extract(info, tmp))
            conn = sqlite3.connect(db_path)
            try:
                with conn:
                    conn.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(SECRET_SETTINGS))})",
                                 SECRET_SETTINGS)
                conn.execute("VACUUM")  # иначе удалённые значения остаются в свободных страницах файла
            finally:
                conn.close()
            dst.write(db_path, backup.DB_NAME)


def _set_state(**fields) -> None:
    for key, value in fields.items():
        db.set_setting(f"backup_cloud_{key}", value)


def upload(name: str, transport=None) -> dict:
    """Отправить копию с диска в облако (без секретов) и оставить в облаке последние CLOUD_KEEP."""
    source = backup.path_of(name)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / name
            # с сохранённым паролем копии ключи едут в облако зашифрованными — переезд без ввода ключей заново
            from . import migration
            migration.seal(source, clean, db.get_setting("backup_password") or None)
            r2.put_file(prefix() + name, clean, transport=transport)
        for old in listing(transport)[CLOUD_KEEP:]:
            r2.delete(prefix() + old["name"], transport=transport)
    except (r2.R2Error, OSError, zipfile.BadZipFile, sqlite3.DatabaseError, ValueError) as exc:
        _set_state(error=f"Копия {name} не отправлена в облако: {exc}")
        log.warning("Облачная копия %s не отправлена: %s", name, exc)
        raise CloudBackupError(f"Копия не отправлена в облако: {exc}")
    _set_state(error="", last=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log.info("Копия %s отправлена в облако R2", name)
    return {"name": name}


def listing(transport=None) -> list[dict]:
    """Копии в облаке, новые сверху."""
    start = prefix()
    items = []
    for obj in r2.list_keys(start, transport=transport):
        name = obj["key"][len(start):]
        if backup.NAME_RE.match(name):
            items.append({**backup.describe(name), "size": obj["size"]})
    return sorted(items, key=lambda b: b["name"], reverse=True)


def stage_restore(name: str, transport=None) -> None:
    """Скачать копию из облака и поставить в очередь на восстановление (как копию с диска)."""
    if not backup.NAME_RE.match(name):
        raise CloudBackupError("Нет такой копии")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        try:
            with open(path, "wb") as out:
                r2.download(prefix() + name, out, transport=transport)
        except r2.R2Error as exc:
            raise CloudBackupError(f"Не удалось скачать копию из облака: {exc}")
        with open(path, "rb") as f:
            backup.stage_restore_stream(f)


def after_backup(info: dict) -> None:
    """Вызывается после копии на диск: ежедневные и ручные копии уходят и в облако, если это включено."""
    if enabled() and info.get("reason") in REASONS:
        try:
            upload(info["name"])
        except CloudBackupError:
            pass  # ошибка сохранена и видна в «Настройках»; копия на диске уже есть


def status() -> dict:
    return {"available": r2.configured(), "enabled": enabled(), "last": db.get_setting("backup_cloud_last"),
            "error": db.get_setting("backup_cloud_error") if enabled() else ""}
