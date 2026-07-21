# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""TOML configuration loading with ENGRAM_ environment overrides."""

from __future__ import annotations

import os
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import EntryKind

ENV_PREFIX = "ENGRAM_"
DEFAULT_CONFIG_PATH = Path("engram.toml")
SUPPORTED_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
MAX_NETWORK_PORT = 65_535


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
class ServerConfig:
    """Streamable HTTP server and write backpressure settings."""

    host: str = "127.0.0.1"
    port: int = 8377
    path: str = "/mcp"
    write_wait_timeout_ms: int = 2000


@dataclass(frozen=True, slots=True)
class CapsuleConfig:
    """Recall capsule token budget settings."""

    default_token_budget: int = 600
    min_token_budget: int = 150
    max_token_budget: int = 1500


class RetrievalMode(StrEnum):
    """Supported retrieval implementations."""

    FTS = "fts"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Lexical and optional remote embedding retrieval settings."""

    mode: RetrievalMode = RetrievalMode.FTS
    embeddings_endpoint: str = "http://127.0.0.1:1234/v1/embeddings"
    embeddings_model: str = ""
    embeddings_timeout_ms: int = 3000
    rrf_k: int = 60


@dataclass(frozen=True, slots=True)
class DatacronConfig:
    """Datacron stdio transport and vault confinement settings."""

    command: str = "datacron-mcp"
    args: tuple[str, ...] = ()
    vault_root: Path | None = None
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    new_note_directory: str = "_memory/engram"
    neighbor_limit: int = 8


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete Engram application configuration."""

    database: DatabaseConfig
    ttl_days: TtlConfig
    limits: LimitsConfig
    logging: LoggingConfig
    server: ServerConfig = field(default_factory=ServerConfig)
    capsule: CapsuleConfig = field(default_factory=CapsuleConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    datacron: DatacronConfig = field(default_factory=DatacronConfig)


DEFAULT_DATABASE_CONFIG = DatabaseConfig()
DEFAULT_TTL_CONFIG = TtlConfig()
DEFAULT_LIMITS_CONFIG = LimitsConfig()
DEFAULT_LOGGING_CONFIG = LoggingConfig()
DEFAULT_SERVER_CONFIG = ServerConfig()
DEFAULT_CAPSULE_CONFIG = CapsuleConfig()
DEFAULT_RETRIEVAL_CONFIG = RetrievalConfig()
DEFAULT_DATACRON_CONFIG = DatacronConfig()


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
    server = _section(raw, "server")
    capsule = _section(raw, "capsule")
    retrieval = _section(raw, "retrieval")
    datacron = _section(raw, "datacron")
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
        server=ServerConfig(
            host=_string_value(server, "host", environment, DEFAULT_SERVER_CONFIG.host),
            port=_integer_value(server, "port", environment, DEFAULT_SERVER_CONFIG.port),
            path=_string_value(server, "path", environment, DEFAULT_SERVER_CONFIG.path),
            write_wait_timeout_ms=_integer_value(
                server,
                "write_wait_timeout_ms",
                environment,
                DEFAULT_SERVER_CONFIG.write_wait_timeout_ms,
            ),
        ),
        capsule=CapsuleConfig(
            default_token_budget=_integer_value(
                capsule,
                "default_token_budget",
                environment,
                DEFAULT_CAPSULE_CONFIG.default_token_budget,
            ),
            min_token_budget=_integer_value(
                capsule,
                "min_token_budget",
                environment,
                DEFAULT_CAPSULE_CONFIG.min_token_budget,
            ),
            max_token_budget=_integer_value(
                capsule,
                "max_token_budget",
                environment,
                DEFAULT_CAPSULE_CONFIG.max_token_budget,
            ),
        ),
        retrieval=RetrievalConfig(
            mode=_retrieval_mode(
                _string_value(retrieval, "mode", environment, DEFAULT_RETRIEVAL_CONFIG.mode)
            ),
            embeddings_endpoint=_string_value(
                retrieval,
                "embeddings_endpoint",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.embeddings_endpoint,
            ),
            embeddings_model=_optional_string_value(
                retrieval,
                "embeddings_model",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.embeddings_model,
            ),
            embeddings_timeout_ms=_integer_value(
                retrieval,
                "embeddings_timeout_ms",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.embeddings_timeout_ms,
            ),
            rrf_k=_integer_value(
                retrieval,
                "rrf_k",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.rrf_k,
            ),
        ),
        datacron=DatacronConfig(
            command=_string_value(
                datacron,
                "command",
                environment,
                DEFAULT_DATACRON_CONFIG.command,
            ),
            args=_string_tuple_value(
                datacron,
                "args",
                environment,
                DEFAULT_DATACRON_CONFIG.args,
            ),
            vault_root=_optional_path_value(
                base_directory,
                datacron,
                "vault_root",
                environment,
                DEFAULT_DATACRON_CONFIG.vault_root,
            ),
            read_paths=_path_tuple_value(
                base_directory,
                datacron,
                "read_paths",
                environment,
                DEFAULT_DATACRON_CONFIG.read_paths,
            ),
            write_paths=_path_tuple_value(
                base_directory,
                datacron,
                "write_paths",
                environment,
                DEFAULT_DATACRON_CONFIG.write_paths,
            ),
            new_note_directory=_string_value(
                datacron,
                "new_note_directory",
                environment,
                DEFAULT_DATACRON_CONFIG.new_note_directory,
            ),
            neighbor_limit=_integer_value(
                datacron,
                "neighbor_limit",
                environment,
                DEFAULT_DATACRON_CONFIG.neighbor_limit,
            ),
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


def _optional_string_value(
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: str,
) -> str:
    value = _raw_value(section, key, environment, default)
    if not isinstance(value, str):
        raise ConfigError(f"Configuration value must be a string: {key}")
    return value.strip()


def _string_tuple_value(
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: tuple[str, ...],
) -> tuple[str, ...]:
    value = _raw_value(section, key, environment, list(default))
    if isinstance(value, str):
        return tuple(shlex.split(value, posix=os.name != "nt"))
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return tuple(item.strip() for item in value)
    raise ConfigError(f"Configuration value must be a string array: {key}")


def _optional_path_value(
    base_directory: Path,
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: Path | None,
) -> Path | None:
    value = _raw_value(section, key, environment, "" if default is None else str(default))
    if not isinstance(value, str):
        raise ConfigError(f"Configuration value must be a path string: {key}")
    return None if not value.strip() else _resolve_path(base_directory, value.strip())


def _path_tuple_value(
    base_directory: Path,
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: tuple[Path, ...],
) -> tuple[Path, ...]:
    value = _raw_value(section, key, environment, [str(path) for path in default])
    if isinstance(value, str):
        parts = tuple(part.strip() for part in value.split(os.pathsep) if part.strip())
    elif isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        parts = tuple(item.strip() for item in value)
    else:
        raise ConfigError(f"Configuration value must be a path array: {key}")
    return tuple(_resolve_path(base_directory, part) for part in parts)


def _retrieval_mode(value: str) -> RetrievalMode:
    try:
        return RetrievalMode(value.casefold())
    except ValueError as exc:
        raise ConfigError(f"Unsupported retrieval mode: {value}") from exc


def _resolve_path(base_directory: Path, configured_path: str) -> Path:
    value = Path(configured_path).expanduser()
    if not value.is_absolute():
        value = base_directory / value
    return value.resolve()


def _validate_config(config: AppConfig) -> None:
    _validate_storage_config(config)
    _validate_server_config(config)
    _validate_retrieval_config(config)
    _validate_datacron_config(config)


def _validate_storage_config(config: AppConfig) -> None:
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


def _validate_server_config(config: AppConfig) -> None:
    if not 1 <= config.server.port <= MAX_NETWORK_PORT:
        raise ConfigError("server.port must be between 1 and 65535")
    if not config.server.path.startswith("/"):
        raise ConfigError("server.path must start with a slash")
    if config.server.write_wait_timeout_ms <= 0:
        raise ConfigError("server.write_wait_timeout_ms must be greater than zero")
    if config.capsule.min_token_budget <= 0:
        raise ConfigError("capsule.min_token_budget must be greater than zero")
    if config.capsule.max_token_budget < config.capsule.min_token_budget:
        raise ConfigError("capsule.max_token_budget must be at least the minimum")
    if not (
        config.capsule.min_token_budget
        <= config.capsule.default_token_budget
        <= config.capsule.max_token_budget
    ):
        raise ConfigError("capsule.default_token_budget must be within the configured bounds")


def _validate_retrieval_config(config: AppConfig) -> None:
    endpoint = urlparse(config.retrieval.embeddings_endpoint)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ConfigError("retrieval.embeddings_endpoint must be an HTTP URL")
    if config.retrieval.embeddings_timeout_ms <= 0:
        raise ConfigError("retrieval.embeddings_timeout_ms must be greater than zero")
    if config.retrieval.rrf_k <= 0:
        raise ConfigError("retrieval.rrf_k must be greater than zero")
    if config.retrieval.mode is RetrievalMode.HYBRID and not config.retrieval.embeddings_model:
        raise ConfigError("retrieval.embeddings_model is required in hybrid mode")


def _validate_datacron_config(config: AppConfig) -> None:
    if config.datacron.neighbor_limit <= 0:
        raise ConfigError("datacron.neighbor_limit must be greater than zero")
    target = Path(config.datacron.new_note_directory)
    if (
        target.is_absolute()
        or ".." in target.parts
        or not target.parts
        or target.parts[0] != "_memory"
    ):
        raise ConfigError("datacron.new_note_directory must be confined inside _memory")
