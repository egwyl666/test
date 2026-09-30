from promloader import tray


def make(codes, pending=False, rollback_ok=True, deps=True):
    codes = list(codes)
    seen = {"spawn": [], "rollback": 0, "notes": []}
    state = {"pending": pending}

    def spawn(restarted):
        seen["spawn"].append(restarted)
        return codes.pop(0)

    def rollback():
        seen["rollback"] += 1
        state["pending"] = False
        return rollback_ok

    sup = tray.Supervisor(spawn, lambda: deps, rollback, lambda: state["pending"], seen["notes"].append)
    return sup, seen


def test_restart_after_update():
    sup, seen = make([3, 3, 0])
    assert sup.run() == "ok"
    assert seen["spawn"] == [False, True, True]


def test_rollback_when_new_version_fails():
    sup, seen = make([1, 0], pending=True)
    assert sup.run() == "ok"
    assert seen["rollback"] == 1 and seen["spawn"] == [False, True]
    assert "предыдущую" in seen["notes"][0]


def test_error_without_pending_update_stops():
    sup, seen = make([1])
    assert sup.run() == "error" and seen["rollback"] == 0


def test_stop_from_menu():
    sup, _ = make([0])
    sup.stopping = True
    assert sup.run() == "stopped"


def test_deps_failure():
    sup, seen = make([0], deps=False)
    assert sup.run() == "deps_failed" and seen["spawn"] == []


def test_needs_deps_and_rollback(tmp_path):
    app, venv, data = tmp_path / "app", tmp_path / "venv", tmp_path / "data"
    app.mkdir(); venv.mkdir()
    (app / "requirements.txt").write_text("a\n")
    assert tray.needs_deps(app, venv)
    (venv / "installed-requirements.txt").write_text("a\n")
    assert not tray.needs_deps(app, venv)

    (data / "updates" / "previous" / "promloader").mkdir(parents=True)
    (data / "updates" / "previous" / "promloader" / "main.py").write_text("OLD")
    (data / "updates" / "pending-check").write_text("1.2.0")
    (app / "promloader").mkdir()
    (app / "promloader" / "main.py").write_text("BROKEN")
    assert tray.pending_update(data)
    assert tray.rollback(app, data)
    assert (app / "promloader" / "main.py").read_text() == "OLD"
    assert not tray.pending_update(data)
    assert (data / "updates" / "skip-version").read_text() == "1.2.0"
    assert not tray.rollback(app, tmp_path / "nothing")


def test_shutdown_only_from_this_computer(client):
    # TestClient приходит с адреса «testclient» — как чужой компьютер в сети: выключать нельзя
    assert client.post("/api/shutdown").status_code == 403


def test_tray_requests_use_app_password(monkeypatch, tmp_path):
    import httpx
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "post", lambda url, timeout, auth: calls.append((url, auth)) or Resp())
    monkeypatch.setenv("APP_PASSWORD", "secret")
    app = tray.App.__new__(tray.App)
    app.data = tmp_path
    (tmp_path / "port").write_text("8001")
    app._post("/api/restart")
    assert calls == [("http://127.0.0.1:8001/api/restart", ("admin", "secret"))]
