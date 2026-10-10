"""Этап 6 — «программа гладкая»: меню в одном месте, первые шаги, чистка, журнал программы."""

import pathlib
import re

from promloader import db, main

STATIC = pathlib.Path(main.__file__).parent / "static"


def nav_items():
    js = (STATIC / "common.js").read_text(encoding="utf-8")
    block = re.search(r"const NAV = \[(.*?)\n\];", js, re.S).group(1)
    return re.findall(r'\["(/[a-z]*)", "([^"]+)"(?:, \[([^\]]*)\])?\]', block)


def test_menu_is_defined_once():
    """Меню было скопировано в 11 HTML — новый пункт приходилось добавлять в каждый."""
    for path, name in main.PAGES.items():
        html = (STATIC / name).read_text(encoding="utf-8")
        assert "<nav></nav>" in html, name
        assert "/static/common.js" in html, name
    items = nav_items()
    covered = {href for href, _, _ in items} | {a.strip(' "') for _, _, also in items for a in also.split(",") if also}
    assert covered == set(main.PAGES)
    assert ("/diagnose", "Проверка выгрузки", "") in items


def test_browser_never_mixes_new_page_with_old_scripts(client, monkeypatch):
    """После обновления до 2.2.0 браузер взял старые common.js и app.css из кеша к новому HTML — пропало меню.
    Ссылки на скрипты и стили — с номером версии, а сами файлы браузер сверяет каждый раз."""
    from promloader import updater
    version = updater.current_version()
    for path in main.PAGES:
        html = client.get(path).text
        assets = re.findall(r'(?:src|href)="(/static/[^"]+\.(?:js|css)[^"]*)"', html)
        assert assets and all(a.endswith(f"?v={version}") for a in assets), (path, assets)
    assert f'/static/common.js?v={version}"' in client.get("/").text
    monkeypatch.setattr(updater, "current_version", lambda *a, **k: "9.9.9")
    assert '/static/common.js?v=9.9.9"' in client.get("/").text      # новая версия — новые адреса
    r = client.get("/static/common.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    again = client.get("/static/common.js", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"


def test_first_steps(client):
    from promloader import products, suppliers
    db.set_setting("prom_token", "")
    f = client.get("/api/meta").json()["first_steps"]
    assert f == {"token": False, "photos": "tunnel", "products": 0, "suppliers": 0, "sent": False}
    db.set_setting("prom_token", "t")
    db.set_setting("photo_tunnel", "0")
    assert client.get("/api/meta").json()["first_steps"]["photos"] == "off"
    db.set_setting("public_base_url", "https://shop.example.com")
    assert client.get("/api/meta").json()["first_steps"]["photos"] == "site"
    pid = products.create({"name": "Гачок", "price": 10})
    suppliers.create("Опт")
    with db.tx() as c:
        c.execute("UPDATE products SET synced_at = ? WHERE id = ?", (db.now(), pid))
    f = client.get("/api/meta").json()["first_steps"]
    assert f["token"] and f["products"] == 1 and f["suppliers"] == 1 and f["sent"]


# ---------- уборка ----------

def _old(path, days=10):
    import os
    t = __import__("time").time() - days * 24 * 3600
    os.utime(path, (t, t))


def test_housekeeping_jobs_runs_files(tmp_path):
    import json

    from promloader import housekeeping, phototunnel, products, suppliers
    from .conftest import make_image
    old = "2026-01-01T00:00:00+00:00"
    sent = products.create({"name": "Выгружен", "price": 1})
    unsent = products.create({"name": "Не выгружен", "price": 1})
    with db.tx() as c:
        c.execute("UPDATE products SET synced_at = ? WHERE id = ?", (db.now(), sent))
        for pids in ([sent], [unsent], [999999]):
            c.execute("INSERT INTO sync_jobs (status, products, next_run_at, created_at, updated_at) VALUES ('done', ?, ?, ?, ?)",
                      (json.dumps({str(p): 1 for p in pids}), old, old, old))
        c.execute("INSERT INTO sync_jobs (status, products, next_run_at, created_at, updated_at) VALUES ('done', ?, ?, ?, ?)",
                  (json.dumps({str(sent): 1}), db.now(), db.now(), db.now()))
    sid = suppliers.create("Опт")
    with db.tx() as c:
        c.executemany("INSERT INTO supplier_runs (supplier_id, status, trigger, started_at) VALUES (?, 'ok', 'schedule', ?)",
                      [(sid, db.now())] * 130)
    stale_import = db.imports_dir() / "abc.xlsx"
    stale_import.write_bytes(b"x")
    _old(stale_import, 4)
    fresh_import = db.imports_dir() / "def.xlsx"
    fresh_import.write_bytes(b"x")
    stale_feed = phototunnel.feeds_dir() / "f.xml"
    stale_feed.write_bytes(b"<x/>")
    _old(stale_feed, 3)
    kept = products.add_image_file(sent, make_image())
    kept_file = db.uploads_dir() / kept["src"].removeprefix("/media/")
    _old(kept_file)
    orphan = db.uploads_dir() / "orphan.jpg"
    orphan.write_bytes(make_image("JPEG"))
    _old(orphan, 2)
    fresh_orphan = db.uploads_dir() / "just-saved.jpg"
    fresh_orphan.write_bytes(make_image("JPEG"))

    r = housekeeping.run()
    assert r["sync_jobs"] == 2                 # выгруженный и удалённый товар; задача с невыгруженным осталась
    left = [json.loads(j["products"]) for j in db.query("SELECT products FROM sync_jobs ORDER BY id")]
    assert left == [{str(unsent): 1}, {str(sent): 1}]
    assert r["supplier_runs"] == 30 and db.query_one("SELECT COUNT(*) AS n FROM supplier_runs")["n"] == 100
    assert not stale_import.exists() and fresh_import.exists() and not stale_feed.exists()
    assert kept_file.exists() and not orphan.exists() and fresh_orphan.exists()
    assert r["orphan_photos"] == 1
    assert housekeeping.run()["sync_jobs"] == 0


def test_housekeeping_keeps_last_diagnose_runs():
    from promloader import diagnose, housekeeping
    diagnose.RUNS.clear()
    for i in range(15):
        diagnose.RUNS[f"r{i}"] = {"id": f"r{i}", "done": i != 14, "started": i}
    assert housekeeping.diagnose_runs() == 4
    assert set(diagnose.RUNS) == {f"r{i}" for i in range(4, 15)}
    diagnose.RUNS.clear()


def test_log_file_rotates(tmp_path, monkeypatch):
    import logging
    monkeypatch.setattr(main, "LOG_LIMIT", 2000)
    handler = main.setup_log_file(tmp_path)
    try:
        log = logging.getLogger("promloader.test")
        for i in range(100):
            log.warning("строка журнала %d %s", i, "x" * 50)
        files = sorted(p.name for p in (tmp_path / "logs").iterdir())
        assert files == ["promloader.log", "promloader.log.1", "promloader.log.2"]
        assert all(p.stat().st_size <= 2200 for p in (tmp_path / "logs").iterdir())
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
