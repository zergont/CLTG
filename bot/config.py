from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Обязательная переменная окружения не задана: {key}")
    return value


def _parse_duration_seconds(value: str) -> int:
    """Разбирает строки вида '10s', '2s' в секунды."""
    value = value.strip()
    if value.endswith("s"):
        return int(value[:-1])
    return int(value)


def _parse_cache_ttl(value: str) -> str:
    value = value.strip().lower()
    if value not in ("5m", "1h"):
        raise RuntimeError(f"CACHE_TTL должен быть 5m или 1h, получено: {value!r}")
    return value


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str
    admin_id: int

    # Anthropic
    anthropic_api_key: str
    anthropic_timeout: int
    anthropic_max_retries: int
    anthropic_concurrency: int

    # Промпт
    system_prompt: str
    default_timezone: str

    # Контекст и саммаризация
    context_budget_tokens: int    # рабочий бюджет контекста (меньше окна модели)
    cache_ttl: str                # время жизни кэша промпта: 5m | 1h
    summary_trigger_tokens: float
    summary_trigger_hours: int
    summary_keep_last: int

    # Лимиты
    max_file_mb: int
    max_log_mb: int

    # Напоминания
    reminder_poll_interval: int   # секунды
    reminder_batch_limit: int
    reminder_lookahead: int       # секунды
    reminder_jitter: int          # секунды
    reminder_default_silent: bool

    # Отладка
    debug_mode: bool
    bug_reports: bool   # отправлять ошибки из логов администратору

    # SearXNG
    searxng_url: str
    search_engine: str  # auto | searxng | native


def load_config() -> Config:
    return Config(
        bot_token=_require("BOT_TOKEN"),
        admin_id=int(_require("ADMIN_ID")),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_timeout=int(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "540")),
        anthropic_max_retries=int(os.getenv("ANTHROPIC_MAX_RETRIES", "3")),
        anthropic_concurrency=int(os.getenv("ANTHROPIC_GLOBAL_CONCURRENCY", "4")),
        system_prompt=os.getenv(
            "SYSTEM_PROMPT",
            "Ты полезный ассистент. Текущее время передаётся в каждом сообщении.",
        ),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow"),
        context_budget_tokens=int(os.getenv("CONTEXT_BUDGET_TOKENS", "300000")),
        cache_ttl=_parse_cache_ttl(os.getenv("CACHE_TTL", "1h")),
        summary_trigger_tokens=float(os.getenv("SUMMARY_TRIGGER_TOKENS", "0.85")),
        summary_trigger_hours=int(os.getenv("SUMMARY_TRIGGER_HOURS", "72")),
        summary_keep_last=int(os.getenv("SUMMARY_KEEP_LAST", "10")),
        max_file_mb=int(os.getenv("MAX_FILE_MB", "20")),
        max_log_mb=int(os.getenv("MAX_LOG_MB", "5")),
        reminder_poll_interval=_parse_duration_seconds(os.getenv("REMINDER_POLL_INTERVAL", "10s")),
        reminder_batch_limit=int(os.getenv("REMINDER_BATCH_LIMIT", "50")),
        reminder_lookahead=_parse_duration_seconds(os.getenv("REMINDER_LOOKAHEAD", "2s")),
        reminder_jitter=_parse_duration_seconds(os.getenv("REMINDER_JITTER", "2s")),
        reminder_default_silent=bool(int(os.getenv("REMINDER_DEFAULT_SILENT", "1"))),
        debug_mode=bool(int(os.getenv("DEBUG_MODE", "0"))),
        bug_reports=bool(int(os.getenv("BUG_REPORTS", "1"))),
        searxng_url=os.getenv("SEARXNG_URL", "http://localhost:8888"),
        search_engine=os.getenv("SEARCH_ENGINE", "searxng"),
    )
