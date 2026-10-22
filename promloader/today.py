"""Главная «Сегодня»: итоги дня и всё, что требует внимания, — с переходом прямо к нужному списку.

Утром открыл программу — и сразу видно, что делать: новые заказы, посылки на отделении, ошибки отправки, товары,
которые ждут ИИ или заканчиваются, сообщения покупателей. Пункты с нулём не показываются."""

from datetime import datetime, timedelta, timezone

from . import chat, db, products, reports, stock, suppliers

ACTIVE = ("pending", "received", "paid")


def _n(sql: str, args=()) -> int:
    return db.query_one(sql, args)["n"]


def attention() -> list[dict]:
    """[{key, icon, text, count, href, tone}] — tone: bad (ошибка), warn (не забыть), info, good (можно двигать дальше)."""
    waiting_since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    ready = products.readiness({})
    status = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM products GROUP BY status")}
    items = [
        ("orders_new", "📦", "Новые заказы — ещё не просмотрены", _n("SELECT COUNT(*) AS n FROM orders WHERE seen = 0"),
         "/orders", "warn"),
        # «не упакованы» — только если упаковку отмечают (режим упаковки): иначе это все принятые заказы подряд
        ("orders_unpacked", "📦", "Принятые заказы ещё не упакованы",
         _n("SELECT COUNT(*) AS n FROM orders WHERE status = 'received' AND packed_at = ''")
         if _n("SELECT COUNT(*) AS n FROM orders WHERE packed_at != ''") else 0, "/orders?status=received", "info"),
        ("prepay", "⚠", "Рисковые покупатели: нужна предоплата",
         _n(f"SELECT COUNT(*) AS n FROM orders WHERE prepay_state = 'required' AND status IN {ACTIVE}"), "/orders", "warn"),
        ("parcels_waiting", "🚚", "Посылки ждут на отделении 3+ дня",
         _n("SELECT COUNT(*) AS n FROM shipments WHERE stage = 'arrived' AND arrived_at != '' AND arrived_at <= ?",
            (waiting_since,)), "/orders", "warn"),
        ("chat", "💬", "Непрочитанные сообщения покупателей", chat.unseen(), "/chat", "warn"),
        ("sync_errors", "⚠", "Товары с ошибкой отправки на Prom", status.get("error", 0), "/?status=error", "bad"),
        ("ready_send", "🚀", "Готовы к отправке на Prom", status.get("ready", 0), "/?status=ready", "good"),
        ("ready_promote", "✓", "Черновики готовы для Prom", ready.get("ready_to_promote", 0),
         "/?status=draft&readiness=ready_to_promote", "good"),
        ("need_ai", "✨", "Черновики ждут ИИ (перевод, ключевые слова)", ready.get("missing_ai", 0),
         "/?status=draft&readiness=missing_ai", "info"),
        ("low_stock", "📉", "Заканчиваются на складе", stock.low_count(), "/?low_stock=1", "warn"),
        ("conflicts", "⚡", "Конфликты с кабинетом Prom", _n("SELECT COUNT(*) AS n FROM products WHERE prom_conflict != ''"),
         "/?conflict=1", "bad"),
        ("suppliers_failed", "⚠", "Поставщики не обновились", len(suppliers.failed()), "/suppliers", "bad"),
    ]
    return [{"key": k, "icon": i, "text": t, "count": n, "href": h, "tone": tone} for k, i, t, n, h, tone in items if n]


def overview() -> dict:
    return {"sales": reports.order_summary(), "attention": attention(),
            "products": _n("SELECT COUNT(*) AS n FROM products"), "now": datetime.now().isoformat(timespec="minutes")}
