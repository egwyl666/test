"""SQLite-хранилище. Всё, что пользователь ввёл, сразу попадает сюда — Prom лишь получает копию."""

import json
import os
import sqlite3
import threading
import time
import uuid
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
    url_ua          TEXT NOT NULL DEFAULT '',
    auto_pipeline   INTEGER NOT NULL DEFAULT 0,
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
    auto_promote INTEGER NOT NULL DEFAULT 0,
    promoted     INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT '',
    date_created  TEXT NOT NULL DEFAULT '',
    data          TEXT NOT NULL,
    seen          INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_date ON orders(date_created);

-- посылки Новой Почты по заказам: и созданные программой, и созданные в кабинете Prom (номер — из заказа Prom)
CREATE TABLE IF NOT EXISTS shipments (
    order_id     INTEGER PRIMARY KEY,
    ttn          TEXT NOT NULL,
    ref          TEXT NOT NULL DEFAULT '',
    own          INTEGER NOT NULL DEFAULT 0,
    status_code  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    stage        TEXT NOT NULL DEFAULT '',
    arrived_at   TEXT,
    alerts       TEXT NOT NULL DEFAULT '[]',
    cost         TEXT NOT NULL DEFAULT '',
    prom_error   TEXT NOT NULL DEFAULT '',
    checked_at   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_recipients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    events      TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);

-- Запросы к ИИ: модель, токены, цена — для «сколько стоит» и лимитов
CREATE TABLE IF NOT EXISTS ai_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    job_id      INTEGER,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ai_usage_at ON ai_usage(at);
CREATE INDEX IF NOT EXISTS ai_usage_job ON ai_usage(job_id);

-- Очередь уведомлений Telegram: не дошло (нет связи) — повтор с паузой, порядок для каждого получателя сохраняется
CREATE TABLE IF NOT EXISTS tg_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    event       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    next_at     TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    sent_at     TEXT,
    failed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tg_outbox_pending ON tg_outbox(sent_at, failed, id);

CREATE TABLE IF NOT EXISTS support_tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    description  TEXT NOT NULL,
    contact      TEXT NOT NULL DEFAULT '',
    page         TEXT NOT NULL DEFAULT '',
    files        TEXT NOT NULL DEFAULT '[]',
    diagnostics  TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'saved',
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    sent_at      TEXT
);

CREATE TABLE IF NOT EXISTS r2_objects (
    target       TEXT NOT NULL,
    file         TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL,
    PRIMARY KEY (target, file)
);

CREATE TABLE IF NOT EXISTS product_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL,
    field       TEXT NOT NULL,
    old         TEXT NOT NULL DEFAULT '',
    new         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS product_changes_product ON product_changes(product_id, id);
CREATE INDEX IF NOT EXISTS product_changes_at ON product_changes(at);
CREATE INDEX IF NOT EXISTS product_changes_source ON product_changes(source);

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

