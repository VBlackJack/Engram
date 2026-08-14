# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Configuration and FileLogger tests."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from engram.config import (
    MAX_HTTP_REQUEST_BODY_BYTES,
    AttestationConfig,
    CapsuleConfig,
    ConfigError,
    LimitsConfig,
    RetrievalConfig,
    RetrievalMode,
    ServerConfig,
    load_config,
    load_preflight_config,
)
from engram.logging_setup import FileLogger
from engram.resources import example_config_text


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
max_request_body_bytes = 65536
session_idle_timeout_seconds = 900

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
fts_query_timeout_ms = 250
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
        "ENGRAM_SERVER_MAX_REQUEST_BODY_BYTES": "32768",
        "ENGRAM_SERVER_SESSION_IDLE_TIMEOUT_SECONDS": "450",
        "ENGRAM_CAPSULE_DEFAULT_TOKEN_BUDGET": "2800",
        "ENGRAM_RETRIEVAL_MODE": "hybrid",
        "ENGRAM_RETRIEVAL_FTS_TOP_K": "48",
        "ENGRAM_RETRIEVAL_FTS_MAX_QUERY_CHARS": "800",
        "ENGRAM_RETRIEVAL_FTS_MAX_QUERY_TERMS": "20",
        "ENGRAM_RETRIEVAL_FTS_MIN_PREFIX_CHARS": "5",
        "ENGRAM_RETRIEVAL_FTS_QUERY_TIMEOUT_MS": "500",
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
    assert config.server.max_request_body_bytes == 32768
    assert config.server.session_idle_timeout_seconds == 450
    assert config.capsule.default_token_budget == 2800
    assert config.retrieval.mode is RetrievalMode.HYBRID
    assert config.retrieval.fts_top_k == 48
    assert config.retrieval.fts_max_query_chars == 800
    assert config.retrieval.fts_max_query_terms == 20
    assert config.retrieval.fts_min_prefix_chars == 5
    assert config.retrieval.fts_query_timeout_ms == 500
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


@pytest.mark.parametrize("value", [True, 9, 2001])
def test_direct_retrieval_config_rejects_invalid_fts_deadline(value: object) -> None:
    with pytest.raises(ConfigError, match="fts_query_timeout_ms"):
        RetrievalConfig(fts_query_timeout_ms=value)  # type: ignore[arg-type]


def test_retrieval_config_keeps_legacy_positional_hybrid_limit() -> None:
    config = RetrievalConfig(
        RetrievalMode.FTS,
        "http://127.0.0.1:1234/v1/embeddings",
        "",
        3000,
        60,
        64,
        1024,
        24,
        4,
        100,
    )

    assert config.hybrid_max_candidates == 100
    assert config.fts_query_timeout_ms == 250


@pytest.mark.parametrize("value", [True, 4095, MAX_HTTP_REQUEST_BODY_BYTES + 1])
def test_direct_server_config_rejects_invalid_body_limit(value: object) -> None:
    with pytest.raises(ConfigError, match="max_request_body_bytes"):
        ServerConfig(max_request_body_bytes=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "",
        " actor",
        "actor\nforged",
        "actor\u2028forged",
        "actor\u2029forged",
        "actor\u202eforged",
        "a" * 258,
    ],
)
def test_attestation_config_rejects_invalid_default_actor(value: str) -> None:
    with pytest.raises(ConfigError, match="default_actor"):
        AttestationConfig(default_actor=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_statement_chars", 16_385),
        ("max_subject_keys", 65),
    ],
)
def test_direct_limits_config_rejects_values_above_hard_ceiling(
    field: str,
    value: int,
) -> None:
    invalid_constructor: Callable[[], LimitsConfig] = (
        (lambda: LimitsConfig(max_statement_chars=value))
        if field == "max_statement_chars"
        else (lambda: LimitsConfig(max_subject_keys=value))
    )
    with pytest.raises(ConfigError, match=field):
        invalid_constructor()


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
    "endpoint",
    [
        "http://127.0.0.1:notaport/v1/embeddings",
        "http://127.0.0.1:65536/v1/embeddings",
        "http://[::1",
        "http://[x]/v1/embeddings",
        "http://local host/v1/embeddings",
        "http://localhost/\ud800",
    ],
)
def test_direct_retrieval_config_rejects_malformed_embedding_endpoint(
    endpoint: str,
) -> None:
    with pytest.raises(ConfigError, match="embeddings_endpoint"):
        RetrievalConfig(embeddings_endpoint=endpoint)


