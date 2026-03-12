from __future__ import annotations

# Лимиты контекстного окна по модели (в токенах)
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-6": 200_000,
}

AVAILABLE_MODELS: list[str] = list(MODEL_CONTEXT_LIMITS.keys())

MODEL_LABELS: dict[str, str] = {
    "claude-haiku-4-5": "Claude Haiku 4.5 (быстрый)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (умный)",
}


def context_limit(model: str) -> int:
    return MODEL_CONTEXT_LIMITS.get(model, 200_000)
