"""Самообновление программы с GitHub.

Схема:
1. Проверка: файл VERSION в ветке main репозитория сравнивается с локальным.
2. Установка: резервная копия данных -> скачивание архива ветки -> проверка (все .py компилируются) ->
   текущий код откладывается в data/updates/previous -> новые файлы копируются поверх.
   Папки data/, .venv/, .git/ не трогаются никогда.
3. Перезапуск: программа завершается с кодом 3, START.bat доустанавливает компоненты и запускает новую версию.
   Если новая версия не стартовала (остался файл pending-check), START.bat возвращает предыдущую
   средствами Windows (robocopy из data/updates/previous) — без Python, на случай если сломан сам код.
"""

import io
import logging
import os
import py_compile
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import httpx

from . import backup, config, db

log = logging.getLogger("promloader.updater")

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "egwyl666/test"
GITHUB_API = os.environ.get("PROMLOADER_GITHUB_API", "https://api.github.com")
BRANCH = "main"
KEEP = {"data", ".venv", ".git", "__pycache__", ".pytest_cache"}
RESTART_CODE = 3


class UpdateError(Exception):
    pass


def current_version(app_dir: Path = APP_DIR) -> str:
    try:
        return (app_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def parse_version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def is_dev_checkout(app_dir: Path = APP_DIR) -> bool:
    """В git-копии разработчика самообновление выключено: там обновляются через git."""
    return (app_dir / ".git").exists()


def _client(transport=None) -> httpx.Client:
    headers = {"User-Agent": "PromLoader-updater", "Accept": "application/vnd.github+json"}
    token = config.get("github_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=GITHUB_API, headers=headers, timeout=60,
                        follow_redirects=True, transport=transport)


def _repo() -> str:
    return config.get("update_repo") or DEFAULT_REPO


def _raw(client: httpx.Client, path: str) -> str:
    r = client.get(f"/repos/{_repo()}/contents/{path}", params={"ref": BRANCH},
                   headers={"Accept": "application/vnd.github.raw"})
    if r.status_code == 404:
        raise UpdateError("Репозиторий с обновлениями недоступен. Если он закрытый — укажите токен GitHub в «Настройках»")
    if r.status_code in (401, 403):
        raise UpdateError("GitHub не принял токен (или превышен лимит запросов). Проверьте токен в «Настройках»")
    if r.status_code >= 400:
        raise UpdateError(f"GitHub ответил ошибкой HTTP {r.status_code}")
    return r.text


def _notes(changelog: str, current: str) -> str:
    """Разделы «## x.y.z» из ЧТО-НОВОГО.md, которые новее установленной версии."""
    out, keep = [], False
    for line in changelog.splitlines():
        m = re.match(r"^##\s+(\S+)", line)
        if m:
            keep = parse_version(m.group(1)) > parse_version(current)
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def check(transport=None, app_dir: Path = APP_DIR) -> dict:
    current = current_version(app_dir)
    if is_dev_checkout(app_dir):
        return {"current": current, "latest": current, "available": False, "notes": "",
                "message": "Это копия разработчика (git) — обновляйте через git pull"}
    try:
        with _client(transport) as client:
            latest = _raw(client, "VERSION").strip()
            try:
                notes = _notes(_raw(client, "ЧТО-НОВОГО.md"), current)
            except UpdateError:
                notes = ""
    except httpx.HTTPError as exc:
        raise UpdateError(f"Нет связи с GitHub: {exc}")
    available = parse_version(latest) > parse_version(current)
    skipped = skipped_version()
    if available and skipped and parse_version(latest) <= parse_version(skipped):
        db.set_setting("update_latest", "")
        return {"current": current, "latest": latest, "available": False, "notes": "",
                "message": f"Версия {skipped} не запустилась на этом компьютере и была отменена. "
                           "Дождитесь следующей версии"}
    db.set_setting("update_latest", latest if available else "")
    return {"current": current, "latest": latest, "available": available, "notes": notes if available else "",
            "message": f"Доступна версия {latest}" if available else "У вас последняя версия"}


def skipped_version() -> str:
    """Версия, которая уже не запустилась и была откачена (её не ставим повторно)."""
    try:
        return (db.data_dir() / "updates" / "skip-version").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _download(transport=None) -> bytes:
    try:
        with _client(transport) as client:
            r = client.get(f"/repos/{_repo()}/zipball/{BRANCH}")
    except httpx.HTTPError as exc:
        raise UpdateError(f"Не удалось скачать обновление: {exc}")
    if r.status_code >= 400:
        raise UpdateError(f"Не удалось скачать обновление: HTTP {r.status_code}")
    return r.content


def _extract(content: bytes, dest: Path) -> Path:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for info in z.infolist():
                target = (dest / info.filename).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise UpdateError("Архив обновления повреждён")
            z.extractall(dest)
    except zipfile.BadZipFile:
        raise UpdateError("Скачанный архив повреждён")
    roots = [p for p in dest.iterdir() if p.is_dir()]
    root = roots[0] if len(roots) == 1 else dest
    if not (root / "promloader" / "main.py").exists() or not (root / "VERSION").exists():
        raise UpdateError("В архиве нет программы — обновление отменено")
    return root


def _verify(root: Path) -> None:
    for path in root.rglob("*.py"):
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(Path(tempfile.gettempdir()) / "promloader_check.pyc"))
        except py_compile.PyCompileError as exc:
            raise UpdateError(f"Новая версия повреждена ({path.name}) — обновление отменено: {exc.msg[:200]}")


def _code_files(root: Path) -> set[Path]:
    files = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in KEEP or "__pycache__" in rel.parts:
            continue
        if path.is_file():
            files.add(rel)
    return files


def install(content: bytes, app_dir: Path = APP_DIR) -> str:
    """Ставит новую версию из архива. Возвращает её номер."""
    updates = db.data_dir() / "updates"
    work = updates / "new"
    previous = updates / "previous"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    root = _extract(content, work)
    _verify(root)
    version = current_version(root)
    if parse_version(version) < parse_version(current_version(app_dir)):
        raise UpdateError("В архиве более старая версия — обновление отменено")

    backup.create(reason="before-update")

    old_files, new_files = _code_files(app_dir), _code_files(root)
    shutil.rmtree(previous, ignore_errors=True)
    for rel in old_files:
        (previous / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(app_dir / rel, previous / rel)
    for rel in new_files:
        (app_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, app_dir / rel)
    for rel in old_files - new_files:  # файлы, удалённые в новой версии
        (app_dir / rel).unlink(missing_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    (updates / "pending-check").write_text(version, encoding="utf-8")
    db.set_setting("update_latest", "")
    log.info("Установлена версия %s", version)
    return version


def update(transport=None, app_dir: Path = APP_DIR) -> str:
    if is_dev_checkout(app_dir):
        raise UpdateError("Это копия разработчика (git) — обновляйте через git pull")
    info = check(transport, app_dir)
    if not info["available"]:
        raise UpdateError("У вас уже последняя версия")
    return install(_download(transport), app_dir)


def confirm_started() -> None:
    """Новая версия запустилась — откат больше не нужен."""
    (db.data_dir() / "updates" / "pending-check").unlink(missing_ok=True)
