"""Доступ с телефона: пароль, вход, домашняя сеть и туннель Cloudflare.

Главное — кто пришёл, решает настоящий адрес клиента, а не заголовок Host: в домашней сети любой может прислать
«Host: localhost», а через cloudflared запросы приходят с 127.0.0.1."""

import pytest
from fastapi.testclient import TestClient

from promloader import backup, notify, phone, runtime
from promloader.main import app

LAN = ("192.168.1.20", 50000)
TUNNEL_HOST = "abc-def.trycloudflare.com"
PASSWORD = "секрет-1234"


@pytest.fixture(autouse=True)
def clean_limiter():
    phone.limiter.reset()
    yield
    phone.limiter.reset()


def lan_client(monkeypatch, enabled=True, **kwargs):
    monkeypatch.setattr(runtime, "lan_bound", True)  # программа слушает домашнюю сеть (как после START.bat)
    phone.update({"password": PASSWORD, "lan": enabled})
    return TestClient(app, base_url="http://192.168.1.5:8000", client=LAN, **kwargs)


def tunnel_client(monkeypatch, remote=True):
    monkeypatch.setattr(phone, "remote_host", lambda: TUNNEL_HOST)
    phone.update({"password": PASSWORD, "remote": remote})
    # cloudflared подключается к программе с этого же компьютера и добавляет адрес телефона в Cf-Connecting-Ip
    return TestClient(app, base_url=f"https://{TUNNEL_HOST}", client=("127.0.0.1", 40000),
                      headers={"cf-connecting-ip": "203.0.113.7"})


def login(c, password=PASSWORD, next_="/"):
    return c.post("/api/login", json={"password": password, "next": next_})


def test_password_rules():
    with pytest.raises(phone.PhoneError, match="не меньше 8"):
        phone.update({"password": "1234567"})
    with pytest.raises(phone.PhoneError, match="Сначала придумайте пароль"):
        phone.update({"lan": True})
    assert phone.update({"password": PASSWORD, "lan": True}) is True  # Wi‑Fi включён — нужен перезапуск
    assert phone.check_password(PASSWORD) and not phone.check_password("секрет-12345")
    assert PASSWORD not in phone.db.get_setting("phone_password_hash")
    assert phone.update({"remote": True}) is False  # пароль уже есть
    assert {"phone_password_hash", "phone_secret", "phone_lan"} <= set(backup.DEVICE_SETTINGS)


def test_lan_disabled_or_not_bound_is_refused(monkeypatch):
    c = lan_client(monkeypatch, enabled=False)
    assert c.get("/api/meta").status_code == 403
    # подделанный Host не превращает чужой адрес в «этот компьютер»
    assert c.get("/api/meta", headers={"host": "localhost"}).status_code == 403
    # фото и фид, как и раньше, открыты (их забирает Prom)
    assert c.get("/media/" + "0" * 32 + ".jpg").status_code == 404


def test_lan_login_flow(monkeypatch):
    c = lan_client(monkeypatch, follow_redirects=False)
    r = c.get("/orders?status=new")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Forders%3Fstatus%3Dnew"
    assert c.get("/login").status_code == 200 and c.get("/static/app.css").status_code == 200
    r = c.get("/api/meta", headers={"host": "localhost"})  # Host не помогает
    assert r.status_code == 401 and r.json()["login"] is True

    assert login(c, "неверный-пароль").status_code == 400
    r = login(c, next_="//evil.example/x")
    assert r.status_code == 200 and r.json()["next"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    meta = c.get("/api/meta")
    assert meta.status_code == 200 and meta.json()["access"] == "phone"
    assert c.get("/").status_code == 200

    # пароль и сам доступ меняются только на компьютере; выключить программу с телефона нельзя
    assert c.get("/api/phone").status_code == 403
    assert c.post("/api/phone", json={"lan": False}).status_code == 403
    assert c.post("/api/shutdown").status_code == 403
    # запрос с чужой страницы отклоняется и после входа
    assert c.post("/api/logout", headers={"origin": "http://evil.example"}).status_code == 403

    # новый пароль на компьютере — все прежние входы недействительны
    phone.update({"password": "новый-пароль-99"})
    assert c.get("/api/meta").status_code == 401


def test_logout(monkeypatch):
    c = lan_client(monkeypatch)
    login(c)
    assert c.get("/api/meta").status_code == 200
    c.post("/api/logout")
    assert c.get("/api/meta").status_code == 401


def test_sign_out_everywhere(monkeypatch):
    c = lan_client(monkeypatch)
    login(c)
    computer = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 40000))
    assert computer.get("/api/meta").json()["access"] == "local"  # сам компьютер — без пароля и при включённом Wi‑Fi
    assert computer.post("/api/phone/sign-out-all").json()["ok"]
    assert c.get("/api/meta").status_code == 401


