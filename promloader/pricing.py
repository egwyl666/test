"""Правила наценки: закупочная цена -> розничная.

Правила проверяются по порядку, срабатывает первое подходящее. Правило может быть привязано
к поставщику, категории (подстрока в группе) и диапазону закупочной цены.
"""

import math

from . import db

ROUNDING = {
    "none": "без округления",
    "int": "до целого вверх",
    "end9": "на 9 (1231 → 1239)",
    "tens": "до десятков вверх",
    "end99": "на 99 (1234 → 1299)",
}

RULE_FIELDS = ("supplier_id", "category", "cost_from", "cost_to", "markup_percent", "markup_fixed", "rounding", "use_rrp")


def round_price(value: float, mode: str) -> float:
    if mode == "int":
        return float(math.ceil(value - 1e-9))
    if mode == "tens":
        return float(math.ceil(value / 10 - 1e-9) * 10)
    if mode == "end9":
        whole = math.ceil(value - 1e-9)
        return float(whole + (9 - whole % 10) % 10)
    if mode == "end99":
        whole = math.ceil(value - 1e-9)
        return float(whole + (99 - whole % 100) % 100)
    return round(value, 2)


def list_rules() -> list[dict]:
    rows = db.query("SELECT * FROM price_rules ORDER BY position, id")
    return [dict(r) for r in rows]


def save_rules(rules: list[dict]) -> list[dict]:
    """Полная замена списка правил (порядок = приоритет)."""
    if not isinstance(rules, list):
        raise ValueError("правила должны быть списком")
    known = {r["id"] for r in db.query("SELECT id FROM suppliers")}
    clean = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise ValueError("неверный формат правила")
        if r.get("supplier_id") not in (None, "") and int(r["supplier_id"]) not in known:
            raise ValueError("поставщик из правила не найден — обновите страницу")
        rounding = r.get("rounding") or "none"
        if rounding not in ROUNDING:
            raise ValueError(f"Неизвестное округление: {rounding}")
        num = lambda v: None if v in (None, "") else float(v)  # noqa: E731
        clean.append((
            i,
            int(r["supplier_id"]) if r.get("supplier_id") not in (None, "") else None,
            str(r.get("category") or "").strip(),
            num(r.get("cost_from")),
            num(r.get("cost_to")),
            float(r.get("markup_percent") or 0),
            float(r.get("markup_fixed") or 0),
            rounding,
            1 if r.get("use_rrp") else 0,
        ))
    with db.tx() as c:
        c.execute("DELETE FROM price_rules")
        c.executemany(
            f"INSERT INTO price_rules (position, {', '.join(RULE_FIELDS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", clean
        )
    return list_rules()


def _matches(rule: dict, cost: float, supplier_id: int | None, category: str) -> bool:
    if rule["supplier_id"] is not None and rule["supplier_id"] != supplier_id:
        return False
    if rule["category"] and rule["category"].lower() not in (category or "").lower():
        return False
    if rule["cost_from"] is not None and cost < rule["cost_from"]:
        return False
    if rule["cost_to"] is not None and cost > rule["cost_to"]:
        return False
    return True


class Pricer:
    """Загружает правила один раз — удобно для обработки тысяч товаров подряд."""

    def __init__(self, rules: list[dict] | None = None):
        self.rules = list_rules() if rules is None else rules

    def rule_for(self, cost: float, supplier_id: int | None, category: str) -> dict | None:
        for rule in self.rules:
            if _matches(rule, cost, supplier_id, category):
                return rule
        return None

    def price(self, cost: float | None, rrp: float | None = None, supplier_id: int | None = None,
              category: str = "") -> tuple[float | None, dict | None]:
        """Розничная цена и сработавшее правило. (None, None) — правило не нашлось."""
        if cost is None:
            return None, None
        rule = self.rule_for(cost, supplier_id, category)
        if rule is None:
            return None, None
        if rule["use_rrp"] and rrp:
            return round_price(rrp, rule["rounding"]), rule
        value = cost * (1 + rule["markup_percent"] / 100) + rule["markup_fixed"]
        return round_price(max(value, cost), rule["rounding"]), rule

    def apply(self, data: dict, supplier_id: int | None = None) -> list[str]:
        """Проставляет data['price'] по закупке/РРЦ. Возвращает предупреждения."""
        cost, rrp = data.get("cost_price"), data.get("rrp")
        # «0» в прайсе поставщика — это «цены нет», а не бесплатный товар: иначе наценка с округлением даст 9 грн
        cost = cost if cost and cost > 0 else None
        rrp = rrp if rrp and rrp > 0 else None
        if cost is None and rrp is None:
            return []
        price, rule = self.price(cost, rrp, supplier_id, data.get("group_name", ""))
        if price is not None:
            data["price"] = price
            return []
        if data.get("price") is not None:
            return []
        if rrp is not None:
            data["price"] = rrp
            return ["Нет правила наценки — стоит РРЦ"]
        data["price"] = cost
        return ["Нет правила наценки — цена равна закупочной"]
