"""Удаление товаров вместе с Prom.

Чтобы не было товаров «где-то застрявших» или «о которых программа не знает»:
- Товар, который есть (или мог попасть) на Prom, не исчезает из программы сразу. Он получает статус «Удаляется с
  Prom» и уходит из программы только когда Prom подтвердил удаление (или ответил, что такого товара у него нет).
- Не получилось (нет связи, Prom недоступен, токен) — программа повторяет сама с нарастающей паузой (до часа) и
  пишет причину у товара. Удаление можно повторить сразу или отменить — товар вернётся в обычный статус.
- Товар, который сейчас отправляется на Prom, удалить нельзя, пока отправка не закончится: иначе импорт мог бы
  создать его на Prom уже после удаления.
- Товары поставщика, удалённые вами, поставщик больше не создаёт заново при обновлении прайса (их можно вернуть
  на странице поставщика).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from . import changes, db, products
from .prom_api import PromError

log = logging.getLogger("promloader.promdelete")

BATCH = 100
RETRY_SECONDS = (60, 120, 300, 600, 1800, 3600)
GONE_STATUSES = {"deleted", "deleted_by_moderator"}


def _later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _ever_sent(ids: set[int]) -> set[int]:
    """Товары, которые хоть раз уходили на Prom (даже если отправка не подтвердилась)."""
    out = set()
    for r in db.query("SELECT products FROM sync_jobs"):
        try:
            out |= {int(pid) for pid in json.loads(r["products"])} & ids
        except (ValueError, TypeError):
            continue
    return out


def _rows(ids: list[int]):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return db.query(f"SELECT id, name, status, synced_at, prom_id FROM products WHERE id IN ({marks})", ids)


def check(ids: list[int]) -> dict:
    """Для окна удаления: сколько из выбранных товаров есть на Prom и сколько сейчас отправляется."""
    rows = _rows([int(i) for i in ids])
    sent = _ever_sent({r["id"] for r in rows})
    on_prom = [r for r in rows if r["synced_at"] or r["prom_id"] or r["id"] in sent]
    return {"total": len(rows), "on_prom": len(on_prom),
            "sending": sum(r["status"] == "sending" for r in rows),
            "deleting": sum(r["status"] == "deleting" for r in rows)}


def _ignore_supplier_items(c, ids: list[int], ignored: int = 1) -> None:
    marks = ",".join("?" * len(ids))
    c.execute(f"UPDATE supplier_items SET ignored = ? WHERE product_id IN ({marks})", [ignored] + ids)


def request(ids: list[int], from_prom: bool = True) -> dict:
    """Удалить товары. from_prom — и на Prom тоже; иначе только из программы (на Prom товары останутся)."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    rows = _rows(ids)
    sent = _ever_sent({r["id"] for r in rows})
    local, remote, rejected = [], [], []
    for r in rows:
        on_prom = bool(r["synced_at"] or r["prom_id"] or r["id"] in sent)
        if from_prom and r["status"] == "deleting":
            continue  # уже удаляется
        if from_prom and on_prom:
            if r["status"] == "sending":
                rejected.append({"id": r["id"], "name": r["name"],
                                 "reason": "Сейчас отправляется на Prom — удалите после окончания отправки"})
            else:
                remote.append(r["id"])
        else:
            local.append(r["id"])
    with db.tx() as c:
        if local or remote:
            _ignore_supplier_items(c, local + remote)  # поставщик не создаст их заново
        for pid in remote:
            c.execute("UPDATE products SET status = 'deleting', last_error = '', delete_attempts = 0, delete_next_at = ? "
                      "WHERE id = ?", (db.now(), pid))
            changes.record(c, pid, "prom", "", "удаляется с Prom")
    kept = sum(1 for r in rows if r["id"] in local and (r["synced_at"] or r["prom_id"] or r["id"] in sent))
    products.delete(local, note="на Prom оставлен" if kept and not from_prom else "")
    return {"deleted": len(local), "deleting": len(remote), "kept_on_prom": kept if not from_prom else 0,
            "rejected": rejected}


