"""Переезд на другой компьютер или сервер: пакет с зашифрованными ключами, восстановление на «пустом» компьютере,
неверный пароль, код подключения к облаку, пауза старого компьютера, пути Windows → Linux."""

import asyncio
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from promloader import backup, cloudbackup, db, migration, products, sync

PASSWORD = "пароль-переїзду-42"
SECRETS = {"prom_token": "prom-SECRET-token-0123456789", "feed_key": "feed-SECRET-key", "gemini_key": "AIza-SECRET-gemini",
           "telegram_token": "123:SECRET-telegram", "r2_access_key_id": "AK-SECRET", "r2_secret_access_key": "SK-SECRET-r2"}


def fill_machine_a():
    for k, v in SECRETS.items():
        db.set_setting(k, v)
    for k, v in {"r2_account_id": "a" * 32, "r2_bucket": "shop", "r2_public_url": "https://pub-x.r2.dev",
                 "shop_name": "Мій магазин"}.items():
        db.set_setting(k, v)
    pid = products.create({"name": "Гірлянда Роса", "price": 460, "external_id": "A906"})
    photo = db.uploads_dir() / ("f" * 32 + ".jpg")
    photo.write_bytes(b"\xff\xd8photo")
    (db.suppliers_dir() / "7.xlsx").write_bytes(b"price-file")
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, file, position) VALUES (?, ?, 0)", (pid, photo.name))
    return pid


def all_bytes(path: Path) -> bytes:
    """Всё содержимое архива в открытом виде (база внутри zip сжата — проверяем распакованное)."""
    with zipfile.ZipFile(path) as z:
        return b"".join(z.read(n) for n in z.namelist())


def switch_to(path: Path):
    path.mkdir(exist_ok=True)
    db.init(path)


def restart_into(data: Path):
    """Как при перезапуске: база закрыта → восстановление → база открыта снова."""
    db._conn.close()
    db._conn = None
    assert backup.apply_pending(data)
    db.init(data)


def test_full_migration_to_blank_machine(tmp_path, data_dir):
    fill_machine_a()
    bundle = migration.create_bundle(PASSWORD)
    path = backup.path_of(bundle["name"])
    raw = all_bytes(path)
    for secret in SECRETS.values():
        assert secret.encode() not in raw  # ни в базе, ни где-либо ещё в открытом виде
    m = bundle["manifest"]
    assert m["products"] == 1 and m["photos_in_file"] == 1 and m["secrets_encrypted"] and "prom_token" in m["secrets"]
    data = path.read_bytes()

    machine_b = tmp_path / "B"
    switch_to(machine_b)
    assert db.query_one("SELECT COUNT(*) AS n FROM products")["n"] == 0
    info = migration.receive(io.BytesIO(data))
    assert info["products"] == 1 and info["secrets_encrypted"]
    migration.apply(PASSWORD)
    restart_into(machine_b)

    assert db.query_one("SELECT name, price FROM products")["name"] == "Гірлянда Роса"
    for k, v in SECRETS.items():
        assert db.get_setting(k) == v
    assert db.get_setting("shop_name") == "Мій магазин"
    assert (machine_b / "uploads" / ("f" * 32 + ".jpg")).read_bytes() == b"\xff\xd8photo"
    assert (machine_b / "suppliers" / "7.xlsx").read_bytes() == b"price-file"
    assert not (machine_b / backup.SECRETS_PENDING).exists()  # расшифрованные ключи не остаются файлом


def test_bundle_without_photos_is_light(data_dir):
    fill_machine_a()
    bundle = migration.create_bundle(PASSWORD, with_photos=False)
    with zipfile.ZipFile(backup.path_of(bundle["name"])) as z:
        names = z.namelist()
    assert not any(n.startswith("uploads/") for n in names) and "suppliers/7.xlsx" in names
    assert bundle["manifest"]["images"] == 1 and bundle["manifest"]["photos_in_file"] == 0


def test_wrong_password_changes_nothing(tmp_path, data_dir):
    fill_machine_a()
    data = backup.path_of(migration.create_bundle(PASSWORD)["name"]).read_bytes()
    switch_to(tmp_path / "B")
    products.create({"name": "Свій товар", "price": 1})
    migration.receive(io.BytesIO(data))
    with pytest.raises(migration.MigrationError, match="Неверный пароль копии"):
        migration.apply("не-той-пароль")
    with pytest.raises(migration.MigrationError, match="введите пароль копии"):
        migration.apply("")
    assert not (tmp_path / "B" / "restore-pending.zip").exists()
    assert db.query_one("SELECT name FROM products")["name"] == "Свій товар"


def test_short_password_rejected():
    with pytest.raises(migration.MigrationError, match="не меньше 8"):
        migration.create_bundle("1234567")


def test_tampered_secrets_detected():
    blob = json.loads(migration.encrypt(b'{"prom_token": "x"}', PASSWORD))
    blob["ciphertext"] = blob["ciphertext"][:-4] + ("AAAA" if not blob["ciphertext"].endswith("AAAA") else "BBBB")
    with pytest.raises(migration.MigrationError):
        migration.decrypt(json.dumps(blob).encode(), PASSWORD)
    assert migration.decrypt(migration.encrypt(b"ok", PASSWORD), PASSWORD) == b"ok"


