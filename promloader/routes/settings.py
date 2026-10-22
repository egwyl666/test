"""Настройки программы, проверка подключения, ИИ, R2."""

from datetime import datetime
import json
import re
import secrets

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import Response

from .. import ai, aiprice, branding, config, db, notify, r2, reports, runtime, support, sync, updater
from ..prom_api import DEFAULT_IMPORT_SETTINGS, PromError

router = APIRouter()


def _settings_view() -> dict:
    base = config.public_base_url()
    return {
        "prom_token": config.mask(config.get("prom_token")),
        "prom_token_set": bool(config.get("prom_token")),
        "public_base_url": base,
        "prom_api_base": config.get("prom_api_base"),
        "shop_name": db.get_setting("shop_name"),
        "import_settings": json.dumps(sync.import_settings() or DEFAULT_IMPORT_SETTINGS, ensure_ascii=False, indent=2),
        "feed_url": f"{base or ''}/feed/prom.yml?key={config.get('feed_key')}",
        "locked": {k: config.from_env(k) for k in config.ENV},
        "ai_provider": config.get("ai_provider"),
        "ai_providers": ai.PROVIDERS,
        "gemini_key": config.mask(config.get("gemini_key")),
        "anthropic_key": config.mask(config.get("anthropic_key")),
        "gemini_model": config.get("gemini_model"),
        "gemini_model_used": db.get_setting("gemini_model_used"),
        "claude_model": config.get("claude_model") or ai.DEFAULT_MODELS["claude"],
        "ai_rate": db.get_setting("ai_rate"),
        "telegram_token": config.mask(notify.token()),
        "telegram_event_labels": notify.EVENTS,
        "daily_summary_time": reports.summary_time(),
        "daily_summary_days": reports.summary_days(),
        "support_token": config.mask(support.channel()["token"]),
        "support_chat_id": support.channel()["chat_id"],
        "auto_update": config.get("auto_update") != "0",
        "photo_tunnel": config.get("photo_tunnel") != "0",
        "photo_storage": "r2" if config.get("photo_storage") == "r2" else ("tunnel" if config.get("photo_tunnel") != "0" else "off"),
        "r2_active": r2.active(),
        **{k: r2.settings()[k] for k in r2.FIELDS if k not in r2.SECRET_FIELDS},
        **{k: config.mask(config.get(k)) for k in r2.SECRET_FIELDS},
        "quick_updates": db.get_setting("quick_updates") != "0",
        "quick_updates_error": db.get_setting("quick_updates_error"),
        "github_token": config.mask(config.get("github_token")),
        "update_repo": config.get("update_repo") or updater.DEFAULT_REPO,
        "version": updater.current_version(),
        "dev_checkout": updater.is_dev_checkout(),
        "can_restart": runtime.can_restart(),
    }


@router.get("/api/settings")
def get_settings():
    return _settings_view()


