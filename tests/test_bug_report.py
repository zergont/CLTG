from __future__ import annotations

import asyncio
import logging

from bot.utils.bug_report import AdminBugReportHandler


class FakeBot:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


def _logger_with(handler: logging.Handler) -> logging.Logger:
    log = logging.getLogger(f"test.bug_report.{id(handler)}")
    log.propagate = False
    log.handlers = [handler]
    log.setLevel(logging.DEBUG)
    return log


async def _drain():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_error_with_traceback_is_reported():
    bot = FakeBot()
    log = _logger_with(AdminBugReportHandler(bot, admin_id=42))
    try:
        {}["missing"]
    except KeyError:
        log.exception("Сбой <обработки> напоминания #%d", 7)
    await _drain()

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 42
    assert "Сбой &lt;обработки&gt; напоминания #7" in text
    assert "KeyError" in text and "<pre>" in text


async def test_warnings_are_not_reported():
    bot = FakeBot()
    log = _logger_with(AdminBugReportHandler(bot, admin_id=42))
    log.warning("временная ошибка")
    await _drain()
    assert bot.sent == []


async def test_repeated_error_is_throttled_and_counted():
    bot = FakeBot()
    handler = AdminBugReportHandler(bot, admin_id=42)
    log = _logger_with(handler)

    def fail():
        log.error("одна и та же ошибка")  # одна строка кода → один ключ

    for _ in range(3):
        fail()
    await _drain()
    assert len(bot.sent) == 1

    # После паузы отчёт приходит снова, с числом подавленных повторов
    key = next(iter(handler._seen))
    handler._seen[key] = (handler._seen[key][0] - 10_000, handler._seen[key][1])
    fail()
    await _drain()
    assert len(bot.sent) == 2
    assert "ещё 2 раз" in bot.sent[1][1]


async def test_send_failure_does_not_recurse():
    bot = FakeBot(fail=True)
    handler = AdminBugReportHandler(bot, admin_id=42)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("test.bug_report.recursion").error("ошибка")
        await _drain()
    finally:
        root.removeHandler(handler)
    # Ошибка отправки логируется как WARNING и не порождает новых отчётов
    assert len(handler._seen) == 1
