"""Массовый ИИ «🗂 Подобрать группу и категорию Prom».

Только массовое действие (в карточке категория выбирается поиском): ИИ выбирает из того, что уже есть в магазине —
групп товаров и известных категорий Prom (из «⬇ Каталог с Prom» и правил «Категорий»). Свои названия не выдумывает:
ответ, которого нет в списке, отбрасывается."""

import re

from . import ai, db

LABEL = "🗂 Подобрать группу и категорию Prom"
BATCH = 15
LIMIT = 300  # столько вариантов в задании, не больше: самые частые группы и категории магазина
SYSTEM = """Ты раскладываешь товары интернет-магазина на Prom.ua по группам магазина и категориям каталога Prom.
Выбирай только из данных списков, точно как там написано: группу — дословно её названием, категорию — её номером.
Если подходящей группы или категории в списке нет — оставь поле пустым. Ничего не придумывай."""


def known_choices() -> dict:
    """Варианты для подбора: группы магазина (по числу товаров) и известные категории Prom (по использованию)."""
    groups = [r["group_name"] for r in db.query(
        "SELECT group_name, COUNT(*) AS n FROM products WHERE group_name != '' GROUP BY group_name "
        "ORDER BY n DESC, group_name LIMIT ?", (LIMIT,))]
    cats = [dict(r) for r in db.query(
        """SELECT c.id, c.caption, c.path, (SELECT COUNT(*) FROM products p WHERE p.target_category_id = c.id
                  OR p.prom_category_id = c.id) AS n
           FROM prom_categories c WHERE c.caption != '' ORDER BY n DESC, c.caption LIMIT ?""", (LIMIT,))]
    return {"groups": groups, "categories": [{"id": c["id"], "caption": c["caption"], "path": c["path"]} for c in cats]}


def prompt_for(items: list[dict], choices: dict) -> tuple[str, tuple]:
    groups = "\n".join(f"- {g}" for g in choices["groups"]) or "(групп нет)"
    cats = "\n".join(f"- {c['id']}: {c['path'] or c['caption']}" for c in choices["categories"]) or "(категорий нет)"
    blocks = "\n\n".join(f"Товар i{n}:\n{ai._product_block(p)}" for n, p in enumerate(items))
    keys = tuple(f"i{n}_{f}" for n in range(len(items)) for f in ("group", "category"))
    task = (f"Для каждого товара i<номер> выбери: i<номер>_group — группу магазина из списка «Группы» (название "
            f"дословно), i<номер>_category — номер категории Prom из списка «Категории» (только цифры). "
            f"Не подходит ничего — пустая строка. Товаров {len(items)}, каждый — отдельно.")
    return f"Группы магазина:\n{groups}\n\nКатегории Prom:\n{cats}\n\n{blocks}\n\nЗадача: {task}", keys


def changes_for(p: dict, group: str, category: str, choices: dict, only_empty: bool = True) -> dict:
    """Ответ ИИ → изменения товара. Только варианты из списка; «только где пусто» — не трогаем заполненное."""
    changes = {}
    by_lower = {g.lower(): g for g in choices["groups"]}
    g = by_lower.get(str(group or "").strip().lower())
    if g and g != (p.get("group_name") or "") and (not only_empty or not (p.get("group_name") or "").strip()):
        changes["group_name"] = g
    digits = re.sub(r"\D", "", str(category or ""))
    ids = {c["id"] for c in choices["categories"]}
    if digits and int(digits) in ids and int(digits) != p.get("target_category_id") \
            and (not only_empty or not p.get("target_category_id")):
        changes["target_category_id"] = int(digits)
    return changes


def match(items: list[dict], choices: dict | None = None, transport=None,
                     only_empty: bool = True) -> tuple[list, dict]:
    """Пачка товаров одним запросом → для каждого изменения {group_name, target_category_id} или AIError."""
    choices = choices or known_choices()
    s = ai.settings()
    if not s["provider"] or not s["key"]:
        raise ai.AIError("ИИ не подключён: выберите провайдера и вставьте ключ в «Настройках»")
    prompt, keys = prompt_for(items, choices)
    call = ai._gemini if s["provider"] == "gemini" else ai._claude
    result, used = call(s, prompt, transport, "match_category", system=SYSTEM, fields=keys)
    out = []
    for n, p in enumerate(items):
        changes = changes_for(p, result.get(f"i{n}_group"), result.get(f"i{n}_category"), choices, only_empty)
        out.append(changes or ai.AIError("ИИ не нашёл в списке подходящей группы или категории"))
    return out, used
