# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""TOML configuration loading with ENGRAM_ environment overrides."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import EntryKind

ENV_PREFIX = "ENGRAM_"
DEFAULT_CONFIG_PATH = Path("engram.toml")
SUPPORTED_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """SQLite connection settings."""

    path: Path = Path("engram.db")
    busy_timeout_ms: int = 5000


@dataclass(frozen=True, slots=True)
class TtlConfig:
    """Fixed entry lifetime in days, where zero disables expiration."""

    preference: int = 0
    decision: int = 0
    fact: int = 0
    project_state: int = 30
    episode: int = 7

    def for_kind(self, kind: EntryKind) -> int:
        """Return the configured lifetime for an entry kind."""
        values = {
            EntryKind.PREFERENCE: self.preference,
            EntryKind.DECISION: self.decision,
            EntryKind.FACT: self.fact,
            EntryKind.PROJECT_STATE: self.project_state,
            EntryKind.EPISODE: self.episode,
        }
        return values[kind]


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    """Input limits enforced before storage."""

    max_statement_chars: int = 2000
    max_subject_keys: int = 8


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """File and console logging settings."""

    path: Path = Path("logs/engram.log")
    file_level: str = "INFO"
    console_level: str = "INFO"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete Engram application configuration."""

    database: DatabaseConfig
    ttl_days: TtlConfig
    limits: LimitsConfig
    logging: LoggingConfig


DEFAULT_DATABASE_CONFIG = DatabaseConfig()
DEFAULT_TTL_CONFIG = TtlConfig()
DEFAULT_LIMITS_CONFIG = LimitsConfig()
DEFAULT_LOGGING_CONFIG = LoggingConfig()


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load a TOML file and apply explicit ENGRAM_ environment overrides."""
    environment = os.environ if environ is None else environ
    selected_path: str | Path
    if path is not None:
        selected_path = path
    else:
        selected_path = environment.get(f"{ENV_PREFIX}CONFIG", str(DEFAULT_CONFIG_PATH))
    config_path = Path(selected_path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {config_path}")

    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML configuration: {exc}") from exc

    database = _section(raw, "database")
    ttl_days = _section(raw, "ttl_days")
    limits = _section(raw, "limits")
    logging_config = _section(raw, "logging")
    base_directory = config_path.parent

    result = AppConfig(
        database=DatabaseConfig(
            path=_resolve_path(
                base_directory,
                _string_value(database, "path", environment, DEFAULT_DATABASE_CONFIG.path),
            ),
            busy_timeout_ms=_integer_value(
                database,
                "busy_timeout_ms",
                environment,
                DEFAULT_DATABASE_CONFIG.busy_timeout_ms,
            ),
        ),
        ttl_days=TtlConfig(
            preference=_integer_value(
                ttl_days, "preference", environment, DEFAULT_TTL_CONFIG.preference
            ),
            decision=_integer_value(ttl_days, "decision", environment, DEFAULT_TTL_CONFIG.decision),
            fact=_integer_value(ttl_days, "fact", environment, DEFAULT_TTL_CONFIG.fact),
            project_state=_integer_value(
                ttl_days,
                "project_state",
                environment,
                DEFAULT_TTL_CONFIG.project_state,
            ),
            episode=_integer_value(ttl_days, "episode", environment, DEFAULT_TTL_CONFIG.episode),
        ),
        limits=LimitsConfig(
            max_statement_chars=_integer_value(
                limits,
                "max_statement_chars",
                environment,
                DEFAULT_LIMITS_CONFIG.max_statement_chars,
            ),
            max_subject_keys=_integer_value(
                limits,
                "max_subject_keys",
                environment,
                DEFAULT_LIMITS_CONFIG.max_subject_keys,
            ),
        ),
        logging=LoggingConfig(
            path=_resolve_path(
                base_directory,
                _string_value(logging_config, "path", environment, DEFAULT_LOGGING_CONFIG.path),
            ),
            file_level=_string_value(
                logging_config,
                "file_level",
                environment,
                DEFAULT_LOGGING_CONFIG.file_level,
            ).upper(),
            console_level=_string_value(
                logging_config,
                "console_level",
                environment,
                DEFAULT_LOGGING_CONFIG.console_level,
            ).upper(),
        ),
    )
    _validate_config(result)
    return result


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration section must be a table: {name}")
    result = dict(value)
    result["__engram_section_name__"] = name
    return result


def _environment_key(section: str, key: str) -> str:
    return f"{ENV_PREFIX}{section.upper()}_{key.upper()}"


def _raw_value(
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: object,
) -> object:
    section_name = _find_section_name(section)
    override = environment.get(_environment_key(section_name, key))
    if override is not None:
        return override
    return section.get(key, default)


def _find_section_name(section: dict[str, Any]) -> str:
    marker = section.get("__engram_section_name__")
    if isinstance(marker, str):
        return marker
    raise ConfigError("Internal configuration section marker is missing")


def _integer_value(
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: int,
) -> int:
    value = _raw_value(section, key, environment, default)
    if isinstance(value, bool):
        raise ConfigError(f"Configuration value must be an integer: {key}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"Configuration value must be an integer: {key}") from exc
    raise ConfigError(f"Configuration value must be an integer: {key}")


def _string_value(
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: str | Path,
) -> str:
    value = _raw_value(section, key, environment, str(default))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Configuration value must be a non-empty string: {key}")
    return value.strip()


def _resolve_path(base_directory: Path, configured_path: str) -> Path:
    value = Path(configured_path).expanduser()
    if not value.is_absolute():
        value = base_directory / value
    return value.resolve()


def _validate_config(config: AppConfig) -> None:
    if config.database.busy_timeout_ms <= 0:
        raise ConfigError("database.busy_timeout_ms must be greater than zero")
    ttl_values = (
        config.ttl_days.preference,
        config.ttl_days.decision,
        config.ttl_days.fact,
        config.ttl_days.project_state,
        config.ttl_days.episode,
    )
    if any(value < 0 for value in ttl_values):
        raise ConfigError("ttl_days values must be zero or greater")
    if config.limits.max_statement_chars <= 0:
        raise ConfigError("limits.max_statement_chars must be greater than zero")
    if config.limits.max_subject_keys <= 0:
        raise ConfigError("limits.max_subject_keys must be greater than zero")
    if config.logging.file_level not in SUPPORTED_LOG_LEVELS:
        raise ConfigError(f"Unsupported file log level: {config.logging.file_level}")
    if config.logging.console_level not in SUPPORTED_LOG_LEVELS:
        raise ConfigError(f"Unsupported console log level: {config.logging.console_level}")
