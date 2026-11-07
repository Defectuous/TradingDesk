"""Logging: loguru to the console and to a daily file in logs/ (kept 30 days)."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(
        log_dir / "tradingdesk_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )
