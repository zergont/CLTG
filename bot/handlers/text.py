from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.utils import db
from bot.utils.anthropic.models import context_limit, MODEL_LABELS
from bot.utils.reminders import parse_reminder
from bot.handlers._common import handle_incoming

if TYPE_CHECKING:
    import anthropic
    from bot.config import Config

logger = logging.getLogger(__name__)
router = Router(name="text")


@router.message(CommandStart())
async def cmd_start(message: Message, config: "Config", is_new_user: bool, **kwargs) -> None:
    if is_new_user:
        await db.mark_welcomed(message.from_user.id)  # type: ignore[union-attr]
        name = message.from_user.first_name or "друг"  # type: ignore[union-attr]
        await message.answer(
            f"👋 Привет, <b>{name}</b>!\n\n"
            "Я семейный бот на базе Claude AI. Просто напиши мне что-нибудь — "
            "отвечу на вопросы, помогу с задачами, запомню наш разговор.\n\n"
            "Для справки: /help",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "👋 С возвращением! Чем могу помочь?\n\n"
            "Текущий контекст сохранён. Для сброса: /reset"
        )


@router.message(Command("help"))
async def cmd_help(message: Message, config: "Config", **kwargs) -> None:
    is_admin = message.from_user.id == config.admin_id  # type: ignore[union-attr]
    text = (
        "📋 <b>Доступные команды:</b>\n\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/reset — сбросить контекст диалога\n"
        "/stats — статистика токенов и расходов\n"
        "/reminders — список активных напоминаний\n"
        "/delreminder &lt;id&gt; — удалить напоминание\n"
    )
    if is_admin:
        text += (
            "\n<b>Команды администратора:</b>\n"
            "/model — сменить модель Claude\n"
            "/ban &lt;user_id&gt; — заблокировать пользователя\n"
            "/unban &lt;user_id&gt; — разблокировать\n"
            "/users — список всех пользователей\n"
            "/usage — общая статистика расходов\n"
            "/context — заполнение контекстных окон\n"
        )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("reset"))
async def cmd_reset(message: Message, **kwargs) -> None:
    await db.reset_history(message.chat.id)
    await message.answer(
        "🔄 Контекст диалога сброшен. История и саммари очищены.\n"
        "Начинаем с чистого листа!"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, **kwargs) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    stats = await db.get_user_stats(user_id)

    total_input = stats.get("total_input") or 0
    total_output = stats.get("total_output") or 0
    total_cost = stats.get("total_cost") or 0.0
    total_req = stats.get("total_requests") or 0

    await message.answer(
        f"📊 <b>Ваша статистика:</b>\n\n"
        f"Запросов: <b>{total_req}</b>\n"
        f"Токенов входящих: <b>{total_input:,}</b>\n"
        f"Токенов исходящих: <b>{total_output:,}</b>\n"
        f"Итого токенов: <b>{total_input + total_output:,}</b>\n"
        f"Стоимость: <b>${total_cost:.4f}</b>",
        parse_mode="HTML",
    )


@router.message(Command("reminders"))
async def cmd_reminders(message: Message, **kwargs) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    rows = await db.get_user_reminders(user_id)

    if not rows:
        await message.answer("📭 У вас нет активных напоминаний.")
        return

    lines = ["🔔 <b>Активные напоминания:</b>\n"]
    for r in rows:
        due = r["due_at"]
        chain_mark = " 🔁" if r["is_chain"] else ""
        steps = f" (осталось: {r['steps_left']})" if r["steps_left"] else ""
        lines.append(f"<b>#{r['id']}</b>{chain_mark} — {r['text']}\n   ⏰ {due}{steps}")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("delreminder"))
async def cmd_delreminder(message: Message, **kwargs) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("❌ Укажите ID напоминания: /delreminder &lt;id&gt;", parse_mode="HTML")
        return

    reminder_id = int(parts[1])
    deleted = await db.delete_reminder(reminder_id, user_id)
    if deleted:
        await message.answer(f"✅ Напоминание #{reminder_id} удалено.")
    else:
        await message.answer(f"❌ Напоминание #{reminder_id} не найдено.")


@router.message()
async def handle_text(
    message: Message,
    config: "Config",
    client: "anthropic.AsyncAnthropic",
    **kwargs,
) -> None:
    """Обрабатывает все текстовые сообщения."""
    if not message.text:
        return

    text = message.text

    # Проверяем — вдруг это запрос на напоминание
    REMINDER_KEYWORDS = ("напомни", "напомнить", "reminder", "каждый", "каждую", "каждое", "ежедневно", "еженедельно")
    if any(kw in text.lower() for kw in REMINDER_KEYWORDS):
        await _try_create_reminder(message, config, client, text)
        return

    await handle_incoming(
        message=message,
        config=config,
        client=client,
        content=text,
        notify_admin=kwargs.get("notify_admin"),
    )


async def _try_create_reminder(
    message: Message,
    config: "Config",
    client: "anthropic.AsyncAnthropic",
    text: str,
) -> None:
    """Пытается создать напоминание из текста. При неудаче — обрабатывает как обычный текст."""
    user_row = await db.get_user(message.from_user.id)  # type: ignore[union-attr]
    user_tz = user_row["timezone"] if user_row else config.default_timezone
    model = await db.get_setting("current_model") or config.model_haiku

    parsed = await parse_reminder(client, config, model, text, user_tz)

    if not parsed or not parsed.get("due_at"):
        # Не похоже на напоминание — обрабатываем как обычный текст
        await handle_incoming(message=message, config=config, client=client, content=text)
        return

    try:
        due_at = datetime.fromisoformat(parsed["due_at"])
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)

        end_at = None
        if parsed.get("end_at"):
            end_at = datetime.fromisoformat(parsed["end_at"])
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)

        # Сохраняем интервал в meta_json
        import json
        meta = {}
        if parsed.get("interval_seconds"):
            meta["interval_seconds"] = parsed["interval_seconds"]

        silent = int(parsed.get("silent", 1 if config.reminder_default_silent else 0))

        reminder_id = await db.add_reminder(
            chat_id=message.chat.id,
            user_id=message.from_user.id,  # type: ignore[union-attr]
            text=parsed.get("text", text),
            due_at=due_at,
            prompt=parsed.get("prompt"),
            is_chain=bool(parsed.get("is_chain", False)),
            silent=silent,
            steps_left=parsed.get("steps_left"),
            end_at=end_at,
        )

        # Обновляем meta_json отдельно если нужно
        if meta:
            import aiosqlite
            from bot.utils.db import DB_PATH
            async with aiosqlite.connect(DB_PATH) as db_conn:
                await db_conn.execute(
                    "UPDATE reminders SET meta_json = ? WHERE id = ?",
                    (json.dumps(meta), reminder_id),
                )
                await db_conn.commit()

        chain_mark = " 🔁" if parsed.get("is_chain") else ""
        steps_info = f"\nПовторений: {parsed['steps_left']}" if parsed.get("steps_left") else ""
        silent_mark = " 🔕" if silent else ""

        await message.answer(
            f"✅ Напоминание создано{chain_mark}{silent_mark}\n\n"
            f"📝 {parsed.get('text', text)}\n"
            f"⏰ {due_at.strftime('%d.%m.%Y %H:%M UTC')}"
            f"{steps_info}\n\n"
            f"ID: #{reminder_id} (для удаления: /delreminder {reminder_id})"
        )
    except Exception:
        logger.exception("Ошибка создания напоминания")
        await handle_incoming(message=message, config=config, client=client, content=text)
