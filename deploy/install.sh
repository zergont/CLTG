#!/usr/bin/env bash
# ============================================================
# CLTG — первичная установка на сервере Ubuntu 24.04
# Запуск: sudo bash deploy/install.sh
# ============================================================
set -euo pipefail

APP_DIR="/opt/cltg"
APP_USER="cltg"
PYTHON_MIN="3.11"

echo "=== CLTG Install Script ==="

# ──────────────────────────────────────────
# 1. Проверка root
# ──────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Запустите скрипт с sudo: sudo bash deploy/install.sh" >&2
    exit 1
fi

# ──────────────────────────────────────────
# 2. Системные зависимости
# ──────────────────────────────────────────
echo "[1/8] Установка системных зависимостей..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# ──────────────────────────────────────────
# 3. Установка Poetry
# ──────────────────────────────────────────
echo "[2/8] Установка Poetry..."
if ! command -v poetry &>/dev/null; then
    curl -sSL https://install.python-poetry.org | python3 -
    export PATH="$HOME/.local/bin:$PATH"
fi
poetry --version

# ──────────────────────────────────────────
# 4. Системный пользователь
# ──────────────────────────────────────────
echo "[3/8] Создание пользователя $APP_USER..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --home "$APP_DIR" --create-home "$APP_USER"
fi

# ──────────────────────────────────────────
# 5. Копирование файлов (если запускается не из APP_DIR)
# ──────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "[4/8] Копирование файлов в $APP_DIR..."
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
    rsync -a --exclude='.git' --exclude='.venv' --exclude='*.db' --exclude='*.log' \
        "$REPO_DIR/" "$APP_DIR/"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ──────────────────────────────────────────
# 6. Виртуальное окружение и зависимости
# ──────────────────────────────────────────
echo "[5/8] Установка Python-зависимостей через Poetry..."
cd "$APP_DIR"
sudo -u "$APP_USER" poetry config virtualenvs.in-project true
sudo -u "$APP_USER" poetry install --no-root --only main

# ──────────────────────────────────────────
# 7. Конфигурация
# ──────────────────────────────────────────
echo "[6/8] Проверка конфигурации..."
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo ""
    echo "⚠️  ВНИМАНИЕ: Заполните $APP_DIR/.env перед запуском!"
    echo "   nano $APP_DIR/.env"
    echo ""
fi

# ──────────────────────────────────────────
# 8. Инициализация БД
# ──────────────────────────────────────────
echo "[7/8] Инициализация базы данных..."
cd "$APP_DIR"
sudo -u "$APP_USER" .venv/bin/python -c "
import asyncio
import sys
sys.path.insert(0, '.')
from bot.utils.db import init_db
asyncio.run(init_db())
print('БД инициализирована.')
" 2>/dev/null || echo "БД будет инициализирована при первом запуске."

# ──────────────────────────────────────────
# 9. Systemd сервисы
# ──────────────────────────────────────────
echo "[8/8] Регистрация systemd-сервисов..."
cp "$APP_DIR/deploy/systemd/cltg-bot.service"    /etc/systemd/system/
cp "$APP_DIR/deploy/systemd/cltg-update.service" /etc/systemd/system/
cp "$APP_DIR/deploy/systemd/cltg-update.timer"   /etc/systemd/system/

systemctl daemon-reload
systemctl enable cltg-bot.service
systemctl enable cltg-update.timer
systemctl start cltg-update.timer

echo ""
echo "✅ Установка завершена!"
echo ""
echo "Следующие шаги:"
echo "  1. Заполните конфиг: nano $APP_DIR/.env"
echo "  2. Запустите бота:   sudo systemctl start cltg-bot"
echo "  3. Логи:             sudo journalctl -u cltg-bot -f"
echo ""
