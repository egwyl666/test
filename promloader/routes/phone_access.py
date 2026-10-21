"""Доступ с телефона: настройки, QR-код. Вход — routes/accounts.py."""


from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import phone, runtime, users

router = APIRouter()


def _this_computer_only(request: Request) -> None:
    """Пароль и сам доступ меняются только на компьютере: вошедший телефон не может их поменять или отключить."""
    if getattr(request.state, "access", "local") != "local":
        raise HTTPException(403, "Доступ с телефона настраивается только на самом компьютере")


@router.get("/api/phone")
def phone_status(request: Request):
    _this_computer_only(request)
    return phone.status()


@router.post("/api/phone")
def phone_save(request: Request, data: dict = Body(...)):
    _this_computer_only(request)
    lan_changed = phone.update(data)
    restarting = lan_changed and runtime.request_restart(2.0)
    return {**phone.status(), "restarting": restarting, "need_restart": lan_changed and not restarting}


@router.post("/api/phone/sign-out-all")
def phone_sign_out_all(request: Request):
    """Все входы всех пользователей (потерян телефон) — кроме этого компьютера: он сразу входит заново."""
    _this_computer_only(request)
    phone.sign_out_everywhere()
    response = JSONResponse({"ok": True})
    user = getattr(request.state, "user", None)
    if user:
        response.set_cookie(users.COOKIE, users.make_session(user["id"]), max_age=users.SESSION_DAYS * 86400,
                            httponly=True, samesite="lax")
    return response


@router.get("/api/phone/qr.svg")
def phone_qr(request: Request, text: str):
    _this_computer_only(request)
    if len(text) > 300:
        raise HTTPException(400, "Слишком длинная ссылка")
    try:
        svg = phone.qr_svg(text)
    except ImportError:
        raise HTTPException(404, "QR-код недоступен: не установлен компонент segno (перезапустите программу)")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.post("/api/restart")
def restart():
    if not runtime.request_restart():
        raise HTTPException(400, "Программа запущена не через START.bat — перезапустите её вручную")
    return {"restarting": True}
