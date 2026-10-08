from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards import setup_commands, get_main_keyboard
from bot.utils import db
from bot.utils.errors import close_menu
from bot.handlers._common import TRANSIENT_API_ERRORS, handle_incoming, reset_dialogue

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
            "Текущий контекст сохранён. Начать новый разговор: /reset",
            reply_markup=kb,
        )


@router.message(Command("help"))
async def cmd_help(message: Message, config: "Config", **kwargs) -> None:
    is_admin = message.from_user.id == config.admin_id  # type: ignore[union-attr]
    text = (
        "📋 <b>Доступные команды:</b>\n\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/reset — начать новый разговор (факты о вас сохранятся)\n"
        "/kill — забыть всё, включая факты о вас\n"
        "/stats — статистика токенов и расходов\n"
        "/reminders — список активных напоминаний (с кнопками удаления)\n"
    )
    if is_admin:
        text += (
            "\n<b>Команды администратора:</b>\n"
            "/model — модель Claude и уровень размышлений\n"
            "/ban &lt;user_id&gt; — заблокировать пользователя\n"
            "/unban &lt;user_id&gt; — разблокировать\n"
            "/users — список всех пользователей\n"
            "/usage — общая статистика расходов\n"
            "/context — заполнение контекстных окон\n"
        )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("reset"))
async def cmd_reset(
    message: Message,
    config: "Config",
    client: "anthropic.AsyncAnthropic",
    **kwargs,
) -> None:
    """Новый разговор: история очищается, долговременные факты о пользователе остаются."""
    status = await message.answer("⏳ Начинаю новый разговор, сохраняю факты о вас…")
    try:
        kept = await reset_dialogue(
            client, config, message.chat.id, message.from_user.id,  # type: ignore[union-attr]
        )
    except Exception as exc:
        if isinstance(exc, TRANSIENT_API_ERRORS):
            logger.warning("Временная ошибка Claude API при /reset chat_id=%d: %r", message.chat.id, exc)
        else:
            logger.exception("Ошибка /reset для chat_id=%d", message.chat.id)
        await status.edit_text(
            "❌ Не удалось сохранить факты, поэтому диалог не сброшен. Попробуйте позже.\n"
            "Забыть всё без сохранения: /kill"
        )
        return

    if kept:
        await status.edit_text(
            "🔄 Начинаем новый разговор. Факты о вас я помню.\n"
            "<i>Забыть всё: /kill</i>",
            parse_mode="HTML",
        )
    else:
        await status.edit_text("🔄 Начинаем с чистого листа!")


_KILL_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🗑 Да, забыть всё", callback_data="kill:yes"),
    InlineKeyboardButton(text="✖ Отмена", callback_data="kill:no"),
]])


@router.message(Command("kill"))
async def cmd_kill(message: Message, **kwargs) -> None:
    """Полное забывание — с подтверждением, чтобы не стереть память случайно."""
    await message.answer(
        "⚠️ <b>Забыть всё?</b>\n\n"
        "Я удалю историю разговоров и всё, что помню о вас: имя, факты, предпочтения. "
        "Отменить это будет нельзя.\n"
        "<i>Напоминания останутся — ими управляет /reminders.</i>",
        parse_mode="HTML",
        reply_markup=_KILL_KEYBOARD,
    )


@router.callback_query(F.data.startswith("kill:"))
async def cb_kill(callback: CallbackQuery, **kwargs) -> None:
    if callback.data == "kill:yes":
        await db.reset_history(callback.message.chat.id)  # type: ignore[union-attr]
        logger.info("Память очищена (/kill) для chat_id=%d", callback.message.chat.id)  # type: ignore[union-attr]
        await callback.message.edit_text(  # type: ignore[union-attr]
            "🧹 Готово: вся память очищена. Начнём знакомство заново!"
        )
    else:
        await callback.message.edit_text("👌 Отменено, я всё помню.")  # type: ignore[union-attr]
    await callback.answer()


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


def _format_due(due_str: str, user_tz: str) -> str:
    try:
        dt = datetime.fromisoformat(due_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(user_tz)).strftime("%d.%m.%Y %H:%M %Z")
    except Exception:
        return due_str


def _reminders_text(rows, user_tz: str) -> str:
    lines = ["🔔 <b>Активные напоминания:</b>\n"]
    for r in rows:
        chain_mark = " 🔁" if r["is_chain"] else ""
        steps = f" (осталось: {r['steps_left']})" if r["steps_left"] else ""
        lines.append(f"<b>#{r['id']}</b>{chain_mark} — {r['text']}\n   ⏰ {_format_due(r['due_at'], user_tz)}{steps}")
    lines.append("\n<i>Нажмите на напоминание чтобы удалить его.</i>")
    return "\n".join(lines)


def _reminders_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        chain_mark = " 🔁" if r["is_chain"] else ""
        label = r["text"]
        if len(label) > 32:
            label = label[:31] + "…"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 #{r['id']}{chain_mark} — {label}",
            callback_data=f"rem:del:{r['id']}",
        )])
    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить все", callback_data="rem:delall"),
        InlineKeyboardButton(text="✖ Закрыть", callback_data="rem:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_reminders_menu(message: Message, user_id: int, config: "Config") -> None:
    rows = await db.get_user_reminders(user_id)
    if not rows:
        await message.answer("📭 У вас нет активных напоминаний.")
        return
    user_row = await db.get_user(user_id)
    user_tz = user_row["timezone"] if user_row else config.default_timezone
    await message.answer(
        _reminders_text(rows, user_tz),
        parse_mode="HTML",
        reply_markup=_reminders_keyboard(rows),
    )


@router.message(Command("reminders"))
async def cmd_reminders(message: Message, config: "Config", **kwargs) -> None:
    await _show_reminders_menu(message, message.from_user.id, config)  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("rem:"))
async def cb_rem(callback: CallbackQuery, config: "Config", **kwargs) -> None:
    user_id = callback.from_user.id
    action = callback.data.split(":", 1)[1]  # type: ignore[union-attr]

    if action == "cancel":
        await close_menu(callback.message)  # type: ignore[arg-type]
        await callback.answer()
        return

    if action == "delall":
        count = await db.delete_all_reminders(user_id)
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"🗑 Удалено напоминаний: <b>{count}</b>.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if action.startswith("del:"):
        reminder_id = int(action.split(":", 1)[1])
        deleted = await db.delete_reminder(reminder_id, user_id)
        if not deleted:
            await callback.answer("Напоминание не найдено.", show_alert=True)
            return
        rows = await db.get_user_reminders(user_id)
        if rows:
            user_row = await db.get_user(user_id)
            user_tz = user_row["timezone"] if user_row else config.default_timezone
            await callback.message.edit_text(  # type: ignore[union-attr]
                _reminders_text(rows, user_tz),
                parse_mode="HTML",
                reply_markup=_reminders_keyboard(rows),
            )
        else:
            await callback.message.edit_text("📭 Все напоминания удалены.")  # type: ignore[union-attr]
        await callback.answer(f"✅ Напоминание #{reminder_id} удалено.")
        return

    await callback.answer()


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
    )
