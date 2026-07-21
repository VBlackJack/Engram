# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""File and console logging without print-based fallbacks."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import LoggingConfig

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class FileLogger:
    """Configure one logger with independent file and console levels."""

    def __init__(self, config: LoggingConfig, *, name: str = "engram") -> None:
        """Store the immutable logging configuration."""
        self._config = config
        self._name = name

    def configure(self) -> logging.Logger:
        """Create file and console handlers and return the configured logger."""
        self._config.path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(self._name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = False

        file_level = _log_level(self._config.file_level)
        console_level = _log_level(self._config.console_level)
        logger.setLevel(min(file_level, console_level))

        formatter = logging.Formatter(LOG_FORMAT)
        file_handler = logging.FileHandler(self._config.path, encoding="utf-8")
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        return logger

    @property
    def path(self) -> Path:
        """Return the configured log file path."""
        return self._config.path


def _log_level(name: str) -> int:
    level = logging.getLevelNamesMapping().get(name.upper())
    if level is None:
        raise ValueError(f"Unsupported log level: {name}")
    return level
