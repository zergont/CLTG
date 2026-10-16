#!/usr/bin/env bash
# ============================================================
# CLTG — обновление бота до последней версии из GitHub (ветка main).
# Запускается вручную: sudo bash /opt/cltg/update_bot.sh
# (ночное автообновление убрано: на прод попадает только то, что выкатили осознанно)
# ============================================================
set -euo pipefail

APP_DIR="/opt/cltg"
APP_USER="cltg"
SERVICE="cltg-bot"
BRANCH="main"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите от root: sudo bash $APP_DIR/update_bot.sh" >&2
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Обновление CLTG..."
cd "$APP_DIR"

# Репозиторий принадлежит $APP_USER, а git запускается от root
git config --global --get-all safe.directory | grep -qx "$APP_DIR" \
    || git config --global --add safe.directory "$APP_DIR"

CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
    echo "Репозиторий на ветке '$CURRENT_BRANCH', а обновление идёт из '$BRANCH'." >&2
    echo "Сохраните изменения ветки и переключитесь: git checkout $BRANCH" >&2
    exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Есть незакоммиченные изменения — обновление остановлено:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

OLD_COMMIT=$(git rev-parse HEAD)
git fetch --quiet origin "$BRANCH"
NEW_COMMIT=$(git rev-parse "origin/$BRANCH")

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "Обновлений нет (HEAD: ${OLD_COMMIT:0:8})."
    exit 0
fi

echo "Обновление: ${OLD_COMMIT:0:8} → ${NEW_COMMIT:0:8}"
git log --oneline "$OLD_COMMIT..$NEW_COMMIT"
git merge --ff-only --quiet "origin/$BRANCH"

# Файлы бота принадлежат пользователю сервиса
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

sudo -u "$APP_USER" /usr/local/bin/poetry install --no-root --only main

systemctl restart "$SERVICE"
sleep 8
if systemctl is-active --quiet "$SERVICE"; then
    echo "Сервис $SERVICE перезапущен и работает."
else
    echo "⚠️  Сервис $SERVICE не запустился. Последние строки журнала:" >&2
    journalctl -u "$SERVICE" -n 30 --no-pager >&2
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Обновление завершено."
