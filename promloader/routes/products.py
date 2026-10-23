"""Товары: список, карточка, фото, массовые действия, журнал."""

import asyncio
from datetime import datetime

from fastapi import (
    APIRouter,
    Body,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import (
    Response,
)

from .. import (
    ai,
    aibulk,
    aicategory,
    aiprice,
    branding,
    changes,
    chat,
    config,
    db,
    duplicates,
    excel,
    pricing,
    products,
    promdelete,
    r2,
    schedule,
    stock,
    suppliers,
    undo,
    updater,
)
from ..web import MAX_UPLOAD, _background
from .suppliers import _run_supplier_bg

router = APIRouter()


@router.get("/api/today")
def today_overview():
    """Главная «Сегодня»: итоги заказов и что требует внимания."""
    from .. import today
    return today.overview()


@router.get("/api/meta")
def meta(request: Request):
    return {
        "access": getattr(request.state, "access", "local"),
        "me": getattr(request.state, "user", None),
        "presence": products.PRESENCE,
        "statuses": products.STATUSES,
        "origin_labels": products.ORIGIN_LABELS,
        "targets": excel.TARGETS,
        "max_images": products.MAX_IMAGES,
        "shop_name": db.get_setting("shop_name"),
        "shop_logo": branding.version(),  # версия логотипа магазина ("" — не загружен)
        "shop_logo_big": branding.version("big"),
        "brand_accent": branding.accent(),  # цвет программы из логотипа ("" — стандартный)
        "prom_token_set": bool(config.get("prom_token")),
        "failed_suppliers": suppliers.failed(),
        "version": updater.current_version(),
        "update_available": db.get_setting("update_latest"),
        "missed_schedules": schedule.missed(),
        "orders_unseen": db.query_one("SELECT COUNT(*) AS n FROM orders WHERE seen = 0")["n"],
        "chat_unseen": chat.unseen(),
        "paused": db.paused(),
        "ai": _ai_meta(),
        "suppliers": [{"id": r["id"], "name": r["name"]} for r in db.query(
            "SELECT id, name FROM suppliers ORDER BY name COLLATE NOCASE")],
        "groups": [r["group_name"] for r in db.query(
            "SELECT DISTINCT group_name FROM products WHERE group_name != '' ORDER BY group_name")],
        # для фильтра списка — только категории, в которых есть наши товары
        "prom_categories": [{"id": r["id"], "caption": r["caption"]} for r in db.query(
            "SELECT id, caption FROM prom_categories WHERE id IN (SELECT prom_category_id FROM products) "
            "OR id IN (SELECT target_category_id FROM products) ORDER BY lower_u(caption), id")],
        "first_steps": _first_steps(),
    }


def _ai_meta() -> dict:
    s = ai.settings()
    today = aiprice.today()
    return {"enabled": ai.enabled(), "provider": s["provider"],
            "model": s["model"] or (db.get_setting("gemini_model_used") if s["provider"] == "gemini" else ""),
            "auto": s["provider"] == "gemini" and not s["model"], "free": s["provider"] == "gemini" and aiprice.free_tier(),
            "today_usd": today["cost_usd"], "today_uah": aiprice.to_uah(today["cost_usd"]), "today_requests": today["requests"],
            "key_error": bool(aiprice.key_errors().get(s["provider"])),
            "actions": {k: v[0] for k, v in ai.ACTIONS.items()},
            "bulk_only": {"match_category": aicategory.LABEL}}  # только в массовом ИИ — не в карточке


def _first_steps() -> dict:
    """Для «Первых шагов» на главной: что уже настроено."""
    if r2.active():
        photos = "r2"
    elif config.public_base_url():
        photos = "site"
    elif config.get("photo_tunnel") != "0":
        photos = "tunnel"
    else:
        photos = "off"
    return {
        "token": bool(config.get("prom_token")),
        "photos": photos,
        "products": db.query_one("SELECT COUNT(*) AS n FROM products")["n"],
        "suppliers": db.query_one("SELECT COUNT(*) AS n FROM suppliers")["n"],
        "sent": db.query_one("SELECT 1 AS x FROM products WHERE synced_at IS NOT NULL LIMIT 1") is not None,
    }


@router.get("/api/products")
def list_products(request: Request, limit: int = 200, offset: int = 0, sort: str = "updated"):
    """Фильтры (все необязательные): status, q, supplier ('' — все, 'none' — без поставщика, число), group ('-' —
    без группы), presence, on_prom (yes/no), no_photo, gone (пропал у поставщика), errors (ошибки заполнения)."""
    return products.list_products(_list_filter(request), sort, max(1, min(limit, 1000)), max(0, offset))


def _list_filter(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in products.FILTER_KEYS}


@router.get("/api/products.xlsx")
def export_products(request: Request, sort: str = "updated"):
    content = products.export_xlsx(_list_filter(request), sort)
    name = f"tovary-{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/api/products/bulk-edit")
