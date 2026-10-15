"""Массовый ИИ."""


from fastapi import APIRouter, Body

from .. import aibulk
from .products import _selected

router = APIRouter()


@router.get("/api/ai/bulk")
def ai_bulk_jobs():
    return {"items": aibulk.list_jobs(), "rate": aibulk.rate_per_minute()}


@router.post("/api/ai/bulk")
def ai_bulk_create(action: str = Body(...), instruction: str = Body(""), only_empty: bool = Body(True),
                         ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return aibulk.create(_selected(ids, filter), action, instruction, only_empty)


@router.post("/api/ai/bulk/{job_id}/status")
def ai_bulk_status(job_id: int, status: str = Body(..., embed=True)):
    return aibulk.set_status(job_id, status)


@router.post("/api/ai/bulk/{job_id}/revert")
def ai_bulk_revert(job_id: int):
    return {"restored": aibulk.revert(job_id)}
