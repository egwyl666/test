"""Доступ с телефона: дома по Wi‑Fi и из любого места через Cloudflare, вход по паролю.

Кто пришёл, решаем по настоящему адресу клиента, а не по заголовку Host: как только программа слушает домашнюю
сеть, любой в этой сети может прислать «Host: localhost». Поэтому:
- запрос с этого компьютера (127.0.0.1) без заголовка Cloudflare — свой, пускаем без пароля, как раньше;
- запрос, пришедший через наш туннель (с компьютера, но с заголовком Cf-Connecting-Ip и адресом туннеля), и
  запрос из домашней сети — «телефон»: нужен вход по паролю;
- всё остальное отклоняется.
Вход даёт подписанную куку на 30 дней. Смена пароля и «Выйти на всех телефонах» меняют ключ подписи — все
прежние входы перестают действовать.
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import socket
import threading
import time
from urllib.parse import urlparse

from . import db, notify, phototunnel, runtime

log = logging.getLogger("promloader.phone")

COOKIE = "pl_phone"
SESSION_DAYS = 30
MIN_PASSWORD = 8
ITERATIONS = 200_000
LOGIN_WINDOW = 15 * 60       # окно подсчёта неудачных попыток входа, секунд
LOGIN_FAILS_PER_CLIENT = 10
LOGIN_FAILS_TOTAL = 30       # на всех вместе: перебор с разных адресов тоже упирается в предел
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico", "/manifest.webmanifest", "/sw.js"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
SETTINGS = ("phone_lan", "phone_remote", "phone_password_hash", "phone_secret")


class PhoneError(Exception):
    pass


# ---------- настройки и пароль ----------

def password_set() -> bool:
    return bool(db.get_setting("phone_password_hash"))


def lan_enabled() -> bool:
    return db.get_setting("phone_lan") == "1" and password_set()


def remote_enabled() -> bool:
    return db.get_setting("phone_remote") == "1" and password_set()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str) -> bool:
    stored = db.get_setting("phone_password_hash")
    try:
        _, iterations, salt, digest = stored.split("$")
        given = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(given.hex(), digest)


def _secret() -> bytes:
    value = db.get_setting("phone_secret")
    if not value:
        value = secrets.token_hex(32)
        db.set_setting("phone_secret", value)
    return bytes.fromhex(value)


def sign_out_everywhere() -> None:
    db.set_setting("phone_secret", secrets.token_hex(32))


def update(data: dict) -> bool:
    """Сохранить настройки доступа. Возвращает True, если поменялся доступ по Wi‑Fi (нужен перезапуск)."""
    password = data.get("password")
    if password is not None:
        password = str(password)
        if len(password) < MIN_PASSWORD:
            raise PhoneError(f"Пароль слишком короткий — нужно не меньше {MIN_PASSWORD} символов")
    has_password = password is not None or password_set()
    for key in ("lan", "remote"):
        if data.get(key) and not has_password:
            raise PhoneError("Сначала придумайте пароль для входа с телефона")
    lan_before = lan_enabled()
    with db.tx():
        if password is not None:
            db.set_setting("phone_password_hash", hash_password(password))
            sign_out_everywhere()  # старый пароль больше не должен открывать уже вошедшие телефоны
        for key in ("lan", "remote"):
            if key in data:
                db.set_setting(f"phone_{key}", "1" if data[key] else "0")
    return lan_enabled() != lan_before


# ---------- вход ----------

def make_session() -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    sign = hmac.new(_secret(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sign}"


def valid_session(token: str | None) -> bool:
    if not token or not password_set():
        return False
    expires, _, sign = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    good = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sign, good)


class LoginLimiter:
    """Неудачные попытки входа: не больше per_client с одного адреса и total со всех за window секунд.
    Когда адрес упирается в предел, владельцу уходит тревога в Telegram — кто-то подбирает пароль."""

    def __init__(self, per_client: int = LOGIN_FAILS_PER_CLIENT, total: int = LOGIN_FAILS_TOTAL,
                 window: int = LOGIN_WINDOW, what: str = "входа с телефона"):
        self.lock = threading.Lock()
        self.fails: dict[str, list[float]] = {}
        self.per_client, self.total, self.window, self.what = per_client, total, window, what

    def _recent(self, key: str, now: float) -> list[float]:
        items = [t for t in self.fails.get(key, []) if now - t < self.window]
        self.fails[key] = items
        return items

    def blocked(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            return len(self._recent(client, now)) >= self.per_client or len(self._recent("*", now)) >= self.total

    def failed(self, client: str) -> None:
        now = time.monotonic()
        with self.lock:
            for key in (client, "*"):
                self._recent(key, now).append(now)
            just_blocked = len(self._recent(client, now)) == self.per_client
        if just_blocked:
            minutes = self.window // 60
            log.warning("Подбор пароля %s с адреса %s — вход закрыт на %d мин", self.what, client, minutes)
            notify.send("phone", f"🔐 Prom Loader: {self.per_client} неверных паролей {self.what} подряд с адреса "
                                 f"{client}. Вход с этого адреса закрыт на {minutes} мин. Если это не вы — смените пароль.")

    def reset(self) -> None:
        with self.lock:
            self.fails.clear()


limiter = LoginLimiter()


# ---------- кто пришёл ----------

def _host_name(value: str) -> str:
    """'localhost:8765' -> 'localhost'; '[::1]:80' -> '[::1]'."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def client_ip(request) -> str:
    return request.client.host if request.client else ""


