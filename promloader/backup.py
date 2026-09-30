"""Резервные копии: база + загруженные фото + файлы прайсов поставщиков в один zip.

- автоматически раз в сутки и перед каждым обновлением программы;
- хранятся последние KEEP_DAILY ежедневных и KEEP_OTHER остальных (ручные, перед обновлением);
- восстановление делается при запуске программы (restore-pending.zip), пока база не открыта.
"""

import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

log = logging.getLogger("promloader.backup")

KEEP_DAILY = 7
KEEP_OTHER = 5
DB_NAME = "promloader.sqlite3"
FOLDERS = ("uploads", "suppliers")
NAME_RE = re.compile(r"^promloader-(\d{8}-\d{6})-([a-z-]+)\.zip$")
REASONS = {"daily": "ежедневная", "manual": "вручную", "before-update": "перед обновлением", "before-restore": "перед восстановлением"}


class BackupError(Exception):
    pass


def backups_dir() -> Path:
    path = db.data_dir() / "backups"
    path.mkdir(exist_ok=True)
    return path


def create(reason: str = "manual") -> dict:
    """Снимок базы делается через sqlite backup API — копия целостная, даже если программа работает."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups_dir() / f"promloader-{stamp}-{reason}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / DB_NAME
        with db._lock:
            dst = sqlite3.connect(snapshot)
            try:
                db._conn.backup(dst)
            finally:
                dst.close()
        partial = target.with_suffix(".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, DB_NAME)
            for folder in FOLDERS:
                base = db.data_dir() / folder
                if not base.exists():
                    continue
                for path in base.rglob("*"):
                    if path.is_file():
                        # фото уже сжаты — не тратим время на повторное сжатие
                        z.write(path, f"{folder}/{path.relative_to(base).as_posix()}", compress_type=zipfile.ZIP_STORED)
        partial.replace(target)
    db.set_setting("last_backup_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    prune()
    log.info("Резервная копия: %s", target.name)
    return _info(target)


def _info(path: Path) -> dict:
    m = NAME_RE.match(path.name)
    try:
        created = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except (AttributeError, ValueError):  # чужое или переименованное имя файла — берём дату изменения
        created = datetime.fromtimestamp(path.stat().st_mtime)
    reason = m.group(2) if m else "manual"
    return {"name": path.name, "size": path.stat().st_size, "created": created.isoformat(timespec="seconds"),
            "reason": reason, "reason_label": REASONS.get(reason, reason)}


def list_backups() -> list[dict]:
    items = [_info(p) for p in backups_dir().glob("promloader-*.zip")]
    return sorted(items, key=lambda b: b["name"], reverse=True)


def prune() -> None:
    items = list_backups()
    daily = [b for b in items if b["reason"] == "daily"]
    other = [b for b in items if b["reason"] != "daily"]
    for b in daily[KEEP_DAILY:] + other[KEEP_OTHER:]:
        (backups_dir() / b["name"]).unlink(missing_ok=True)


def path_of(name: str) -> Path:
    if not NAME_RE.match(name):
        raise BackupError("Нет такой копии")
    path = backups_dir() / name
    if not path.exists():
        raise BackupError("Нет такой копии")
    return path


def due() -> bool:
    last = db.get_setting("last_backup_at")
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=23)


def _validate(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            if DB_NAME not in z.namelist():
                raise BackupError("В архиве нет базы товаров — это не резервная копия Prom Loader")
            bad = z.testzip()
            if bad:
                raise BackupError(f"Копия повреждена ({bad})")
    except zipfile.BadZipFile:
        raise BackupError("Файл повреждён или это не резервная копия")


def stage_restore(name: str) -> None:
    """Запланировать восстановление: само восстановление произойдёт при перезапуске."""
    path = path_of(name)
    _validate(path)
    shutil.copy2(path, db.data_dir() / "restore-pending.zip")


def stage_restore_upload(content: bytes) -> None:
    target = db.data_dir() / "restore-pending.zip"
    target.write_bytes(content)
    try:
        _validate(target)
    except BackupError:
        target.unlink(missing_ok=True)
        raise


def apply_pending(data_dir: Path) -> bool:
    """Вызывается при запуске ДО открытия базы. Текущие данные сперва сохраняются в копию."""
    pending = data_dir / "restore-pending.zip"
    if not pending.exists():
        return False
    db.init(data_dir)
    try:
        create(reason="before-restore")
    finally:
        db._conn.close()
        db._conn = None
    with zipfile.ZipFile(pending) as z:
        for folder in FOLDERS:
            shutil.rmtree(data_dir / folder, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            (data_dir / (DB_NAME + suffix)).unlink(missing_ok=True)
        for info in z.infolist():
            target = (data_dir / info.filename).resolve()
            if not str(target).startswith(str(data_dir.resolve())):
                continue
            z.extract(info, data_dir)
    pending.unlink()
    log.info("Данные восстановлены из резервной копии")
    return True
