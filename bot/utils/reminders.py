from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytz

from bot.utils import db
from bot.utils.anthropic.chat import call_claude_isolated
from bot.utils.anthropic.models import context_limit
from bot.utils.errors import handle_telegram_error

if TYPE_CHECKING:
    import anthropic
    from aiogram import Bot
    from bot.config import Config

logger = logging.getLogger(__name__)


def _next_due(current_due: datetime, interval_seconds: int) -> datetime:
    """Вычисляет следующее время срабатывания."""
    return current_due + timedelta(seconds=interval_seconds)


def _parse_interval(meta: dict | None) -> int | None:
    if not meta:
        return None
    return meta.get("interval_seconds")


async def run_scheduler(bot: "Bot", config: "Config", client: "anthropic.AsyncAnthropic") -> None:
    """Основной цикл планировщика напоминаний."""
    logger.info("Планировщик напоминаний запущен (интервал: %ds)", config.reminder_poll_interval)

    while True:
        try:
            await _process_batch(bot, config, client)
        except Exception:
            logger.exception("Ошибка в цикле планировщика")
        await asyncio.sleep(config.reminder_poll_interval)


async def _process_batch(
    bot: "Bot",
    config: "Config",
    client: "anthropic.AsyncAnthropic",
) -> None:
    now = datetime.now(timezone.utc)

    # Случайный джиттер для защиты от дрейфа
    jitter = random.uniform(0, config.reminder_jitter)
    now_with_jitter = now + timedelta(seconds=jitter)

    rows = await db.pick_due_reminders(
        now=now_with_jitter,
        lookahead_seconds=config.reminder_lookahead,
        batch_limit=config.reminder_batch_limit,
    )

    if not rows:
        return

    logger.info("Обрабатываю %d напоминаний", len(rows))

    tasks = [
        _fire_reminder(bot, config, client, dict(row), now)
        for row in rows
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _fire_reminder(
    bot: "Bot",
    config: "Config",
    client: "anthropic.AsyncAnthropic",
    reminder: dict,
    now: datetime,
) -> None:
    reminder_id = reminder["id"]
    chat_id = reminder["chat_id"]
    user_id = reminder["user_id"]
    text = reminder["text"]
    prompt = reminder.get("prompt")
    is_chain = bool(reminder.get("is_chain"))
    silent = bool(reminder.get("silent", 0))
    steps_left = reminder.get("steps_left")
    end_at_str = reminder.get("end_at")
    meta = json.loads(reminder["meta_json"]) if reminder.get("meta_json") else {}

    try:
        # Получаем текущую модель
        model = await db.get_setting("current_model") or config.model_haiku

        # Определяем текст для отправки
        if prompt:
            # Вызов Claude с изолированным промптом
            response_text, usage = await call_claude_isolated(
                client, config, model, prompt
            )
            # Логируем стоимость
            price_input, price_output = _get_prices(config, model)
            cost = usage.input_tokens * price_input + usage.output_tokens * price_output
            await db.log_usage(chat_id, user_id, usage.input_tokens, usage.output_tokens, cost, model)

            send_text = response_text
            history_user = f"🔔 REMINDER: {text}"
            history_assistant = response_text
        else:
            send_text = f"🔔 {text}"
            history_user = f"🔔 REMINDER: {text}"
            history_assistant = text

        # Отправляем сообщение
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=send_text,
                disable_notification=silent,
            )
        except Exception as send_exc:
            handled = await handle_telegram_error(send_exc, chat_id=chat_id, user_id=user_id)
            if not handled:
                raise
            return  # бот заблокирован — прекращаем обработку

        # Добавляем в историю диалога
        history = await db.get_history(chat_id)
        raw = history.get("messages_json") or "[]"
        messages: list[dict] = json.loads(raw)
        messages.append({"role": "user", "content": history_user})
        messages.append({"role": "assistant", "content": history_assistant})
        tokens = history.get("total_tokens_approx") or 0
        await db.save_history(chat_id, messages, tokens)

        # Планируем следующее срабатывание
        reschedule_at = None
        new_steps_left = None

        if is_chain:
            interval = _parse_interval(meta)

            # Проверяем ограничения
            if steps_left is not None:
                new_steps = steps_left - 1
                if new_steps <= 0:
                    reschedule_at = None  # завершаем
                else:
                    new_steps_left = new_steps
                    reschedule_at = _next_due(
                        datetime.fromisoformat(reminder["due_at"]),
                        interval or 86400,
                    )
            else:
                new_steps_left = None
                reschedule_at = _next_due(
                    datetime.fromisoformat(reminder["due_at"]),
                    interval or 86400,
                )

            # Проверяем end_at
            if reschedule_at and end_at_str:
                end_at = datetime.fromisoformat(end_at_str)
                if end_at.tzinfo is None:
                    end_at = end_at.replace(tzinfo=timezone.utc)
                if reschedule_at > end_at:
                    reschedule_at = None

        await db.complete_reminder(reminder_id, reschedule_at, new_steps_left)
        logger.info("Напоминание #%d отправлено chat_id=%d", reminder_id, chat_id)

    except Exception as e:
        logger.exception("Ошибка при обработке напоминания #%d: %s", reminder_id, e)


def _get_prices(config: "Config", model: str) -> tuple[float, float]:
    if "sonnet" in model:
        return config.price_input_sonnet, config.price_output_sonnet
    return config.price_input_haiku, config.price_output_haiku


async def parse_reminder(
    client: "anthropic.AsyncAnthropic",
    config: "Config",
    model: str,
    user_text: str,
    user_tz: str,
) -> dict | None:
    """
    Парсит текст пользователя в параметры напоминания через Claude.
    Возвращает dict с параметрами или None при ошибке.
    """
    from bot.utils.prompts import REMINDER_PARSE_PROMPT

    try:
        tz = pytz.timezone(user_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone(config.default_timezone)

    current_time = datetime.now(tz).strftime("%d.%m.%Y %H:%M %Z")

    prompt = REMINDER_PARSE_PROMPT.format(
        current_time=current_time,
        timezone=user_tz,
        user_text=user_text,
    )

    try:
        text, _ = await call_claude_isolated(client, config, model, prompt)
        # Ищем JSON в ответе
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        return json.loads(text[start:end])
    except Exception:
        logger.exception("Ошибка парсинга напоминания")
        return None
