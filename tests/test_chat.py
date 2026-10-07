from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.utils.anthropic import chat
from bot.utils.anthropic.models import MODELS
from tests.conftest import FakeClient, make_message, make_usage

HAIKU = MODELS["claude-haiku-5-5"]
SONNET = MODELS["claude-sonnet-5-5"]


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


async def collect(gen):
    texts, usage = [], None
    async for chunk, chunk_usage in gen:
        if chunk:
            texts.append(chunk)
        if chunk_usage:
            usage = chunk_usage
    return "".join(texts), usage


# ── Сборка запроса ──────────────────────────────────────────

def test_system_prompt_is_static(config):
    system = chat._build_system_prompt(config)
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Текущее время:" not in system[0]["text"]


def test_turn_context_is_appended_to_new_message_and_history_is_cached(config):
    history = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуй"},
    ]
    context = chat._turn_context(config, "Europe/Moscow", "Иван Петров (@ivan)")
    messages = chat._build_messages(history, "старое саммари", "как дела?", context)

    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    first, extra = messages[-1]["content"]
    assert first == {"type": "text", "text": "как дела?"}
    assert extra["text"].startswith(
        f"{chat.TURN_CONTEXT_HEADER}\nСобеседник: Иван Петров (@ivan)\nТекущее время:"
    )
    assert "UTC+03:00" in extra["text"]
    assert "cache_control" not in extra
    # Кэш-маркеры: саммари и последнее сообщение истории
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[3]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_turn_context_follows_image_content(config):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
    caption = {"type": "text", "text": "что на фото?"}
    messages = chat._build_messages([], None, [image, caption], "контекст")
    assert messages[-1]["content"] == [image, caption, {"type": "text", "text": "контекст"}]


def test_unknown_timezone_falls_back_to_default(config):
    text = chat._turn_context(config, "Mars/Olympus")
    assert "Собеседник" not in text  # без имени — только время
    assert "UTC+03:00" in text  # DEFAULT_TIMEZONE=Europe/Moscow


@pytest.mark.parametrize(
    ("first", "last", "username", "expected"),
    [
        ("Иван", "Петров", "ivan", "Иван Петров (@ivan)"),
        ("Мама", None, None, "Мама"),
        (" ", None, "dad", "@dad"),
        (None, None, None, None),
    ],
)
def test_display_name(first, last, username, expected):
    assert chat.display_name(first, last, username) == expected


# ── Agentic loop ────────────────────────────────────────────

async def test_stream_sends_effort_and_adaptive_thinking(config):
    client = FakeClient([(["Привет!"], make_message([text_block("Привет!")]))])
    text, usage = await collect(chat.stream_response(
        client, config, HAIKU, "low", [{"role": "user", "content": "hi"}], [],
    ))
    assert text == "Привет!"
    params = client.messages.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "low"}
    assert "fallbacks" not in params  # у Haiku 5.5 серверного fallback нет
    assert usage.cost > 0
    assert usage.context_tokens == 150


async def test_sonnet_uses_server_fallback_and_new_web_search(config, monkeypatch):
    monkeypatch.setattr(chat, "_search_engine", "native")
    client = FakeClient([(["ok"], make_message([text_block("ok")], model="claude-sonnet-5-5"))])
    await collect(chat.stream_response(client, config, SONNET, "medium", [], []))
    params = client.messages.calls[0]
    assert params["fallbacks"] == "default"
    assert params["betas"] == [chat.SERVER_FALLBACK_BETA]
    assert params["tools"][0] == {"type": "web_search_20260209", "name": "web_search"}


# ── Режимы поиска ───────────────────────────────────────────

def _no_network_check(monkeypatch, result: bool) -> list[int]:
    calls: list[int] = []

    async def fake_check():
        calls.append(1)
        monkeypatch.setattr(chat, "_searxng_available", result)
        return result

    monkeypatch.setattr(chat, "_check_searxng", fake_check)
    return calls


async def test_searxng_mode_never_uses_paid_search(monkeypatch):
    monkeypatch.setattr(chat, "_search_engine", "searxng")
    monkeypatch.setattr(chat, "_searxng_available", False)  # SearXNG лежит
    calls = _no_network_check(monkeypatch, False)
    assert await chat._search_tool(HAIKU) is chat.WEB_SEARCH_TOOL
    assert calls == []


