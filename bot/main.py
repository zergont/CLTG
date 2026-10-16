from __future__ import annotations

import asyncio
import logging

import anthropic
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from bot.config import load_config
from bot.keyboards import setup_commands
from bot.middlewares import RegisterUserMiddleware
from bot.utils import db
from bot.utils.anthropic.models import EFFORT_LABELS
from bot.utils.backup import run_backup_scheduler
from bot.utils.bug_report import setup_bug_reports
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
    await init_searxng(config.searxng_url, config.search_engine, config.searxng_engines)

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

    # Баг-репорты: все ошибки уровня ERROR из логов уходят администратору
    if config.bug_reports:
        setup_bug_reports(bot, config.admin_id)

    @dp.errors()
    async def on_unhandled_error(event: ErrorEvent) -> bool:
        """Необработанное исключение в хендлере — в лог (и баг-репорт) с контекстом."""
        update = event.update
        source = update.message or (update.callback_query and update.callback_query.message)
        user = (update.message and update.message.from_user) or (
            update.callback_query and update.callback_query.from_user
        )
        logger.error(
            "Необработанная ошибка в обработчике: update_id=%s, chat_id=%s, user_id=%s, тип=%s",
            update.update_id,
            source.chat.id if source else None,
            user.id if user else None,
            update.event_type,
            exc_info=event.exception,
        )
        return True

    # Middleware: регистрация и проверка доступа (сообщения и кнопки)
    access_middleware = RegisterUserMiddleware()
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)

    # Данные для инъекции в хендлеры
    dp["config"] = config
    dp["client"] = client

    # Роутеры (порядок важен: admin и photo/document — до общего text)
    dp.include_router(admin_handler.router)
    dp.include_router(photo_handler.router)
    dp.include_router(document_handler.router)
    dp.include_router(text_handler.router)

    # Настройка команд в меню Telegram
    await setup_commands(bot, config.admin_id)

    async def notify_admin_startup() -> None:
        """Сообщает администратору о запуске бота и текущих настройках."""
        model, effort = await db.get_model_settings()
        fetch = "вкл" if config.web_fetch else "выкл"
        try:
            await bot.send_message(
                config.admin_id,
                "🟢 <b>Бот перезапущен</b>\n\n"
                f"Модель: {model.label}, размышления: {EFFORT_LABELS[effort].lower()}\n"
                f"Поиск: {config.search_engine}, чтение страниц: {fetch}",
                parse_mode="HTML",
            )
            logger.info("Администратору отправлено уведомление о запуске")
        except Exception as exc:
            await handle_telegram_error(exc, chat_id=config.admin_id, user_id=config.admin_id)

    # Фоновые задачи: напоминания и резервные копии базы
    background_tasks = [
        asyncio.create_task(run_scheduler(bot, config, client)),
        asyncio.create_task(run_backup_scheduler(bot, config)),
    ]

    # Уведомление о запуске — только администратору
    dp.startup.register(notify_admin_startup)

    logger.info("Бот запущен. Polling...")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()
        await client.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
