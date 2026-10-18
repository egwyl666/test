"""Технический фундамент 2.13.0: настройки SQLite, одна выборка вместо запроса на товар, пустой ключ фида,
защита APP_PASSWORD от перебора, уборка, облачные копии без секретов."""

import io
import re
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from promloader import backup, cloudbackup, db, housekeeping, notify, products, r2

TOKEN = "prom-token-SECRET-1234567890"


def test_sqlite_settings_and_index():
    assert db.query_one("PRAGMA synchronous")[0] == 1  # NORMAL
    assert db.query_one("PRAGMA busy_timeout")[0] == 10000
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "product_changes_source" in names
    assert housekeeping.run()["optimize"] == 0


def test_feed_rejects_empty_key(client):
    assert client.get("/feed/prom.yml", params={"key": ""}).status_code == 403
    assert client.get("/feed/prom.yml").status_code == 403


def test_job_file_skips_deleted_products_in_one_query(client):
    a = products.create({"name": "Перший", "price": 10})
    b = products.create({"name": "Другий", "price": 20})
    with db.tx() as c:
        job = c.execute("INSERT INTO sync_jobs (status, products, created_at, next_run_at, updated_at) VALUES ('done', ?, ?, ?, ?)",
                        (f"[{b}, 999999, {a}]", db.now(), db.now(), db.now())).lastrowid
    xml = client.get(f"/api/sync/jobs/{job}/file").text
    assert xml.index("Другий") < xml.index("Перший") and "999999" not in xml  # порядок задания, без удалённого


def test_app_password_brute_force_is_paused(monkeypatch):
    from promloader import main

    monkeypatch.setenv("APP_PASSWORD", "правильний")
    sent = []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append(text))
    main.basic_limiter.reset()
    c = TestClient(main.app, client=("203.0.113.9", 5000))
    assert c.get("/api/meta").status_code == 401  # первый запрос браузера без пароля — не попытка
    for _ in range(5):
        assert c.get("/api/meta", auth=("admin", "не-той")).status_code == 401
    r = c.get("/api/meta", auth=("admin", "правильний"))
    assert r.status_code == 429 and "5 минут" in r.json()["detail"]
    assert len(sent) == 1 and "203.0.113.9" in sent[0]
    # другой адрес не заблокирован
    other = TestClient(main.app, client=("203.0.113.10", 5000))
    assert other.get("/api/meta", auth=("admin", "правильний")).status_code == 200
    main.basic_limiter.reset()


# ---------- облачные копии ----------

class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/", 2)[2] if request.url.path.count("/") >= 2 else ""
        if request.method == "PUT":
            assert request.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
            self.objects[key] = request.read()
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        if request.method == "GET" and request.url.params.get("list-type") == "2":
            prefix = request.url.params["prefix"]
            body = "".join(f"<Contents><Key>{k}</Key><Size>{len(v)}</Size><LastModified>2026-10-15T00:00:00Z</LastModified></Contents>"
                           for k, v in sorted(self.objects.items()) if k.startswith(prefix))
            return httpx.Response(200, text=f"<ListBucketResult>{body}</ListBucketResult>")
        if request.method == "GET" and key in self.objects:
            return httpx.Response(200, content=self.objects[key])
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")


@pytest.fixture
def cloud(monkeypatch):
    for key, value in {"r2_account_id": "a" * 32, "r2_access_key_id": "AK", "r2_secret_access_key": "SK-SECRET-r2",
                       "r2_bucket": "shop", "r2_public_url": "https://pub-x.r2.dev"}.items():
        db.set_setting(key, value)
    db.set_setting("backup_cloud", "1")
    fake = FakeR2()
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.Client
    monkeypatch.setattr(r2.httpx, "Client", lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}))
    return fake


def test_cloud_copy_has_no_secrets_and_random_name(cloud):
    db.set_setting("prom_token", TOKEN)
    db.set_setting("phone_password_hash", "pbkdf2_sha256$1$aa$bb")
    products.create({"name": "Гірлянда", "price": 100})
    info = backup.create("manual")
    cloudbackup.after_backup(info)
    (key, data), = cloud.objects.items()
    assert re.fullmatch(r"backups/[0-9a-f]{32}/promloader-\d{8}-\d{6}-manual\.zip", key)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        raw = z.read(backup.DB_NAME)
    # ни в значениях, ни в «свободных страницах» файла (VACUUM)
    for secret in (TOKEN, "SK-SECRET-r2", "pbkdf2_sha256"):
        assert secret.encode() not in raw
    assert "Гірлянда".encode() in raw
    # копия «перед обновлением» в облако не уходит
    cloudbackup.after_backup(backup.create("before-update"))
    assert len(cloud.objects) == 1
    assert [b["name"] for b in cloudbackup.listing()] == [info["name"]]


