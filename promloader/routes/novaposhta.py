"""Новая Почта: настройки отправителя, ТТН из заказа, этикетка, отслеживание."""

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

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
    return {**_np(novaposhta.draft, order_id), "ready": novaposhta.sender_ready(), "payers": novaposhta.PAYERS}


@router.post("/api/orders/{order_id}/ttn")
async def ttn_create(order_id: int, form: dict = Body(...)):
    """Создать ТТН и передать её номер в заказ Prom. Не передался — ТТН всё равно есть, ошибка видна у заказа."""
    try:
        s = await asyncio.to_thread(novaposhta.create, order_id, form)
    except novaposhta.NPError as exc:
        raise HTTPException(400, str(exc))
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
