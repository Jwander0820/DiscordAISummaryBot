from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "discord_digest_bot"
LOG_PATH = Path("bot.log")


def configure_logging() -> logging.Logger:
    """Initialize shared logging once for both CLI helpers and the Discord bot."""
    logger = logging.getLogger(LOGGER_NAME)
    if getattr(configure_logging, "_configured", False):
        return logger

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s:%(levelname)s:%(name)s: %(message)s",
    )

    formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
    log_path = LOG_PATH.resolve()
    existing_file_handler = any(
        isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path
        for handler in logger.handlers
    )
    if not existing_file_handler:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    configure_logging._configured = True
    return logger
