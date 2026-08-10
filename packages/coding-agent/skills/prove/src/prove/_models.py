from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


GateKind = Literal["command", "reproducer"]
Expectation = Literal["success", "failure"]
VerificationStatus = Literal["verified", "failed", "incomplete", "error"]
RequirementStatus = Literal["proved", "failed", "unproved"]


class ProofError(RuntimeError):
    """Base error for acceptance contracts and verification runs."""


class ContractError(ProofError):
    """Raised when an acceptance contract is malformed or stale."""


class VerificationError(ProofError):
    """Raised when a verification run cannot be executed safely."""


class VerificationFailed(ProofError):
    """Raised when code requires a verified report but receives another status."""


@dataclass(frozen=True, slots=True)
class Requirement:
    """One observable claim that a candidate must prove."""

    id: str
    description: str
    hard: bool = True


@dataclass(frozen=True, slots=True)
class Gate:
    """One deterministic command gate and the claims it can prove."""

    id: str
    title: str
    argv: tuple[str, ...]
    kind: GateKind = "command"
    required: bool = True
    proves: tuple[str, ...] = ()
    cwd: str = "."
    timeout_seconds: float = 300.0
    candidate_expectation: Expectation = "success"
    baseline_expectation: Expectation | None = None


@dataclass(frozen=True, slots=True)
class AcceptanceContract:
    """Repository- and commit-bound definition of completion."""

    id: str
    goal: str
    requirements: tuple[Requirement, ...]
    gates: tuple[Gate, ...]
    invariants: tuple[str, ...]
    constraints: tuple[str, ...]
    repo_root: Path
    common_dir: Path
    base_commit: str
    created_at: str
    digest: str


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """Immutable command evidence stored in the report ledger."""

    phase: Literal["baseline", "candidate"]
    argv: tuple[str, ...]
    cwd: Path
    expectation: Expectation
    passed: bool
    exit_code: int | None
    timed_out: bool
    started_at: str
    finished_at: str
    duration_ms: int
    stdout_path: Path
    stderr_path: Path
    stdout_sha256: str
    stderr_sha256: str
    stdout_preview: str
    stderr_preview: str


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome and evidence for one contract gate."""

    gate_id: str
    title: str
    required: bool
    passed: bool
    candidate: ExecutionEvidence
    baseline: ExecutionEvidence | None = None
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class RequirementResult:
    """Proof coverage for one contract requirement."""

    requirement_id: str
    description: str
    hard: bool
    status: RequirementStatus
    gate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Verifier decision plus a content-addressed evidence ledger."""

    id: str
    contract_id: str
    contract_digest: str
    workspace_id: str
    status: VerificationStatus
    patch_sha256: str
    files_changed: int
    gate_results: tuple[GateResult, ...]
    requirement_results: tuple[RequirementResult, ...]
    started_at: str
    finished_at: str
    duration_ms: int
    artifact_dir: Path
    report_path: Path
    limitations: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def required_gates_passed(self) -> int:
        return sum(result.required and result.passed for result in self.gate_results)

    def require_verified(self) -> "VerificationReport":
        if not self.verified:
            raise VerificationFailed(
                f"verification report {self.id} is {self.status!r}, not 'verified'"
            )
        return self
