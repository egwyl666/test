"""Пользователи 2.23.1: края и жизнь — права отняли посреди работы, выход на всех устройствах, кука через
Cloudflare, сервер с APP_PASSWORD, переезд, перебор кода сброса, «кто менял» у фоновых заданий."""

import io
import time

import pytest
from fastapi.testclient import TestClient

from promloader import aibulk, backup, changes, db, migration, phone, products, suppliers, users
from promloader.main import app

from .conftest import ADMIN_LOGIN, ADMIN_PASSWORD, sign_in
from .test_suppliers import BASE, make_supplier, make_yml

TUNNEL = "abc-def.trycloudflare.com"


@pytest.fixture(autouse=True)
def clean_limiters():
    from promloader import main
    from promloader.routes import accounts

    for lim in (phone.limiter, accounts.reset_limiter, main.basic_limiter):
        lim.reset()
    yield
    for lim in (phone.limiter, accounts.reset_limiter, main.basic_limiter):
        lim.reset()


def other(login, role, name=""):
    """Второй человек в своём браузере."""
    c = sign_in(TestClient(app, follow_redirects=False), login, "пароль-1234", role)
    if name:
        users.update(db.query_one("SELECT id FROM users WHERE login = ?", (login,))["id"], {"name": name})
    return c


def tunnel_client(ip="203.0.113.7"):
    """Телефон через Cloudflare: cloudflared подключается с 127.0.0.1 и сообщает адрес телефона."""
    db.set_setting("phone_remote", "1")
    return TestClient(app, base_url=f"https://{TUNNEL}", client=("127.0.0.1", 40000),
                      headers={"cf-connecting-ip": ip}, follow_redirects=False)


def user_id(login):
    return db.query_one("SELECT id FROM users WHERE login = ?", (login,))["id"]


# ---------- 1. права отняли, пока человек работает ----------

def test_permissions_revoked_mid_session_apply_to_the_very_next_request(client):
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    pid = products.create({"name": "Товар", "price": 100, "external_id": "D-1"})
    a = other("anna", "manager")
    assert a.patch(f"/api/products/{pid}", json={"price": 110}).status_code == 200
    # администратор убрал у роли «Менеджер» ИИ — следующий же запрос ИИ отклонён, без нового входа
    assert client.patch("/api/roles/manager", json={"perms": ["catalog", "orders", "chat"]}).status_code == 200
    r = a.post(f"/api/products/{pid}/ai", json={"action": "keywords"})
    assert r.status_code == 403 and r.json()["forbidden"]
    assert a.post("/api/ai/bulk", json={"ids": [pid], "action": "keywords"}).status_code == 403
    assert a.patch(f"/api/products/{pid}", json={"price": 120}).status_code == 200  # товары — по-прежнему можно
    # убрал и товары
    client.patch("/api/roles/manager", json={"perms": ["orders"]})
    assert a.patch(f"/api/products/{pid}", json={"price": 130}).status_code == 403
    # перевёл человека в «Только просмотр» — заказы тоже закрыты
    client.patch(f"/api/users/{user_id('anna')}", json={"role": "viewer"})
    assert a.post("/api/orders/seen", json={"ids": []}).status_code == 403
    assert a.get("/api/products").status_code == 200  # смотреть можно, сессия жива
    me = a.get("/api/me").json()
    assert me["role"] == "viewer" and me["perms"] == ["view"]
    assert products.get(pid)["price"] == 120


# ---------- 2. «Выйти на всех устройствах» ----------

def test_sign_out_everywhere_keeps_only_the_admin_who_pressed_it(client):
    b = other("bohdan", "manager")
    version_before = db.query_one("SELECT session_version FROM users WHERE login = 'bohdan'")["session_version"]
    r = client.post("/api/phone/sign-out-all")
    assert r.status_code == 200 and users.COOKIE in r.headers["set-cookie"]
    assert client.get("/api/meta").status_code == 200  # нажавший — с новой кукой
    # у второго версия входа та же, но подпись старым ключом — отказ
    assert db.query_one("SELECT session_version FROM users WHERE login = 'bohdan'")["session_version"] == version_before
    assert b.get("/api/meta").status_code == 401
    assert b.post("/api/login", json={"login": "bohdan", "password": "пароль-1234"}).status_code == 200
    assert b.get("/api/meta").status_code == 200


