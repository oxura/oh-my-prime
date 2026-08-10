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


@dataclass(frozen=True, slots=True)
class StoreStats:
    candidates: int
    events: int
    by_status: dict[str, int]
    by_category: dict[str, int]
