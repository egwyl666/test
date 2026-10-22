"""Остатки и закупка.

«Заканчивается»: товар в наличии, количество известно и не больше порога (настройка low_stock_threshold, по
умолчанию 3). Видно плашкой над списком товаров (фильтр low_stock); раз в день в Telegram — о товарах, которые
стали такими с прошлого раза (low_notified_at: повторно — только после пополнения).

Выполненный заказ уменьшает остаток (apply_delivered): у товаров с известным количеством — на проданные штуки
(до нуля → «нет в наличии»), один раз на заказ (orders.stock_applied_at), и изменение сразу уходит на Prom. Это
не ручная правка: 🔒 не ставится, следующий прайс поставщика пришлёт его остаток как обычно. Заказы, выполненные
до появления этой функции или загруженные при первом подключении магазина, остаток не трогают ('legacy').

«Заказ поставщику»: продажи товаров поставщика за N дней (без отменённых заказов) → сколько заказать, чтобы
хватило на M дней с учётом остатка: ceil(продано / N × M − остаток). Остаток неизвестен — считаем его нулём и
так и пишем. Excel — с артикулом поставщика (как в его прайсе), чтобы файл можно было сразу отправить ему."""

import io
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta

from . import changes, db

THRESHOLD_DEFAULT = 3
DAYS_DEFAULT = 30
COVER_DEFAULT = 30
NOTIFY_LIST = 15  # столько товаров перечисляем в одном сообщении Telegram, остальные — числом


class StockError(ValueError):
    pass


def threshold() -> int:
    try:
        return max(0, int(float(db.get_setting("low_stock_threshold") or THRESHOLD_DEFAULT)))
    except ValueError:
        return THRESHOLD_DEFAULT


def set_threshold(value) -> int:
    try:
        n = int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        raise StockError("Порог «заканчивается» — целое число, например 3") from None
    if not 0 <= n <= 100000:
        raise StockError("Порог «заканчивается» — от 0 до 100 000")
    db.set_setting("low_stock_threshold", str(n))
    return n


def low_sql() -> str:
    """Условие «заканчивается» для таблицы products p (фильтр списка и счётчик)."""
    return (f"(p.presence = 'available' AND p.quantity IS NOT NULL AND p.quantity <= {threshold()} "
            "AND p.status != 'deleting' AND p.dup_of IS NULL)")


def low_count() -> int:
    return db.query_one(f"SELECT COUNT(*) AS n FROM products p WHERE {low_sql()}")["n"]


# ---------- уведомление ----------

def notify_text(rows: list, more: int) -> str:
    from .notify import esc
    lines = [f"📉 <b>Заканчиваются товары</b> (остаток ≤ {threshold()}):"]
    lines += [f"• {esc(r['name'] or r['external_id'])} — {r['quantity']:g} шт." for r in rows]
    if more:
        lines.append(f"…и ещё {more}")
    return "\n".join(lines)


def maybe_notify(now: datetime | None = None) -> int:
    """Раз в день: о товарах, которые стали «заканчиваются» с прошлого сообщения. Пополнили — пометка снимается,
    и при следующем падении остатка сообщим снова. Возвращает, о скольких товарах сообщили."""
    from . import notify
    today = (now or datetime.now()).date().isoformat()
    if db.get_setting("low_stock_checked") == today:
        return 0
    db.set_setting("low_stock_checked", today)
    with db.tx() as c:
        c.execute(f"UPDATE products SET low_notified_at = NULL WHERE low_notified_at IS NOT NULL "
                  f"AND id NOT IN (SELECT p.id FROM products p WHERE {low_sql()})")
        fresh = c.execute(f"SELECT p.id, p.name, p.external_id, p.quantity FROM products p "
                          f"WHERE {low_sql()} AND p.low_notified_at IS NULL ORDER BY p.quantity, p.name").fetchall()
        if fresh:
            c.execute(f"UPDATE products SET low_notified_at = ? WHERE id {db.IN_LIST}",
                      (db.now(), db.as_list([r["id"] for r in fresh])))
    if fresh and any("low_stock" in r["events"] for r in notify.recipients()):
        notify.send("low_stock", notify_text(fresh[:NOTIFY_LIST], len(fresh) - NOTIFY_LIST if len(fresh) > NOTIFY_LIST else 0),
                    background=False)
    return len(fresh)


# ---------- выполненный заказ уменьшает остаток ----------

def mark_legacy() -> int:
    """Уже выполненные заказы (до 2.29.0 или при первой загрузке магазина) — без списания остатка."""
    with db.tx() as c:
        n = c.execute("UPDATE orders SET stock_applied_at = 'legacy' WHERE status = 'delivered' AND stock_applied_at = ''"
                      ).rowcount
        db.set_setting("stock_applied_since", db.now())
    return n


