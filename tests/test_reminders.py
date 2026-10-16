from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.utils import db, reminders
from bot.utils.anthropic import chat

CHAT = 42
UTC = timezone.utc


class FakeBot:
    def __init__(self, error: Exception | None = None):
        self.sent: list[str] = []
        self.error = error

    async def send_message(self, chat_id, text, **kwargs):
        if self.error:
            raise self.error
        self.sent.append(text)


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SendMessage(chat_id=CHAT, text="x"), message="bot was blocked by the user")


def bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=CHAT, text="x"), message="chat not found")


async def add(due: datetime, **kwargs) -> dict:
    reminder_id = await db.add_reminder(chat_id=CHAT, user_id=CHAT, text="полить цветы", due_at=due, **kwargs)
    return await reminder_row(reminder_id)


async def reminder_row(reminder_id: int) -> dict:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        return dict(await cur.fetchone())


async def fire(row: dict, bot: FakeBot, config, now: datetime) -> dict:
    await reminders._fire_reminder(bot, config, client=None, reminder=row, now=now)
    return await reminder_row(row["id"])


@pytest.fixture
async def fresh_db(db_path):
    await db.init_db()


# ── Расписание ───────────────────────────────────────────────

def test_next_due_skips_missed_occurrences_instead_of_bursting():
    due = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # бот лежал 4 дня
    next_due, skipped = reminders._next_due(due, 86400, now)
    assert next_due == datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    assert skipped == 4


def test_next_due_normal_case_and_minimum_interval():
    due = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert reminders._next_due(due, 3600, due) == (due + timedelta(hours=1), 0)
    assert reminders._next_due(due, 1, due)[0] == due + timedelta(seconds=60)


# ── Хранение времени ─────────────────────────────────────────

async def test_tool_stores_time_in_utc_and_interval_in_meta(fresh_db):
    result = await chat._execute_reminder_tool(
        {"text": "зарядка", "due_at": "2026-10-20T09:00:00+03:00", "is_chain": True,
         "interval_seconds": 86400, "prompt": "пожелай доброго утра"},
        chat_id=CHAT, user_id=CHAT, default_silent=True,
    )
    assert "успешно создано" in result
    (row,) = [dict(r) for r in await db.get_user_reminders(CHAT)]
    full = await reminder_row(row["id"])
    assert full["due_at"] == "2026-10-20T06:00:00+00:00"
    assert json.loads(full["meta_json"]) == {"interval_seconds": 86400}
    assert full["prompt"] == "пожелай доброго утра"


async def test_stuck_reminder_is_retried_after_timeout_not_next_day(fresh_db):
    now = datetime.now(UTC)
    await add(now - timedelta(minutes=1))

    assert len(await db.pick_due_reminders(now, 2, 50)) == 1
    assert await db.pick_due_reminders(now, 2, 50) == []  # взято, ждём таймаут

    # Обработка «упала» 2 минуты назад — напоминание снова доступно сегодня же
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("UPDATE reminders SET picked_at = ?", (db.utc_iso(now - timedelta(minutes=2)),))
        await conn.commit()
    assert len(await db.pick_due_reminders(now, 2, 50)) == 1


async def test_migration_normalizes_old_time_formats(db_path):
    await db.init_db()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("ALTER TABLE reminders DROP COLUMN attempts")  # как в старой схеме
        await conn.execute(
            "INSERT INTO reminders (chat_id, user_id, text, due_at, picked_at) "
            "VALUES (1, 1, 't', '2026-10-20T09:00:00.123456+03:00', '2026-10-19T10:00:00.5+00:00')"
        )
        await conn.commit()

    await db.init_db()
    async with aiosqlite.connect(db_path) as conn:
        due_at, picked_at, attempts = await (
            await conn.execute("SELECT due_at, picked_at, attempts FROM reminders")
        ).fetchone()
    assert (due_at, picked_at, attempts) == ("2026-10-20T06:00:00+00:00", None, 0)


# ── Отправка ─────────────────────────────────────────────────

async def test_one_shot_reminder_is_sent_and_done(fresh_db, config):
    now = datetime.now(UTC)
    bot = FakeBot()
    row = await fire(await add(now), bot, config, now)
    assert bot.sent == ["🔔 полить цветы"] and row["status"] == "done"


async def test_chain_after_downtime_moves_to_future(fresh_db, config):
    now = datetime.now(UTC)
    row = await add(now - timedelta(days=3, hours=1), is_chain=True, interval_seconds=86400)
    row = await fire(row, FakeBot(), config, now)
    next_due = datetime.fromisoformat(row["due_at"])
    assert row["status"] == "scheduled" and now < next_due <= now + timedelta(days=1)


async def test_blocked_user_does_not_loop_forever(fresh_db, config):
    now = datetime.now(UTC)
    one_shot = await fire(await add(now), FakeBot(error=forbidden()), config, now)
    assert one_shot["status"] == "done"

    chain = await fire(await add(now, is_chain=True, interval_seconds=3600), FakeBot(error=forbidden()), config, now)
    assert chain["status"] == "scheduled" and chain["picked_at"] is None


async def test_send_errors_retry_then_give_up(fresh_db, config):
    now = datetime.now(UTC)
    row = await add(now)
    bot = FakeBot(error=bad_request())
    for attempt in range(1, db.REMINDER_MAX_ATTEMPTS + 1):
        state = await fire(row, bot, config, now)
        assert state["attempts"] == attempt
    assert state["status"] == "failed"
