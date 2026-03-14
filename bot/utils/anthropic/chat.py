from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone as dt_timezone
from typing import AsyncGenerator

import aiosqlite
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

REMINDER_TOOL: dict = {
    "name": "create_reminder",
    "description": (
        "Создаёт напоминание для пользователя. Используй всегда когда пользователь "
        "просит что-то напомнить, поставить будильник или создать повторяющееся уведомление. "
        "Время due_at рассчитывай в UTC относительно текущего времени из системного промпта."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Текст напоминания"},
            "due_at": {"type": "string", "description": "Время срабатывания ISO 8601 UTC. Конвертируй из часового пояса пользователя используя смещение из системного промпта. Пример: если сейчас 14:30 UTC+03:00 и нужно через 30 мин → 2026-03-12T12:00:00Z"},
            "is_chain": {"type": "boolean", "description": "true если повторяющееся"},
            "interval_seconds": {"type": "integer", "description": "Интервал повтора в секундах"},
            "steps_left": {"type": "integer", "description": "Макс. число срабатываний (без поля = бессрочно)"},
            "end_at": {"type": "string", "description": "Дата окончания ISO 8601 UTC (без поля = бессрочно)"},
            "silent": {"type": "integer", "description": "1 = тихий режим, 0 = со звуком"},
        },
        "required": ["text", "due_at"],
    },
}


async def _execute_reminder_tool(
    tool_input: dict,
    chat_id: int,
    user_id: int,
    default_silent: bool,
) -> str:
    """Создаёт напоминание по параметрам от Claude, возвращает строку-результат."""
    from bot.utils import db as _db
    try:
        text = tool_input.get("text", "")
        due_at = datetime.fromisoformat(tool_input["due_at"].replace("Z", "+00:00"))
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=dt_timezone.utc)

        end_at = None
        if tool_input.get("end_at"):
            end_at = datetime.fromisoformat(tool_input["end_at"].replace("Z", "+00:00"))
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=dt_timezone.utc)

        is_chain = bool(tool_input.get("is_chain", False))
        silent = int(tool_input.get("silent", 1 if default_silent else 0))
        steps_left = tool_input.get("steps_left")
        interval_seconds = tool_input.get("interval_seconds")

        reminder_id = await _db.add_reminder(
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            due_at=due_at,
            prompt=tool_input.get("prompt"),
            is_chain=is_chain,
            silent=silent,
            steps_left=steps_left,
            end_at=end_at,
        )

        if interval_seconds:
            async with aiosqlite.connect(_db.DB_PATH) as conn:
                await conn.execute(
                    "UPDATE reminders SET meta_json = ? WHERE id = ?",
                    (json.dumps({"interval_seconds": interval_seconds}), reminder_id),
                )
                await conn.commit()

        chain_mark = " 🔁" if is_chain else ""
        silent_mark = " 🔕" if silent else ""
        steps_info = f", повторений: {steps_left}" if steps_left else ""
        logger.info("Создано напоминание #%d для user_id=%d: %s", reminder_id, user_id, text)
        return (
            f"Напоминание #{reminder_id} успешно создано{chain_mark}{silent_mark}. "
            f"Текст: «{text}». Время: {due_at.strftime('%d.%m.%Y %H:%M UTC')}{steps_info}."
        )
    except Exception as exc:
        logger.error("Ошибка создания напоминания через tool: %s", exc)
        return f"Ошибка создания напоминания: {exc}"


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


