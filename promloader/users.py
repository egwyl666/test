"""Пользователи программы: вход по логину и паролю, роли и права.

Раньше программа на самом компьютере открывалась без пароля, а с телефона — по одному общему паролю. Теперь у
каждого свой вход, и видно, кто что менял. После обновления (и в новой программе) пользователей ещё нет — первый,
кто откроет программу на компьютере, создаёт администратора; дальше администратор заводит остальных.

Пароли хранятся солёным PBKDF2-SHA256 (200 000 повторов), как пароль доступа с телефона раньше: md5 подбирается
за секунды на обычной видеокарте, а этот — нет, и устроен так же просто (только стандартная библиотека Python).

Права — по адресу запроса (`required`): новый обработчик, которого нет в таблице, изменять может только
администратор, смотреть — все. Так забытая строка в таблице закрывает лишнее, а не открывает.
"""

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from . import db

COOKIE = "pl_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
ITERATIONS = 200_000
RESET_MINUTES = 15
RESET_FILE = "СБРОС-ПАРОЛЯ.txt"
SERVICE_FILE = "service-token"
SERVICE_HEADER = "x-promloader-service"
LOGIN_RE = re.compile(r"^[\w.@-]{2,32}$")

# Права: что можно делать. Смотреть («view») может каждый вошедший; остальное — галочками в роли.
PERM_LABELS = {
    "view": "просмотр",
    "catalog": "товары, поставщики, цены и отправка на Prom",
    "ai": "ИИ-помощник (тратит запросы и деньги)",
    "orders": "заказы и Новая Почта",
    "chat": "ответы покупателям в чате",
    "admin": "настройки и пользователи (только администратор)",
}
PERM_HINTS = {
    "catalog": "Создавать и менять товары, импорт, поставщики, наценка, курс, категории, отправка и удаление на Prom",
    "ai": "Кнопки ✨ ИИ в карточке, массовый ИИ, «Заполнить по фото», черновик ответа в чате",
    "orders": "Менять статусы заказов, создавать и удалять ТТН",
    "chat": "Писать покупателям и отмечать сообщения прочитанными",
}
EDITABLE_PERMS = ("catalog", "ai", "orders", "chat")  # «admin» — только у роли «Администратор»
# Готовые роли — они же примеры, как может выглядеть своя. «Администратор» не меняется и не удаляется;
# остальные можно поправить или удалить (если ею никто не пользуется)
DEFAULT_ROLES = {
    "admin": ("Администратор", ("catalog", "ai", "orders", "chat", "admin")),
    "manager": ("Менеджер", ("catalog", "ai", "orders", "chat")),
    "operator": ("Оператор заказов", ("orders", "chat")),
    "viewer": ("Только просмотр", ()),
}

# (начало адреса, право на чтение GET, право на изменение POST/PUT/PATCH/DELETE). Первое совпадение — главное,
# поэтому частные адреса стоят выше общих (/api/np/settings выше /api/np). None — достаточно войти.
RULES = [
    # ИИ — отдельное право. Адреса с номером товара — регулярным выражением (начинается с «^»)
    (r"^/api/products/\d+/ai$", "view", "ai"),
    (r"^/api/chat/rooms/[^/]+/ai-draft$", "view", "ai"),
    ("/api/ai/bulk", "view", ("catalog", "ai")),
    (r"^/api/orders/\d+/remind$", "view", ("orders", "chat")),  # напоминание о посылке — это сообщение в чат  # массовый ИИ меняет товары — нужны оба права
    ("/api/me", None, None),
    ("/api/roles", "admin", "admin"),
    ("/api/push", None, None),           # уведомления на свой телефон — у каждого свои
    ("/api/users", "admin", "admin"),
    ("/api/settings", "admin", "admin"),
    ("/api/backups", "admin", "admin"),
    ("/api/migration", "admin", "admin"),
    ("/api/pause", "admin", "admin"),
    ("/api/update", "admin", "admin"),
    ("/api/phone", "admin", "admin"),
    ("/api/restart", "admin", "admin"),
    ("/api/shutdown", "admin", "admin"),
    ("/api/autostart", "admin", "admin"),
    ("/api/telegram", "admin", "admin"),
    ("/api/r2", "admin", "admin"),
    ("/api/support", "admin", "admin"),
    ("/api/np/settings", "admin", "admin"),
    ("/api/np/check", "admin", "admin"),
    ("/api/prom/autosync", "view", "admin"),
    ("/api/ai/model", "view", "admin"),   # выбор модели и проверка ключа — настройка, она общая на всех
    ("/api/ai/check", "view", "admin"),
    ("/api/ai/models/check", "view", "admin"),
    ("/api/orders", "view", "orders"),
    ("/api/chat", "view", "chat"),
    ("/api/np", "view", "orders"),
    ("/api/meta", "view", "view"),
    ("/api/reports", "view", "view"),
    ("/api/products", "view", "catalog"),
    ("/api/suppliers", "view", "catalog"),
    ("/api/pricing", "view", "catalog"),
    ("/api/rates", "view", "catalog"),
    ("/api/import", "catalog", "catalog"),
    ("/api/sync", "view", "catalog"),
    ("/api/ai", "view", "ai"),
    ("/api/categories", "view", "catalog"),
    ("/api/changes", "view", "catalog"),
    ("/api/diagnose", "view", "catalog"),
    ("/api/schedules", "view", "catalog"),
    ("/api/prom", "view", "catalog"),
    ("/settings", "admin", "admin"),
    ("/support", "admin", "admin"),
    ("/import", "catalog", "catalog"),
    ("/docs", "admin", "admin"),
    ("/redoc", "admin", "admin"),
    ("/openapi.json", "admin", "admin"),
]
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class UserError(Exception):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