def bulk_edit(fields: dict = Body(...), ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    with changes.source("Массово «✏ Изменить»"):
        return products.bulk_edit(_selected(ids, filter), fields)


@router.post("/api/products")
def create_product(data: dict = Body(default={})):
    return products.get(products.create(data))


@router.get("/api/products/alerts")
def products_alerts():
    """Раньше маршрута /api/products/{product_id}. Конфликты с кабинетом Prom и возможные дубли — для полоски над списком."""
    return products.list_alerts()


@router.post("/api/products/duplicates/keep-best")
def duplicates_keep_best():
    """«Оставить выгодный»: в каждой группе дублей остальные — в черновики с пометкой «скрыт как дубль»."""
    stamp = undo.stamp()
    r = duplicates.keep_best(stamp)
    return r | {"undo": undo.remember(r.pop("before"), stamp, "Оставить выгодный")}


@router.post("/api/products/{product_id}/dup-unhide")
def duplicate_unhide(product_id: int):
    return duplicates.unhide(product_id)


@router.post("/api/products/{product_id}/prom-conflict")
def product_conflict(product_id: int, action: str = Body(...), fields: list[str] | None = Body(None)):
    return products.resolve_conflict(product_id, action, fields)


@router.get("/api/products/readiness")
def products_readiness(request: Request):
    """Раньше маршрута /api/products/{product_id}: иначе «readiness» попал бы в него.
    Полоска над черновиками: готовы для Prom и чего не хватает остальным (с фильтрами списка)."""
    return {**products.readiness(_list_filter(request)), "labels": products.READINESS_LABELS}


@router.get("/api/products/{product_id}")
def get_product(product_id: int):
    return products.get(product_id)


@router.patch("/api/products/{product_id}")
def update_product(product_id: int, data: dict = Body(...)):
    """_ai_fields — поля, которые пришли из ИИ в карточке («Применить»): в журнале они записываются как «ИИ»,
    а не «Вручную», — по журналу карточка показывает, где текст писал ИИ."""
    ai_fields = data.pop("_ai_fields", None)
    if isinstance(ai_fields, list):
        ai_part = {k: data.pop(k) for k in [f for f in ai_fields if isinstance(f, str)] if k in data}
        if ai_part:
            with changes.source("ИИ (в карточке)"):
                product = products.update(product_id, ai_part)
            if not data:
                return product
    return products.update(product_id, data)


@router.post("/api/products/{product_id}/ai")
def ai_edit(product_id: int, action: str = Body(...), instruction: str = Body("")):
    """Предложение ИИ по карточке. Ничего не сохраняет: пользователь сам решает, применять ли."""
    product = products.get(product_id)
    changes, used = ai.run_detailed(product, action, instruction)
    return {"changes": changes, "usage": {**used, "cost_uah": aiprice.to_uah(used["cost_usd"])}}


@router.get("/api/ai/status")
def ai_status():
    """Для окна «ИИ» на любой странице: что выбрано, сколько потрачено, лимиты."""
    s = ai.settings()
    keys = {"gemini": bool(ai.keys("gemini")), "claude": bool(ai.keys("claude"))}
    summary = aiprice.summary()
    for period in ("today", "month"):
        summary[period]["cost_uah"] = aiprice.to_uah(summary[period]["cost_usd"])
    return {
        "provider": s["provider"], "providers": ai.PROVIDERS, "keys": keys, "enabled": ai.enabled(),
        "model": s["model"], "model_used": db.get_setting("gemini_model_used") if s["provider"] == "gemini" else s["model"],
        "free_tier": aiprice.free_tier(), "free_tier_setting": db.get_setting("gemini_free_tier"),
        "summary": summary, "limits": aiprice.limits().get(s["provider"], {}),
        "key_error": aiprice.key_errors().get(s["provider"]),
        "pace_per_minute": aibulk.rate_per_minute(), "prices_checked": aiprice.PRICES_CHECKED,
        "key_count": len(s["keys"]), "parallel": aibulk.parallel(), "streams": aibulk.streams(),
        "usd_rate": aiprice.usd_rate(), "actions": {k: v[0] for k, v in ai.ACTIONS.items()},
    }


@router.get("/api/ai/models")
def ai_models():
    try:
        return {"items": ai.list_models()}
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/ai/models/check")
def ai_models_check(models: list[str] = Body(..., embed=True)):
    """Короткий настоящий запрос к каждой модели — какие работают на этом ключе. Стоит доли цента."""
    items = ai.check_models(models)
    spent = round(sum(x["cost_usd"] or 0 for x in items), 6)
    return {"items": items, "spent_usd": spent, "spent_uah": aiprice.to_uah(spent)}


@router.post("/api/ai/model")
def ai_choose(provider: str | None = Body(None), model: str | None = Body(None), free_tier: bool | None = Body(None)):
    """Выбор провайдера и модели из окна «ИИ». Ключи вводятся только в «Настройках»."""
    with db.tx():
        if provider is not None:
            if provider not in ai.PROVIDERS:
                raise HTTPException(400, "Неизвестный провайдер ИИ")
            if not ai.keys(provider):
                raise HTTPException(400, f"Для {ai.PROVIDERS[provider]} не введён ключ — вставьте его в «Настройках»")
            db.set_setting("ai_provider", provider)
        if model is not None:
            current = provider or ai.settings()["provider"]
            if current not in ai.PROVIDERS:
                raise HTTPException(400, "Сначала выберите провайдера ИИ")
            db.set_setting(f"{current}_model", str(model).strip().removeprefix("models/"))
        if free_tier is not None:
            db.set_setting("gemini_free_tier", "1" if free_tier else "0")
    return ai_status()


@router.post("/api/ai/parallel")
def ai_parallel(multi: bool = Body(..., embed=True)):
    """Переключатель «Один поток / Много потоков» массового ИИ (окно «✨ ИИ», окно массового ИИ, «Настройки»)."""
    aibulk.set_parallel(multi)
    return ai_status()


@router.post("/api/ai/bulk/estimate")
def ai_bulk_estimate(action: str = Body(...), ids: list[int] | None = Body(None), filter: dict | None = Body(None),
                     limit: int | None = Body(None, ge=1, le=100000)):
    return aibulk.estimate(len(_selected(ids, filter)), action, limit)


@router.post("/api/ai/check")
def ai_check():
    try:
        return ai.check()
    except ai.AIError as exc:
        return {"ok": False, "models": [], "message": str(exc)}


@router.post("/api/products/{product_id}/unlock")
def unlock_fields(product_id: int, fields: list[str] = Body(..., embed=True)):
    """Снять закрепление: поля снова берутся у поставщика / из правил наценки."""
    products.unlock(product_id, fields)
    suppliers.reapply(product_id)
    if "price" in fields:
        _recalc_one(product_id)
    return products.get(product_id)


def _recalc_one(product_id: int) -> None:
    p = products.get(product_id)
    if p["supplier_id"] or (p["cost_price"] is None and p["rrp"] is None):
        return
    data = {k: p[k] for k in ("cost_price", "rrp", "group_name", "price", "currency")}
    data["cost_currency"] = p["cost_currency"] or p["currency"] or "UAH"
    pricing.Pricer().apply(data)
    if data.get("price") is not None and data["price"] != p["price"]:
        products.update(product_id, {"price": data["price"]}, lock=False)


@router.post("/api/products/{product_id}/duplicate")
def duplicate_product(product_id: int):
    src = products.get(product_id)
    data = {k: src[k] for k in products.EDITABLE if k != "external_id"}
    data["name"] = (src["name"] + " (копия)").strip()
    new_id = products.create(data)
    for img in products.image_rows(product_id):
        if img["url"]:
            products.add_image_url(new_id, img["url"])
            continue
        try:
            content = (db.uploads_dir() / img["file"]).read_bytes()
        except OSError:
            continue  # файл фото пропал с диска — копия без него, а не ошибка посреди копирования
        products.add_image_file(new_id, content)
    return products.get(new_id)


@router.get("/api/suppliers/{supplier_id}/deleted")
def supplier_deleted(supplier_id: int):
    return suppliers.deleted_items(supplier_id)


@router.post("/api/suppliers/{supplier_id}/restore-deleted")
async def supplier_restore_deleted(supplier_id: int, body: dict = Body(default={})):
    """Вернуть удалённые вами товары поставщика: все или skus; run — сразу обновить прайс, чтобы они появились."""
    skus = body.get("skus")
    count = await asyncio.to_thread(suppliers.restore_deleted, supplier_id,
                                    [str(x) for x in skus] if skus is not None else None)
    started = False
    if count and body.get("run", True) and not (await asyncio.to_thread(suppliers.get, supplier_id))["running"]:
        task = asyncio.create_task(_run_supplier_bg(supplier_id, False))
        _background.add(task)
        task.add_done_callback(_background.discard)
        started = True
    return {"count": count, "started": started}


def _selected(ids: list[int] | None, flt: dict | None) -> list[int]:
    """Выбранные товары: список id или «все по фильтру списка» ({status, q, supplier})."""
    if flt is not None:
        return products.ids_for_filter(flt)
    if ids is None:
        raise HTTPException(400, "Не выбраны товары")
    return [int(i) for i in ids]


@router.post("/api/products/delete")
def delete_products(ids: list[int] | None = Body(None), prom: bool = Body(True), filter: dict | None = Body(None)):
    """Удалить товары. prom=True — и на Prom (товар уйдёт из программы, когда Prom подтвердит удаление)."""
    return promdelete.request(_selected(ids, filter), prom)


@router.post("/api/products/delete/check")
def delete_check(ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    return promdelete.check(_selected(ids, filter))


@router.post("/api/products/delete/retry")
def delete_retry(ids: list[int] = Body(..., embed=True)):
    return {"count": promdelete.retry(ids)}


@router.post("/api/products/delete/cancel")
def delete_cancel(ids: list[int] = Body(..., embed=True)):
    return {"count": promdelete.cancel(ids)}


@router.post("/api/products/promote-ready")
def promote_ready(filter: dict | None = Body(None, embed=True)):
    """Все готовые для Prom черновики (по фильтрам списка) — в «Готов к отправке»."""
    ids, stamp = products.ready_drafts(filter or {}), undo.stamp()
    before = undo.snapshot(ids)
    moved = products.promote_ready(ids=ids, stamp=stamp)
    return {"moved": moved, "undo": undo.remember(before, stamp, "В «Готов к отправке»") if moved else None,
            **products.readiness(filter or {})}


@router.post("/api/products/status")
def set_status(status: str = Body(...), ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    chosen, stamp = _selected(ids, filter), undo.stamp()
    before = undo.snapshot(chosen)
    changed = products.set_status(chosen, status, stamp)
    label = UNDO_LABELS.get(status, status)
    return {"ok": True, "changed": changed, "undo": undo.remember(before, stamp, label) if changed else None}


UNDO_LABELS = {"draft": "В черновики", "ready": "Пометить «готов»", "synced": "Вернуть «На Prom»"}


@router.post("/api/products/low-stock-threshold")
def low_stock_threshold(value: str = Body(..., embed=True)):
    """Порог «заканчивается»: остаток не больше него — товар в плашке «📉 Заканчиваются» и в уведомлении."""
    try:
        return {"threshold": stock.set_threshold(value)}
    except stock.StockError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/products/undo/{token}")
def undo_action(token: str):
    """«↩ Отменить» последнего быстрого действия со статусами (несколько секунд после него)."""
    try:
        return {"restored": undo.apply(token)}
    except KeyError:
        raise HTTPException(410, "Отменить уже нельзя — прошло слишком много времени") from None


@router.post("/api/products/{product_id}/images")
async def upload_images(product_id: int, files: list[UploadFile] = File(...)):
    added, errors = [], []
    for f in files:
        content = await f.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            errors.append(f"{f.filename}: файл больше 25 МБ")
            continue
        try:
            added.append(await asyncio.to_thread(products.add_image_file, product_id, content))
        except products.ProductError as exc:
            errors.append(f"{f.filename}: {exc}")
    return {"added": added, "errors": errors, "product": await asyncio.to_thread(products.get, product_id)}


@router.post("/api/products/{product_id}/images/url")
def add_image_url(product_id: int, url: str = Body(..., embed=True)):
    products.add_image_url(product_id, url)
    return products.get(product_id)


@router.delete("/api/products/{product_id}/images/{image_id}")
def delete_image(product_id: int, image_id: int):
    products.delete_image(product_id, image_id)
    return products.get(product_id)


@router.post("/api/products/{product_id}/images/order")
def reorder_images(product_id: int, ids: list[int] = Body(..., embed=True)):
    products.reorder_images(product_id, ids)
    return products.get(product_id)
