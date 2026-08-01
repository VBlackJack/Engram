# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Console commands for the Engram server and trusted local workflows."""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import socket
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from . import __version__
from .config import AppConfig, ConfigError, load_config, load_preflight_config
from .consolidation.gateway import DatacronGatewayError
from .consolidation.mcp_gateway import McpDatacronGateway
from .consolidation.models import ApplyStatus, ConsolidationPlan
from .consolidation.report import (
    model_json,
    render_apply_markdown,
    render_freshness_markdown,
    render_plan_markdown,
)
from .consolidation.service import ConsolidationService
from .db import (
    DatabaseError,
    SQLiteVersionError,
    latest_schema_version,
    preflight_database,
)
from .eval.gate import EvaluationGateError
from .eval.models import EvalMode
from .eval.runner import run_evaluation
from .logging_setup import FileLogger
from .models import Confidence, Entry, EntryKind, EntryStatus, Evidence, EvidenceType, SourceType
from .normalization import (
    HARD_MAX_STATEMENT_CHARS,
    HARD_MAX_SUBJECT_KEYS,
    normalize_actor,
    normalize_claim_key,
    normalize_trusted_claim_key,
)
from .process_lock import (
    DatabaseLockError,
    DatabaseLockRole,
    DatabaseProcessLock,
)
from .retrieval import HybridRetriever, VectorRebuildError, build_retriever
from .server import create_mcp_server
from .store import EngramReader, EngramStore, StoreBusyError, StoreValidationError

EXIT_USAGE_OR_CONFIG = 2
EXIT_LOCAL_RESOURCE = 3
EXIT_EXTERNAL_DEPENDENCY = 4
EXIT_TRANSIENT_BUSY = 5
EXIT_PARTIAL_RESULT = 6
EXIT_INTERRUPTED = 130
DEBUG_ENVIRONMENT_KEY = "ENGRAM_DEBUG"
DEBUG_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
WINDOWS_ADDRESS_IN_USE = getattr(errno, "WSAEADDRINUSE", 10048)


class ServerBindError(OSError):
    """Raised when the configured HTTP endpoint cannot be reserved."""


class ConsolidationApplyError(RuntimeError):
    """Raised after an apply report records failed or stale propositions."""


class UpgradePreflightError(RuntimeError):
    """Raised when a read-only upgrade check finds incompatible persisted data."""


def main() -> None:
    """Parse one supported command and execute its isolated workflow."""
    parser = _build_parser()
    arguments = parser.parse_args()
    debug = bool(arguments.debug) or _environment_debug_enabled()
    logger: logging.Logger | None = None
    try:
        config = load_preflight_config() if arguments.command == "preflight" else load_config()
        logger = FileLogger(config.logging).configure()
        _dispatch(parser, arguments, config=config, logger=logger)
    except KeyboardInterrupt:
        if logger is not None:
            logger.info("Engram interrupted by operator")
        parser.exit(status=EXIT_INTERRUPTED, message="engram: interrupted\n")
    except (
        ConfigError,
        DatabaseError,
        DatabaseLockError,
        DatacronGatewayError,
        ConsolidationApplyError,
        UpgradePreflightError,
        EvaluationGateError,
        VectorRebuildError,
        OSError,
        sqlite3.Error,
        SQLiteVersionError,
        StoreBusyError,
        StoreValidationError,
    ) as exc:
        if debug:
            raise
        exit_code = _error_exit_code(exc)
        if logger is not None:
            _log_known_error(logger, exc)
        parser.exit(status=exit_code, message=f"engram: error: {_ascii_message(exc)}\n")


def _build_parser() -> argparse.ArgumentParser:
    """Build the complete command parser without opening application resources."""
    parser = argparse.ArgumentParser(prog="engram")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--debug",
        action="store_true",
        help=f"Show tracebacks for known failures (or set {DEBUG_ENVIRONMENT_KEY}=1)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the streamable HTTP MCP server")
    commands.add_parser("migrate", help="Migrate and validate storage offline")
    commands.add_parser(
        "preflight",
        help="Validate an existing database read-only before an upgrade",
    )
    commands.add_parser("reindex", help="Rebuild derived FTS and vector indexes")
    _add_trusted_command_parsers(commands)
    _add_evaluation_parser(commands)
    _add_consolidation_parser(commands)
    return parser


