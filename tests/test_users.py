"""Пользователи и роли (2.23.0): первый запуск, обновление со старой версии, права, сброс пароля."""

import pytest
from fastapi.testclient import TestClient

from promloader import backup, changes, db, phone, products, runtime, users
from promloader.main import all_routes, app

from .conftest import sign_in

LAN = ("192.168.1.20", 50000)


@pytest.fixture(autouse=True)
def clean_limiters():
    from promloader.routes import accounts

    phone.limiter.reset()
    accounts.reset_limiter.reset()
    yield
    phone.limiter.reset()
    accounts.reset_limiter.reset()


def fresh():
    return TestClient(app, follow_redirects=False)


def make_old_install(data_dir):
    """База, как у тех, кто пользовался программой до 2.23.0: товары, поставщик, заказ, журнал, токены, пароль
    телефона — и ни одного пользователя (таблицы users и колонки журнала «кто» ещё нет)."""
    with db.tx() as c:
        c.execute("INSERT INTO suppliers (name, url, created_at, updated_at) VALUES ('Склад', 'http://example.com/p.xml', ?, ?)",
                  (db.now(), db.now()))
    p1 = products.create({"name": "Кросівки", "price": 1500, "external_id": "OLD-1"})
    products.create({"name": "Рюкзак", "price": 700, "external_id": "OLD-2"})
    products.update(p1, {"price": 1600})
    for key, value in {"prom_token": "old-prom-token", "telegram_token": "123:ABC", "shop_name": "Мій магазин",
                       "phone_lan": "1", "phone_password_hash": "pbkdf2_sha256$1$00$00", "gemini_key": "AIza-old"}.items():
        db.set_setting(key, value)
    with db.tx() as c:
        c.execute("DROP TABLE users")
        c.execute("ALTER TABLE product_changes DROP COLUMN user")
    return snapshot()


def snapshot():
    return {
        "products": [dict(r) for r in db.query("SELECT * FROM products ORDER BY id")],
        "suppliers": [dict(r) for r in db.query("SELECT * FROM suppliers ORDER BY id")],
        "settings": {r["key"]: r["value"] for r in db.query("SELECT * FROM settings") if r["key"] != "auth_secret"},
        "changes": [(r["id"], r["field"], r["old"], r["new"], r["source"]) for r in db.query(
            "SELECT * FROM product_changes ORDER BY id")],
    }


# ---------- обновление со старой версии ----------

def test_update_from_old_version_asks_for_admin_password_and_keeps_data(data_dir):
    before = make_old_install(data_dir)
    db.init(data_dir)  # программа запустилась уже новой версией
    assert "user" in {r["name"] for r in db.query("PRAGMA table_info(product_changes)")}
    with fresh() as c:
        # любая страница ведёт на создание администратора, а потом — туда, куда шли
        r = c.get("/orders?status=new")
        assert r.status_code == 303 and r.headers["location"] == "/setup?next=%2Forders%3Fstatus%3Dnew"
        assert c.get("/login").headers["location"] == "/setup"
        assert c.get("/setup").status_code == 200
        api = c.get("/api/products")
        assert api.status_code == 401 and api.json()["setup"] is True
        assert c.post("/api/setup", json={"login": "admin", "password": "корот", "password2": "корот"}).status_code == 400
        assert c.post("/api/setup", json={"login": "admin", "password": "надійний-1", "password2": "інший-1"}).status_code == 400
        r = c.post("/api/setup", json={"login": "Admin", "name": "Олег", "password": "надійний-1",
                                       "password2": "надійний-1", "next": "/orders"})
        assert r.status_code == 200 and r.json()["next"] == "/orders" and r.json()["user"]["role"] == "admin"
        # сразу вошли: товары на месте, ничего не стёрлось
        items = c.get("/api/products").json()["items"]
        assert {i["external_id"] for i in items} == {"OLD-1", "OLD-2"}
        assert c.get("/api/meta").json()["me"]["login"] == "admin"
        # второго «первого администратора» не будет
        assert c.post("/api/setup", json={"login": "x2", "password": "надійний-2", "password2": "надійний-2"}).status_code == 400
        assert c.get("/setup").headers["location"] == "/login"
    after = snapshot()
    assert after["products"] == before["products"]
    assert after["suppliers"] == before["suppliers"]
    assert after["changes"] == before["changes"]  # журнал цел; у старых записей «кто» пусто
    assert {k: v for k, v in after["settings"].items() if k in before["settings"]} == before["settings"]
    assert db.query_one("SELECT COUNT(*) AS n FROM product_changes WHERE user != ''")["n"] == 0


