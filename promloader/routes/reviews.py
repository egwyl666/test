"""Отзывы о магазине и сообщения с сайта Prom (2.31.0)."""

import asyncio

from fastapi import APIRouter, Body, HTTPException, Request

from .. import ai, db, reviews, sync
from ..prom_api import PromError

router = APIRouter()


@router.get("/api/reviews")
def reviews_list(filter: str = ""):
    return {"items": reviews.list_reviews(filter), "stats": reviews.stats(), "enabled": reviews.enabled()}


@router.post("/api/reviews/refresh")
async def reviews_refresh():
    """«↻ Проверить» — отзывы (все страницы) и сообщения с сайта сейчас, не дожидаясь получаса."""
    try:
        new = await asyncio.to_thread(reviews.poll_reviews, None, True)
        async with sync.make_client() as client:
            new_messages = await reviews.poll_messages(client)
    except (reviews.ReviewsError, PromError) as exc:
        raise HTTPException(502, str(exc))
    return {"new": new, "new_messages": new_messages, "stats": reviews.stats()}


@router.post("/api/reviews/seen")
def reviews_seen():
    return {"seen": reviews.mark_seen()}


@router.post("/api/reviews/{review_id}/draft")
def reviews_draft(review_id: int, text: str = Body(..., embed=True)):
    return reviews.save_draft(review_id, text)


@router.post("/api/reviews/{review_id}/ai-draft")
def reviews_ai_draft(review_id: int):
    try:
        with ai.usage_source("chat"):
            return reviews.ai_draft(review_id)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/reviews/messages")
def site_messages():
    return {"items": reviews.list_messages(), "unread": reviews.unread_messages(),
            "error": db.get_setting("site_messages_error") or ""}


@router.post("/api/reviews/messages/{message_id}/reply")
async def site_message_reply(message_id: int, request: Request, text: str = Body(..., embed=True)):
    user = getattr(request.state, "user", None) or {}
    try:
        async with sync.make_client() as client:
            return await reviews.reply_message(client, message_id, text, user.get("name") or user.get("login") or "")
    except reviews.ReviewsError as exc:
        raise HTTPException(400, str(exc))
    except PromError as exc:
        raise HTTPException(502, f"Prom не принял ответ: {exc}")


@router.post("/api/reviews/messages/{message_id}/read")
async def site_message_read(message_id: int):
    try:
        async with sync.make_client() as client:
            await reviews.mark_message_read(client, message_id)
    except PromError as exc:
        raise HTTPException(502, str(exc))
    return {"unread": reviews.unread_messages()}


@router.post("/api/reviews/messages/{message_id}/ai-draft")
def site_message_ai_draft(message_id: int):
    try:
        with ai.usage_source("chat"):
            return reviews.ai_message_draft(message_id)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))
