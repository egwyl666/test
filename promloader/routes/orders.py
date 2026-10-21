"""Заказы с Prom и Telegram."""


from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from .. import config, db, notify, orders, reports, sync
from ..prom_api import PromError

router = APIRouter()


@router.get("/api/orders")
def list_orders(status: str = "", q: str = "", date_from: str = "", date_to: str = "", limit: int = 50,
                offset: int = 0):
    flt = {"status": status, "q": q, "date_from": date_from, "date_to": date_to}
    return {**orders.list_orders(flt, max(1, min(limit, 200)), max(0, min(offset, 10 ** 9))),
            "statuses": orders.STATUSES, "settable": orders.SETTABLE, "cancel_reasons": orders.CANCEL_REASONS,
            "enabled": orders.enabled()}


@router.get("/api/orders/summary")
def orders_summary():
    return reports.order_summary()


def _stats(period, date_from, date_to, q, sort):
    try:
        return reports.order_stats(period, date_from, date_to, q.strip(), sort)
    except reports.ReportError as exc:
        raise HTTPException(400, str(exc))


# Excel — та же таблица, что на вкладке «Статистика»
@router.get("/api/orders/stats.xlsx")
def orders_stats_xlsx(period: str = "30d", date_from: str = "", date_to: str = "", q: str = "", sort: str = "revenue"):
    report = _stats(period, date_from, date_to, q, sort)
    name = f"tovary-v-zakazah-{report['from']}-{report['to']}.xlsx"
    return Response(reports.xlsx(report), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/orders/stats")
def orders_stats(period: str = "30d", date_from: str = "", date_to: str = "", q: str = "", sort: str = "revenue"):
    return _stats(period, date_from, date_to, q, sort)


@router.post("/api/orders/refresh")
async def refresh_orders():
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    try:
        async with sync.make_client() as client:
            new = await orders.poll(client)
    except PromError as exc:
        raise HTTPException(502, str(exc))
    return {"new": new}


@router.get("/api/orders/scan")
def orders_scan(code: str = ""):
    """Скан камерой или ввод: ТТН или номер заказа → заказ."""
    try:
        return orders.find_by_code(code)
    except orders.ScanError as exc:
        raise HTTPException(exc.status, str(exc))


@router.post("/api/orders/{order_id}/packed")
def order_packed(request: Request, order_id: int, packed: bool = Body(True, embed=True), force: bool = Body(False, embed=True)):
    """Режим упаковки: «Упакован» (или снять отметку). Кто упаковал — имя вошедшего пользователя."""
    if not packed:
        return {"ok": True, "order": orders.unpack(order_id), "warnings": []}
    user = getattr(request.state, "user", None) or {}
    try:
        return orders.pack(order_id, user.get("name") or user.get("login") or "", force=force)
    except orders.ScanError as exc:
        raise HTTPException(exc.status, str(exc))


@router.post("/api/orders/seen")
def orders_seen(ids: list[int] | None = Body(None, embed=True)):
    """ids — эти заказы; без ids — все. Пустой список — ни одного (раньше он помечал все)."""
    orders.mark_seen(ids)
    return {"ok": True}


@router.post("/api/orders/{order_id}/status")
async def order_status(order_id: int, status: str = Body(...), reason: str = Body(""), text: str = Body("")):
    try:
        async with sync.make_client() as client:
            return await orders.set_status(client, order_id, status, reason, text)
    except PromError as exc:
        raise HTTPException(502 if exc.retryable else 400, str(exc))


@router.get("/api/telegram/recipients")
def telegram_recipients():
    return {"items": notify.recipients(), "events": notify.EVENTS, "token_set": bool(notify.token()),
            "outbox": notify.outbox_state()}


@router.post("/api/telegram/retry")
def telegram_retry():
    """«Повторить сейчас» для уведомлений, которые не дошли."""
    with db.tx() as c:
        c.execute("UPDATE tg_outbox SET next_at = ? WHERE sent_at IS NULL AND failed = 0", (db.now(),))
    return {"sent": notify.flush(), "outbox": notify.outbox_state()}


@router.post("/api/telegram/candidates")
def telegram_candidates():
    """Кто недавно писал боту — чтобы добавить в получатели одним нажатием."""
    known = {r["chat_id"] for r in notify.recipients()}
    chats = notify.recent_chats(notify.token())
    return {"items": [c for c in chats if c["chat_id"] not in known]}


@router.post("/api/telegram/recipients")
def telegram_add(chat_id: str = Body(...), name: str = Body(""), events: list[str] | None = Body(None)):
    return notify.add_recipient(chat_id, name, events)


@router.patch("/api/telegram/recipients/{rid}")
def telegram_update(rid: int, events: list[str] | None = Body(None), name: str | None = Body(None)):
    notify.update_recipient(rid, events, name)
    return {"items": notify.recipients()}


@router.delete("/api/telegram/recipients/{rid}")
def telegram_remove(rid: int):
    notify.remove_recipient(rid)
    return {"items": notify.recipients()}


@router.post("/api/telegram/recipients/{rid}/test")
def telegram_test(rid: int):
    r = next((x for x in notify.recipients() if x["id"] == rid), None)
    if r is None:
        raise HTTPException(404)
    notify.send_to(r["chat_id"], "✅ Prom Loader: уведомления подключены. "
                            "Подписки: " + ", ".join(notify.EVENTS[e] for e in r["events"]))
    return {"ok": True}
