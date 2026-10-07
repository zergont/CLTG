from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any, AsyncGenerator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
import anthropic
import httpx2

from bot.config import Config
from bot.utils.anthropic.models import MODELS, SERVICE_EFFORT, ModelInfo, calc_cost

logger = logging.getLogger(__name__)

# Режим поиска (auto | searxng | native) и доступность SearXNG — задаются init_searxng()
_search_engine: str = "searxng"
_searxng_url: str = ""
_searxng_available: bool = False
_searxng_checked_at: float = 0.0
# В режиме auto недоступный SearXNG перепроверяется не чаще раза в N секунд
SEARXNG_RECHECK_SECONDS = 300

# Лимит на один ответ (включая размышления); стриминг снимает риск HTTP-таймаута
MAX_TOKENS = 64_000
# Лимит для изолированных (не стриминговых) вызовов
ISOLATED_MAX_TOKENS = 16_000
# Максимум итераций agentic loop (вызовы инструментов)
MAX_TOOL_ITERATIONS = 5

# Серверный fallback при отказе модели: запрос повторяется на рекомендованной модели
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Заголовок служебного блока, который бот добавляет к каждому сообщению пользователя
TURN_CONTEXT_HEADER = "[Служебная информация от бота, не от пользователя]"

REFUSAL_TEXT ="⚠️ Модель отказалась отвечать на этот запрос. Попробуйте переформулировать."
MAX_TOKENS_TEXT = "✂️ Ответ обрезан: достигнут лимит длины."

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

REMINDER_TOOL: dict = {
    "name": "create_reminder",
    "description": (
        "Создаёт напоминание для пользователя. Используй всегда когда пользователь "
        "просит что-то напомнить, поставить будильник или создать повторяющееся уведомление. "
        "Время due_at рассчитывай в UTC относительно текущего времени из служебного блока в сообщении пользователя."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Текст напоминания"},
            "due_at": {"type": "string", "description": "Время срабатывания ISO 8601 UTC. Конвертируй из часового пояса пользователя используя смещение из служебного блока «Текущее время». Пример: если сейчас 14:30 UTC+03:00 и нужно через 30 мин → 2026-03-12T12:00:00Z"},
            "is_chain": {"type": "boolean", "description": "true если повторяющееся"},
            "interval_seconds": {"type": "integer", "description": "Интервал повтора в секундах"},
            "steps_left": {"type": "integer", "description": "Макс. число срабатываний (без поля = бессрочно)"},
            "end_at": {"type": "string", "description": "Дата окончания ISO 8601 UTC (без поля = бессрочно)"},
            "silent": {"type": "integer", "description": "1 = тихий режим, 0 = со звуком"},
        },
        "required": ["text", "due_at"],
    },
}


def _native_web_search_tool(model: ModelInfo) -> dict:
    """Нативный web_search Anthropic (платный: $0.01 за запрос + токены результатов)."""
    return {"type": model.web_search_tool, "name": "web_search"}


async def _search_tool(model: ModelInfo) -> dict:
    """Инструмент поиска для запроса с учётом режима SEARCH_ENGINE.

    searxng — всегда бесплатный SearXNG (если он лежит, модель получит «поиск недоступен»);
    native  — всегда платный нативный поиск;
    auto    — SearXNG, а платный только пока SearXNG недоступен (с периодической перепроверкой).
    """
    if _search_engine == "native":
        return _native_web_search_tool(model)
    if _search_engine == "auto" and not _searxng_available:
        if time.monotonic() - _searxng_checked_at >= SEARXNG_RECHECK_SECONDS:
            await _check_searxng()
        if not _searxng_available:
            return _native_web_search_tool(model)
    return WEB_SEARCH_TOOL