def _add_trusted_command_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add local attestation and lifecycle inspection commands."""
    attest = commands.add_parser("attest", help="Create a trusted local attestation")
    attest.add_argument("statement", help="Canonical statement to attest")
    attest.add_argument("kind", choices=tuple(kind.value for kind in EntryKind))
    attest.add_argument("scope", help="global, user, project/<id>, or session/<id>")
    attest.add_argument(
        "--subject-key",
        action="append",
        default=[],
        help="Retrieval/topic subject key (repeatable)",
    )
    attest.add_argument(
        "--source-type",
        choices=(SourceType.HUMAN.value, SourceType.TOOL_VERIFIED.value),
        default=SourceType.HUMAN.value,
    )
    attest.add_argument(
        "--confidence",
        choices=tuple(confidence.value for confidence in Confidence),
        default=Confidence.HIGH.value,
    )
    attest.add_argument(
        "--evidence",
        action="append",
        type=_parse_evidence,
        default=[],
        metavar="TYPE=REF",
        help="Evidence reference (repeatable)",
    )
    attest.add_argument("--valid-from", type=_parse_date, metavar="YYYY-MM-DD")
    attest.add_argument("--valid-until", type=_parse_date, metavar="YYYY-MM-DD")
    attest.add_argument("--observed-at", type=_parse_datetime, metavar="ISO-8601")
    attest.add_argument("--actor", help="Audit actor (default: attestation.default_actor)")
    attest.add_argument(
        "--claim-key",
        help=(
            "Semantic conflict-family identity; required for preference, decision, "
            "and fact attestations"
        ),
    )
    attest.add_argument(
        "--supersedes",
        action="append",
        default=[],
        metavar="ENTRY_ID",
        help="Entry replaced by this attestation (repeatable)",
    )
    supersede = commands.add_parser("supersede", help="Link a replacement entry")
    supersede.add_argument("--old", required=True, metavar="ENTRY_ID")
    supersede.add_argument("--new", required=True, metavar="ENTRY_ID")
    supersede.add_argument("--actor", help="Audit actor (default: attestation.default_actor)")
    classify = commands.add_parser(
        "classify",
        help="Assign a claim key to one trusted legacy entry",
    )
    classify.add_argument("entry_id", metavar="ENTRY_ID")
    classify.add_argument("--claim-key", required=True)
    classify.add_argument(
        "--actor",
        help="Audit actor (default: attestation.default_actor)",
    )
    list_command = commands.add_parser("list", help="List entries by lifecycle status")
    list_filter = list_command.add_mutually_exclusive_group(required=True)
    list_filter.add_argument(
        "--status",
        choices=tuple(status.value for status in EntryStatus),
    )
    list_filter.add_argument(
        "--unclassified",
        action="store_true",
        help="List active trusted legacy entries that still need a claim key",
    )


def _add_evaluation_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add deterministic evaluation arguments."""
    evaluate = commands.add_parser("eval", help="Run the deterministic evaluation suite")
    evaluate.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in EvalMode),
        default=EvalMode.BOTH.value,
        help="Retrieval mode to measure (default: both)",
    )
    evaluate.add_argument(
        "--out",
        type=Path,
        default=Path("local/eval"),
        help="Artifact directory (default: local/eval)",
    )


