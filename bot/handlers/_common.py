from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic
from aiogram.exceptions import TelegramBadRequest

from bot.utils import db
from bot.utils.anthropic.chat import (
    TurnUsage,
    call_claude_isolated,
    display_name,
    process_message,
    summarize,
)
from bot.utils.anthropic.models import ModelInfo
from bot.utils.errors import user_error_message
from bot.utils.html import TELEGRAM_MAX_LENGTH, markdown_to_html, split_long_message
from bot.utils.prompts import TIMEZONE_DETECT_PROMPT

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.config import Config

logger = logging.getLogger(__name__)

# Интервал обновления стримингового сообщения (секунды)
STREAM_UPDATE_INTERVAL = 1.5
# Длина куска ответа: запас под HTML-теги и экранирование до лимита Telegram
TELEGRAM_SAFE_LENGTH = 3800

# Временные сбои API: пользователю — понятное сообщение, в лог — WARNING без баг-репорта
TRANSIENT_API_ERRORS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,  # включая APITimeoutError
    asyncio.TimeoutError,
)

# Ссылки на фоновые задачи, чтобы их не собрал сборщик мусора
_background_tasks: set[asyncio.Task] = set()


def context_budget(config: "Config", model: ModelInfo) -> int:
    """Рабочий бюджет контекста: меньше окна модели, чтобы диалог оставался дешёвым и быстрым."""
    return min(config.context_budget_tokens, model.context_window)


async def _try_detect_timezone(
    client: anthropic.AsyncAnthropic,
    config: "Config",
    model: ModelInfo,
    user_id: int,
    messages: list[dict],
) -> None:
    """Пытается определить часовой пояс из последних сообщений."""
    try:
        recent = messages[-6:] if len(messages) >= 6 else messages
        formatted = "\n".join(
            f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: "
            + (m["content"] if isinstance(m["content"], str) else "[медиа]")
            for m in recent
        )
        prompt = TIMEZONE_DETECT_PROMPT.format(messages=formatted)
        text, _ = await call_claude_isolated(client, config, model, prompt)

        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1:
            return
        data = json.loads(text[start:end])
        tz = data.get("timezone")
        if tz:
            ZoneInfo(tz)  # проверяем, что такой пояс существует
            await db.update_timezone(user_id, tz)
            logger.debug("Определён часовой пояс %s для user_id=%d", tz, user_id)
    except (ZoneInfoNotFoundError, ValueError):
        logger.debug("Модель вернула неизвестный часовой пояс")
    except Exception:
        logger.debug("Не удалось определить часовой пояс", exc_info=True)


async def _send_answer(placeholder: "Message", message: "Message", text: str) -> None:
    """Отправляет готовый ответ: первая часть — в сообщение-заглушку, остальные — новыми.

    Если HTML не прошёл (слишком длинный после разметки или Telegram не разобрал теги),
    часть отправляется обычным текстом — ответ не теряется.
    """
    for i, part in enumerate(split_long_message(text, TELEGRAM_SAFE_LENGTH)):
        send = placeholder.edit_text if i == 0 else message.answer
        html = markdown_to_html(part)
        try:
            if len(html) > TELEGRAM_MAX_LENGTH:
                raise ValueError("HTML длиннее лимита Telegram")
            await send(html, parse_mode="HTML")
        except (TelegramBadRequest, ValueError) as exc:
            logger.warning("Ответ отправлен без разметки: %s", exc)
            await send(part, parse_mode=None)


