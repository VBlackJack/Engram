# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Console commands for the Engram server, indexes, and evaluation."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import AppConfig, load_config
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