@router.post("/api/settings")
def save_settings(data: dict = Body(...)):
    """Всё или ничего: ошибка в одном поле не оставляет половину настроек сохранённой."""
    with db.tx():
        if data.get("prom_token"):
            db.set_setting("prom_token", data["prom_token"].strip())
        if data.get("telegram_token"):
            db.set_setting("telegram_token", data["telegram_token"].strip())
        if "daily_summary_days" in data:
            try:
                db.set_setting("daily_summary_days", reports.clean_summary_days(data["daily_summary_days"]))
            except reports.ReportError as exc:
                raise HTTPException(400, str(exc))
        if "daily_summary_time" in data:
            try:
                value = reports.clean_summary_time(data["daily_summary_time"])
            except reports.ReportError as exc:
                raise HTTPException(400, str(exc))
            if value != reports.summary_time():
                db.set_setting("daily_summary_time", value)
                # перенесли на время, которое сегодня ещё не наступило, — сегодняшняя сводка придёт в новое время
                if value > datetime.now().strftime("%H:%M"):
                    db.set_setting("daily_summary_sent", "")
        if data.get("support_token"):
            db.set_setting("support_token", data["support_token"].strip())
        if data.get("support_token_clear"):
            db.set_setting("support_token", "")
        if "support_chat_id" in data:
            chat = str(data["support_chat_id"] or "").strip()
            if chat and not chat.lstrip("-").isdigit():
                raise HTTPException(400, "ID чата поддержки — это число")
            db.set_setting("support_chat_id", chat)
        if "quick_updates" in data:
            db.set_setting("quick_updates", "1" if data["quick_updates"] else "0")
            if data["quick_updates"]:
                db.set_setting("quick_updates_error", "")
        if "photo_tunnel" in data:
            db.set_setting("photo_tunnel", "1" if data["photo_tunnel"] else "0")
        if any(k in data for k in r2.FIELDS):
            fresh = {k: str(data.get(k) or "").strip() for k in r2.FIELDS}
            fresh["r2_account_id"] = r2.account_id(fresh["r2_account_id"])
            fresh["r2_public_url"] = r2.public_url(fresh["r2_public_url"])
            try:
                r2.validate(fresh)
            except r2.R2Error as exc:
                raise HTTPException(400, str(exc))
            for key, value in fresh.items():
                if key in data and (value or key not in r2.SECRET_FIELDS):  # пустой ключ = «не менять»
                    db.set_setting(key, value)
        if "photo_storage" in data:
            mode = data["photo_storage"]
            if mode not in ("r2", "tunnel", "off"):
                raise HTTPException(400, "Неизвестный способ передачи фото")
            if mode == "r2" and not r2.configured():
                raise HTTPException(400, "Чтобы выбрать хранилище R2, заполните все поля R2 и нажмите «Проверить»")
            db.set_setting("photo_storage", "r2" if mode == "r2" else "")
            if mode != "r2":
                db.set_setting("photo_tunnel", "1" if mode == "tunnel" else "0")
        if "auto_update" in data:
            db.set_setting("auto_update", "1" if data["auto_update"] else "0")
        if data.get("github_token"):
            db.set_setting("github_token", data["github_token"].strip())
        if "update_repo" in data:
            db.set_setting("update_repo", str(data["update_repo"] or "").strip())
        key_warning = ""
        for key, provider in (("gemini_key", "gemini"), ("anthropic_key", "claude")):
            value = ai.clean_key(data.get(key) or "")
            if value:
                db.set_setting(key, value)
                aiprice.clear_key_error(provider)  # новый ключ — прежняя «ключ не принят» к нему не относится
                key_warning = key_warning or ai.key_warning(provider, value)
        if "ai_provider" in data:
            if data["ai_provider"] not in ("", *ai.PROVIDERS):
                raise HTTPException(400, "Неизвестный провайдер ИИ")
            db.set_setting("ai_provider", data["ai_provider"])
        if "ai_rate" in data:
            rate = str(data["ai_rate"] or "").strip()
            if rate and (not rate.isdigit() or not 1 <= int(rate) <= 1000):
                raise HTTPException(400, "Запросов в минуту: число от 1 до 1000")
            db.set_setting("ai_rate", rate)
        for key in ("gemini_model", "claude_model"):
            if key in data:
                db.set_setting(key, str(data[key] or "").strip().removeprefix("models/"))
        for key in ("public_base_url", "prom_api_base"):
            if key in data:
                url = str(data[key] or "").strip().rstrip("/")
                if url and not re.match(r"^https?://", url):
                    url = "https://" + url  # «shop.example.com» -> «https://shop.example.com»
                if url and not re.match(r"^https?://[^\s/]+\.[^\s]+$|^https?://(localhost|127\.0\.0\.1)(:\d+)?(/\S*)?$", url):
                    raise HTTPException(400, f"Не похоже на адрес сайта: {data[key]}")
                db.set_setting(key, url)
        if "shop_name" in data:
            db.set_setting("shop_name", str(data["shop_name"] or "").strip())
        if "import_settings" in data:
            raw = (data["import_settings"] or "").strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    assert isinstance(parsed, dict)
                except (ValueError, AssertionError):
                    raise HTTPException(400, "Параметры импорта должны быть JSON-объектом")
                db.set_setting("import_settings", json.dumps(parsed, ensure_ascii=False))
            else:
                db.set_setting("import_settings", "")
        if data.get("regenerate_feed_key"):
            db.set_setting("feed_key", secrets.token_urlsafe(16))
    return {**_settings_view(), "ai_key_warning": key_warning}


@router.post("/api/settings/check")
async def check_connection():
    try:
        async with sync.make_client() as client:
            await client.check()
    except PromError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "Подключение к Prom работает"}


# ---------- оформление: логотипы и цвет ----------

@router.post("/api/settings/logo")
def upload_logo(file: UploadFile = File(...), slot: str = "logo"):
    """slot=logo — малый (шапка, вкладка), slot=big — большой (фон страницы входа)."""
    if slot not in branding.SLOTS:
        raise HTTPException(400, "Неизвестный логотип")
    try:
        return {"version": branding.save_logo(file.file.read(branding.MAX_UPLOAD + 1), slot), **branding.public()}
    except branding.BrandingError as exc:
        raise HTTPException(400, str(exc)) from None


@router.delete("/api/settings/logo")
def delete_logo(slot: str = "logo"):
    if slot not in branding.SLOTS:
        raise HTTPException(400, "Неизвестный логотип")
    branding.delete_logo(slot)
    return {"ok": True, **branding.public()}


@router.post("/api/settings/brand-accent")
def brand_accent(on: bool = Body(..., embed=True)):
    """«Цвет программы из логотипа»: включить или вернуть стандартный."""
    db.set_setting("brand_accent_off", "" if on else "1")
    return branding.public()


@router.get("/api/brand")
def brand():
    """Без входа: логотипы и цвет — для страницы входа."""
    return branding.public()


def _logo_response(slot: str):
    data = branding.logo(slot)
    if not data:
        raise HTTPException(404, "Логотип не загружен")
    return Response(data, media_type=branding.media_type(data), headers={"Cache-Control": "no-cache"})


@router.get("/shop-logo.png")
def shop_logo():
    """Без входа: логотип виден и на странице входа. Адрес с ?v=<версия> — после замены браузер возьмёт новый."""
    return _logo_response("logo")


@router.get("/shop-logo-big")
def shop_logo_big():
    return _logo_response("big")
