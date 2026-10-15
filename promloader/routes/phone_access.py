"""Доступ с телефона: вход по паролю, настройки, QR-код."""


from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import phone, runtime
from ..web import _page, log

router = APIRouter()


def _this_computer_only(request: Request) -> None:
    """Пароль и сам доступ меняются только на компьютере: вошедший телефон не может их поменять или отключить."""
    if getattr(request.state, "access", "local") != "local":
        raise HTTPException(403, "Доступ с телефона настраивается только на самом компьютере")


@router.get("/login", include_in_schema=False)
def login_page():
    return _page("login.html")()


@router.post("/api/login")
def login(request: Request, data: dict = Body(...)):
    if getattr(request.state, "access", "local") == "local":
        return {"ok": True, "next": phone.safe_next(data.get("next"))}  # с этого компьютера пароль не нужен
    if not phone.password_set():
        raise HTTPException(400, "Доступ с телефона не настроен")
    who = phone.client_key(request)
    if phone.limiter.blocked(who):
        raise HTTPException(429, "Слишком много неудачных попыток — подождите 15 минут")
    if not phone.check_password(str(data.get("password") or "")):
        phone.limiter.failed(who)
        log.warning("Неверный пароль входа с телефона (%s)", who)
        raise HTTPException(400, "Неверный пароль")
    response = JSONResponse({"ok": True, "next": phone.safe_next(data.get("next"))})
    secure = request.url.scheme == "https" or "cf-connecting-ip" in request.headers  # через Cloudflare — https
    response.set_cookie(phone.COOKIE, phone.make_session(), max_age=phone.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=secure)
    return response


@router.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(phone.COOKIE)
    return response


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
    _this_computer_only(request)
    phone.sign_out_everywhere()
    return {"ok": True}


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
