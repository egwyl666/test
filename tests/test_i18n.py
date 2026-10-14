"""Украинский интерфейс: словарь полный, страницы его подключают, переводчик страницы (static/i18n.js) работает."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from promloader import i18n_extract

STATIC = Path(__file__).parent.parent / "promloader" / "static"
NODE = shutil.which("node")


def test_every_interface_string_has_translation():
    """Новая строка в интерфейсе без перевода не останется незаметной: python -m promloader.i18n_extract покажет её."""
    missing = i18n_extract.missing()
    assert not missing, "нет перевода (добавьте в static/i18n-uk.js):\n" + "\n".join(
        f"{json.dumps(s, ensure_ascii=False)} — {', '.join(sorted(w))}" for s, w in sorted(missing.items()))


def test_dictionary_has_no_stale_entries():
    strings = i18n_extract.collect()
    stale = [k for k in i18n_extract.dictionary() if k not in strings]
    assert not stale, f"в словаре строки, которых больше нет в программе: {stale}"


def test_translations_are_ukrainian_and_keep_placeholders():
    for ru, uk in i18n_extract.dictionary().items():
        assert ru.count("{}") == uk.count("{}"), ru
        assert uk.strip(), ru
        if uk != ru:  # имена файлов и заголовки Prom остаются как есть
            assert not re.search(r"[ыэъёЫЭЪЁ]", uk), uk


def test_extractor_skips_what_users_never_see():
    src = '''
import re
SYSTEM = "Ты помощник продавца"
log.info("Запуск поставщика %s", name)
assert ok, "база не открыта"
if "перегруж" in text: pass
c.execute("UPDATE t SET message = 'отменено' WHERE id = ?")
raise ProductError(f"Цена {price} ниже закупки")
'''
    found = i18n_extract.py_strings(src)
    assert found == {"Цена {} ниже закупки", "отменено"}
    js = 'toast(`Удалено: ${n}`); const re = /[а-я]+/; el.innerHTML = `<b>Итог:</b> ${x} <span title="Подсказка">?</span>`;'
    assert i18n_extract.js_strings(js) == {"Удалено: {}", "Итог:", "Подсказка"}


@pytest.mark.parametrize("page", sorted(p.name for p in STATIC.glob("*.html")))
def test_page_loads_translator_in_head(page):
    """Переводчик стоит в <head> до стилей: страница прячется до перевода и не мелькает русским текстом."""
    head = (STATIC / page).read_text(encoding="utf-8").split("</head>")[0]
    assert '<script src="/static/i18n-uk.js"></script>' in head
    assert head.index("/static/i18n-uk.js") < head.index("/static/i18n.js") < head.index("/static/app.css")


def test_pages_served_with_versioned_translator(client):
    html = client.get("/").text
    assert re.search(r'src="/static/i18n-uk\.js\?v=[^"]+"', html)
    assert re.search(r'src="/static/i18n\.js\?v=[^"]+"', html)


def test_language_switch_in_top_bar():
    common = (STATIC / "common.js").read_text(encoding="utf-8")
    assert re.search(r"^renderLangSwitch\(\);", common, re.M)  # рисуется на каждой странице вместе с меню
    assert "I18N.set(" in common


# ---------- переводчик страницы (без браузера: node и заглушка документа) ----------

STUB = """
const store = {LANG};
globalThis.window = globalThis;
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem(k, v) { store[k] = v; } };
globalThis.location = { reload() {} };
globalThis.confirm = globalThis.prompt = globalThis.alert = (m) => m;
globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
globalThis.NodeFilter = { SHOW_TEXT: 4 };
const root = { nodeType: 1, isConnected: true, tagName: "HTML", closest: () => null, getAttribute: () => null,
               querySelectorAll: () => [], classList: { add() {}, remove() {} } };
globalThis.document = { documentElement: root, readyState: "complete", head: { appendChild() {} },
                        createElement: () => ({}), createTreeWalker: () => ({ nextNode: () => null }) };
globalThis.MutationObserver = class { observe() {} };
require(__DICT__);
require(__TRANSLATOR__);
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify({
  out: cases.map((s) => I18N.tr(s)),
  confirm: confirm("Удалить это фото у товара?"),
  locale: uiLocale(),
  lang: document.documentElement.lang ?? null,
  missing: [...I18N.missing],
}));
"""


def run_tr(cases, lang="uk"):
    code = (STUB.replace("{LANG}", json.dumps({"promloader-lang": lang} if lang else {}))
            .replace("__DICT__", json.dumps(str(STATIC / "i18n-uk.js")))
            .replace("__TRANSLATOR__", json.dumps(str(STATIC / "i18n.js"))))
    out = subprocess.run([NODE, "-e", code, json.dumps(cases)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_translator_exact_patterns_and_whitespace():
    cases = {
        "Сохранено ✓": "Збережено ✓",
        "  Сохранено ✓\n  ": "  Збережено ✓\n  ",           # пробелы по краям текстового узла сохраняются
        "Отменить\n      заказ": "Скасувати замовлення",       # перенос строки в HTML — как пробел
        "Новых заказов: 5": "Нових замовлень: 5",
        "Сохранено. Цен изменено: 3": "Збережено. Цін змінено: 3",  # необязательный хвост `${…}` пустой
        # подстановка — тоже фраза программы: переводится и она
        "Не удалось получить заказы: Нет связи с Prom: timeout": "Не вдалося отримати замовлення: Немає зв'язку з Prom: timeout",
        "Поставщик «Кружка синяя»": "Постачальник «Кружка синяя»",  # данные пользователя — как есть
        "по будням в 09:00": "у будні о 09:00",
        "Пн, Ср в 18:30": "Пн, Ср о 18:30",
        "2 ч 30 мин (около 21:14)": "2 год 30 хв (близько 21:14)",
        "Чехол в коробке": "Чехол в коробке",                  # короткий шаблон «{} в {}» не задевает названия
        # пустая подстановка посреди фразы: на экране между кусками один пробел
        ". Цена и наличие обновляются из прайса. Поля, которые вы поменяли руками, помечены":
            ". Ціна й наявність оновлюються з прайсу. Поля, які ви змінили вручну, позначено",
        # куски, склеенные страницей: имя сервиса перед фразой, разделители « · »
        "Prom: Prom отклонил токен: проверьте токен и его права в кабинете":
            "Prom: Prom відхилив токен: перевірте токен і його права в кабінеті",
        "Anthropic Claude — нет ключа": "Anthropic Claude — немає ключа",
        "· в прайсе 6 товаров ·": "· у прайсі 6 товарів ·",
        "08.10, 20:58 · Курс валют (изменён вручную)": "08.10, 20:58 · Курс валют (змінено вручну)",
        "Prom Loader": "Prom Loader",
    }
    res = run_tr(list(cases))
    assert dict(zip(cases, res["out"])) == cases
    assert res["confirm"] == "Видалити це фото в товару?"
    assert res["locale"] == "uk-UA" and res["lang"] == "uk"
    assert res["missing"] == ["Чехол в коробке"]


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_russian_is_default_and_untouched():
    res = run_tr(["Сохранено ✓", "Новых заказов: 5"], lang=None)
    assert res["out"] == ["Сохранено ✓", "Новых заказов: 5"]
    assert res["confirm"] == "Удалить это фото у товара?"
    assert res["locale"] == "ru-RU" and res["missing"] == []