@dataclass
class TurnUsage:
    """Суммарный usage всех вызовов API за один ход диалога."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    # Размер контекста последнего вызова (весь промпт + ответ) — для триггера саммаризации
    context_tokens: int = 0

    def add(self, model: ModelInfo, message: Any) -> None:
        usage = message.usage
        cache_write = usage.cache_creation_input_tokens or 0
        cache_read = usage.cache_read_input_tokens or 0
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_write_tokens += cache_write
        self.cache_read_tokens += cache_read
        # После серверного fallback ответ мог дать другая модель — считаем по её ценам, если знаем их
        self.cost += calc_cost(MODELS.get(getattr(message, "model", None), model), usage)
        self.context_tokens = usage.input_tokens + cache_write + cache_read + usage.output_tokens


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


async def _check_searxng() -> bool:
    """Проверяет доступность SearXNG и запоминает результат."""
    global _searxng_available, _searxng_checked_at
    was_available = _searxng_available
    _searxng_checked_at = time.monotonic()
    try:
        async with httpx2.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{_searxng_url}/search",
                params={"q": "test", "format": "json"},
            )
            r.raise_for_status()
        _searxng_available = True
        if not was_available:
            logger.info("✅ Поисковый движок: SearXNG (%s)", _searxng_url)
    except Exception as exc:
        _searxng_available = False
        if _search_engine == "auto":
            logger.warning(
                "⚠️  SearXNG недоступен (%s). Временно платный нативный web_search.", exc
            )
        else:
            logger.warning(
                "⚠️  SearXNG недоступен (%s). Поиск не работает, пока SearXNG не поднимется.", exc
            )
    return _searxng_available


async def init_searxng(url: str, engine: str = "searxng") -> bool:
    """Задаёт режим поиска и проверяет SearXNG. Вызывается при старте бота.

    engine: searxng (только бесплатный SearXNG) | auto (SearXNG, при сбое — платный) | native
    """
    global _search_engine, _searxng_url
    _search_engine = engine if engine in ("auto", "searxng", "native") else "searxng"
    _searxng_url = url

    if _search_engine == "native":
        logger.info("🔍 Поисковый движок: нативный Anthropic web_search (платный)")
        return False
    return await _check_searxng()


async def _searxng_search(query: str, searxng_url: str, max_results: int = 5) -> str:
    """HTTP-запрос к SearXNG, возвращает текст с результатами."""
    try:
        async with httpx2.AsyncClient(timeout=10.0) as client:
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


def _build_system_prompt(config: Config) -> list[dict]:
    """Статический system-блок с кэшированием.

    Текущее время сюда не входит: system стоит в начале промпта, и любое его
    изменение сбрасывает кэш всей истории. Время передаётся отдельным
    служебным блоком в конце нового сообщения (см. _turn_context).
    """
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
        "в UTC, используя смещение из служебного блока «Текущее время».\n\n"
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
        "- **Имя** — имя собеседника из профиля Telegram приходит в служебном блоке "
        "(строка «Собеседник»). Обращайся по имени тепло и естественно, но не в каждой реплике. "
        "Если человек представился иначе или попросил называть его по-другому — используй это имя\n"
        "- **Часовой пояс** — определяется автоматически и указан в «Текущем времени»\n"
        "- **Ключевые факты** — профессия, локация, семья, здоровье, предпочтения, проекты; "
        "учитывай их в советах (например, аллергии — в рецептах)\n"
        "- **Незавершённые задачи** — если в саммари есть открытые вопросы, можешь напомнить о них, "
        "когда это к месту, но не в каждом сообщении\n"
        "- **Стиль общения** — подстраивайся под формальность/неформальность пользователя\n\n"
        "Команда /reset очищает текущий диалог, но факты о пользователе сохраняются; "
        "/kill стирает всю память о нём.\n\n"
        "### 🕐 Текущее время\n"
        "В конце каждого сообщения пользователя бот добавляет служебный блок "
        "«[Служебная информация от бота, не от пользователя]» с именем собеседника и текущим "
        "временем (часовой пояс и UTC-смещение пользователя). Его пишет бот, а не человек: "
        "не цитируй и не упоминай его. Время используй для:\n"
        "- Расчёта `due_at` в напоминаниях (конвертация в UTC)\n"
        "- Понимания относительных выражений («сегодня», «завтра», «через час», «в эту пятницу»)\n"
        "- Ответов на прямые вопросы о времени и дате\n"
        "- Определения уместности приветствия (утро/день/вечер)\n\n"
        "Не комментируй время суток, не напоминай, что уже поздно, и не советуй лечь спать, "
        "если пользователь сам об этом не заговорил.\n"
    )
    return [
        {
            "type": "text",
            "text": config.system_prompt + capabilities,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _user_zone(config: Config, user_tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(user_tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(config.default_timezone)


def display_name(
    first_name: str | None,
    last_name: str | None = None,
    username: str | None = None,
) -> str | None:
    """Имя собеседника из профиля Telegram: «Имя Фамилия (@username)»."""
    name = " ".join(p.strip() for p in (first_name, last_name) if p and p.strip())
    if username:
        name = f"{name} (@{username})" if name else f"@{username}"
    return name[:100] or None


def _turn_context(config: Config, user_tz: str, user_name: str | None = None) -> str:
    """Служебный блок с именем собеседника и его текущим временем.

    Добавляется в конец нового сообщения пользователя: время меняется каждую минуту,
    а в группе от сообщения к сообщению меняется и собеседник — в кэшируемый префикс
    это не кладём. (Отдельное role="system" сообщение API принимает, но в проверке
    на Haiku 5.5 модель его содержимое не использовала — поэтому текстовый блок.)
    """
    now_dt = datetime.now(_user_zone(config, user_tz))
    raw_offset = now_dt.strftime("%z")  # e.g. +0300 or -0530
    if raw_offset:
        utc_offset = f"UTC{raw_offset[:3]}:{raw_offset[3:]}"  # UTC+03:00
    else:
        utc_offset = "UTC"
    now = now_dt.strftime(f"%d.%m.%Y %H:%M %Z ({utc_offset})")

    lines = [TURN_CONTEXT_HEADER]
    if user_name:
        lines.append(f"Собеседник: {user_name}")
    lines.append(f"Текущее время: {now}")
    return "\n".join(lines)


def _strip_images_from_history(messages: list[dict]) -> list[dict]:
    """Заменяет base64-изображения в истории на текстовый плейсхолдер.

    Модель уже видела эти изображения и ответила на них — хранить base64
    в контексте бессмысленно и вызывает ошибку при накоплении many-image.
    """
    result = []
    for msg in messages:
        content = msg["content"]
        if not isinstance(content, list):
            result.append(msg)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image", "document"):
                new_content.append({"type": "text", "text": "[изображение из предыдущего сообщения]"})
            else:
                new_content.append(block)
        result.append({"role": msg["role"], "content": new_content})
    return result


def _build_messages(
    live_history: list[dict],
    summary: str | None,
    new_content: list[dict] | str,
    turn_context: str,
) -> list[dict]:
    """Формирует итоговый массив messages[] для API с маркерами кэширования.

    Структура (с кэшем):
      [user: SUMMARY + cache_control]     — если есть
      [assistant: Понял]                  — если есть
      [... live_history[:-1] ...]
      [последний элемент истории + cache_control на последнем блоке]
      [user: new_content + собеседник/время] — БЕЗ cache_control: время меняется каждую минуту
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

    # --- живая история (изображения заменяем плейсхолдером) ---
    if live_history:
        clean_history = _strip_images_from_history(live_history)

        # Все сообщения кроме последнего — как есть
        messages.extend(clean_history[:-1])

        # Последнее сообщение истории получает cache_control
        last = clean_history[-1]
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

    # --- новое сообщение + собеседник и текущее время — без кэша ---
    if isinstance(new_content, str):
        user_blocks: list[dict] = [{"type": "text", "text": new_content}]
    else:
        user_blocks = list(new_content)
    user_blocks.append({"type": "text", "text": turn_context})
    messages.append({"role": "user", "content": user_blocks})
    return messages


