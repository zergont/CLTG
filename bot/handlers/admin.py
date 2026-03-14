from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.utils import db
from bot.utils.anthropic.models import context_limit, MODEL_LABELS, AVAILABLE_MODELS

if TYPE_CHECKING:
    from bot.config import Config

logger = logging.getLogger(__name__)
router = Router(name="admin")


def _admin_filter(message: Message, config: "Config") -> bool:
    return message.from_user is not None and message.from_user.id == config.admin_id


@router.message(Command("model"))
async def cmd_model(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    current = await db.get_setting("current_model") or config.model_haiku
    await message.answer(
        "🤖 <b>Выбор модели Claude:</b>",
        parse_mode="HTML",
        reply_markup=_model_keyboard(current),
    )


def _model_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for m in AVAILABLE_MODELS:
        label = MODEL_LABELS.get(m, m)
        mark = " ✅" if m == current else ""
        buttons.append([InlineKeyboardButton(
            text=f"{label}{mark}",
            callback_data=f"model:{m}",
        )])
    buttons.append([InlineKeyboardButton(text="✖ Отмена", callback_data="model:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("model:"))
async def cb_model(callback: CallbackQuery, config: "Config", **kwargs) -> None:
    if callback.from_user.id != config.admin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]  # type: ignore[union-attr]

    if action == "cancel":
        await callback.message.delete()  # type: ignore[union-attr]
        await callback.answer()
        return

    await db.set_setting("current_model", action)
    label = MODEL_LABELS.get(action, action)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✅ Модель переключена на <b>{label}</b>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(lambda m: m.text and m.text.strip() in AVAILABLE_MODELS)
async def cmd_model_set(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    new_model = message.text.strip()  # type: ignore[union-attr]
    await db.set_setting("current_model", new_model)
    label = MODEL_LABELS.get(new_model, new_model)
    await message.answer(f"✅ Модель переключена на <b>{label}</b>", parse_mode="HTML")


@router.message(Command("ban"))
async def cmd_ban(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("❌ Укажите ID пользователя: /ban &lt;user_id&gt;", parse_mode="HTML")
        return

    target_id = int(parts[1])
    if target_id == config.admin_id:
        await message.answer("❌ Нельзя заблокировать администратора.")
        return

    found = await db.set_banned(target_id, True)
    if found:
        await message.answer(f"🚫 Пользователь {target_id} заблокирован.")
        try:
            await message.bot.send_message(  # type: ignore[union-attr]
                target_id,
                "🚫 Ваш доступ к боту ограничен администратором."
            )
        except Exception:
            pass
    else:
        await message.answer(f"❌ Пользователь {target_id} не найден в базе.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("❌ Укажите ID пользователя: /unban &lt;user_id&gt;", parse_mode="HTML")
        return

    target_id = int(parts[1])
    found = await db.set_banned(target_id, False)
    if found:
        await message.answer(f"✅ Пользователь {target_id} разблокирован.")
    else:
        await message.answer(f"❌ Пользователь {target_id} не найден в базе.")


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
        status = "🚫" if r["is_banned"] else "✅"
        uname = f"@{r['username']}" if r["username"] else "—"
        name = r["first_name"] or "—"
        lines.append(f"{status} <code>{r['user_id']}</code> {name} ({uname})")

    # Разбиваем если длинный список
    from bot.utils.html import split_long_message
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

    from bot.utils.html import split_long_message
    full_text = "\n".join(lines)
    parts = split_long_message(full_text)
    for part in parts:
        await message.answer(part, parse_mode="HTML")


@router.message(Command("context"))
async def cmd_context(message: Message, config: "Config", **kwargs) -> None:
    if not _admin_filter(message, config):
        return

    current_model = await db.get_setting("current_model") or config.model_haiku
    limit = context_limit(current_model)

    rows = await db.get_all_users()
    if not rows:
        await message.answer("📭 Пользователей нет.")
        return

    lines = [
        f"📊 <b>Контекстные окна</b> (модель: <code>{current_model}</code>, "
        f"лимит: {limit:,} токенов)\n"
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

    from bot.utils.html import split_long_message
    full_text = "\n".join(lines)
    parts = split_long_message(full_text)
    for part in parts:
        await message.answer(part, parse_mode="HTML")