def _build_system_prompt(config: Config, user_tz: str) -> list[dict]:
    """Возвращает system-блоки для Claude API с кэшированием статической части.

    Структура:
      [0] статический блок (промпт + инструменты) — с cache_control
      [1] динамический блок (текущее время) — без cache_control
    """
    try:
        tz = pytz.timezone(user_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone(config.default_timezone)
    now_dt = datetime.now(tz)
    raw_offset = now_dt.strftime("%z")  # e.g. +0300 or -0530
    if raw_offset:
        utc_offset = f"UTC{raw_offset[:3]}:{raw_offset[3:]}"  # UTC+03:00
    else:
        utc_offset = "UTC"
    now = now_dt.strftime(f"%d.%m.%Y %H:%M %Z ({utc_offset})")

    # --- статическая часть (кэшируется) ---
    capabilities = (
        "\n\n## Твои инструменты и возможности\n\n"
        "### 🔍 web_search\n"
        "Поиск актуальной информации в интернете. Ты вызываешь инструмент с параметром "
        "`query` — он возвращает список результатов с заголовками, URL и описаниями.\n\n"
        "**Когда использовать:**\n"
        "- Вопросы о текущих событиях, новостях, датах мероприятий\n"
        "- Погода, курсы валют, цены, наличие товаров\n"
        "- Свежая документация, changelog, совместимость версий\n"
        "- Любые факты, которые могли измениться после твоего обучения\n"
        "- Проверка информации, в которой ты не уверен\n\n"
        "**Когда НЕ использовать:**\n"
        "- Общие знания, математика, программирование (если не нужна свежая документация)\n"
        "- Вопросы о самом пользователе или контексте диалога\n\n"
        "**Советы по запросам:**\n"
        "- Для технических тем формулируй запрос на английском — результаты будут точнее\n"
        "- Используй конкретные ключевые слова, а не полные предложения\n"
        "- Если первый поиск не дал результата — перефразируй запрос\n\n"
        "### ⏰ create_reminder\n"
        "Создание напоминаний, будильников и повторяющихся уведомлений.\n\n"
        "**Когда использовать:**\n"
        "- Любая просьба напомнить о чём-то: «напомни через 30 минут», «напомни завтра в 9»\n"
        "- Будильники: «поставь будильник на 7 утра»\n"
        "- Повторяющиеся уведомления: «каждый день в 9 утра — зарядка», «каждый понедельник — отчёт»\n"
        "- Таймеры: «через 2 часа напомни выключить духовку»\n\n"
        "**Расчёт времени (ВАЖНО):**\n"
        "Параметр `due_at` всегда в UTC. Ты обязан конвертировать локальное время пользователя "
        "в UTC, используя смещение из строки «Текущее время» ниже.\n\n"
        "Примеры конвертации (при UTC+03:00):\n"
        "- «в 9 утра» → ближайшие 09:00 локально → минус 3 часа → 06:00Z\n"
        "- «через 2 часа» → текущее UTC + 2 часа\n"
        "- «завтра в 18:00» → завтра 18:00 локально → минус 3 часа → 15:00Z\n\n"
        "**Повторяющиеся напоминания:**\n"
        "- Установи `is_chain: true` и `interval_seconds` (3600=час, 86400=день, 604800=неделя)\n"
        "- `steps_left` — ограничить число повторений (без параметра = бессрочно)\n"
        "- `end_at` — дата окончания в UTC (без параметра = бессрочно)\n"
        "- `silent` — 1=без звука (по умолчанию), 0=со звуком\n\n"
        "### 🧠 Память и контекст\n"
        "Ты ведёшь непрерывный диалог с пользователем. Между сессиями сохраняется "
        "структурированное саммари предыдущих разговоров. Запоминай и используй:\n"
        "- **Имя** — обращайся по имени, если пользователь представился\n"
        "- **Часовой пояс** — определяется автоматически и указан в «Текущем времени»\n"
        "- **Ключевые факты** — профессия, локация, предпочтения, проекты над которыми работает\n"
        "- **Незавершённые задачи** — если в саммари есть открытые вопросы, можешь напомнить о них\n"
        "- **Стиль общения** — подстраивайся под формальность/неформальность пользователя\n\n"
        "### 🕐 Текущее время\n"
        "В каждом запросе указано текущее время пользователя с часовым поясом "
        "и UTC-смещением. Используй его для:\n"
        "- Расчёта `due_at` в напоминаниях (конвертация в UTC)\n"
        "- Понимания относительных выражений («сегодня», «завтра», «через час», «в эту пятницу»)\n"
        "- Ответов на прямые вопросы о времени и дате\n"
        "- Определения уместности приветствия (утро/день/вечер)\n"
    )
    static_text = config.system_prompt + capabilities

    # --- динамическая часть (не кэшируется) ---
    dynamic_text = f"Текущее время: {now}"

    return [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": dynamic_text,
        },
    ]


