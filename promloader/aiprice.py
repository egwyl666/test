"""Цены моделей ИИ и учёт запросов: сколько стоил каждый запрос, сколько потрачено за день и месяц, лимиты.

Цены — долларов за 1 млн токенов (вход, выход), проверены по страницам цен Anthropic и Google на PRICES_CHECKED.
Размышления модели (thinking) оплачиваются как выходные токены — они входят в «выход».
Неизвестная модель оценивается по семейству (flash-lite / flash / pro, haiku / sonnet / opus) с пометкой «≈».
"""

import json
import re
import threading
from datetime import date, datetime, timedelta, timezone

from . import db

PRICES_CHECKED = "2026-10-09"
PRICES = {
    # Anthropic (claude-api, цены первой стороны)
    "claude-fable-5-1": (10.0, 50.0), "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0), "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1.0, 5.0),
    # Google Gemini (ai.google.dev/gemini-api/docs/pricing, ставки до 31.12.2026)
    "gemini-3.8-flash": (0.75, 3.75), "gemini-3.7-flash": (0.75, 3.75), "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00), "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50), "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-pro": (1.25, 10.00), "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-flash-lite": (0.10, 0.40),
}
# семейство → цена для моделей, которых нет в таблице (берём самую свежую известную модель семейства)
FAMILY = [
    (re.compile(r"flash-lite"), PRICES["gemini-3.5-flash-lite"]),
    (re.compile(r"gemini.*flash"), PRICES["gemini-3.8-flash"]),
    (re.compile(r"gemini.*pro"), PRICES["gemini-3.1-pro-preview"]),
    (re.compile(r"claude-haiku"), PRICES["claude-haiku-5-5"]),
    (re.compile(r"claude-sonnet"), PRICES["claude-sonnet-5-5"]),
    (re.compile(r"claude-opus"), PRICES["claude-opus-5-5"]),
    (re.compile(r"claude-(fable|mythos)"), PRICES["claude-fable-5-1"]),
]
# типичный запрос карточки (по замерам: данные товара + задача → JSON с описанием)
TYPICAL_IN, TYPICAL_OUT = 1500, 900


def price(model: str) -> dict:
    """{'input', 'output' ($ за 1 млн), 'exact'} или {'input': None, ...} — цена неизвестна."""
    model = (model or "").removeprefix("models/")
    base = re.sub(r"-\d{3}$|-\d{8}$", "", model)  # gemini-2.5-flash-001, claude-…-20260101
    if model in PRICES or base in PRICES:
        inp, out = PRICES.get(model) or PRICES[base]
        return {"input": inp, "output": out, "exact": True}
    for pattern, (inp, out) in FAMILY:
        if pattern.search(model):
            return {"input": inp, "output": out, "exact": False}
    return {"input": None, "output": None, "exact": False}


def free_tier() -> bool:
    """Бесплатный ключ Gemini: отмечен вручную или узнан из ответа Google о лимите («FreeTier»)."""
    return db.get_setting("gemini_free_tier") == "1"


def cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> float | None:
    if provider == "gemini" and free_tier():
        return 0.0
    p = price(model)
    if p["input"] is None:
        return None
    return round((tokens_in * p["input"] + tokens_out * p["output"]) / 1_000_000, 6)


def typical_cost(provider: str, model: str) -> float | None:
    """≈ цена одного запроса по карточке — для списка моделей, пока своих замеров нет."""
    return cost(provider, model, TYPICAL_IN, TYPICAL_OUT)


# ---------- учёт ----------

