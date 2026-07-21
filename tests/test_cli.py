# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Derived-index command tests."""

from __future__ import annotations

import logging
import sqlite3

from engram.cli import _reindex
from engram.config import AppConfig
from engram.models import SourceType
from engram.retrieval import FtsRetriever, RetrievalRequest
from engram.store import EngramStore


def test_reindex_command_rebuilds_a_dropped_fts_table(app_config: AppConfig) -> None:
    with EngramStore(app_config) as store:
        entry = store.add_attested(
            kind="fact",
            scope="user",
            statement="The command rebuilds derived search.",
            source_type=SourceType.TOOL_VERIFIED,
        )

    connection = sqlite3.connect(app_config.database.path)
    try:
        connection.execute("DROP TABLE entries_fts")
        connection.commit()
    finally:
        connection.close()

    _reindex(config=app_config, logger=logging.getLogger("engram.test.cli"))

    with EngramStore(app_config) as store:
        result = FtsRetriever(store).retrieve(
            RetrievalRequest(
                query="derived search",
                scope=None,
                kinds=None,
                writer_model="test-client/1.0",
            )
        )
        assert result.matches[0].id == entry.id
