"""Application logging helpers."""

from __future__ import annotations

import logging
import os

from .app_config import altheapath, log_path


logger = logging.getLogger("althea")


def setup_logging() -> None:
    os.makedirs(altheapath, exist_ok=True)

    # Avoid duplicate handlers if main() is called again.
    if getattr(setup_logging, "_configured", False):
        return

    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path(), encoding="utf-8")
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    setup_logging._configured = True


def log_info(message: str) -> None:
    try:
        logger.info(message)
    except Exception:
        pass


def log_exception(message: str) -> None:
    try:
        logger.exception(message)
    except Exception:
        pass
