"""Настройки: переменные окружения имеют приоритет над значениями, сохранёнными через веб-интерфейс."""

import os
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
