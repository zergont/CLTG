from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.handlers._common import context_budget
from bot.middlewares import describe_user
from bot.utils import db
from bot.utils.anthropic.models import EFFORT_LABELS, MODELS, ModelInfo
from bot.utils.errors import close_menu, handle_telegram_error
from bot.utils.html import split_long_message

if TYPE_CHECKING:
    from bot.config import Config

logger = logging.getLogger(__name__)
router = Router(name="admin")


def _admin_filter(message: Message, config: "Config") -> bool:
    return message.from_user is not None and message.from_user.id == config.admin_id


# ──────────────────────────────────────────
# /model — рабочая модель и уровень размышлений (только администратор)
# ──────────────────────────────────────────

def _model_text(model: ModelInfo, effort: str) -> str:
    return (
        "🤖 <b>Настройки модели</b> (общие для всех чатов)\n\n"
        f"Модель: <b>{model.label}</b>\n"
        f"Размышления: <b>{EFFORT_LABELS[effort]}</b>\n\n"
        "<i>Чем выше уровень размышлений, тем вдумчивее ответы, "
        "но они дольше и дороже. Уровень запоминается для каждой модели.</i>"
    )


def _model_keyboard(model: ModelInfo, effort: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{m.label}{' ✅' if m.id == model.id else ''}",
            callback_data=f"model:set:{m.id}",
        )]
        for m in MODELS.values()
    ]

    effort_buttons = [
        InlineKeyboardButton(
            text=f"🧠 {label}{' ✅' if level == effort else ''}",
            callback_data=f"model:effort:{level}",
        )
        for level, label in EFFORT_LABELS.items()
    ]
    buttons.append(effort_buttons[:3])
    buttons.append(effort_buttons[3:])

    buttons.append([InlineKeyboardButton(text="✖ Закрыть", callback_data="model:close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("model"))
async def cmd_model(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    model, effort = await db.get_model_settings()
    await message.answer(
        _model_text(model, effort),
        parse_mode="HTML",
        reply_markup=_model_keyboard(model, effort),
    )


@router.callback_query(F.data.startswith("model:"))
async def cb_model(callback: CallbackQuery, config: "Config", **kwargs) -> None:
    if callback.from_user.id != config.admin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    _, action, *rest = callback.data.split(":")  # type: ignore[union-attr]
    value = rest[0] if rest else ""

    if action in ("close", "cancel"):
        await close_menu(callback.message)  # type: ignore[arg-type]
        await callback.answer()
        return

    if action == "set" and value in MODELS:
        await db.set_setting("current_model", value)
        notice = f"Модель: {MODELS[value].label}"
    elif action == "effort" and value in EFFORT_LABELS:
        model = await db.get_current_model()
        await db.set_setting(f"effort:{model.id}", value)
        notice = f"Размышления: {EFFORT_LABELS[value]}"
    else:
        # Кнопка из старого меню или неизвестное значение
        await callback.answer("Меню устарело, откройте /model заново.", show_alert=True)
        return

    model, effort = await db.get_model_settings()
    try:
        await callback.message.edit_text(  # type: ignore[union-attr]
            _model_text(model, effort),
            parse_mode="HTML",
            reply_markup=_model_keyboard(model, effort),
        )
    except TelegramBadRequest:
        pass  # нажали уже выбранный вариант — сообщение не изменилось
    await callback.answer(f"✅ {notice}")


@router.message(lambda m: m.text and m.text.strip() in MODELS)
async def cmd_model_set(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    new_model = message.text.strip()  # type: ignore[union-attr]
    await db.set_setting("current_model", new_model)
    await message.answer(
        f"✅ Модель переключена на <b>{MODELS[new_model].label}</b>", parse_mode="HTML"
    )


# ──────────────────────────────────────────
# Доступ: одобрение, бан
# ──────────────────────────────────────────

APPROVED_TEXT = "✅ Администратор открыл вам доступ! Напишите что-нибудь — я отвечу."
DENIED_TEXT = "🚫 Администратор отклонил запрос на доступ к боту."


async def _notify_user(bot, user_id: int, text: str) -> None:
    """Сообщение пользователю о решении по доступу (если он заблокировал бота — не страшно)."""
    try:
        await bot.send_message(user_id, text)
    except Exception as exc:
        await handle_telegram_error(exc, chat_id=user_id, user_id=user_id)


async def _target_id(message: Message, command: str) -> int | None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(f"❌ Укажите ID пользователя: /{command} &lt;user_id&gt;", parse_mode="HTML")
        return None
    return int(parts[1])


@router.callback_query(F.data.startswith("access:"))
async def cb_access(callback: CallbackQuery, config: "Config", **kwargs) -> None:
    """Кнопки «Пустить / Отклонить» в запросе доступа."""
    if callback.from_user.id != config.admin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    _, action, raw_id = callback.data.split(":")  # type: ignore[union-attr]
    user_id = int(raw_id)
    row = await db.get_user(user_id)
    if row is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    who = describe_user(row["first_name"], row["username"], user_id)

    if action == "approve":
        await db.approve_user(user_id)
        await _notify_user(callback.bot, user_id, APPROVED_TEXT)
        result = f"✅ Доступ выдан: {who}"
    else:
        await db.ban_user(user_id)
        await _notify_user(callback.bot, user_id, DENIED_TEXT)
        result = f"🚫 Запрос отклонён: {who}\n<i>Передумаете — /approve {user_id}</i>"

    logger.info("Доступ user_id=%d: %s", user_id, action)
    try:
        await callback.message.edit_text(result, parse_mode="HTML")  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.message(Command("approve"))
async def cmd_approve(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    target_id = await _target_id(message, "approve")
    if target_id is None:
        return
    if await db.approve_user(target_id):
        await message.answer(f"✅ Пользователю {target_id} открыт доступ.")
        await _notify_user(message.bot, target_id, APPROVED_TEXT)
    else:
        await message.answer(
            f"❌ Пользователь {target_id} не найден в базе. Он должен сначала написать боту."
        )


@router.message(Command("ban"))
async def cmd_ban(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    target_id = await _target_id(message, "ban")
    if target_id is None:
        return
    if target_id == config.admin_id:
        await message.answer("❌ Нельзя заблокировать администратора.")
        return

    if await db.ban_user(target_id):
        await message.answer(f"🚫 Пользователь {target_id} заблокирован.")
        await _notify_user(message.bot, target_id, "🚫 Ваш доступ к боту ограничен администратором.")
    else:
        await message.answer(f"❌ Пользователь {target_id} не найден в базе.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    target_id = await _target_id(message, "unban")
    if target_id is None:
        return
    if await db.approve_user(target_id):
        await message.answer(f"✅ Пользователь {target_id} разблокирован, доступ открыт.")
    else:
        await message.answer(f"❌ Пользователь {target_id} не найден в базе.")


def _user_status(row) -> str:
    if row["is_banned"]:
        status = "🚫"
    elif not row["is_approved"]:
        status = "⏳"
    else:
        status = "✅"
    return status + (" 🔕" if row["blocked_bot"] else "")


@router.message(Command("users"))
async def cmd_users(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    rows = await db.get_all_users()
    if not rows:
        await message.answer("📭 Пользователей нет.")
        return

    lines = [f"👥 <b>Пользователи ({len(rows)}):</b>\n"]
    for r in rows:
        uname = f"@{html.escape(r['username'])}" if r["username"] else "—"
        name = html.escape(r["first_name"] or "—")
        lines.append(f"{_user_status(r)} <code>{r['user_id']}</code> {name} ({uname})")
    lines.append(
        "\n<i>✅ доступ есть · ⏳ ждёт одобрения · 🚫 забанен · 🔕 заблокировал бота\n"
        "Управление: /approve, /ban, /unban &lt;user_id&gt;</i>"
    )

    # Разбиваем если длинный список
    full_text = "\n".join(lines)
    parts = split_long_message(full_text)
    for part in parts:
        await message.answer(part, parse_mode="HTML")


@router.message(Command("usage"))
async def cmd_usage(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    rows = await db.get_global_stats()

    total_cost = sum(r["total_cost"] or 0 for r in rows)
    lines = [f"💰 <b>Общая статистика расходов:</b>\n\nИтого: <b>${total_cost:.4f}</b>\n"]

    for r in rows:
        uname = f"@{r['username']}" if r["username"] else str(r["user_id"])
        name = r["first_name"] or "—"
        cost = r["total_cost"] or 0.0
        req = r["total_requests"] or 0
        if req == 0:
            continue
        lines.append(
            f"• {name} ({uname}): <b>${cost:.4f}</b> / {req} запросов"
        )

    full_text = "\n".join(lines)
    parts = split_long_message(full_text)
    for part in parts:
        await message.answer(part, parse_mode="HTML")


@router.message(Command("context"))
async def cmd_context(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return

    current_model = await db.get_current_model()
    limit = context_budget(config, current_model)

    rows = await db.get_all_users()
    if not rows:
        await message.answer("📭 Пользователей нет.")
        return

    lines = [
        f"📊 <b>Контекстные окна</b> (модель: <code>{current_model.id}</code>, "
        f"бюджет: {limit:,} токенов, саммари с {config.summary_trigger_tokens:.0%})\n"
    ]

    for r in rows:
        history = await db.get_history(r["user_id"])
        if not history:
            continue

        tokens = history.get("total_tokens_approx") or 0
        pct = tokens / limit * 100

        raw = history.get("messages_json") or "[]"
        msgs: list = json.loads(raw)
        pairs = len(msgs) // 2

        has_summary = "✅" if history.get("summary") else "—"
        last_msg = history.get("last_message_at") or "—"
        if last_msg != "—":
            try:
                dt = datetime.fromisoformat(last_msg)
                last_msg = dt.strftime("%d.%m %H:%M")
            except ValueError:
                pass

        # Статистика кэширования за последние 20 запросов
        cache = await db.get_cache_stats(r["user_id"])
        total_all = (
            (cache.get("total_input") or 0)
            + (cache.get("total_cache_write") or 0)
            + (cache.get("total_cache_read") or 0)
        )
        cache_read_pct = (
            (cache.get("total_cache_read") or 0) / total_all * 100
            if total_all > 0 else 0.0
        )

        uname = f"@{r['username']}" if r["username"] else str(r["user_id"])
        lines.append(
            f"• {uname}: {pct:.1f}% ({tokens:,} / {limit:,})\n"
            f"  пар: {pairs}, саммари: {has_summary}, "
            f"cache: {cache_read_pct:.0f}%, последнее: {last_msg}"
        )

    if len(lines) == 1:
        lines.append("Нет истории ни у одного пользователя.")

    full_text = "\n".join(lines)
    parts = split_long_message(full_text)
    for part in parts:
        await message.answer(part, parse_mode="HTML")
