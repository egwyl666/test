"""Возможные дубли: один товар у двух поставщиков или дважды в каталоге (штрихкод, код поставщика, название).

Выгодный в группе остаётся, остальные можно скрыть в черновики («Оставить выгодный») — на Prom ничего не удаляется."""

import re

from . import changes, db, products, rates


def _norm_code(value) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]", "", str(value or "").lower())


def _norm_name(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value or "").lower())).strip()


def find() -> list[dict]:
    """Группы товаров, похожих на один и тот же: одинаковый штрихкод, код поставщика (от 4 символов) или название.
    Удаляемые и уже скрытые как дубль не учитываются. Товар попадает в одну группу (первое совпадение: штрихкод →
    код → название). В каждой группе ids упорядочены: первый — выгодный (best)."""
    rows = db.query("""SELECT p.id, p.name, p.barcode, p.vendor_code, p.supplier_id, p.status
                       FROM products p WHERE p.status != 'deleting' AND p.dup_of IS NULL""")
    groups: dict = {}
    taken: set = set()
    for kind, keyfn, minimum in (("barcode", lambda r: re.sub(r"\D", "", r["barcode"] or ""), 8),
                                 ("vendor_code", lambda r: _norm_code(r["vendor_code"]), 4),
                                 ("name", lambda r: _norm_name(r["name"]), 6)):
        by_key: dict = {}
        for r in rows:
            if r["id"] in taken:
                continue
            key = keyfn(r)
            if len(key) >= minimum:
                by_key.setdefault(key, []).append(r["id"])
        for key, ids in by_key.items():
            if len(ids) > 1:
                groups[(kind, key)] = ids
                taken.update(ids)
    found = [{"kind": kind, "key": key, "ids": ids} for (kind, key), ids in groups.items()]
    if found:
        table = rates.Table()
        detail = {r["id"]: r for r in db.query(f"{products.LIST_SELECT} WHERE p.id {db.IN_LIST}",
                                                (db.as_list([i for g in found for i in g["ids"]]),))}
        for g in found:
            g["ids"].sort(key=lambda i: _best_key(detail[i], table), reverse=True)
            g["best"] = g["ids"][0]
    return found


def _best_key(r, table) -> tuple:
    """Какой из дублей оставить: в наличии → закупка известна → больше наценка → дешевле закупка → есть фото."""
    m = products.margin(r, table)[0]
    cost = products._in_uah(table, r["cost_price"], r["cost_currency"], r["supplier_id"])
    return (r["presence"] == "available", m is not None, m if m is not None else 0.0,
            -cost if cost is not None else float("-inf"), (r["image_count"] or 0) > 0, -r["id"])


DUP_SOURCE = "Дубли: оставлен выгодный"


def keep_best(stamp: str | None = None) -> dict:
    """«Оставить выгодный»: в каждой группе дублей выгодный остаётся как есть, остальные — в черновики с пометкой
    «скрыт как дубль» (dup_of). На Prom ничего не удаляется: уже выгруженные дубли там остаются, их число — в ответе."""
    groups = find()
    stamp = stamp or db.now()
    hidden, on_prom, busy = 0, 0, 0
    before = []  # как было у скрытых — для «↩ Отменить»
    with db.tx() as c:
        for g in groups:
            for pid in g["ids"][1:]:
                row = c.execute("SELECT status, synced_at FROM products WHERE id = ?", (pid,)).fetchone()
                if row is None:
                    continue
                if row["status"] in ("sending", "deleting"):
                    busy += 1  # отправляется прямо сейчас — не трогаем, решится на следующем шаге
                    continue
                c.execute("UPDATE products SET dup_of = ?, status = 'draft', updated_at = ? WHERE id = ?",
                          (g["best"], stamp, pid))
                before.append({"id": pid, "status": row["status"], "dup_of": None})
                if row["status"] != "draft":
                    changes.record(c, pid, "status", products.STATUSES.get(row["status"], row["status"]), products.STATUSES["draft"],
                                   who=DUP_SOURCE)
                hidden += 1
                on_prom += 1 if row["synced_at"] else 0
    return {"groups": len(groups), "hidden": hidden, "on_prom": on_prom, "busy": busy, "before": before}


def unhide(product_id: int) -> dict:
    """Вернуть товар, скрытый как дубль (остаётся черновиком — дальше как обычно)."""
    with db.tx() as c:
        if not c.execute("UPDATE products SET dup_of = NULL, updated_at = ? WHERE id = ?",
                         (db.now(), product_id)).rowcount:
            raise KeyError(product_id)
    return products.get(product_id)


KINDS = {"barcode": "штрихкод", "vendor_code": "код поставщика", "name": "название"}
