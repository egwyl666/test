"""Наценка и курс валют."""


from fastapi import APIRouter, Body, HTTPException

from .. import changes, db, pricing, products, rates, suppliers
from .products import _selected

router = APIRouter()


@router.get("/api/pricing")
def get_pricing():
    return {"rules": pricing.list_rules(), "rounding": pricing.ROUNDING}


@router.put("/api/pricing")
def save_pricing(rules: list[dict] = Body(..., embed=True)):
    try:
        return {"rules": pricing.save_rules(rules), "rounding": pricing.ROUNDING}
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Ошибка в правилах: {exc}")


@router.post("/api/pricing/test")
def test_pricing(body: dict = Body(...)):
    cost = products.parse_number(body.get("cost"))
    rrp = products.parse_number(body.get("rrp"))
    supplier_id = int(body["supplier_id"]) if body.get("supplier_id") else None
    try:
        price, rule = pricing.Pricer().price(cost, rrp, supplier_id, body.get("category") or "", body.get("currency") or "UAH")
    except rates.RateError as exc:
        return {"error": str(exc)}
    return {"price": price, "rule": rule}


@router.post("/api/products/currencies")
def products_currencies(ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    """В какой валюте сейчас цена у выбранных товаров (закупка/РРЦ — в своей валюте, иначе — валюта цены)."""
    ids = _selected(ids, filter)
    if not ids:
        return {}
    rows = db.query(f"""SELECT UPPER(CASE WHEN (cost_price IS NOT NULL OR rrp IS NOT NULL) AND cost_currency != ''
                        THEN cost_currency ELSE COALESCE(NULLIF(currency, ''), 'UAH') END) AS c, COUNT(*) AS n
                        FROM products WHERE id {db.IN_LIST} GROUP BY c""", (db.as_list(int(i) for i in ids),))
    return {("UAH" if r["c"] == "ГРН" else r["c"]): r["n"] for r in rows}


@router.post("/api/products/prices")
def products_prices(action: str = Body(...), value: float = Body(0), currency: str = Body(""),
                          ids: list[int] | None = Body(None), filter: dict | None = Body(None)):
    ids = _selected(ids, filter)
    label = {"as_cost": "опт → закупка", "percent": f"{value:+g}%", "recalc": "пересчёт по наценке"}.get(action, "")
    with changes.source(f"Массово «💲 Цены»: {label}" if label else "Вручную"):
        try:
            return suppliers.bulk_prices(ids, action, value, currency)
        except suppliers.SupplierError as exc:
            raise HTTPException(400, str(exc))


@router.post("/api/pricing/preview")
def preview_pricing(rules: list[dict] | None = Body(None, embed=True)):
    """Сколько цен изменит пересчёт (с правилами из формы, если переданы) и сколько уйдёт на Prom."""
    rates.prefetch()

    def run():
        if rules is not None:
            pricing.save_rules(rules)
        return suppliers.recalc_prices()
    try:
        with changes.source("Пересчёт по наценке"):
            return changes.preview(run)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Ошибка в правилах: {exc}")


@router.post("/api/rates/preview")
def preview_rates(data: dict = Body(...)):
    """Сколько цен изменит новый курс (настройки из формы применяются понарошку)."""
    rates.prefetch(force=True)  # курс НБУ — до пробного прогона, даже если сейчас сохранён «свой» курс

    def run():
        rates.save_settings(data)
        return suppliers.recalc_prices()
    try:
        with changes.source("Курс валют"):
            return changes.preview(run)
    except rates.RateError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/pricing/apply")
def apply_pricing():
    with changes.source("Пересчёт по наценке"):
        result = suppliers.recalc_prices()
    rates.mark_applied()
    return result


@router.get("/api/pricing/low-margin")
def low_margin():
    """Сторож наценки: товары в продаже ниже минимальной наценки или в минус."""
    return {"items": products.low_margin(), "min_margin_pct": products.min_margin_pct()}


@router.post("/api/pricing/min-margin")
def set_min_margin(value: float = Body(..., embed=True)):
    if not -100 < value < 1000:
        raise HTTPException(400, "Минимальная наценка — число процентов, например 10")
    db.set_setting("min_margin_pct", f"{value:g}")
    return low_margin()
