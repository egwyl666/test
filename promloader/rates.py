"""Курсы валют: закупка в $/€ -> розничная цена в гривнах.

Закупка хранится в своей валюте (2.50 $), розничная цена считается в гривнах по текущему курсу. Поэтому когда
курс меняется, программа пересчитывает цены сама (см. main.rates_worker) и, если включено, отправляет новые
цены на Prom быстрым обновлением.

Курс — один на всю программу («Наценка» → «Курс валют»): по НБУ (раз в день, плюс надбавка в %) или свой.
У поставщика можно задать его собственный курс («по курсу 41.8»). Если НБУ недоступен — берётся последний
известный курс, чтобы цены не останавливались из-за сайта банка.
"""

import json
import logging
from datetime import date

import httpx

from . import db

log = logging.getLogger("promloader.rates")

NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
CURRENCIES = ("USD", "EUR", "PLN", "GBP")
SIGN = {"USD": "$", "EUR": "€", "PLN": "zł", "GBP": "£", "UAH": "грн"}
LOCAL = ("UAH", "ГРН", "")


class RateError(Exception):
    pass


# ---------- курс НБУ ----------

def _cached(code: str) -> dict | None:
    raw = db.get_setting(f"nbu_rate_{code}")
    return json.loads(raw) if raw else None


def nbu(code: str, transport=None) -> dict:
    """{"rate": 41.23, "date": "2026-10-04", "stale": False} — курс НБУ гривны за 1 единицу валюты."""
    code = code.upper()
    cached = _cached(code)
    today = date.today().isoformat()
    if cached and cached["date"] == today:
        return {**cached, "stale": False}
    try:
        with httpx.Client(timeout=20, transport=transport) as client:
            r = client.get(NBU_URL, params={"valcode": code, "json": ""})
            r.raise_for_status()
            rate = float(r.json()[0]["rate"])
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        if cached:
            log.warning("НБУ недоступен (%s) — беру курс %s за %s", exc, code, cached["date"])
            return {**cached, "stale": True}
        raise RateError(f"Не удалось получить курс {code} НБУ ({exc}). Укажите свой курс на странице «Наценка»")
    value = {"rate": rate, "date": today}
    db.set_setting(f"nbu_rate_{code}", json.dumps(value))
    return {**value, "stale": False}


# ---------- настройки ----------

def settings() -> dict:
    manual = json.loads(db.get_setting("rate_manual") or "{}")
    return {
        "mode": db.get_setting("rate_mode") or "nbu",                     # nbu | manual
        "add": float(db.get_setting("rate_add") or 0),                    # надбавка к курсу НБУ, %
        "manual": {k: float(v) for k, v in manual.items() if v},          # свой курс по валютам
        "auto_send": db.get_setting("rate_auto_send") != "0",             # новые цены сразу на Prom
    }


def _number(value, what: str) -> float:
    try:
        return float(str(value if value is not None else 0).replace(",", ".").strip() or 0)
    except ValueError:
        raise RateError(f"{what}: нужно число, например 41.5")


def save_settings(data: dict) -> dict:
    if "mode" in data:
        if data["mode"] not in ("nbu", "manual"):
            raise RateError("Курс: по НБУ или свой")
        db.set_setting("rate_mode", data["mode"])
    if "add" in data:
        add = _number(data["add"], "Надбавка к курсу")
        if not -50 <= add <= 100:
            raise RateError("Надбавка к курсу — от -50 до 100%")
        db.set_setting("rate_add", str(add))
    if "manual" in data:
        manual = {}
        for code, value in (data["manual"] or {}).items():
            if code.upper() not in CURRENCIES:
                raise RateError(f"Неизвестная валюта {code}")
            value = _number(value, f"Курс {code}")
            if value < 0:
                raise RateError("Курс не может быть отрицательным")
            if value:
                manual[code.upper()] = value
        db.set_setting("rate_manual", json.dumps(manual))
    if "auto_send" in data:
        db.set_setting("rate_auto_send", "1" if data["auto_send"] else "0")
    return settings()


