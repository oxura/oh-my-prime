from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from prove import PromotionResult, VerificationReport
from workspace import Workspace


ChildStatus = Literal["running", "completed", "error", "missing"]


class ProofTreeError(RuntimeError):
    """Base error for speculative exploration runs."""


class ExplorationStartError(ProofTreeError):
    """Raised when a run starts only partially and must be recovered by id."""

    def __init__(self, run_id: str, message: str) -> None:
        self.run_id = run_id
        super().__init__(f"ProofTree run {run_id} failed to start completely: {message}")


class ExplorationTimeout(ProofTreeError):
    """Raised when candidate agents do not reach a terminal state in time."""


class NoVerifiedCandidate(ProofTreeError):
    """Raised when selection has no candidate that passed every hard gate."""


@dataclass(frozen=True, slots=True)
class Strategy:
    """A deliberately distinct implementation trajectory."""

    name: str
    instructions: str


@dataclass(frozen=True, slots=True)
class ProofTreeCandidate:
    """One isolated implementation branch and its child-agent identity."""

    id: str
    index: int
    strategy: Strategy
    workspace: Workspace
    child_id: str
    child_name: str
    child_session_dir: Path
    child_model: str
    child_cwd: Path


@dataclass(frozen=True, slots=True, order=True)
class SelectionScore:
    """Deterministic lower-is-better ordering among fully verified candidates."""

    optional_gate_failures: int
    files_changed: int
    patch_bytes: int
    verification_duration_ms: int
    candidate_index: int


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """Terminal child status plus verifier outcome for one candidate."""

    candidate: ProofTreeCandidate
    child_status: ChildStatus
    report: VerificationReport | None
    patch_bytes: int
    failure: str | None = None

    @property
    def verified(self) -> bool:
        return self.report is not None and self.report.verified

    @property
    def score(self) -> SelectionScore | None:
        if not self.verified or self.report is None:
            return None
        return SelectionScore(
            optional_gate_failures=sum(
                not result.required and not result.passed for result in self.report.gate_results
            ),
            files_changed=self.report.files_changed,
            patch_bytes=self.patch_bytes,
            verification_duration_ms=self.report.duration_ms,
            candidate_index=self.candidate.index,
        )


@dataclass(frozen=True, slots=True)
class ProofTreePromotion:
    """Verified promotion plus best-effort loser cleanup outcomes."""

    promotion: PromotionResult
    winner: CandidateEvaluation
    discarded_candidate_ids: tuple[str, ...]
    cleanup_failures: tuple[str, ...]
