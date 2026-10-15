from __future__ import annotations

import aiosqlite
import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Message

from bot.handlers import admin
from bot.middlewares import (
    ACCESS_PENDING_TEXT,
    ACCESS_REQUESTED_TEXT,
    BANNED_TEXT,
    RegisterUserMiddleware,
)
from bot.utils import db
from bot.utils.errors import handle_telegram_error

ADMIN_ID = 1  # из фикстуры config
STRANGER = 777


class FakeBot:
    """Подставной бот: методы Telegram API записываются, а не отправляются."""

    id = 999

    def __init__(self):
        self.calls: list = []

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(SendMessage(chat_id=chat_id, text=text, **kwargs))

    def sent_to(self, chat_id: int) -> list[str]:
        return [
            c.text for c in self.calls
            if isinstance(c, SendMessage) and c.chat_id == chat_id
        ]


def make_message(bot, user_id=STRANGER, text="привет", chat_type="private") -> Message:
    chat_id = user_id if chat_type == "private" else -100
    return Message.model_validate({
        "message_id": 1, "date": 0,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "JIM", "username": "jim"},
        "text": text,
    }, context={"bot": bot})  # как в aiogram: бот привязан ко всему дереву объектов


def make_callback(bot, user_id, data) -> CallbackQuery:
    return CallbackQuery.model_validate({
        "id": "cb", "chat_instance": "x", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": "Admin"},
        "message": make_message(bot, user_id=user_id, text="меню").model_dump(),
    }, context={"bot": bot})


@pytest.fixture
async def env(config, db_path):
    await db.init_db()
    bot = FakeBot()
    handled: list = []

    async def handler(event, data):
        handled.append(event)

    async def run(event):
        await RegisterUserMiddleware()(handler, event, {"config": config, "bot": bot})

    return bot, handled, run


async def test_stranger_gets_one_access_request(env):
    bot, handled, run = env

    await run(make_message(bot, text="кто ты?"))
    assert handled == []
    assert bot.sent_to(STRANGER) == [ACCESS_REQUESTED_TEXT]
    (request,) = bot.sent_to(ADMIN_ID)
    assert "Запрос доступа" in request and "кто ты?" in request and "@jim" in request
    admin_msg = [c for c in bot.calls if isinstance(c, SendMessage) and c.chat_id == ADMIN_ID][0]
    buttons = [b.callback_data for b in admin_msg.reply_markup.inline_keyboard[0]]
    assert buttons == [f"access:approve:{STRANGER}", f"access:deny:{STRANGER}"]

    # Повторное сообщение — без нового запроса админу
    await run(make_message(bot, text="ау"))
    assert handled == []
    assert bot.sent_to(STRANGER)[-1] == ACCESS_PENDING_TEXT
    assert len(bot.sent_to(ADMIN_ID)) == 1


async def test_approved_user_passes(env):
    bot, handled, run = env
    await run(make_message(bot))  # регистрация
    await db.approve_user(STRANGER)
    await run(make_message(bot, text="теперь можно?"))
    assert len(handled) == 1


async def test_banned_user_is_rejected(env):
    bot, handled, run = env
    await run(make_message(bot))
    await db.ban_user(STRANGER)
    await run(make_message(bot, text="пустите"))
    assert handled == []
    assert bot.sent_to(STRANGER)[-1] == BANNED_TEXT


async def test_admin_always_passes(env):
    bot, handled, run = env
    await run(make_message(bot, user_id=ADMIN_ID))
    assert len(handled) == 1
    assert (await db.get_user(ADMIN_ID))["is_approved"]


async def test_stranger_in_group_is_ignored_silently(env):
    bot, handled, run = env
    await run(make_message(bot, chat_type="group"))
    assert handled == [] and bot.calls == []


async def test_stranger_cannot_press_buttons(env):
    bot, handled, run = env
    await run(make_callback(bot, STRANGER, "rem:delall"))
    assert handled == []
    assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in bot.calls)


@pytest.mark.parametrize(("action", "approved", "banned", "notice"), [
    ("approve", True, False, admin.APPROVED_TEXT),
    ("deny", False, True, admin.DENIED_TEXT),
])
async def test_admin_buttons_decide_access(env, config, action, approved, banned, notice):
    bot, _, run = env
    await run(make_message(bot))  # запрос от незнакомца

    callback = make_callback(bot, ADMIN_ID, f"access:{action}:{STRANGER}")
    await admin.cb_access(callback, config=config)

    row = await db.get_user(STRANGER)
    assert bool(row["is_approved"]) is approved and bool(row["is_banned"]) is banned
    assert bot.sent_to(STRANGER)[-1] == notice
    assert any(isinstance(c, EditMessageText) for c in bot.calls)


async def test_user_who_blocked_bot_is_not_banned(env):
    bot, _, run = env
    await run(make_message(bot))
    await db.approve_user(STRANGER)

    forbidden = TelegramForbiddenError(
        method=SendMessage(chat_id=STRANGER, text="x"), message="Forbidden: bot was blocked by the user",
    )
    assert await handle_telegram_error(forbidden, chat_id=STRANGER, user_id=STRANGER)
    row = await db.get_user(STRANGER)
    assert row["blocked_bot"] and not row["is_banned"]
    assert STRANGER not in [r["user_id"] for r in await db.get_all_active_users()]

    # Разблокировал и написал — пометка снимается, доступ сохраняется
    await run(make_message(bot, text="я вернулся"))
    row = await db.get_user(STRANGER)
    assert not row["blocked_bot"] and row["is_approved"]


async def test_migration_keeps_access_for_existing_users(db_path):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, "
            "last_name TEXT, first_seen DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "is_welcomed BOOLEAN DEFAULT FALSE, timezone TEXT DEFAULT 'Europe/Moscow', "
            "is_banned BOOLEAN DEFAULT FALSE)"
        )
        await conn.execute("INSERT INTO users (user_id, first_name) VALUES (10, 'Iuliia')")
        await conn.execute("INSERT INTO users (user_id, first_name, is_banned) VALUES (20, 'JIM', TRUE)")
        await conn.commit()

    await db.init_db()

    family, stranger = await db.get_user(10), await db.get_user(20)
    assert family["is_approved"] and not family["is_banned"]
    assert not stranger["is_approved"] and stranger["is_banned"]

    # Повторный старт не перезаписывает решения админа
    await db.approve_user(20)
    await db.ban_user(10)
    await db.init_db()
    assert (await db.get_user(20))["is_approved"] and (await db.get_user(10))["is_banned"]
