# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Console entry point for the Engram server process."""

from __future__ import annotations

import argparse
import logging

from .config import AppConfig, load_config
from .logging_setup import FileLogger
from .retrieval import HybridRetriever, build_retriever
from .server import create_mcp_server
from .store import EngramStore


def main() -> None:
    """Parse the single supported command and run streamable HTTP only."""
    parser = argparse.ArgumentParser(prog="engram")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the streamable HTTP MCP server")
    commands.add_parser("reindex", help="Rebuild derived FTS and vector indexes")
    arguments = parser.parse_args()

    config = load_config()
    logger = FileLogger(config.logging).configure()
    if arguments.command == "serve":
        _serve(config=config, logger=logger)
    elif arguments.command == "reindex":
        _reindex(config=config, logger=logger)
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
