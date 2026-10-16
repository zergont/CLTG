from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile

from bot.utils import db

if TYPE_CHECKING:
    from aiogram import Bot
    from bot.config import Config

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("backups")
BACKUP_GLOB = "cltg-*.db.gz"
# Как часто планировщик проверяет, не пора ли делать копию
CHECK_INTERVAL_SECONDS = 600
# Когда последняя копия отправлялась админу в Telegram (bot_settings)
LAST_SENT_KEY = "backup_last_sent"


def _make_backup_sync(keep: int) -> Path:
    """Онлайн-копия SQLite (не мешает работающему боту), сжатие gzip, ротация."""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    raw = BACKUP_DIR / f".cltg-{stamp}.db"
    partial = BACKUP_DIR / f".cltg-{stamp}.db.gz.part"
    final = BACKUP_DIR / f"cltg-{stamp}.db.gz"

    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        with open(raw, "rb") as f_in, gzip.open(partial, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.replace(partial, final)
    finally:
        raw.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)

    for old in sorted(BACKUP_DIR.glob(BACKUP_GLOB))[:-keep or None]:
        old.unlink()
    return final


async def make_backup(keep: int) -> Path:
    path = await asyncio.to_thread(_make_backup_sync, keep)
    logger.info("Резервная копия базы: %s (%.1f МБ)", path, path.stat().st_size / 1024 / 1024)
    return path


def _has_backup_for(day: date) -> bool:
    return any(BACKUP_DIR.glob(f"cltg-{day.isoformat()}_*.db.gz"))


async def send_backup(bot: "Bot", admin_id: int, path: Path) -> None:
    size_mb = path.stat().st_size / 1024 / 1024
    await bot.send_document(
        admin_id,
        FSInputFile(path),
        caption=(
            f"💾 Резервная копия базы бота ({size_mb:.1f} МБ).\n"
            "Здесь вся память бота: история, факты о пользователях, напоминания. "
            "Сохраните в надёжном месте."
        ),
    )
    await db.set_setting(LAST_SENT_KEY, datetime.now(timezone.utc).date().isoformat())


async def _send_due(config: "Config") -> bool:
    if config.backup_send_days <= 0:
        return False
    last = await db.get_setting(LAST_SENT_KEY)
    if not last:
        return True
    today = datetime.now(timezone.utc).date()
    return (today - date.fromisoformat(last)).days >= config.backup_send_days


async def run_backup_scheduler(bot: "Bot", config: "Config") -> None:
    """Раз в сутки (после BACKUP_HOUR по UTC) делает копию; раз в N дней шлёт её админу."""
    logger.info(
        "Резервное копирование: ежедневно после %02d:00 UTC, хранить %d, админу раз в %d дн.",
        config.backup_hour, config.backup_keep, config.backup_send_days,
    )
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour >= config.backup_hour and not _has_backup_for(now.date()):
                path = await make_backup(config.backup_keep)
                if await _send_due(config):
                    await send_backup(bot, config.admin_id, path)
        except Exception:
            logger.exception("Ошибка резервного копирования")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