# ---------- 3. кука через Cloudflare, ссылка «первого запуска» ----------

def secure(set_cookie: str) -> bool:
    return "secure" in [p.strip().lower() for p in set_cookie.split(";")]


def test_cookie_secure_only_over_https(anon):
    users.create("admin", "пароль-адміна", "admin")
    local = anon.post("/api/login", json={"login": "admin", "password": "пароль-адміна"})
    assert local.status_code == 200 and not secure(local.headers["set-cookie"])  # http://localhost — без Secure
    proxied = anon.post("/api/login", json={"login": "admin", "password": "пароль-адміна"},
                        headers={"x-forwarded-proto": "https"})
    assert secure(proxied.headers["set-cookie"])
    phone_c = tunnel_client()
    r = phone_c.post("/api/login", json={"login": "admin", "password": "пароль-адміна"})
    assert r.status_code == 200 and secure(r.headers["set-cookie"])
    assert phone_c.get("/api/meta").json()["access"] == "phone"
    # через Cloudflare адрес после перезапуска другой: кука старого адреса браузером туда не уйдёт — снова вход
    fresh = TestClient(app, base_url="https://new-one.trycloudflare.com", client=("127.0.0.1", 40000),
                       headers={"cf-connecting-ip": "203.0.113.7"}, follow_redirects=False)
    assert fresh.get("/orders").headers["location"].startswith("/login")


def test_welcome_link_survives_first_admin_setup(anon):
    anon.follow_redirects = False
    r = anon.get("/?welcome=1")
    assert r.status_code == 303 and r.headers["location"] == "/setup?next=%2F%3Fwelcome%3D1"
    assert anon.get("/setup?welcome=1").status_code == 200
    r = anon.post("/api/setup", json={"login": "admin", "password": "пароль-адміна", "password2": "пароль-адміна",
                                      "next": "/?welcome=1"})
    assert r.json()["next"] == "/?welcome=1"
    assert anon.get("/?welcome=1").status_code == 200
    assert anon.get("/setup?welcome=1").headers["location"] == "/login?welcome=1"
    # и ярлык первого запуска (START.bat открывает /settings?welcome=1) тоже ведёт обратно
    first = TestClient(app, follow_redirects=False)
    assert first.get("/settings?welcome=1").headers["location"] == "/login?next=%2Fsettings%3Fwelcome%3D1"


# ---------- 4. сервер (Docker) с APP_PASSWORD ----------

def test_app_password_then_user_session(monkeypatch, data_dir):
    monkeypatch.setenv("APP_PASSWORD", "пароль-сервера")
    bridge = ("172.17.0.1", 5000)  # контейнер: запросы не с 127.0.0.1
    c = TestClient(app, client=bridge, follow_redirects=False)
    r = c.get("/api/meta")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    basic = ("admin", "пароль-сервера")
    r = c.get("/", auth=basic)
    assert r.status_code == 303 and r.headers["location"].startswith("/setup")  # пароль сервера — ещё не вход
    assert c.get("/setup", auth=basic).status_code == 200
    r = c.post("/api/setup", auth=basic, json={"login": "boss", "password": "пароль-боса", "password2": "пароль-боса"})
    assert r.status_code == 200  # за паролем сервера администратора можно создать и не с 127.0.0.1
    assert c.get("/api/meta", auth=basic).status_code == 200
    assert c.get("/api/meta").status_code == 401  # кука есть, а пароля сервера нет — не пускаем
    stranger = TestClient(app, client=("172.17.0.2", 5000))
    r = stranger.get("/api/meta", auth=basic)
    assert r.status_code == 401 and r.json()["login"] and not r.json()["setup"]


# ---------- 5. переезд с пользователями и ролями ----------

