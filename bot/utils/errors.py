from __future__ import annotations

from __future__ import annotations

import logging
import asyncio
from typing import Callable, Awaitable, Any, TypeVar

import anthropic
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.config import Config

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Максимум попыток для ретраев
MAX_RETRIES = 3
# Повторов для временных серверных ошибок 500 и сетевых проблем (ТЗ п.7.1)
ONE_RETRY = 1


async def with_anthropic_retry(
    func: Callable[[], Awaitable[T]],
    config: Config,
    notify_admin: Callable[[str], Awaitable[None]] | None = None,
) -> T:
    """
    Выполняет вызов Claude API с экспоненциальными ретраями для временных ошибок.
    Пробрасывает исходное исключение после исчерпания попыток.
    """
    delays = [5, 15, 45]  # секунды между попытками

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await func()
        except anthropic.RateLimitError as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                delay = delays[attempt]
                logger.warning("Claude rate_limit (429), попытка %d/%d, жду %ds", attempt + 1, MAX_RETRIES, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("Claude rate_limit: исчерпаны все попытки")
        except anthropic.InternalServerError as e:
            last_exc = e
            status = getattr(e, "status_code", None)
            # 529 overloaded — до 3 повторов; 500 api_error — только 1 повтор (ТЗ п.7.1)
            max_for_this = MAX_RETRIES if status == 529 else ONE_RETRY + 1
            if attempt < max_for_this - 1:
                delay = delays[attempt]
                logger.warning("Claude server error %s, попытка %d/%d, жду %ds", status, attempt + 1, max_for_this, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("Claude server error %s: исчерпаны все попытки", status)
                raise
        except anthropic.AuthenticationError as e:
            logger.critical("Claude authentication_error (401): %s", e)
            if notify_admin:
                await notify_admin("🚨 Критическая ошибка: неверный ANTHROPIC_API_KEY (401)")
            raise
        except anthropic.BadRequestError as e:
            logger.error("Claude invalid_request_error (400): %s", e)
            raise
        except anthropic.APIConnectionError as e:
            last_exc = e
            # Сетевая ошибка — только 1 повтор (ТЗ п.7.1)
            if attempt < ONE_RETRY:
                logger.warning("Claude сетевая ошибка, попытка %d/%d, жду 3s", attempt + 1, ONE_RETRY + 1)
                await asyncio.sleep(3)
            else:
                logger.error("Claude сетевая ошибка: исчерпаны попытки")
                raise
        except asyncio.TimeoutError as e:
            logger.error("Claude таймаут (%ds)", config.anthropic_timeout)
            raise

    assert last_exc is not None
    raise last_exc


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
    if isinstance(exc, asyncio.TimeoutError):
        return "⏱ Превышено время ожидания ответа от Claude. Попробуйте позже."
    if isinstance(exc, anthropic.APIConnectionError):
        return "🌐 Ошибка соединения с Claude. Проверьте сеть и попробуйте снова."
    return "❌ Произошла непредвиденная ошибка. Попробуйте позже."
