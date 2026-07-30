# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Pure normalization and persisted-content identity rules."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence

from .models import PROJECT_STATE_CLAIM_KEY, EntryKind

SCOPE_PATTERN = re.compile(
    r"^(?:global|user|project/[a-z0-9][a-z0-9._-]*|session/[a-z0-9][a-z0-9._-]*)$"
)
ULID_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
ULID_LENGTH = 26
MAX_CLAIM_KEY_CHARS = 200
CANONICAL_KEY_HEX_LENGTH = 64


class StoreValidationError(ValueError):
    """Raised when entry content violates the storage contract."""


def required_text(value: object, field_name: str) -> str:
    """Return non-empty surrounding-whitespace-trimmed text."""
    if not isinstance(value, str):
        raise StoreValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise StoreValidationError(f"{field_name} must not be empty")
    return normalized


def normalize_scope(scope: str) -> str:
    """Normalize and validate one supported recall scope."""
    normalized = required_text(scope, "scope").casefold()
    if SCOPE_PATTERN.fullmatch(normalized) is None:
        raise StoreValidationError(f"Invalid scope: {scope}")
    return normalized


def normalize_statement(statement: str, maximum_chars: int | None) -> str:
    """Normalize statement Unicode and whitespace with an optional size bound."""
    normalized = " ".join(unicodedata.normalize("NFKC", statement).split())
    if not normalized:
        raise StoreValidationError("statement must not be empty")
    if maximum_chars is not None and len(normalized) > maximum_chars:
        raise StoreValidationError(
            f"statement exceeds configured limit of {maximum_chars} characters"
        )
    return normalized


def normalize_subject_keys(
    values: Sequence[str],
    maximum_keys: int | None,
) -> tuple[str, ...]:
    """Normalize, de-duplicate, and optionally bound subject keys."""
    if isinstance(values, str):
        raise StoreValidationError("subject_keys must be a sequence of strings")
    normalized: list[str] = []
    for value in values:
        item = required_text(value, "subject key").casefold()
        if item not in normalized:
            normalized.append(item)
    if maximum_keys is not None and len(normalized) > maximum_keys:
        raise StoreValidationError(f"subject_keys exceeds configured limit of {maximum_keys} items")
    return tuple(normalized)


def canonical_key(kind: EntryKind, scope: str, statement: str) -> str:
    """Return the exact normalized content identity hash."""
    payload = [kind.value, scope, statement.casefold()]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def generation_key(
    *,
    canonical_key: str,
    entry_id: str,
) -> str:
    """Return the stable fingerprint for one concrete entry generation."""
    payload = [canonical_key, entry_id]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_claim_key(value: str) -> str:
    """Normalize a semantic claim-family key."""
    normalized = " ".join(
        unicodedata.normalize("NFKC", required_text(value, "claim_key")).split()
    ).casefold()
    if len(normalized) > MAX_CLAIM_KEY_CHARS:
        raise StoreValidationError(f"claim_key exceeds limit of {MAX_CLAIM_KEY_CHARS} characters")
    return normalized


def normalize_entry_id(value: str, field_name: str = "entry_id") -> str:
    """Validate one canonical Crockford-base32 ULID."""
    normalized = required_text(value, field_name)
    if (
        len(normalized) != ULID_LENGTH
        or normalized[0] not in "01234567"
        or any(character not in ULID_ALPHABET for character in normalized)
    ):
        raise StoreValidationError(f"{field_name} must be a canonical ULID")
    return normalized


def normalize_sha256_hex(value: str, field_name: str) -> str:
    """Validate one lowercase SHA-256 hexadecimal digest."""
    normalized = required_text(value, field_name)
    if (
        len(normalized) != CANONICAL_KEY_HEX_LENGTH
        or normalized.casefold() != normalized
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise StoreValidationError(f"{field_name} must be lowercase SHA-256 hexadecimal")
    return normalized


def normalize_trusted_claim_key(
    kind: EntryKind,
    claim_key: str | None,
) -> str | None:
    """Normalize the claim classification required for trusted content."""
    if kind is EntryKind.PROJECT_STATE:
        if claim_key is None:
            return PROJECT_STATE_CLAIM_KEY
        normalized = normalize_claim_key(claim_key)
        if normalized != PROJECT_STATE_CLAIM_KEY:
            raise StoreValidationError(f"project_state claim_key must be {PROJECT_STATE_CLAIM_KEY}")
        return normalized
    if kind is EntryKind.EPISODE:
        if claim_key is not None:
            raise StoreValidationError("episode entries do not accept claim_key")
        return None
    if claim_key is None:
        raise StoreValidationError(f"{kind.value} attestation requires claim_key")
    return normalize_claim_key(claim_key)


def validate_persisted_content(  # noqa: C901, PLR0913
    *,
    kind: EntryKind,
    entry_id: str,
    scope: str,
    statement: str,
    subject_keys: Sequence[str],
    canonical: str,
    idempotency_key: str,
    claim_key: str | None,
    trusted: bool,
    max_statement_chars: int | None,
    max_subject_keys: int | None,
    allow_legacy_trusted_claim: bool = True,
) -> None:
    """Reject stored content whose normalized representation or identity drifted."""
    if normalize_entry_id(entry_id) != entry_id:
        raise StoreValidationError("stored entry id is not normalized")
    if normalize_scope(scope) != scope:
        raise StoreValidationError("stored scope is not normalized")
    if normalize_statement(statement, max_statement_chars) != statement:
        raise StoreValidationError("stored statement is not normalized")
    if normalize_subject_keys(subject_keys, max_subject_keys) != tuple(subject_keys):
        raise StoreValidationError("stored subject_keys are not normalized")
    expected_canonical = canonical_key(kind, scope, statement)
    if canonical != expected_canonical:
        raise StoreValidationError("stored canonical_key does not match entry content")
    normalize_sha256_hex(canonical, "canonical_key")
    normalize_sha256_hex(idempotency_key, "idempotency_key")
    if idempotency_key != canonical:
        expected_generation = generation_key(
            canonical_key=canonical,
            entry_id=entry_id,
        )
        if idempotency_key != expected_generation:
            raise StoreValidationError("stored idempotency_key does not match entry generation")
    if not trusted:
        if claim_key is not None:
            raise StoreValidationError("untrusted entry must not have claim_key")
        return
    if claim_key is None and allow_legacy_trusted_claim and kind is not EntryKind.EPISODE:
        return
    expected_claim = normalize_trusted_claim_key(kind, claim_key)
    if expected_claim != claim_key:
        raise StoreValidationError("stored claim_key is not normalized")