# ---------- курс для расчёта цен ----------

class Table:
    """Курсы на один пересчёт: НБУ спрашиваем не чаще раза на валюту, свой курс поставщика — из его настроек."""

    def __init__(self, transport=None):
        self.cfg = settings()
        self.transport = transport
        self.cache: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.suppliers = {r["id"]: dict(r) for r in db.query(
            "SELECT id, rate_mode, rate_value, rate_add FROM suppliers WHERE rate_mode = 'manual' AND rate_value > 0")}

    def general(self, code: str) -> float:
        """Общий курс программы (с надбавкой). RateError — курса нет."""
        code = code.upper()
        if code in self.errors:
            raise RateError(self.errors[code])
        if code not in self.cache:
            try:
                if self.cfg["mode"] == "manual":
                    base = self.cfg["manual"].get(code)
                    if not base:
                        raise RateError(f"Не задан свой курс {code} — укажите его на странице «Наценка»")
                    self.cache[code] = base
                else:
                    self.cache[code] = nbu(code, self.transport)["rate"] * (1 + self.cfg["add"] / 100)
            except RateError as exc:
                self.errors[code] = str(exc)
                raise
        return self.cache[code]

    def rate(self, code: str | None, supplier_id: int | None = None) -> float | None:
        """Гривен за 1 единицу валюты. None — валюта уже гривна."""
        code = (code or "UAH").upper()
        if code in LOCAL:
            return None
        own = self.suppliers.get(supplier_id)
        if own:
            return own["rate_value"] * (1 + (own["rate_add"] or 0) / 100)
        return self.general(code)


def current(transport=None) -> dict:
    """Для страницы «Наценка»: действующий курс по валютам, которые есть у товаров (и всегда USD)."""
    used = {r["c"] for r in db.query("SELECT DISTINCT cost_currency AS c FROM products WHERE cost_currency != ''")}
    table, out = Table(transport), {}
    for code in sorted((used | {"USD"}) - set(LOCAL)):
        info = {"code": code, "sign": SIGN.get(code, code)}
        try:
            info["rate"] = round(table.general(code), 4)
            if table.cfg["mode"] == "nbu":
                n = nbu(code, transport)
                info.update(nbu=n["rate"], date=n["date"], stale=n["stale"])
        except RateError as exc:
            info["error"] = str(exc)
        out[code] = info
    return out


def check_changed(transport=None) -> dict | None:
    """Курс изменился с прошлого пересчёта? Тогда пересчитать цены. Вызывается фоновой задачей раз в час."""
    from . import suppliers

    used = {r["c"] for r in db.query("SELECT DISTINCT cost_currency AS c FROM products WHERE cost_currency != ''")}
    used -= set(LOCAL)
    if not used:
        return None
    table = Table(transport)
    now = {}
    for code in used:
        try:
            now[code] = round(table.general(code), 4)
        except RateError as exc:
            log.warning("Курс %s недоступен: %s", code, exc)
    raw = db.get_setting("rate_applied")
    if not raw:
        # первый запуск после обновления: запоминаем курс как исходный, цены сами не трогаем
        db.set_setting("rate_applied", json.dumps(now))
        return None
    applied = json.loads(raw)
    if not now or all(applied.get(k) == v for k, v in now.items()):
        return None
    result = suppliers.recalc_prices()
    db.set_setting("rate_applied", json.dumps({**applied, **now}))
    log.info("Курс изменился (%s) — пересчитано цен: %s, отправлено на Prom: %s", now, result["changed"], result["queued"])
    return {"rates": now, **result}


def mark_applied(transport=None) -> None:
    """После ручного пересчёта: запомнить курс, по которому посчитаны цены."""
    table, now = Table(transport), {}
    for r in db.query("SELECT DISTINCT cost_currency AS c FROM products WHERE cost_currency != ''"):
        if r["c"].upper() not in LOCAL:
            try:
                now[r["c"].upper()] = round(table.general(r["c"]), 4)
            except RateError:
                pass
    db.set_setting("rate_applied", json.dumps(now))
