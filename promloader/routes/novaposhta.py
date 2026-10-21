"""Новая Почта: настройки отправителя, ТТН из заказа, этикетка, отслеживание."""

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse, Response

from .. import chat, config, db, novaposhta, orders, sync
from ..prom_api import PromError

router = APIRouter()
log = logging.getLogger("promloader.novaposhta")


def _np(fn, *args):
    try:
        return fn(*args)
    except novaposhta.NPError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/np/settings")
def np_settings():
    return novaposhta.settings_view()


@router.post("/api/np/check")
def np_check(key: str = Body("", embed=True)):
    """Проверить ключ (новый или сохранённый): отправитель и контактные лица из кабинета Новой Почты."""
    key = key.strip() or novaposhta.api_key()
    if not key:
        raise HTTPException(400, "Вставьте ключ API Новой Почты")
    info = _np(novaposhta.check, key)
    with db.tx():
        db.set_setting("np_api_key", key)
        db.set_setting("np_sender_ref", info["sender"]["ref"])
        db.set_setting("np_sender_name", info["sender"]["name"])
    return {**info, **novaposhta.settings_view()}


SETTABLE = ("np_contact_ref", "np_contact_name", "np_sender_phone", "np_city_ref", "np_city_name", "np_warehouse_ref",
            "np_warehouse_name", "np_weight", "np_description", "np_payer", "np_storage_days")


@router.post("/api/np/settings")
def np_save(data: dict = Body(...)):
    if "auto_flag_risky_cod" in data:
        db.set_setting("auto_flag_risky_cod", "1" if data["auto_flag_risky_cod"] else "0")
    values = {k: str(data[k]).strip() for k in SETTABLE if k in data}
    if "np_sender_phone" in values and values["np_sender_phone"]:
        values["np_sender_phone"] = _np(novaposhta._phone, values["np_sender_phone"])
    if values.get("np_payer") and values["np_payer"] not in novaposhta.PAYERS:
        raise HTTPException(400, "Кто платит за доставку — покупатель или вы")
    for key, low, high, label in (("np_weight", 0.1, 1000, "Вес по умолчанию — от 0,1 до 1000 кг"),
                                  ("np_storage_days", 1, 30, "Дней до напоминания — от 1 до 30")):
        if key in values:
            try:
                ok = low <= float(values[key].replace(",", ".")) <= high
            except ValueError:
                ok = False
            if not ok:
                raise HTTPException(400, label)
            values[key] = values[key].replace(",", ".")
    with db.tx():
        for k, v in values.items():
            db.set_setting(k, v)
    return novaposhta.settings_view()


@router.get("/api/np/cities")
def np_cities(q: str = ""):
    return {"items": _np(novaposhta.cities, q) if len(q.strip()) >= 2 else []}


@router.get("/api/np/warehouses")
def np_warehouses(city_ref: str, q: str = ""):
    return {"items": _np(novaposhta.warehouses, city_ref, q)}


@router.post("/api/np/track")
def np_track():
    return {"checked": _np(novaposhta.track)}


@router.get("/api/orders/{order_id}/ttn/draft")
def ttn_draft(order_id: int):
    d = _np(novaposhta.draft, order_id)
    # история покупателя: отказывался от посылок — оператор увидит это до отправки наложенным платежом
    return {**d, "ready": novaposhta.sender_ready(), "payers": novaposhta.PAYERS,
            "buyer": orders.buyer_history(d.get("phone"), exclude_order_id=order_id)}


@router.post("/api/orders/{order_id}/prepay-request")
async def prepay_request(order_id: int):
    """«💬 Запросить предоплату в чат Prom» рисковому покупателю (раз в сутки, отметка видна всем операторам)."""
    row = await asyncio.to_thread(db.query_one, "SELECT prepay_state, prepay_requested_at FROM orders WHERE id = ?",
                                  (order_id,))
    if row is None:
        raise HTTPException(404, "Нет такого заказа")
    if novaposhta.remind_blocked(row["prepay_requested_at"]):
        raise HTTPException(409, "Предоплату у покупателя уже просили — повторно можно через сутки")
    if not config.get("prom_token"):
        raise HTTPException(400, "Сообщение не отправлено: не указан токен Prom")
    owner = await asyncio.to_thread(db.acquire, f"prepay:{order_id}", 60)
    if owner is None:
        raise HTTPException(409, "Сообщение уже отправляется")
    try:
        async with sync.make_client() as client:
            await chat.send_prepay_request(client, order_id)

        def save():
            with db.tx() as c:
                c.execute("UPDATE orders SET prepay_state = 'requested', prepay_requested_at = ? WHERE id = ?",
                          (db.now(), order_id))
        await asyncio.to_thread(save)
    except chat.ChatError as exc:
        raise HTTPException(400, str(exc))
    except PromError as exc:
        raise HTTPException(400, f"Сообщение не отправлено: {exc}")
    finally:
        await asyncio.to_thread(db.release, f"prepay:{order_id}", owner)
    return {"ok": True}


@router.post("/api/orders/{order_id}/prepay")
def order_prepay(order_id: int, state: str = Body(..., embed=True)):
    """Снять пометку «нужна предоплата» (ok) или поставить вручную (required)."""
    return orders.set_prepay(order_id, state)


