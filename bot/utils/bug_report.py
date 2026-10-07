from __future__ import annotations

import asyncio
import html
import logging
import time
import traceback
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

# Одинаковая ошибка (одно место в коде + тип исключения) репортится не чаще раза за период
REPORT_COOLDOWN_SECONDS = 15 * 60
# Хвост трейсбека, который помещается в сообщение Telegram вместе с текстом
TRACEBACK_TAIL_CHARS = 2500
MESSAGE_TAIL_CHARS = 800

# Выставляется внутри задачи отправки: ошибки при самой отправке не репортим (без рекурсии)
_sending: ContextVar[bool] = ContextVar("bug_report_sending", default=False)


class AdminBugReportHandler(logging.Handler):
    """Пересылает записи лога уровня ERROR и выше администратору в Telegram."""

    def __init__(self, bot: "Bot", admin_id: int) -> None:
        super().__init__(level=logging.ERROR)
        self._bot = bot
        self._admin_id = admin_id
        # ключ ошибки → (время последнего отчёта, сколько повторов подавлено с тех пор)
        self._seen: dict[tuple[str, int, str], tuple[float, int]] = {}
        self._tasks: set[asyncio.Task] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if _sending.get():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # вне event loop (старт/остановка) отчёт не отправить

        exc_type = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
        key = (record.name, record.lineno, exc_type)
        now = time.monotonic()
        last_sent, suppressed = self._seen.get(key, (0.0, 0))
        if last_sent and now - last_sent < REPORT_COOLDOWN_SECONDS:
            self._seen[key] = (last_sent, suppressed + 1)
            return
        self._seen[key] = (now, 0)

        try:
            text = self._format(record, suppressed)
        except Exception:
            return
        task = loop.create_task(self._send(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _format(self, record: logging.LogRecord, suppressed: int) -> str:
        message = record.getMessage()
        if len(message) > MESSAGE_TAIL_CHARS:
            message = message[:MESSAGE_TAIL_CHARS] + "…"

        parts = [
            f"🐞 <b>Ошибка в боте</b> ({html.escape(record.levelname)})",
            f"<code>{html.escape(record.name)}:{record.lineno}</code> в <code>{html.escape(record.funcName)}</code>",
            "",
            html.escape(message),
        ]
        if record.exc_info and record.exc_info[1]:
            tb = "".join(traceback.format_exception(*record.exc_info))
            if len(tb) > TRACEBACK_TAIL_CHARS:
                tb = "…" + tb[-TRACEBACK_TAIL_CHARS:]
            parts += ["", f"<pre>{html.escape(tb)}</pre>"]
        if suppressed:
            parts += ["", f"<i>Эта же ошибка повторялась ещё {suppressed} раз(а) после прошлого отчёта.</i>"]
        return "\n".join(parts)

    async def _send(self, text: str) -> None:
        _sending.set(True)
        try:
            await self._bot.send_message(self._admin_id, text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Не удалось отправить баг-репорт администратору: %s", exc)


def setup_bug_reports(bot: "Bot", admin_id: int) -> AdminBugReportHandler:
    """Подключает отправку баг-репортов к корневому логгеру."""
    handler = AdminBugReportHandler(bot, admin_id)
    logging.getLogger().addHandler(handler)
    return handler