def test_connection_code_to_new_machine(tmp_path, data_dir):
    fill_machine_a()
    prefix = cloudbackup.prefix()
    code = migration.connection_code(PASSWORD)
    assert code.startswith("PLK1-") and "SK-SECRET" not in code
    switch_to(tmp_path / "B")
    with pytest.raises(migration.MigrationError, match="Неверный пароль"):
        migration.connect(code, "не-той-пароль")
    migration.connect(f"  {code[:20]}\n{code[20:]} ", PASSWORD)  # перенос строки при копировании не мешает
    assert db.get_setting("r2_secret_access_key") == "SK-SECRET-r2" and cloudbackup.prefix() == prefix
    assert cloudbackup.enabled()


def test_cloud_copy_with_saved_password_carries_encrypted_keys(data_dir, monkeypatch):
    fill_machine_a()
    db.set_setting("backup_password", PASSWORD)
    sent = {}
    monkeypatch.setattr(cloudbackup.r2, "put_file", lambda key, path, **kw: sent.update(key=key, data=path.read_bytes()))
    monkeypatch.setattr(cloudbackup, "listing", lambda transport=None: [])
    info = backup.create("daily")
    cloudbackup.upload(info["name"])
    with zipfile.ZipFile(io.BytesIO(sent["data"])) as z:
        raw = b"".join(z.read(n) for n in z.namelist())
        keys = json.loads(migration.decrypt(z.read("secrets.enc"), PASSWORD))
    assert keys["prom_token"] == SECRETS["prom_token"] and keys["feed_key"] == SECRETS["feed_key"]
    for secret in (*SECRETS.values(), PASSWORD):
        assert secret.encode() not in raw
    # без сохранённого пароля — как раньше, ключей нет вовсе
    db.set_setting("backup_password", "")
    cloudbackup.upload(info["name"])
    with zipfile.ZipFile(io.BytesIO(sent["data"])) as z:
        assert "secrets.enc" not in z.namelist()


def test_pause_stops_background_sending(monkeypatch, client):
    calls = []

    async def fake_run_once():
        calls.append(1)

    monkeypatch.setattr(sync, "run_once", fake_run_once)
    monkeypatch.setattr(sync.promdelete, "process", lambda factory: asyncio.sleep(0))

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(sync.worker(stop, interval=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        await task

    assert client.post("/api/pause", json={"paused": True}).json()["paused"]
    assert client.get("/api/meta").json()["paused"] is True
    asyncio.run(run_briefly())
    assert calls == []
    client.post("/api/pause", json={"paused": False})
    asyncio.run(run_briefly())
    assert calls


def test_bundle_with_pause_does_not_carry_pause(client, tmp_path):
    fill_machine_a()
    r = client.post("/api/migration/bundle", json={"password": PASSWORD, "photos": True, "pause": True}).json()
    assert r["paused"] is True and db.paused()
    with zipfile.ZipFile(backup.path_of(r["name"])) as z:
        z.extract(backup.DB_NAME, tmp_path / "x")
    rows = dict(sqlite3.connect(tmp_path / "x" / backup.DB_NAME).execute("SELECT key, value FROM settings").fetchall())
    assert rows.get("paused") != "1"  # новый компьютер после восстановления сразу работает


def test_windows_paths_become_names(data_dir):
    pid = products.create({"name": "Фото з Windows", "price": 1})
    with db.tx() as c:
        c.execute("INSERT INTO images (product_id, file, position) VALUES (?, ?, 0)",
                  (pid, "C:\\Users\\shop\\data\\uploads\\" + "a" * 32 + ".jpg"))
        c.execute("INSERT INTO suppliers (name, source_file, created_at, updated_at) VALUES ('П', 'data/suppliers/3.xlsx', ?, ?)",
                  (db.now(), db.now()))
    assert backup.normalize_paths(data_dir / backup.DB_NAME) == 2
    assert db.query_one("SELECT file FROM images")["file"] == "a" * 32 + ".jpg"
    assert db.query_one("SELECT source_file FROM suppliers")["source_file"] == "3.xlsx"


def test_api_inspect_and_apply(client, tmp_path):
    fill_machine_a()
    data = backup.path_of(client.post("/api/migration/bundle", json={"password": PASSWORD}).json()["name"]).read_bytes()
    r = client.post("/api/migration/inspect", files={"file": ("move.zip", data, "application/zip")})
    assert r.status_code == 200 and r.json()["products"] == 1 and r.json()["secrets_encrypted"]
    bad = client.post("/api/migration/apply", json={"password": "не-той-пароль"})
    assert bad.status_code == 400 and "Неверный пароль" in bad.json()["detail"]
    assert client.post("/api/migration/apply", json={"password": PASSWORD}).status_code == 200
    junk = client.post("/api/migration/inspect", files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert junk.status_code == 400
