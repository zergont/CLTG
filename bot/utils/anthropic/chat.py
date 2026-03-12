from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import AsyncGenerator, Callable, Awaitable

import anthropic
import pytz

from bot.config import Config
from bot.utils.errors import with_anthropic_retry

logger = logging.getLogger(__name__)

WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20250305",
    "name": "web_search",
}


def _build_system_prompt(config: Config, user_tz: str) -> str:
    """Добавляет текущее время к системному промпту."""
    try:
        tz = pytz.timezone(user_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone(config.default_timezone)
    now = datetime.now(tz).strftime("%d.%m.%Y %H:%M %Z")
    return f"{config.system_prompt}\n\nТекущее время: {now}"


def _build_messages(
    live_history: list[dict],
    summary: str | None,
    new_content: list[dict] | str,
) -> list[dict]:
    """Формирует итоговый массив messages[] для API.

    Структура:
      [summary_user + summary_assistant]  - если есть
      [... live_history ...]
      [user: new_content]
    """
    messages: list[dict] = []
    if summary:
        messages.append({"role": "user", "content": f"SUMMARY: {summary}"})
        messages.append({"role": "assistant", "content": "Понял, продолжаем."})
    messages.extend(live_history)
    messages.append({"role": "user", "content": new_content})  # type: ignore[arg-type]
    return messages


async def stream_response(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: str,
    messages: list[dict],
    system: str,
) -> AsyncGenerator[tuple[str, anthropic.Usage | None], None]:
    """Async generator: стриминг ответа Claude.

    Yields (chunk_text, None) для каждого текстового чанка.
    Финальный yield: ("", usage).
    """
    collected_usage: anthropic.Usage | None = None

    async def _open_stream():
        return client.messages.stream(
            model=model,
            max_tokens=8192,
            system=system,
            messages=messages,
            tools=[WEB_SEARCH_TOOL],  # type: ignore[list-item]
        )

    stream_cm = await with_anthropic_retry(_open_stream, config)
    async with stream_cm as stream:
        async for text_chunk in stream.text_stream:
            yield text_chunk, None
        final_msg = await stream.get_final_message()
        if final_msg and final_msg.usage:
            collected_usage = final_msg.usage
    yield "", collected_usage


async def call_claude_isolated(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: str,
    prompt: str,
    system: str | None = None,
) -> tuple[str, anthropic.Usage]:
    """Изолированный вызов Claude без истории. Возвращает (text, usage)."""
    sys_prompt = system or config.system_prompt

    async def _call():
        return await client.messages.create(
            model=model,
            max_tokens=4096,
            system=sys_prompt,
            messages=[{"role": "user", "content": prompt}],
        )

    response = await with_anthropic_retry(_call, config)
    text = response.content[0].text if response.content else ""
    return text, response.usage


SUMMARY_PROMPT_TEMPLATE = (
    "Создай структурированное резюме диалога.\n"
    "Выдели:\n"
    "1. Факты о пользователе (имя, локация, профессия, предпочтения)\n"
    "2. Принятые решения и договорённости\n"
    "3. Технические детали, которые могут понадобиться в будущем\n"
    "4. Незакрытые вопросы и задачи\n\n"
    "Предыдущее саммари (если есть):\n{prev_summary}\n\n"
    "Диалог для саммаризации:\n{dialogue}\n\n"
    "Ответь только структурированным резюме без вводных фраз."
)

SUMMARY_PROMPT_TIMEOUT = (
    "Создай структурированное резюме завершённой сессии.\n"
    "Акцент на итогах и незакрытых вопросах (прошло более 72 часов).\n\n"
    "Выдели:\n"
    "1. Факты о пользователе (имя, локация, профессия, предпочтения)\n"
    "2. Итоги сессии - что было сделано\n"
    "3. Незакрытые вопросы и задачи на будущее\n"
    "4. Технические детали для контекста\n\n"
    "Предыдущее саммари (если есть):\n{prev_summary}\n\n"
    "Диалог:\n{dialogue}\n\n"
    "Ответь только структурированным резюме без вводных фраз."
)


def _format_dialogue(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = "Пользователь" if m["role"] == "user" else "Ассистент"
        content = m["content"]
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = " ".join(text_parts)
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def summarize(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: str,
    messages_to_summarize: list[dict],
    prev_summary: str | None,
    timeout_trigger: bool = False,
) -> tuple[str, anthropic.Usage]:
    """Создаёт новое саммари, объединяя со старым."""
    template = SUMMARY_PROMPT_TIMEOUT if timeout_trigger else SUMMARY_PROMPT_TEMPLATE
    prompt = template.format(
        prev_summary=prev_summary or "отсутствует",
        dialogue=_format_dialogue(messages_to_summarize),
    )
    return await call_claude_isolated(client, config, model, prompt)


def process_message(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: str,
    chat_id: int,
    new_content: list[dict] | str,
    user_tz: str,
    db_history: dict,
    notify_admin: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncGenerator[tuple[str, anthropic.Usage | None], None]:
    """Точка входа для обработки входящего сообщения.

    Обычная (не async) функция - возвращает async generator напрямую.
    Использование: async for chunk, usage in process_message(...): ...
    """
    raw = db_history.get("messages_json") or "[]"
    live_history: list[dict] = json.loads(raw)
    summary: str | None = db_history.get("summary")
    system = _build_system_prompt(config, user_tz)
    messages = _build_messages(live_history, summary, new_content)
    return stream_response(client, config, model, messages, system)