def _request_params(
    model: ModelInfo,
    effort: str,
    max_tokens: int,
    **params: Any,
) -> dict:
    """Общие параметры запроса: адаптивные размышления + уровень effort."""
    return {
        "model": model.id,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        **params,
    }


def _open_stream(client: anthropic.AsyncAnthropic, model: ModelInfo, params: dict):
    """Открывает стрим; для Sonnet/Opus включает серверный fallback при отказе."""
    if model.server_fallback:
        return client.beta.messages.stream(
            **params,
            betas=[SERVER_FALLBACK_BETA],
            fallbacks="default",
        )
    return client.messages.stream(**params)


def _echo_content(content: list[Any]) -> list[Any]:
    """Готовит content ответа к возврату в следующий запрос agentic loop.

    Если посреди ответа сработал fallback, блоки до последней точки переключения
    (размышления и незавершённые вызовы инструментов отказавшей модели) возвращать
    нельзя — оставляем из них только текст и завершённые серверные поиски.
    """
    boundary = max(
        (i for i, block in enumerate(content) if block.type == "fallback"),
        default=-1,
    )
    if boundary < 0:
        return content

    head = content[:boundary]
    result_ids = {
        block.tool_use_id for block in head if getattr(block, "tool_use_id", None)
    }
    kept = [
        block for block in head
        if block.type == "text"
        or (block.type == "server_tool_use" and block.id in result_ids)
        or block.type.endswith("_tool_result")
    ]
    return kept + content[boundary + 1:]


