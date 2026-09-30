"""Связь веб-сервера с запускатором: позволяет программе перезапустить саму себя (после обновления/восстановления)."""

import threading

RESTART_CODE = 3

server = None          # uvicorn.Server, если программа запущена через launcher
restart_requested = False


def can_restart() -> bool:
    return server is not None


def request_restart(delay: float = 1.0) -> bool:
    """Мягко остановить сервер через delay секунд (успеть отдать ответ браузеру); launcher выйдет с кодом 3."""
    global restart_requested
    if server is None:
        return False
    restart_requested = True

    def stop():
        server.should_exit = True

    threading.Timer(delay, stop).start()
    return True


def request_stop(delay: float = 0.5) -> bool:
    """Выключить программу (из меню значка у часов)."""
    if server is None:
        return False

    def stop():
        server.should_exit = True

    threading.Timer(delay, stop).start()
    return True
