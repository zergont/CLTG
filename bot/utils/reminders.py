from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from bot.utils import db
from bot.utils.anthropic.chat import call_claude_isolated
from bot.utils.errors import handle_telegram_error, log_api_error

if TYPE_CHECKING:
    import anthropic
    from aiogram import Bot
    from bot.config import Config

logger = logging.getLogger(__name__)


# Минимальный интервал повтора — защита от напоминаний «каждую секунду»
MIN_INTERVAL_SECONDS = 60


def _next_due(current_due: datetime, interval_seconds: int, now: datetime) -> tuple[datetime, int]:
    """Следующее срабатывание в будущем и число пропущенных (если бот был выключен).

    Пропущенные повторы не догоняются пачкой: сразу переходим к ближайшему будущему.
    """
    step = timedelta(seconds=max(interval_seconds, MIN_INTERVAL_SECONDS))
    next_due = current_due + step
    skipped = 0
    if next_due <= now:
        skipped = int((now - next_due) / step) + 1
        next_due += step * skipped
    return next_due, skipped


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
        send_text = f"🔔 {text}"
        history_user = f"🔔 REMINDER: {text}"
        history_assistant = text

        if prompt:
            # Вызов Claude с изолированным промптом на текущей модели
            model, effort = await db.get_model_settings()
            try:
                response_text, usage = await call_claude_isolated(
                    client, config, model, prompt, effort=effort,
                )
                await db.log_usage(
                    chat_id, user_id, usage.input_tokens, usage.output_tokens, usage.cost,
                    model.id, usage.cache_write_tokens, usage.cache_read_tokens,
                )
                if response_text.strip():
                    send_text = response_text
                    history_assistant = response_text
            except Exception as exc:
                # Не удалось сгенерировать — отправляем обычный текст напоминания
                await log_api_error(
                    logger, exc, f"Ошибка генерации текста напоминания #{reminder_id}",
                    bot, config.admin_id,
                )

        # Отправляем сообщение
        delivered = True
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=send_text,
                disable_notification=silent,
            )
        except Exception as send_exc:
            if not await handle_telegram_error(send_exc, chat_id=chat_id, user_id=user_id):
                raise
            # Пользователь заблокировал бота: не повторяем бесконечно — разовое закрываем,
            # повторяющееся переносим (вдруг разблокирует)
            delivered = False

        if delivered:
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
            new_steps_left = steps_left - 1 if steps_left is not None else None
            if new_steps_left is None or new_steps_left > 0:
                reschedule_at, skipped = _next_due(
                    _parse_utc(reminder["due_at"]),
                    _parse_interval(meta) or 86400,
                    now,
                )
                if skipped:
                    logger.info(
                        "Напоминание #%d: пропущено повторов за время простоя: %d", reminder_id, skipped,
                    )
            # Проверяем end_at
            if reschedule_at and end_at_str and reschedule_at > _parse_utc(end_at_str):
                reschedule_at = None

        await db.complete_reminder(reminder_id, reschedule_at, new_steps_left)
        if delivered:
            logger.info("Напоминание #%d отправлено chat_id=%d", reminder_id, chat_id)

    except Exception as e:
        # Сбой отправки: напоминание повторится через минуту, после нескольких неудач — failed
        gave_up = await db.fail_reminder(reminder_id)
        if gave_up:
            logger.exception(
                "Напоминание #%d не удалось отправить после %d попыток, отменено: %s",
                reminder_id, db.REMINDER_MAX_ATTEMPTS, e,
            )
        else:
            logger.warning("Ошибка при отправке напоминания #%d, будет повтор: %r", reminder_id, e)
