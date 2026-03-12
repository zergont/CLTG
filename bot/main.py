from __future__ import annotations

import asyncio
import logging

import anthropic
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import load_config
from bot.keyboards import setup_commands
from bot.middlewares import RegisterUserMiddleware
from bot.utils import db
from bot.utils.errors import handle_telegram_error
from bot.utils.log import setup_logging
from bot.utils.reminders import run_scheduler
from bot.utils.anthropic.chat import init_searxng
from bot.handlers import text as text_handler
from bot.handlers import photo as photo_handler
from bot.handlers import document as document_handler
from bot.handlers import admin as admin_handler

logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()
    setup_logging(config)

    logger.info("Запуск CLTG бота...")

    # Инициализация БД
    await db.init_db()

    # Проверка SearXNG
    await init_searxng(config.searxng_url, config.search_engine)

    # Anthropic клиент
    client = anthropic.AsyncAnthropic(
        api_key=config.anthropic_api_key,
        timeout=config.anthropic_timeout,
        max_retries=config.anthropic_max_retries,
    )

    # Telegram бот
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # Функция уведомления администратора
    async def notify_admin(text: str) -> None:
        try:
            await bot.send_message(config.admin_id, text)
        except Exception:
            logger.exception("Не удалось уведомить администратора")

    # Middleware
    dp.message.middleware(RegisterUserMiddleware())

    # Данные для инъекции в хендлеры
    dp["config"] = config
    dp["client"] = client
    dp["notify_admin"] = notify_admin

    # Роутеры (порядок важен: admin и photo/document — до общего text)
    dp.include_router(admin_handler.router)
    dp.include_router(photo_handler.router)
    dp.include_router(document_handler.router)
    dp.include_router(text_handler.router)

    # Настройка команд в меню Telegram
    await setup_commands(bot, config.admin_id)

    async def broadcast_startup() -> None:
        """Рассылает приветствие всем активным пользователям при старте бота."""
        users = await db.get_all_active_users()
        if not users:
            return
        logger.info("Рассылка приветствия %d пользователям...", len(users))
        ok = 0
        for user in users:
            try:
                name = user["first_name"] or "друг"
                await bot.send_message(
                    user["user_id"],
                    f"👋 Привет, <b>{name}</b>! Бот запущен и готов к работе.",
                    parse_mode="HTML",
                )
                ok += 1
            except Exception as exc:
                await handle_telegram_error(exc, chat_id=user["user_id"], user_id=user["user_id"])
        logger.info("Приветствие отправлено %d/%d пользователям", ok, len(users))

    # Запуск планировщика напоминаний
    scheduler_task = asyncio.create_task(
        run_scheduler(bot, config, client)
    )

    # Рассылка приветствия при старте
    dp.startup.register(broadcast_startup)

    logger.info("Бот запущен. Polling...")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await client.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