def test_cloud_keeps_last_ten(cloud, monkeypatch):
    monkeypatch.setattr(cloudbackup, "CLOUD_KEEP", 2)
    names = []
    for i in range(3):
        path = backup.backups_dir() / f"promloader-2026101{i}-000000-daily.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("uploads/x.jpg", b"1")
        cloudbackup.upload(path.name)
        names.append(path.name)
    assert [b["name"] for b in cloudbackup.listing()] == names[:0:-1]


def test_restore_from_cloud_keeps_this_computers_tokens(cloud, data_dir):
    db.set_setting("prom_token", TOKEN)
    products.create({"name": "Старий товар", "price": 1})
    info = backup.create("manual")
    cloudbackup.upload(info["name"])
    products.create({"name": "Новий товар", "price": 2})

    cloudbackup.stage_restore(info["name"])
    assert (data_dir / "restore-pending.zip").exists()
    db._conn.close()
    db._conn = None
    assert backup.apply_pending(Path(data_dir))
    db.init(data_dir)
    names = {r["name"] for r in db.query("SELECT name FROM products")}
    assert names == {"Старий товар"}
    assert db.get_setting("prom_token") == TOKEN  # в облачной копии токена нет — остался свой
    assert db.get_setting("backup_cloud") == "1"


def test_cloud_toggle_needs_r2(client):
    r = client.post("/api/backups/cloud", json={"enabled": True})
    assert r.status_code == 400 and "R2" in r.json()["detail"]
    assert client.get("/api/backups/cloud").json()["available"] is False


def test_cloud_listing_endpoint(cloud, client):
    client.post("/api/backups")  # ручная копия — в облако в фоне
    import time
    for _ in range(50):
        if cloud.objects:
            break
        time.sleep(0.1)
    items = client.get("/api/backups/cloud").json()["items"]
    assert len(items) == 1 and items[0]["reason"] == "manual"


def test_strip_secrets_leaves_other_settings(tmp_path):
    tmp_path = tmp_path / "strip"  # сам tmp_path — папка данных программы
    tmp_path.mkdir()
    src = tmp_path / "a.zip"
    dbfile = tmp_path / backup.DB_NAME
    conn = sqlite3.connect(dbfile)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO settings VALUES (?, ?)", [("prom_token", TOKEN), ("shop_name", "Мій магазин")])
    conn.commit()
    conn.close()
    with zipfile.ZipFile(src, "w") as z:
        z.write(dbfile, backup.DB_NAME)
    out = tmp_path / "b.zip"
    cloudbackup.strip_secrets(src, out)
    with zipfile.ZipFile(out) as z:
        z.extract(backup.DB_NAME, tmp_path / "x")
    rows = dict(sqlite3.connect(tmp_path / "x" / backup.DB_NAME).execute("SELECT key, value FROM settings").fetchall())
    assert rows == {"shop_name": "Мій магазин"}


def test_log_redacts_secrets_in_urls(tmp_path):
    """2.17.3: httpx писал в журнал токен бота Telegram и ключи из ссылок прайсов — журнал уходит в «Поддержку»."""
    import logging

    from promloader import config, main
    line = ('HTTP Request: POST https://api.telegram.org/bot8902121553:AAH_VJS-x/sendMessage "200" '
            'GET https://ncase.ua/xml2/?key=01a5e35d&lang=ua POST https://fcm.googleapis.com/fcm/send/fWF1:APA91b "201"')
    clean = config.redact(line)
    assert "AAH_VJS" not in clean and "01a5e35d" not in clean and "APA91b" not in clean
    assert "/bot***/sendMessage" in clean and "?key=***&lang=ua" in clean
    handler = main.setup_log_file(tmp_path)
    logging.getLogger("httpx").warning("HTTP Request: %s", "https://api.telegram.org/bot123:SECRET/getMe")
    handler.flush()
    text = (tmp_path / "logs" / "promloader.log").read_text(encoding="utf-8")
    assert "SECRET" not in text and "/bot***/getMe" in text
    logging.getLogger().removeHandler(handler)
    handler.close()
