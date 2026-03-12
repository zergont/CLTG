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
router = Router(name="document")

SUPPORTED_MIME_TYPES = {
    "application/pdf": "application/pdf",
    "text/plain": "text/plain",
    "text/csv": "text/plain",
    "text/html": "text/html",
    "text/xml": "text/xml",
    "application/json": "text/plain",
}


@router.message(lambda m: m.document is not None)
async def handle_document(
    message: Message,
    config: "Config",
    client: "anthropic.AsyncAnthropic",
    **kwargs,
) -> None:
    """Обрабатывает входящие документы и PDF через Claude."""
    doc = message.document  # type: ignore[union-attr]
    max_bytes = config.max_file_mb * 1024 * 1024

    if doc.file_size and doc.file_size > max_bytes:
        await message.answer(
            f"❌ Файл слишком большой (максимум {config.max_file_mb} МБ)."
        )
        return

    mime = doc.mime_type or "application/octet-stream"
    claude_mime = SUPPORTED_MIME_TYPES.get(mime)

    if not claude_mime:
        await message.answer(
            f"❌ Тип файла <code>{mime}</code> не поддерживается.\n"
            "Поддерживаются: PDF, TXT, CSV, HTML, XML, JSON.",
            parse_mode="HTML",
        )
        return

    try:
        file = await message.bot.get_file(doc.file_id)  # type: ignore[union-attr]
        file_bytes = await message.bot.download_file(file.file_path)  # type: ignore[union-attr]
        doc_data = base64.standard_b64encode(file_bytes.read()).decode()  # type: ignore[union-attr]
    except Exception:
        logger.exception("Ошибка загрузки документа")
        await message.answer("❌ Не удалось загрузить файл. Попробуйте ещё раз.")
        return

    caption = message.caption or f"Проанализируй документ «{doc.file_name}»."

    if claude_mime == "application/pdf":
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": doc_data,
                },
            },
            {"type": "text", "text": caption},
        ]
    else:
        # Текстовые форматы — декодируем и передаём как текст
        try:
            raw_bytes = base64.b64decode(doc_data)
            text_content = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            text_content = f"[не удалось декодировать {doc.file_name}]"

        content = [
            {
                "type": "text",
                "text": f"{caption}\n\nСодержимое файла «{doc.file_name}»:\n```\n{text_content}\n```",
            }
        ]

    await handle_incoming(
        message=message,
        config=config,
        client=client,
        content=content,
        notify_admin=kwargs.get("notify_admin"),
    )