async def handle_incoming(
    message: "Message",
    config: "Config",
    client: anthropic.AsyncAnthropic,
    content: list[dict] | str,
) -> None:
    """
    Универсальный обработчик входящего сообщения (текст / фото / документ).
    Выполняет стриминг, обновляет историю, проверяет триггеры саммаризации.
    """
    chat_id = message.chat.id
    user_id = message.from_user.id  # type: ignore[union-attr]

    # Загружаем данные пользователя и историю
    user_row = await db.get_user(user_id)
    user_tz = user_row["timezone"] if user_row else config.default_timezone
    history = await db.get_history(chat_id)

    # Текущая модель и уровень размышлений (выбирает администратор)
    model, effort = await db.get_model_settings()

    # Проверяем триггер по времени (72ч тишины)
    last_msg_str = history.get("last_message_at")
    timeout_summary_needed = False
    if last_msg_str:
        last_msg_at = datetime.fromisoformat(last_msg_str)
        if last_msg_at.tzinfo is None:
            last_msg_at = last_msg_at.replace(tzinfo=timezone.utc)
        hours_silent = (datetime.now(timezone.utc) - last_msg_at).total_seconds() / 3600
        if hours_silent >= config.summary_trigger_hours:
            timeout_summary_needed = True

    # Заглушка "печатает..."
    thinking_msg = await message.answer("⏳")

    full_text = ""
    usage: TurnUsage | None = None

    try:
        # Запускаем стриминг
        gen = process_message(
            client=client,
            config=config,
            model=model,
            effort=effort,
            chat_id=chat_id,
            user_id=user_id,
            new_content=content,
            user_tz=user_tz,
            db_history=history,
            user_name=display_name(
                message.from_user.first_name,  # type: ignore[union-attr]
                message.from_user.last_name,  # type: ignore[union-attr]
                message.from_user.username,  # type: ignore[union-attr]
            ),
        )

        last_edit = time.monotonic()

        async for chunk, chunk_usage in gen:
            if chunk:
                full_text += chunk
                now = time.monotonic()
                if (
                    now - last_edit >= STREAM_UPDATE_INTERVAL
                    and len(full_text) <= TELEGRAM_SAFE_LENGTH
                ):
                    try:
                        await thinking_msg.edit_text(
                            markdown_to_html(full_text) + " ▌",
                            parse_mode="HTML",
                        )
                        last_edit = now
                    except Exception:
                        pass
            if chunk_usage:
                usage = chunk_usage

    except Exception as exc:
        if isinstance(exc, TRANSIENT_API_ERRORS):
            # Временный сбой API (SDK уже сделал повторы) — не баг, баг-репорт не нужен
            logger.warning("Временная ошибка Claude API для chat_id=%d: %r", chat_id, exc)
        else:
            # ERROR уходит администратору баг-репортом (в т.ч. неверный ключ, 400, баги кода)
            logger.exception("Ошибка при обработке сообщения chat_id=%d, user_id=%d", chat_id, user_id)
        await thinking_msg.edit_text(user_error_message(exc))
        return

    if not full_text:
        full_text = "_(пустой ответ)_"

    # Сохраняем историю и расходы до отправки — ответ не потеряется при ошибке Telegram
    raw = history.get("messages_json") or "[]"
    live_history: list[dict] = json.loads(raw)
    live_history.append({"role": "user", "content": content})
    live_history.append({"role": "assistant", "content": full_text})

    if usage:
        await db.log_usage(
            chat_id, user_id, usage.input_tokens, usage.output_tokens, usage.cost, model.id,
            usage.cache_write_tokens, usage.cache_read_tokens,
        )

    # Реальный размер контекста последнего запроса (промпт + ответ)
    total_tokens = usage.context_tokens if usage else (history.get("total_tokens_approx") or 0)
    await db.save_history(chat_id=chat_id, messages=live_history, total_tokens=total_tokens)

    try:
        await _send_answer(thinking_msg, message, full_text)
    except Exception:
        logger.exception("Не удалось отправить ответ в chat_id=%d", chat_id)

    # Проверяем триггер по токенам
    token_threshold = int(context_budget(config, model) * config.summary_trigger_tokens)
    token_summary_needed = total_tokens >= token_threshold

    if timeout_summary_needed or token_summary_needed:
        trigger = "timeout" if timeout_summary_needed else "tokens"
        logger.info(
            "Триггер саммаризации [%s] для chat_id=%d (токены: %d/%d)",
            trigger, chat_id, total_tokens, token_threshold,
        )
        keep = config.summary_keep_last * 2  # пар → сообщений
        to_summarize = live_history[:-keep] if len(live_history) > keep else live_history
        kept_history = live_history[-keep:] if len(live_history) > keep else []
        try:
            new_summary, _ = await summarize(
                client=client,
                config=config,
                model=model,
                messages_to_summarize=to_summarize,
                prev_summary=history.get("summary"),
                timeout_trigger=timeout_summary_needed,
            )
            if not new_summary.strip():
                raise RuntimeError("модель вернула пустое саммари")
            # Историю обрезаем только после успешной саммаризации
            live_history = kept_history
            await db.save_history(
                chat_id=chat_id,
                messages=live_history,
                total_tokens=0,  # точный размер станет известен на следующем запросе
                summary=new_summary,
                summary_updated_at=datetime.now(timezone.utc),
            )
            logger.info("Саммаризация выполнена для chat_id=%d", chat_id)
        except Exception:
            logger.exception("Ошибка саммаризации для chat_id=%d", chat_id)

    # Фоновое определение часового пояса (раз в 10 сообщений)
    if len(live_history) % 10 == 0:
        task = asyncio.create_task(
            _try_detect_timezone(client, config, model, user_id, live_history)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
