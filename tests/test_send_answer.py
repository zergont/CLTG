from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from bot.handlers._common import TELEGRAM_SAFE_LENGTH, _send_answer
from bot.utils.html import TELEGRAM_MAX_LENGTH


class FakeMessage:
    def __init__(self, reject_html: bool = False):
        self.calls: list[tuple[str, str | None]] = []
        self.reject_html = reject_html

    async def _record(self, text, parse_mode=None):
        if parse_mode == "HTML" and self.reject_html:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=1, text=text),
                message="Bad Request: can't parse entities",
            )
        assert len(text) <= TELEGRAM_MAX_LENGTH
        self.calls.append((text, parse_mode))

    edit_text = _record
    answer = _record


async def test_short_answer_edits_placeholder_with_html():
    placeholder, message = FakeMessage(), FakeMessage()
    await _send_answer(placeholder, message, "**Привет**")
    assert placeholder.calls == [("<b>Привет</b>", "HTML")]
    assert message.calls == []


async def test_long_answer_with_heavy_markup_never_exceeds_limit():
    # Экранирование «<» в «&lt;» раздувает HTML: раньше такой ответ падал целиком
    text = ("a < b && c > d " * 600)[: TELEGRAM_SAFE_LENGTH * 2]
    placeholder, message = FakeMessage(), FakeMessage()
    await _send_answer(placeholder, message, text)
    sent = placeholder.calls + message.calls
    assert len(sent) >= 2
    assert all(len(t) <= TELEGRAM_MAX_LENGTH for t, _ in sent)


async def test_unparseable_html_falls_back_to_plain_text():
    placeholder, message = FakeMessage(reject_html=True), FakeMessage()
    await _send_answer(placeholder, message, "**жирный *курсив** конец*")
    assert placeholder.calls == [("**жирный *курсив** конец*", None)]