def _add_consolidation_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add reviewed Datacron consolidation arguments."""
    consolidate = commands.add_parser(
        "consolidate",
        help="Plan, apply, or check reviewed Datacron promotions",
    )
    consolidate_mode = consolidate.add_mutually_exclusive_group(required=True)
    consolidate_mode.add_argument(
        "--plan",
        action="store_true",
        help="Generate a deterministic human-review plan",
    )
    consolidate_mode.add_argument(
        "--apply",
        type=Path,
        metavar="PLAN",
        help="Apply approved decisions from a JSON plan",
    )
    consolidate_mode.add_argument(
        "--check-freshness",
        action="store_true",
        help="Mark promotions whose Datacron hash has diverged",
    )
    consolidate.add_argument(
        "--out",
        type=Path,
        help="JSON artifact path (mode-specific default under local/consolidation)",
    )


def _dispatch(  # noqa: C901
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    *,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    """Execute one parsed command."""
    if arguments.command == "serve":
        _serve(config=config, logger=logger)
    elif arguments.command == "migrate":
        _migrate(config=config, logger=logger)
    elif arguments.command == "preflight":
        _preflight(config=config, logger=logger)
    elif arguments.command == "reindex":
        _reindex(config=config, logger=logger)
    elif arguments.command == "attest":
        _attest(
            config=config,
            logger=logger,
            statement=arguments.statement,
            kind=EntryKind(arguments.kind),
            scope=arguments.scope,
            subject_keys=tuple(arguments.subject_key),
            source_type=SourceType(arguments.source_type),
            confidence=Confidence(arguments.confidence),
            evidence=tuple(arguments.evidence),
            valid_from=arguments.valid_from,
            valid_until=arguments.valid_until,
            observed_at=arguments.observed_at,
            actor=arguments.actor,
            claim_key=arguments.claim_key,
            supersedes=tuple(arguments.supersedes),
        )
    elif arguments.command == "supersede":
        _supersede(
            config=config,
            logger=logger,
            old_id=arguments.old,
            new_id=arguments.new,
            actor=arguments.actor,
        )
    elif arguments.command == "classify":
        _classify(
            config=config,
            logger=logger,
            entry_id=arguments.entry_id,
            claim_key=arguments.claim_key,
            actor=arguments.actor,
        )
    elif arguments.command == "list":
        _list_entries(
            config=config,
            status=(None if arguments.status is None else EntryStatus(arguments.status)),
            unclassified=bool(arguments.unclassified),
        )
    elif arguments.command == "eval":
        _evaluate(
            config=config,
            logger=logger,
            mode=EvalMode(arguments.mode),
            output_directory=arguments.out,
        )
    elif arguments.command == "consolidate":
        _consolidate(
            config=config,
            logger=logger,
            generate_plan=bool(arguments.plan),
            apply_path=arguments.apply,
            check_freshness=bool(arguments.check_freshness),
            output_path=arguments.out,
        )
    else:
        parser.error(f"Unsupported command: {arguments.command}")


def _serve(*, config: AppConfig, logger: logging.Logger) -> None:
    _ensure_server_bind_available(config.server.host, config.server.port)
    with (
        DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.DAEMON,
            command="serve",
        ) as process_lock,
        EngramStore(config, process_lock=process_lock) as store,
    ):
        server = create_mcp_server(config, store)
        logger.info(
            "Starting Engram MCP server on http://%s:%d%s",
            config.server.host,
            config.server.port,
            config.server.path,
        )
        try:
            server.run(transport="streamable-http")
        except SystemExit as exc:
            if exc.code in {None, 0}:
                return
            raise ServerBindError(
                f"Engram server could not start on {config.server.host}:{config.server.port}; "
                "verify server.host and choose an available server.port"
            ) from exc
        logger.info("Engram MCP server stopped")


def _ensure_server_bind_available(host: str, port: int) -> None:
    """Fail before opening SQLite when the configured HTTP endpoint is unavailable."""
    address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    address: tuple[str, int] | tuple[str, int, int, int]
    address = (host, port, 0, 0) if address_family == socket.AF_INET6 else (host, port)
    with socket.socket(address_family, socket.SOCK_STREAM) as listener:
        # sys.platform rather than os.name, unlike the other platform branches in
        # this project. Both are equivalent at run time, but only sys.platform is
        # narrowed by the type checker, and this branch is the one that reaches a
        # socket option which exists on Windows alone.
        if sys.platform == "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(address)
        except OSError as exc:
            if (
                exc.errno in {errno.EADDRINUSE, WINDOWS_ADDRESS_IN_USE}
                or getattr(exc, "winerror", None) == WINDOWS_ADDRESS_IN_USE
            ):
                raise ServerBindError(
                    f"Port {port} is already in use on {host}; stop the other service "
                    "or configure server.port"
                ) from exc
            reason = exc.strerror or str(exc)
            raise ServerBindError(
                f"Cannot bind Engram to {host}:{port}: {reason}; verify server.host and server.port"
            ) from exc


def _migrate(*, config: AppConfig, logger: logging.Logger) -> None:
    """Apply pending migrations under the offline-writer process lock."""
    with (
        DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="migrate",
        ) as process_lock,
        EngramStore(config, process_lock=process_lock),
    ):
        pass
    schema_version = latest_schema_version()
    logger.info("Database migration complete: schema_version=%d", schema_version)
    _write_json({"schema_version": schema_version})


def _preflight(*, config: AppConfig, logger: logging.Logger) -> None:
    """Validate the configured database without migrations or content writes."""
    with DatabaseProcessLock(
        config.database.path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="preflight",
    ):
        try:
            report = preflight_database(
                config.database,
                limits=config.limits,
            )
        except DatabaseError as exc:
            raise UpgradePreflightError(
                "Upgrade preflight rejected persisted data: "
                f"{exc}. No data was changed. Keep the verified backup and use "
                "Engram 2026.0730.01 or the version named by the diagnostic to review "
                "or export the reported row before retrying."
            ) from exc
    required_changes: list[str] = []
    if config.limits.max_statement_chars > HARD_MAX_STATEMENT_CHARS:
        required_changes.append(
            f"limits.max_statement_chars must be at most {HARD_MAX_STATEMENT_CHARS}"
        )
    if config.limits.max_subject_keys > HARD_MAX_SUBJECT_KEYS:
        required_changes.append(f"limits.max_subject_keys must be at most {HARD_MAX_SUBJECT_KEYS}")
    if required_changes:
        raise UpgradePreflightError(
            "Upgrade preflight accepted persisted data, but configuration changes are "
            f"required: {'; '.join(required_changes)}. No data was changed. Update the "
            "configuration and rerun preflight."
        )
    logger.info(
        "Upgrade preflight complete: compatible=true schema=%d target=%d",
        report.schema_version,
        report.target_schema_version,
    )
    _write_json(
        {
            "compatible": True,
            "database": str(config.database.path),
            "schema_version": report.schema_version,
            "target_schema_version": report.target_schema_version,
            "fts_rebuild_required": report.fts_rebuild_required,
            "vector_rebuild_required": report.vector_rebuild_required,
        }
    )


def _environment_debug_enabled() -> bool:
    value = os.environ.get(DEBUG_ENVIRONMENT_KEY, "")
    return value.strip().casefold() in DEBUG_TRUE_VALUES


def _error_exit_code(error: BaseException) -> int:
    if isinstance(error, ConfigError | StoreValidationError):
        return EXIT_USAGE_OR_CONFIG
    if isinstance(error, DatacronGatewayError | VectorRebuildError):
        return EXIT_EXTERNAL_DEPENDENCY
    if isinstance(error, StoreBusyError):
        return EXIT_TRANSIENT_BUSY
    if isinstance(error, ConsolidationApplyError | EvaluationGateError):
        return EXIT_PARTIAL_RESULT
    return EXIT_LOCAL_RESOURCE


def _log_known_error(logger: logging.Logger, error: BaseException) -> None:
    """Record one expected failure to file without duplicating the stderr message."""
    record = logger.makeRecord(
        logger.name,
        logging.ERROR,
        __file__,
        0,
        "%s",
        (error,),
        None,
    )
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and record.levelno >= handler.level:
            handler.handle(record)


def _ascii_message(error: BaseException) -> str:
    """Return a locale-independent stderr representation for one known failure."""
    return str(error).encode("ascii", errors="backslashreplace").decode("ascii")


def _attest(  # noqa: PLR0913
    *,
    config: AppConfig,
    logger: logging.Logger,
    statement: str,
    kind: EntryKind,
    scope: str,
    subject_keys: tuple[str, ...],
    source_type: SourceType,
    confidence: Confidence,
    evidence: tuple[Evidence, ...],
    valid_from: date | None,
    valid_until: date | None,
    observed_at: datetime | None,
    actor: str | None,
    claim_key: str | None,
    supersedes: tuple[str, ...],
) -> None:
    """Create one trusted entry and retire its explicitly replaced versions."""
    selected_actor = normalize_actor(config.attestation.default_actor if actor is None else actor)
    normalized_claim_key = normalize_trusted_claim_key(kind, claim_key)
    unique_supersedes = tuple(dict.fromkeys(supersedes))
    with (
        DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="attest",
        ) as process_lock,
        EngramStore(config, process_lock=process_lock) as store,
        store.write_access(config.server.write_wait_timeout_ms),
    ):
        entry = store.add_attested(
            kind=kind,
            scope=scope,
            statement=statement,
            source_type=source_type,
            actor=selected_actor,
            confidence=confidence,
            subject_keys=subject_keys,
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            evidence=evidence,
            claim_key=normalized_claim_key,
            supersedes=unique_supersedes,
        )
        stored_entry = store.get_entry(entry.id)
        if stored_entry is None:  # pragma: no cover - protected by the writer lock
            raise RuntimeError("Attested entry disappeared")
    logger.info("Attested entry %s as %s", stored_entry.id, selected_actor)
    _write_json(_entry_payload(stored_entry))


def _classify(
    *,
    config: AppConfig,
    logger: logging.Logger,
    entry_id: str,
    claim_key: str,
    actor: str | None,
) -> None:
    """Classify one trusted legacy entry through the offline writer path."""
    normalized_claim_key = normalize_claim_key(claim_key)
    selected_actor = normalize_actor(config.attestation.default_actor if actor is None else actor)
    with (
        DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="classify",
        ) as process_lock,
        EngramStore(config, process_lock=process_lock) as store,
        store.write_access(config.server.write_wait_timeout_ms),
    ):
        entry = store.classify_claim(
            entry_id,
            normalized_claim_key,
            actor=selected_actor,
        )
    logger.info("Classified entry %s as %s", entry.id, normalized_claim_key)
    _write_json(_entry_payload(entry))


def _supersede(
    *,
    config: AppConfig,
    logger: logging.Logger,
    old_id: str,
    new_id: str,
    actor: str | None,
) -> None:
    """Retire one entry in favor of an existing replacement."""
    selected_actor = normalize_actor(config.attestation.default_actor if actor is None else actor)
    with (
        DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="supersede",
        ) as process_lock,
        EngramStore(config, process_lock=process_lock) as store,
        store.write_access(config.server.write_wait_timeout_ms),
    ):
        store.supersede(old_id, new_id, actor=selected_actor)
        old_entry = store.get_entry(old_id)
        new_entry = store.get_entry(new_id)
        if old_entry is None or new_entry is None:  # pragma: no cover - validated by supersede
            raise RuntimeError("Supersession entries disappeared")
    logger.info("Superseded entry %s with %s as %s", old_id, new_id, selected_actor)
    _write_json(
        {
            "new": _entry_payload(new_entry),
            "old": _entry_payload(old_entry),
        }
    )


def _list_entries(
    *,
    config: AppConfig,
    status: EntryStatus | None = None,
    unclassified: bool = False,
) -> None:
    """Print entries matching one lifecycle filter as stable JSON."""
    if (status is None) == (not unclassified):
        raise StoreValidationError("list requires exactly one of status or unclassified")
    with EngramReader(config) as reader:
        entries = reader.list_entries()
        if unclassified:
            entries = tuple(
                entry
                for entry in entries
                if entry.status is EntryStatus.ACTIVE
                and entry.kind in {EntryKind.PREFERENCE, EntryKind.DECISION, EntryKind.FACT}
                and entry.claim_key is None
            )
        else:
            entries = tuple(entry for entry in entries if entry.status is status)
    _write_json([_entry_payload(entry) for entry in entries])


def _reindex(*, config: AppConfig, logger: logging.Logger) -> None:
    with DatabaseProcessLock(
        config.database.path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="reindex",
    ) as process_lock:
        store = EngramStore(config, process_lock=process_lock)
        try:
            store.rebuild_fts()
            retriever = build_retriever(config, store)
            if isinstance(retriever, HybridRetriever):
                vector_count = retriever.rebuild_vectors()
                logger.info("Reindex complete: FTS rebuilt, vectors=%d", vector_count)
            else:
                logger.info("Reindex complete: FTS rebuilt, vectors skipped in FTS mode")
        finally:
            store.close()


def _evaluate(
    *,
    config: AppConfig,
    logger: logging.Logger,
    mode: EvalMode,
    output_directory: Path,
) -> None:
    """Run the seeded suite and write its machine and human artifacts."""
    run_evaluation(
        config,
        mode=mode,
        output_directory=output_directory,
        logger=logger,
    )


def _consolidate(  # noqa: PLR0913
    *,
    config: AppConfig,
    logger: logging.Logger,
    generate_plan: bool,
    apply_path: Path | None,
    check_freshness: bool,
    output_path: Path | None,
) -> None:
    """Run one isolated consolidation workflow through Datacron MCP."""
    # Preserve the daemon/offline-writer diagnostic before starting Datacron,
    # without retaining the database lock across subprocess startup.
    with DatabaseProcessLock(
        config.database.path,
        role=DatabaseLockRole.OFFLINE_WRITER,
        command="consolidate",
    ):
        pass
    with (
        McpDatacronGateway(config.datacron) as gateway,
        DatabaseProcessLock(
            config.database.path,
            role=DatabaseLockRole.OFFLINE_WRITER,
            command="consolidate",
        ) as process_lock,
        EngramStore(config, process_lock=process_lock) as store,
    ):
        service = ConsolidationService(store, gateway, config.datacron)
        if generate_plan:
            target = output_path or Path("local/consolidation/plan.json")
            plan = service.plan()
            _write_artifacts(target, model_json(plan), render_plan_markdown(plan))
            logger.info("Consolidation plan written: %s", target)
            return
        if apply_path is not None:
            plan = ConsolidationPlan.model_validate_json(apply_path.read_text(encoding="utf-8"))
            report = service.apply(plan)
            target = output_path or apply_path.with_name("apply-report.json")
            _write_artifacts(target, model_json(report), render_apply_markdown(report))
            logger.info("Consolidation apply report written: %s", target)
            unresolved = tuple(
                outcome
                for outcome in report.outcomes
                if outcome.status in {ApplyStatus.FAILED, ApplyStatus.STALE}
            )
            if unresolved:
                raise ConsolidationApplyError(
                    f"consolidation apply completed with {len(unresolved)} failed or stale "
                    f"proposition(s); inspect {target} and generate a new plan"
                )
            return
        if check_freshness:
            freshness_report = service.check_freshness()
            target = output_path or Path("local/consolidation/freshness.json")
            _write_artifacts(
                target,
                model_json(freshness_report),
                render_freshness_markdown(freshness_report),
            )
            logger.info("Consolidation freshness report written: %s", target)


def _write_artifacts(json_path: Path, json_content: str, markdown_content: str) -> None:
    """Write one machine artifact and its same-name Markdown companion locally."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json_content, encoding="utf-8")
    json_path.with_suffix(".md").write_text(markdown_content, encoding="utf-8")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO date: {value}") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO datetime: {value}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("ISO datetime must include a timezone")
    return result


