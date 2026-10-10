"""«Поддержка»: пользователь описывает проблему и прикладывает скриншоты — сразу, не откладывая.

- обращение всегда сохраняется локально (data/support/<номер>/): описание, скриншоты, техническая информация;
- если настроен бот поддержки (токен + чат разработчика) — отправляется в Telegram; нет связи — повтор позже;
- любое обращение можно скачать архивом и переслать как угодно.

Бот поддержки настраивается отдельно от бота уведомлений: в «Настройках» (для разработчика), переменными
окружения PROMLOADER_SUPPORT_TOKEN / PROMLOADER_SUPPORT_CHAT или файлом support.json в папке программы:
{"token": "...", "chat_id": "..."} — так разработчик может выдать клиентам уже настроенную программу.
Внимание: токен бота из support.json виден любому, у кого есть программа (он может слать сообщения этому боту).
"""

import io
import json
import logging
import os
import platform
import shutil
import sys
import threading
import zipfile
from pathlib import Path

from . import config, db, notify, products

log = logging.getLogger("promloader.support")

APP_DIR = Path(__file__).resolve().parent.parent
MAX_FILES = 10
MAX_FILE = 15 * 1024 * 1024
ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf", ".txt", ".log", ".xlsx", ".csv", ".xml", ".mp4"}
LOG_TAIL = 300
RETRY_MINUTES = 10
_send_lock = threading.Lock()  # фоновая досылка и кнопка «Отправить снова» не должны отправить дважды


class SupportError(ValueError):
    pass


def support_dir() -> Path:
    path = db.data_dir() / "support"
    path.mkdir(exist_ok=True)
    return path


# ---------- куда отправлять ----------

