from __future__ import annotations

import logging
import asyncio

import anthropic
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

logger = logging.getLogger(__name__)

# Ретраи вызовов Claude (429, 529, 5xx, сетевые ошибки) выполняет сам SDK
# с экспоненциальной задержкой и учётом retry-after: см. ANTHROPIC_MAX_RETRIES.


async def handle_telegram_error(
    exc: Exception,
    chat_id: int | None = None,
    user_id: int | None = None,
) -> bool:
    """
    Обрабатывает ошибки Telegram. Возвращает True если ошибка обработана мягко,
    False если нужно пробросить дальше.
    """
    if isinstance(exc, TelegramForbiddenError):
        logger.warning("Бот заблокирован пользователем chat_id=%s", chat_id)
        # Помечаем пользователя в БД, чтобы больше не слать ему сообщения (ТЗ п.7.2)
        if user_id is not None:
            try:
                from bot.utils import db
                await db.set_banned(user_id, True)
            except Exception:
                logger.debug("Не удалось пометить user_id=%s как заблокировавшего бота", user_id)
        return True
    if isinstance(exc, TelegramRetryAfter):
        retry_after = exc.retry_after
        logger.warning("Telegram flood control, жду %ds", retry_after)
        await asyncio.sleep(retry_after)
        return False  # можно повторить
    logger.error("Telegram ошибка для chat_id=%s: %s", chat_id, exc, exc_info=True)
    return False


def user_error_message(exc: Exception) -> str:
    """Возвращает понятное сообщение пользователю на русском."""
    if isinstance(exc, anthropic.RateLimitError):
        return "⏳ Превышен лимит запросов к Claude. Попробуйте через минуту."
    if isinstance(exc, anthropic.InternalServerError):
        status = getattr(exc, "status_code", None)
        if status == 529:
            return "⚙️ Claude сейчас перегружен. Попробуйте через несколько минут."
        return "⚙️ Временная ошибка сервера Claude. Попробуйте позже."
    if isinstance(exc, anthropic.AuthenticationError):
        return "🔧 Ошибка конфигурации бота. Администратор уведомлён."
    if isinstance(exc, anthropic.BadRequestError):
        return "❌ Некорректный запрос. Попробуйте переформулировать."
    if isinstance(exc, (asyncio.TimeoutError, anthropic.APITimeoutError)):
        return "⏱ Превышено время ожидания ответа от Claude. Попробуйте позже."
    if isinstance(exc, anthropic.APIConnectionError):
        return "🌐 Ошибка соединения с Claude. Проверьте сеть и попробуйте снова."
    return "❌ Произошла непредвиденная ошибка. Попробуйте позже."
