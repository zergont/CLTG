from __future__ import annotations

from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram import Bot

USER_COMMANDS = [
    BotCommand(command="start",       description="Приветствие и регистрация"),
    BotCommand(command="help",        description="Список команд"),
    BotCommand(command="reset",       description="Сбросить контекст диалога"),
    BotCommand(command="stats",       description="Статистика токенов и расходов"),
    BotCommand(command="reminders",   description="Список активных напоминаний"),
    BotCommand(command="delreminder", description="Удалить напоминание: /delreminder <id>"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="model",   description="Сменить модель Claude"),
    BotCommand(command="ban",     description="Заблокировать: /ban <user_id>"),
    BotCommand(command="unban",   description="Разблокировать: /unban <user_id>"),
    BotCommand(command="users",   description="Список пользователей"),
    BotCommand(command="usage",   description="Общая статистика расходов"),
    BotCommand(command="context", description="Заполнение контекстного окна"),
]


def get_main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="/help"),       KeyboardButton(text="/stats")],
        [KeyboardButton(text="/reset"),      KeyboardButton(text="/reminders")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="/users"), KeyboardButton(text="/usage")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def setup_commands(bot: Bot, admin_id: int) -> None:
    # Чистим все scope-ы, где могли остаться старые команды
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
        BotCommandScopeChat(chat_id=admin_id),
    ):
        await bot.delete_my_commands(scope=scope)

    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=admin_id),
    )
