-- ============================================================
-- CLTG — схема базы данных
-- ============================================================

-- История диалогов и саммари
CREATE TABLE IF NOT EXISTS chat_history (
    chat_id              INTEGER PRIMARY KEY,
    messages_json        TEXT,         -- полный массив messages[]
    summary              TEXT,         -- накопленное саммари
    summary_updated_at   DATETIME,
    last_message_at      DATETIME,     -- для триггера 72ч
    total_tokens_approx  INTEGER       -- для триггера 85%
);

-- Пользователи
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    first_seen  DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_welcomed BOOLEAN  DEFAULT FALSE,
    timezone    TEXT     DEFAULT 'Europe/Moscow',
    is_banned   BOOLEAN  DEFAULT FALSE
);

-- Настройки бота
CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('current_model', 'claude-haiku-4-5');

-- Учёт использования (input/output раздельно)
CREATE TABLE IF NOT EXISTS usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER,
    user_id        INTEGER,
    input_tokens   INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cost           REAL    NOT NULL,
    model          TEXT    NOT NULL,
    ts             DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Напоминания
CREATE TABLE IF NOT EXISTS reminders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER  NOT NULL,
    user_id          INTEGER  NOT NULL,
    text             TEXT     NOT NULL,
    prompt           TEXT,
    is_chain         BOOLEAN  DEFAULT FALSE,
    due_at           DATETIME NOT NULL,
    silent           INTEGER  DEFAULT 0,
    status           TEXT     DEFAULT 'scheduled',
    steps_left       INTEGER,
    end_at           DATETIME,
    executed_at      DATETIME,
    picked_at        DATETIME,
    fired_at         DATETIME,
    idempotency_key  TEXT,
    meta_json        TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_usage_chat_id    ON usage(chat_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_id    ON usage(user_id);
CREATE INDEX IF NOT EXISTS idx_reminders_due    ON reminders(status, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_chat   ON reminders(chat_id);
CREATE INDEX IF NOT EXISTS idx_users_username   ON users(username);
