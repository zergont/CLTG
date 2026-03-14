from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from bot.config import Config


def setup_logging(config: Config) -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    level = logging.DEBUG if config.debug_mode else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Консоль
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Файл с ротацией
    max_bytes = config.max_log_mb * 1024 * 1024
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "cltg.log",
        maxBytes=max_bytes,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Приглушаем шумные библиотеки
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
