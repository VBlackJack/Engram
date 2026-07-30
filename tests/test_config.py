# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Configuration and FileLogger tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from engram.config import (
    CapsuleConfig,
    ConfigError,
    RetrievalConfig,
    RetrievalMode,
    ServerConfig,
    load_config,
)
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
default_token_budget = 4800
min_token_budget = 1200
max_token_budget = 6000

[retrieval]
mode = "fts"
fts_top_k = 64
fts_max_query_chars = 1024
fts_max_query_terms = 24
fts_min_prefix_chars = 4
hybrid_max_candidates = 4096
embeddings_endpoint = "http://127.0.0.1:1234/v1/embeddings"
embeddings_model = ""
embeddings_timeout_ms = 3000
rrf_k = 60
""".strip(),
        encoding="utf-8",
    )
    environment = {
        "ENGRAM_DATABASE_PATH": "override.db",
        "ENGRAM_DATABASE_BUSY_TIMEOUT_MS": "7500",
        "ENGRAM_TTL_DAYS_EPISODE": "3",
        "ENGRAM_LIMITS_MAX_SUBJECT_KEYS": "4",
        "ENGRAM_LOGGING_FILE_LEVEL": "debug",
        "ENGRAM_ATTESTATION_DEFAULT_ACTOR": "reviewer@example",
        "ENGRAM_SERVER_PORT": "9000",
        "ENGRAM_SERVER_PATH": "/memory",
        "ENGRAM_SERVER_TTL_SWEEP_INTERVAL_SECONDS": "0.25",
        "ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET": "2800",
        "ENGRAM_RETRIEVAL_MODE": "hybrid",
        "ENGRAM_RETRIEVAL_FTS_TOP_K": "48",
        "ENGRAM_RETRIEVAL_FTS_MAX_QUERY_CHARS": "800",
        "ENGRAM_RETRIEVAL_FTS_MAX_QUERY_TERMS": "20",
        "ENGRAM_RETRIEVAL_FTS_MIN_PREFIX_CHARS": "5",
        "ENGRAM_RETRIEVAL_HYBRID_MAX_CANDIDATES": "2048",
        "ENGRAM_RETRIEVAL_EMBEDDINGS_MODEL": "text-embedding-test",
        "ENGRAM_RETRIEVAL_RRF_K": "80",
        "ENGRAM_DATACRON_STARTUP_TIMEOUT_MS": "11000",
        "ENGRAM_DATACRON_REQUEST_TIMEOUT_MS": "31000",
        "ENGRAM_DATACRON_SHUTDOWN_TIMEOUT_MS": "6000",
    }

    config = load_config(config_path, environ=environment)

    assert config.database.path == (tmp_path / "override.db").resolve()
    assert config.database.busy_timeout_ms == 7500
    assert config.ttl_days.episode == 3
    assert config.ttl_days.project_state == 30
    assert config.limits.max_subject_keys == 4
    assert config.logging.file_level == "DEBUG"
    assert config.logging.path == (tmp_path / "logs" / "base.log").resolve()
    assert config.attestation.default_actor == "reviewer@example"
    assert config.server.port == 9000
    assert config.server.path == "/memory"
    assert config.server.write_wait_timeout_ms == 2000
    assert config.server.ttl_sweep_interval_seconds == 0.25
    assert config.capsule.default_token_budget == 2800
    assert config.retrieval.mode is RetrievalMode.HYBRID
    assert config.retrieval.fts_top_k == 48
    assert config.retrieval.fts_max_query_chars == 800
    assert config.retrieval.fts_max_query_terms == 20
    assert config.retrieval.fts_min_prefix_chars == 5
    assert config.retrieval.hybrid_max_candidates == 2048
    assert config.retrieval.embeddings_model == "text-embedding-test"
    assert config.retrieval.rrf_k == 80
    assert config.datacron.startup_timeout_ms == 11000
    assert config.datacron.request_timeout_ms == 31000
    assert config.datacron.shutdown_timeout_ms == 6000


def test_load_config_rejects_invalid_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text("[limits]\nmax_statement_chars = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="max_statement_chars"):
        load_config(config_path, environ={})


def test_load_config_rejects_invalid_capsule_bounds(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        "[capsule]\nmin_token_budget = 1200\nmax_token_budget = 1100\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="at least the minimum"):
        load_config(config_path, environ={})


def test_load_config_rejects_capsule_budget_below_mcp_envelope(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text("[capsule]\nmin_token_budget = 1199\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="mandatory MCP envelope"):
        load_config(config_path, environ={})


@pytest.mark.parametrize("host", ["127.0.0.1", "127.42.0.7", "::1"])
def test_server_config_accepts_loopback_ip_literals(host: str) -> None:
    assert ServerConfig(host=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104 - must be rejected by the fail-closed test
        "::",
        "192.168.1.20",
        "localhost",
        "example.test",
        " 127.0.0.1",
        "::1%1",
    ],
)
def test_server_config_rejects_non_loopback_or_ambiguous_hosts(host: str) -> None:
    with pytest.raises(ConfigError, match="loopback IP literal"):
        ServerConfig(host=host)


@pytest.mark.parametrize("value", [True, 1.5])
def test_direct_capsule_config_rejects_non_integer_budgets(value: object) -> None:
    with pytest.raises(ConfigError, match="must be integers"):
        CapsuleConfig(default_token_budget=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 1.5, 16_385])
def test_direct_retrieval_config_rejects_invalid_hard_bounds(value: object) -> None:
    with pytest.raises(ConfigError, match="hybrid_max_candidates"):
        RetrievalConfig(hybrid_max_candidates=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_load_config_rejects_invalid_ttl_sweep_interval(tmp_path: Path, value: str) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        f"[server]\nttl_sweep_interval_seconds = '{value}'\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="ttl_sweep_interval_seconds"):
        load_config(config_path, environ={})


@pytest.mark.parametrize(
    "name",
    ["startup_timeout_ms", "request_timeout_ms", "shutdown_timeout_ms"],
)
def test_load_config_rejects_nonpositive_datacron_timeout(
    tmp_path: Path,
    name: str,
) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(f"[datacron]\n{name} = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=rf"datacron\.{name}"):
        load_config(config_path, environ={})


def test_load_config_rejects_excessive_datacron_neighbor_limit(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text("[datacron]\nneighbor_limit = 65\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"neighbor_limit.*between 1 and 64"):
        load_config(config_path, environ={})


def test_load_config_requires_embedding_model_in_hybrid_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text("[retrieval]\nmode = 'hybrid'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="embeddings_model"):
        load_config(config_path, environ={})


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("fts_top_k", "0", r"fts_top_k.*between 1 and 256"),
        ("fts_top_k", "257", r"fts_top_k.*between 1 and 256"),
        ("fts_max_query_chars", "4097", r"fts_max_query_chars.*between 1 and 4096"),
        ("fts_max_query_terms", "65", r"fts_max_query_terms.*between 1 and 64"),
        ("fts_min_prefix_chars", "1", r"fts_min_prefix_chars.*between 2 and 32"),
        ("fts_min_prefix_chars", "33", r"fts_min_prefix_chars.*between 2 and 32"),
    ],
)
def test_load_config_rejects_unsafe_fts_bounds(
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(f"[retrieval]\n{name} = {value}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path, environ={})


def test_load_config_rejects_prefix_longer_than_query_bound(tmp_path: Path) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        "[retrieval]\nfts_max_query_chars = 3\nfts_min_prefix_chars = 4\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must not exceed"):
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
