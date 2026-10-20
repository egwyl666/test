"""Сбор русских строк интерфейса — для словаря украинского перевода (static/i18n-uk.js) и теста его полноты.

Переводчик страницы (static/i18n.js) сравнивает с словарём готовый текст на экране: текстовые узлы и атрибуты
title/placeholder/aria-label/alt. Поэтому и здесь строки режутся так же, как их увидит браузер: по тегам HTML,
а подстановки `${…}` (JS) и `{…}` (f-строки Python) становятся `{}` — это шаблон, в который подставится что угодно.

Запуск: python -m promloader.i18n_extract — печатает строки без перевода (их нужно добавить в static/i18n-uk.js).
"""

import ast
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CYR = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
HOLE = ""  # место подстановки внутри шаблона
ATTRS = re.compile(r"""\b(?:title|placeholder|aria-label|alt|label)\s*=\s*(["'])(.*?)\1""", re.S)
TAG = re.compile(r"<[^<>]*>")
SQL = re.compile(r"\s*(SELECT|UPDATE|INSERT|DELETE|CREATE|ALTER|WITH)\s", re.I)

# не интерфейс: служебные модули и куски, которые пользователь не видит как текст программы
PY_SKIP_MODULES = {"i18n_extract.py", "tray.py", "launcher.py", "__main__.py"}
PY_SKIP_NAMES = {
    "SYSTEM", "CHECK_PROMPT", "CHAT_SYSTEM",  # ai.py — задания для модели
    "HINTS", "VIDEO_HINT",               # excel.py — распознавание заголовков колонок
    "_presence_words", "CURRENCY_CODES",  # products.py — распознавание значений из прайсов
    "EXPORT_COLUMNS",                    # products.py — заголовки файла Excel
    "SCHEMA",                            # db.py — схема базы
    "task",                              # ai.py — просьба пользователя для модели
    "XLSX_TABLES", "QUALITY_LABELS",     # reports.py — заголовки листов Excel
    "MISMATCH_HEADERS", "CAT_HEADERS", "COD_WORDS", "NP_DEFAULTS",                  # categories.py — колонки файла для кабинета Prom
}
PY_SKIP_RETURNS = {"build_prompt", "build_batch_prompt"}     # ai.py — что возвращается, уходит модели
PY_SKIP_FUNCS = {"ai_draft",           # chat.py — текст для модели, а не для экрана
                 "build_batch_prompt",  # ai.py — задание для модели
                 "ttn_message",         # chat.py — сообщение покупателю по-украински
                 "_notify_job",         # aibulk.py — текст в Telegram (Telegram — по-русски)
                 "daily_summary_text"}  # reports.py — сообщение в Telegram (Telegram — по-русски)
PY_SKIP_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "send", "notify", "_tg",
                 # регулярные выражения
                 "compile", "search", "match", "fullmatch", "sub", "split", "findall", "finditer",
                 "maketrans", "writerow"}  # writerow — заголовки CSV для Excel остаются русскими


def norm(text: str) -> str:
    return " ".join(text.split())


def _segments(text: str) -> list[str]:
    """Строка/шаблон -> куски текста, как их покажет браузер (по тегам), с `{}` на месте подстановок."""
    out = [m.group(2) for m in ATTRS.finditer(text)]
    for part in TAG.sub("\x00", text).split("\x00"):
        out.append(part)
    result = []
    for part in out:
        part = norm(html.unescape(part).replace(HOLE, "{}"))
        if CYR.search(part):
            result.append(part)
    return result


# ---------- JS ----------

_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")


def _js_literals(src: str) -> list[str]:
    """Тексты строк и шаблонов JS (шаблон — с HOLE на месте `${…}`; вложенные шаблоны — отдельно)."""
    found: list[str] = []
    n = len(src)
    last = ""  # последний значимый символ — чтобы отличить регулярное выражение от деления

    def read_string(i: int, quote: str) -> tuple[str, int]:
        buf = []
        i += 1
        while i < n and src[i] != quote:
            if src[i] == "\\" and i + 1 < n:
                nxt = src[i + 1]
                buf.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                i += 2
                continue
            buf.append(src[i])
            i += 1
        return "".join(buf), i + 1

    def read_template(i: int) -> tuple[str, int]:
        buf = []
        i += 1
        while i < n and src[i] != "`":
            if src[i] == "\\" and i + 1 < n:
                buf.append({"n": "\n", "t": "\t"}.get(src[i + 1], src[i + 1]))
                i += 2
            elif src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                i = read_code(i + 2, "}")
                buf.append(HOLE)
            else:
                buf.append(src[i])
                i += 1
        return "".join(buf), i + 1

    def read_code(i: int, stop: str | None) -> int:
        nonlocal last
        depth = 0
        while i < n:
            ch = src[i]
            if stop and ch == stop and depth == 0:
                return i + 1
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
            if ch in "'\"":
                text, i = read_string(i, ch)
                found.append(text)
                last = "a"
                continue
            if ch == "`":
                text, i = read_template(i)
                found.append(text)
                last = "a"
                continue
            if src.startswith("//", i):
                i = src.find("\n", i)
                i = n if i < 0 else i
                continue
            if src.startswith("/*", i):
                i = src.find("*/", i + 2)
                i = n if i < 0 else i + 2
                continue
            if ch == "/" and (last in _REGEX_BEFORE or last == "" or re.search(r"\b(return|typeof|case)\s*$", src[max(0, i - 8):i])):
                j, in_class = i + 1, False
                while j < n and (src[j] != "/" or in_class) and src[j] != "\n":
                    if src[j] == "\\":
                        j += 1
                    elif src[j] == "[":
                        in_class = True
                    elif src[j] == "]":
                        in_class = False
                    j += 1
                i = j + 1
                last = "a"
                continue
            if not ch.isspace():
                last = ch
            i += 1
        return i

    read_code(0, None)
    return found