def test_admin_is_created_only_on_this_computer(data_dir, monkeypatch):
    make_old_install(data_dir)
    db.init(data_dir)
    monkeypatch.setattr(runtime, "lan_bound", True)  # доступ по Wi‑Fi был включён до обновления
    lan = TestClient(app, base_url="http://192.168.1.5:8000", client=LAN, follow_redirects=False)
    # без пользователей телефон не пускаем вовсе: иначе любой в сети стал бы администратором первым
    r = lan.get("/")
    assert r.status_code == 403 and "создайте пароль администратора" in r.text
    assert lan.post("/api/setup", json={"login": "evil", "password": "надійний-1", "password2": "надійний-1"}).status_code == 403
    assert not users.any_users()
    with TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 40000)) as c:  # сам компьютер
        assert c.post("/api/setup", json={"login": "admin", "password": "надійний-1", "password2": "надійний-1"}).status_code == 200
    # теперь телефон — на вход тем же логином
    r = lan.get("/orders")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert lan.post("/api/login", json={"login": "admin", "password": "надійний-1"}).status_code == 200
    assert lan.get("/api/meta").json()["access"] == "phone"


def test_restoring_old_backup_keeps_current_users(data_dir):
    products.create({"name": "З копії", "price": 100, "external_id": "B-1"})
    name = backup.create("manual")["name"]  # копия «старой версии»: в ней ещё нет пользователей
    users.create("anna", "пароль-анни", "admin")
    products.create({"name": "Після копії", "price": 100, "external_id": "B-2"})
    with open(backup.path_of(name), "rb") as f:
        backup.stage_restore_stream(f)
    with TestClient(app):  # при запуске программа применяет восстановление
        assert products.find_by_external_id("B-1") and not products.find_by_external_id("B-2")
    assert [u["login"] for u in users.list_users()] == ["anna"]
    assert users.authenticate("anna", "пароль-анни")


# ---------- роли ----------

def as_role(role):
    if not users.any_users():
        users.create("boss", "admin-pass-1", "admin")
    return sign_in(TestClient(app, follow_redirects=False), f"u-{role}", "пароль-1234", role)


def test_viewer_sees_but_changes_nothing(data_dir):
    pid = products.create({"name": "Товар", "price": 100, "external_id": "V-1"})
    c = as_role("viewer")
    assert c.get("/api/products").status_code == 200
    assert c.get("/api/orders").status_code == 200 and c.get("/api/reports/sales").status_code == 200
    r = c.patch(f"/api/products/{pid}", json={"price": 1})
    assert r.status_code == 403 and r.json()["forbidden"] and "Только просмотр" in r.json()["detail"]
    assert c.post("/api/orders/seen", json={"ids": [1]}).status_code == 403
    assert c.get("/api/settings").status_code == 403
    page = c.get("/settings")
    assert page.status_code == 403 and "text/html" in page.headers["content-type"]
    assert c.get("/").status_code == 200 and c.get("/orders").status_code == 200
    me = c.get("/api/meta").json()["me"]
    assert me["role"] == "viewer" and me["perms"] == ["view"]
    assert products.get(pid)["price"] == 100
    assert c.post("/api/logout").status_code == 200  # выйти может любая роль (раньше — 403, и кука оставалась)
    assert c.get("/api/meta").status_code == 401


def test_operator_works_with_orders_only(data_dir):
    pid = products.create({"name": "Товар", "price": 100, "external_id": "O-1"})
    c = as_role("operator")
    assert c.post("/api/orders/seen", json={"ids": []}).status_code == 200
    assert c.patch(f"/api/products/{pid}", json={"price": 1}).status_code == 403
    assert c.post("/api/sync", json={"ids": [pid]}).status_code == 403
    assert c.get("/import").status_code == 403
    assert c.post("/api/np/settings", json={}).status_code == 403  # ключ Новой Почты — настройка администратора


