from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.methods import SendMessage
from aiogram.types import Message

from bot.handlers import text as text_handlers
from bot.handlers._common import _Progress, render_progress
from bot.utils.anthropic import chat
from bot.utils.anthropic.models import MODELS
from tests.conftest import FakeClient, make_message

HAIKU = MODELS["claude-haiku-5-5"]


def block_start(block_type):
    return SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type=block_type))


def server_tool_stop(name, tool_input):
    return SimpleNamespace(
        type="content_block_stop",
        content_block=SimpleNamespace(type="server_tool_use", name=name, input=tool_input),
    )


# ── Статусы из стрима ───────────────────────────────────────

@pytest.mark.parametrize(("event", "expected"), [
    (block_start("thinking"), "💭 Думаю…"),
    (server_tool_stop("web_fetch", {"url": "https://www.gismeteo.ru/weather-kazan/"}), "🌐 Читаю страницу: gismeteo.ru"),
    (server_tool_stop("web_search", {"query": "погода в Казани"}), "🔍 Ищу в интернете: «погода в Казани»"),
    (server_tool_stop("web_fetch", {}), None),
    (server_tool_stop("code_execution", {}), "📑 Выбираю нужное…"),
    (block_start("text"), None),
    (SimpleNamespace(type="message_delta"), None),
])
def test_status_for_event(event, expected):
    assert chat._status_for_event(event) == expected


def test_search_status_for_several_queries_is_short():
    status = chat._search_status(["очень длинный запрос " * 10, "второй"])
    assert status.startswith("🔍 Ищу в интернете: «очень длинный") and status.endswith("» и ещё 1")
    assert len(status) < 100


async def test_stream_reports_statuses_in_order(config, monkeypatch):
    monkeypatch.setattr(chat, "_search_engine", "ddgs")

    async def fake_ddgs(query, max_results=5):
        return [{"title": "t", "url": "https://example.com", "content": "c"}]

    monkeypatch.setattr(chat, "_ddgs_search", fake_ddgs)
    tool_call = SimpleNamespace(type="tool_use", id="t1", name="web_search", input={"query": "курс доллара"})
    first = make_message([tool_call], stop_reason="tool_use")
    second = make_message([SimpleNamespace(type="text", text="90 ₽")])
    client = FakeClient([
        ([], first, [block_start("thinking")]),
        (["90 ₽"], second, [server_tool_stop("web_fetch", {"url": "https://cbr.ru/"})]),
    ])

    statuses: list[str] = []

    async def on_status(text):
        statuses.append(text)

    async for _ in chat.stream_response(client, config, HAIKU, "low", [], [], on_status=on_status):
        pass
    assert statuses == ["💭 Думаю…", "🔍 Ищу в интернете: «курс доллара»", "🌐 Читаю страницу: cbr.ru"]


# ── Отображение в Telegram ──────────────────────────────────

def test_render_progress():
    assert render_progress("", None) == "⏳"
    assert render_progress("", "💭 Думаю…") == "<i>💭 Думаю…</i>"
    assert render_progress("Сейчас **посмотрю**", "🔍 Ищу <x>") == (
        "Сейчас <b>посмотрю</b>\n\n<i>🔍 Ищу &lt;x&gt;</i>"
    )
    assert render_progress("Ответ", None) == "Ответ ▌"


class FakePlaceholder:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text, parse_mode=None):
        self.edits.append(text)


async def test_progress_shows_status_then_text_replaces_it(monkeypatch):
    placeholder = FakePlaceholder()
    progress = _Progress(placeholder)
    await progress.set_status("💭 Думаю…")
    await progress.set_status("💭 Думаю…")  # повтор не дёргает Telegram
    assert placeholder.edits == ["<i>💭 Думаю…</i>"]

    monkeypatch.setattr("bot.handlers._common.STREAM_UPDATE_INTERVAL", 0)
    await progress.add_text("Готово")
    assert placeholder.edits[-1] == "Готово ▌"
    assert progress.status is None


# ── Голосовые и прочее ──────────────────────────────────────

class FakeBot:
    id = 999

    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, method, request_timeout=None):
        if isinstance(method, SendMessage):
            self.sent.append(method.text)
        return True


def incoming(bot, chat_type="private", **payload) -> Message:
    return Message.model_validate({
        "message_id": 1, "date": 0,
        "chat": {"id": 10 if chat_type == "private" else -100, "type": chat_type},
        "from": {"id": 10, "is_bot": False, "first_name": "Оля"},
        **payload,
    }, context={"bot": bot})


VOICE = {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}}
VIDEO_NOTE = {"video_note": {"file_id": "n", "file_unique_id": "n", "length": 240, "duration": 3}}
LOCATION = {"location": {"latitude": 55.79, "longitude": 49.12}}
STICKER = {"sticker": {
    "file_id": "s", "file_unique_id": "s", "type": "regular",
    "width": 512, "height": 512, "is_animated": False, "is_video": False, "emoji": "😂",
}}


@pytest.mark.parametrize("payload", [VOICE, VIDEO_NOTE])
async def test_voice_gets_polite_reply_in_private_chat(payload):
    bot = FakeBot()
    await text_handlers.handle_voice(incoming(bot, **payload))
    assert bot.sent == [text_handlers.VOICE_REPLY]


async def test_voice_in_group_is_ignored():
    bot = FakeBot()
    await text_handlers.handle_voice(incoming(bot, chat_type="group", **VOICE))
    assert bot.sent == []


async def test_other_unsupported_message_gets_hint_but_sticker_does_not(config):
    bot = FakeBot()
    await text_handlers.handle_text(incoming(bot, **LOCATION), config=config, client=None)
    assert bot.sent == [text_handlers.UNSUPPORTED_REPLY]

    await text_handlers.handle_text(incoming(bot, **STICKER), config=config, client=None)
    assert bot.sent == [text_handlers.UNSUPPORTED_REPLY]  # стикер — без ответа