async def test_auto_mode_rechecks_and_returns_to_searxng(monkeypatch):
    monkeypatch.setattr(chat, "_search_engine", "auto")
    monkeypatch.setattr(chat, "_searxng_available", False)
    monkeypatch.setattr(chat, "_searxng_checked_at", 0.0)  # давно не проверяли
    calls = _no_network_check(monkeypatch, True)  # SearXNG поднялся
    assert await chat._search_tool(HAIKU) is chat.WEB_SEARCH_TOOL
    assert calls == [1]


async def test_auto_mode_uses_paid_search_while_searxng_down(monkeypatch):
    monkeypatch.setattr(chat, "_search_engine", "auto")
    monkeypatch.setattr(chat, "_searxng_available", False)
    monkeypatch.setattr(chat, "_searxng_checked_at", chat.time.monotonic())  # только что проверяли
    calls = _no_network_check(monkeypatch, True)
    assert await chat._search_tool(HAIKU) == {"type": "web_search_20250305", "name": "web_search"}
    assert calls == []  # перепроверка не чаще раза в SEARXNG_RECHECK_SECONDS


async def test_tool_loop_runs_search_and_continues(config, monkeypatch):
    monkeypatch.setattr(chat, "_searxng_available", True)

    async def fake_search(query, url):
        return f"результаты для {query}"

    monkeypatch.setattr(chat, "_searxng_search", fake_search)
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    first = make_message(
        [thinking, text_block("Ищу."), tool_use_block("t1", "web_search", {"query": "погода"})],
        stop_reason="tool_use",
        usage=make_usage(input_tokens=100, output_tokens=20),
    )
    second = make_message([text_block("Солнечно.")], usage=make_usage(input_tokens=200, output_tokens=30))
    client = FakeClient([(["Ищу."], first), (["Солнечно."], second)])

    text, usage = await collect(chat.stream_response(client, config, HAIKU, "medium", [], []))

    assert text == "Ищу.\n\nСолнечно."
    second_call = client.messages.calls[1]["messages"]
    # Ответ с размышлениями возвращается целиком, затем результаты инструмента
    assert second_call[-2]["content"][0] is thinking
    assert second_call[-1]["content"][0]["content"] == "результаты для погода"
    assert usage.input_tokens == 300
    assert usage.context_tokens == 230  # последний вызов: 200 + 30


async def test_refusal_shows_notice_and_stops(config):
    client = FakeClient([([], make_message([], stop_reason="refusal"))])
    text, usage = await collect(chat.stream_response(client, config, HAIKU, "medium", [], []))
    assert text == chat.REFUSAL_TEXT
    assert len(client.messages.calls) == 1
    assert usage is not None


async def test_pause_turn_continues_same_turn(config):
    paused = make_message([text_block("Часть 1.")], stop_reason="pause_turn")
    done = make_message([text_block("Часть 2.")])
    client = FakeClient([(["Часть 1."], paused), (["Часть 2."], done)])
    text, _ = await collect(chat.stream_response(client, config, HAIKU, "medium", [], []))
    assert text == "Часть 1.\n\nЧасть 2."
    second_call = client.messages.calls[1]["messages"]
    assert second_call[-1]["role"] == "assistant"  # продолжение без нового user-сообщения


def test_echo_content_drops_declined_blocks_before_fallback():
    declined_thinking = SimpleNamespace(type="thinking")
    declined_tool = SimpleNamespace(type="tool_use", id="x")
    partial = text_block("Начало")
    fallback = SimpleNamespace(type="fallback")
    tail = [text_block("Продолжение"), tool_use_block("t2", "web_search", {"query": "q"})]

    echoed = chat._echo_content([declined_thinking, partial, declined_tool, fallback, *tail])
    assert echoed == [partial, *tail]


def test_echo_content_without_fallback_is_unchanged():
    content = [SimpleNamespace(type="thinking"), text_block("x")]
    assert chat._echo_content(content) is content


# ── Изолированные вызовы ────────────────────────────────────

async def test_isolated_call_skips_thinking_blocks(config):
    response = make_message([SimpleNamespace(type="thinking", thinking=""), text_block("Резюме")])
    client = FakeClient([(None, response)])
    text, usage = await chat.call_claude_isolated(client, config, HAIKU, "prompt")
    assert text == "Резюме"
    assert client.messages.calls[0]["output_config"] == {"effort": "low"}
    assert usage.cost > 0


async def test_isolated_call_raises_on_refusal(config):
    client = FakeClient([(None, make_message([], stop_reason="refusal"))])
    with pytest.raises(RuntimeError):
        await chat.call_claude_isolated(client, config, HAIKU, "prompt")