@router.post("/api/orders/{order_id}/remind")
async def remind_buyer(order_id: int):
    """«🔔 Напомнить в чат»: посылка ждёт на отделении — вежливое сообщение покупателю в чат Prom (один раз в сутки)."""
    s = await asyncio.to_thread(novaposhta.shipment, order_id)
    if not s or s["stage"] != "arrived":
        raise HTTPException(400, "Посылка не на отделении — напоминать пока не о чем")
    if novaposhta.remind_blocked(s["reminded_at"]):
        raise HTTPException(409, "Покупателю уже напомнили — повторно можно через сутки")
    if not config.get("prom_token"):
        raise HTTPException(400, "Напоминание не отправлено: не указан токен Prom")
    owner = await asyncio.to_thread(db.acquire, f"remind:{order_id}", 60)
    if owner is None:  # два оператора нажали одновременно
        raise HTTPException(409, "Напоминание уже отправляется")
    try:
        async with sync.make_client() as client:
            await chat.send_reminder(client, order_id, s)
        def save():
            with db.tx() as c:
                c.execute("UPDATE shipments SET reminded_at = ? WHERE order_id = ?", (db.now(), order_id))
        await asyncio.to_thread(save)
    except chat.ChatError as exc:
        raise HTTPException(400, str(exc))
    except PromError as exc:
        raise HTTPException(400, f"Напоминание не отправлено: {exc}")
    finally:
        await asyncio.to_thread(db.release, f"remind:{order_id}", owner)
    log.info("Заказ %s: покупателю напомнили забрать посылку %s", order_id, s["ttn"])
    return {"ok": True}


@router.post("/api/orders/{order_id}/ttn")
async def ttn_create(order_id: int, form: dict = Body(...)):
    """Создать ТТН и передать её номер в заказ Prom. Не передался — ТТН всё равно есть, ошибка видна у заказа."""
    # рисковый покупатель (отказывался от посылок): наложенный платёж — только осознанно, после подтверждения
    flag = await asyncio.to_thread(db.query_one, "SELECT prepay_state FROM orders WHERE id = ?", (order_id,))
    if flag and flag["prepay_state"] in ("required", "requested") and form.get("cod") and not form.get("confirm_risky"):
        return JSONResponse({"detail": "Рисковый покупатель: отказывался от посылок или отменял заказы. Наложенный "
                                       "платёж заблокирован — возьмите предоплату или подтвердите отправку наложенным "
                                       "платежом", "risky": True}, status_code=409)
    try:
        s = await asyncio.to_thread(novaposhta.create, order_id, form)
    except novaposhta.NPError as exc:
        raise HTTPException(409 if isinstance(exc, novaposhta.TTNConflict) else 400, str(exc))
    error = ""
    if config.get("prom_token"):
        try:
            async with sync.make_client() as client:
                await client.save_declaration(order_id, s["ttn"])
        except PromError as exc:
            error = str(exc)
    else:
        error = "Номер ТТН не передан в Prom: не указан токен Prom"

    def save_error():
        with db.tx() as c:
            c.execute("UPDATE shipments SET prom_error = ? WHERE order_id = ?", (error, order_id))
    await asyncio.to_thread(save_error)
    result = {**s, "prom_error": error}
    new_status = str(form.get("set_status") or "")
    if new_status:
        # статус заказа на Prom тем же шагом (обычно «Принят»); не вышло — ТТН всё равно создана, причина — в ответе
        result["status_updated"], result["status_error"] = False, ""
        current = db.query_one("SELECT status FROM orders WHERE id = ?", (order_id,))
        if new_status not in ("received", "delivered"):
            result["status_error"] = "Статус заказа не изменён: из окна ТТН можно поставить «Принят» или «Выполнен»"
        elif current and current["status"] == new_status:
            result["status_updated"] = True  # уже в этом статусе
        elif not config.get("prom_token"):
            result["status_error"] = "Статус заказа не изменён: не указан токен Prom"
        else:
            try:
                async with sync.make_client() as client:
                    await orders.set_status(client, order_id, new_status)
                result["status_updated"] = True
            except PromError as exc:
                result["status_error"] = f"Статус заказа не изменён: {exc}"
            except Exception as exc:
                log.exception("ТТН %s: статус заказа не изменён", s["ttn"])
                result["status_error"] = f"Статус заказа не изменён: {exc}"
    if form.get("notify_buyer"):
        # номер ТТН покупателю в чат Prom; не вышло — ТТН всё равно создана, причина — в ответе
        result["chat_sent"], result["chat_error"] = False, ""
        if not config.get("prom_token"):
            result["chat_error"] = "Сообщение покупателю не отправлено: не указан токен Prom"
        else:
            try:
                async with sync.make_client() as client:
                    await chat.send_ttn(client, order_id, s["ttn"])
                result["chat_sent"] = True
            except (chat.ChatError, PromError) as exc:
                result["chat_error"] = f"Сообщение покупателю не отправлено: {exc}"
            except Exception as exc:  # чат не должен ломать ответ о созданной ТТН
                log.exception("ТТН %s: сообщение покупателю не отправлено", s["ttn"])
                result["chat_error"] = f"Сообщение покупателю не отправлено: {exc}"
    return result


@router.delete("/api/orders/{order_id}/ttn")
def ttn_delete(order_id: int):
    _np(novaposhta.delete, order_id)
    return {"ok": True}


@router.get("/api/orders/{order_id}/ttn/label.pdf")
def ttn_label(order_id: int):
    pdf = _np(novaposhta.label_pdf, order_id)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="ttn-{order_id}.pdf"'})
