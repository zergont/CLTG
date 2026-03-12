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
echo "[1/10] Установка системных зависимостей..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl openssl rsync

# ──────────────────────────────────────────
# Docker
# ──────────────────────────────────────────
echo "[2/10] Установка Docker..."
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi
systemctl enable docker
systemctl start docker
docker --version

# ──────────────────────────────────────────
# 3. Установка Poetry
# ──────────────────────────────────────────
echo "[3/10] Установка Poetry..."
if ! command -v poetry &>/dev/null; then
    curl -sSL https://install.python-poetry.org | POETRY_HOME=/usr/local python3 -
fi
poetry --version

# ──────────────────────────────────────────
# 4. Системный пользователь
# ──────────────────────────────────────────
echo "[4/10] Создание пользователя $APP_USER..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --home "$APP_DIR" --create-home "$APP_USER"
fi

# ──────────────────────────────────────────
# 5. Копирование файлов (если запускается не из APP_DIR)
# ──────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "[5/10] Копирование файлов в $APP_DIR..."
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
    rsync -a --exclude='.git' --exclude='.venv' --exclude='*.db' --exclude='*.log' \
        "$REPO_DIR/" "$APP_DIR/"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ──────────────────────────────────────────
# 6. Виртуальное окружение и зависимости
# ──────────────────────────────────────────
echo "[6/10] Установка Python-зависимостей через Poetry..."
POETRY_BIN="/usr/local/bin/poetry"
cd "$APP_DIR"
sudo -u "$APP_USER" "$POETRY_BIN" config virtualenvs.in-project true
sudo -u "$APP_USER" "$POETRY_BIN" install --no-root --only main

# ──────────────────────────────────────────
# 7. SearXNG
# ──────────────────────────────────────────
echo "[7/10] Запуск SearXNG (веб-поиск)..."
SEARXNG_DIR="$APP_DIR/searxng"
mkdir -p "$SEARXNG_DIR"
cp "$APP_DIR/deploy/searxng/settings.yml" "$SEARXNG_DIR/settings.yml"
SECRET_KEY=$(openssl rand -hex 32)
sed -i "s/REPLACE_WITH_GENERATED_KEY/$SECRET_KEY/" "$SEARXNG_DIR/settings.yml"
chown -R "$APP_USER:$APP_USER" "$SEARXNG_DIR"

if docker ps -a --format '{{.Names}}' | grep -q '^searxng$'; then
    echo "   Контейнер searxng уже существует, перезапускаем..."
    docker stop searxng && docker rm searxng
fi

docker run -d \
    --name searxng \
    --restart unless-stopped \
    -p 127.0.0.1:8888:8080 \
    -v "$SEARXNG_DIR:/etc/searxng:rw" \
    searxng/searxng:latest

echo "   SearXNG запущен на http://127.0.0.1:8888"

# ──────────────────────────────────────────
# 8. Конфигурация
# ──────────────────────────────────────────
echo "[8/10] Проверка конфигурации..."
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
# 9. БД
# ──────────────────────────────────────────
echo "[9/10] Инициализация базы данных..."
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
# 10. Systemd сервисы
# ──────────────────────────────────────────
echo "[10/10] Регистрация systemd-сервисов..."
cp "$APP_DIR/deploy/systemd/cltg-bot.service"    /etc/systemd/system/
cp "$APP_DIR/deploy/systemd/cltg-update.service" /etc/systemd/system/
cp "$APP_DIR/deploy/systemd/cltg-update.timer"   /etc/systemd/system/

systemctl daemon-reload

# Включить автозапуск
systemctl enable cltg-bot
systemctl enable cltg-update.timer

# Запустить, только если .env уже заполнен (есть реальный токен)
if grep -q "^BOT_TOKEN=.\+" "$APP_DIR/.env" 2>/dev/null; then
    systemctl start cltg-bot
    systemctl start cltg-update.timer
    echo ""
    echo "✅ Бот запущен как systemd-служба."
    echo "   Статус:  sudo systemctl status cltg-bot"
    echo "   Логи:    sudo journalctl -u cltg-bot -f"
else
    echo ""
    echo "⚠️  Заполните конфигурацию и запустите бота вручную:"
    echo "   sudo nano $APP_DIR/.env"
    echo "   sudo systemctl start cltg-bot"
fi

echo ""
echo "=== Установка завершена ==="
echo "Автозапуск при перезагрузке сервера: ВКЛЮЧЁН (cltg-bot, cltg-update.timer)"

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
