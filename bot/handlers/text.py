from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.keyboards import setup_commands, get_main_keyboard
from bot.utils import db
from bot.utils.anthropic.models import context_limit, MODEL_LABELS
from bot.handlers._common import handle_incoming

if TYPE_CHECKING:
    import anthropic
    from bot.config import Config

logger = logging.getLogger(__name__)
router = Router(name="text")


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, config: "Config", is_new_user: bool, **kwargs) -> None:
    await setup_commands(bot, config.admin_id)
    is_admin = message.from_user.id == config.admin_id  # type: ignore[union-attr]
    kb = get_main_keyboard(is_admin)
    if is_new_user:
        await db.mark_welcomed(message.from_user.id)  # type: ignore[union-attr]
        name = message.from_user.first_name or "друг"  # type: ignore[union-attr]
        await message.answer(
            f"👋 Привет, <b>{name}</b>!\n\n"
            "Я семейный бот на базе Claude AI. Просто напиши мне что-нибудь — "
            "отвечу на вопросы, помогу с задачами, запомню наш разговор.\n\n"
            "Для справки: /help",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await message.answer(
            "👋 С возвращением! Чем могу помочь?\n\n"
            "Текущий контекст сохранён. Для сброса: /reset",
            reply_markup=kb,
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

    await handle_incoming(
        message=message,
        config=config,
        client=client,
        content=message.text,
        notify_admin=kwargs.get("notify_admin"),
    )