def test_migration_carries_users_roles_auth_secret_and_journal(tmp_path, data_dir):
    pid = products.create({"name": "Товар", "price": 100, "external_id": "MG-1"})
    users.create("admin", "пароль-адміна", "admin")
    role = users.create_role("Контент без ИИ", ["catalog"])
    users.create("kate", "пароль-кейт", role["id"], "Катя")
    with changes.user("Катя"):
        products.update(pid, {"price": 150})
    token = users.make_session(user_id("kate"))
    secret = db.get_setting("auth_secret")
    bundle = migration.create_bundle("пароль-переїзду-42")
    data = backup.path_of(bundle["name"]).read_bytes()
    assert secret.encode() not in data  # ключ подписи входа — только зашифрованным

    machine_b = tmp_path / "B"
    machine_b.mkdir()
    db.init(machine_b)
    migration.receive(io.BytesIO(data))
    migration.apply("пароль-переїзду-42")
    db._conn.close()
    db._conn = None
    assert backup.apply_pending(machine_b)
    db.init(machine_b)

    assert [u["login"] for u in users.list_users()] == ["admin", "kate"]
    assert users.authenticate("kate", "пароль-кейт")["role_label"] == "Контент без ИИ"
    assert next(r for r in users.roles() if r["id"] == role["id"])["perms"] == ["catalog", "view"]
    assert db.get_setting("auth_secret") == secret
    assert users.session_user(token)["login"] == "kate"  # вход, выданный на старом компьютере, подписан тем же ключом
    row = db.query_one("SELECT user, new FROM product_changes WHERE product_id = ? AND field = 'price'", (pid,))
    assert row["user"] == "Катя" and row["new"] == "150"


# ---------- 6. перебор кода сброса ----------

def read_code(data_dir):
    return (data_dir / users.RESET_FILE).read_text(encoding="utf-8").split("Код: ")[1].split()[0]


def reset(c, code, login="admin", password="новий-пароль-1"):
    return c.post("/api/password/reset", json={"login": login, "code": code, "password": password, "password2": password})


def test_reset_code_burns_after_five_wrong_guesses_from_any_addresses(anon, data_dir):
    users.create("admin", "забутий-пароль", "admin")
    assert anon.post("/api/password/reset-code").status_code == 200
    code = read_code(data_dir)
    wrong = "000000" if code != "000000" else "111111"
    for n in range(users.RESET_TRIES):  # каждый — со своего адреса: защита «на адрес» тут не помогла бы
        r = reset(tunnel_client(f"198.51.100.{n + 1}"), wrong)
        assert r.status_code == 400
    assert "больше не действует" in r.json()["detail"]
    assert not (data_dir / users.RESET_FILE).exists()
    r = reset(anon, code)  # верный код — но он уже сгорел
    assert r.status_code == 400 and "устарел" in r.json()["detail"]
    assert users.authenticate("admin", "забутий-пароль")


def test_reset_code_file_removed_when_expired(anon, data_dir, monkeypatch):
    users.create("admin", "забутий-пароль", "admin")
    anon.post("/api/password/reset-code")
    assert (data_dir / users.RESET_FILE).exists() and not users.cleanup_reset()
    real = time.time
    monkeypatch.setattr(users.time, "time", lambda: real() + users.RESET_MINUTES * 60 + 1)
    assert users.cleanup_reset()  # фоновая проверка (каждые несколько секунд) убирает файл
    assert not (data_dir / users.RESET_FILE).exists() and not db.get_setting("password_reset")


def test_blocked_user_cannot_unblock_self_remotely(anon, data_dir):
    users.create("admin", "пароль-адміна", "admin")
    fired = users.create("fired", "пароль-звільн", "operator")
    users.update(fired["id"], {"disabled": True})
    anon.post("/api/password/reset-code")
    r = reset(tunnel_client(), read_code(data_dir), login="fired")
    assert r.status_code == 400 and "заблокирован" in r.json()["detail"]
    assert users.get(fired["id"])["disabled"] and not users.authenticate("fired", "новий-пароль-1")
    # на самом компьютере — можно (это хозяин): разблокирует и задаёт пароль
    computer = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 40000))
    assert reset(computer, read_code(data_dir), login="fired").status_code == 200
    assert not users.get(fired["id"])["disabled"]


