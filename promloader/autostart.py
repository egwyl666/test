"""«Запускать вместе с Windows»: ярлык программы в папке «Автозагрузка» текущего пользователя."""

import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
NAME = "Prom Loader.lnk"


class AutostartError(Exception):
    pass


def supported() -> bool:
    return os.name == "nt"


def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def enabled() -> bool:
    return supported() and (startup_dir() / NAME).exists()


def set_enabled(on: bool) -> bool:
    if not supported():
        raise AutostartError("Автозапуск настраивается только в Windows")
    link = startup_dir() / NAME
    if not on:
        link.unlink(missing_ok=True)
        return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PL_LINK);"
        "$s.TargetPath=$env:PL_TARGET; $s.Arguments='-m promloader.tray';"
        "$s.WorkingDirectory=$env:PL_DIR; $s.IconLocation=$env:PL_ICON; $s.Save()"
    )
    env = dict(os.environ, PL_LINK=str(link), PL_TARGET=str(target), PL_DIR=str(APP_DIR),
               PL_ICON=str(APP_DIR / "promloader" / "static" / "icon.ico"))
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                            env=env, capture_output=True, text=True, creationflags=0x08000000)
    if result.returncode != 0 or not link.exists():
        raise AutostartError(f"Не удалось включить автозапуск: {result.stderr.strip()[:200]}")
    return True
