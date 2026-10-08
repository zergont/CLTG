from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.config import load_config


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:test")
    monkeypatch.setenv("ADMIN_ID", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("SYSTEM_PROMPT", "Тестовый промпт.")
    monkeypatch.setenv("CONTEXT_BUDGET_TOKENS", "100000")
    monkeypatch.setenv("SUMMARY_TRIGGER_TOKENS", "0.85")
    return load_config()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    from bot.utils import db

    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    return path


def make_usage(
    input_tokens=100, output_tokens=50, cache_write=0, cache_read=0, searches=0,
    cache_write_5m=None, cache_write_1h=None,
):
    breakdown = None
    if cache_write_5m is not None or cache_write_1h is not None:
        breakdown = SimpleNamespace(
            ephemeral_5m_input_tokens=cache_write_5m or 0,
            ephemeral_1h_input_tokens=cache_write_1h or 0,
        )
        cache_write = (cache_write_5m or 0) + (cache_write_1h or 0)
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_write,
        cache_creation=breakdown,
        cache_read_input_tokens=cache_read,
        server_tool_use=SimpleNamespace(web_search_requests=searches) if searches else None,
    )


def make_message(content, stop_reason="end_turn", model="claude-haiku-5-5", usage=None):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=None,
        model=model,
        usage=usage or make_usage(),
    )


class FakeStream:
    def __init__(self, texts, final):
        self._texts = texts
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for t in self._texts:
                yield t
        return gen()

    async def get_final_message(self):
        return self._final


class FakeMessages:
    """Отдаёт заранее заданные ответы и запоминает параметры запросов."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **params):
        self.calls.append(params)
        texts, final = self.responses.pop(0)
        return FakeStream(texts, final)

    async def create(self, **params):
        self.calls.append(params)
        _, final = self.responses.pop(0)
        return final


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)
        self.beta = SimpleNamespace(messages=self.messages)
