from __future__ import annotations

import os

import aiosqlite

from bot.utils import db
from bot.utils.anthropic.models import DEFAULT_EFFORT


async def test_fresh_db_defaults_to_haiku_5_5(db_path):
    await db.init_db()
    model, effort = await db.get_model_settings()
    assert model.id == "claude-haiku-5-5"
    assert effort == DEFAULT_EFFORT


async def test_legacy_model_in_db_is_migrated(db_path):
    await db.init_db()
    await db.set_setting("current_model", "claude-sonnet-4-6")

    await db.init_db()  # повторный старт бота применяет миграции

    assert await db.get_setting("current_model") == "claude-sonnet-5-5"


async def test_effort_is_stored_per_model(db_path):
    await db.init_db()
    await db.set_setting("current_model", "claude-opus-5-5")
    await db.set_setting("effort:claude-opus-5-5", "high")
    await db.set_setting("effort:claude-haiku-5-5", "bogus")

    assert await db.get_model_settings() == (db.get_model("claude-opus-5-5"), "high")
    assert await db.get_effort("claude-haiku-5-5") == DEFAULT_EFFORT


async def test_migration_keeps_existing_usage_table(db_path):
    # Старая схема usage без колонок кэша
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE usage (id INTEGER PRIMARY KEY, chat_id INTEGER, user_id INTEGER, "
            "input_tokens INTEGER, output_tokens INTEGER, cost REAL NOT NULL, model TEXT NOT NULL, "
            "ts DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await conn.commit()

    await db.init_db()
    await db.log_usage(1, 1, 10, 20, 0.5, "claude-haiku-5-5", 3, 4)
    stats = await db.get_cache_stats(1)
    assert stats == {"total_input": 10, "total_cache_write": 3, "total_cache_read": 4}
    assert os.path.exists(db_path)
