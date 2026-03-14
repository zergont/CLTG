#!/usr/bin/env bash
# ============================================================
# CLTG — скрипт обновления (запускается systemd timer'ом)
# Можно запускать вручную: sudo bash update_bot.sh
# ============================================================
set -euo pipefail

APP_DIR="/opt/cltg"
APP_USER="cltg"
SERVICE="cltg-bot"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Запуск обновления CLTG..."

cd "$APP_DIR"

# Git может ругаться на владельца директории при запуске от root
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

# Сохраняем текущий коммит
OLD_COMMIT=$(git rev-parse HEAD)

# Получаем обновления
git fetch origin main
NEW_COMMIT=$(git rev-parse origin/main)

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "Нет новых обновлений (HEAD: ${OLD_COMMIT:0:8})"
    exit 0
fi

echo "Обновление: ${OLD_COMMIT:0:8} → ${NEW_COMMIT:0:8}"
git pull origin main

# Обновляем зависимости
sudo -u "$APP_USER" /usr/local/bin/poetry install --no-root --only main

# Миграция БД: добавляем колонки кэш-токенов (если отсутствуют)
DB_FILE="$APP_DIR/cltg.db"
if [[ -f "$DB_FILE" ]]; then
    HAS_CACHE_COL=$(sqlite3 "$DB_FILE" "PRAGMA table_info(usage);" | grep -c 'cache_write_tokens' || true)
    if [[ "$HAS_CACHE_COL" -eq 0 ]]; then
        echo "Миграция БД: добавляю колонки cache_write_tokens, cache_read_tokens..."
        sqlite3 "$DB_FILE" "ALTER TABLE usage ADD COLUMN cache_write_tokens INTEGER DEFAULT 0;"
        sqlite3 "$DB_FILE" "ALTER TABLE usage ADD COLUMN cache_read_tokens INTEGER DEFAULT 0;"
        echo "Миграция завершена."
    fi
fi

# Перезапускаем сервис
systemctl restart "$SERVICE"
echo "Сервис $SERVICE перезапущен."

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Обновление завершено."
