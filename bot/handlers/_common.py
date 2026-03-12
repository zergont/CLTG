from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Awaitable

import anthropic

from bot.utils import db
from bot.utils.anthropic.chat import summarize, process_message, call_claude_isolated
from bot.utils.anthropic.models import context_limit
from bot.utils.errors import user_error_message
from bot.utils.html import markdown_to_html, split_long_message
from bot.utils.prompts import TIMEZONE_DETECT_PROMPT

if TYPE_CHECKING:
    from aiogram.types import Message
    from aiogram import Bot
    from bot.config import Config

logger = logging.getLogger(__name__)

# Интервал обновления стримингового сообщения (секунды)
STREAM_UPDATE_INTERVAL = 1.5


def _get_prices(config: "Config", model: str) -> tuple[float, float]:
    if "sonnet" in model:
        return config.price_input_sonnet, config.price_output_sonnet
    return config.price_input_haiku, config.price_output_haiku


async def _try_detect_timezone(
    client: anthropic.AsyncAnthropic,
    config: "Config",
    model: str,
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
            await db.update_timezone(user_id, tz)
            logger.debug("Определён часовой пояс %s для user_id=%d", tz, user_id)
    except Exception:
        logger.debug("Не удалось определить часовой пояс", exc_info=True)


async def handle_incoming(
    message: "Message",
    config: "Config",
    client: anthropic.AsyncAnthropic,
    content: list[dict] | str,
    notify_admin: "Callable[[str], Awaitable[None]] | None" = None,
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

    # Получаем текущую модель
    model = await db.get_setting("current_model") or config.model_haiku

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

    try:
        # Запускаем стриминг
        gen = process_message(
            client=client,
            config=config,
            model=model,
            chat_id=chat_id,
            new_content=content,
            user_tz=user_tz,
            db_history=history,
            notify_admin=notify_admin,
        )

        full_text = ""
        usage: anthropic.Usage | None = None
        last_edit = asyncio.get_event_loop().time()

        async for chunk, chunk_usage in gen:
            if chunk:
                full_text += chunk
                now = asyncio.get_event_loop().time()
                if now - last_edit >= STREAM_UPDATE_INTERVAL:
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

        # Финальное обновление сообщения
        if not full_text:
            full_text = "_(пустой ответ)_"

        parts = split_long_message(markdown_to_html(full_text))
        await thinking_msg.edit_text(parts[0], parse_mode="HTML")
        for part in parts[1:]:
            await message.answer(part, parse_mode="HTML")

    except Exception as exc:
        logger.exception("Ошибка при обращении к Claude: %s", exc)
        await thinking_msg.edit_text(user_error_message(exc))
        return

    # Сохраняем историю
    raw = history.get("messages_json") or "[]"
    live_history: list[dict] = json.loads(raw)
    summary_text = history.get("summary")

    # Добавляем новые пары
    if isinstance(content, str):
        live_history.append({"role": "user", "content": content})
    else:
        live_history.append({"role": "user", "content": content})
    live_history.append({"role": "assistant", "content": full_text})

    # Логируем usage
    input_tokens = usage.input_tokens if usage else 0
    output_tokens = usage.output_tokens if usage else 0
    price_input, price_output = _get_prices(config, model)
    cost = input_tokens * price_input + output_tokens * price_output

    if usage:
        await db.log_usage(chat_id, user_id, input_tokens, output_tokens, cost, model)

    # Обновляем общее приближение токенов
    total_tokens = (history.get("total_tokens_approx") or 0) + input_tokens + output_tokens

    # Проверяем триггер по токенам
    limit = context_limit(model)
    token_threshold = int(limit * config.summary_trigger_tokens)
    token_summary_needed = total_tokens >= token_threshold

    new_summary = None
    summary_updated_at = None

    if timeout_summary_needed or token_summary_needed:
        trigger = "timeout" if timeout_summary_needed else "tokens"
        logger.info(
            "Триггер саммаризации [%s] для chat_id=%d (токены: %d/%d)",
            trigger, chat_id, total_tokens, token_threshold,
        )
        try:
            keep = config.summary_keep_last * 2  # пар → сообщений
            to_summarize = live_history[:-keep] if len(live_history) > keep else live_history
            live_history = live_history[-keep:] if len(live_history) > keep else []

            new_summary, _ = await summarize(
                client=client,
                config=config,
                model=model,
                messages_to_summarize=to_summarize,
                prev_summary=summary_text,
                timeout_trigger=timeout_summary_needed,
            )
            summary_updated_at = datetime.now(timezone.utc)
            total_tokens = output_tokens  # сбрасываем счётчик
            logger.info("Саммаризация выполнена для chat_id=%d", chat_id)
        except Exception:
            logger.exception("Ошибка саммаризации для chat_id=%d", chat_id)

    await db.save_history(
        chat_id=chat_id,
        messages=live_history,
        total_tokens=total_tokens,
        summary=new_summary,
        summary_updated_at=summary_updated_at,
    )

    # Фоновое определение часового пояса (раз в 10 сообщений)
    if len(live_history) % 10 == 0:
        asyncio.create_task(
            _try_detect_timezone(client, config, model, user_id, live_history)
        )