-- Категории каталога Prom (не группы магазина): узнаём их из своего каталога, поиска и ссылок prom.ua
CREATE TABLE IF NOT EXISTS prom_categories (
    id          INTEGER PRIMARY KEY,
    caption     TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

-- Чат с покупателями Prom: комнаты и сообщения (копия, чтобы список открывался сразу и без интернета)
CREATE TABLE IF NOT EXISTS chat_rooms (
    room_ident      TEXT PRIMARY KEY,
    room_id         TEXT NOT NULL DEFAULT '',
    project         TEXT NOT NULL DEFAULT 'promua',
    buyer_name      TEXT NOT NULL DEFAULT '',
    buyer_phone     TEXT NOT NULL DEFAULT '',
    last_message    TEXT NOT NULL DEFAULT '',
    last_at         TEXT NOT NULL DEFAULT '',
    last_message_id INTEGER,
    unread          INTEGER NOT NULL DEFAULT 0,
    product_id      INTEGER,
    prom_product_id INTEGER,
    order_id        INTEGER,
    data            TEXT NOT NULL DEFAULT '{}',
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_rooms_unread ON chat_rooms(unread);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    events      TEXT NOT NULL DEFAULT '[]',
    device      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    last_error  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id           INTEGER PRIMARY KEY,
    room_ident   TEXT NOT NULL,
    sender       TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    is_my        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT '',
    type         TEXT NOT NULL DEFAULT 'message',
    status       TEXT NOT NULL DEFAULT '',
    product_id   INTEGER,
    attachments  TEXT NOT NULL DEFAULT '[]',
    data         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS chat_messages_room ON chat_messages(room_ident, created_at);

-- Пользователи программы (users.py): логин без учёта регистра, пароль — солёный PBKDF2. session_version
-- растёт при смене пароля и блокировке: все прежние входы этого пользователя перестают действовать
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY,
    login            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name             TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT 'viewer',
    password_hash    TEXT NOT NULL,
    disabled         INTEGER NOT NULL DEFAULT 0,
    session_version  INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    last_login_at    TEXT
);

-- Короткие замки на опасные действия (db.acquire): видны и другому процессу, и программе после перезапуска.
-- until — до какого времени (секунды Unix) замок действует, если его не сняли (процесс упал посреди запроса)
CREATE TABLE IF NOT EXISTS locks (
    name   TEXT PRIMARY KEY,
    owner  TEXT NOT NULL,
    until  REAL NOT NULL
);

-- Роли пользователей: готовые (builtin) и свои; perms — JSON-список прав (users.EDITABLE_PERMS)
CREATE TABLE IF NOT EXISTS roles (
    id        TEXT PRIMARY KEY,
    label     TEXT NOT NULL,
    perms     TEXT NOT NULL DEFAULT '[]',
    builtin   INTEGER NOT NULL DEFAULT 0,
    position  INTEGER NOT NULL DEFAULT 0
);

-- Правило «группа магазина → категория Prom»: применяется к товарам группы, у которых категория не закреплена
CREATE TABLE IF NOT EXISTS group_categories (
    group_name   TEXT PRIMARY KEY,
    category_id  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

# Колонки, добавленные после первой версии: для уже существующих баз добавляются через ALTER TABLE.
MIGRATIONS = {
    "product_changes": {
        "user": "TEXT NOT NULL DEFAULT ''",  # кто из пользователей программы сделал правку (пусто — до 2.23.0 и фон)
    },
    "products": {
        "prom_conflict": "TEXT NOT NULL DEFAULT ''",
        "prom_seen": "TEXT NOT NULL DEFAULT ''",
        "cost_price": "REAL",
        "rrp": "REAL",
        "supplier_id": "INTEGER REFERENCES suppliers(id) ON DELETE SET NULL",
        "locked_fields": "TEXT NOT NULL DEFAULT '[]'",
        "prom_id": "INTEGER",
        "pending_fields": "TEXT NOT NULL DEFAULT '[]'",
        "barcode": "TEXT NOT NULL DEFAULT ''",
        "vendor_code": "TEXT NOT NULL DEFAULT ''",
        "cost_currency": "TEXT NOT NULL DEFAULT ''",
        "delete_next_at": "TEXT",
        "delete_attempts": "INTEGER NOT NULL DEFAULT 0",
        # в кабинете Prom у товара не заполнен «Ідентифікатор_товару» (узнали при загрузке каталога)
        "prom_no_ext": "INTEGER NOT NULL DEFAULT 0",
        "videos": "TEXT NOT NULL DEFAULT '[]'",  # ссылки YouTube (JSON-список)
        # категория каталога Prom. Две колонки, а не одна: что стоит на Prom сейчас (пишет только загрузка
        # каталога) и что нужно нам — иначе не увидеть, где Prom поставил не то
        "prom_category_id": "INTEGER",
        "target_category_id": "INTEGER",
        # «скрыт как дубль»: номер выгодного товара, ради которого этот убран в черновики. Такой товар не считается
        # готовым, не попадает в автоконвейер и в «возможные дубли»
        "dup_of": "INTEGER",
    },
    "supplier_items": {
        "ignored": "INTEGER NOT NULL DEFAULT 0",
    },
    "ai_jobs": {
        "auto_promote": "INTEGER NOT NULL DEFAULT 0",
        "promoted": "INTEGER NOT NULL DEFAULT 0",
        # кто запустил задание: его имя пишется в журнал у правок ИИ (пусто — автоконвейер по расписанию)
        "user": "TEXT NOT NULL DEFAULT ''",
    },
    "suppliers": {
        "merge_by_barcode": "INTEGER NOT NULL DEFAULT 1",
        "rate_mode": "TEXT NOT NULL DEFAULT ''",
        "rate_currency": "TEXT NOT NULL DEFAULT 'USD'",
        "rate_value": "REAL NOT NULL DEFAULT 0",
        "rate_add": "REAL NOT NULL DEFAULT 0",
        "clean_names": "INTEGER NOT NULL DEFAULT 0",
        "url_ua": "TEXT NOT NULL DEFAULT ''",
        "auto_pipeline": "INTEGER NOT NULL DEFAULT 0",
    },
    "shipments": {
        "warehouse": "TEXT NOT NULL DEFAULT ''",     # отделение получателя (из отслеживания)
        "keep_until": "TEXT NOT NULL DEFAULT ''",    # с какой даты хранение платное (DatePayedKeeping), ГГГГ-ММ-ДД
        "reminded_at": "TEXT NOT NULL DEFAULT ''",   # когда напомнили покупателю в чат забрать посылку
    },
    "sync_jobs": {
        "kind": "TEXT NOT NULL DEFAULT 'import'",
        "started_at": "TEXT",
        "sent": "TEXT",
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


def default_dir() -> Path:
    """Папка данных из настроек запуска — её можно узнать и до открытия базы."""
    return Path(os.environ.get("PROMLOADER_DATA", "data")).resolve()


def init(path: str | os.PathLike | None = None) -> None:
    """Открывает (или создаёт) базу. Повторный вызов переключает на другой каталог — удобно для тестов."""
    global _conn, _data_dir
    base = Path(path).resolve() if path else default_dir()
    base.mkdir(parents=True, exist_ok=True)
    (base / "uploads").mkdir(exist_ok=True)
    (base / "imports").mkdir(exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        conn = sqlite3.connect(base / "promloader.sqlite3", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # LIKE в SQLite не различает регистр только у латиницы — для поиска по-русски/по-украински сравниваем
        # строки в нижнем регистре: lower_u(name) LIKE '%кросс%'
        conn.create_function("lower_u", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        conn.execute("PRAGMA journal_mode=WAL")
        # в режиме WAL NORMAL не теряет целостность базы при сбое питания, но пишет заметно быстрее FULL;
        # busy_timeout — подождать, а не упасть «database is locked», если базу держит другой процесс (копия)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
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
                if (table, name) == ("products", "cost_currency"):
                    # раньше закупка была в той же валюте, что и цена
                    conn.execute("UPDATE products SET cost_currency = currency "
                                 "WHERE cost_price IS NOT NULL OR rrp IS NOT NULL")
                if (table, name) == ("products", "pending_fields"):
                    # что именно меняли до обновления программы, неизвестно — такие товары отправим полным импортом
                    conn.execute("""UPDATE products SET pending_fields = '["*"]'
                                    WHERE synced_at IS NOT NULL AND status IN ('ready', 'error', 'sending')""")
    conn.execute("CREATE INDEX IF NOT EXISTS products_supplier ON products(supplier_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS products_status ON products(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS products_barcode ON products(barcode)")


def suppliers_dir() -> Path:
    path = data_dir() / "suppliers"
    path.mkdir(exist_ok=True)
    return path


@contextmanager
def tx():
    """Транзакция: либо всё записано, либо ничего.

    Вложенный вызов (например, запомнить курс НБУ посреди пересчёта цен) становится частью внешней транзакции.
    """
    with _lock:
        assert _conn is not None, "db.init() не вызван"
        if _conn.in_transaction:
            yield _conn
            return
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
        except BaseException:
            _conn.execute("ROLLBACK")
            raise
        else:
            _conn.execute("COMMIT")


def in_transaction() -> bool:
    """Идёт ли сейчас внешняя транзакция (например, «пробный прогон», который потом откатится)."""
    return bool(_conn is not None and _conn.in_transaction)


# Список значений одним параметром: «id IN_LIST» + as_list(ids). Вместо «IN (?, ?, …)»: в SQLite не больше 32 766
# параметров в запросе (а в старых версиях — 999), и «все товары по фильтру» в большом магазине давали ошибку.
IN_LIST = "IN (SELECT value FROM json_each(?))"


def paused() -> bool:
    """Фоновая работа на паузе: данные перенесены на другой компьютер или сервер, и работает он — иначе оба
    забирали бы заказы и чат с Prom, слали двойные уведомления и мешали друг другу при отправке."""
    return get_setting("paused") == "1"


def acquire(name: str, ttl: float) -> str | None:
    """Взять замок name на ttl секунд. Возвращает ключ владельца (для release) или None — замок уже держит кто-то
    другой (ещё не истёк). Одна строка INSERT OR IGNORE: атомарно и между процессами (SQLite сам их упорядочивает)."""
    owner = f"{os.getpid()}-{uuid.uuid4().hex}"
    now = time.time()
    with tx() as c:
        c.execute("DELETE FROM locks WHERE name = ? AND until < ?", (name, now))
        taken = c.execute("INSERT OR IGNORE INTO locks (name, owner, until) VALUES (?, ?, ?)",
                          (name, owner, now + ttl)).rowcount
    return owner if taken else None


def release(name: str, owner: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))


def as_list(values) -> str:
    return json.dumps(list(values), ensure_ascii=False)


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
