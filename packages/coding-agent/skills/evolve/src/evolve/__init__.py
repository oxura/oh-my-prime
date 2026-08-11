"""Oh My Prime Evolution Lab: proof-backed continual harness learning."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
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
    ProcessEvidence,
    PromotionRejected,
    PromotionResult,
    ReplayCase,
    ReplayCaseResult,
    ReplayReport,
    ReplaySuite,
    RollbackResult,
    StoreStats,
)
from ._promotion import PromotionRuntime
from ._replay import ReplayRuntime

_default_lab = EvolutionLab()
_default_replay = ReplayRuntime(_default_lab)
_default_promotion = PromotionRuntime(_default_lab, _default_replay)


async def attest(
    artifact: str | os.PathLike[str],
    *,
    kind: str = "artifact",
    verifier: str = "",
) -> EvidenceRef:
    """Hash an observation artifact without claiming it is verified."""
    return await _default_lab.attest(artifact, kind=kind, verifier=verifier)


async def attest_verification(
    report: object,
    *,
    repo: str | os.PathLike[str] = ".",
) -> EvidenceRef:
    """Attest an integrity-checked successful Verifier Fabric report."""
    return await _default_lab.attest_verification(report, repo=repo)


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
    candidate = await _default_lab.contradict(
        candidate_id,
        evidence,
        reason=reason,
        repo=repo,
    )
    freshness = await _default_lab.freshness(candidate)
    if candidate.status == "active" and not freshness.fresh:
        await _default_promotion.rollback(candidate.id, reason=reason, repo=repo)
        return await _default_lab.get(candidate.id, repo=repo)
    return candidate


async def invalidate(
    candidate_id: str,
    *,
    reason: str,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    candidate = await _default_lab.invalidate(candidate_id, reason=reason, repo=repo)
    if candidate.status in {
        "rejected",
        "rolled_back",
        "expired",
        "invalidated",
    } and isinstance(candidate.metadata.get("shadow"), dict):
        await _default_promotion.cleanup_shadow(candidate, repo=repo)
    if (
        candidate.status == "active"
        and candidate.metadata.get("rollback_required") is True
    ):
        await _default_promotion.rollback(candidate.id, reason=reason, repo=repo)
        return await _default_lab.get(candidate.id, repo=repo)
    return candidate


async def decay(*, repo: str | os.PathLike[str] = ".") -> DecayReport:
    report = await _default_lab.decay(repo=repo)
    for status in ("rejected", "rolled_back", "expired", "invalidated"):
        candidates = await _default_lab.list(
            status=status,
            limit=2_000,
            repo=repo,
        )
        for candidate in candidates:
            if isinstance(candidate.metadata.get("shadow"), dict):
                await _default_promotion.cleanup_shadow(candidate, repo=repo)
    rolled_back = await _default_promotion.maintain(repo=repo)
    return replace(report, rolled_back_ids=rolled_back)


async def events(
    candidate_id: str,
    *,
    limit: int = 200,
    repo: str | os.PathLike[str] = ".",
) -> list[EvolutionEvent]:
    return await _default_lab.events(candidate_id, limit=limit, repo=repo)


async def stats(*, repo: str | os.PathLike[str] = ".") -> StoreStats:
    return await _default_lab.stats(repo=repo)


async def create_replay_suite(
    name: str,
    cases: Sequence[ReplayCase],
    *,
    require_improvement: bool = True,
    minimum_improvements: int = 1,
    repo: str | os.PathLike[str] = ".",
) -> ReplaySuite:
    return await _default_replay.create_suite(
        name,
        cases,
        require_improvement=require_improvement,
        minimum_improvements=minimum_improvements,
        repo=repo,
    )


async def load_replay_suite(
    suite_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> ReplaySuite:
    return await _default_replay.load_suite(suite_id, repo=repo)


async def run_replay(
    candidate_id: str,
    suite: ReplaySuite | str,
    *,
    phase: str = "replay",
    harness_state_dir: str | os.PathLike[str] | None = None,
    repo: str | os.PathLike[str] = ".",
) -> ReplayReport:
    """Compare baseline and candidate in isolated code and harness workspaces."""
    return await _default_replay.run(
        candidate_id,
        suite,
        phase=phase,
        harness_state_dir=harness_state_dir,
        repo=repo,
    )


async def load_replay(
    report_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> ReplayReport:
    return await _default_replay.load_report(report_id, repo=repo)


async def begin_shadow(
    candidate_id: str,
    replay_report: ReplayReport | str,
    *,
    harness_state_dir: str | os.PathLike[str] | None = None,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    return await _default_promotion.begin_shadow(
        candidate_id,
        replay_report,
        harness_state_dir=harness_state_dir,
        repo=repo,
    )


async def evaluate_shadow(
    candidate_id: str,
    shadow_report: ReplayReport | str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> MemoryCandidate:
    return await _default_promotion.evaluate_shadow(
        candidate_id, shadow_report, repo=repo
    )


async def promote(
    candidate_id: str,
    replay_report: ReplayReport | str,
    shadow_report: ReplayReport | str,
    *,
    harness_state_dir: str | os.PathLike[str] | None = None,
    repo: str | os.PathLike[str] = ".",
) -> PromotionResult:
    """Activate memory only after independent replay and shadow reports pass."""
    return await _default_promotion.promote(
        candidate_id,
        replay_report,
        shadow_report,
        harness_state_dir=harness_state_dir,
        repo=repo,
    )


async def rollback(
    candidate_id: str,
    *,
    reason: str,
    repo: str | os.PathLike[str] = ".",
) -> RollbackResult:
    return await _default_promotion.rollback(candidate_id, reason=reason, repo=repo)


async def recover(*, repo: str | os.PathLike[str] = ".") -> tuple[str, ...]:
    """Reconcile interrupted promotion and rollback journals."""
    return await _default_promotion.recover(repo=repo)


async def maintain(*, repo: str | os.PathLike[str] = ".") -> tuple[str, ...]:
    return await _default_promotion.maintain(repo=repo)


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
    "ProcessEvidence",
    "PromotionRejected",
    "PromotionResult",
    "PromotionRuntime",
    "ReplayCase",
    "ReplayCaseResult",
    "ReplayReport",
    "ReplayRuntime",
    "ReplaySuite",
    "RollbackResult",
    "StoreStats",
    "attest",
    "attest_verification",
    "begin_shadow",
    "confirm",
    "contradict",
    "create_replay_suite",
    "decay",
    "evaluate_shadow",
    "events",
    "freshness",
    "get",
    "invalidate",
    "load_replay",
    "load_replay_suite",
    "maintain",
    "memories",
    "promote",
    "propose_memory",
    "recover",
    "rollback",
    "run_replay",
    "stats",
]
