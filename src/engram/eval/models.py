# Copyright 2026 Julien Bombled
# SPDX-License-Identifier: Apache-2.0

"""Typed evaluation tasks, outcomes, and machine-readable metrics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from engram.models import EntryKind, SourceType


class EvalMode(StrEnum):
    """Evaluation modes accepted by the console command."""

    FTS = "fts"
    HYBRID = "hybrid"
    BOTH = "both"


class RecallSubset(StrEnum):
    """Gold query subsets used by the P2 decision."""

    GLOBAL = "global"
    DEGRADED = "degraded"
    FTS_CONTRACT = "fts_contract"


class DegradationClass(StrEnum):
    """Versioned degraded-query families covered by the FTS contract."""

    NATURAL = "natural"
    ADVERSARIAL = "adversarial"


class CapsuleSection(StrEnum):
    """Capsule sections eligible for F1 gold grading."""

    CURRENT = "current"
    NEXT_ACTION = "next_action"
    RELEVANT = "relevant"


class ConsolidationClass(StrEnum):
    """Read-only classifications emitted before durable promotion."""

    NEW = "new"
    REDUNDANT = "redundant"
    CONTRADICTORY = "contradictory"
    UPDATE = "update"


class ModeStatus(StrEnum):
    """Availability of one requested retrieval mode."""

    MEASURED = "measured"
    UNAVAILABLE = "unavailable"


class P2Verdict(StrEnum):
    """Machine-issued P2 decisions."""

    HYBRID_RETAINED = "hybrid_retained"
    FTS_ADOPTED = "fts_adopted_hybrid_flag_kept"
    HYBRID_UNMEASURED = "fts_by_default_hybrid_unmeasured"


@dataclass(frozen=True, slots=True)
class SeedEntry:
    """One hand-authored entry loaded into a disposable database."""

    key: str
    kind: EntryKind
    scope: str
    statement: str
    subject_keys: tuple[str, ...]
    source_type: SourceType = SourceType.HUMAN
    writer_model: str | None = None


@dataclass(frozen=True, slots=True)
class RecallTask:
    """One gold recall query and its expected rendered section."""

    task_id: str
    query: str
    gold_key: str
    expected_section: CapsuleSection
    scope: str
    subset: RecallSubset
    degradation: DegradationClass | None = None


@dataclass(frozen=True, slots=True)
class ConflictTask:
    """One unresolved conflict family expected in F2."""

    task_id: str
    query: str
    entry_keys: tuple[str, str]
    scope: str


@dataclass(frozen=True, slots=True)
class ComplementTask:
    """One fact and decision pair that must remain complementary."""

    task_id: str
    query: str
    entry_keys: tuple[str, str]
    scope: str


@dataclass(frozen=True, slots=True)
class ConsolidationStatement:
    """A statement with explicit subject identity for pure classification."""

    statement_id: str
    statement: str
    subject_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsolidationTask:
    """Expected classification for one candidate statement."""

    candidate: ConsolidationStatement
    expected: ConsolidationClass


@dataclass(frozen=True, slots=True)
class ConsolidationProposal:
    """One deterministic read-only classification result."""

    candidate_id: str
    classification: ConsolidationClass


class GraderOutcome(BaseModel):
    """Code-based pass/fail result for one evaluation task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    passed: bool
    checks: dict[str, bool]
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


class FamilyMetrics(BaseModel):
    """Aggregate outcome for one evaluation family."""

    model_config = ConfigDict(extra="forbid")

    tasks: int
    passed: int
    failed: int
    pass_rate: float
    outcomes: list[GraderOutcome]


class RecallFamilyMetrics(FamilyMetrics):
    """F1 metrics split into direct and deliberately degraded queries."""

    global_tasks: int
    degraded_tasks: int
    fts_contract_tasks: int
    gold_in_capsule_rate: float
    global_gold_in_capsule_rate: float
    degraded_gold_in_capsule_rate: float
    fts_contract_gold_in_capsule_rate: float
    natural_degraded_gold_in_capsule_rate: float
    adversarial_degraded_gold_in_capsule_rate: float
    mean_gold_position: float | None
    budget_pass_rate: float
    complete_recall_rate: float
    global_complete_recall_rate: float
    fts_contract_complete_recall_rate: float
    incomplete_recall_count: int
    warning_counts: dict[str, int]
    fts_contract_warning_counts: dict[str, int]


