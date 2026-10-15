"""Обращения в поддержку."""

import asyncio
import json

from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    UploadFile,
)
from fastapi.responses import (
    Response,
)

from .. import (
    notify,
    support,
)

router = APIRouter()


@router.get("/api/support")
def support_list():
    return {"items": support.list_tickets(), "configured": support.configured()}


@router.get("/api/support/diagnostics")
def support_diagnostics():
    """Что именно уйдёт вместе с обращением (показываем пользователю)."""
    d = support.diagnostics()
    d["log_tail"] = d["log_tail"][-20:]
    return d


@router.post("/api/support")
async def support_create(description: str = Form(""), contact: str = Form(""), page: str = Form(""),
                         include_diagnostics: bool = Form(True), client: str = Form("{}"),
                         files: list[UploadFile] = File(default=[])):
    try:
        client_info = json.loads(client)
    except ValueError:
        client_info = {}
    payload = [(f.filename or "file", await f.read(support.MAX_FILE + 1)) for f in files]
    ticket = await asyncio.to_thread(support.create, description, contact, page, payload, include_diagnostics, client_info)
    message = "Обращение сохранено."
    if support.configured():
        try:
            ticket = await asyncio.to_thread(support.send, ticket["id"])
            message = "Обращение отправлено разработчику."
        except support.SupportError as exc:
            message = str(exc)
    else:
        message += " Отправка разработчику пока не настроена — скачайте архив обращения и перешлите его."
    return {"ticket": ticket, "message": message}


@router.post("/api/support/{ticket_id}/send")
def support_send(ticket_id: int):
    return support.send(ticket_id)


@router.get("/api/support/{ticket_id}/archive")
def support_archive(ticket_id: int):
    content = support.archive(ticket_id)
    return Response(content, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="promloader-obrashchenie-{ticket_id}.zip"'})


@router.delete("/api/support/{ticket_id}")
def support_delete(ticket_id: int):
    support.delete(ticket_id)
    return {"ok": True}


@router.post("/api/support/channel/chats")
def support_chats(token: str = Body("", embed=True)):
    """Для разработчика: найти свой чат, написав боту поддержки (можно до сохранения токена)."""
    return {"items": notify.recent_chats(token.strip() or support.channel()["token"])}
