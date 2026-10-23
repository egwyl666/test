"""Документы для пользователя не расходятся с программой: ссылки между ними живые, у текущей версии есть раздел
в ЧТО-НОВОГО.md (его показывает окно обновления), и каждый заголовок «## …» там — номер версии."""

import re
from pathlib import Path

from promloader import updater

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")])
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_local_links_point_to_existing_files():
    broken = []
    for doc in DOCS:
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if re.match(r"^[a-z]+:", target):  # https:, mailto:
                continue
            if not (doc.parent / target).exists():
                broken.append(f"{doc.relative_to(ROOT)} → {target}")
    assert broken == []


def test_changelog_has_current_version_and_only_version_headings():
    text = (ROOT / "ЧТО-НОВОГО.md").read_text(encoding="utf-8")
    heads = re.findall(r"^##\s+(\S+)", text, flags=re.M)
    assert heads[0] == updater.current_version(ROOT)  # новая версия — первым разделом
    for h in heads:  # окно обновления сравнивает заголовки как номера версий
        assert re.fullmatch(r"\d+\.\d+\.\d+", h), h
    assert updater._notes(text, "0.0.1").startswith("## " + heads[0])


def test_no_dead_documents_referenced_from_code():
    gone = ["ОТЧЁТ-ПРОВЕРКИ.md"]
    for path in [*ROOT.joinpath("promloader").rglob("*.py"), *ROOT.joinpath("promloader", "static").glob("*.*"),
                 *ROOT.joinpath("tests").glob("*.py"), *DOCS]:
        # история версий описывает прошлое как есть — там старые имена файлов уместны
        if path.name in ("test_docs.py", "ЧТО-НОВОГО.md", "ЧТО-НОВОГО-АРХИВ.md") or path.suffix not in (".py", ".js", ".html", ".md"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not any(g in text for g in gone), path