def test_manager_edits_catalog_but_not_settings_and_journal_knows_who(data_dir):
    pid = products.create({"name": "Товар", "price": 100, "external_id": "M-1"})
    c = as_role("manager")
    users.update(users.list_users()[-1]["id"], {"name": "Марія"})
    assert c.patch(f"/api/products/{pid}", json={"price": 120}).status_code == 200
    assert c.get("/api/settings").status_code == 403 and c.get("/api/users").status_code == 403
    assert c.post("/api/update/install").status_code == 403 and c.post("/api/backups").status_code == 403
    assert c.post("/api/ai/model", json={"provider": "gemini", "model": ""}).status_code == 403
    row = db.query_one("SELECT * FROM product_changes WHERE product_id = ? AND field = 'price'", (pid,))
    assert row["user"] == "Марія" and row["source"] == "Вручную"
    hist = c.get(f"/api/products/{pid}/changes").json()
    assert any(h.get("user") == "Марія" for h in (hist["items"] if isinstance(hist, dict) else hist))
    # работа по расписанию (без входа) — без имени
    products.update(pid, {"price": 130})
    assert db.query_one("SELECT user FROM product_changes ORDER BY id DESC LIMIT 1")["user"] == ""
    assert changes._user.get() == ""


def test_every_api_route_has_a_rule():
    """Новый обработчик без строки в users.RULES менять может только администратор — это безопасно, но
    скорее всего ошибка (менеджер не сможет им пользоваться). Пусть тест напомнит добавить строку."""
    public = {"/api/login", "/api/logout", "/api/setup", "/api/password/reset-code", "/api/password/reset"}
    missing = []
    for r in all_routes():
        path = getattr(r, "path", "")
        if not path.startswith("/api/") or path in public:
            continue
        if not any(users.rule_matches(p, path) for p, _, _ in users.RULES):
            missing.append(path)
    assert missing == []


# ---------- пользователи ----------

def test_user_management_rules(client):
    admin = client.get("/api/me").json()
    r = client.post("/api/users", json={"login": "Олена", "name": "Олена", "role": "manager", "password": "пароль-олени"})
    assert r.status_code == 200 and r.json()["login"] == "олена"
    assert client.post("/api/users", json={"login": "ОЛЕНА", "role": "viewer", "password": "пароль-олени"}).json()["detail"] \
        == "Пользователь с таким логином уже есть"
    assert client.post("/api/users", json={"login": "a b", "password": "пароль-1234"}).json()["field"] == "login"
    assert client.post("/api/users", json={"login": "ok", "role": "god", "password": "пароль-1234"}).json()["field"] == "role"
    # последнего администратора не понизить, не заблокировать, не удалить; себя — тоже
    assert client.patch(f"/api/users/{admin['id']}", json={"role": "viewer"}).status_code == 400
    assert client.patch(f"/api/users/{admin['id']}", json={"disabled": True}).status_code == 400
    assert client.delete(f"/api/users/{admin['id']}").status_code == 400
    listing = client.get("/api/users").json()
    assert [u["login"] for u in listing["items"]] == ["admin", "олена"] and len(listing["roles"]) == 4
    assert "password_hash" not in str(listing)
    olena = listing["items"][1]
    assert client.patch(f"/api/users/{olena['id']}", json={"role": "admin"}).json()["role"] == "admin"
    assert client.patch(f"/api/users/{admin['id']}", json={"role": "manager"}).status_code == 200  # теперь админ не последний
    assert client.get("/api/users").status_code == 403  # права меняются сразу, без нового входа


def test_disabled_user_is_signed_out_and_cannot_sign_in(client):
    u = users.create("petro", "пароль-петра", "manager")
    other = sign_in(TestClient(app), "petro", "пароль-петра")
    assert other.get("/api/meta").status_code == 200
    client.patch(f"/api/users/{u['id']}", json={"disabled": True})
    assert other.get("/api/meta").status_code == 401
    assert other.post("/api/login", json={"login": "petro", "password": "пароль-петра"}).status_code == 400
    client.patch(f"/api/users/{u['id']}", json={"disabled": False})
    assert other.post("/api/login", json={"login": "PETRO", "password": "пароль-петра"}).status_code == 200


