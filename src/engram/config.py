# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""TOML configuration loading with ENGRAM_ environment overrides."""

from __future__ import annotations

import math
import os
import shlex
import tomllib
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from dataclasses import fields as dataclass_fields
from difflib import get_close_matches
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import EntryKind
from .normalization import (
    HARD_MAX_STATEMENT_CHARS,
    HARD_MAX_SUBJECT_KEYS,
    MAX_EMBEDDING_MODEL_CHARS,
    StoreValidationError,
    normalize_actor,
    normalize_embedding_model,
)

ENV_PREFIX = "ENGRAM_"
DEFAULT_CONFIG_PATH = Path("engram.toml")
SUPPORTED_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
MAX_NETWORK_PORT = 65_535
MAX_DATACRON_NEIGHBOR_LIMIT = 64
MAX_FTS_TOP_K = 256
MAX_FTS_QUERY_CHARS = 4096
MAX_FTS_QUERY_TERMS = 64
MIN_FTS_PREFIX_CHARS = 2
MAX_FTS_MIN_PREFIX_CHARS = 32
MIN_FTS_QUERY_TIMEOUT_MS = 10
MAX_FTS_QUERY_TIMEOUT_MS = 2000
MAX_HYBRID_CANDIDATES = 16_384
MIN_CAPSULE_BYTE_BUDGET = 1200
MIN_HTTP_REQUEST_BODY_BYTES = 4096
MAX_HTTP_REQUEST_BODY_BYTES = 512 * 1024
MAX_EMBEDDINGS_ENDPOINT_CHARS = 2048


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


def _is_plain_int(value: object) -> bool:
    """Return true only for integers, excluding bool and coercible numerics."""
    return isinstance(value, int) and not isinstance(value, bool)


def validate_loopback_host(host: str) -> None:
    """Reject every server host that is not an unambiguous loopback IP literal."""
    if not isinstance(host, str) or not host or host.strip() != host or "%" in host:
        raise ConfigError("server.host must be a loopback IP literal")
    try:
        address = ip_address(host)
    except ValueError as exc:
        raise ConfigError("server.host must be a loopback IP literal") from exc
    if not address.is_loopback:
        raise ConfigError("server.host must be a loopback IP literal")


def format_endpoint(host: str, port: int, path: str) -> str:
    """Render the URL a client must be pointed at, for any accepted loopback host.

    An IPv6 literal has to be bracketed inside a URL, because the character that
    separates a host from its port is the same one the address is written with:
    `http://::1:8377/mcp` parses as neither a host nor a port, and every client
    rejects it. `server.host` accepts any loopback literal, `::1` included, so a
    configuration this project documents as supported produced an endpoint no
    client could resolve, printed by commands whose whole purpose is to hand that
    endpoint over.

    The socket API takes the address unbracketed, so only what is shown or
    written to a client configuration goes through here.
    """
    rendered = f"[{host}]" if ":" in host else host
    return f"http://{rendered}:{port}{path}"


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
    _allow_legacy_oversize: InitVar[bool] = False

    def __post_init__(self, _allow_legacy_oversize: bool) -> None:
        """Keep configurable storage bounds inside fixed safe ceilings."""
        if (
            not _is_plain_int(self.max_statement_chars)
            or self.max_statement_chars < 1
            or (self.max_statement_chars > HARD_MAX_STATEMENT_CHARS and not _allow_legacy_oversize)
        ):
            raise ConfigError(
                f"limits.max_statement_chars must be between 1 and {HARD_MAX_STATEMENT_CHARS}"
            )
        if (
            not _is_plain_int(self.max_subject_keys)
            or self.max_subject_keys < 1
            or (self.max_subject_keys > HARD_MAX_SUBJECT_KEYS and not _allow_legacy_oversize)
        ):
            raise ConfigError(
                f"limits.max_subject_keys must be between 1 and {HARD_MAX_SUBJECT_KEYS}"
            )


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """File and console logging settings."""

    path: Path = Path("logs/engram.log")
    file_level: str = "INFO"
    console_level: str = "INFO"


