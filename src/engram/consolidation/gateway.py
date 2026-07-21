# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Datacron boundary plus an in-memory contract implementation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, Self, runtime_checkable

from .models import ContradictionSignal, NeighborSection, NoteView

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", flags=re.MULTILINE)


class DatacronGatewayError(RuntimeError):
    """Base error for Datacron transport or contract failures."""


class DatacronConflictError(DatacronGatewayError):
    """Raised when a note changed or appeared after planning."""


class DatacronVerificationError(DatacronGatewayError):
    """Raised when a write cannot be verified by rereading Datacron."""


@runtime_checkable
class DatacronGateway(Protocol):
    """Only boundary through which consolidation may access Datacron."""

    def get_note(self, rel_path: str) -> NoteView | None:
        """Read one current note or return ``None`` when absent."""
        ...

    def search_neighbors(
        self,
        subject_keys: Sequence[str],
        scope: str,
        limit: int,
    ) -> tuple[NeighborSection, ...]:
        """Return bounded durable sections relevant to one candidate."""
        ...

    def contradiction_scan(self) -> ContradictionSignal | None:
        """Return a complementary vault-wide contradiction signal."""
        ...

    def patch_section(
        self,
        rel_path: str,
        heading: str,
        new_content: str,
        expected_hash: str,
    ) -> str:
        """Patch one section under content-addressed compare-and-swap."""
        ...

    def create_note(self, rel_path: str, content: str) -> str:
        """Create one note without overwriting an existing path."""
        ...


class FakeDatacronGateway:
    """In-memory gateway with real hashes, CAS conflicts, and drift injection."""

    def __init__(
        self,
        notes: Mapping[str, str] | None = None,
        *,
        neighbors: Sequence[NeighborSection] = (),
        contradiction_signal: ContradictionSignal | None = None,
        drift_after_write: Sequence[str] = (),
    ) -> None:
        """Seed notes, neighbor results, signals, and optional post-write drift."""
        self._notes = dict(notes or {})
        self._neighbors = tuple(neighbors)
        self._contradiction_signal = contradiction_signal
        self._drift_after_write = set(drift_after_write)
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> Self:
        """Return the in-memory gateway."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Leave the in-memory gateway without resources to close."""

    def get_note(self, rel_path: str) -> NoteView | None:
        """Return a current content-addressed note."""
        self.calls.append(("get_note", rel_path))
        content = self._notes.get(rel_path)
        if content is None:
            return None
        return NoteView(
            rel_path=rel_path,
            title=_note_title(content, rel_path),
            content=content,
            content_hash=_content_hash(content),
        )

    def search_neighbors(
        self,
        subject_keys: Sequence[str],
        scope: str,
        limit: int,
    ) -> tuple[NeighborSection, ...]:
        """Return seeded neighbors sharing one requested subject key."""
        self.calls.append(("search_neighbors", scope))
        requested = set(subject_keys)
        matches = [
            neighbor
            for neighbor in self._neighbors
            if not requested or requested.intersection(neighbor.subject_keys)
        ]
        return tuple(matches[:limit])

    def contradiction_scan(self) -> ContradictionSignal | None:
        """Return the seeded complementary scan signal."""
        self.calls.append(("contradiction_scan", ""))
        return self._contradiction_signal

    def patch_section(
        self,
        rel_path: str,
        heading: str,
        new_content: str,
        expected_hash: str,
    ) -> str:
        """Replace a seeded section only when the expected hash matches."""
        self.calls.append(("patch_section", rel_path))
        current = self._notes.get(rel_path)
        if current is None:
            raise DatacronConflictError(f"Datacron note is missing: {rel_path}")
        if _content_hash(current) != expected_hash:
            raise DatacronConflictError(f"Datacron hash changed: {rel_path}")
        self._notes[rel_path] = _replace_section(current, heading, new_content)
        written_hash = _content_hash(self._notes[rel_path])
        self._inject_drift(rel_path)
        return written_hash

    def create_note(self, rel_path: str, content: str) -> str:
        """Create a note without overwriting an existing path."""
        self.calls.append(("create_note", rel_path))
        if rel_path in self._notes:
            raise DatacronConflictError(f"Datacron note already exists: {rel_path}")
        self._notes[rel_path] = content
        written_hash = _content_hash(content)
        self._inject_drift(rel_path)
        return written_hash

    def replace_note(self, rel_path: str, content: str) -> None:
        """Simulate an out-of-band mutation for CAS and freshness tests."""
        self._notes[rel_path] = content

    def _inject_drift(self, rel_path: str) -> None:
        if rel_path in self._drift_after_write:
            self._notes[rel_path] += "\nOut-of-band drift.\n"


def _replace_section(content: str, heading: str, new_content: str) -> str:
    matches = list(HEADING_PATTERN.finditer(content))
    target_index = next(
        (index for index, match in enumerate(matches) if match.group(2).strip() == heading.strip()),
        None,
    )
    if target_index is None:
        raise DatacronConflictError(f"Datacron heading is missing: {heading}")
    target = matches[target_index]
    level = len(target.group(1))
    end = len(content)
    for following in matches[target_index + 1 :]:
        if len(following.group(1)) <= level:
            end = following.start()
            break
    prefix = content[: target.end()].rstrip()
    suffix = content[end:].lstrip("\r\n")
    updated = f"{prefix}\n\n{new_content.strip()}\n"
    return updated if not suffix else f"{updated}\n{suffix}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _note_title(content: str, rel_path: str) -> str:
    match = HEADING_PATTERN.search(content)
    return match.group(2).strip() if match is not None else rel_path.rsplit("/", 1)[-1]