def test_admin_sets_new_password_for_forgetful_user(client):
    u = users.create("ira", "старий-пароль", "operator")
    phone_c = sign_in(TestClient(app), "ira", "старий-пароль")
    assert client.patch(f"/api/users/{u['id']}", json={"password": "новий-пароль"}).status_code == 200
    assert phone_c.get("/api/meta").status_code == 401  # старые входы закончились
    assert users.authenticate("ira", "старий-пароль") is None and users.authenticate("ira", "новий-пароль")


def test_change_own_password_keeps_this_session(client):
    other = sign_in(TestClient(app))  # тот же админ на другом устройстве
    bad = client.post("/api/me/password", json={"old": "не-той", "new": "новий-пароль-1", "new2": "новий-пароль-1"})
    assert bad.status_code == 400 and bad.json()["field"] == "old"
    assert client.post("/api/me/password", json={"old": "admin-password-1", "new": "новий-пароль-1",
                                                 "new2": "інший"}).status_code == 400
    r = client.post("/api/me/password", json={"old": "admin-password-1", "new": "новий-пароль-1", "new2": "новий-пароль-1"})
    assert r.status_code == 200
    assert client.get("/api/meta").status_code == 200   # здесь продолжаем работать
    assert other.get("/api/meta").status_code == 401    # на другом устройстве — войти заново
    assert users.authenticate("admin", "новий-пароль-1")
    assert "новий-пароль-1" not in str(db.query("SELECT * FROM users"))


def test_logout_and_wrong_password_limit(client):
    client.post("/api/logout")
    assert client.get("/api/meta").status_code == 401
    for _ in range(phone.LOGIN_FAILS_PER_CLIENT):
        assert client.post("/api/login", json={"login": "admin", "password": "не-той"}).json()["detail"] == "Неверный логин или пароль"
    r = client.post("/api/login", json={"login": "admin", "password": "admin-password-1"})
    assert r.status_code == 429


def test_tampered_or_expired_cookie_is_rejected(client, monkeypatch):
    token = client.cookies.get(users.COOKIE)
    uid, ver, exp, sign = token.split(".")
    c = TestClient(app)
    c.cookies.set(users.COOKIE, f"{int(uid) + 1}.{ver}.{exp}.{sign}")
    assert c.get("/api/meta").status_code == 401
    c.cookies.set(users.COOKIE, token)
    assert c.get("/api/meta").status_code == 200
    monkeypatch.setattr(users.time, "time", lambda: int(exp) + 1)
    assert c.get("/api/meta").status_code == 401


# ---------- «Забыли пароль?» ----------

def test_reset_password_with_code_from_data_folder(anon, data_dir):
    users.create("admin", "забутий-пароль", "admin")
    r = anon.post("/api/password/reset-code")
    assert r.status_code == 200 and r.json()["file"] == users.RESET_FILE
    text = (data_dir / users.RESET_FILE).read_text(encoding="utf-8")
    code = text.split("Код: ")[1].split()[0]
    assert len(code) == 6 and code not in db.get_setting("password_reset")  # в базе — только отпечаток кода
    wrong = anon.post("/api/password/reset", json={"login": "admin", "code": "000000" if code != "000000" else "111111",
                                                   "password": "новий-пароль-2", "password2": "новий-пароль-2"})
    assert wrong.status_code == 400 and wrong.json()["field"] == "code"
    ok = anon.post("/api/password/reset", json={"login": "admin", "code": code, "password": "новий-пароль-2",
                                                "password2": "новий-пароль-2"})
    assert ok.status_code == 200
    assert anon.get("/api/meta").status_code == 200  # сразу вошли
    assert not (data_dir / users.RESET_FILE).exists()
    again = anon.post("/api/password/reset", json={"login": "admin", "code": code, "password": "третій-пароль",
                                                   "password2": "третій-пароль"})
    assert again.status_code == 400  # код одноразовый
    assert users.authenticate("admin", "новий-пароль-2")


