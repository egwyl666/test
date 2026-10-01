"""Фото с компьютера для Prom: временный публичный адрес только для загруженных фото.

Prom не принимает фото файлами — он скачивает их по ссылкам из YML. Программа на домашнем компьютере
из интернета не видна, поэтому на время отправки поднимается:
- крошечный отдельный веб-сервер, который отдаёт ТОЛЬКО файлы фото (имена — случайные 32 hex-символа,
  списка файлов нет, остальная программа через него недоступна);
- бесплатный туннель Cloudflare (cloudflared, «quick tunnel») с адресом вида https://xxx.trycloudflare.com.
Через IDLE_MINUTES после последней отправки туннель закрывается.
"""

import logging
import os
import platform
import re
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from . import config, db

log = logging.getLogger("promloader.phototunnel")

IDLE_MINUTES = 30
START_TIMEOUT = 60
FILE_RE = re.compile(r"^/media/([0-9a-f]{32}\.(?:jpg|png|gif))$")
FEED_RE = re.compile(r"^/feed/([0-9a-f]{32}\.xml)$")  # файл выгрузки для импорта Prom по ссылке
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DOWNLOADS = {
    ("Windows", "AMD64"): "cloudflared-windows-amd64.exe",
    ("Windows", "x86"): "cloudflared-windows-386.exe",
    ("Windows", "ARM64"): "cloudflared-windows-amd64.exe",
    ("Linux", "x86_64"): "cloudflared-linux-amd64",
    ("Linux", "aarch64"): "cloudflared-linux-arm64",
}
RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "xml": "text/xml; charset=utf-8"}


class TunnelError(Exception):
    pass


def enabled() -> bool:
    """Туннель нужен, только если фото не идут через R2, нет постоянного адреса и пользователь его не выключил."""
    from . import r2

    return not r2.active() and not config.public_base_url() and config.get("photo_tunnel") != "0"


def feeds_dir() -> Path:
    path = db.data_dir() / "feeds"
    path.mkdir(exist_ok=True)
    return path


# ---------- сервер только для фото (и файлов выгрузки) ----------

class _PhotoHandler(BaseHTTPRequestHandler):
    uploads: Path = Path(".")
    feeds: Path = Path(".")

    def _file(self) -> Path | None:
        url = self.path.split("?")[0]
        m, folder = FILE_RE.match(url), self.uploads
        if not m:
            m, folder = FEED_RE.match(url), self.feeds
        if not m:
            return None
        path = folder / m.group(1)
        return path if path.is_file() else None

    def _send(self, body: bool) -> None:
        path = self._file()
        if path is None:
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[path.suffix[1:]])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._send(True)

    def do_HEAD(self):  # noqa: N802
        self._send(False)

    def log_message(self, *args):
        pass


class Tunnel:
    def __init__(self):
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self.last_used = 0.0
        self.error = ""
        self.reach_timeout = 30

    # --- сервер фото ---
    def _ensure_server(self) -> int:
        if self.server is None:
            handler = type("Handler", (_PhotoHandler,), {"uploads": db.uploads_dir(), "feeds": feeds_dir()})
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    # --- cloudflared ---
    def _binary(self, transport=None) -> Path:
        override = os.environ.get("PROMLOADER_CLOUDFLARED")
        if override:
            return Path(override)
        name = DOWNLOADS.get((platform.system(), platform.machine()))
        if not name:
            raise TunnelError("Для этой системы нет готового cloudflared — укажите публичный адрес сайта в «Настройках»")
        path = db.data_dir() / "tools" / ("cloudflared.exe" if name.endswith(".exe") else "cloudflared")
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Скачиваю cloudflared…")
        try:
            with httpx.Client(follow_redirects=True, timeout=300, transport=transport) as client:
                r = client.get(RELEASE + name)
        except httpx.HTTPError as exc:
            raise TunnelError(f"Не удалось скачать cloudflared: {exc}")
        if r.status_code >= 400 or len(r.content) < 1_000_000:
            raise TunnelError(f"Не удалось скачать cloudflared (HTTP {r.status_code})")
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tmp.replace(path)
        return path

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and bool(self.url)

    def ensure_url(self, transport=None) -> str:
        """Публичный адрес для фото (поднимает туннель, если нужно). Блокирует до START_TIMEOUT секунд."""
        with self.lock:
            self.last_used = time.monotonic()
            if self._alive():
                return self.url
            self._stop_proc()
            port = self._ensure_server()
            binary = self._binary(transport)
            try:
                self.proc = subprocess.Popen(
                    [str(binary), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                    creationflags=NO_WINDOW,
                )
            except OSError as exc:
                raise TunnelError(f"Не удалось запустить cloudflared: {exc}")
            found = threading.Event()

            def read():
                for line in self.proc.stdout:
                    m = URL_RE.search(line)
                    if m and not found.is_set():
                        self.url = m.group(0)
                        found.set()

            threading.Thread(target=read, daemon=True).start()
            if not found.wait(START_TIMEOUT):
                self._stop_proc()
                self.error = "Cloudflare не выдал адрес вовремя"
                raise TunnelError("Не удалось открыть временный доступ к фото (Cloudflare не ответил). Повторим позже")
            self._wait_reachable(self.reach_timeout)
            self.error = ""
            log.info("Фото доступны Prom по адресу %s", self.url)
            return self.url

    def _wait_reachable(self, seconds: int = 30) -> None:
        """Новому адресу Cloudflare нужно несколько секунд, чтобы заработать."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                r = httpx.head(f"{self.url}/media/{'0' * 32}.jpg", timeout=5)
                if r.status_code == 404:  # ответил наш сервер фото — туннель работает
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def _stop_proc(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.url = ""

    def maybe_close(self, busy: bool) -> None:
        with self.lock:
            if busy:
                self.last_used = time.monotonic()
            elif self.proc is not None and time.monotonic() - self.last_used > IDLE_MINUTES * 60:
                log.info("Временный доступ к фото закрыт")
                self._stop_proc()

    def close(self) -> None:
        with self.lock:
            self._stop_proc()
            if self.server is not None:
                self.server.shutdown()
                self.server = None

    def status(self) -> dict:
        return {"active": self._alive(), "url": self.url if self._alive() else "", "error": self.error}


tunnel = Tunnel()
