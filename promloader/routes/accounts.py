"""Вход, первый запуск (создание администратора), «Забыли пароль?», свой пароль и пользователи программы."""

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import notify, phone, users
from ..web import _page, log

router = APIRouter()
reset_limiter = phone.LoginLimiter(per_client=5, total=20, what="кода сброса пароля")


def _me(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Нужно войти")
    return user


def _signed_in(request: Request, user: dict, body: dict) -> JSONResponse:
    response = JSONResponse(body)
    users.set_cookie(response, request, user["id"])
    return response


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    if not users.any_users():
        return RedirectResponse("/setup?" + request.url.query if request.url.query else "/setup", 303)
    return _page("login.html")()


@router.get("/setup", include_in_schema=False)
def setup_page(request: Request):
    if users.any_users():
        return RedirectResponse("/login?" + request.url.query if request.url.query else "/login", 303)
    if getattr(request.state, "access", "local") != "local":
        raise HTTPException(403, "Администратора создают на компьютере, где установлена программа")
    return _page("setup.html")()


@router.post("/api/setup")
def setup(request: Request, data: dict = Body(...)):
    if getattr(request.state, "access", "local") != "local":
        raise HTTPException(403, "Администратора создают на компьютере, где установлена программа")
    if str(data.get("password") or "") != str(data.get("password2") or ""):
        raise users.UserError("Пароли не совпадают", "password2")
    user = users.setup_admin(data.get("login"), data.get("password"), data.get("name") or "")
    log.warning("Создан администратор программы «%s»", user["login"])
    return _signed_in(request, user, {"ok": True, "user": user, "next": phone.safe_next(data.get("next"))})


@router.post("/api/login")
def login(request: Request, data: dict = Body(...)):
    if not users.any_users():
        raise HTTPException(400, "Сначала создайте пароль администратора")
    who = phone.client_key(request)
    if phone.limiter.blocked(who):
        raise HTTPException(429, "Слишком много неудачных попыток — подождите 15 минут")
    user = users.authenticate(data.get("login"), data.get("password"))
    if user is None:
        phone.limiter.failed(who)
        log.warning("Неверный логин или пароль (%s, %s)", str(data.get("login") or "")[:40], who)
        raise HTTPException(400, "Неверный логин или пароль")
    return _signed_in(request, user, {"ok": True, "user": user, "next": phone.safe_next(data.get("next"))})


@router.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(users.COOKIE)
    return response


@router.post("/api/password/reset-code")
def reset_code(request: Request):
    """«Забыли пароль?»: код — файлом в папке с данными (см. users.request_reset)."""
    who = phone.client_key(request)
    if reset_limiter.blocked(who):
        raise HTTPException(429, "Слишком много попыток — подождите 15 минут")
    reset_limiter.failed(who)  # каждый запрос кода — попытка: иначе файл можно перезаписывать без конца
    path = users.request_reset()
    log.warning("Запрошен код сброса пароля (%s)", who)
    return {"ok": True, "file": users.RESET_FILE, "folder": str(users.reset_file().parent),
            "minutes": users.RESET_MINUTES, "path": path if getattr(request.state, "access", "") == "local" else ""}


@router.post("/api/password/reset")
def reset_password(request: Request, data: dict = Body(...)):
    who = phone.client_key(request)
    if reset_limiter.blocked(who):
        raise HTTPException(429, "Слишком много попыток — подождите 15 минут")
    if str(data.get("password") or "") != str(data.get("password2") or ""):
        raise users.UserError("Пароли не совпадают", "password2")
    try:
        user = users.reset_with_code(data.get("login"), data.get("code"), data.get("password"),
                                     from_this_computer=phone.from_this_computer(request))
    except users.UserError:
        reset_limiter.failed(who)
        raise
    log.warning("Пароль пользователя «%s» сброшен по коду из файла (%s)", user["login"], who)
    notify.send("phone", f"🔐 Prom Loader: пароль пользователя «{user['login']}» сброшен по коду из папки с данными. "
                         "Если это не вы — проверьте, кто имеет доступ к компьютеру.")
    return _signed_in(request, user, {"ok": True, "user": user, "next": "/"})


@router.get("/api/me")
def me(request: Request):
    return _me(request)


@router.post("/api/me/password")
def my_password(request: Request, data: dict = Body(...)):
    user = _me(request)
    if str(data.get("new") or "") != str(data.get("new2") or ""):
        raise users.UserError("Пароли не совпадают", "new2")
    user = users.change_password(user["id"], data.get("old"), data.get("new"))
    # прежние входы (другой телефон) больше не действуют, а этот — продолжаем с новой подписью
    return _signed_in(request, user, {"ok": True, "user": user})


@router.get("/api/users")
def list_users():
    return {"items": users.list_users(), "roles": users.roles(), "min_password": users.MIN_PASSWORD,
            "perms": [{"id": k, "label": users.PERM_LABELS[k], "hint": users.PERM_HINTS[k]} for k in users.EDITABLE_PERMS]}


@router.post("/api/roles")
def create_role(request: Request, data: dict = Body(...)):
    role = users.create_role(data.get("label"), data.get("perms") or [])
    log.warning("«%s» создал роль «%s»: %s", _me(request)["login"], role["label"], ", ".join(role["perms"]))
    return role


@router.patch("/api/roles/{role_id}")
def update_role(request: Request, role_id: str, data: dict = Body(...)):
    role = users.update_role(role_id, data)
    log.warning("«%s» изменил роль «%s»: %s", _me(request)["login"], role["label"], ", ".join(role["perms"]))
    return role


@router.delete("/api/roles/{role_id}")
def delete_role(request: Request, role_id: str):
    users.delete_role(role_id)
    log.warning("«%s» удалил роль %s", _me(request)["login"], role_id)
    return {"ok": True}


@router.post("/api/users")
def create_user(request: Request, data: dict = Body(...)):
    user = users.create(data.get("login"), data.get("password"), data.get("role") or "viewer", data.get("name") or "")
    log.warning("«%s» создал пользователя «%s» (%s)", _me(request)["login"], user["login"], user["role"])
    return user


@router.patch("/api/users/{user_id}")
def update_user(request: Request, user_id: int, data: dict = Body(...)):
    actor = _me(request)
    user = users.update(user_id, data, actor_id=actor["id"])
    log.warning("«%s» изменил пользователя «%s»: %s", actor["login"], user["login"],
                ", ".join(k for k in data if k != "password") + (", пароль" if data.get("password") else ""))
    return user


@router.delete("/api/users/{user_id}")
def delete_user(request: Request, user_id: int):
    actor = _me(request)
    users.delete(user_id, actor_id=actor["id"])
    log.warning("«%s» удалил пользователя №%d", actor["login"], user_id)
    return {"ok": True}
