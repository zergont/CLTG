from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = "cltg.db"


async def init_db() -> None:
    """Инициализирует БД из schema.sql."""
    with open("schema.sql", encoding="utf-8") as f:
        schema = f.read()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(schema)
        await db.commit()
    logger.info("База данных инициализирована")


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
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name
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


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row is not None and bool(row["is_banned"])


async def set_banned(user_id: int, banned: bool) -> bool:
    """Возвращает True если пользователь найден."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?", (banned, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


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
            "SELECT user_id, username, first_name, is_banned, first_seen FROM users ORDER BY first_seen"
        ) as cur:
            return await cur.fetchall()


async def get_all_active_users() -> list[aiosqlite.Row]:
    """Возвращает всех незабаненных пользователей для рассылки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, first_name FROM users WHERE is_banned = FALSE ORDER BY first_seen"
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
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO usage (chat_id, user_id, input_tokens, output_tokens, cost, model)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, input_tokens, output_tokens, cost, model),
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
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO reminders
                (chat_id, user_id, text, prompt, is_chain, due_at, silent,
                 steps_left, end_at, idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id, user_id, text, prompt, is_chain,
                due_at.isoformat(),
                silent,
                steps_left,
                end_at.isoformat() if end_at else None,
                idempotency_key,
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
    cutoff = datetime.fromtimestamp(
        now.timestamp() + lookahead_seconds, tz=timezone.utc
    ).isoformat()
    now_iso = now.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'scheduled'
              AND due_at <= ?
              AND (picked_at IS NULL OR picked_at < datetime('now', '-60 seconds'))
            ORDER BY due_at
            LIMIT ?
            """,
            (cutoff, batch_limit),
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
                "UPDATE reminders SET status = 'done', executed_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), reminder_id),
            )
        else:
            await db.execute(
                """
                UPDATE reminders
                SET due_at = ?, steps_left = ?, fired_at = ?,
                    picked_at = NULL
                WHERE id = ?
                """,
                (
                    reschedule_at.isoformat(),
                    steps_left,
                    datetime.now(timezone.utc).isoformat(),
                    reminder_id,
                ),
            )
        await db.commit()