def from_this_computer(request) -> bool:
    """Запрос с самого компьютера, а не через туннель Cloudflare (cloudflared тоже подключается с 127.0.0.1)."""
    ip = client_ip(request)
    return (ip in LOOPBACK or ip.startswith("127.")) and "cf-connecting-ip" not in request.headers


def client_key(request) -> str:
    """Чей это вход — для подсчёта неудачных попыток. Через туннель настоящий адрес сообщает Cloudflare."""
    return request.headers.get("cf-connecting-ip") or client_ip(request)


def client_kind(request, allowed_hosts: set[str]) -> str | None:
    """'local' — свой компьютер; 'phone' — нужен вход по паролю; None — не пускать."""
    ip = client_ip(request)
    host = _host_name(request.headers.get("host", ""))
    loopback = ip in LOOPBACK or ip.startswith("127.")
    if loopback and "cf-connecting-ip" not in request.headers:
        return "local" if host in allowed_hosts else None
    if loopback:  # пришло через cloudflared: только наш туннель для телефона и только когда он включён
        tunnel_host = remote_host()
        if not remote_enabled() or not tunnel_host:
            return None
        # cloudflared передаёт адрес, по которому открыли туннель; если он его заменит адресом программы — тоже наш
        return "phone" if host in (tunnel_host, "localhost", "127.0.0.1") else None
    if runtime.lan_bound:  # программа слушает домашнюю сеть: заголовку Host здесь верить нельзя
        return "phone" if lan_enabled() else None
    # программа запущена не через START.bat (Docker, свой uvicorn): как раньше, по адресу в Host
    return "local" if host in allowed_hosts else None


def safe_next(value: str | None) -> str:
    """Куда вернуться после входа: только страница этой же программы."""
    value = value or "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


# ---------- адреса ----------

