"""Курсы валют для пересчёта прайсов поставщиков в гривны.

Курс НБУ берётся раз в день (bank.gov.ua) и запоминается; если НБУ недоступен — используется последний
известный курс, чтобы обновление поставщика не останавливалось из-за сайта банка.
"""

import json
import logging
from datetime import date

import httpx

from . import db

log = logging.getLogger("promloader.rates")

NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
CURRENCIES = ("USD", "EUR", "PLN", "GBP")


class RateError(Exception):
    pass


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
        raise RateError(f"Не удалось получить курс {code} НБУ: {exc}. Укажите курс вручную в настройках поставщика")
    value = {"rate": rate, "date": today}
    db.set_setting(f"nbu_rate_{code}", json.dumps(value))
    return {**value, "stale": False}


def converter(mode: str, currency: str, manual: float, add_percent: float, transport=None):
    """Функция «валюта -> множитель в гривны» по настройкам поставщика; None — не пересчитывать."""
    if mode not in ("manual", "nbu"):
        return None
    if mode == "manual" and not manual:
        raise RateError("Включён пересчёт по своему курсу, но курс не указан")
    factor = 1 + (add_percent or 0) / 100
    cache: dict[str, float] = {}

    def rate_for(code: str | None) -> float | None:
        code = (code or currency or "USD").upper()
        if code in ("UAH", "ГРН"):
            return None
        if code not in cache:
            base = manual if mode == "manual" else nbu(code, transport)["rate"]
            cache[code] = base * factor
        return cache[code]

    return rate_for
