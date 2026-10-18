"""Настройки: переменные окружения имеют приоритет над значениями, сохранёнными через веб-интерфейс."""

import logging
import os
import re
import secrets

from . import db

DEFAULT_API_BASE = "https://my.prom.ua/api/v1"

# ключ настройки -> переменная окружения
ENV = {
    "prom_token": "PROM_API_TOKEN",
    "public_base_url": "PUBLIC_BASE_URL",
    "prom_api_base": "PROM_API_BASE",
    "feed_key": "FEED_KEY",
    "ai_provider": "AI_PROVIDER",
    "gemini_key": "GEMINI_API_KEY",
    "gemini_model": "GEMINI_MODEL",
    "anthropic_key": "ANTHROPIC_API_KEY",
    "claude_model": "CLAUDE_MODEL",
    "github_token": "PROMLOADER_GITHUB_TOKEN",
    "update_repo": "PROMLOADER_UPDATE_REPO",
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "photo_storage": "PROMLOADER_PHOTO_STORAGE",
    "r2_account_id": "R2_ACCOUNT_ID",
    "r2_access_key_id": "R2_ACCESS_KEY_ID",
    "r2_secret_access_key": "R2_SECRET_ACCESS_KEY",
    "r2_bucket": "R2_BUCKET",
    "r2_public_url": "R2_PUBLIC_URL",
}


def get(key: str) -> str:
    env_name = ENV.get(key)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    value = db.get_setting(key)
    if not value and key == "prom_api_base":
        return DEFAULT_API_BASE
    if not value and key == "feed_key":
        value = secrets.token_urlsafe(16)
        db.set_setting("feed_key", value)
    return value


def from_env(key: str) -> bool:
    env_name = ENV.get(key)
    return bool(env_name and os.environ.get(env_name))


def public_base_url() -> str:
    return get("public_base_url").rstrip("/")


def mask(token: str) -> str:
    if not token:
        return ""
    return token[:4] + "…" + token[-4:] if len(token) > 10 else "…"


# ---------- секреты в журнале ----------
# httpx пишет в журнал полный адрес запроса: токен бота Telegram (/bot<токен>/), ключи в ссылках прайсов (?key=…),
# адрес push-подписки. Журнал уходит в «Поддержку» с диагностикой — секреты там оставаться не должны.
_SECRET_PATTERNS = [
    (re.compile(r"(/bot)\d+:[\w-]+"), r"\1***"),
    (re.compile(r"([?&](?:key|token|api_key|apikey|access_token|auth|password|pass|secret|sig|signature)=)[^&\s\"']+",
                re.I), r"\1***"),
    (re.compile(r"(/fcm/send/|/wpush/v\d/|/push/v\d/)[^\s\"']+"), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True