async def stream_response(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: ModelInfo,
    effort: str,
    messages: list[dict],
    system: list[dict],
    chat_id: int = 0,
    user_id: int = 0,
) -> AsyncGenerator[tuple[str, TurnUsage | None], None]:
    """Async generator: стриминг ответа Claude.

    Поддерживает agentic loop: обрабатывает tool_use блоки (web_search, create_reminder).
    Если SearXNG недоступен — используется нативный Anthropic web_search (сервер-сайд).

    Yields (chunk_text, None) для каждого текстового чанка.
    Финальный yield: ("", usage) с суммарным usage всех вызовов.
    """
    usage = TurnUsage()
    current_messages = list(messages)
    has_text = False

    active_tools: list[dict] = [await _search_tool(model), REMINDER_TOOL]

    for _iteration in range(MAX_TOOL_ITERATIONS):
        params = _request_params(
            model, effort, MAX_TOKENS,
            system=system, messages=current_messages, tools=active_tools,
        )
        # Текст до и после вызова инструмента разделяем пустой строкой
        separator_pending = has_text

        async with _open_stream(client, model, params) as stream:
            async for text_chunk in stream.text_stream:
                if separator_pending:
                    yield "\n\n", None
                    separator_pending = False
                has_text = True
                yield text_chunk, None
            final_msg = await stream.get_final_message()

        usage.add(model, final_msg)
        stop_reason = final_msg.stop_reason

        if stop_reason == "refusal":
            details = getattr(final_msg, "stop_details", None)
            logger.warning(
                "Отказ модели %s для chat_id=%d (категория: %s)",
                final_msg.model, chat_id, getattr(details, "category", None),
            )
            yield ("\n\n" if has_text else "") + REFUSAL_TEXT, None
            break

        if stop_reason == "max_tokens":
            yield ("\n\n" if has_text else "") + MAX_TOKENS_TEXT, None
            break

        if stop_reason == "pause_turn":
            # Серверный инструмент (нативный web_search) взял паузу — продолжаем тот же ход
            current_messages = current_messages + [
                {"role": "assistant", "content": _echo_content(final_msg.content)},
            ]
            continue

        if stop_reason != "tool_use":
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
            {"role": "assistant", "content": _echo_content(final_msg.content)},
            {"role": "user", "content": tool_results},
        ]

    yield "", usage


