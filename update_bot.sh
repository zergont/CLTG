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

# Перезапускаем сервис (миграции БД выполняются автоматически при старте бота)
systemctl restart "$SERVICE"
echo "Сервис $SERVICE перезапущен."

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Обновление завершено."
