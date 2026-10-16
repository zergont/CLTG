from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from bot.utils.anthropic.models import (
    DEFAULT_EFFORT,
    EFFORT_LABELS,
    LEGACY_MODELS,
    ModelInfo,
    get_model,
)

logger = logging.getLogger(__name__)

DB_PATH = "cltg.db"

# Через сколько секунд взятое, но не завершённое напоминание можно взять повторно
PICK_TIMEOUT_SECONDS = 60
# Сколько раз пробовать отправить напоминание, прежде чем пометить его failed
REMINDER_MAX_ATTEMPTS = 3


def utc_iso(dt: datetime) -> str:
    """Время для хранения в БД: всегда UTC, с точностью до секунд.

    Времена в БД сравниваются как строки, поэтому формат должен быть единым
    (раньше смешивались «2026-10-16T10:00:00.123+00:00», «+03:00» и
    «2026-10-16 10:00:00» из SQLite — из-за этого напоминания залипали).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


async def init_db() -> None:
    """Инициализирует БД из schema.sql и применяет миграции."""
    with open("schema.sql", encoding="utf-8") as f:
        schema = f.read()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(schema)
        await conn.commit()

        # Миграции для существующих БД
        await _migrate(conn)

    logger.info("База данных инициализирована")


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Добавляет недостающие колонки в существующие таблицы."""
    # usage: cache_write_tokens, cache_read_tokens (v1.1.0)
    cursor = await conn.execute("PRAGMA table_info(usage)")
    columns = {row[1] for row in await cursor.fetchall()}

    added = False
    if "cache_write_tokens" not in columns:
        await conn.execute(
            "ALTER TABLE usage ADD COLUMN cache_write_tokens INTEGER DEFAULT 0"
        )
        added = True
    if "cache_read_tokens" not in columns:
        await conn.execute(
            "ALTER TABLE usage ADD COLUMN cache_read_tokens INTEGER DEFAULT 0"
        )
        added = True

    if added:
        await conn.commit()
        logger.info("Миграция: добавлены колонки cache_write_tokens, cache_read_tokens")

    # users: доступ по одобрению администратора (v1.3.0)
    cursor = await conn.execute("PRAGMA table_info(users)")
    user_columns = {row[1] for row in await cursor.fetchall()}
    if "is_approved" not in user_columns:
        await conn.execute("ALTER TABLE users ADD COLUMN is_approved BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE users ADD COLUMN blocked_bot BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE users ADD COLUMN access_requested_at DATETIME")
        # Те, кто уже пользуется ботом, сохраняют доступ. Помеченные 🚫 остаются
        # без доступа: раньше так же помечали и тех, кто сам заблокировал бота
        await conn.execute("UPDATE users SET is_approved = TRUE WHERE is_banned = FALSE")
        await conn.commit()
        logger.info("Миграция: доступ по одобрению, текущие пользователи одобрены")

    # reminders: счётчик попыток и единый формат времени в UTC (v1.4.0)
    cursor = await conn.execute("PRAGMA table_info(reminders)")
    reminder_columns = {row[1] for row in await cursor.fetchall()}
    if "attempts" not in reminder_columns:
        await conn.execute("ALTER TABLE reminders ADD COLUMN attempts INTEGER DEFAULT 0")
        cursor = await conn.execute(
            "SELECT id, due_at, end_at FROM reminders WHERE status = 'scheduled'"
        )
        for reminder_id, due_at, end_at in await cursor.fetchall():
            await conn.execute(
                "UPDATE reminders SET due_at = ?, end_at = ?, picked_at = NULL WHERE id = ?",
                (
                    utc_iso(datetime.fromisoformat(due_at)),
                    utc_iso(datetime.fromisoformat(end_at)) if end_at else None,
                    reminder_id,
                ),
            )
        await conn.commit()
        logger.info("Миграция: напоминания приведены к UTC, добавлен счётчик попыток")

    # bot_settings: устаревшие модели → актуальные (v1.2.0)
    cursor = await conn.execute("SELECT value FROM bot_settings WHERE key = 'current_model'")
    row = await cursor.fetchone()
    if row and row[0] in LEGACY_MODELS:
        await conn.execute(
            "UPDATE bot_settings SET value = ? WHERE key = 'current_model'",
            (LEGACY_MODELS[row[0]],),
        )
        await conn.commit()
        logger.info("Миграция: модель %s заменена на %s", row[0], LEGACY_MODELS[row[0]])


# ──────────────────────────────────────────
# Пользователи
# ──────────────────────────────────────────

