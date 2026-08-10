"""Oh My Prime Evolution Lab: proof-backed continual harness learning."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime

from ._memory import EvolutionLab
from ._models import (
    CandidateConflict,
    CandidateNotFound,
    DecayReport,
    EvidenceError,
    EvidenceRef,
    EvolutionError,
    EvolutionEvent,
    MemoryCandidate,
    MemoryFreshness,
    PromotionRejected,
    StoreStats,
)

_default_lab = EvolutionLab()


async def attest(
    artifact: str | os.PathLike[str],
    *,
    kind: str = "artifact",
    verifier: str = "",
) -> EvidenceRef:
    """Hash an observation artifact without claiming it is verified."""
    return await _default_lab.attest(artifact, kind=kind, verifier=verifier)


async def attest_verification(report: object) -> EvidenceRef:
    """Attest a persisted successful Verifier Fabric report."""
    return await _default_lab.attest_verification(report)


async def propose_memory(
    claim: str,
    *,
    title: str,
    category: str,
    evidence: Sequence[EvidenceRef] = (),
    confidence: float,
    scope: str = "local",
    path: str = "knowledge",
    target_id: str | None = None,
    applies_to: Sequence[str] = (),
    dependencies: Sequence[str] = (),
    expires_at: str | datetime | None = None,
    metadata: Mapping[str, object] | None = None,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    """Persist a proof-backed candidate without activating it in the harness."""
    return await _default_lab.propose_memory(
        claim,
        title=title,
        category=category,
        evidence=evidence,
        confidence=confidence,
        scope=scope,
        path=path,
        target_id=target_id,
        applies_to=applies_to,
        dependencies=dependencies,
        expires_at=expires_at,
        metadata=metadata,
        repo=repo,
    )


async def get(
    candidate_id: str, *, repo: str | os.PathLike[str] = "."
) -> MemoryCandidate:
    return await _default_lab.get(candidate_id, repo=repo)


async def memories(
    *,
    status: str | None = None,
    category: str | None = None,
    limit: int = 200,
    repo: str | os.PathLike[str] = ".",
) -> list[MemoryCandidate]:
    return await _default_lab.list(
        status=status, category=category, limit=limit, repo=repo
    )


async def freshness(candidate: MemoryCandidate) -> MemoryFreshness:
    return await _default_lab.freshness(candidate)


async def confirm(
    candidate_id: str,
    evidence: Sequence[EvidenceRef],
    *,
    reason: str,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    return await _default_lab.confirm(candidate_id, evidence, reason=reason, repo=repo)


async def contradict(
    candidate_id: str,
    evidence: Sequence[EvidenceRef],
    *,
    reason: str,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    return await _default_lab.contradict(
        candidate_id, evidence, reason=reason, repo=repo
    )


async def invalidate(
    candidate_id: str,
    *,
    reason: str,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    return await _default_lab.invalidate(candidate_id, reason=reason, repo=repo)


async def decay(*, repo: str | os.PathLike[str] = ".") -> DecayReport:
    return await _default_lab.decay(repo=repo)


async def events(
    candidate_id: str,
    *,
    limit: int = 200,
    repo: str | os.PathLike[str] = ".",
) -> list[EvolutionEvent]:
    return await _default_lab.events(candidate_id, limit=limit, repo=repo)


async def stats(*, repo: str | os.PathLike[str] = ".") -> StoreStats:
    return await _default_lab.stats(repo=repo)


__all__ = [
    "CandidateConflict",
    "CandidateNotFound",
    "DecayReport",
    "EvidenceError",
    "EvidenceRef",
    "EvolutionError",
    "EvolutionEvent",
    "EvolutionLab",
    "MemoryCandidate",
    "MemoryFreshness",
    "PromotionRejected",
    "StoreStats",
    "attest",
    "attest_verification",
    "confirm",
    "contradict",
    "decay",
    "events",
    "freshness",
    "get",
    "invalidate",
    "memories",
    "propose_memory",
    "stats",
]