def test_blocked_admin_can_be_reset_remotely(anon, data_dir):
    users.create("boss", "пароль-боса", "admin")
    second = users.create("admin2", "пароль-адм2", "admin")
    users.update(second["id"], {"disabled": True})
    anon.post("/api/password/reset-code")
    assert reset(tunnel_client(), read_code(data_dir), login="admin2").status_code == 200
    assert not users.get(second["id"])["disabled"]


# ---------- 7. «кто менял» у фоновой работы ----------

def wait_supplier(c, sid):
    for _ in range(100):
        s = c.get(f"/api/suppliers/{sid}").json()
        if not s["running"] and s["runs"] and s["runs"][0]["status"] != "running":
            return s
        time.sleep(0.05)
    raise AssertionError("поставщик не обновился")


def test_manual_supplier_run_is_attributed_scheduled_is_not(client):
    sid = make_supplier(make_yml(BASE))
    a = other("olena", "manager", "Олена")
    assert a.post(f"/api/suppliers/{sid}/run", json={}).json() == {"started": True}
    assert wait_supplier(a, sid)["runs"][0]["status"] == "ok"
    who = {r["user"] for r in db.query("SELECT user FROM product_changes WHERE field = 'created'")}
    assert who == {"Олена"}
    suppliers.store_source(sid, make_yml([("A1", "Кружка белая", "999", "10", "true")]), "feed.xml")
    suppliers.run(sid, "schedule")  # по расписанию — без имени
    row = db.query_one("SELECT user FROM product_changes WHERE field = 'cost_price' ORDER BY id DESC LIMIT 1")
    assert row is not None and row["user"] == ""


def test_bulk_ai_job_remembers_who_started_it(client):
    db.set_setting("ai_provider", "gemini")
    db.set_setting("gemini_key", "k")
    ids = [products.create({"name": f"Товар {n}", "price": 100, "external_id": f"BA-{n}"}) for n in range(2)]
    a = other("ivan", "manager", "Іван")
    r = a.post("/api/ai/bulk", json={"ids": ids, "action": "keywords"})
    assert r.status_code == 200
    assert db.query_one("SELECT user FROM ai_jobs WHERE id = ?", (r.json()["id"],))["user"] == "Іван"
    # задание выполняет фон (не запрос) — но правки подписаны тем, кто его запустил
    while aibulk.process_one(lambda p, action, instruction="": {"keywords": "ключ"}) != "idle":
        pass
    rows = db.query("SELECT user FROM product_changes WHERE field = 'keywords'")
    assert [r["user"] for r in rows] == ["Іван", "Іван"]
    # задание, созданное без человека (автоконвейер по расписанию), — без имени
    job = aibulk.create(ids, "name")
    assert db.query_one("SELECT user FROM ai_jobs WHERE id = ?", (job["id"],))["user"] == ""
    assert changes.current_user() == ""  # ничего не «прилипло» к потоку тестов


def test_user_name_does_not_leak_between_requests(client):
    pid = products.create({"name": "Товар", "price": 100, "external_id": "LK-1"})
    a = other("u-a", "manager", "Перший")
    b = other("u-b", "manager", "Другий")
    for n, c in enumerate([a, b, a, client, b]):
        assert c.patch(f"/api/products/{pid}", json={"price": 200 + n}).status_code == 200
    rows = [r["user"] for r in db.query(
        "SELECT user FROM product_changes WHERE product_id = ? AND field = 'price' ORDER BY id", (pid,))]
    assert rows == ["Перший", "Другий", "Перший", ADMIN_LOGIN, "Другий"]
    products.update(pid, {"price": 300})  # не из запроса
    assert db.query_one("SELECT user FROM product_changes ORDER BY id DESC LIMIT 1")["user"] == ""
    assert ADMIN_PASSWORD  # (пароль администратора из conftest — для ясности, чей это вход)
