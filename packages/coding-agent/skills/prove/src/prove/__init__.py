"""Acceptance contracts, deterministic verification, and evidence-ledger promotion."""

from __future__ import annotations

import os
from typing import Sequence

from workspace import PromotionResult, Workspace

from ._models import (
    AcceptanceContract,
    ContractError,
    ExecutionEvidence,
    Gate,
    GateResult,
    ProofError,
    Requirement,
    RequirementResult,
    VerificationError,
    VerificationFailed,
    VerificationReport,
)
from ._runtime import ProofRuntime, make_command_gate, make_reproducer_gate


_default_runtime = ProofRuntime()


def command(
    argv: Sequence[str],
    *,
    id: str,
    title: str | None = None,
    required: bool = True,
    proves: Sequence[str] = (),
    cwd: str = ".",
    timeout_seconds: float = 300.0,
    expectation: str = "success",
) -> Gate:
    """Declare a deterministic command gate without invoking a shell."""
    return make_command_gate(
        argv,
        id=id,
        title=title,
        required=required,
        proves=proves,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        expectation=expectation,
    )


def reproducer(
    argv: Sequence[str],
    *,
    id: str,
    title: str | None = None,
    required: bool = True,
    proves: Sequence[str] = (),
    cwd: str = ".",
    timeout_seconds: float = 300.0,
    baseline_expectation: str = "failure",
    candidate_expectation: str = "success",
) -> Gate:
    """Declare a command that must expose the bug before and pass after the fix."""
    return make_reproducer_gate(
        argv,
        id=id,
        title=title,
        required=required,
        proves=proves,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        baseline_expectation=baseline_expectation,
        candidate_expectation=candidate_expectation,
    )


async def contract(
    goal: str,
    *,
    requirements: Sequence[str | Requirement] = (),
    gates: Sequence[Gate] = (),
    invariants: Sequence[str] = (),
    constraints: Sequence[str] = (),
    repo: str | os.PathLike[str] = ".",
) -> AcceptanceContract:
    """Create and persist a commit-bound acceptance contract."""
    return await _default_runtime.contract(
        goal,
        requirements=requirements,
        gates=gates,
        invariants=invariants,
        constraints=constraints,
        repo=repo,
    )


async def load_contract(
    contract_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> AcceptanceContract:
    """Load and integrity-check a persisted contract."""
    return await _default_runtime.load_contract(contract_id, repo=repo)


async def run(
    target: str | Workspace,
    acceptance_contract: AcceptanceContract,
    *,
    fail_fast: bool = False,
) -> VerificationReport:
    """Run deterministic gates and persist an evidence ledger."""
    return await _default_runtime.run(target, acceptance_contract, fail_fast=fail_fast)


async def promote(target: str | Workspace, report: VerificationReport) -> PromotionResult:
    """Promote exactly the snapshot attested by a verified report."""
    return await _default_runtime.promote(target, report)


__all__ = [
    "AcceptanceContract",
    "ContractError",
    "ExecutionEvidence",
    "Gate",
    "GateResult",
    "ProofError",
    "ProofRuntime",
    "Requirement",
    "RequirementResult",
    "VerificationError",
    "VerificationFailed",
    "VerificationReport",
    "command",
    "contract",
    "load_contract",
    "promote",
    "reproducer",
    "run",
]
