from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.types import Message

from bot.handlers._common import handle_incoming

if TYPE_CHECKING:
    import anthropic
    from bot.config import Config

logger = logging.getLogger(__name__)
router = Router(name="photo")

# Максимальный размер изображения для передачи в Claude (байты)
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 МБ


@router.message(lambda m: m.photo is not None)
async def handle_photo(
    message: Message,
    config: "Config",
    client: "anthropic.AsyncAnthropic",
    **kwargs,
) -> None:
    """Обрабатывает входящие фотографии через Claude Vision."""
    photo = message.photo[-1]  # Берём наибольшее разрешение

    if photo.file_size and photo.file_size > MAX_IMAGE_SIZE:
        await message.answer(
            f"❌ Изображение слишком большое (максимум {MAX_IMAGE_SIZE // 1024 // 1024} МБ)."
        )
        return

    try:
        file = await message.bot.get_file(photo.file_id)  # type: ignore[union-attr]
        file_bytes = await message.bot.download_file(file.file_path)  # type: ignore[union-attr]
        image_data = base64.standard_b64encode(file_bytes.read()).decode()  # type: ignore[union-attr]
    except Exception:
        logger.exception("Ошибка загрузки фото")
        await message.answer("❌ Не удалось загрузить изображение. Попробуйте ещё раз.")
        return

    caption = message.caption or "Опиши это изображение."

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_data,
            },
        },
        {
            "type": "text",
            "text": caption,
        },
    ]

    await handle_incoming(
        message=message,
        config=config,
        client=client,
        content=content,
        notify_admin=kwargs.get("notify_admin"),
    )
