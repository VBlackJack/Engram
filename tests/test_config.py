# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Configuration and FileLogger tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from engram.config import ConfigError, load_config
from engram.logging_setup import FileLogger


def test_load_config_applies_environment_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        """
[database]
path = "base.db"
busy_timeout_ms = 5000

[ttl_days]
preference = 0
decision = 0
fact = 0
project_state = 30
episode = 7

[limits]
max_statement_chars = 2000
max_subject_keys = 8

[logging]
path = "logs/base.log"
file_level = "INFO"
console_level = "WARNING"

[server]
host = "127.0.0.1"
port = 8377
path = "/mcp"
write_wait_timeout_ms = 2000

[capsule]
default_token_budget = 600
min_token_budget = 150
max_token_budget = 1500
""".strip(),
        encoding="utf-8",
    )
    environment = {
        "ENGRAM_DATABASE_PATH": "override.db",
        "ENGRAM_DATABASE_BUSY_TIMEOUT_MS": "7500",
        "ENGRAM_TTL_DAYS_EPISODE": "3",
        "ENGRAM_LIMITS_MAX_SUBJECT_KEYS": "4",
        "ENGRAM_LOGGING_FILE_LEVEL": "debug",
        "ENGRAM_SERVER_PORT": "9000",
        "ENGRAM_SERVER_PATH": "/memory",
        "ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET": "700",
    }

    config = load_config(config_path, environ=environment)

    assert config.database.path == (tmp_path / "override.db").resolve()
    assert config.database.busy_timeout_ms == 7500
    assert config.ttl_days.episode == 3
    assert config.ttl_days.project_state == 30
    assert config.limits.max_subject_keys == 4
    assert config.logging.file_level == "DEBUG"
    assert config.logging.path == (tmp_path / "logs" / "base.log").resolve()
    assert config.server.port == 9000
    assert config.server.path == "/memory"
    assert config.server.write_wait_timeout_ms == 2000
    assert config.capsule.default_token_budget == 700


def test_load_config_rejects_invalid_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text("[limits]\nmax_statement_chars = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="max_statement_chars"):
        load_config(config_path, environ={})


def test_load_config_rejects_invalid_capsule_bounds(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        "[capsule]\nmin_token_budget = 500\nmax_token_budget = 400\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="at least the minimum"):
        load_config(config_path, environ={})


def test_load_config_uses_environment_selected_file(tmp_path: Path) -> None:
    config_path = tmp_path / "selected.toml"
    config_path.write_text("[database]\npath = 'selected.db'\n", encoding="utf-8")

    config = load_config(environ={"ENGRAM_CONFIG": str(config_path)})

    assert config.database.path == (tmp_path / "selected.db").resolve()


def test_file_logger_writes_file_and_configures_console(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        """
[logging]
path = "logs/engram.log"
file_level = "DEBUG"
console_level = "ERROR"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path, environ={})
    logger = FileLogger(config.logging, name="engram.test").configure()

    logger.info("Storage logger is ready")
    for handler in logger.handlers:
        handler.flush()

    assert config.logging.path.read_text(encoding="utf-8").endswith(
        "INFO engram.test: Storage logger is ready\n"
    )
    assert len(logger.handlers) == 2
    assert any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers)