# ---------- пароли ----------

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_hash(stored: str, password: str) -> bool:
    try:
        _, iterations, salt, digest = (stored or "").split("$")
        given = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(given.hex(), digest)


def _check_password(password, field: str = "password") -> str:
    password = "" if password is None else str(password)
    if len(password) < MIN_PASSWORD:
        raise UserError(f"Пароль слишком короткий — нужно не меньше {MIN_PASSWORD} символов", field)
    if len(password) > 200:
        raise UserError("Пароль слишком длинный", field)
    return password


def _check_login(login) -> str:
    login = str(login or "").strip().lower()
    if not LOGIN_RE.fullmatch(login):
        raise UserError("Логин — от 2 до 32 букв, цифр или знаков . _ @ - без пробелов", "login")
    return login


def _check_role(role) -> str:
    if role not in _roles():
        raise UserError("Неизвестная роль", "role")
    return role


# ---------- роли ----------

def _roles() -> dict:
    """{id: {id, label, perms, builtin}}. Готовые роли появляются при первом обращении (и после обновления)."""
    rows = db.query("SELECT * FROM roles ORDER BY position, id")
    if not rows:
        with db.tx() as c:
            for i, (key, (label, perms)) in enumerate(DEFAULT_ROLES.items()):
                c.execute("INSERT OR IGNORE INTO roles (id, label, perms, builtin, position) VALUES (?, ?, ?, 1, ?)",
                          (key, label, json.dumps(list(perms)), i))
        rows = db.query("SELECT * FROM roles ORDER BY position, id")
    out = {}
    for r in rows:
        perms = set(json.loads(r["perms"] or "[]")) & set(PERM_LABELS)
        if r["id"] == "admin":
            perms = set(PERM_LABELS)  # администратор может всё, что бы ни лежало в базе
        out[r["id"]] = {"id": r["id"], "label": r["label"], "perms": sorted(perms | {"view"}),
                        "builtin": bool(r["builtin"]), "locked": r["id"] == "admin"}
    return out