def _parse_evidence(value: str) -> Evidence:
    evidence_type, separator, reference = value.partition("=")
    if not separator or not reference.strip():
        raise argparse.ArgumentTypeError("Evidence must use TYPE=REF")
    try:
        normalized_type = EvidenceType(evidence_type.strip().casefold())
    except ValueError as exc:
        supported = ", ".join(item.value for item in EvidenceType)
        raise argparse.ArgumentTypeError(f"Unsupported evidence type; choose: {supported}") from exc
    return Evidence(type=normalized_type, ref=reference.strip())


def _entry_payload(entry: Entry) -> dict[str, object]:
    return {
        "confidence": entry.confidence.value,
        "canonical_key": entry.canonical_key,
        "claim_key": entry.claim_key,
        "datacron_hash": entry.datacron_hash,
        "datacron_ref": entry.datacron_ref,
        "evidence": [
            {"ref": evidence.ref, "type": evidence.type.value} for evidence in entry.evidence
        ],
        "expires_at": None if entry.expires_at is None else entry.expires_at.isoformat(),
        "id": entry.id,
        "idempotency_key": entry.idempotency_key,
        "kind": entry.kind.value,
        "observed_at": None if entry.observed_at is None else entry.observed_at.isoformat(),
        "promotion_state": entry.promotion_state.value,
        "recorded_at": entry.recorded_at.isoformat(),
        "scope": entry.scope,
        "source_type": entry.source_type.value,
        "stale": entry.stale,
        "statement": entry.statement,
        "status": entry.status.value,
        "subject_keys": list(entry.subject_keys),
        "supersedes": list(entry.supersedes),
        "synced_at": None if entry.synced_at is None else entry.synced_at.isoformat(),
        "valid_from": None if entry.valid_from is None else entry.valid_from.isoformat(),
        "valid_until": None if entry.valid_until is None else entry.valid_until.isoformat(),
        "writer_model": entry.writer_model,
    }


def _write_json(payload: object) -> None:
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    sys.stdout.write(f"{serialized}\n")
