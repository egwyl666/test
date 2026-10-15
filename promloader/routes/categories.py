"""Категории каталога Prom."""

import asyncio
from datetime import datetime

from fastapi import (
    APIRouter,
    Body,
    HTTPException,
)
from fastapi.responses import (
    Response,
)

from .. import (
    categories,
    config,
    feed,
    products,
    promcatalog,
    sync,
)
from ..prom_api import PromError
from ..web import _background, log

router = APIRouter()


@router.get("/api/categories")
def categories_overview():
    return {"groups": categories.overview(), "known": categories.listing(), "send": feed.CATEGORY_TAG is not None,
            "mismatch_total": len(categories.mismatch_rows()), "fix": _fix_state()}


def _fix_state() -> dict:
    st = promcatalog.state()
    return {k[4:]: v for k, v in st.items() if k.startswith("cat_")}


@router.post("/api/categories/fix")
async def categories_fix():
    """Исправить категории на Prom импортом Excel (в фоне): сначала 2 товара с проверкой, потом остальные."""
    if promcatalog._lock.locked():
        raise HTTPException(409, "Программа уже работает с каталогом Prom — подождите, пока закончит")
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    task = asyncio.create_task(_fix_categories_bg())
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.sleep(0.05)
    return promcatalog.state()


async def _fix_categories_bg() -> None:
    try:
        await promcatalog.run_fix_categories(sync.make_client)
    except PromError:
        pass
    except Exception as exc:
        log.exception("Сбой исправления категорий на Prom")
        promcatalog._cat_state(running=False, error=f"Внутренняя ошибка: {exc}")


@router.get("/api/categories/mismatch.xlsx")
def categories_mismatch_xlsx():
    name = f"kategorii-dlya-kabineta-{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return Response(categories.mismatch_xlsx(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/api/categories/group")
def categories_set_group(group: str = Body(...), category_id: int | str | None = Body(None)):
    try:
        changed = categories.set_group(group, category_id)
    except products.ProductError as exc:
        raise HTTPException(400, str(exc))
    return {"changed": changed, "groups": categories.overview()}


@router.post("/api/categories/group/preview")
def categories_preview_group(group: str = Body(...), category_id: int | str | None = Body(None)):
    try:
        return categories.preview_group(group, category_id)
    except products.ProductError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/categories/suggest")
def categories_suggest(q: str = ""):
    try:
        return {"items": categories.suggest(q)}
    except categories.CategoryError as exc:
        raise HTTPException(502, str(exc))


@router.post("/api/categories/url")
def categories_from_url(url: str = Body(..., embed=True)):
    try:
        return categories.from_url(url)
    except categories.CategoryError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/categories/refresh")
async def categories_refresh():
    """Только категории с Prom (без загрузки всего каталога): что сейчас стоит у наших товаров."""
    if not config.get("prom_token"):
        raise HTTPException(400, "Сначала укажите API-токен Prom в «Настройках»")
    try:
        async with sync.make_client() as client:
            return await categories.refresh(client)
    except PromError as exc:
        raise HTTPException(502, str(exc))
