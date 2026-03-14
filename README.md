# CLTG — Семейный Telegram-бот на базе Claude API

Telegram-бот с поддержкой текста, изображений, документов, напоминаний и учётом токенов.
Работает на [Anthropic Claude API](https://www.anthropic.com/).

---

## Возможности

- 💬 **Диалог с Claude** — стриминг ответов в реальном времени
- 🔍 **Веб-поиск** — встроенный через Claude tool use
- 🖼 **Изображения** — Claude Vision (base64 → нативно)
- 📄 **Документы и PDF** — нативная поддержка Claude
- 🔔 **Напоминания** — одноразовые и цепочки, с генерацией ответа через Claude
- 📊 **Статистика** — учёт токенов и расходов по каждому пользователю
- 🧠 **Управление контекстом** — автосаммаризация по токенам (85%) и по времени (72ч)
- 🌍 **Часовой пояс** — автоопределение из контекста диалога
- 🚫 **Бан-лист** — блокировка пользователей администратором
- 🤖 **Две модели** — Claude Haiku 4.5 (дефолт) и Claude Sonnet 4.6

---

## Структура проекта

```
cltg/
├── bot/
│   ├── handlers/
│   │   ├── _common.py      # общая логика стриминга и истории
│   │   ├── admin.py        # команды администратора
│   │   ├── document.py     # обработка документов/PDF
│   │   ├── photo.py        # обработка изображений
│   │   └── text.py         # текстовые сообщения и напоминания
│   ├── utils/
│   │   ├── anthropic/
│   │   │   ├── chat.py     # стриминг, саммаризация, сборка messages[]
│   │   │   └── models.py   # константы моделей и лимитов контекста
│   │   ├── db.py           # все операции с SQLite
│   │   ├── errors.py       # обработка ошибок API, ретраи
│   │   ├── html.py         # Markdown→HTML, разбивка длинных сообщений
│   │   ├── log.py          # настройка логирования с ротацией
│   │   ├── prompts.py      # промпты для Claude (напоминания, timezone)
│   │   └── reminders.py    # планировщик напоминаний
│   ├── config.py           # загрузка конфигурации из .env
│   ├── keyboards.py        # команды меню BotFather
│   ├── main.py             # точка входа
│   └── middlewares.py      # регистрация и проверка бана
├── deploy/
│   ├── install.sh          # скрипт первичной установки
│   ├── searxng/
│   │   └── settings.yml    # конфиг SearXNG (веб-поиск)
│   └── systemd/
│       ├── cltg-bot.service
│       ├── cltg-update.service
│       └── cltg-update.timer
├── .env.example            # шаблон конфигурации
├── .gitignore
├── pyproject.toml
├── schema.sql              # схема БД
└── update_bot.sh           # скрипт обновления
```

---

## Требования

- Python 3.11+
- [Poetry](https://python-poetry.org/)
- Токен Telegram-бота ([@BotFather](https://t.me/BotFather))
- API-ключ Anthropic ([console.anthropic.com](https://console.anthropic.com/))

---

## Разработка (Windows / локально)

### 1. Клонирование

```bash
git clone https://github.com/zergont/CLTG.git
cd CLTG
```

### 2. Установка зависимостей

```bash
poetry install
```

### 3. Конфигурация

```bash
copy .env.example .env
# Откройте .env и заполните BOT_TOKEN, ADMIN_ID, ANTHROPIC_API_KEY
```

### 4. Запуск

```bash
poetry run python -m bot.main
```

---

## Деплой на Ubuntu 24.04

### Первичная установка

#### 1. Клонируйте репозиторий на сервер

```bash
git clone https://github.com/zergont/CLTG.git /opt/cltg
cd /opt/cltg
```

#### 2. Заполните конфигурацию

```bash
sudo cp /opt/cltg/.env.example /opt/cltg/.env
sudo nano /opt/cltg/.env
```

Обязательно заполните:
```env
BOT_TOKEN=ваш_токен_от_botfather
ADMIN_ID=ваш_telegram_id
ANTHROPIC_API_KEY=ваш_ключ_anthropic
```

#### 3. Запустите скрипт установки

```bash
sudo bash deploy/install.sh
```

Скрипт автоматически:
- Установит системные зависимости (python3, git, curl, rsync)
- Установит Docker и запустит его как службу
- Установит Poetry глобально (`/usr/local/bin/poetry`)
- Создаст системного пользователя `cltg`
- Скопирует файлы в `/opt/cltg` и настроит права
- Установит Python-зависимости через Poetry
- Запустит **SearXNG** в Docker на `127.0.0.1:8888` (бесплатный веб-поиск)
- Инициализирует базу данных
- Зарегистрирует systemd-сервисы и **включит автозапуск** при старте системы
- Запустит бота (если `.env` уже заполнен)

#### 4. Проверьте статус

```bash
sudo systemctl status cltg-bot
```

#### 5. Проверьте логи

```bash
sudo journalctl -u cltg-bot -f
```

---

### Управление сервисом

| Действие | Команда |
|---|---|
| Запустить | `sudo systemctl start cltg-bot` |
| Остановить | `sudo systemctl stop cltg-bot` |
| Перезапустить | `sudo systemctl restart cltg-bot` |
| Статус | `sudo systemctl status cltg-bot` |
| Логи (live) | `sudo journalctl -u cltg-bot -f` |
| Логи за сегодня | `sudo journalctl -u cltg-bot --since today` |
| Отключить автозапуск | `sudo systemctl disable cltg-bot` |
| Включить автозапуск | `sudo systemctl enable cltg-bot` |

---

### Ручное обновление

```bash
cd /opt/cltg
sudo bash update_bot.sh
```

### Автообновление (systemd timer)

Таймер `cltg-update.timer` автоматически запускает `update_bot.sh` каждую ночь в 03:00.

```bash
# Статус таймера
sudo systemctl status cltg-update.timer

# Список активных таймеров
sudo systemctl list-timers | grep cltg

# Принудительный запуск обновления
sudo systemctl start cltg-update.service
```

Для изменения расписания отредактируйте `/etc/systemd/system/cltg-update.timer`:
```ini
[Timer]
OnCalendar=*-*-* 03:00:00   # каждую ночь в 03:00
```

После изменения:
```bash
sudo systemctl daemon-reload
sudo systemctl restart cltg-update.timer
```

---

### Удаление сервиса

```bash
# 1. Останавливаем и отключаем
sudo systemctl stop cltg-bot cltg-update.timer cltg-update.service
sudo systemctl disable cltg-bot cltg-update.timer cltg-update.service

# 2. Удаляем systemd-файлы
sudo rm -f /etc/systemd/system/cltg-bot.service
sudo rm -f /etc/systemd/system/cltg-update.service
sudo rm -f /etc/systemd/system/cltg-update.timer
sudo systemctl daemon-reload
sudo systemctl reset-failed

# 3. Удаляем пользователя
sudo userdel -r cltg 2>/dev/null || true

# 4. Удаляем файлы (осторожно: удалит БД и логи!)
sudo rm -rf /opt/cltg
```

> ⚠️ **Резервная копия базы данных** перед удалением:
> ```bash
> sudo cp /opt/cltg/cltg.db ~/cltg_backup.db
> ```

---

## Команды бота

### Для всех пользователей

| Команда | Описание |
|---|---|
| `/start` | Приветствие и регистрация |
| `/help` | Список команд |
| `/reset` | Сбросить контекст диалога |
| `/stats` | Статистика токенов и расходов |
| `/reminders` | Список активных напоминаний (с кнопками удаления) |

### Только для администратора

| Команда | Описание |
|---|---|
| `/model` | Сменить активную модель Claude |
| `/ban <user_id>` | Заблокировать пользователя |
| `/unban <user_id>` | Разблокировать пользователя |
| `/users` | Список всех пользователей |
| `/usage` | Общая статистика расходов |
| `/context` | Заполнение контекстных окон по пользователям |

---

## Напоминания

Создаются естественным языком — Claude сам извлекает параметры:

```
напомни через 30 минут купить хлеб
каждый понедельник в 9 утра составить план недели, 4 раза
каждый день в 22:00 пора спать, тихо
```

Поддерживаются:
- **Одноразовые** и **цепочки** (повторяющиеся)
- **Ограничения** по количеству срабатываний (`steps_left`) и дате (`end_at`)
- **Тихий режим** — без звука и вибрации (`silent=1`)
- **Генерация через Claude** — вместо фиксированного текста бот вызывает модель по заданному промпту

Управление: `/reminders` — список с кнопками удаления.

---

## Управление контекстом

Каждый запрос к Claude строится из трёх уровней:

```
[system: основной промпт + текущее время]
[user: SUMMARY: накопленное саммари]    ← если есть
[assistant: Понял, продолжаем.]         ← если есть
[... живые пары сообщений ...]
[user: новое сообщение]
```

**Триггеры саммаризации:**
- **85% контекстного окна** — сворачивает всё кроме последних `SUMMARY_KEEP_LAST=10` пар
- **72 часа тишины** — «финальное» саммари с акцентом на итоги сессии

---

## Конфигурация (.env)

Все параметры описаны в `.env.example` с комментариями на русском.

Ключевые параметры:

```env
BOT_TOKEN=              # токен от BotFather
ADMIN_ID=               # ваш Telegram ID
ANTHROPIC_API_KEY=      # ключ Anthropic

SYSTEM_PROMPT=          # системный промпт
DEFAULT_TIMEZONE=Europe/Moscow

SUMMARY_TRIGGER_TOKENS=0.85   # порог токенов для саммаризации
SUMMARY_TRIGGER_HOURS=72      # часов тишины для саммаризации
SUMMARY_KEEP_LAST=10          # последних пар не сжимать

MAX_FILE_MB=20          # максимальный размер файла
DEBUG_MODE=0            # 1 для отладки
```

---

## Лицензия

MIT
