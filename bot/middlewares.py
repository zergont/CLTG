from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from bot.utils import db

logger = logging.getLogger(__name__)


class RegisterUserMiddleware(BaseMiddleware):
    """
    Регистрирует пользователя при каждом входящем сообщении.
    Проверяет бан и блокирует обработку для забаненных.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user = event.from_user

        # Регистрация / обновление
        is_new = await db.upsert_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        data["is_new_user"] = is_new

        # Проверка бана
        if await db.is_banned(user.id):
            await event.answer(
                "🚫 Ваш доступ к боту ограничен. Обратитесь к администратору."
            )
            return  # прерываем цепочку обработчиков

        return await handler(event, data)