class PerClassMetrics(BaseModel):
    """Precision and recall for one consolidation class."""

    model_config = ConfigDict(extra="forbid")

    precision: float
    recall: float
    support: int


class ConsolidationFamilyMetrics(FamilyMetrics):
    """F3 aggregate with exact per-class scores."""

    per_class: dict[ConsolidationClass, PerClassMetrics]


class LatencyMetrics(BaseModel):
    """Stable summary of one latency sample in milliseconds."""

    model_config = ConfigDict(extra="forbid")

    samples: int
    mean_ms: float
    p50_ms: float
    p95_ms: float


class SystemMetrics(BaseModel):
    """Non-blocking F5 system measurements for one retrieval mode."""

    model_config = ConfigDict(extra="forbid")

    remember_latency: LatencyMetrics
    recall_latency: LatencyMetrics
    concurrent_writes: int
    sqlite_busy_count: int
    wal_write_count: int
    wal_size_bytes: int
    fts_reindex_ms: float
    vector_reindex_ms: float | None
    vectors_indexed: int | None
    expire_ms: float
    expired_entries: int
    purge_ms: float
    purged_entries: int


class ModeMetrics(BaseModel):
    """All mode-dependent family results."""

    model_config = ConfigDict(extra="forbid")

    status: ModeStatus
    unavailable_reason: str | None = None
    f1_recall: RecallFamilyMetrics | None = None
    f2_contradiction: FamilyMetrics | None = None
    f4_poisoning: FamilyMetrics | None = None
    f5_system: SystemMetrics | None = None


class CorpusMetrics(BaseModel):
    """Seeded corpus dimensions included in the machine report."""

    model_config = ConfigDict(extra="forbid")

    version: str
    semantic_benchmark_version: str
    semantic_benchmark_sha256: str
    fts_contract_version: str
    fts_contract_sha256: str
    entries: int
    trusted_entries: int
    quarantined_entries: int
    recall_tasks: int
    global_tasks: int
    degraded_tasks: int
    fts_contract_tasks: int
    natural_degraded_tasks: int
    adversarial_degraded_tasks: int
    scopes: int
    kinds: int


class FtsContractThresholds(BaseModel):
    """Minimum release-quality thresholds for the deterministic FTS baseline."""

    model_config = ConfigDict(extra="forbid")

    minimum_global_gold_in_capsule_rate: float
    minimum_lexical_degraded_gold_in_capsule_rate: float
    minimum_budget_pass_rate: float
    minimum_global_complete_recall_rate: float
    minimum_lexical_complete_recall_rate: float
    minimum_contradiction_pass_rate: float
    minimum_poisoning_pass_rate: float
    maximum_recall_p95_ms: float


class FtsContractMetrics(BaseModel):
    """Measured FTS release gate serialized beside every evaluation report."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    checks: dict[str, bool]
    thresholds: FtsContractThresholds
    capsule_byte_budget: int
    capsule_budget_unit: str
    capsule_estimator_version: str
    call_result_byte_margin: int
    retrieval_config: dict[str, str | int]
    global_gold_in_capsule_rate: float
    lexical_degraded_gold_in_capsule_rate: float
    budget_pass_rate: float
    global_complete_recall_rate: float
    lexical_complete_recall_rate: float
    contradiction_pass_rate: float
    poisoning_pass_rate: float
    warning_counts: dict[str, int]
    recall_p95_ms: float


class P2Measures(BaseModel):
    """The three measurements that mechanically motivate the P2 verdict."""

    model_config = ConfigDict(extra="forbid")

    degraded_gain_points: float | None
    global_delta_points: float | None
    hybrid_recall_p95_ms: float | None


class P2Thresholds(BaseModel):
    """Fixed OM-03 thresholds, serialized to make the decision auditable."""

    model_config = ConfigDict(extra="forbid")

    minimum_degraded_gain_points: float
    minimum_global_delta_points: float
    maximum_hybrid_recall_p95_ms: float


class EvaluationMetrics(BaseModel):
    """Complete metrics.json schema."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 3
    requested_mode: EvalMode
    surface: str
    corpus: CorpusMetrics
    modes: dict[str, ModeMetrics]
    fts_contract: FtsContractMetrics
    f3_consolidation: ConsolidationFamilyMetrics
    p2_verdict: P2Verdict
    p2_measures: P2Measures
    p2_thresholds: P2Thresholds