def lan_addresses() -> list[str]:
    """Адреса компьютера в домашней сети. Первый — тот, через который компьютер выходит в интернет (UDP-«подключение»
    ничего не отправляет, только выбирает сетевую карту)."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    result = []
    for ip in found:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private and not addr.is_loopback and not addr.is_link_local and ip not in result:
            result.append(ip)
    return result


def lan_urls() -> list[str]:
    if not runtime.port:
        return []
    return [f"http://{ip}:{runtime.port}/" for ip in lan_addresses()]


def qr_svg(text: str) -> str:
    """QR-код ссылки: навести камеру телефона — и адрес открылся, без набора руками."""
    import io

    import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, dark="#111", light="#fff")
    return buf.getvalue().decode()


# ---------- туннель для телефона ----------

class PhoneTunnel(phototunnel.Tunnel):
    """Тот же cloudflared, что и для фото, но к самой программе и без закрытия по простою."""
    label = "Программа доступна с телефона"
    start_error = "Cloudflare не выдал адрес для телефона. Программа попробует ещё раз через пару минут"
    probe = ("/static/icon.png", 200)

    def _ensure_server(self) -> int:
        if not runtime.port:
            raise phototunnel.TunnelError("Доступ из интернета работает, когда программа запущена ярлыком «Prom Loader» (START.bat)")
        return runtime.port


tunnel = PhoneTunnel()
RETRY_SECONDS = 120
HEALTH_SECONDS = 180   # как часто проверять, что адрес Cloudflare действительно открывается
HEALTH_FAILS = 2       # столько неудачных проверок подряд — и адрес пересоздаётся
_state = {"next_try": 0.0, "notified": "", "checked": float("-inf"), "fails": 0}


def remote_host() -> str:
    url = tunnel.url if tunnel.proc is not None else ""
    return (urlparse(url).hostname or "").lower() if url else ""


def remote_url() -> str:
    return tunnel.status()["url"]


def keep_tunnel() -> str:
    """Шаг фонового воркера: держит туннель открытым, пока включён доступ из интернета. Блокирует (cloudflared)."""
    if not remote_enabled():
        if tunnel.proc is not None:
            log.info("Доступ с телефона через интернет выключен")
            tunnel.close()
        return ""
    if tunnel.status()["active"]:
        # cloudflared может жить, но потерять связь с Cloudflare (сон компьютера, смена сети) — тогда по адресу
        # «Ошибка 1033». Процесс при этом не падает, и без проверки программа считала бы адрес рабочим вечно
        if time.monotonic() - _state["checked"] < HEALTH_SECONDS:
            return tunnel.url
        _state["checked"] = time.monotonic()
        if tunnel.check_reachable():
            _state["fails"] = 0
            return tunnel.url
        _state["fails"] += 1
        if _state["fails"] < HEALTH_FAILS:
            return tunnel.url
        log.warning("Адрес для телефона %s больше не открывается — создаю новый", tunnel.url)
        _state["fails"] = 0
        tunnel.close()
    if time.monotonic() < _state["next_try"]:
        return ""
    try:
        url = tunnel.ensure_url()
        if not tunnel.reachable:
            # адрес выдан, но не открывается: не рассылаем его и пробуем позже (иначе в Telegram шли бы адреса подряд)
            tunnel.close()
            raise phototunnel.TunnelError("Cloudflare выдал адрес, но он не открывается. Программа попробует ещё раз через пару минут")
    except phototunnel.TunnelError as exc:
        tunnel.error = str(exc)
        _state["next_try"] = time.monotonic() + RETRY_SECONDS
        log.warning("Доступ с телефона: %s", exc)
        return ""
    _state["next_try"] = 0.0
    if url != _state["notified"]:
        _state["notified"] = url
        # у бесплатного туннеля адрес новый после каждого запуска — пусть он сам придёт в Telegram
        notify.send("phone", f"📱 Prom Loader открывается с телефона по новому адресу:\n{url}\nВход — по паролю из «Настроек».")
    return url


def status() -> dict:
    remote = tunnel.status()
    return {
        "password_set": password_set(),
        "lan": db.get_setting("phone_lan") == "1",
        "remote": db.get_setting("phone_remote") == "1",
        "lan_active": runtime.lan_bound and lan_enabled(),
        "lan_urls": lan_urls() if runtime.lan_bound else [],
        "remote_active": remote["active"],
        "remote_url": remote["url"],
        "remote_error": remote["error"] if remote_enabled() else "",
        "launcher": runtime.can_restart(),
        "port": runtime.port,
        "app_password": bool(os.environ.get("APP_PASSWORD")),
    }