def test_reset_code_expires(anon, data_dir, monkeypatch):
    users.create("admin", "забутий-пароль", "admin")
    anon.post("/api/password/reset-code")
    code = (data_dir / users.RESET_FILE).read_text(encoding="utf-8").split("Код: ")[1].split()[0]
    real = users.time.time
    monkeypatch.setattr(users.time, "time", lambda: real() + users.RESET_MINUTES * 60 + 1)
    r = anon.post("/api/password/reset", json={"login": "admin", "code": code, "password": "новий-пароль-2",
                                               "password2": "новий-пароль-2"})
    assert r.status_code == 400 and "устарел" in r.json()["detail"]


# ---------- значок у часов ----------

def test_tray_service_key_only_for_restart_and_shutdown(anon, data_dir):
    users.create("admin", "пароль-адміна", "admin")
    key = (data_dir / users.SERVICE_FILE).read_text()
    assert len(key) >= 32
    assert anon.post("/api/restart").status_code == 401
    assert anon.post("/api/restart", headers={"X-PromLoader-Service": "x" * 64}).status_code == 401
    assert anon.post("/api/restart", headers={"X-PromLoader-Service": key}).status_code == 400  # не START.bat — но пущен
    assert anon.get("/api/settings", headers={"X-PromLoader-Service": key}).status_code == 401


# ---------- свои роли ----------

def test_default_roles_are_examples_and_custom_role_works(client):
    roles = {r["id"]: r for r in client.get("/api/users").json()["roles"]}
    assert set(roles) == {"admin", "manager", "operator", "viewer"}
    assert roles["admin"]["locked"] and "admin" in roles["admin"]["perms"]
    assert roles["manager"]["perms"] == ["ai", "catalog", "chat", "orders", "view"]
    assert client.patch("/api/roles/admin", json={"perms": []}).status_code == 400
    assert client.delete("/api/roles/admin").status_code == 400
    # «Контент без ИИ»: товары можно, ИИ нельзя
    r = client.post("/api/roles", json={"label": "Контент без ИИ", "perms": ["catalog"]})
    assert r.status_code == 200
    role = r.json()
    assert role["perms"] == ["catalog", "view"] and not role["builtin"]
    assert client.post("/api/roles", json={"label": "контент без ии", "perms": []}).json()["field"] == "label"
    assert client.post("/api/roles", json={"label": "X", "perms": ["admin"]}).status_code == 400  # админку — только админу
    pid = products.create({"name": "Товар", "price": 100, "external_id": "C-1"})
    c = sign_in(TestClient(app), "kate", "пароль-кейт", role["id"])
    assert c.get("/api/meta").json()["me"]["role_label"] == "Контент без ИИ"
    assert c.patch(f"/api/products/{pid}", json={"price": 150}).status_code == 200
    denied = c.post(f"/api/products/{pid}/ai", json={"action": "keywords"})
    assert denied.status_code == 403 and "ИИ-помощник" in denied.json()["detail"]
    assert c.post("/api/ai/bulk", json={"ids": [pid], "action": "keywords"}).status_code == 403
    # администратор дал ИИ — сразу работает (запрос уже доходит до ИИ; ключа нет — это 400, а не 403)
    client.patch(f"/api/roles/{role['id']}", json={"perms": ["catalog", "ai"]})
    assert c.post(f"/api/products/{pid}/ai", json={"action": "keywords"}).status_code != 403
    # роль с пользователями удалить нельзя; без них — можно
    assert client.delete(f"/api/roles/{role['id']}").status_code == 400
    client.patch(f"/api/users/{users.list_users()[-1]['id']}", json={"role": "viewer"})
    assert client.delete(f"/api/roles/{role['id']}").status_code == 200


def test_ai_is_a_separate_permission_for_builtin_roles(data_dir):
    pid = products.create({"name": "Товар", "price": 100, "external_id": "A-1"})
    op = as_role("operator")
    assert op.post(f"/api/products/{pid}/ai", json={"action": "keywords"}).status_code == 403
    assert op.post("/api/chat/rooms/r1/ai-draft").status_code == 403
    assert op.get("/api/ai/status").status_code == 200  # смотреть, что подключено, можно
    users.update_role("operator", {"perms": ["orders", "chat", "ai"]})
    assert op.post("/api/chat/rooms/r1/ai-draft").status_code != 403