def _build_messages(
    live_history: list[dict],
    summary: str | None,
    new_content: list[dict] | str,
) -> list[dict]:
    """Формирует итоговый массив messages[] для API с маркерами кэширования.

    Структура (с кэшем):
      [user: SUMMARY + cache_control]     — если есть
      [assistant: Понял]                  — если есть
      [... live_history[:-1] ...]
      [последний элемент истории + cache_control на последнем блоке]
      [user: new_content]                 — БЕЗ cache_control
    """
    messages: list[dict] = []

    # --- саммари ---
    if summary:
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"SUMMARY: {summary}",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        })
        messages.append({
            "role": "assistant",
            "content": "Понял, продолжаем.",
        })

    # --- живая история ---
    if live_history:
        # Все сообщения кроме последнего — как есть
        messages.extend(live_history[:-1])

        # Последнее сообщение истории получает cache_control
        last = live_history[-1]
        content = last["content"]

        if isinstance(content, str):
            cached_content = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        elif isinstance(content, list):
            cached_content = list(content)
            if cached_content:
                last_block = dict(cached_content[-1])
                last_block["cache_control"] = {"type": "ephemeral"}
                cached_content[-1] = last_block
        else:
            cached_content = content

        messages.append({"role": last["role"], "content": cached_content})

    # --- новое сообщение — без кэша ---
    messages.append({"role": "user", "content": new_content})  # type: ignore[arg-type]
    return messages


async def stream_response(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: str,
    messages: list[dict],
    system: list[dict],
    chat_id: int = 0,
    user_id: int = 0,
) -> AsyncGenerator[tuple[str, anthropic.Usage | None], None]:
    """Async generator: стриминг ответа Claude.

    Поддерживает agentic loop: обрабатывает tool_use блоки (web_search, create_reminder).
    Если SearXNG недоступен — используется нативный Anthropic web_search (сервер-сайд).

    Yields (chunk_text, None) для каждого текстового чанка.
    Финальный yield: ("", usage) с суммарным usage всех вызовов.
    """
    total_input = 0
    total_output = 0
    total_cache_write = 0
    total_cache_read = 0
    current_messages = list(messages)

    active_tools: list[dict] = (
        [WEB_SEARCH_TOOL, REMINDER_TOOL]
        if _searxng_available
        else [NATIVE_WEB_SEARCH_TOOL, REMINDER_TOOL]  # type: ignore[list-item]
    )

    for _iteration in range(5):  # максимум 5 итераций
        msgs = current_messages

        async def _open_stream(m=msgs):
            return client.messages.stream(
                model=model,
                max_tokens=8192,
                system=system,
                messages=m,
                tools=active_tools,  # type: ignore[arg-type]
            )

        stream_cm = await with_anthropic_retry(_open_stream, config)
        async with stream_cm as stream:
            async for text_chunk in stream.text_stream:
                yield text_chunk, None
            final_msg = await stream.get_final_message()

        if final_msg and final_msg.usage:
            total_input += final_msg.usage.input_tokens
            total_output += final_msg.usage.output_tokens
            total_cache_write += getattr(final_msg.usage, "cache_creation_input_tokens", 0) or 0
            total_cache_read += getattr(final_msg.usage, "cache_read_input_tokens", 0) or 0

        if not final_msg or final_msg.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in final_msg.content if b.type == "tool_use"]
        if not tool_blocks:
            break

        # Разделяем блоки по типу инструмента
        search_blocks = [b for b in tool_blocks if b.name == "web_search"]
        reminder_blocks = [b for b in tool_blocks if b.name == "create_reminder"]

        tool_results: list[dict] = []

        # Поисковые запросы выполняем параллельно
        if search_blocks:
            queries = [b.input.get("query", "") for b in search_blocks]  # type: ignore[union-attr]
            logger.info("SearXNG поиск (%d запросов): %s", len(queries), queries)
            search_results = await asyncio.gather(
                *[_searxng_search(q, config.searxng_url) for q in queries]
            )
            tool_results.extend(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
                for block, result in zip(search_blocks, search_results)
            )

        # Напоминания создаём последовательно (запись в БД)
        for block in reminder_blocks:
            logger.info("Создание напоминания через tool: %s", block.input)
            result = await _execute_reminder_tool(
                block.input,  # type: ignore[arg-type]
                chat_id,
                user_id,
                config.reminder_default_silent,
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

        current_messages = current_messages + [
            {"role": "assistant", "content": final_msg.content},
            {"role": "user", "content": tool_results},
        ]

    yield "", anthropic.types.Usage(
        input_tokens=total_input,
        output_tokens=total_output,
        cache_creation_input_tokens=total_cache_write,
        cache_read_input_tokens=total_cache_read,
    )


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
    user_id: int,
    new_content: list[dict] | str,
    user_tz: str,
    db_history: dict,
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
    return stream_response(client, config, model, messages, system, chat_id=chat_id, user_id=user_id)
