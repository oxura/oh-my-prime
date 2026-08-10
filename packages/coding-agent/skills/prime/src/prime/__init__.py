"""ProofTree: isolated speculative execution with verifier-gated promotion."""

from __future__ import annotations

import os
from typing import Sequence

from prove import AcceptanceContract

from ._models import (
    CandidateEvaluation,
    ExplorationStartError,
    ExplorationTimeout,
    NoVerifiedCandidate,
    ProofTreeCandidate,
    ProofTreeError,
    ProofTreePromotion,
    SelectionScore,
    Strategy,
)
from ._runtime import ExplorationRun, ProofTreeRuntime, Winner


_default_runtime = ProofTreeRuntime()


async def explore(
    goal: str,
    *,
    contract: AcceptanceContract,
    candidates: int = 3,
    strategies: Sequence[str | Strategy] | None = None,
    models: Sequence[str | None] = (),
) -> ExplorationRun:
    """Fork independent candidates and spawn one strategy-specific child per branch."""
    return await _default_runtime.explore(
        goal,
        contract=contract,
        candidates=candidates,
        strategies=strategies,
        models=models,
    )


async def load(
    run_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> ExplorationRun:
    """Recover a durable ProofTree run after kernel restart."""
    return await _default_runtime.load(run_id, repo=repo)


__all__ = [
    "CandidateEvaluation",
    "ExplorationRun",
    "ExplorationStartError",
    "ExplorationTimeout",
    "NoVerifiedCandidate",
    "ProofTreeCandidate",
    "ProofTreeError",
    "ProofTreePromotion",
    "ProofTreeRuntime",
    "SelectionScore",
    "Strategy",
    "Winner",
    "explore",
    "load",
]
