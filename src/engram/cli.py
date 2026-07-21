# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Console entry point for the Engram server process."""

from __future__ import annotations

import argparse
import logging

from .config import AppConfig, load_config
from .logging_setup import FileLogger
from .server import create_mcp_server
from .store import EngramStore


def main() -> None:
    """Parse the single supported command and run streamable HTTP only."""
    parser = argparse.ArgumentParser(prog="engram")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the streamable HTTP MCP server")
    arguments = parser.parse_args()
    if arguments.command != "serve":
        parser.error("Only the serve command is supported")

    config = load_config()
    logger = FileLogger(config.logging).configure()
    _serve(config=config, logger=logger)


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
