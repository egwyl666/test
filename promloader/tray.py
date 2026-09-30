"""Работа без чёрного окна: значок у часов + присмотр за программой.

Этот процесс (pythonw.exe, без консоли) запускает сервер отдельным процессом и следит за ним:
- код выхода 3 — перезапуск (после обновления/восстановления): при необходимости доустанавливает компоненты;
- новая версия не стартовала (остался pending-check) — возвращает предыдущую из data/updates/previous;
- любая другая ошибка — понятное окно с сообщением и путём к журналу.
Журнал работы: data/logs/promloader.log.
"""

import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
RESTART_CODE = 3
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
LOG_LIMIT = 5 * 1024 * 1024


def data_dir() -> Path:
    return Path(os.environ.get("PROMLOADER_DATA") or APP_DIR / "data").resolve()


def log_path() -> Path:
    path = data_dir() / "logs" / "promloader.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_LIMIT:
        path.replace(path.with_suffix(".old.log"))
    return path


def console_python() -> str:
    """Для pip нужен python.exe (не pythonw) — окно всё равно не покажется благодаря CREATE_NO_WINDOW."""
    exe = Path(sys.executable)
    candidate = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    return str(candidate if candidate.exists() else exe)


def message(text: str, error: bool = False) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "Prom Loader", 0x10 if error else 0x40)
    else:
        print(text)


# ---------- компоненты и откат ----------

def needs_deps(app: Path = APP_DIR, venv: Path | None = None) -> bool:
    marker = (venv or Path(sys.prefix)) / "installed-requirements.txt"
    try:
        return (app / "requirements.txt").read_bytes() != marker.read_bytes()
    except OSError:
        return True


def install_deps(app: Path = APP_DIR, log=None) -> bool:
    cmd = [console_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt"]
    result = subprocess.run(cmd, cwd=app, stdout=log, stderr=subprocess.STDOUT if log else None, creationflags=NO_WINDOW)
    if result.returncode != 0:
        return False
    shutil.copyfile(app / "requirements.txt", Path(sys.prefix) / "installed-requirements.txt")
    return True


def pending_update(data: Path) -> bool:
    return (data / "updates" / "pending-check").exists()


def rollback(app: Path, data: Path) -> bool:
    previous = data / "updates" / "previous"
    pending = data / "updates" / "pending-check"
    if not previous.exists():
        return False
    shutil.copytree(previous, app, dirs_exist_ok=True)
    # запоминаем неудачную версию, чтобы автообновление не ставило её по кругу
    if pending.exists():
        (data / "updates" / "skip-version").write_text(pending.read_text().strip(), encoding="utf-8")
    pending.unlink(missing_ok=True)
    return True


# ---------- присмотр ----------

class Supervisor:
    """Цикл «запустить — дождаться выхода — решить, что дальше». Функции можно подменить в тестах."""

    def __init__(self, spawn, deps_ok, rollback, pending, notify=lambda text: None):
        self.spawn, self.deps_ok, self.rollback, self.pending, self.notify = spawn, deps_ok, rollback, pending, notify
        self.stopping = False

    def run(self) -> str:
        restarted = False
        while True:
            if not self.deps_ok():
                return "deps_failed"
            code = self.spawn(restarted)
            if self.stopping:
                return "stopped"
            if code == RESTART_CODE:
                restarted = True
                continue
            if code != 0 and self.pending():
                self.notify("Новая версия не запустилась — возвращаю предыдущую")
                if self.rollback():
                    restarted = True
                    continue
            return "ok" if code == 0 else "error"


class App:
    def __init__(self):
        self.data = data_dir()
        self.child: subprocess.Popen | None = None
        self.icon = None
        self.log = open(log_path(), "a", encoding="utf-8", buffering=1)
        self.supervisor = Supervisor(self.spawn, self.deps_ok, lambda: rollback(APP_DIR, self.data),
                                     lambda: pending_update(self.data), self.notify)

    def notify(self, text: str) -> None:
        self.log.write(f"[tray] {text}\n")
        if self.icon is not None:
            try:
                self.icon.notify(text, "Prom Loader")
            except Exception:
                pass

    def deps_ok(self) -> bool:
        if not needs_deps():
            return True
        self.notify("Устанавливаю компоненты программы — это может занять пару минут…")
        return install_deps(APP_DIR, self.log)

    def spawn(self, restarted: bool) -> int:
        env = dict(os.environ, PROMLOADER_DATA=str(self.data), PYTHONIOENCODING="utf-8", PROMLOADER_TRAY="1")
        if restarted:
            env["PROMLOADER_RESTARTED"] = "1"
        self.child = subprocess.Popen([sys.executable, "-m", "promloader.launcher"], cwd=APP_DIR, env=env,
                                      stdout=self.log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        return self.child.wait()

    # ---------- меню ----------

    def port(self) -> int | None:
        try:
            return int((self.data / "port").read_text().strip())
        except (OSError, ValueError):
            return None

    def open_app(self, *_):
        port = self.port()
        if port:
            webbrowser.open(f"http://localhost:{port}/")
        else:
            self.notify("Программа ещё запускается — подождите несколько секунд")

    def restart(self, *_):
        import httpx

        port = self.port()
        try:
            httpx.post(f"http://127.0.0.1:{port}/api/restart", timeout=5)
        except Exception:
            if self.child:
                self.child.terminate()  # не ответила — перезапустим принудительно

    def open_folder(self, *_):
        if os.name == "nt":
            os.startfile(self.data)  # noqa: S606

    def quit(self, *_):
        import httpx

        self.supervisor.stopping = True
        port = self.port()
        try:
            httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=5)
            self.child.wait(timeout=15)
        except Exception:
            if self.child and self.child.poll() is None:
                self.child.terminate()
        if self.icon:
            self.icon.stop()

    def watch(self):
        result = self.supervisor.run()
        if result == "deps_failed":
            message("Не удалось установить компоненты программы. Проверьте интернет и запустите Prom Loader ещё раз.", True)
        elif result == "error":
            message("Prom Loader остановился из-за ошибки.\n\nЖурнал работы:\n"
                    f"{self.data / 'logs' / 'promloader.log'}\n\nОтправьте этот файл разработчику.", True)
        if self.icon:
            self.icon.stop()

    def run(self):
        try:
            import pystray
            from PIL import Image
        except Exception:
            self.watch()  # нет значка (не Windows) — просто присматриваем
            return
        image = Image.open(APP_DIR / "promloader" / "static" / "icon.png")
        menu = pystray.Menu(
            pystray.MenuItem("Открыть Prom Loader", self.open_app, default=True),
            pystray.MenuItem("Перезапустить", self.restart),
            pystray.MenuItem("Папка с данными", self.open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выключить", self.quit),
        )
        self.icon = pystray.Icon("PromLoader", image, "Prom Loader", menu)

        def setup(icon):
            icon.visible = True
            icon.notify("Prom Loader работает. Значок — справа внизу, у часов.", "Prom Loader")
            threading.Thread(target=self.watch, daemon=True).start()

        self.icon.run(setup=setup)


def main():
    os.chdir(APP_DIR)
    from .launcher import PORTS, running_at

    for port in PORTS:  # уже запущена — просто открыть
        if running_at(port):
            webbrowser.open(f"http://localhost:{port}/")
            return
    App().run()


if __name__ == "__main__":
    main()
