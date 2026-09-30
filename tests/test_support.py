import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from promloader import db, notify, support

from .conftest import make_image


@pytest.fixture
def bot(monkeypatch):
    sent = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        sent.append((request.url.path.split("/")[1], method, request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw:
                        real(token, method, httpx.MockTransport(handler), files, **kw))
    return sent


def test_ticket_saved_locally_without_channel():
    t = support.create("Не загружается прайс", "@shop", "/supplier?id=1", [("экран.png", make_image())],
                       client={"js_errors": ["TypeError: x is null"], "ua": "Chrome"})
    assert t["status"] == "saved" and t["files"][0]["name"] == "экран.png"
    assert (support.support_dir() / str(t["id"]) / "01.png").exists()
    diag = json.loads(db.query_one("SELECT diagnostics FROM support_tickets WHERE id = ?", (t["id"],))["diagnostics"])
    assert diag["version"] and "prom_token_set" in diag["settings"] and diag["client"]["ua"] == "Chrome"
    assert not any("token" in k and k != "prom_token_set" for k in diag["settings"])  # секреты не уходят
    z = zipfile.ZipFile(io.BytesIO(support.archive(t["id"])))
    assert set(z.namelist()) >= {"обращение.txt", "техническая-информация.json"} and len(z.namelist()) == 3
    assert "TypeError" in z.read("обращение.txt").decode()
    with pytest.raises(support.SupportError, match="не настроена"):
        support.send(t["id"])


def test_validation():
    with pytest.raises(support.SupportError):
        support.create("", files=[])
    with pytest.raises(support.SupportError):
        support.create("x", files=[("virus.exe", b"MZ")])
    with pytest.raises(support.SupportError):
        support.create("x", files=[(f"{i}.png", b"1") for i in range(11)])


def test_send_to_developer_bot(bot):
    db.set_setting("support_token", "999:SUPPORT")
    db.set_setting("support_chat_id", "12345")
    db.set_setting("telegram_token", "111:SHOP")  # бот магазина не используется для поддержки
    t = support.create("Ошибка при выгрузке", files=[("a.png", make_image()), ("лог.txt", b"log")])
    t = support.send(t["id"])
    assert t["status"] == "sent"
    assert {b for b, _, _ in bot} == {"bot999:SUPPORT"}
    assert [m for _, m, _ in bot] == ["sendMessage", "sendPhoto", "sendDocument", "sendDocument"]


def test_retry_after_network_error(monkeypatch):
    db.set_setting("support_token", "999:S")
    db.set_setting("support_chat_id", "1")
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda *a, **k: (_ for _ in ()).throw(notify.NotifyError("Нет связи")))
    t = support.create("проблема")
    with pytest.raises(support.SupportError, match="сохранено"):
        support.send(t["id"])
    assert support.get(t["id"])["status"] == "send_failed"
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw:
                        real(token, method, httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})), files, **kw))
    assert support.send_pending() == 1 and support.get(t["id"])["status"] == "sent"


def test_support_json_file(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "APP_DIR", tmp_path)
    (tmp_path / "support.json").write_text(json.dumps({"token": "777:FILE", "chat_id": 42}))
    assert support.channel() == {"token": "777:FILE", "chat_id": "42"} and support.configured()


def test_api(client):
    r = client.post("/api/support", data={"description": "Всё сломалось", "page": "/import", "client": "{}"},
                    files=[("files", ("s.png", make_image(), "image/png"))])
    body = r.json()
    assert r.status_code == 200 and "скачайте архив" in body["message"]
    tid = body["ticket"]["id"]
    assert client.get(f"/api/support/{tid}/archive").headers["content-type"] == "application/zip"
    assert client.get("/api/support").json()["items"][0]["id"] == tid
    assert "log_tail" in client.get("/api/support/diagnostics").json()
    assert client.post("/api/support", data={"description": ""}).status_code == 400
    assert client.get("/support").status_code == 200
    assert client.delete(f"/api/support/{tid}").json() == {"ok": True}


def test_channel_settings_api(client, monkeypatch):
    monkeypatch.setattr(support, "APP_DIR", Path("/nonexistent"))
    s = client.post("/api/settings", json={"support_token": "123456:SECRET-TOKEN", "support_chat_id": "42"}).json()
    assert "SECRET" not in s["support_token"] and s["support_chat_id"] == "42" and support.configured()
    assert client.post("/api/settings", json={"support_chat_id": "abc"}).status_code == 400
    s = client.post("/api/settings", json={"support_token_clear": True, "support_chat_id": ""}).json()
    assert s["support_token"] == "" and not support.configured()
    # «найти чат» без сохранённого токена — понятная ошибка, а не 500
    r = client.post("/api/support/channel/chats", json={"token": ""})
    assert r.status_code == 400 and "токен" in r.json()["detail"]


def test_broken_picture_goes_as_document_and_no_double_send(monkeypatch):
    db.set_setting("support_token", "999:S")
    db.set_setting("support_chat_id", "1")
    calls = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "sendPhoto":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: IMAGE_PROCESS_FAILED"})
        return httpx.Response(200, json={"ok": True})
    real = notify.call
    monkeypatch.setattr(notify, "call", lambda token, method, transport=None, files=None, **kw:
                        real(token, method, httpx.MockTransport(handler), files, **kw))
    t = support.create("битый скриншот", files=[("s.png", b"not really a png")])
    assert support.send(t["id"])["status"] == "sent"
    assert calls == ["sendMessage", "sendPhoto", "sendDocument", "sendDocument"]
    support.send(t["id"])  # уже отправлено — повторно не шлём
    assert len(calls) == 4
