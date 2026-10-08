from __future__ import annotations

import logging
import asyncio
import time
from typing import TYPE_CHECKING

import anthropic
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

# Ретраи вызовов Claude (429, 529, 5xx, сетевые ошибки) выполняет сам SDK
# с экспоненциальной задержкой и учётом retry-after: см. ANTHROPIC_MAX_RETRIES.

# Временные сбои API (SDK уже сделал повторы): пользователю — понятное сообщение,
# в лог — WARNING без баг-репорта. В SDK 1.x перегрузка (529) — отдельный класс,
# не наследник InternalServerError.
TRANSIENT_API_ERRORS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    anthropic.APIConnectionError,  # включая APITimeoutError
    asyncio.TimeoutError,
)

# Оповещение о закончившемся балансе — не чаще раза в час
BILLING_ALERT_COOLDOWN_SECONDS = 3600
BILLING_URL = "https://platform.claude.com/settings/billing"
_last_billing_alert: float = 0.0


def is_billing_error(exc: BaseException) -> bool:
    """Закончились средства на счёте Anthropic (402 billing_error или «credit balance is too low»)."""
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    if exc.status_code == 402 or getattr(exc, "type", None) == "billing_error":
        return True
    return "credit balance" in str(exc).lower()


async def alert_admin_billing(bot: "Bot", admin_id: int) -> None:
    """Срочное уведомление админу о нулевом балансе (с ограничением частоты)."""
    global _last_billing_alert
    now = time.monotonic()
    if _last_billing_alert and now - _last_billing_alert < BILLING_ALERT_COOLDOWN_SECONDS:
        return
    _last_billing_alert = now
    try:
        await bot.send_message(
            admin_id,
            "💳 <b>Закончились средства на счёте Anthropic API</b>\n\n"
            "Бот не может отвечать, пока счёт не пополнен.\n"
            f"Пополнить: {BILLING_URL}\n\n"
            "<i>Пока проблема не решена, напоминаю не чаще раза в час.</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Не удалось отправить админу оповещение о балансе: %s", exc)


async def log_api_error(
    log: logging.Logger,
    exc: BaseException,
    what: str,
    bot: "Bot | None" = None,
    admin_id: int | None = None,
) -> None:
    """Логирует ошибку вызова Claude по её виду.

    Нет денег на счёте — WARNING + срочное оповещение админу;
    временный сбой API — WARNING; всё остальное — ERROR с трейсбеком (баг-репорт).
    """
    if is_billing_error(exc):
        log.warning("%s: закончился баланс Anthropic API (%r)", what, exc)
        if bot is not None and admin_id is not None:
            await alert_admin_billing(bot, admin_id)
    elif isinstance(exc, TRANSIENT_API_ERRORS):
        log.warning("%s: временная ошибка Claude API: %r", what, exc)
    else:
        log.error(what, exc_info=exc, stacklevel=2)


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


async def close_menu(message: Message) -> None:
    """Закрывает inline-меню: удаляет сообщение, а если нельзя — убирает кнопки.

    Удалить не получится, если сообщение уже удалено (двойное нажатие «Закрыть»)
    или старше 48 часов — это не ошибка бота, баг-репорт не нужен.
    """
    try:
        await message.delete()
    except TelegramBadRequest:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


def user_error_message(exc: Exception) -> str:
    """Возвращает понятное сообщение пользователю на русском."""
    if is_billing_error(exc):
        return (
            "💳 У бота закончились средства на счёте Claude, поэтому ответить не получится. "
            "Администратор уже знает — попробуйте позже."
        )
    if isinstance(exc, anthropic.RateLimitError):
        return "⏳ Превышен лимит запросов к Claude. Попробуйте через минуту."
    if isinstance(exc, anthropic.OverloadedError) or getattr(exc, "status_code", None) == 529:
        return "⚙️ Claude сейчас перегружен. Попробуйте через несколько минут."
    if isinstance(exc, (
        anthropic.InternalServerError,
        anthropic.ServiceUnavailableError,
        anthropic.DeadlineExceededError,
    )):
        return "⚙️ Временная ошибка сервера Claude. Попробуйте позже."
    if isinstance(exc, anthropic.AuthenticationError):
        return "🔧 Ошибка конфигурации бота. Администратор уведомлён."
    if isinstance(exc, anthropic.RequestTooLargeError):
        return "📦 Сообщение или файл слишком большие для Claude. Попробуйте поменьше."
    if isinstance(exc, anthropic.BadRequestError):
        return "❌ Некорректный запрос. Попробуйте переформулировать."
    if isinstance(exc, (asyncio.TimeoutError, anthropic.APITimeoutError)):
        return "⏱ Превышено время ожидания ответа от Claude. Попробуйте позже."
    if isinstance(exc, anthropic.APIConnectionError):
        return "🌐 Ошибка соединения с Claude. Проверьте сеть и попробуйте снова."
    return "❌ Произошла непредвиденная ошибка. Попробуйте позже."