def roles() -> list[dict]:
    counts = {r["role"]: r["n"] for r in db.query("SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    return [{**r, "users": counts.get(k, 0)} for k, r in _roles().items()]


def _check_perms(perms) -> list[str]:
    if not isinstance(perms, list | tuple):
        raise UserError("Права — списком", "perms")
    bad = [p for p in perms if p not in EDITABLE_PERMS]
    if bad:
        raise UserError("Неизвестное право", "perms")
    return [p for p in EDITABLE_PERMS if p in perms]


def _check_label(label, c, except_id: str = "") -> str:
    label = str(label or "").strip()[:40]
    if not label:
        raise UserError("Впишите название роли", "label")
    if c.execute("SELECT 1 FROM roles WHERE lower_u(label) = lower_u(?) AND id != ?", (label, except_id)).fetchone():
        raise UserError("Роль с таким названием уже есть", "label")
    return label


def create_role(label, perms) -> dict:
    _roles()
    perms = _check_perms(perms)
    with db.tx() as c:
        label = _check_label(label, c)
        n = c.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM roles").fetchone()[0]
        key = f"r{secrets.token_hex(4)}"
        c.execute("INSERT INTO roles (id, label, perms, builtin, position) VALUES (?, ?, ?, 0, ?)",
                  (key, label, json.dumps(perms), n))
    return next(r for r in roles() if r["id"] == key)


def update_role(role_id: str, data: dict) -> dict:
    if role_id not in _roles():
        raise KeyError(role_id)
    if role_id == "admin":
        raise UserError("Роль «Администратор» не меняется: у неё все права")
    with db.tx() as c:
        if "label" in data:
            c.execute("UPDATE roles SET label = ? WHERE id = ?", (_check_label(data["label"], c, role_id), role_id))
        if "perms" in data:
            c.execute("UPDATE roles SET perms = ? WHERE id = ?", (json.dumps(_check_perms(data["perms"])), role_id))
    return next(r for r in roles() if r["id"] == role_id)


def delete_role(role_id: str) -> None:
    if role_id not in _roles():
        raise KeyError(role_id)
    if role_id == "admin":
        raise UserError("Роль «Администратор» удалить нельзя")
    with db.tx() as c:
        n = c.execute("SELECT COUNT(*) FROM users WHERE role = ?", (role_id,)).fetchone()[0]
        if n:
            raise UserError(f"Этой ролью пользуются ({n}) — сначала назначьте им другую")
        c.execute("DELETE FROM roles WHERE id = ?", (role_id,))


# ---------- пользователи ----------

def _public(row) -> dict | None:
    if row is None:
        return None
    role = _roles().get(row["role"]) or {"label": row["role"], "perms": ["view"]}
    return {"id": row["id"], "login": row["login"], "name": row["name"], "role": row["role"],
            "role_label": role["label"], "disabled": bool(row["disabled"]),
            "created_at": row["created_at"], "last_login_at": row["last_login_at"],
            "perms": role["perms"] if not row["disabled"] else []}


def any_users() -> bool:
    return db.query_one("SELECT 1 AS x FROM users LIMIT 1") is not None


def get(user_id: int) -> dict | None:
    return _public(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


def list_users() -> list[dict]:
    return [_public(r) for r in db.query("SELECT * FROM users ORDER BY role != 'admin', lower_u(login)")]


def _active_admins(c, exclude: int | None = None) -> int:
    return c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                     (exclude or 0,)).fetchone()[0]


def create(login, password, role: str = "viewer", name: str = "") -> dict:
    login = _check_login(login)
    password = _check_password(password)
    role = _check_role(role)
    name = str(name or "").strip()[:60]
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE login = ?", (login,)).fetchone():
            raise UserError("Пользователь с таким логином уже есть", "login")
        cur = c.execute("INSERT INTO users (login, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                        (login, name, role, hash_password(password), db.now()))
    return get(cur.lastrowid)


def setup_admin(login, password, name: str = "") -> dict:
    """Первый администратор. Только пока пользователей нет совсем — иначе это был бы способ стать админом без
    пароля. Проверка и запись — в одной транзакции: два одновременных запроса не создадут двух «первых»."""
    login = _check_login(login)
    password = _check_password(password)
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise UserError("Администратор уже создан — войдите под своим логином")
        cur = c.execute("INSERT INTO users (login, name, role, password_hash, created_at, last_login_at) "
                        "VALUES (?, ?, 'admin', ?, ?, ?)",
                        (login, str(name or "").strip()[:60], hash_password(password), db.now(), db.now()))
    return get(cur.lastrowid)


def update(user_id: int, data: dict, actor_id: int | None = None) -> dict:
    """Администратор меняет имя, роль, блокировку или задаёт новый пароль (если пользователь забыл свой).
    Последнего действующего администратора нельзя ни понизить, ни заблокировать — иначе в настройки не войти."""
    with db.tx() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        fields = {}
        if "name" in data:
            fields["name"] = str(data["name"] or "").strip()[:60]
        if "role" in data:
            fields["role"] = _check_role(data["role"])
        if "disabled" in data:
            fields["disabled"] = 1 if data["disabled"] else 0
            if fields["disabled"] and user_id == actor_id:
                raise UserError("Нельзя заблокировать самого себя")
        losing_admin = row["role"] == "admin" and not row["disabled"] and (
            fields.get("role", "admin") != "admin" or fields.get("disabled", 0))
        if losing_admin and not _active_admins(c, exclude=user_id):
            raise UserError("Это последний администратор — сначала назначьте администратором кого-то ещё", "role")
        if data.get("password"):
            fields["password_hash"] = hash_password(_check_password(data["password"]))
        if "password_hash" in fields or fields.get("disabled"):
            fields["session_version"] = row["session_version"] + 1  # прежние входы этого пользователя — всё
        if fields:
            c.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      (*fields.values(), user_id))
    return get(user_id)


def delete(user_id: int, actor_id: int | None = None) -> None:
    if user_id == actor_id:
        raise UserError("Нельзя удалить самого себя")
    with db.tx() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        if row["role"] == "admin" and not row["disabled"] and not _active_admins(c, exclude=user_id):
            raise UserError("Это последний администратор — его нельзя удалить")
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


def change_password(user_id: int, old: str, new: str) -> dict:
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if row is None:
        raise KeyError(user_id)
    if not verify_hash(row["password_hash"], str(old or "")):
        raise UserError("Текущий пароль указан неверно", "old")
    new = _check_password(new, "new")
    with db.tx() as c:
        c.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                  (hash_password(new), user_id))
    return get(user_id)


def authenticate(login, password) -> dict | None:
    row = db.query_one("SELECT * FROM users WHERE login = ?", (str(login or "").strip().lower(),))
    if row is None:
        verify_hash(hash_password("x"), str(password or ""))  # то же время ответа: не выдаём, есть ли такой логин
        return None
    if not verify_hash(row["password_hash"], str(password or "")) or row["disabled"]:
        return None
    with db.tx() as c:
        c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), row["id"]))
    return get(row["id"])


