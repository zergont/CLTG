from __future__ import annotations

import pytest

from bot.utils.anthropic import chat

RESULT = [{"title": "Заголовок", "url": "https://example.com", "content": "Фрагмент"}]


@pytest.fixture
def backends(monkeypatch):
    """Подменяет оба бесплатных движка; возвращает журнал вызовов и их ответы."""
    calls: list[str] = []
    answers: dict[str, list | None] = {"ddgs": None, "searxng": None}

    async def fake_ddgs(query, max_results=5):
        calls.append("ddgs")
        return answers["ddgs"]

    async def fake_searxng(query, max_results=5):
        calls.append("searxng")
        return answers["searxng"]

    monkeypatch.setattr(chat, "_ddgs_search", fake_ddgs)
    monkeypatch.setattr(chat, "_searxng_search", fake_searxng)
    return calls, answers


async def test_ddgs_answers_without_touching_searxng(monkeypatch, backends):
    calls, answers = backends
    monkeypatch.setattr(chat, "_search_engine", "ddgs")
    answers["ddgs"] = RESULT
    assert (await chat._free_search("q")).startswith("1. Заголовок")
    assert calls == ["ddgs"]


async def test_searxng_is_the_fallback_when_ddgs_fails(monkeypatch, backends):
    calls, answers = backends
    monkeypatch.setattr(chat, "_search_engine", "ddgs")
    answers["searxng"] = RESULT
    assert "https://example.com" in await chat._free_search("q")
    assert calls == ["ddgs", "searxng"]


async def test_searxng_mode_falls_back_to_ddgs(monkeypatch, backends):
    calls, answers = backends
    monkeypatch.setattr(chat, "_search_engine", "searxng")
    answers["ddgs"] = RESULT
    await chat._free_search("q")
    assert calls == ["searxng", "ddgs"]


async def test_both_engines_down_gives_clear_message(monkeypatch, backends):
    calls, _ = backends
    monkeypatch.setattr(chat, "_search_engine", "ddgs")
    text = await chat._free_search("q")
    assert "ничего не нашёл или временно недоступен" in text
    assert calls == ["ddgs", "searxng"]


async def test_searxng_request_names_working_engines(monkeypatch):
    sent: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"title": "t", "url": "u", "content": "c"}], "unresponsive_engines": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params):
            sent.update(url=url, params=params)
            return FakeResponse()

    monkeypatch.setattr(chat.httpx2, "AsyncClient", FakeClient)
    monkeypatch.setattr(chat, "_searxng_url", "http://localhost:8888")
    monkeypatch.setattr(chat, "_searxng_engines", "yandex,bing")

    assert await chat._searxng_search("погода") == [{"title": "t", "url": "u", "content": "c"}]
    assert sent["params"]["engines"] == "yandex,bing"
