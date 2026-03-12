from __future__ import annotations

from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
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


async def setup_commands(bot: Bot, admin_id: int) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=admin_id),
    )