async def call_claude_isolated(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: ModelInfo,
    prompt: str,
    effort: str = SERVICE_EFFORT,
    system: str | None = None,
) -> tuple[str, TurnUsage]:
    """Изолированный вызов Claude без истории. Возвращает (text, usage)."""
    response = await client.messages.create(
        **_request_params(
            model, effort, ISOLATED_MAX_TOKENS,
            system=system or config.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
    )
    usage = TurnUsage()
    usage.add(model, response)

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Модель {response.model} отказалась отвечать (refusal)")

    # Ответ может начинаться с блоков размышлений — берём только текст
    text = "".join(b.text for b in response.content if b.type == "text")
    return text, usage


# Ответ модели, если долговременных фактов о пользователе нет
NO_FACTS = "НЕТ"

MEMORY_SYSTEM_PROMPT = (
    "Ты ведёшь долговременную память семейного ассистента о пользователе. "
    "Пиши кратко и по делу, без вводных фраз."
)

# Общие правила обновления памяти: предыдущее саммари — более ранняя часть общения,
# его не перепроверяют по новому диалогу, а факты из него переносят
_MEMORY_RULES = (
    "Правила:\n"
    "- Предыдущее саммари описывает более раннюю часть общения. Новый диалог не обязан его "
    "подтверждать: не ставь его под сомнение и не отмечай «расхождения».\n"
    "- Все факты о пользователе из предыдущего саммари перенеси, если новый диалог им прямо "
    "не противоречит. Если противоречит — оставь новую версию.\n"
    "- Записывай только то, что пригодится в будущих разговорах. Не записывай мелочи, "
    "дежурные фразы, время суток, служебные детали и сам факт подведения итогов.\n"
)

SUMMARY_PROMPT_TEMPLATE = (
    "Обнови память: объедини предыдущее саммари с новой частью диалога.\n\n"
    + _MEMORY_RULES
    + "{session_note}\n"
    "Структура (пустые разделы пропускай):\n"
    "1. Факты о пользователе (имя, город, работа, семья, питомцы, здоровье и ограничения, предпочтения)\n"
    "2. Договорённости и принятые решения\n"
    "3. Открытые задачи и вопросы, к которым стоит вернуться\n"
    "4. Важный контекст, в т.ч. технические детали, которые понадобятся позже\n\n"
    "Предыдущее саммари:\n{prev_summary}\n\n"
    "Новая часть диалога:\n{dialogue}"
)

# Добавляется при саммаризации после долгой паузы
SUMMARY_TIMEOUT_NOTE = (
    "- Разговор прервался надолго (больше 72 часов): в разделе 4 кратко подведи итоги сессии.\n"
)

# Для /reset: оставить только долговременные факты, без хода разговора
FACTS_PROMPT_TEMPLATE = (
    "Пользователь сбрасывает текущий разговор, но долговременная память о нём должна сохраниться.\n"
    "Из предыдущего саммари и диалога выпиши только устойчивые факты о пользователе "
    "и договорённости на будущее: имя, город, работа, семья, питомцы, здоровье и ограничения, "
    "предпочтения, планы, о которых он просил помнить.\n\n"
    + _MEMORY_RULES
    + "- Не включай ход разговора и обсуждавшиеся темы.\n\n"
    "Формат: краткий маркированный список под заголовком «Факты о пользователе». "
    f"Если фактов нет — ответь ровно: {NO_FACTS}\n\n"
    "Предыдущее саммари:\n{prev_summary}\n\n"
    "Диалог:\n{dialogue}"
)


def _format_dialogue(messages: list[dict]) -> str:
    """Диалог в виде текста для саммаризации (изображения и PDF пропускаются)."""
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
    model: ModelInfo,
    messages_to_summarize: list[dict],
    prev_summary: str | None,
    timeout_trigger: bool = False,
) -> tuple[str, TurnUsage]:
    """Создаёт новое саммари, объединяя со старым."""
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        session_note=SUMMARY_TIMEOUT_NOTE if timeout_trigger else "",
        prev_summary=prev_summary or "отсутствует",
        dialogue=_format_dialogue(messages_to_summarize) or "(пусто)",
    )
    return await call_claude_isolated(client, config, model, prompt, system=MEMORY_SYSTEM_PROMPT)


async def extract_facts(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: ModelInfo,
    messages: list[dict],
    prev_summary: str | None,
) -> tuple[str | None, TurnUsage]:
    """Оставляет из памяти только долговременные факты (для /reset). None — фактов нет."""
    prompt = FACTS_PROMPT_TEMPLATE.format(
        prev_summary=prev_summary or "отсутствует",
        dialogue=_format_dialogue(messages) or "(пусто)",
    )
    text, usage = await call_claude_isolated(client, config, model, prompt, system=MEMORY_SYSTEM_PROMPT)
    facts = text.strip()
    if not facts or facts.strip(" .").upper() == NO_FACTS:
        return None, usage
    return facts, usage


def process_message(
    client: anthropic.AsyncAnthropic,
    config: Config,
    model: ModelInfo,
    effort: str,
    chat_id: int,
    user_id: int,
    new_content: list[dict] | str,
    user_tz: str,
    db_history: dict,
    user_name: str | None = None,
) -> AsyncGenerator[tuple[str, TurnUsage | None], None]:
    """Точка входа для обработки входящего сообщения.

    Обычная (не async) функция - возвращает async generator напрямую.
    Использование: async for chunk, usage in process_message(...): ...
    """
    raw = db_history.get("messages_json") or "[]"
    live_history: list[dict] = json.loads(raw)
    summary: str | None = db_history.get("summary")
    system = _build_system_prompt(config)
    turn_context = _turn_context(config, user_tz, user_name)
    messages = _build_messages(live_history, summary, new_content, turn_context)
    return stream_response(
        client, config, model, effort, messages, system, chat_id=chat_id, user_id=user_id,
    )