def retry(ids: list[int]) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with db.tx() as c:
        return c.execute(f"UPDATE products SET delete_next_at = ?, last_error = '' WHERE status = 'deleting' AND id IN ({marks})",
                         [db.now()] + ids).rowcount


def cancel(ids: list[int]) -> int:
    """Передумали удалять: товар возвращается в обычный статус, поставщик снова его обновляет."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT id, synced_at, pending_fields FROM products WHERE status = 'deleting' AND id IN ({marks})", ids)
    with db.tx() as c:
        for r in rows:
            status = "synced" if r["synced_at"] and (r["pending_fields"] or "[]") == "[]" else "ready"
            c.execute("UPDATE products SET status = ?, last_error = '', delete_next_at = NULL, delete_attempts = 0 "
                      "WHERE id = ?", (status, r["id"]))
            changes.record(c, r["id"], "prom", "удаляется с Prom", "удаление отменено")
        if rows:
            _ignore_supplier_items(c, [r["id"] for r in rows], 0)
    return len(rows)


def _failed(rows, message: str) -> None:
    with db.tx() as c:
        for r in rows:
            attempts = (r["delete_attempts"] or 0) + 1
            wait = RETRY_SECONDS[min(attempts - 1, len(RETRY_SECONDS) - 1)]
            c.execute("UPDATE products SET delete_attempts = ?, delete_next_at = ?, last_error = ? WHERE id = ? "
                      "AND status = 'deleting'",
                      (attempts, _later(wait), f"Не удалось удалить на Prom (попытка {attempts}): {message}. "
                                               "Программа повторит сама", r["id"]))


def _done(ids: list[int]) -> None:
    if not ids:
        return
    with changes.source("Prom"):
        products.delete(ids, note="удалён на Prom")
    log.info("Удалено на Prom и в программе: %d", len(ids))


def _error_text(err) -> str:
    if isinstance(err, dict):
        return "; ".join(str(v) for v in err.values()) or json.dumps(err, ensure_ascii=False)
    return str(err)


async def process(client_factory) -> int:
    """Удаляет на Prom товары, у которых подошло время. Возвращает число обработанных."""
    due = db.query("SELECT id, external_id, prom_id, delete_attempts FROM products WHERE status = 'deleting' "
                   "AND delete_next_at IS NOT NULL AND delete_next_at <= ? ORDER BY delete_next_at, id LIMIT ?",
                   (db.now(), BATCH))
    if not due:
        return 0
    try:
        client = client_factory()
    except PromError as err:
        _failed(due, str(err))
        return len(due)
    by_id = {r["id"]: r for r in due}
    done, failed = [], {}
    try:
        async with client:
            targets: dict[int, int] = {}  # id на Prom -> id в программе
            for r in due:
                if r["prom_id"]:
                    targets[int(r["prom_id"])] = r["id"]
                    continue
                found = await client.get_by_external_id(r["external_id"])
                if found is None or str(found.get("status", "")) in GONE_STATUSES or not found.get("id"):
                    done.append(r["id"])  # на Prom такого товара нет — удалять нечего
                else:
                    targets[int(found["id"])] = r["id"]
            if targets:
                body = await client.delete_products(list(targets))
                processed = {int(x) for x in body.get("processed_ids") or []}
                errors = body.get("errors") or {}
                for prom_id, pid in targets.items():
                    err = errors.get(str(prom_id))
                    text = _error_text(err) if err else ""
                    if prom_id in processed or "не найден" in text.lower() or "not found" in text.lower():
                        done.append(pid)
                    else:
                        failed[pid] = text or "Prom не подтвердил удаление"
    except PromError as err:
        rest = [r for r in due if r["id"] not in done]
        _done(done)
        _failed(rest, str(err))
        return len(due)
    _done(done)
    for pid, text in failed.items():
        _failed([by_id[pid]], text)
    return len(due)