def apply_delivered() -> dict:
    """Списать остаток по выполненным заказам, по которым ещё не списывали. → {orders, products, queued}."""
    from . import products, reports, sync
    if not db.get_setting("stock_applied_since"):
        mark_legacy()  # первый запуск новой версии: старые выполненные заказы уже учтены в остатках
        return {"orders": 0, "products": 0, "queued": 0}
    rows = db.query("SELECT id, data FROM orders WHERE status = 'delivered' AND stock_applied_at = '' ORDER BY id")
    if not rows:
        return {"orders": 0, "products": 0, "queued": 0}
    batch = [{"id": r["id"], "items": reports.order_items(json.loads(r["data"] or "{}"))} for r in rows]
    reports._link_products(batch)
    changed: set[int] = set()
    for o in batch:
        with db.tx() as c:
            # «занять» заказ в той же транзакции: два одновременных опроса не спишут его дважды
            if not c.execute("UPDATE orders SET stock_applied_at = ? WHERE id = ? AND stock_applied_at = ''",
                             (db.now(), o["id"])).rowcount:
                continue
            with changes.source(f"Заказ №{o['id']} выполнен"):
                for i in o["items"]:
                    p = i.get("product")
                    if not p:
                        continue
                    row = c.execute("SELECT quantity, presence, status FROM products WHERE id = ?", (p["id"],)).fetchone()
                    if row is None or row["quantity"] is None or row["status"] == "deleting":
                        continue  # остаток не ведётся (неизвестен) — уменьшать нечего
                    left = max(0, row["quantity"] - i["qty"])
                    left = int(left) if float(left).is_integer() else left
                    fields = {"quantity": left}
                    if left == 0 and row["presence"] == "available":
                        fields["presence"] = "not_available"
                    products._touch(c, p["id"], fields, row["status"])
                    changed.add(p["id"])
    ready = {r["id"] for r in db.query("SELECT id FROM products WHERE status = 'ready' AND synced_at IS NOT NULL")}
    to_send = sorted(changed & ready)
    queued = sync.enqueue(to_send)["accepted"] if to_send else 0  # на Prom — сразу, иначе там остаток старый
    return {"orders": len(batch), "products": len(changed), "queued": queued}


# ---------- заказ поставщику ----------

def _clean(value, default: int, low: int, high: int, what: str) -> int:
    try:
        n = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        raise StockError(f"{what}: нужно целое число") from None
    if not low <= n <= high:
        raise StockError(f"{what}: от {low} до {high}")
    return n


def purchase(supplier_id: int, days=DAYS_DEFAULT, cover=COVER_DEFAULT, today: date | None = None) -> dict:
    """Что заказать у поставщика: строки с продажами за days дней или с остатком «заканчивается»."""
    from . import reports
    days = _clean(days, DAYS_DEFAULT, 1, 365, "Продажи за, дней")
    cover = _clean(cover, COVER_DEFAULT, 1, 365, "Хватить на, дней")
    sup = db.query_one("SELECT id, name FROM suppliers WHERE id = ?", (supplier_id,))
    if sup is None:
        raise KeyError(supplier_id)
    today = today or datetime.now().date()
    orders = reports._load_orders(today - timedelta(days=days - 1), today)
    reports._link_products(orders)
    sold: Counter = Counter()
    for o in orders:
        if o["canceled"]:
            continue
        for i in o["items"]:
            if i["product"]:
                sold[i["product"]["id"]] += i["qty"]
    rows = db.query(f"""SELECT p.id, p.name, p.external_id, p.quantity, p.presence,
                               (SELECT si.sku FROM supplier_items si WHERE si.product_id = p.id AND si.supplier_id = ?
                                ORDER BY si.missing LIMIT 1) AS supplier_sku,
                               {low_sql()} AS low
                        FROM products p
                        WHERE p.status != 'deleting' AND (p.supplier_id = ? OR EXISTS (
                              SELECT 1 FROM supplier_items si WHERE si.product_id = p.id AND si.supplier_id = ?))""",
                    (supplier_id, supplier_id, supplier_id))
    out = []
    for r in rows:
        units = sold.get(r["id"], 0.0)
        if not units and not r["low"]:
            continue
        stock = r["quantity"]
        need = math.ceil(units / days * cover - (stock or 0) - 1e-9)
        out.append({"product_id": r["id"], "name": r["name"], "code": r["external_id"], "supplier_sku": r["supplier_sku"] or "",
                    "sold": round(units, 2), "stock": stock, "stock_known": stock is not None,
                    "order": max(0, need), "low": bool(r["low"])})
    out.sort(key=lambda x: (-x["order"], -x["sold"], x["name"] or ""))
    return {"supplier": sup["name"], "supplier_id": sup["id"], "days": days, "cover": cover,
            "rows": out, "total_units": sum(x["order"] for x in out)}


XLSX_HEADERS = [("Артикул поставщика", "supplier_sku"), ("Название", "name"), ("Заказать, шт.", "order"),
                ("Продано за период, шт.", "sold"), ("Остаток сейчас", "stock"), ("Артикул в программе", "code")]


def purchase_xlsx(report: dict) -> bytes:
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Заказ")
    ws.append([f"Заказ поставщику «{report['supplier']}»: продажи за {report['days']} дн., запас на {report['cover']} дн."])
    ws.append([title for title, _ in XLSX_HEADERS])
    for r in report["rows"]:
        if r["order"] <= 0:
            continue
        ws.append([("" if r[k] is None else r[k]) if k != "stock" or r["stock_known"] else "неизвестно"
                   for _, k in XLSX_HEADERS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
