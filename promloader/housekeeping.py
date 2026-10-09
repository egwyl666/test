"""Уборка раз в сутки: чтобы база и папка данных не росли бесконечно.

Запускается после суточной резервной копии — всё удалённое остаётся в ней.
Удаляется только то, что программе больше не нужно:
- задачи выгрузки старше 90 дней, если все их товары уже выгружены или удалены. Задачи с невыгруженным товаром
  остаются: по ним программа знает, что товар мог попасть на Prom (удаление спросит и Prom);
- история запусков поставщика — кроме последних 100;
- файлы импорта старше 3 дней и файлы выгрузки «по ссылке» старше 2 дней;
- файлы фото, которых нет ни у одного товара (старше суток — чтобы не задеть фото, которое как раз сохраняется);
- журнал изменений товаров старше полугода;
- учёт запросов к ИИ старше года (расходы показываются за день и за месяц);
- результаты «Проверки выгрузки» в памяти — кроме последних 10.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import changes, db, diagnose, phototunnel, sync

log = logging.getLogger("promloader.housekeeping")

JOB_KEEP_DAYS = 90
RUNS_PER_SUPPLIER = 100
IMPORT_KEEP_SECONDS = 3 * 24 * 3600
PHOTO_GRACE_SECONDS = 24 * 3600
DIAGNOSE_KEEP = 10
AI_USAGE_KEEP_DAYS = 366


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def old_jobs() -> int:
    rows = db.query("SELECT id, products FROM sync_jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
                    (_ago(JOB_KEEP_DAYS),))
    if not rows:
        return 0
    # товары, о которых задача ещё «помнит» что-то нужное: есть в программе, но не выгружены и без номера на Prom
    unsent = {r["id"] for r in db.query("SELECT id FROM products WHERE synced_at IS NULL AND prom_id IS NULL")}
    drop = []
    for r in rows:
        try:
            ids = {int(pid) for pid in json.loads(r["products"])}
        except (ValueError, TypeError):
            ids = set()
        if not ids & unsent:
            drop.append(r["id"])
    if drop:
        with db.tx() as c:
            c.execute(f"DELETE FROM sync_jobs WHERE id {db.IN_LIST}", (db.as_list(drop),))
    return len(drop)


def old_runs() -> int:
    with db.tx() as c:
        return c.execute(
            """DELETE FROM supplier_runs WHERE id IN (
                   SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY supplier_id ORDER BY id DESC) AS n
                                   FROM supplier_runs) WHERE n > ?)""", (RUNS_PER_SUPPLIER,)).rowcount


def _old_files(folder, seconds: float, keep=frozenset()) -> int:
    count, now = 0, time.time()
    for path in folder.iterdir():
        if not path.is_file() or path.name in keep:
            continue
        try:
            if now - path.stat().st_mtime > seconds:
                path.unlink()
                count += 1
        except OSError:
            continue  # файл занят (Windows) — в следующий раз
    return count


def orphan_photos() -> int:
    used = {r["file"] for r in db.query("SELECT DISTINCT file FROM images WHERE file IS NOT NULL")}
    return _old_files(db.uploads_dir(), PHOTO_GRACE_SECONDS, frozenset(used))


def diagnose_runs() -> int:
    finished = sorted((r for r in diagnose.RUNS.values() if r.get("done")), key=lambda r: r.get("started", 0))
    extra = finished[:-DIAGNOSE_KEEP] if len(finished) > DIAGNOSE_KEEP else []
    for r in extra:
        diagnose.RUNS.pop(r["id"], None)
    return len(extra)


def old_ai_usage() -> int:
    with db.tx() as c:
        return c.execute("DELETE FROM ai_usage WHERE at < ?", (_ago(AI_USAGE_KEEP_DAYS),)).rowcount


def run() -> dict:
    """Всё сразу. Ошибка одной уборки не мешает остальным."""
    steps = {
        "sync_jobs": old_jobs,
        "supplier_runs": old_runs,
        "import_files": lambda: _old_files(db.imports_dir(), IMPORT_KEEP_SECONDS),
        "feed_files": lambda: _old_files(phototunnel.feeds_dir(), sync.FEED_KEEP_SECONDS),
        "orphan_photos": orphan_photos,
        "changes": changes.cleanup,
        "diagnose_runs": diagnose_runs,
        "ai_usage": old_ai_usage,
    }
    result = {}
    for name, step in steps.items():
        try:
            result[name] = step()
        except Exception:
            log.exception("Уборка «%s» не удалась", name)
            result[name] = None
    if any(result.values()):
        log.info("Уборка: %s", ", ".join(f"{k} {v}" for k, v in result.items() if v))
    return result
