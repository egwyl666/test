"""Приложение на телефоне: push-уведомления (подписка, отписка, проверка)."""

from fastapi import APIRouter, Body, HTTPException

from .. import notify, webpush

router = APIRouter()


@router.get("/api/push")
def push_state(endpoint: str = ""):
    """Есть ли push и на что подписан этот телефон (браузер знает свой endpoint)."""
    if not webpush.available():
        return {"available": False, "events": notify.EVENTS}
    return {"available": True, "public_key": webpush.public_key(), "events": notify.EVENTS,
            "default_events": webpush.DEFAULT_EVENTS, "devices": webpush.count(),
            "subscription": webpush.get(endpoint) if endpoint else None}


@router.post("/api/push/subscribe")
def push_subscribe(subscription: dict = Body(...), events: list[str] = Body(None), device: str = Body("")):
    if not webpush.available():
        raise HTTPException(400, "Push-уведомления недоступны: не установлен компонент cryptography (перезапустите программу)")
    try:
        return webpush.subscribe(subscription, events, device)
    except webpush.PushError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/push/unsubscribe")
def push_unsubscribe(endpoint: str = Body(..., embed=True)):
    webpush.unsubscribe(endpoint)
    return {"ok": True}


@router.post("/api/push/test")
def push_test(endpoint: str = Body(..., embed=True)):
    sub = webpush.get(endpoint)
    if not sub:
        raise HTTPException(404, "Этот телефон не подписан на уведомления")
    try:
        alive = webpush.push(sub, {"title": "Prom Loader", "body": "Проверка: уведомления на этот телефон приходят ✓",
                                   "url": "/settings", "tag": "test"})
    except webpush.PushError as exc:
        raise HTTPException(400, str(exc))
    if not alive:
        raise HTTPException(400, "Подписка больше не действует — включите уведомления заново")
    return {"ok": True}
