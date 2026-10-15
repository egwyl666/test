"""Обновления программы, резервные копии, выключение и перезапуск."""

import threading

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .. import backup, cloudbackup, db, phone, r2, runtime, updater

router = APIRouter()


@router.get("/api/update/check")
def update_check():
    return updater.check()


@router.post("/api/update/install")
def update_install():
    version = updater.update()
    restarting = runtime.request_restart()
    return {"version": version, "restarting": restarting}


@router.get("/api/backups")
def list_backups():
    return {"items": backup.list_backups(), "last": db.get_setting("last_backup_at"), "can_restart": runtime.can_restart()}


@router.post("/api/backups")
def create_backup():
    info = backup.create("manual")
    if cloudbackup.enabled():  # в облако — в фоне: копия с фото отправляется минутами
        threading.Thread(target=cloudbackup.after_backup, args=(info,), daemon=True).start()
    return {**info, "cloud": cloudbackup.enabled()}


# облачные пути — до /api/backups/{name}, иначе «cloud» приняли бы за имя копии
@router.get("/api/backups/cloud")
def cloud_backups():
    st = cloudbackup.status()
    if not st["enabled"]:
        return {**st, "items": []}
    try:
        return {**st, "items": cloudbackup.listing()}
    except r2.R2Error as exc:
        return {**st, "items": [], "error": f"Не удалось получить список копий в облаке: {exc}"}


@router.post("/api/backups/cloud")
def cloud_backups_toggle(enabled: bool = Body(..., embed=True)):
    if enabled and not r2.configured():
        raise HTTPException(400, "Сначала подключите хранилище R2 в разделе «Фото для Prom»")
    db.set_setting("backup_cloud", "1" if enabled else "0")
    return cloud_backups()


@router.post("/api/backups/cloud/{name}/restore")
def cloud_backup_restore(name: str):
    try:
        cloudbackup.stage_restore(name)
    except cloudbackup.CloudBackupError as exc:
        raise HTTPException(400, str(exc))
    return {"restarting": runtime.request_restart()}


@router.get("/api/backups/{name}")
def download_backup(name: str):
    path = backup.path_of(name)
    return FileResponse(path, filename=name, media_type="application/zip")


@router.post("/api/backups/{name}/restore")
def restore_backup(name: str):
    backup.stage_restore(name)
    return {"restarting": runtime.request_restart()}


@router.post("/api/backups/upload")
def restore_uploaded_backup(file: UploadFile = File(...)):
    # копия может весить гигабайты (фото) — пишем на диск частями, а не читаем целиком в память
    backup.stage_restore_stream(file.file)
    return {"restarting": runtime.request_restart()}


@router.post("/api/shutdown")
def shutdown(request: Request):
    """Выключение из меню значка у часов. Только с этого компьютера (не с телефона и не через туннель)."""
    if not phone.from_this_computer(request):
        raise HTTPException(403)
    return {"stopping": runtime.request_stop()}
