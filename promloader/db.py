"""SQLite-хранилище. Всё, что пользователь ввёл, сразу попадает сюда — Prom лишь получает копию."""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id     TEXT UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    name_ua         TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    description_ua  TEXT NOT NULL DEFAULT '',
    price           REAL,
    old_price       REAL,
    currency        TEXT NOT NULL DEFAULT 'UAH',
    unit            TEXT NOT NULL DEFAULT 'шт.',
    quantity        INTEGER,
    presence        TEXT NOT NULL DEFAULT 'available',
    group_name      TEXT NOT NULL DEFAULT '',
    vendor          TEXT NOT NULL DEFAULT '',
    country         TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '',
    params          TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',
    last_error      TEXT NOT NULL DEFAULT '',
    revision        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    synced_at       TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    file        TEXT,
    url         TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS images_product ON images(product_id, position);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT NOT NULL,
    products     TEXT NOT NULL,
    import_id    TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_run_at  TEXT NOT NULL,
    last_error   TEXT NOT NULL DEFAULT '',
    result       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL DEFAULT '',
    source_file     TEXT NOT NULL DEFAULT '',
    source_name     TEXT NOT NULL DEFAULT '',
    sheet           TEXT NOT NULL DEFAULT '',
    header_row      INTEGER NOT NULL DEFAULT 1,
    rows            TEXT NOT NULL DEFAULT '',
    mapping         TEXT NOT NULL DEFAULT '{}',
    defaults        TEXT NOT NULL DEFAULT '{}',
    prefix          TEXT NOT NULL DEFAULT '',
    new_status      TEXT NOT NULL DEFAULT 'draft',
    missing_action  TEXT NOT NULL DEFAULT 'not_available',
    auto_sync       INTEGER NOT NULL DEFAULT 0,
    interval_hours  REAL NOT NULL DEFAULT 0,
    next_run_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id  INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    sku          TEXT NOT NULL,
    product_id   INTEGER REFERENCES products(id) ON DELETE SET NULL,
    data         TEXT NOT NULL,
    images_hash  TEXT NOT NULL DEFAULT '',
    missing      INTEGER NOT NULL DEFAULT 0,
    seen_at      TEXT NOT NULL,
    seen_run     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (supplier_id, sku)
);
CREATE INDEX IF NOT EXISTS supplier_items_product ON supplier_items(product_id);

CREATE TABLE IF NOT EXISTS supplier_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id  INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    stats        TEXT NOT NULL DEFAULT '{}',
    message      TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS supplier_runs_supplier ON supplier_runs(supplier_id, id);

CREATE TABLE IF NOT EXISTS schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    days            TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
    time            TEXT NOT NULL DEFAULT '09:00',
    missed_action   TEXT NOT NULL DEFAULT 'ask',
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_slot       TEXT,
    pending_slot    TEXT,
    snooze_until    TEXT,
    last_run_at     TEXT,
    last_result     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,
    instruction  TEXT NOT NULL DEFAULT '',
    only_empty   INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'running',
    message      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES ai_jobs(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    message     TEXT NOT NULL DEFAULT '',
    old         TEXT,
    new         TEXT
);
CREATE INDEX IF NOT EXISTS ai_items_job ON ai_items(job_id, status);

CREATE TABLE IF NOT EXISTS price_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position        INTEGER NOT NULL DEFAULT 0,
    supplier_id     INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    category        TEXT NOT NULL DEFAULT '',
    cost_from       REAL,
    cost_to         REAL,
    markup_percent  REAL NOT NULL DEFAULT 0,
    markup_fixed    REAL NOT NULL DEFAULT 0,
    rounding        TEXT NOT NULL DEFAULT 'none',
    use_rrp         INTEGER NOT NULL DEFAULT 0
);
"""

# Колонки, добавленные после первой версии: для уже существующих баз добавляются через ALTER TABLE.
MIGRATIONS = {
    "products": {
        "cost_price": "REAL",
        "rrp": "REAL",
        "supplier_id": "INTEGER REFERENCES suppliers(id) ON DELETE SET NULL",
        "locked_fields": "TEXT NOT NULL DEFAULT '[]'",
        "prom_id": "INTEGER",
        "pending_fields": "TEXT NOT NULL DEFAULT '[]'",
        "barcode": "TEXT NOT NULL DEFAULT ''",
    },
    "suppliers": {
        "merge_by_barcode": "INTEGER NOT NULL DEFAULT 1",
    },
    "sync_jobs": {
        "kind": "TEXT NOT NULL DEFAULT 'import'",
    },
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_data_dir: Path | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_dir() -> Path:
    assert _data_dir is not None, "db.init() не вызван"
    return _data_dir


def uploads_dir() -> Path:
    return data_dir() / "uploads"


def imports_dir() -> Path:
    return data_dir() / "imports"


def init(path: str | os.PathLike | None = None) -> None:
    """Открывает (или создаёт) базу. Повторный вызов переключает на другой каталог — удобно для тестов."""
    global _conn, _data_dir
    base = Path(path or os.environ.get("PROMLOADER_DATA", "data")).resolve()
    base.mkdir(parents=True, exist_ok=True)
    (base / "uploads").mkdir(exist_ok=True)
    (base / "imports").mkdir(exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        conn = sqlite3.connect(base / "promloader.sqlite3", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _conn = conn
        _data_dir = base


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS products_supplier ON products(supplier_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS products_status ON products(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS products_barcode ON products(barcode)")


def suppliers_dir() -> Path:
    path = data_dir() / "suppliers"
    path.mkdir(exist_ok=True)
    return path


@contextmanager
def tx():
    """Транзакция: либо всё записано, либо ничего."""
    with _lock:
        assert _conn is not None, "db.init() не вызван"
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
        except BaseException:
            _conn.execute("ROLLBACK")
            raise
        else:
            _conn.execute("COMMIT")


def query(sql: str, params=()) -> list[sqlite3.Row]:
    with _lock:
        assert _conn is not None, "db.init() не вызван"
        return _conn.execute(sql, params).fetchall()


def query_one(sql: str, params=()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
