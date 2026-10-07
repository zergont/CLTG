from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Цены — USD за 1M токенов, по https://platform.claude.com/docs/en/about-claude/pricing (октябрь 2026).
# cache_write — запись в 5-минутный кэш, cache_read — чтение из кэша.

# Стоимость одного нативного web_search ($10 за 1000 запросов)
WEB_SEARCH_PRICE = 0.01


@dataclass(frozen=True)
class Pricing:
    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    context_window: int
    pricing: Pricing
    # Haiku 5.5 тарифицируется дороже, если промпт длиннее порога
    long_pricing: Pricing | None = None
    long_threshold: int = 0
    # Версия нативного web_search (новая версия доступна не всем моделям)
    web_search_tool: str = "web_search_20250305"
    # Серверный fallback при отказе модели (fallbacks: "default", только Sonnet/Opus)
    server_fallback: bool = False

    def pricing_for(self, prompt_tokens: int) -> Pricing:
        if self.long_pricing and prompt_tokens > self.long_threshold:
            return self.long_pricing
        return self.pricing


MODELS: dict[str, ModelInfo] = {
    m.id: m
    for m in (
        ModelInfo(
            id="claude-haiku-5-5",
            label="Claude Haiku 5.5 (быстрая)",
            context_window=1_000_000,
            pricing=Pricing(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
            long_pricing=Pricing(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05),
            long_threshold=100_000,
        ),
        ModelInfo(
            id="claude-sonnet-5-5",
            label="Claude Sonnet 5.5 (умная)",
            context_window=1_000_000,
            pricing=Pricing(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
            web_search_tool="web_search_20260209",
            server_fallback=True,
        ),
        ModelInfo(
            id="claude-opus-5-5",
            label="Claude Opus 5.5 (максимум)",
            context_window=1_000_000,
            pricing=Pricing(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
            web_search_tool="web_search_20260209",
            server_fallback=True,
        ),
    )
}

DEFAULT_MODEL_ID = "claude-haiku-5-5"

# Старые ID из БД → актуальные модели
LEGACY_MODELS: dict[str, str] = {
    "claude-haiku-4-5": "claude-haiku-5-5",
    "claude-sonnet-4-6": "claude-sonnet-5-5",
}

# Уровни размышлений (output_config.effort), от быстрого к самому вдумчивому
EFFORT_LABELS: dict[str, str] = {
    "low": "Низкая",
    "medium": "Средняя",
    "high": "Высокая",
    "xhigh": "Очень высокая",
    "max": "Максимальная",
}

DEFAULT_EFFORT = "medium"
# Для служебных вызовов (саммари, часовой пояс) хватает минимального уровня
SERVICE_EFFORT = "low"


def get_model(model_id: str | None) -> ModelInfo:
    """Возвращает модель по ID; старые и неизвестные ID заменяются актуальными."""
    if model_id in MODELS:
        return MODELS[model_id]
    return MODELS[LEGACY_MODELS.get(model_id or "", DEFAULT_MODEL_ID)]


def calc_cost(model: ModelInfo, usage: Any) -> float:
    """Стоимость одного вызова API по его usage (с учётом кэша и web_search)."""
    input_tokens = usage.input_tokens or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    prices = model.pricing_for(input_tokens + cache_write + cache_read)

    cost = (
        input_tokens * prices.input
        + (usage.output_tokens or 0) * prices.output
        + cache_write * prices.cache_write
        + cache_read * prices.cache_read
    ) / 1_000_000

    server_tool_use = getattr(usage, "server_tool_use", None)
    searches = getattr(server_tool_use, "web_search_requests", 0) or 0
    return cost + searches * WEB_SEARCH_PRICE
