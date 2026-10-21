"""Сторожа на стыках: страница ↔ сервер, скрипт ↔ HTML, модуль ↔ модуль, перевод ↔ подстановки.

Такие поломки не видны в тестах одного модуля: переименовали адрес API — сервер работает, страница молча ломается;
убрали поле из HTML — скрипт падает только у пользователя. Тесты читают исходники и сверяют их между собой."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from promloader import users
from promloader.main import PAGES, all_routes

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "promloader" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))
PAGES_HTML = sorted(STATIC.glob("*.html"))

# адрес API в коде страницы: "/api/…", '/api/…' или `/api/…${x}…` (до кавычки или «?»)
API_CALL = re.compile(r"""["'`](/api/[^"'`?\s]*)""")


def _route_patterns():
    pats = []
    for r in all_routes():
        path = getattr(r, "path", "")
        if path.startswith("/api/"):
            rx = re.sub(r"\{[^}]+\}", "[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
            pats.append(re.compile(rf"^{rx}$"))
    return pats


def _calls():
    for f in [*SCRIPTS, *PAGES_HTML]:
        if f.name == "i18n-uk.js":
            continue
        for m in API_CALL.finditer(f.read_text(encoding="utf-8")):
            yield f.name, m.group(1)


def test_every_api_address_used_by_pages_exists_on_the_server():
    """Страница обращается только к тем адресам, которые сервер знает (переименовали обработчик — тест покажет,
    где страница всё ещё зовёт старый адрес)."""
    patterns = _route_patterns()
    paths = [re.sub(r"\{[^}]+\}", "X", getattr(r, "path", "")) for r in all_routes()]
    missing = []
    for name, url in _calls():
        url = re.sub(r"\$\{[^}]*$", "", url)  # подстановка с вложенной строкой (`${a ? `?x` : ""}`) — до неё
        concrete = re.sub(r"\$\{[^}]*\}", "X", url).rstrip("/")
        # кусок адреса из переменной (`delete/${action}`) — подходит, если есть адрес с любым значением на его месте
        loose = re.compile("^" + re.sub(r"\\\$\\\{[^}]*\\\}", "[^/]+", re.escape(url.rstrip("/"))) + "$")
        if not any(p.match(concrete) for p in patterns) and not any(loose.match(p) for p in paths):
            missing.append(f"{name}: {url}")
    assert missing == []


ID_USE = re.compile(r"""(?:\$|getElementById|querySelector)\(\s*["'`]#?([A-Za-z][\w-]*)["'`]\s*\)""")
ID_USE_HASH = re.compile(r"""\$\(\s*["'`]#([A-Za-z][\w-]*)["'`]""")
ID_DEFINED = re.compile(r"""\bid=\\?["']([A-Za-z][\w-]*)\\?["']|\.id\s*=\s*["']([A-Za-z][\w-]*)["']""")


def _page_scripts(html: str) -> list[str]:
    return re.findall(r'<script src="/static/([^"?]+\.js)', html)


def test_every_element_a_script_looks_up_exists_on_its_page():
    """$("#id") в скрипте страницы — элемент есть в её HTML или его создаёт какой-то скрипт. Иначе на этой странице
    скрипт падает при загрузке («Cannot read properties of null»), а заметно это только у пользователя."""
    created = set()
    for f in [*SCRIPTS, *PAGES_HTML]:
        for a, b in ID_DEFINED.findall(f.read_text(encoding="utf-8")):
            created.add(a or b)
    problems = []
    for page in PAGES_HTML:
        html = page.read_text(encoding="utf-8")
        own = {a or b for a, b in ID_DEFINED.findall(html)}
        inline = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        sources = [("inline", inline)] + [(s, (STATIC / s).read_text(encoding="utf-8")) for s in _page_scripts(html)
                                          if s not in ("common.js", "i18n.js", "i18n-uk.js", "scanner.js")]
        for src, code in sources:
            for el in set(ID_USE_HASH.findall(code)):
                if el not in own and el not in created:
                    problems.append(f"{page.name} ← {src}: #{el}")
    assert problems == []


def test_every_page_route_serves_an_existing_file():
    for route, name in PAGES.items():
        assert (STATIC / name).is_file(), route


@pytest.mark.parametrize("module", sorted(p.stem for p in (ROOT / "promloader").glob("*.py")
                                          if p.stem not in ("__init__", "__main__", "tray", "launcher")))
def test_every_module_imports_on_its_own(module):
    """Каждый модуль импортируется сам по себе, в чистом процессе. Ловит круговые импорты, которые в общем прогоне
    тестов не видны (там модули уже загружены в «удачном» порядке)."""
    out = subprocess.run([sys.executable, "-c", f"import promloader.{module}"], cwd=ROOT, capture_output=True,
                         text=True, timeout=60, env={"PROMLOADER_WORKER": "0", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-1500:]


def _dictionary() -> dict:
    text = (STATIC / "i18n-uk.js").read_text(encoding="utf-8")
    body = text[text.index("window.I18N_UK = {") + len("window.I18N_UK = "):text.rindex("}") + 1]
    return json.loads(body)


def test_translations_keep_placeholders_and_emoji():
    """В переводе столько же подстановок {}, сколько в русском, и те же значки: иначе в украинском интерфейсе
    пропадёт число («Товарів: » без количества) или кнопка потеряет значок, по которому её ищут глазами."""
    emoji = re.compile(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]")
    bad = []
    for ru, uk in _dictionary().items():
        if ru.count("{}") != uk.count("{}"):
            bad.append(f"{{}}: {ru!r} → {uk!r}")
        if sorted(emoji.findall(ru)) != sorted(emoji.findall(uk)):
            bad.append(f"значки: {ru!r} → {uk!r}")
    assert bad == []


def test_ukrainian_dictionary_has_no_russian_only_letters():
    """ы, э, ъ, ё в украинском переводе — недопереведённая строка (кроме имён файлов и папок, которые не переводятся)."""
    bad = [f"{ru!r} → {uk!r}" for ru, uk in _dictionary().items() if re.search(r"[ыэъёЫЭЪЁ]", uk) and uk != ru]
    assert bad == []


def test_every_permission_rule_points_to_a_real_route():
    """Строка в таблице прав, которой не соответствует ни один адрес, — опечатка: настоящий адрес тогда защищён
    правилом «по умолчанию», а не тем, что задумано."""
    paths = [getattr(r, "path", "") for r in all_routes()]
    concrete = [re.sub(r"\{[^}]+\}", "1", p) for p in paths]
    dead = [prefix for prefix, _, _ in users.RULES
            if not any(users.rule_matches(prefix, p) for p in concrete + paths)]
    assert dead == []
