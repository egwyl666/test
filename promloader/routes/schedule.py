"""Расписание выгрузок и автозапуск."""


from fastapi import APIRouter, Body

from .. import autostart, schedule

router = APIRouter()


@router.get("/api/schedules")
def list_schedules():
    return {"items": schedule.list_schedules(), "missed_actions": schedule.MISSED_ACTIONS, "days": schedule.DAY_NAMES,
            "autostart": {"supported": autostart.supported(), "enabled": autostart.enabled()}}


@router.post("/api/schedules")
def create_schedule(data: dict = Body(default={})):
    return schedule.create(data)


@router.patch("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: int, data: dict = Body(...)):
    return schedule.update(schedule_id, data)


@router.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: int):
    schedule.delete(schedule_id)
    return {"ok": True}


@router.post("/api/schedules/{schedule_id}/resolve")
def resolve_schedule(schedule_id: int, decision: str = Body(..., embed=True)):
    return schedule.resolve(schedule_id, decision)


@router.post("/api/autostart")
def set_autostart(enabled: bool = Body(..., embed=True)):
    return {"enabled": autostart.set_enabled(enabled)}
