import io
import json
import zipfile

import httpx
import pytest

from promloader import backup, db, products, updater


def make_app(tmp_path, version="1.0.0"):
    app = tmp_path / "app"
    (app / "promloader").mkdir(parents=True)
    (app / "VERSION").write_text(version)
    (app / "promloader" / "main.py").write_text("OLD = 1\n")
    (app / "promloader" / "gone.py").write_text("x = 1\n")
    (app / "START.bat").write_text("old bat")
    (app / ".venv").mkdir()
    (app / ".venv" / "keep.txt").write_text("venv")
    return app


def make_zip(version="1.1.0", main="NEW = 2\n", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = "egwyl666-test-abc123/"
        z.writestr(top + "VERSION", version + "\n")
        z.writestr(top + "promloader/main.py", main)
        z.writestr(top + "promloader/added.py", "y = 2\n")
        z.writestr(top + "START.bat", "new bat")
        for name, content in (extra or {}).items():
            z.writestr(top + name, content)
    return buf.getvalue()


def github(version="1.1.0", zip_bytes=None, status=200):
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if status != 200:
            return httpx.Response(status, json={"message": "nope"})
        if path.endswith("/contents/VERSION"):
            return httpx.Response(200, text=version + "\n")
        if "/contents/" in path:
            return httpx.Response(200, text="# Что нового\n\n## 1.1.0\n\n- Новое\n\n## 1.0.0\n\n- Старое\n")
        if "/zipball/" in path:
            return httpx.Response(200, content=zip_bytes or make_zip(version))
        return httpx.Response(404)
    return httpx.MockTransport(handler), calls


def test_version_compare():
    assert updater.parse_version("1.10.0") > updater.parse_version("1.9.9")
    assert updater.parse_version("garbage") == (0,)


def test_check_reports_new_version_and_notes(tmp_path):
    app = make_app(tmp_path)
    transport, calls = github()
    db.set_setting("github_token", "tok")
    info = updater.check(transport, app)
    assert info["available"] and info["latest"] == "1.1.0"
    assert "Новое" in info["notes"] and "Старое" not in info["notes"]
    assert calls[0].headers["authorization"] == "Bearer tok"
    assert calls[0].url.params["ref"] == "main"
    assert db.get_setting("update_latest") == "1.1.0"

    same, _ = github(version="1.0.0")
    assert updater.check(same, app)["available"] is False


def test_private_repo_without_token(tmp_path):
    transport, _ = github(status=404)
    with pytest.raises(updater.UpdateError, match="токен"):
        updater.check(transport, make_app(tmp_path))


def test_dev_checkout_is_never_updated(tmp_path):
    app = make_app(tmp_path)
    (app / ".git").mkdir()
    assert updater.check(github()[0], app)["available"] is False
    with pytest.raises(updater.UpdateError, match="git"):
        updater.update(github()[0], app)


def test_install_replaces_code_keeps_data(tmp_path):
    app = make_app(tmp_path)
    products.create({"name": "Кружка", "price": 1})
    transport, _ = github()
    version = updater.update(transport, app)

    assert version == "1.1.0"
    assert (app / "VERSION").read_text().strip() == "1.1.0"
    assert (app / "promloader" / "main.py").read_text() == "NEW = 2\n"
    assert (app / "promloader" / "added.py").exists()
    assert not (app / "promloader" / "gone.py").exists()
    assert (app / ".venv" / "keep.txt").read_text() == "venv"
    previous = db.data_dir() / "updates" / "previous"
    assert (previous / "promloader" / "main.py").read_text() == "OLD = 1\n"
    assert (previous / "promloader" / "gone.py").exists()
    assert (db.data_dir() / "updates" / "pending-check").read_text() == "1.1.0"
    assert [b["reason"] for b in backup.list_backups()] == ["before-update"]
    assert products.list_products()["total"] == 1

    updater.confirm_started()
    assert not (db.data_dir() / "updates" / "pending-check").exists()


def test_broken_update_is_rejected(tmp_path):
    app = make_app(tmp_path)
    transport, _ = github(zip_bytes=make_zip(main="def broken(:\n"))
    with pytest.raises(updater.UpdateError, match="повреждена"):
        updater.update(transport, app)
    assert (app / "promloader" / "main.py").read_text() == "OLD = 1\n"
    assert (app / "VERSION").read_text() == "1.0.0"


def test_archive_without_program_is_rejected(tmp_path):
    app = make_app(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/readme.txt", "hi")
    transport, _ = github(zip_bytes=buf.getvalue())
    with pytest.raises(updater.UpdateError, match="нет программы"):
        updater.update(transport, app)


# ---------- резервные копии ----------

def test_backup_and_restore(tmp_path):
    from .conftest import make_image

    pid = products.create({"name": "Было", "price": 10})
    products.add_image_file(pid, make_image())
    b = backup.create("manual")
    assert b["reason_label"] == "вручную" and b["size"] > 0
    assert backup.due() is False

    products.update(pid, {"name": "Стало"})
    products.delete_image(pid, products.get(pid)["images"][0]["id"])
    backup.stage_restore(b["name"])

    data_dir = db.data_dir()
    db._conn.close()
    db._conn = None
    assert backup.apply_pending(data_dir)
    db.init(data_dir)
    p = products.get(pid)
    assert p["name"] == "Было" and len(p["images"]) == 1
    assert (data_dir / "uploads" / p["images"][0]["src"].split("/")[-1]).exists()
    assert any(x["reason"] == "before-restore" for x in backup.list_backups())
    assert not (data_dir / "restore-pending.zip").exists()


def test_backup_prune_and_validation():
    for i in range(backup.KEEP_DAILY + 3):
        (backup.backups_dir() / f"promloader-202601{i + 1:02d}-000000-daily.zip").write_bytes(b"x")
    (backup.backups_dir() / "promloader-20260100-000000-manual.zip").write_bytes(b"x")  # кривая дата не ломает список
    backup.prune()
    assert len([b for b in backup.list_backups() if b["reason"] == "daily"]) == backup.KEEP_DAILY
    with pytest.raises(backup.BackupError):
        backup.path_of("../../etc/passwd")
    with pytest.raises(backup.BackupError):
        backup.stage_restore_upload(b"not a zip")


def test_api(client):
    r = client.post("/api/backups").json()
    assert client.get(f"/api/backups/{r['name']}").status_code == 200
    assert client.get("/api/backups").json()["items"][0]["name"] == r["name"]
    # в тестах программа запущена не через launcher — перезапуск невозможен
    assert client.post(f"/api/backups/{r['name']}/restore").json() == {"restarting": False}
    meta = client.get("/api/meta").json()
    assert meta["version"] and "update_available" in meta
    s = client.post("/api/settings", json={"auto_update": False, "github_token": "github_pat_123456789"}).json()
    assert s["auto_update"] is False and s["github_token"].startswith("gith")
    assert client.post("/api/restart").status_code == 400


def test_failed_version_is_not_reinstalled(tmp_path):
    app = make_app(tmp_path)
    (db.data_dir() / "updates").mkdir(parents=True, exist_ok=True)
    (db.data_dir() / "updates" / "skip-version").write_text("1.1.0")
    info = updater.check(github(version="1.1.0")[0], app)
    assert info["available"] is False and "не запустилась" in info["message"]
    with pytest.raises(updater.UpdateError):
        updater.update(github(version="1.1.0")[0], app)
    assert updater.check(github(version="1.1.1")[0], app)["available"] is True  # следующая версия — снова можно


def test_update_keeps_user_files_and_uses_manifest(tmp_path):
    app = make_app(tmp_path)
    (app / "прайс поставщика.xlsx").write_bytes(b"user file")
    (app / "promloader" / "old_module.py").write_text("x = 1\n")
    updater.update(github()[0], app)
    assert (app / "прайс поставщика.xlsx").exists()          # файл пользователя на месте
    assert not (app / "promloader" / "old_module.py").exists()  # устаревший код программы удалён
    manifest = json.loads((db.data_dir() / "updates" / "manifest.json").read_text())
    assert "promloader/added.py" in manifest and "START.bat" in manifest


def test_restore_rejects_foreign_files_and_keeps_update_source(tmp_path):
    import io
    import zipfile as zf

    from promloader import backup, db

    db.set_setting("update_repo", "owner/real")
    db.set_setting("shop_name", "Мій магазин")
    good = backup.create("manual")
    # подложенная копия: исполняемый файл и чужой репозиторий обновлений
    evil = io.BytesIO()
    with zf.ZipFile(backup.path_of(good["name"])) as src, zf.ZipFile(evil, "w") as z:
        for info in src.infolist():
            z.writestr(info, src.read(info))
        z.writestr("tools/cloudflared.exe", b"MZ")
    with pytest.raises(backup.BackupError, match="посторонние"):
        backup.stage_restore_upload(evil.getvalue())

    db.set_setting("update_repo", "attacker/evil")      # «копия» с чужим источником обновлений
    snap = backup.create("manual")
    db.set_setting("update_repo", "owner/real")
    backup.stage_restore(snap["name"])
    data_dir = db.data_dir()
    db._conn.close()
    db._conn = None
    assert backup.apply_pending(data_dir)
    db.init(data_dir)
    assert db.get_setting("update_repo") == "owner/real" and db.get_setting("shop_name") == "Мій магазин"
