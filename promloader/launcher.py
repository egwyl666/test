"""Запуск «одной кнопкой»: поднимает сервер на свободном порту и открывает браузер.

Если программа уже запущена (например, повторный двойной щелчок по START.bat) — просто открывает её в браузере.
При первом запуске открывается страница настроек с подсказками.
Перед стартом: восстановление из резервной копии (если его запланировали) и автообновление (если включено).
Код выхода 3 означает «перезапустить» — START.bat доустановит компоненты и запустит программу снова.
"""

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx

HOST = "127.0.0.1"
PORTS = range(8000, 8011)
log = logging.getLogger("promloader.launcher")


def running_at(port: int) -> bool:
    """На этом порту уже работает Prom Loader?"""
    try:
        r = httpx.get(f"http://{HOST}:{port}/api/meta", timeout=1.5)
    except httpx.HTTPError:
        return False
    if r.status_code == 401:  # включён пароль (APP_PASSWORD)
        return "Prom Loader" in r.headers.get("www-authenticate", "")
    return r.status_code == 200 and '"presence"' in r.text


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def first_run(data_dir: str) -> bool:
    return not os.path.exists(os.path.join(data_dir, "promloader.sqlite3"))


def wait_and_open(url: str, port: int, open_browser: bool) -> None:
    for _ in range(240):
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    else:
        return
    from . import updater

    try:
        updater.confirm_started()
    except Exception:
        pass
    if open_browser:
        webbrowser.open(url)


def before_start(data_dir: Path) -> None:
    """Восстановление и автообновление — до того, как сервер откроет базу."""
    from . import backup, config, db, updater

    if backup.apply_pending(data_dir):
        print(" Данные восстановлены из резервной копии.")
    db.init(data_dir)
    if config.get("auto_update") == "0" or updater.is_dev_checkout():
        return
    try:
        info = updater.check()
        if info["available"]:
            print(f" Найдена новая версия {info['latest']} — устанавливаю...")
            version = updater.update()
            print(f" Установлена версия {version}. Перезапуск...")
            sys.exit(updater.RESTART_CODE)
    except updater.UpdateError as exc:
        print(f" Обновление пропущено: {exc}")
    except httpx.HTTPError:
        pass


def main() -> None:
    for port in PORTS:
        if running_at(port):
            url = f"http://localhost:{port}/"
            print(f"Prom Loader уже запущен — открываю {url}")
            webbrowser.open(url)
            return

    port = next((p for p in PORTS if port_free(p)), None)
    if port is None:
        print("Не нашёл свободный порт (8000–8010). Закройте лишние программы и попробуйте ещё раз.")
        sys.exit(1)

    data_dir = Path(os.environ.get("PROMLOADER_DATA", "data")).resolve()
    is_first = first_run(str(data_dir))
    before_start(data_dir)

    from . import runtime, updater

    url = f"http://localhost:{port}/" + ("settings?welcome=1" if is_first else "")
    line = "=" * 60
    print(line)
    print(f" Prom Loader {updater.current_version()} запущен")
    print(f" Адрес в браузере: http://localhost:{port}/")
    if not os.environ.get("PROMLOADER_TRAY"):
        print()
        print(" НЕ ЗАКРЫВАЙТЕ это окно, пока работаете с программой.")
        print(" Чтобы выключить программу — просто закройте это окно.")
    print(f" Ваши товары и фото хранятся в папке: {data_dir}")
    print(line)

    open_browser = not os.environ.get("PROMLOADER_RESTARTED")
    threading.Thread(target=wait_and_open, args=(url, port, open_browser), daemon=True).start()

    import uvicorn

    config = uvicorn.Config("promloader.main:app", host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    runtime.server = server
    port_file = data_dir / "port"  # по нему значок у часов открывает программу
    port_file.write_text(str(port))
    try:
        server.run()
    finally:
        port_file.unlink(missing_ok=True)
    if runtime.restart_requested:
        sys.exit(runtime.RESTART_CODE)


if __name__ == "__main__":
    main()
