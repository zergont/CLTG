from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bot.utils import backup, db


class FakeBot:
    def __init__(self):
        self.documents: list = []

    async def send_document(self, chat_id, document, caption=None):
        self.documents.append((chat_id, document.path, caption))


@pytest.fixture
async def backup_env(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    await db.init_db()
    await db.set_setting("current_model", "claude-sonnet-5-5")
    return tmp_path / "backups"


async def test_backup_is_a_valid_compressed_copy(backup_env, tmp_path):
    path = await backup.make_backup(keep=3)
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(path.read_bytes()))
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT value FROM bot_settings WHERE key = 'current_model'").fetchone() == (
            "claude-sonnet-5-5",
        )
    assert not list(backup_env.glob(".*"))  # временные файлы убраны


async def test_rotation_keeps_newest(backup_env):
    for day in range(5):
        (backup_env).mkdir(exist_ok=True)
        (backup_env / f"cltg-2026-10-0{day + 1}_010000.db.gz").write_bytes(b"old")
    newest = await backup.make_backup(keep=3)
    remaining = sorted(p.name for p in backup_env.glob("cltg-*.db.gz"))
    assert len(remaining) == 3 and remaining[-1] == newest.name


async def test_send_to_admin_weekly(backup_env, config, monkeypatch):
    monkeypatch.setattr(config.__class__, "backup_send_days", 7, raising=False)
    path = await backup.make_backup(keep=3)
    bot = FakeBot()

    assert await backup._send_due(config)  # ни разу не отправляли
    await backup.send_backup(bot, config.admin_id, path)
    assert bot.documents[0][0] == config.admin_id
    assert not await backup._send_due(config)

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    await db.set_setting(backup.LAST_SENT_KEY, week_ago)
    assert await backup._send_due(config)
