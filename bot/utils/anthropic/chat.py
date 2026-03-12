from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import AsyncGenerator, Callable, Awaitable

import anthropic
import httpx
import pytz

from bot.config import Config
from bot.utils.errors import with_anthropic_retry

logger = logging.getLogger(__name__)

# Флаг доступности SearXNG, устанавливается при старте через init_searxng()
_searxng_available: bool = False

# Custom tool — через SearXNG
WEB_SEARCH_TOOL: dict = {
    "name": "web_search",
    "description": (
        "Поиск актуальной информации в интернете. Используй для вопросов "
        "о текущих событиях, новостях, погоде, ценах и любой информации, "
        "которая могла измениться с момента обучения."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос",
            }
        },
        "required": ["query"],
    },
}


# Нативный tool Anthropic — fallback если SearXNG недоступен
NATIVE_WEB_SEARCH_TOOL: dict = {
    "type": "web_search_20250305",
    "name": "web_search",
}


async def init_searxng(url: str, engine: str = "auto") -> bool:
    """Проверяет доступность SearXNG. Вызывается один раз при старте бота.

    engine: auto | searxng | native
    """
    global _searxng_available

    if engine == "native":
        _searxng_available = False
        logger.info("🔍 Поисковый движок: нативный Anthropic web_search")
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{url}/search",
                params={"q": "test", "format": "json"},
            )
            r.raise_for_status()
        _searxng_available = True
        logger.info("✅ Поисковый движок: SearXNG (%s)", url)
        return True
    except Exception as exc:
        _searxng_available = False
        if engine == "searxng":
            logger.warning(
                "⚠️  SearXNG недоступен (%s). Веб-поиск отключён.", exc
            )
        else:  # auto
            logger.warning(
                "⚠️  SearXNG недоступен (%s). Fallback: нативный Anthropic web_search.", exc
            )
        return False


async def _searxng_search(query: str, searxng_url: str, max_results: int = 5) -> str:
    """HTTP-запрос к SearXNG, возвращает текст с результатами."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{searxng_url}/search",
                params={"q": query, "format": "json", "categories": "general"},
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])[:max_results]
            if not results:
                return "Поиск не вернул результатов."
            lines = []
            for i, item in enumerate(results, 1):
                title = item.get("title", "")
                url = item.get("url", "")
                content = item.get("content", "")
                lines.append(f"{i}. {title}\n   URL: {url}\n   {content}")
            return "\n\n".join(lines)
    except Exception as exc:
        logger.warning("Ошибка SearXNG поиска: %s", exc)
        return f"Поиск временно недоступен: {exc}"


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

    Поддерживает agentic loop: если Claude решает искать — выполняет запрос
    к SearXNG и отправляет результат обратно.

    Yields (chunk_text, None) для каждого текстового чанка.
    Финальный yield: ("", usage) с суммарным usage всех вызовов.
    """
    total_input = 0
    total_output = 0
    current_messages = list(messages)

    # Fallback на нативный tool если SearXNG недоступен
    if not _searxng_available:
        async def _native_stream():
            return client.messages.stream(
                model=model,
                max_tokens=8192,
                system=system,
                messages=current_messages,
                tools=[NATIVE_WEB_SEARCH_TOOL],  # type: ignore[list-item]
            )
        stream_cm = await with_anthropic_retry(_native_stream, config)
        async with stream_cm as stream:
            async for text_chunk in stream.text_stream:
                yield text_chunk, None
            final_msg = await stream.get_final_message()
        yield "", (final_msg.usage if final_msg else None)
        return

    for _iteration in range(3):  # максимум 3 итерации поиска
        msgs = current_messages

        async def _open_stream(m=msgs):
            return client.messages.stream(
                model=model,
                max_tokens=8192,
                system=system,
                messages=m,
                tools=[WEB_SEARCH_TOOL],  # type: ignore[list-item]
            )

        stream_cm = await with_anthropic_retry(_open_stream, config)
        async with stream_cm as stream:
            async for text_chunk in stream.text_stream:
                yield text_chunk, None
            final_msg = await stream.get_final_message()

        if final_msg and final_msg.usage:
            total_input += final_msg.usage.input_tokens
            total_output += final_msg.usage.output_tokens

        if not final_msg or final_msg.stop_reason != "tool_use":
            break

        tool_block = next(
            (b for b in final_msg.content if b.type == "tool_use"),
            None,
        )
        if not tool_block:
            break

        query = tool_block.input.get("query", "")  # type: ignore[union-attr]
        logger.info("SearXNG поиск: %s", query)
        search_result = await _searxng_search(query, config.searxng_url)

        current_messages = current_messages + [
            {"role": "assistant", "content": final_msg.content},
            {"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": search_result,
                }
            ]},
        ]

    yield "", anthropic.types.Usage(input_tokens=total_input, output_tokens=total_output)


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