@dataclass(frozen=True, slots=True)
class AttestationConfig:
    """Trusted local attestation defaults."""

    default_actor: str = "local-operator"

    def __post_init__(self) -> None:
        """Reject audit identities that would fail only on the first mutation."""
        try:
            normalized = normalize_actor(self.default_actor)
        except StoreValidationError as exc:
            raise ConfigError(f"attestation.default_actor is invalid: {exc}") from exc
        if normalized != self.default_actor:
            raise ConfigError("attestation.default_actor must not contain surrounding whitespace")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Streamable HTTP server and write backpressure settings."""

    host: str = "127.0.0.1"
    port: int = 8377
    path: str = "/mcp"
    write_wait_timeout_ms: int = 2000
    ttl_sweep_interval_seconds: float = 60.0
    max_request_body_bytes: int = MAX_HTTP_REQUEST_BODY_BYTES

    def __post_init__(self) -> None:
        """Make unsafe direct construction fail closed, including library use."""
        validate_loopback_host(self.host)
        if not _is_plain_int(self.port) or not 1 <= self.port <= MAX_NETWORK_PORT:
            raise ConfigError("server.port must be between 1 and 65535")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ConfigError("server.path must start with a slash")
        if not _is_plain_int(self.write_wait_timeout_ms) or self.write_wait_timeout_ms <= 0:
            raise ConfigError("server.write_wait_timeout_ms must be a positive integer")
        if (
            not _is_plain_int(self.max_request_body_bytes)
            or not MIN_HTTP_REQUEST_BODY_BYTES
            <= self.max_request_body_bytes
            <= MAX_HTTP_REQUEST_BODY_BYTES
        ):
            raise ConfigError(
                "server.max_request_body_bytes must be between "
                f"{MIN_HTTP_REQUEST_BODY_BYTES} and {MAX_HTTP_REQUEST_BODY_BYTES}"
            )
        if (
            isinstance(self.ttl_sweep_interval_seconds, bool)
            or not isinstance(self.ttl_sweep_interval_seconds, int | float)
            or not math.isfinite(self.ttl_sweep_interval_seconds)
            or self.ttl_sweep_interval_seconds <= 0
        ):
            raise ConfigError("server.ttl_sweep_interval_seconds must be a finite positive number")


@dataclass(frozen=True, slots=True)
class CapsuleConfig:
    """Recall capsule token budget settings."""

    default_token_budget: int = 4800
    min_token_budget: int = 1200
    # The ceiling a client may request, not the amount it usually gets. At 6000
    # a conflict family of six recorded versions, or two long ones, needed more
    # bytes than any permitted budget, so those memories were reachable through
    # no MCP call at all -- only through the command line. The default stays
    # conservative; what changes is that asking for more is now possible.
    max_token_budget: int = 32_768

    def __post_init__(self) -> None:
        """Reject an impossible serialized MCP envelope in every construction path."""
        if not all(
            _is_plain_int(value)
            for value in (
                self.default_token_budget,
                self.min_token_budget,
                self.max_token_budget,
            )
        ):
            raise ConfigError("capsule token budgets must be integers")
        if self.min_token_budget < MIN_CAPSULE_BYTE_BUDGET:
            raise ConfigError(
                "capsule.min_token_budget must be at least "
                f"{MIN_CAPSULE_BYTE_BUDGET} for the mandatory MCP envelope"
            )
        if self.max_token_budget < self.min_token_budget:
            raise ConfigError("capsule.max_token_budget must be at least the minimum")
        if not self.min_token_budget <= self.default_token_budget <= self.max_token_budget:
            raise ConfigError("capsule.default_token_budget must be within the configured bounds")


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
    fts_top_k: int = 64
    fts_max_query_chars: int = 1024
    fts_max_query_terms: int = 24
    fts_min_prefix_chars: int = 4
    hybrid_max_candidates: int = 4096
    fts_query_timeout_ms: int = 250

    def __post_init__(self) -> None:
        """Enforce retrieval bounds for configuration and direct library use."""
        _validate_retrieval_values(self)


def _validate_retrieval_values(config: RetrievalConfig) -> None:
    """Validate one retrieval configuration without requiring an AppConfig."""
    if not isinstance(config.mode, RetrievalMode):
        raise ConfigError("retrieval.mode must be a RetrievalMode")
    _validate_retrieval_integer_types(config)
    _validate_fts_bounds(config)
    _validate_hybrid_and_embedding_settings(config)


def _validate_retrieval_integer_types(config: RetrievalConfig) -> None:
    """Reject bool, float, and other late-coercion hazards in direct use."""
    integer_values = {
        "fts_top_k": config.fts_top_k,
        "fts_max_query_chars": config.fts_max_query_chars,
        "fts_max_query_terms": config.fts_max_query_terms,
        "fts_min_prefix_chars": config.fts_min_prefix_chars,
        "fts_query_timeout_ms": config.fts_query_timeout_ms,
        "hybrid_max_candidates": config.hybrid_max_candidates,
        "embeddings_timeout_ms": config.embeddings_timeout_ms,
        "rrf_k": config.rrf_k,
    }
    for name, value in integer_values.items():
        if not _is_plain_int(value):
            raise ConfigError(f"retrieval.{name} must be an integer")


def _validate_fts_bounds(config: RetrievalConfig) -> None:
    """Enforce the hard lexical query and result limits."""
    if not 1 <= config.fts_top_k <= MAX_FTS_TOP_K:
        raise ConfigError(f"retrieval.fts_top_k must be between 1 and {MAX_FTS_TOP_K}")
    if not 1 <= config.fts_max_query_chars <= MAX_FTS_QUERY_CHARS:
        raise ConfigError(
            f"retrieval.fts_max_query_chars must be between 1 and {MAX_FTS_QUERY_CHARS}"
        )
    if not 1 <= config.fts_max_query_terms <= MAX_FTS_QUERY_TERMS:
        raise ConfigError(
            f"retrieval.fts_max_query_terms must be between 1 and {MAX_FTS_QUERY_TERMS}"
        )
    if not MIN_FTS_PREFIX_CHARS <= config.fts_min_prefix_chars <= MAX_FTS_MIN_PREFIX_CHARS:
        raise ConfigError(
            "retrieval.fts_min_prefix_chars must be between "
            f"{MIN_FTS_PREFIX_CHARS} and {MAX_FTS_MIN_PREFIX_CHARS}"
        )
    if config.fts_min_prefix_chars > config.fts_max_query_chars:
        raise ConfigError("retrieval.fts_min_prefix_chars must not exceed fts_max_query_chars")
    if not MIN_FTS_QUERY_TIMEOUT_MS <= config.fts_query_timeout_ms <= MAX_FTS_QUERY_TIMEOUT_MS:
        raise ConfigError(
            "retrieval.fts_query_timeout_ms must be between "
            f"{MIN_FTS_QUERY_TIMEOUT_MS} and {MAX_FTS_QUERY_TIMEOUT_MS}"
        )


def _validate_hybrid_and_embedding_settings(config: RetrievalConfig) -> None:
    """Validate the bounded semantic scan and remote endpoint settings."""
    if not 1 <= config.hybrid_max_candidates <= MAX_HYBRID_CANDIDATES:
        raise ConfigError(
            f"retrieval.hybrid_max_candidates must be between 1 and {MAX_HYBRID_CANDIDATES}"
        )
    _validate_embedding_endpoint(config.embeddings_endpoint)
    if config.embeddings_timeout_ms <= 0:
        raise ConfigError("retrieval.embeddings_timeout_ms must be greater than zero")
    if config.rrf_k <= 0:
        raise ConfigError("retrieval.rrf_k must be greater than zero")
    normalized_model = _validate_embedding_model(config.embeddings_model)
    if config.mode is RetrievalMode.HYBRID and not normalized_model:
        raise ConfigError("retrieval.embeddings_model is required in hybrid mode")


def _validate_embedding_endpoint(value: object) -> None:
    """Reject an endpoint that URL parsers or HTTPX would reinterpret later."""
    if (
        not isinstance(value, str)
        or value.strip() != value
        or len(value) > MAX_EMBEDDINGS_ENDPOINT_CHARS
        or any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        )
    ):
        raise ConfigError("retrieval.embeddings_endpoint must be an HTTP URL")
    try:
        endpoint = urlparse(value)
        hostname = endpoint.hostname
        _parsed_port = endpoint.port
    except ValueError as exc:
        raise ConfigError("retrieval.embeddings_endpoint must be an HTTP URL") from exc
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or hostname is None:
        raise ConfigError("retrieval.embeddings_endpoint must be an HTTP URL")


def _validate_embedding_model(value: object) -> str:
    """Return one normalized model identifier or fail during configuration."""
    if not isinstance(value, str):
        raise ConfigError(
            "retrieval.embeddings_model must be a string of at most "
            f"{MAX_EMBEDDING_MODEL_CHARS} characters"
        )
    if not value:
        return ""
    try:
        normalized = normalize_embedding_model(value)
    except StoreValidationError as exc:
        raise ConfigError(f"retrieval.embeddings_model is invalid: {exc}") from exc
    if normalized != value:
        raise ConfigError("retrieval.embeddings_model must not have surrounding whitespace")
    return normalized


@dataclass(frozen=True, slots=True)
class DatacronConfig:
    """Datacron stdio transport and vault confinement settings."""

    command: str = "datacron"
    args: tuple[str, ...] = ("mcp", "serve")
    vault_root: Path | None = None
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    new_note_directory: str = "_memory/engram"
    neighbor_limit: int = 8
    startup_timeout_ms: int = 10_000
    request_timeout_ms: int = 30_000
    shutdown_timeout_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete Engram application configuration."""

    database: DatabaseConfig
    ttl_days: TtlConfig
    limits: LimitsConfig
    logging: LoggingConfig
    attestation: AttestationConfig = field(default_factory=AttestationConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    capsule: CapsuleConfig = field(default_factory=CapsuleConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    datacron: DatacronConfig = field(default_factory=DatacronConfig)


DEFAULT_DATABASE_CONFIG = DatabaseConfig()
DEFAULT_TTL_CONFIG = TtlConfig()
DEFAULT_LIMITS_CONFIG = LimitsConfig()
DEFAULT_LOGGING_CONFIG = LoggingConfig()
DEFAULT_ATTESTATION_CONFIG = AttestationConfig()
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
        raise ConfigError(
            f"Configuration file does not exist: {config_path}. "
            "Run 'engram init' to write a starting configuration there."
        )

    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML configuration: {exc}") from exc

    _reject_unknown_configuration(raw)
    database = _section(raw, "database")
    ttl_days = _section(raw, "ttl_days")
    limits = _section(raw, "limits")
    logging_config = _section(raw, "logging")
    attestation = _section(raw, "attestation")
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
        attestation=AttestationConfig(
            default_actor=_string_value(
                attestation,
                "default_actor",
                environment,
                DEFAULT_ATTESTATION_CONFIG.default_actor,
            ),
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
            ttl_sweep_interval_seconds=_float_value(
                server,
                "ttl_sweep_interval_seconds",
                environment,
                DEFAULT_SERVER_CONFIG.ttl_sweep_interval_seconds,
            ),
            max_request_body_bytes=_integer_value(
                server,
                "max_request_body_bytes",
                environment,
                DEFAULT_SERVER_CONFIG.max_request_body_bytes,
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
            fts_top_k=_integer_value(
                retrieval,
                "fts_top_k",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.fts_top_k,
            ),
            fts_max_query_chars=_integer_value(
                retrieval,
                "fts_max_query_chars",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.fts_max_query_chars,
            ),
            fts_max_query_terms=_integer_value(
                retrieval,
                "fts_max_query_terms",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.fts_max_query_terms,
            ),
            fts_min_prefix_chars=_integer_value(
                retrieval,
                "fts_min_prefix_chars",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.fts_min_prefix_chars,
            ),
            fts_query_timeout_ms=_integer_value(
                retrieval,
                "fts_query_timeout_ms",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.fts_query_timeout_ms,
            ),
            hybrid_max_candidates=_integer_value(
                retrieval,
                "hybrid_max_candidates",
                environment,
                DEFAULT_RETRIEVAL_CONFIG.hybrid_max_candidates,
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
            startup_timeout_ms=_integer_value(
                datacron,
                "startup_timeout_ms",
                environment,
                DEFAULT_DATACRON_CONFIG.startup_timeout_ms,
            ),
            request_timeout_ms=_integer_value(
                datacron,
                "request_timeout_ms",
                environment,
                DEFAULT_DATACRON_CONFIG.request_timeout_ms,
            ),
            shutdown_timeout_ms=_integer_value(
                datacron,
                "shutdown_timeout_ms",
                environment,
                DEFAULT_DATACRON_CONFIG.shutdown_timeout_ms,
            ),
        ),
    )
    _validate_config(result)
    return result


def load_preflight_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load only database, legacy limits, and logging needed for preflight."""
    environment = os.environ if environ is None else environ
    selected_path: str | Path
    if path is not None:
        selected_path = path
    else:
        selected_path = environment.get(f"{ENV_PREFIX}CONFIG", str(DEFAULT_CONFIG_PATH))
    config_path = Path(selected_path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"Configuration file does not exist: {config_path}. "
            "Run 'engram init' to write a starting configuration there."
        )
    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML configuration: {exc}") from exc

    _reject_unknown_configuration(raw)
    database = _section(raw, "database")
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
        ttl_days=DEFAULT_TTL_CONFIG,
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
            _allow_legacy_oversize=True,
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
    if result.database.busy_timeout_ms <= 0:
        raise ConfigError("database.busy_timeout_ms must be greater than zero")
    if result.logging.file_level not in SUPPORTED_LOG_LEVELS:
        raise ConfigError(f"Unsupported file log level: {result.logging.file_level}")
    if result.logging.console_level not in SUPPORTED_LOG_LEVELS:
        raise ConfigError(f"Unsupported console log level: {result.logging.console_level}")
    return result


def _known_sections() -> dict[str, frozenset[str]]:
    """Map every configuration section to the keys Engram actually reads from it."""
    return {
        "database": frozenset(field.name for field in dataclass_fields(DatabaseConfig)),
        "ttl_days": frozenset(field.name for field in dataclass_fields(TtlConfig)),
        "limits": frozenset(field.name for field in dataclass_fields(LimitsConfig)),
        "logging": frozenset(field.name for field in dataclass_fields(LoggingConfig)),
        "attestation": frozenset(field.name for field in dataclass_fields(AttestationConfig)),
        "server": frozenset(field.name for field in dataclass_fields(ServerConfig)),
        "capsule": frozenset(field.name for field in dataclass_fields(CapsuleConfig)),
        "retrieval": frozenset(field.name for field in dataclass_fields(RetrievalConfig)),
        "datacron": frozenset(field.name for field in dataclass_fields(DatacronConfig)),
    }


def _reject_unknown_configuration(raw: Mapping[str, Any]) -> None:
    """Refuse a key Engram does not read, instead of running on the default it hides.

    A misspelt key loads without a word and leaves the value the user meant to
    set at its default. Measured before this check existed: `[server] prot` and
    `[servr] port` both left the endpoint on 8377, and a misspelt `[database]
    path` opens a different database from the configured one, so new memories go
    somewhere nobody will look for them again. The loader validated types and
    never names.
    """
    known = _known_sections()
    for name in sorted(raw):
        if name not in known:
            raise ConfigError(f"Unknown configuration section: [{name}]{_suggestion(name, known)}")
        table = raw[name]
        if not isinstance(table, dict):
            continue
        for key in sorted(table):
            if key not in known[name]:
                raise ConfigError(f"Unknown key in [{name}]: {key}{_suggestion(key, known[name])}")


def _suggestion(value: str, candidates: Iterable[str]) -> str:
    close = get_close_matches(value, list(candidates), n=1)
    return f". Did you mean {close[0]}?" if close else ""


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


def _float_value(
    section: dict[str, Any],
    key: str,
    environment: Mapping[str, str],
    default: float,
) -> float:
    value = _raw_value(section, key, environment, default)
    if isinstance(value, bool):
        raise ConfigError(f"Configuration value must be a number: {key}")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"Configuration value must be a number: {key}") from exc
    raise ConfigError(f"Configuration value must be a number: {key}")


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
    validate_loopback_host(config.server.host)
    if not 1 <= config.server.port <= MAX_NETWORK_PORT:
        raise ConfigError("server.port must be between 1 and 65535")
    if not config.server.path.startswith("/"):
        raise ConfigError("server.path must start with a slash")
    if config.server.write_wait_timeout_ms <= 0:
        raise ConfigError("server.write_wait_timeout_ms must be greater than zero")
    if not (
        MIN_HTTP_REQUEST_BODY_BYTES
        <= config.server.max_request_body_bytes
        <= MAX_HTTP_REQUEST_BODY_BYTES
    ):
        raise ConfigError(
            "server.max_request_body_bytes must be between "
            f"{MIN_HTTP_REQUEST_BODY_BYTES} and {MAX_HTTP_REQUEST_BODY_BYTES}"
        )
    if (
        not math.isfinite(config.server.ttl_sweep_interval_seconds)
        or config.server.ttl_sweep_interval_seconds <= 0
    ):
        raise ConfigError("server.ttl_sweep_interval_seconds must be a finite positive number")
    if config.capsule.min_token_budget < MIN_CAPSULE_BYTE_BUDGET:
        raise ConfigError(
            "capsule.min_token_budget must be at least "
            f"{MIN_CAPSULE_BYTE_BUDGET} for the mandatory MCP envelope"
        )
    if config.capsule.max_token_budget < config.capsule.min_token_budget:
        raise ConfigError("capsule.max_token_budget must be at least the minimum")
    if not (
        config.capsule.min_token_budget
        <= config.capsule.default_token_budget
        <= config.capsule.max_token_budget
    ):
        raise ConfigError("capsule.default_token_budget must be within the configured bounds")


def _validate_retrieval_config(config: AppConfig) -> None:
    _validate_retrieval_values(config.retrieval)


def _validate_datacron_config(config: AppConfig) -> None:
    if not 1 <= config.datacron.neighbor_limit <= MAX_DATACRON_NEIGHBOR_LIMIT:
        raise ConfigError(
            f"datacron.neighbor_limit must be between 1 and {MAX_DATACRON_NEIGHBOR_LIMIT}"
        )
    for name, value in (
        ("startup_timeout_ms", config.datacron.startup_timeout_ms),
        ("request_timeout_ms", config.datacron.request_timeout_ms),
        ("shutdown_timeout_ms", config.datacron.shutdown_timeout_ms),
    ):
        if value <= 0:
            raise ConfigError(f"datacron.{name} must be greater than zero")
    target = Path(config.datacron.new_note_directory)
    if (
        target.is_absolute()
        or ".." in target.parts
        or not target.parts
        or target.parts[0] != "_memory"
    ):
        raise ConfigError("datacron.new_note_directory must be confined inside _memory")