def record(provider: str, model: str, action: str, source: str, tokens_in: int = 0, tokens_out: int = 0,
           ok: bool = True, error: str = "", job_id: int | None = None, key_error: bool = False,
           retry_after: float | None = None) -> dict:
    """key_error — сервис не принял ключ: это про ключ, а не про модель, статус модели не трогаем.
    retry_after — через сколько секунд снова можно (лимит модели): до этого «авто» её не выбирает первой."""
    value = cost(provider, model, tokens_in, tokens_out) if ok else 0.0
    with db.tx() as c:
        c.execute("""INSERT INTO ai_usage (at, provider, model, action, source, job_id, tokens_in, tokens_out, cost_usd,
                                           ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (db.now(), provider, model, action, source, job_id, tokens_in, tokens_out, value, 1 if ok else 0,
                   error[:300]))
    if ok:
        clear_key_error(provider)
    if key_error:
        set_key_error(provider, error)
    elif model:
        set_model_status(provider, model, "ok" if ok else _status_from_error(error), error, retry_after)
    return {"provider": provider, "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": value,
            "free": provider == "gemini" and free_tier()}


def _status_from_error(error: str) -> str:
    """Статус модели по тексту ошибки: unavailable — ключу недоступна, limit — лимит, busy — перегружена."""
    text = error.lower()
    if any(s in text for s in ("недоступна на вашем ключе", "не найдена", "нет доступа")):
        return "unavailable"
    if "лимит" in text or "quota" in text:
        return "limit"
    if "перегруж" in text or "недоступен" in text:
        return "busy"
    return "error"


def _since(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _day_start() -> str:
    local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def _month_start() -> str:
    local = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def _sum(where: str, args=()) -> dict:
    r = db.query_one(f"""SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS usd, COALESCE(SUM(tokens_in), 0) AS tin,
                                COALESCE(SUM(tokens_out), 0) AS tout, SUM(cost_usd IS NULL AND ok = 1) AS unknown
                         FROM ai_usage WHERE {where}""", args)
    return {"requests": r["n"], "cost_usd": round(r["usd"], 6), "tokens_in": r["tin"], "tokens_out": r["tout"],
            "unpriced": r["unknown"] or 0}


def today() -> dict:
    return _sum("at >= ?", (_day_start(),))


def summary() -> dict:
    """Для окна «ИИ»: расходы сегодня / за месяц / по моделям, последние запросы, частота за минуту."""
    by_model = [dict(r) for r in db.query(
        """SELECT provider, model, COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS cost_usd FROM ai_usage
           WHERE at >= ? AND ok = 1 GROUP BY provider, model ORDER BY cost_usd DESC""", (_month_start(),))]
    recent = [dict(r) for r in db.query("SELECT * FROM ai_usage ORDER BY id DESC LIMIT 10")]
    return {
        "today": today(),
        "month": _sum("at >= ?", (_month_start(),)),
        "last_minute": _sum("at >= ?", (_since(timedelta(minutes=1)),))["requests"],
        "by_model": by_model,
        "recent": recent,
    }


def job_spent(job_id: int) -> dict:
    return _sum("job_id = ? AND ok = 1", (job_id,))


def average_cost(provider: str, model: str, action: str | None = None) -> float | None:
    """Средняя цена последних удачных запросов этой модели (и действия, если хватает замеров)."""
    rows = []
    if action:
        rows = db.query("""SELECT cost_usd FROM ai_usage WHERE provider = ? AND model = ? AND action = ? AND ok = 1
                           AND cost_usd IS NOT NULL ORDER BY id DESC LIMIT 20""", (provider, model, action))
    if len(rows) < 3:
        rows = db.query("""SELECT cost_usd FROM ai_usage WHERE provider = ? AND model = ? AND ok = 1
                           AND cost_usd IS NOT NULL ORDER BY id DESC LIMIT 20""", (provider, model))
    if len(rows) < 3:
        return None
    return sum(r["cost_usd"] for r in rows) / len(rows)


# ---------- статус моделей и лимиты ----------

def model_statuses() -> dict:
    try:
        return json.loads(db.get_setting("ai_model_status") or "{}")
    except ValueError:
        return {}


def set_model_status(provider: str, model: str, status: str, message: str = "", retry_after: float | None = None) -> None:
    data = model_statuses()
    info = {"status": status, "message": message[:300], "at": db.now()}
    if retry_after:
        info["until"] = (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat(timespec="seconds")
    data[f"{provider}:{model}"] = info
    db.set_setting("ai_model_status", json.dumps(data, ensure_ascii=False))


def limited_now(provider: str, model: str) -> bool:
    """У модели исчерпан лимит, и он ещё не сбросился."""
    info = model_statuses().get(f"{provider}:{model}") or {}
    return info.get("status") == "limit" and info.get("until", "") > datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- ключ не принят ----------

def key_errors() -> dict:
    try:
        return json.loads(db.get_setting("ai_key_error") or "{}")
    except ValueError:
        return {}


def set_key_error(provider: str, message: str) -> None:
    data = key_errors()
    data[provider] = {"message": message[:500], "at": db.now()}
    db.set_setting("ai_key_error", json.dumps(data, ensure_ascii=False))


def clear_key_error(provider: str) -> None:
    data = key_errors()
    if provider in data:
        del data[provider]
        db.set_setting("ai_key_error", json.dumps(data, ensure_ascii=False))


def limits() -> dict:
    try:
        return json.loads(db.get_setting("ai_limits") or "{}")
    except ValueError:
        return {}


def save_limits(provider: str, info: dict) -> None:
    data = limits()
    data[provider] = {**info, "at": db.now()}
    db.set_setting("ai_limits", json.dumps(data, ensure_ascii=False))


_usd_fetching = threading.Lock()


def usd_rate() -> float | None:
    """Курс доллара программы для показа цен ИИ в гривнах — без ожидания интернета: свой курс или последний
    сохранённый курс НБУ (с надбавкой). Устаревший курс НБУ обновляется в фоне — окно «ИИ» открывается сразу."""
    from . import rates

    cfg = rates.settings()
    if cfg["mode"] == "manual":
        return cfg["manual"].get("USD") or None
    cached = rates._cached("USD")
    if (not cached or cached.get("date") != date.today().isoformat()) and _usd_fetching.acquire(blocking=False):
        nbu = rates.nbu

        def fetch():
            try:
                nbu("USD")
            except Exception:  # noqa: BLE001 — нет связи: покажем по старому курсу или только в долларах
                pass
            finally:
                _usd_fetching.release()
        threading.Thread(target=fetch, daemon=True).start()
    return cached["rate"] * (1 + cfg["add"] / 100) if cached else None


def to_uah(usd: float | None) -> float | None:
    """В гривнах по курсу доллара программы, если он известен."""
    rate = usd_rate() if usd is not None else None
    return round(usd * rate, 4) if rate else None
