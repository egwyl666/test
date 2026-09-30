"""Запуск «одной кнопкой»: поднимает сервер на свободном порту и открывает браузер.

Если программа уже запущена (например, повторный двойной щелчок по START.bat) — просто открывает её в браузере.
При первом запуске открывается страница настроек с подсказками.
"""

import os
import socket
import sys
import threading
import time
import webbrowser

import httpx

HOST = "127.0.0.1"
PORTS = range(8000, 8011)


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


def open_when_ready(url: str, port: int) -> None:
    for _ in range(120):
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    webbrowser.open(url)


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

    data_dir = os.environ.get("PROMLOADER_DATA", "data")
    url = f"http://localhost:{port}/" + ("settings?welcome=1" if first_run(data_dir) else "")

    line = "=" * 60
    print(line)
    print(" Prom Loader запущен")
    print(f" Адрес в браузере: http://localhost:{port}/")
    print()
    print(" НЕ ЗАКРЫВАЙТЕ это окно, пока работаете с программой.")
    print(" Чтобы выключить программу — просто закройте это окно.")
    print(f" Ваши товары и фото хранятся в папке: {os.path.abspath(data_dir)}")
    print(line)

    threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()

    import uvicorn

    uvicorn.run("promloader.main:app", host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