def test_too_many_failed_logins(monkeypatch):
    c = lan_client(monkeypatch)
    for _ in range(phone.LOGIN_FAILS_PER_CLIENT):
        assert login(c, "не-тот-пароль").status_code == 400
    r = login(c)  # даже верный пароль — после 10 неудач с этого адреса
    assert r.status_code == 429 and "15 минут" in r.json()["detail"]


def test_tunnel_requires_login_and_secure_cookie(monkeypatch):
    c = tunnel_client(monkeypatch)
    assert c.get("/api/products").status_code == 401
    r = login(c)
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
    assert c.get("/api/products").status_code == 200
    assert c.post("/api/shutdown").status_code == 403  # через туннель тоже «с 127.0.0.1», но это не компьютер


def test_tunnel_refused_when_off_or_other_host(monkeypatch):
    c = tunnel_client(monkeypatch, remote=False)
    assert c.get("/login").status_code == 403
    phone.update({"remote": True})
    # через Cloudflare, но по другому адресу (например, туннель фото или свой домен) — интерфейса нет
    assert c.get("/login", headers={"host": "other.trycloudflare.com"}).status_code == 403
    assert c.get("/login").status_code == 200


def test_this_computer_needs_no_password(client):
    phone.update({"password": PASSWORD, "lan": True, "remote": True})
    assert client.get("/api/meta").json()["access"] == "local"
    st = client.get("/api/phone").json()
    assert st["password_set"] and st["lan"] and st["remote"] and not st["lan_active"]  # не через START.bat
    r = client.post("/api/phone", json={"lan": False})
    assert r.status_code == 200 and r.json()["need_restart"] is True and r.json()["restarting"] is False
    assert client.post("/api/phone", json={"password": "123"}).status_code == 400
    svg = client.get("/api/phone/qr.svg", params={"text": "http://192.168.1.5:8000/"})
    assert svg.status_code == 200 and svg.text.startswith("<svg")


def test_keep_tunnel_notifies_new_address_once(monkeypatch):
    sent, state = [], {"active": False, "closed": 0}
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append((event, text)))
    monkeypatch.setattr(phone.tunnel, "status", lambda: {"active": state["active"], "url": phone.tunnel.url, "error": ""})

    def ensure():
        phone.tunnel.url = f"https://{TUNNEL_HOST}"
        phone.tunnel.reachable = True
        state["active"] = True
        return phone.tunnel.url
    monkeypatch.setattr(phone.tunnel, "ensure_url", ensure)
    monkeypatch.setattr(phone.tunnel, "close", lambda: state.update(closed=state["closed"] + 1))
    monkeypatch.setitem(phone._state, "notified", "")
    phone.update({"password": PASSWORD, "remote": True})

    assert phone.keep_tunnel() == f"https://{TUNNEL_HOST}"
    assert phone.keep_tunnel() == f"https://{TUNNEL_HOST}"
    assert len(sent) == 1 and sent[0][0] == "phone" and TUNNEL_HOST in sent[0][1]

    phone.update({"remote": False})
    monkeypatch.setattr(phone.tunnel, "proc", object())
    assert phone.keep_tunnel() == "" and state["closed"] == 1


def test_safe_next():
    assert phone.safe_next("/orders?x=1") == "/orders?x=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "", None):
        assert phone.safe_next(bad) == "/"


def test_unreachable_address_is_not_sent(monkeypatch):
    sent, closed = [], []
    monkeypatch.setattr(notify, "send", lambda event, text, background=True: sent.append(text))
    monkeypatch.setattr(phone.tunnel, "status", lambda: {"active": False, "url": "", "error": phone.tunnel.error})

    def ensure():
        phone.tunnel.reachable = False  # Cloudflare выдал адрес, но он не открывается
        return f"https://{TUNNEL_HOST}"
    monkeypatch.setattr(phone.tunnel, "ensure_url", ensure)
    monkeypatch.setattr(phone.tunnel, "close", lambda: closed.append(1))
    monkeypatch.setitem(phone._state, "next_try", 0.0)
    phone.update({"password": PASSWORD, "remote": True})
    assert phone.keep_tunnel() == "" and sent == [] and closed == [1]
    assert "не открывается" in phone.status()["remote_error"]
    assert phone.keep_tunnel() == "" and closed == [1]  # следующая попытка — через пару минут, не сразу
    phone.tunnel.error = ""