def can(user: dict | None, perm) -> bool:
    """perm — право, кортеж прав (нужны все) или None (достаточно войти)."""
    if user is None or user["disabled"]:
        return False
    if perm is None:
        return True
    return all(p in user["perms"] for p in ((perm,) if isinstance(perm, str) else perm))


def rule_matches(prefix: str, path: str) -> bool:
    if prefix.startswith("^"):
        return re.match(prefix, path) is not None
    return path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + ".")


def required(method: str, path: str):
    """Какое право нужно для запроса. Страницы (не /api) — только смотреть, кроме перечисленных в RULES."""
    for prefix, read, write in RULES:
        if rule_matches(prefix, path):
            return read if method in SAFE_METHODS else write
    if path.startswith("/api/"):
        return "view" if method in SAFE_METHODS else "admin"
    return "view"


# ---------- вход ----------

def _secret() -> bytes:
    value = db.get_setting("auth_secret")
    if not value:
        value = secrets.token_hex(32)
        db.set_setting("auth_secret", value)
    return bytes.fromhex(value)


def sign_out_everywhere() -> None:
    """Все входы всех пользователей перестают действовать (например, потерян телефон)."""
    db.set_setting("auth_secret", secrets.token_hex(32))


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def cookie_secure(request) -> bool:
    """Куку помечаем Secure, только когда браузер пришёл по https (Cloudflare, свой прокси): на http://localhost
    браузер такую куку просто выбросил бы, и войти на самом компьютере было бы нельзя."""
    return (request.url.scheme == "https" or "cf-connecting-ip" in request.headers
            or request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https")


def set_cookie(response, request, user_id: int) -> None:
    response.set_cookie(COOKIE, make_session(user_id), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=cookie_secure(request))


def make_session(user_id: int) -> str:
    row = db.query_one("SELECT session_version FROM users WHERE id = ?", (user_id,))
    payload = f"{user_id}.{row['session_version']}.{int(time.time()) + SESSION_DAYS * 86400}"
    return f"{payload}.{_sign(payload)}"


def session_user(token: str | None) -> dict | None:
    parts = (token or "").split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
        return None
    user_id, version, expires, sign = int(parts[0]), int(parts[1]), int(parts[2]), parts[3]
    if expires < time.time() or not hmac.compare_digest(sign, _sign(".".join(parts[:3]))):
        return None
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if row is None or row["disabled"] or row["session_version"] != version:
        return None
    return _public(row)


# ---------- «Забыли пароль?» ----------
# Код сброса программа кладёт файлом в папку с данными. Кто может открыть эту папку — тот и так может унести
# базу целиком, так что это честная граница: прочитать код может только хозяин компьютера (или сервера), а не
# человек с телефоном. Почты и SMS у программы пока нет.

def reset_file() -> Path:
    return db.data_dir() / RESET_FILE


RESET_TRIES = 5  # неверных кодов всего (со всех адресов) — и код сгорает: 10^6 вариантов так не перебрать


def _saved_reset() -> dict:
    try:
        saved = json.loads(db.get_setting("password_reset") or "{}")
    except ValueError:
        saved = {}
    return saved if isinstance(saved, dict) else {}


def burn_reset() -> None:
    """Код больше не действует: из базы и файл с диска."""
    with db.tx() as c:
        c.execute("DELETE FROM settings WHERE key = 'password_reset'")
    reset_file().unlink(missing_ok=True)


def cleanup_reset() -> bool:
    """Срок кода вышел — файл не должен лежать дальше (вызывается раз в несколько секунд из фона и при входе)."""
    saved = _saved_reset()
    if saved and saved.get("until", 0) < time.time() or not saved and reset_file().exists():
        burn_reset()
        return True
    return False


def request_reset() -> str:
    code = f"{secrets.randbelow(10 ** 6):06d}"
    db.set_setting("password_reset", json.dumps({"hash": hash_password(code), "until": time.time() + RESET_MINUTES * 60,
                                                 "fails": 0}))
    reset_file().write_text(
        "Prom Loader — сброс пароля\n\n"
        f"Код: {code}\n\n"
        f"Код действует {RESET_MINUTES} минут и только один раз. Введите его на странице входа в окне «Забыли пароль?».\n"
        "Если сброс просили не вы — просто удалите этот файл.\n", encoding="utf-8")
    return str(reset_file())


def reset_with_code(login, code, password, from_this_computer: bool = False) -> dict:
    """Новый пароль по коду из файла. Заблокированного администратором НЕ разблокирует — кроме администраторов и
    сброса на самом компьютере: иначе уволенный сотрудник, узнавший код, вернул бы себе доступ с телефона."""
    password = _check_password(password)
    saved = _saved_reset()
    if not saved or saved.get("until", 0) < time.time():
        burn_reset()
        raise UserError("Код устарел — нажмите «Получить код» ещё раз", "code")
    if not verify_hash(saved.get("hash", ""), str(code or "").strip()):
        fails = int(saved.get("fails", 0)) + 1
        if fails >= RESET_TRIES:
            burn_reset()
            raise UserError("Слишком много неверных кодов — этот код больше не действует. Нажмите «Получить код» ещё раз",
                            "code")
        db.set_setting("password_reset", json.dumps({**saved, "fails": fails}))
        raise UserError("Неверный код", "code")
    row = db.query_one("SELECT * FROM users WHERE login = ?", (str(login or "").strip().lower(),))
    if row is None:
        raise UserError("Нет пользователя с таким логином", "login")
    if row["disabled"] and row["role"] != "admin" and not from_this_computer:
        raise UserError("Этот пользователь заблокирован администратором — сброс пароля его не разблокирует. "
                        "Обратитесь к администратору", "login")
    with db.tx() as c:
        c.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1, disabled = 0 "
                  "WHERE id = ?", (hash_password(password), row["id"]))
    burn_reset()
    return get(row["id"])


# ---------- служебный вход значка у часов ----------

def service_token() -> str:
    """Значок у часов («Перезапустить», «Выключить») обращается к программе без браузера и без входа. Ключ — в файле
    в папке с данными: прочитать его может только тот, кто и так управляет программой на этом компьютере."""
    path = db.data_dir() / SERVICE_FILE
    try:
        value = path.read_text().strip()
    except OSError:
        value = ""
    if len(value) < 32:
        value = secrets.token_hex(32)
        path.write_text(value)
    return value


def service_ok(request) -> bool:
    given = request.headers.get(SERVICE_HEADER, "")
    return bool(given) and request.url.path in ("/api/restart", "/api/shutdown") \
        and secrets.compare_digest(given.encode(), service_token().encode())
