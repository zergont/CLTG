from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from bot.utils import db

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import User
    from bot.config import Config

logger = logging.getLogger(__name__)

BANNED_TEXT = "🚫 Ваш доступ к боту ограничен. Обратитесь к администратору."
ACCESS_REQUESTED_TEXT = (
    "👋 Привет! Это семейный бот, доступ к нему выдаёт администратор.\n"
    "Я отправил ему запрос — как только он его одобрит, я напишу."
)
ACCESS_PENDING_TEXT = "⏳ Запрос на доступ ещё у администратора. Как только он ответит, я напишу."


def access_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пустить", callback_data=f"access:approve:{user_id}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"access:deny:{user_id}"),
    ]])


def describe_user(first_name: str | None, username: str | None, user_id: int) -> str:
    """Имя пользователя для сообщений администратору (HTML)."""
    name = html.escape(first_name or "—")
    handle = f" (@{html.escape(username)})" if username else ""
    return f"{name}{handle}, ID <code>{user_id}</code>"


class RegisterUserMiddleware(BaseMiddleware):
    """
    Регистрирует пользователя и проверяет доступ — для сообщений и нажатий кнопок.

    Администратор проходит всегда. Забаненные получают отказ. Новые пользователи
    без одобрения до Claude не доходят: администратору уходит запрос с кнопками
    «Пустить / Отклонить» (один раз), пользователь получает ответ «запрос отправлен».
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        config: "Config" = data["config"]

        if isinstance(event, Message):
            data["is_new_user"] = await db.upsert_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )

        row = await db.get_user(user.id)

        if user.id == config.admin_id:
            if row is not None and not row["is_approved"]:
                await db.approve_user(user.id)
            return await handler(event, data)

        if row is not None and row["is_banned"]:
            if isinstance(event, Message):
                await event.answer(BANNED_TEXT)
            else:
                await event.answer(BANNED_TEXT, show_alert=True)
            return None

        if row is None or not row["is_approved"]:
            if isinstance(event, Message):
                await self._request_access(event, user, data["bot"], config)
            else:
                await event.answer("Доступ к боту ещё не выдан.", show_alert=True)
            return None

        return await handler(event, data)

    @staticmethod
    async def _request_access(message: Message, user: "User", bot: "Bot", config: "Config") -> None:
        # В группах незнакомцев молча игнорируем — запросы только в личке
        if message.chat.type != "private":
            return

        if not await db.mark_access_requested(user.id):
            await message.answer(ACCESS_PENDING_TEXT)
            return

        logger.info("Запрос доступа от user_id=%d (@%s)", user.id, user.username)
        await message.answer(ACCESS_REQUESTED_TEXT)

        first_text = message.text or message.caption or "[фото, файл или стикер]"
        if len(first_text) > 300:
            first_text = first_text[:300] + "…"
        try:
            await bot.send_message(
                config.admin_id,
                "🔐 <b>Запрос доступа к боту</b>\n\n"
                f"{describe_user(user.first_name, user.username, user.id)}\n\n"
                f"Первое сообщение: «{html.escape(first_text)}»",
                parse_mode="HTML",
                reply_markup=access_request_keyboard(user.id),
            )
        except Exception:
            logger.exception("Не удалось отправить администратору запрос доступа от user_id=%d", user.id)