@pytest.mark.parametrize(
    "model",
    [
        " padded",
        "padded ",
        "\0",
        "\ud800",
        "m" * 201,
    ],
)
def test_direct_retrieval_config_rejects_invalid_embedding_model(model: str) -> None:
    with pytest.raises(ConfigError, match="embeddings_model"):
        RetrievalConfig(
            mode=RetrievalMode.HYBRID,
            embeddings_model=model,
        )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("fts_top_k", "0", r"fts_top_k.*between 1 and 256"),
        ("fts_top_k", "257", r"fts_top_k.*between 1 and 256"),
        ("fts_max_query_chars", "4097", r"fts_max_query_chars.*between 1 and 4096"),
        ("fts_max_query_terms", "65", r"fts_max_query_terms.*between 1 and 64"),
        ("fts_min_prefix_chars", "1", r"fts_min_prefix_chars.*between 2 and 32"),
        ("fts_min_prefix_chars", "33", r"fts_min_prefix_chars.*between 2 and 32"),
        ("fts_query_timeout_ms", "9", r"fts_query_timeout_ms.*between 10 and 2000"),
        ("fts_query_timeout_ms", "2001", r"fts_query_timeout_ms.*between 10 and 2000"),
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
    rotating = next(
        handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
    )
    assert rotating.maxBytes > 0
    assert rotating.backupCount > 0


def test_file_logger_validates_rotation_lock_before_returning(tmp_path: Path) -> None:
    log_path = tmp_path / "engram.log"
    lock_path = Path(f"{log_path}.rotate.lock")
    lock_path.mkdir()
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        f'[logging]\npath = "{log_path.as_posix()}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path, environ={})

    # The contract is that configuration refuses rather than returning a logger that
    # cannot rotate. Which OSError subclass carries the refusal is the operating
    # system's choice: Windows reports PermissionError, Linux IsADirectoryError.
    # Naming the path keeps the assertion from passing on an unrelated failure.
    with pytest.raises(OSError, match=re.escape(lock_path.name)) as failure:
        FileLogger(config.logging, name="engram.invalid-log-lock").configure()

    assert str(failure.value.filename) == str(lock_path)


def test_file_logger_contains_rotation_lock_failure_after_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "engram.toml"
    config_path.write_text(
        '[logging]\npath = "engram.log"\n',
        encoding="utf-8",
    )
    config = load_config(config_path, environ={})
    logger = FileLogger(config.logging, name="engram.late-log-lock-failure").configure()
    rotating = next(
        handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
    )
    lock_path = Path(f"{rotating.baseFilename}.rotate.lock")
    lock_path.unlink()
    lock_path.mkdir()
    handled: list[logging.LogRecord] = []
    monkeypatch.setattr(rotating, "handleError", handled.append)

    logger.info("This operation may already have committed.")

    assert len(handled) == 1


def test_rotating_file_handler_serializes_multiple_processes(tmp_path: Path) -> None:
    log_path = tmp_path / "shared.log"
    script = """
import logging
import sys
from engram.logging_setup import InterProcessRotatingFileHandler

logger = logging.getLogger(f"engram.concurrent.{sys.argv[2]}")
logger.setLevel(logging.INFO)
logger.propagate = False
handler = InterProcessRotatingFileHandler(
    sys.argv[1],
    max_bytes=1024,
    backup_count=3,
    encoding="utf-8",
)
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)
for index in range(300):
    logger.info("%s-%04d %s", sys.argv[2], index, "x" * 96)
handler.close()
""".strip()
    processes = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", script, str(log_path), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]

    results = [process.communicate(timeout=30) for process in processes]

    assert [process.returncode for process in processes] == [0, 0]
    assert all("Logging error" not in stderr for _stdout, stderr in results)
    log_files = [log_path, *sorted(tmp_path.glob("shared.log.[1-3]"))]
    assert len(log_files) == 4
    assert all(path.stat().st_size <= 1150 for path in log_files)


def test_a_misspelt_key_is_refused_instead_of_silently_ignored(tmp_path: Path) -> None:
    """A key Engram does not read is a value the user set and never got."""
    path = tmp_path / "engram.toml"
    path.write_text("[server]\nprot = 9000\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"Unknown key in \[server\]: prot"):
        load_config(path)


def test_a_misspelt_key_names_the_one_it_resembles(tmp_path: Path) -> None:
    path = tmp_path / "engram.toml"
    path.write_text("[server]\nprot = 9000\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Did you mean port"):
        load_config(path)


def test_a_misspelt_section_is_refused(tmp_path: Path) -> None:
    """[servr] used to load with every server value left at its default."""
    path = tmp_path / "engram.toml"
    path.write_text("[servr]\nport = 9000\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"Unknown configuration section: \[servr\]"):
        load_config(path)


def test_a_misspelt_database_path_cannot_open_a_different_database(tmp_path: Path) -> None:
    """The worst case of the class: memories written where nobody will look."""
    path = tmp_path / "engram.toml"
    path.write_text('[database]\npathh = "elsewhere.db"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"Unknown key in \[database\]: pathh"):
        load_config(path)


def test_the_shipped_template_passes_its_own_key_check(tmp_path: Path) -> None:
    """The check is worthless if the configuration the project ships cannot clear it."""
    path = tmp_path / "engram.toml"
    path.write_text(example_config_text(), encoding="utf-8")

    assert load_config(path).server.port == 8377


def test_the_preflight_loader_refuses_the_same_keys(tmp_path: Path) -> None:
    path = tmp_path / "engram.toml"
    path.write_text("[limits]\nmax_statement_charss = 10\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"Unknown key in \[limits\]"):
        load_preflight_config(path)
