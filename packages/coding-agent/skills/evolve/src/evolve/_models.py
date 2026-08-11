from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MemoryCategory = Literal[
    "verified_knowledge",
    "hypothesis",
    "known_error",
    "temporary_observation",
]
EvolutionStatus = Literal[
    "candidate",
    "shadow",
    "active",
    "rejected",
    "rolled_back",
    "expired",
    "invalidated",
]
HarnessScope = Literal["local", "global"]
EvidenceKind = Literal[
    "artifact", "verification_report", "replay_report", "observation"
]


class EvolutionError(RuntimeError):
    """Base error for proof-backed continual harness evolution."""


class EvidenceError(EvolutionError):
    """Raised when evidence is absent, stale, malformed, or unverified."""


class CandidateNotFound(EvolutionError):
    """Raised when an evolution candidate does not exist."""


class CandidateConflict(EvolutionError):
    """Raised when an optimistic candidate update sees another revision."""


class PromotionRejected(EvolutionError):
    """Raised when a candidate has not earned promotion."""


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    uri: str
    sha256: str
    kind: EvidenceKind
    verified: bool
    verifier: str
    captured_at: str


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    schema_version: int
    id: str
    revision: int
    category: MemoryCategory
    title: str
    claim: str
    scope: HarnessScope
    path: str
    target_id: str
    applies_to: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    confidence: float
    confirmations: int
    contradictions: int
    status: EvolutionStatus
    repo_root: Path
    code_version: str
    dependency_hashes: dict[str, str]
    created_at: str
    updated_at: str
    expires_at: str | None
    metadata: dict[str, object]
    digest: str


@dataclass(frozen=True, slots=True)
class MemoryFreshness:
    fresh: bool
    effective_confidence: float
    expired: bool
    changed_dependencies: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    invalid_evidence: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvolutionEvent:
    id: int
    candidate_id: str
    action: str
    reason: str
    before_digest: str | None
    after_digest: str | None
    evidence: tuple[EvidenceRef, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class DecayReport:
    checked: int
    expired_ids: tuple[str, ...]
    invalidated_ids: tuple[str, ...]
    evaluated_at: str
    rolled_back_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoreStats:
    candidates: int
    events: int
    by_status: dict[str, int]
    by_category: dict[str, int]


ReplayPhase = Literal["replay", "shadow"]
ReplayStatus = Literal["passed", "failed", "error"]


@dataclass(frozen=True, slots=True)
class ReplayCase:
    id: str
    title: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 300.0
    expected_exit: int = 0
    stdout_contains: tuple[str, ...] = ()
    stdout_excludes: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class ReplaySuite:
    schema_version: int
    id: str
    name: str
    cases: tuple[ReplayCase, ...]
    require_improvement: bool
    minimum_improvements: int
    created_at: str
    digest: str


@dataclass(frozen=True, slots=True)
class ProcessEvidence:
    variant: Literal["baseline", "candidate"]
    passed: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_path: Path
    stderr_path: Path
    stdout_sha256: str
    stderr_sha256: str
    stdout_preview: str
    stderr_preview: str


@dataclass(frozen=True, slots=True)
class ReplayCaseResult:
    case_id: str
    title: str
    weight: float
    baseline: ProcessEvidence
    candidate: ProcessEvidence
    improved: bool
    regressed: bool


@dataclass(frozen=True, slots=True)
class ReplayReport:
    schema_version: int
    id: str
    candidate_id: str
    candidate_digest: str
    candidate_revision: int
    suite_id: str
    suite_digest: str
    source_revision: str
    baseline_harness_sha256: str
    candidate_harness_sha256: str
    phase: ReplayPhase
    status: ReplayStatus
    baseline_score: float
    candidate_score: float
    improvements: tuple[str, ...]
    regressions: tuple[str, ...]
    case_results: tuple[ReplayCaseResult, ...]
    artifact_dir: Path
    report_path: Path
    started_at: str
    finished_at: str
    duration_ms: int
    limitations: tuple[str, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class PromotionResult:
    id: str
    candidate_id: str
    candidate_revision: int
    replay_report_id: str
    shadow_report_id: str
    harness_state_path: Path
    target_id: str
    before_entry: dict[str, object] | None
    after_entry: dict[str, object]
    promoted_at: str
    digest: str


@dataclass(frozen=True, slots=True)
class RollbackResult:
    id: str
    candidate_id: str
    promotion_id: str
    harness_state_path: Path
    target_id: str
    restored_entry: dict[str, object] | None
    rolled_back_at: str
    reason: str
    digest: str