def js_strings(src: str) -> set[str]:
    return {seg for lit in _js_literals(src) for seg in _segments(lit)}


def html_strings(src: str) -> set[str]:
    """Текст и атрибуты страницы (вне <script>) + строки встроенных скриптов."""
    out: set[str] = set()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
    markup = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", "", src, flags=re.S)
    out |= set(_segments(markup))
    for code in scripts:
        out |= js_strings(code)
    return out


# ---------- Python ----------

def _fstring(node: ast.JoinedStr) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        else:
            parts.append(HOLE)
    return "".join(parts)


def py_strings(src: str) -> set[str]:
    tree = ast.parse(src)
    skip: set[int] = set()
    for node in ast.walk(tree):
        # докстроки
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if isinstance(node, ast.FunctionDef) and node.name in PY_SKIP_FUNCS:
            skip |= {id(x) for x in ast.walk(node) if x is not node}
        if isinstance(node, ast.FunctionDef) and node.name in PY_SKIP_RETURNS:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    skip |= {id(x) for x in ast.walk(sub)}
        # проверки (`"…" in text`, assert) — не текст на экране
        if isinstance(node, ast.Assert) or (isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn, ast.Eq, ast.NotEq)) for op in node.ops)):
            skip |= {id(x) for x in ast.walk(node)}
        # служебные константы и вызовы журнала/Telegram
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in PY_SKIP_NAMES for t in targets):
                skip |= {id(x) for x in ast.walk(node)}
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name in PY_SKIP_CALLS:
                skip |= {id(x) for x in ast.walk(node)}
        # задания для модели в ai.ACTIONS: подпись кнопки — интерфейс, текст задания — нет
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ACTIONS" for t in node.targets) \
                and isinstance(node.value, ast.Dict):
            for v in node.value.values:
                if isinstance(v, ast.Tuple) and len(v.elts) > 1:
                    skip |= {id(x) for x in ast.walk(v.elts[1])}
    out: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                skip.add(id(v))
            out |= set(_segments(_fstring(node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r"\\[sdwb]|\(\?[:=!<]", node.value):
                continue  # регулярное выражение, а не текст
            if SQL.match(node.value):
                # запрос к базе; строки в нём («message = 'отменено'») пользователь потом видит
                out |= {seg for lit in re.findall(r"'([^']*)'", node.value) for seg in _segments(lit)}
                continue
            text = re.sub(r"\{[^{}]*\}", HOLE, node.value) if "{" in node.value else node.value
            out |= set(_segments(text))
    return out


def collect() -> dict[str, set[str]]:
    """{строка: файлы, где она встречается}."""
    found: dict[str, set[str]] = {}

    def add(strings, where):
        for s in strings:
            found.setdefault(s, set()).add(where)

    for path in sorted(STATIC.glob("*.html")):
        add(html_strings(path.read_text(encoding="utf-8")), path.name)
    for path in sorted(STATIC.glob("*.js")):
        # preview.js — витрина Prom со своим переключателем RU/UA, на странице она внутри translate="no"
        if path.name.startswith("i18n") or path.name == "preview.js":
            continue
        add(js_strings(path.read_text(encoding="utf-8")), path.name)
    for path in sorted([*ROOT.glob("*.py"), *ROOT.glob("routes/*.py")]):  # routes/ — обработчики API по разделам
        if path.name in PY_SKIP_MODULES:
            continue
        add(py_strings(path.read_text(encoding="utf-8")), path.relative_to(ROOT).as_posix())
    return found


def dictionary() -> dict[str, str]:
    """Словарь static/i18n-uk.js: `window.I18N_UK = {JSON};`."""
    text = (STATIC / "i18n-uk.js").read_text(encoding="utf-8")
    start = text.index("{", text.index("window.I18N_UK"))
    return json.loads(text[start:text.rindex("}") + 1])


def missing() -> dict[str, set[str]]:
    """Строки интерфейса, которых нет в словаре: {строка: файлы}."""
    known = dictionary()
    return {s: where for s, where in collect().items() if s not in known}


if __name__ == "__main__":
    import sys

    todo = missing()
    print(f"без перевода: {len(todo)}", file=sys.stderr)
    for s, where in sorted(todo.items()):
        print(json.dumps(s, ensure_ascii=False), "—", ", ".join(sorted(where)))