def _file_config() -> dict:
    try:
        return json.loads((APP_DIR / "support.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def channel() -> dict:
    """Токен и чат разработчика: настройки > переменные окружения > support.json."""
    file_cfg = _file_config()
    return {
        "token": db.get_setting("support_token") or os.environ.get("PROMLOADER_SUPPORT_TOKEN") or file_cfg.get("token", ""),
        "chat_id": db.get_setting("support_chat_id") or os.environ.get("PROMLOADER_SUPPORT_CHAT")
        or str(file_cfg.get("chat_id", "")),
    }


def configured() -> bool:
    c = channel()
    return bool(c["token"] and c["chat_id"])


# ---------- техническая информация ----------

def diagnostics(client: dict | None = None) -> dict:
    """Всё, что поможет разобраться, — без токенов, ключей и паролей."""
    from . import updater

    def count(sql):
        return db.query_one(sql)["n"]

    lines = []
    # журнал программы и вывод при запуске (там — ошибки, из-за которых программа не стартовала)
    for name in ("console.log", "promloader.log"):
        try:
            tail = (db.data_dir() / "logs" / name).read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
        except OSError:
            continue
        lines += [f"--- {name} ---"] + tail
    return {
        "version": updater.current_version(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "products": count("SELECT COUNT(*) AS n FROM products"),
        "products_by_status": {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM products GROUP BY status")},
        "suppliers": count("SELECT COUNT(*) AS n FROM suppliers"),
        "settings": {
            "prom_token_set": bool(config.get("prom_token")),
            "public_base_url_set": bool(config.public_base_url()),
            "photo_tunnel": config.get("photo_tunnel") != "0",
            "photo_storage": config.get("photo_storage") or "",
            "ai_provider": config.get("ai_provider"),
            "auto_update": config.get("auto_update") != "0",
            "quick_updates": db.get_setting("quick_updates") != "0",
            "telegram_recipients": len(notify.recipients()),
        },
        "recent_sync_errors": [r["last_error"] for r in db.query(
            "SELECT last_error FROM sync_jobs WHERE last_error != '' ORDER BY id DESC LIMIT 5")],
        "recent_supplier_errors": [r["message"] for r in db.query(
            "SELECT message FROM supplier_runs WHERE status = 'failed' ORDER BY id DESC LIMIT 5")],
        "client": client or {},
        "log_tail": lines,
    }


# ---------- обращения ----------

def _row(ticket_id: int) -> dict:
    row = db.query_one("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
    if row is None:
        raise KeyError(ticket_id)
    t = dict(row)
    t["files"] = json.loads(t["files"])
    return t


def get(ticket_id: int) -> dict:
    t = _row(ticket_id)
    t.pop("diagnostics", None)
    return t


def list_tickets(limit: int = 50) -> list[dict]:
    return [get(r["id"]) for r in db.query("SELECT id FROM support_tickets ORDER BY id DESC LIMIT ?", (limit,))]


def create(description: str, contact: str = "", page: str = "", files: list[tuple[str, bytes]] | None = None,
           include_diagnostics: bool = True, client: dict | None = None) -> dict:
    description = products.clean_text(description or "").strip()
    files = files or []
    if not description and not files:
        raise SupportError("Опишите проблему или приложите скриншот")
    if len(files) > MAX_FILES:
        raise SupportError(f"Не больше {MAX_FILES} файлов в одном обращении")
    for name, content in files:
        if Path(name).suffix.lower() not in ALLOWED:
            raise SupportError(f"«{name}»: такие файлы приложить нельзя (подходят скриншоты, PDF, Excel, текст)")
        if len(content) > MAX_FILE:
            raise SupportError(f"«{name}» больше 15 МБ")
    diag = diagnostics(client) if include_diagnostics else {"client": client or {}}
    with db.tx() as c:
        ticket_id = c.execute(
            "INSERT INTO support_tickets (description, contact, page, diagnostics, created_at) VALUES (?, ?, ?, ?, ?)",
            (description or "(без описания)", contact.strip()[:200], page[:500], json.dumps(diag, ensure_ascii=False),
             db.now())).lastrowid
    folder = support_dir() / str(ticket_id)
    folder.mkdir(exist_ok=True)
    stored = []
    for i, (name, content) in enumerate(files, 1):
        safe = f"{i:02d}{Path(name).suffix.lower()}"
        (folder / safe).write_bytes(content)
        stored.append({"file": safe, "name": Path(name).name[:120], "size": len(content)})
    with db.tx() as c:
        c.execute("UPDATE support_tickets SET files = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), ticket_id))
    return get(ticket_id)


def archive(ticket_id: int) -> bytes:
    """ZIP с описанием, скриншотами и технической информацией — чтобы переслать вручную."""
    t = _row(ticket_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("обращение.txt", _summary(t))
        z.writestr("техническая-информация.json", t["diagnostics"])
        for f in t["files"]:
            path = support_dir() / str(ticket_id) / f["file"]
            if path.exists():
                z.write(path, f"файлы/{f['file']}_{f['name']}")
    return buf.getvalue()


def _summary(t: dict) -> str:
    diag = json.loads(t["diagnostics"]) if isinstance(t["diagnostics"], str) else t["diagnostics"]
    lines = [
        f"Обращение №{t['id']} от {t['created_at']}",
        f"Версия программы: {diag.get('version', '?')}, {diag.get('os', '')}",
        f"Страница: {t['page'] or '—'}",
        f"Контакт: {t['contact'] or '—'}",
        "",
        t["description"],
    ]
    client = diag.get("client") or {}
    if client.get("js_errors"):
        lines += ["", "Ошибки на странице:"] + [f"• {e}" for e in client["js_errors"][:5]]
    return "\n".join(lines)


def delete(ticket_id: int) -> None:
    _row(ticket_id)
    with db.tx() as c:
        c.execute("DELETE FROM support_tickets WHERE id = ?", (ticket_id,))
    shutil.rmtree(support_dir() / str(ticket_id), ignore_errors=True)


# ---------- отправка разработчику ----------

def send(ticket_id: int, transport=None) -> dict:
    with _send_lock:
        return _send(ticket_id, transport)


def _send_file(ch: dict, ticket_id: int, f: dict, path: Path, transport) -> None:
    caption = f"Обращение №{ticket_id}: {f['name']}"[:1000]
    if path.suffix in (".png", ".jpg", ".jpeg", ".webp") and f["size"] < 10 * 1024 * 1024:
        try:
            notify.call(ch["token"], "sendPhoto", transport, files={"photo": (f["name"], path.read_bytes())},
                        chat_id=ch["chat_id"], caption=caption)
            return
        except notify.NotifyError as exc:
            if str(exc).startswith("Нет связи"):
                raise
            # Telegram не смог обработать картинку (битый файл, слишком вытянутая) — отправим как файл
    notify.call(ch["token"], "sendDocument", transport, files={"document": (f["name"], path.read_bytes())},
                chat_id=ch["chat_id"], caption=caption)


def _send(ticket_id: int, transport=None) -> dict:
    t = _row(ticket_id)
    if t["status"] == "sent":
        return get(ticket_id)
    ch = channel()
    if not (ch["token"] and ch["chat_id"]):
        raise SupportError("Отправка в поддержку не настроена — обращение сохранено. Скачайте архив и перешлите разработчику")
    try:
        text = "🆘 " + _summary(t)
        notify.call(ch["token"], "sendMessage", transport, chat_id=ch["chat_id"], text=text[:4000])
        for f in t["files"]:
            path = support_dir() / str(ticket_id) / f["file"]
            if path.exists():
                _send_file(ch, ticket_id, f, path, transport)
        notify.call(ch["token"], "sendDocument", transport,
                    files={"document": (f"обращение-{ticket_id}-диагностика.json", t["diagnostics"].encode("utf-8"))},
                    chat_id=ch["chat_id"], caption=f"Обращение №{ticket_id}: техническая информация")
    except notify.NotifyError as exc:
        with db.tx() as c:
            c.execute("UPDATE support_tickets SET status = 'send_failed', error = ?, attempts = attempts + 1 WHERE id = ?",
                      (str(exc), ticket_id))
        raise SupportError(f"Обращение сохранено, но не отправлено: {exc}. Попробую ещё раз автоматически")
    with db.tx() as c:
        c.execute("UPDATE support_tickets SET status = 'sent', error = '', sent_at = ? WHERE id = ?", (db.now(), ticket_id))
    return get(ticket_id)


def send_pending() -> int:
    """Фоновая досылка обращений, которые не ушли (не было связи)."""
    if not configured():
        return 0
    sent = 0
    for r in db.query("SELECT id FROM support_tickets WHERE status IN ('saved', 'send_failed') AND attempts < 20 ORDER BY id"):
        try:
            send(r["id"])
            sent += 1
        except SupportError:
            break  # нет связи — остальные тоже не уйдут, попробуем позже
    return sent
