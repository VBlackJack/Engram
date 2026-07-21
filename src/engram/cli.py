# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Console commands for the Engram server, indexes, and evaluation."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import AppConfig, load_config
from .consolidation.mcp_gateway import McpDatacronGateway
from .consolidation.models import ConsolidationPlan
from .consolidation.report import (
    model_json,
    render_apply_markdown,
    render_freshness_markdown,
    render_plan_markdown,
)
from .consolidation.service import ConsolidationService
from .eval.models import EvalMode
from .eval.runner import run_evaluation
from .logging_setup import FileLogger
from .retrieval import HybridRetriever, build_retriever
from .server import create_mcp_server
from .store import EngramStore


def main() -> None:
    """Parse one supported command and execute its isolated workflow."""
    parser = argparse.ArgumentParser(prog="engram")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the streamable HTTP MCP server")
    commands.add_parser("reindex", help="Rebuild derived FTS and vector indexes")
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
    arguments = parser.parse_args()

    config = load_config()
    logger = FileLogger(config.logging).configure()
    if arguments.command == "serve":
        _serve(config=config, logger=logger)
    elif arguments.command == "reindex":
        _reindex(config=config, logger=logger)
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
    store = EngramStore(config)
    server = create_mcp_server(config, store)
    logger.info(
        "Starting Engram MCP server on http://%s:%d%s",
        config.server.host,
        config.server.port,
        config.server.path,
    )
    try:
        server.run(transport="streamable-http")
    except KeyboardInterrupt:
        logger.info("Engram MCP server stopped")
    finally:
        store.close()


def _reindex(*, config: AppConfig, logger: logging.Logger) -> None:
    store = EngramStore(config)
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
    with EngramStore(config) as store, McpDatacronGateway(config.datacron) as gateway:
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
