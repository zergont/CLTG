from __future__ import annotations

import logging

import anthropic
import httpx2
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from bot.utils import errors
from bot.utils.errors import close_menu, is_billing_error, log_api_error, user_error_message


def api_error(status: int, error_type: str, message: str = "m") -> anthropic.APIStatusError:
    """Исключение ровно такого класса, какой SDK создаёт для ответа API с этим кодом."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        status, request=request, json={"type": "error", "error": {"type": error_type, "message": message}},
    )
    return anthropic.AsyncAnthropic(api_key="x")._make_status_error_from_response(response)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def reset_billing_throttle(monkeypatch):
    monkeypatch.setattr(errors, "_last_billing_alert", 0.0)


def test_billing_errors_are_recognized():
    assert is_billing_error(api_error(402, "billing_error"))
    assert is_billing_error(api_error(
        400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API.",
    ))
    assert not is_billing_error(api_error(400, "invalid_request_error", "messages: roles must alternate"))
    assert not is_billing_error(RuntimeError("credit balance"))


def test_user_messages_for_billing_and_overload():
    assert "закончились средства" in user_error_message(api_error(402, "billing_error"))
    assert "перегружен" in user_error_message(api_error(529, "overloaded_error"))
    assert "слишком большие" in user_error_message(api_error(413, "request_too_large"))


async def test_billing_error_alerts_admin_once_per_hour(caplog):
    bot = FakeBot()
    log = logging.getLogger("test.billing")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await log_api_error(log, api_error(402, "billing_error"), "запрос", bot, admin_id=42)
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 42 and errors.BILLING_URL in bot.sent[0][1]
    # Без трейсбека и баг-репорта: только WARNING
    assert all(r.levelno == logging.WARNING for r in caplog.records)


async def test_overload_is_transient_not_a_bug(caplog):
    bot = FakeBot()
    with caplog.at_level(logging.WARNING):
        await log_api_error(logging.getLogger("test.overload"), api_error(529, "overloaded_error"), "запрос", bot, 42)
    assert bot.sent == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


async def test_code_bug_is_logged_as_error_with_traceback(caplog):
    with caplog.at_level(logging.WARNING):
        try:
            raise KeyError("x")
        except KeyError as exc:
            await log_api_error(logging.getLogger("test.bug"), exc, "сбой обработки")
    (record,) = caplog.records
    assert record.levelno == logging.ERROR and record.exc_info
    assert record.funcName == "test_code_bug_is_logged_as_error_with_traceback"  # место вызова, а не errors.py


def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=DeleteMessage(chat_id=1, message_id=1), message=text)


class FakeMenuMessage:
    def __init__(self, delete_error: str | None = None, edit_error: str | None = None):
        self.delete_error = delete_error
        self.edit_error = edit_error
        self.deleted = False
        self.markup_removed = False

    async def delete(self):
        if self.delete_error:
            raise _bad_request(self.delete_error)
        self.deleted = True

    async def edit_reply_markup(self, reply_markup=None):
        if self.edit_error:
            raise _bad_request(self.edit_error)
        self.markup_removed = True


async def test_close_menu_deletes_message():
    msg = FakeMenuMessage()
    await close_menu(msg)
    assert msg.deleted


async def test_close_menu_removes_buttons_when_message_too_old():
    msg = FakeMenuMessage(delete_error="Bad Request: message can't be deleted")
    await close_menu(msg)
    assert msg.markup_removed


async def test_close_menu_ignores_already_deleted_message():
    # Двойное нажатие «Закрыть»: ни удалить, ни отредактировать — но и не ошибка
    msg = FakeMenuMessage(
        delete_error="Bad Request: message to delete not found",
        edit_error="Bad Request: message to edit not found",
    )
    await close_menu(msg)
    assert not msg.deleted and not msg.markup_removed
