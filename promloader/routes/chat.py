"""Чат с покупателями Prom."""

import asyncio

from fastapi import (
    APIRouter,
    Body,
    HTTPException,
)

from .. import (
    ai,
    chat,
    db,
    sync,
)
from ..prom_api import PromError

router = APIRouter()


@router.get("/api/chat/rooms")
def chat_rooms(q: str = "", unread: bool = False):
    return {"rooms": chat.rooms(q, unread), "unseen": chat.unseen(), "enabled": chat.enabled(),
            "error": db.get_setting("chat_error") or "", "last_poll": db.get_setting("chat_last_poll") or ""}


@router.get("/api/chat/rooms/{room_ident}/messages")
def chat_messages(room_ident: str):
    return chat.messages(room_ident)


@router.post("/api/chat/rooms/{room_ident}/send")
async def chat_send(room_ident: str, text: str = Body(..., embed=True)):
    try:
        async with sync.make_client() as client:
            result = await chat.send(client, room_ident, text)
    except chat.ChatError as exc:
        raise HTTPException(400, str(exc))
    except PromError as exc:
        raise HTTPException(502, f"Prom не принял сообщение: {exc}")
    return {**result, **await asyncio.to_thread(chat.messages, room_ident)}


@router.post("/api/chat/rooms/{room_ident}/read")
async def chat_read(room_ident: str):
    try:
        async with sync.make_client() as client:
            n = await chat.mark_read(client, room_ident)
    except PromError as exc:
        raise HTTPException(502, str(exc))
    return {"read": n, "unseen": await asyncio.to_thread(chat.unseen)}


@router.post("/api/chat/rooms/{room_ident}/ai-draft")
def chat_ai_draft(room_ident: str):
    try:
        with ai.usage_source("chat"):
            return chat.ai_draft(room_ident)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/chat/refresh")
async def chat_refresh():
    if not chat.enabled():
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    try:
        async with sync.make_client() as client:
            new = await chat.poll(client)
    except PromError as exc:
        raise HTTPException(502, str(exc))
    return {"new": new, "unseen": await asyncio.to_thread(chat.unseen)}
