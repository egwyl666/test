"""Переезд на другой компьютер или сервер и пауза фоновой работы старого компьютера."""

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

from .. import backup, cloudbackup, db, migration, r2, runtime

router = APIRouter()


def _state() -> dict:
    return {"paused": db.paused(), "password_saved": bool(db.get_setting("backup_password")), "r2": r2.configured(),
            "cloud": cloudbackup.enabled(), "can_restart": runtime.can_restart()}


@router.get("/api/migration")
def migration_state():
    return _state()


@router.post("/api/migration/password")
def migration_password(password: str = Body("", embed=True)):
    """Пароль копии на этом компьютере: с ним ежедневные копии в облаке везут ключи зашифрованными. Пусто — забыть."""
    if password:
        migration._check_password(password)
    db.set_setting("backup_password", password)
    return _state()


@router.post("/api/migration/bundle")
def migration_bundle(password: str = Body(...), photos: bool = Body(True), cloud: bool = Body(False),
                     pause: bool = Body(False)):
    try:
        info = migration.create_bundle(password, with_photos=photos)
        if cloud:
            migration.upload_bundle(info["name"])
    except migration.MigrationError as exc:
        raise HTTPException(400, str(exc))
    if pause:  # после снимка: в самом пакете пауза не записана, новый компьютер сразу работает
        db.set_setting("paused", "1")
    return {**info, "cloud": cloud, **_state()}


@router.post("/api/migration/code")
def migration_code(password: str = Body(..., embed=True)):
    try:
        return {"code": migration.connection_code(password)}
    except migration.MigrationError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/migration/connect")
def migration_connect(code: str = Body(...), password: str = Body(...)):
    try:
        migration.connect(code, password)
        items = cloudbackup.listing()
    except migration.MigrationError as exc:
        raise HTTPException(400, str(exc))
    except r2.R2Error as exc:
        raise HTTPException(400, f"Код принят, но облако не ответило: {exc}")
    return {"items": items, **_state()}


@router.post("/api/migration/inspect")
def migration_inspect(file: UploadFile = File(...)):
    try:
        return migration.receive(file.file)
    except migration.MigrationError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/migration/inspect-cloud")
def migration_inspect_cloud(name: str = Body(..., embed=True)):
    try:
        return migration.receive_cloud(name)
    except migration.MigrationError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/migration/apply")
def migration_apply(password: str = Body("", embed=True)):
    try:
        migration.apply(password)
    except (migration.MigrationError, backup.BackupError) as exc:
        raise HTTPException(400, str(exc))
    return {"restarting": runtime.request_restart()}


@router.post("/api/pause")
def set_pause(paused: bool = Body(..., embed=True)):
    db.set_setting("paused", "1" if paused else "0")
    return _state()
