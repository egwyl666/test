"""Общее для main.py и обработчиков в routes/: журнал, фоновые задачи, отдача страниц."""

import asyncio
import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import HTMLResponse

from . import updater

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
log = logging.getLogger("promloader")
_background: set[asyncio.Task] = set()  # фоновые задачи: держим ссылки, иначе сборщик мусора их остановит

_ASSET = re.compile(r'((?:src|href)="/static/[^"?]+\.(?:js|css))"')


@lru_cache(maxsize=32)
def _page_html(name: str, mtime_ns: int, version: str) -> str:
    """Ссылки на скрипты и стили — с номером версии: после обновления у них новые адреса, и браузер не может
    подставить файлы прошлой версии из кеша, даже если они там ещё лежат."""
    html = (STATIC / name).read_text(encoding="utf-8")
    return _ASSET.sub(lambda m: f'{m.group(1)}?v={quote(version)}"', html)


def _page(name: str):
    def handler():
        path = STATIC / name
        html = _page_html(name, path.stat().st_mtime_ns, updater.current_version())
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
    return handler