async def upsert_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> bool:
    """
    Создаёт или обновляет пользователя.
    Возвращает True если пользователь новый (is_welcomed=False).
    Раз человек пишет боту, значит не блокирует его — снимаем пометку blocked_bot.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username    = excluded.username,
                first_name  = excluded.first_name,
                last_name   = excluded.last_name,
                blocked_bot = FALSE
            """,
            (user_id, username, first_name, last_name),
        )
        await db.commit()
        async with db.execute(
            "SELECT is_welcomed FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row is not None and not row["is_welcomed"]


async def mark_welcomed(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_welcomed = TRUE WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def get_user(user_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()


async def _update_user(sql: str, params: tuple) -> bool:
    """Выполняет UPDATE по пользователю. Возвращает True, если пользователь найден."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount > 0


async def approve_user(user_id: int) -> bool:
    """Открывает доступ (и снимает бан)."""
    return await _update_user(
        "UPDATE users SET is_approved = TRUE, is_banned = FALSE WHERE user_id = ?", (user_id,)
    )


async def ban_user(user_id: int) -> bool:
    """Бан администратором: доступа нет, рассылки не приходят."""
    return await _update_user(
        "UPDATE users SET is_banned = TRUE, is_approved = FALSE WHERE user_id = ?", (user_id,)
    )


async def set_blocked_bot(user_id: int, blocked: bool) -> bool:
    """Пользователь сам заблокировал бота в Telegram (это не бан)."""
    return await _update_user(
        "UPDATE users SET blocked_bot = ? WHERE user_id = ?", (blocked, user_id)
    )


async def mark_access_requested(user_id: int) -> bool:
    """Отмечает запрос доступа. True — запрос новый и админа нужно уведомить."""
    return await _update_user(
        "UPDATE users SET access_requested_at = ? WHERE user_id = ? AND access_requested_at IS NULL",
        (datetime.now(timezone.utc).isoformat(), user_id),
    )


async def update_timezone(user_id: int, tz: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?", (tz, user_id)
        )
        await db.commit()


async def get_all_users() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, username, first_name, is_banned, is_approved, blocked_bot, first_seen "
            "FROM users ORDER BY first_seen"
        ) as cur:
            return await cur.fetchall()


async def get_all_active_users() -> list[aiosqlite.Row]:
    """Пользователи для рассылки: с доступом, не забанены и не заблокировали бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, first_name FROM users "
            "WHERE is_approved = TRUE AND is_banned = FALSE AND blocked_bot = FALSE "
            "ORDER BY first_seen"
        ) as cur:
            return await cur.fetchall()


# ──────────────────────────────────────────
# История диалогов
# ──────────────────────────────────────────

async def get_history(chat_id: int) -> dict:
    """Возвращает запись chat_history или пустой словарь."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chat_history WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return {}
            return dict(row)


async def save_history(
    chat_id: int,
    messages: list[dict],
    total_tokens: int,
    summary: str | None = None,
    summary_updated_at: datetime | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO chat_history
                (chat_id, messages_json, summary, summary_updated_at, last_message_at, total_tokens_approx)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                messages_json       = excluded.messages_json,
                summary             = COALESCE(excluded.summary, summary),
                summary_updated_at  = COALESCE(excluded.summary_updated_at, summary_updated_at),
                last_message_at     = excluded.last_message_at,
                total_tokens_approx = excluded.total_tokens_approx
            """,
            (
                chat_id,
                json.dumps(messages, ensure_ascii=False),
                summary,
                summary_updated_at.isoformat() if summary_updated_at else None,
                now,
                total_tokens,
            ),
        )
        await db.commit()


async def reset_history(chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE chat_history
            SET messages_json = '[]', summary = NULL,
                summary_updated_at = NULL, total_tokens_approx = 0
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        await db.commit()


# ──────────────────────────────────────────
# Настройки бота
# ──────────────────────────────────────────

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_current_model() -> ModelInfo:
    return get_model(await get_setting("current_model"))


async def get_effort(model_id: str) -> str:
    """Уровень размышлений, выбранный для модели (хранится отдельно для каждой модели)."""
    effort = await get_setting(f"effort:{model_id}")
    return effort if effort in EFFORT_LABELS else DEFAULT_EFFORT


async def get_model_settings() -> tuple[ModelInfo, str]:
    """Текущая рабочая модель и её уровень размышлений."""
    model = await get_current_model()
    return model, await get_effort(model.id)


# ──────────────────────────────────────────
# Учёт использования
# ──────────────────────────────────────────

async def log_usage(
    chat_id: int,
    user_id: int,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    model: str,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO usage (chat_id, user_id, input_tokens, output_tokens,
                               cache_write_tokens, cache_read_tokens, cost, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, input_tokens, output_tokens,
             cache_write_tokens, cache_read_tokens, cost, model),
        )
        await db.commit()


async def get_user_stats(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                SUM(input_tokens)  AS total_input,
                SUM(output_tokens) AS total_output,
                SUM(cost)          AS total_cost,
                COUNT(*)           AS total_requests
            FROM usage WHERE user_id = ?
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_global_stats() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                SUM(us.input_tokens)  AS total_input,
                SUM(us.output_tokens) AS total_output,
                SUM(us.cost)          AS total_cost,
                COUNT(us.id)          AS total_requests
            FROM users u
            LEFT JOIN usage us ON u.user_id = us.user_id
            GROUP BY u.user_id
            ORDER BY total_cost DESC NULLS LAST
            """
        ) as cur:
            return await cur.fetchall()


async def get_cache_stats(user_id: int, last_n: int = 20) -> dict:
    """Возвращает статистику кэширования за последние N запросов."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0)        AS total_input,
                COALESCE(SUM(cache_write_tokens), 0)   AS total_cache_write,
                COALESCE(SUM(cache_read_tokens), 0)    AS total_cache_read
            FROM (
                SELECT input_tokens, cache_write_tokens, cache_read_tokens
                FROM usage
                WHERE user_id = ?
                ORDER BY ts DESC
                LIMIT ?
            )
            """,
            (user_id, last_n),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


# ──────────────────────────────────────────
# Напоминания
# ──────────────────────────────────────────

async def add_reminder(
    chat_id: int,
    user_id: int,
    text: str,
    due_at: datetime,
    prompt: str | None = None,
    is_chain: bool = False,
    silent: int = 0,
    steps_left: int | None = None,
    end_at: datetime | None = None,
    idempotency_key: str | None = None,
    interval_seconds: int | None = None,
) -> int:
    meta = {"interval_seconds": interval_seconds} if interval_seconds else None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO reminders
                (chat_id, user_id, text, prompt, is_chain, due_at, silent,
                 steps_left, end_at, idempotency_key, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id, user_id, text, prompt, is_chain,
                utc_iso(due_at),
                silent,
                steps_left,
                utc_iso(end_at) if end_at else None,
                idempotency_key,
                json.dumps(meta) if meta else None,
            ),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def get_user_reminders(user_id: int) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, text, due_at, is_chain, steps_left, end_at, silent
            FROM reminders
            WHERE user_id = ? AND status = 'scheduled'
            ORDER BY due_at
            """,
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def delete_reminder(reminder_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_all_reminders(user_id: int) -> int:
    """Удаляет все активные напоминания пользователя. Возвращает количество удалённых."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM reminders WHERE user_id = ? AND status = 'scheduled'",
            (user_id,),
        )
        await db.commit()
        return cur.rowcount


async def pick_due_reminders(
    now: datetime,
    lookahead_seconds: int,
    batch_limit: int,
) -> list[aiosqlite.Row]:
    """
    Атомарно выбирает напоминания, готовые к отправке.
    picked_at защищает от двойного срабатывания.
    """
    cutoff = utc_iso(now + timedelta(seconds=lookahead_seconds))
    stale_before = utc_iso(datetime.now(timezone.utc) - timedelta(seconds=PICK_TIMEOUT_SECONDS))
    now_iso = utc_iso(now)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'scheduled'
              AND due_at <= ?
              AND (picked_at IS NULL OR picked_at < ?)
            ORDER BY due_at
            LIMIT ?
            """,
            (cutoff, stale_before, batch_limit),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return []

        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE reminders SET picked_at = ? WHERE id IN ({placeholders})",
            [now_iso, *ids],
        )
        await db.commit()
        return rows


async def complete_reminder(
    reminder_id: int,
    reschedule_at: datetime | None,
    steps_left: int | None,
) -> None:
    """
    Завершает или перепланирует напоминание.
    reschedule_at=None → статус 'done'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if reschedule_at is None:
            await db.execute(
                "UPDATE reminders SET status = 'done', executed_at = ?, attempts = 0 WHERE id = ?",
                (utc_iso(datetime.now(timezone.utc)), reminder_id),
            )
        else:
            await db.execute(
                """
                UPDATE reminders
                SET due_at = ?, steps_left = ?, fired_at = ?,
                    picked_at = NULL, attempts = 0
                WHERE id = ?
                """,
                (
                    utc_iso(reschedule_at),
                    steps_left,
                    utc_iso(datetime.now(timezone.utc)),
                    reminder_id,
                ),
            )
        await db.commit()


async def fail_reminder(reminder_id: int) -> bool:
    """Учитывает неудачную попытку отправки.

    Напоминание остаётся взятым и повторится через PICK_TIMEOUT_SECONDS.
    После REMINDER_MAX_ATTEMPTS неудач — статус failed. Возвращает True, если сдались.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET attempts = COALESCE(attempts, 0) + 1 WHERE id = ?", (reminder_id,)
        )
        cur = await db.execute(
            "UPDATE reminders SET status = 'failed' WHERE id = ? AND attempts >= ?",
            (reminder_id, REMINDER_MAX_ATTEMPTS),
        )
        await db.commit()
        return cur.rowcount > 0
