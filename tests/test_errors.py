from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from bot.utils.errors import close_menu


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
