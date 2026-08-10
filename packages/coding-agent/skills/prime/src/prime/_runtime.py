from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Protocol, Sequence

from prove import AcceptanceContract, ProofRuntime, VerificationReport
from rlm import RLMSpawnHandle, rlm as default_rlm
from workspace import WorkspaceManager

from ._models import (
    CandidateEvaluation,
    ChildStatus,
    ExplorationStartError,
    ExplorationTimeout,
    NoVerifiedCandidate,
    ProofTreeCandidate,
    ProofTreeError,
    ProofTreePromotion,
    SelectionScore,
    Strategy,
)


_MANIFEST_VERSION = 1
_MIN_CANDIDATES = 2
_MAX_CANDIDATES = 8
_DEFAULT_WAIT_SECONDS = 3_600.0


_BUILTIN_STRATEGIES = (
    Strategy(
        "root-cause",
        "Trace the failure to its earliest incorrect assumption or state transition. Fix the source, not a downstream symptom.",
    ),
    Strategy(
        "minimal-fix",
        "Find the smallest complete change that satisfies every requirement while preserving unrelated behavior.",
    ),
    Strategy(
        "adversarial",
        "First search for counterexamples, races, partial failures, and rollback hazards, then implement a fix resilient to them.",
    ),
    Strategy(
        "architectural",
        "Reconsider the responsible abstraction and data flow. Use an architectural change only when it removes the root cause cleanly.",
    ),
    Strategy(
        "test-first",
        "Make the failure observable with a focused reproducer before changing production code, then implement against that evidence.",
    ),
    Strategy(
        "compatibility-first",
        "Prioritize public API, data, and behavioral compatibility while repairing the underlying defect.",
    ),
    Strategy(
        "concurrency-first",
        "Model interleavings and ownership explicitly; make unsafe states unrepresentable or atomically guarded.",
    ),
    Strategy(
        "simplification",
        "Remove accidental complexity and duplicated state so the required invariant follows from a simpler implementation.",
    ),
)
_BUILTIN_STRATEGIES_BY_NAME = {strategy.name: strategy for strategy in _BUILTIN_STRATEGIES}


class _RlmSubagent(Protocol):
    rlm_child_id: str
    status: str


class RlmClient(Protocol):
    async def __call__(self, prompt: str, **kwargs: object) -> RLMSpawnHandle: ...

    async def list_subagents(self) -> list[_RlmSubagent]: ...

    async def delete_subagent(self, target: str) -> object: ...


def _default_state_dir() -> Path:
    override = os.environ.get("OH_MY_PRIME_STATE_HOME")
    if override:
        return Path(override).expanduser() / "prooftree"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return root / "oh-my-prime" / "prooftree"


def _repo_key(common_dir: Path) -> str:
    canonical = os.path.normcase(str(common_dir.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "candidate"


def _strategy_payload(strategy: Strategy) -> dict[str, str]:
    return {"name": strategy.name, "instructions": strategy.instructions}


class ProofTreeRuntime:
    """Spawn isolated implementation trajectories and select only verified output."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        workspace_manager: WorkspaceManager | None = None,
        proof_runtime: ProofRuntime | None = None,
        rlm_client: RlmClient | None = None,
    ) -> None:
        if proof_runtime is not None and workspace_manager is None:
            workspace_manager = proof_runtime.workspace_manager
        self.workspace_manager = workspace_manager or WorkspaceManager()
        self.proof_runtime = proof_runtime or ProofRuntime(workspace_manager=self.workspace_manager)
        if self.proof_runtime.workspace_manager is not self.workspace_manager:
            raise ValueError("proof_runtime and ProofTree must share one WorkspaceManager")
        self.rlm_client = rlm_client or default_rlm
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else _default_state_dir().resolve()

    async def explore(
        self,
        goal: str,
        *,
        contract: AcceptanceContract,
        candidates: int = 3,
        strategies: Sequence[str | Strategy] | None = None,
        models: Sequence[str | None] = (),
    ) -> "ExplorationRun":
        """Fork independent workspaces and admit one strategy-specific child per branch."""
        if not isinstance(goal, str) or not goal.strip():
            raise ProofTreeError("goal must be a non-empty string")
        if not isinstance(contract, AcceptanceContract):
            raise TypeError("contract must be AcceptanceContract")
        persisted_contract = await self.proof_runtime.load_contract(contract.id, repo=contract.repo_root)
        if persisted_contract != contract:
            raise ProofTreeError("acceptance contract differs from its persisted digest-bound record")
        contract = persisted_contract
        if not isinstance(candidates, int) or isinstance(candidates, bool):
            raise TypeError("candidates must be int")
        if candidates < _MIN_CANDIDATES or candidates > _MAX_CANDIDATES:
            raise ProofTreeError(
                f"candidates must be between {_MIN_CANDIDATES} and {_MAX_CANDIDATES}"
            )
        selected_strategies = self._resolve_strategies(candidates, strategies)
        selected_models = self._resolve_models(candidates, models)
        run_id = uuid.uuid4().hex
        run = ExplorationRun(
            runtime=self,
            id=run_id,
            goal=goal.strip(),
            contract=contract,
            candidates=(),
            created_at=time.time(),
            state="starting",
        )
        run._persist()

        admitted: list[ProofTreeCandidate] = []
        try:
            for index, (strategy, model) in enumerate(
                zip(selected_strategies, selected_models, strict=True), start=1
            ):
                workspace = await self.workspace_manager.fork(
                    contract.repo_root,
                    name=f"{strategy.name}-{run_id[:8]}-{index}",
                    base=contract.base_commit,
                )
                prompt = self._candidate_prompt(goal.strip(), contract, strategy, workspace.path, index)
                kwargs: dict[str, object] = {
                    "name": f"proof-{_slug(strategy.name)[:28]}-{run_id[:6]}-{index}",
                    "cwd": str(workspace.path),
                }
                if model is not None:
                    kwargs["model"] = model
                try:
                    handle = await self.rlm_client(prompt, **kwargs)
                except Exception:
                    await self.workspace_manager.discard(workspace)
                    raise
                if handle.cwd.resolve() != workspace.path.resolve():
                    try:
                        await self.rlm_client.delete_subagent(handle.rlm_child_id)
                    finally:
                        await self.workspace_manager.discard(workspace)
                    raise ProofTreeError(
                        f"child {handle.rlm_child_id} was admitted in {handle.cwd}, not {workspace.path}"
                    )
                admitted.append(
                    ProofTreeCandidate(
                        id=uuid.uuid4().hex,
                        index=index,
                        strategy=strategy,
                        workspace=workspace,
                        child_id=handle.rlm_child_id,
                        child_name=handle.name,
                        child_session_dir=handle.session_dir,
                        child_model=handle.model,
                        child_cwd=handle.cwd,
                    )
                )
                run.candidates = tuple(admitted)
                run._persist()
        except Exception as error:
            run.state = "start_error"
            run.candidates = tuple(admitted)
            run._persist(error=str(error))
            raise ExplorationStartError(run_id, str(error)) from error

        run.state = "running"
        run._persist()
        return run

    async def load(
        self,
        run_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> "ExplorationRun":
        """Recover a run after kernel restart; verification is safely rerun."""
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise ProofTreeError(f"invalid ProofTree run id: {run_id!r}")
        common_dir = await self._git_common_dir(repo)
        path = self._manifest_path(common_dir, run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ProofTreeError(f"ProofTree run not found: {run_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ProofTreeError(f"cannot read ProofTree run {run_id}: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") != _MANIFEST_VERSION:
            raise ProofTreeError(f"unsupported ProofTree manifest: {run_id}")
        contract_id = payload.get("contract_id")
        if not isinstance(contract_id, str):
            raise ProofTreeError(f"ProofTree manifest has no contract id: {run_id}")
        contract = await self.proof_runtime.load_contract(contract_id, repo=repo)
        if payload.get("contract_digest") != contract.digest:
            raise ProofTreeError(f"ProofTree contract digest mismatch: {run_id}")
        if Path(str(payload.get("common_dir"))).resolve() != common_dir:
            raise ProofTreeError(f"ProofTree manifest belongs to a different Git repository: {run_id}")
        candidate_payloads = payload.get("candidates")
        if not isinstance(candidate_payloads, list):
            raise ProofTreeError(f"ProofTree manifest has invalid candidates: {run_id}")
        recovered: list[ProofTreeCandidate] = []
        try:
            for item in candidate_payloads:
                if not isinstance(item, dict):
                    raise TypeError
                workspace = await self.workspace_manager.get(item["workspace_id"], repo=repo)
                strategy_payload = item["strategy"]
                if not isinstance(strategy_payload, dict):
                    raise TypeError
                child_cwd = Path(item["child_cwd"]).resolve()
                if child_cwd != workspace.path.resolve():
                    raise ProofTreeError(
                        f"candidate {item.get('id')} child cwd no longer matches its workspace"
                    )
                recovered.append(
                    ProofTreeCandidate(
                        id=item["id"],
                        index=item["index"],
                        strategy=Strategy(
                            name=strategy_payload["name"],
                            instructions=strategy_payload["instructions"],
                        ),
                        workspace=workspace,
                        child_id=item["child_id"],
                        child_name=item["child_name"],
                        child_session_dir=Path(item["child_session_dir"]),
                        child_model=item["child_model"],
                        child_cwd=child_cwd,
                    )
                )
            goal = payload["goal"]
            state = payload["state"]
            created_at = float(payload["created_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProofTreeError(f"ProofTree manifest has invalid candidate fields: {run_id}") from error
        if not isinstance(goal, str) or not isinstance(state, str):
            raise ProofTreeError(f"ProofTree manifest has invalid run fields: {run_id}")
        return ExplorationRun(
            runtime=self,
            id=run_id,
            goal=goal,
            contract=contract,
            candidates=tuple(recovered),
            created_at=created_at,
            state=state,
            promoted_candidate_id=(
                payload.get("promoted_candidate_id")
                if isinstance(payload.get("promoted_candidate_id"), str)
                else None
            ),
        )

    def _resolve_strategies(
        self,
        candidates: int,
        strategies: Sequence[str | Strategy] | None,
    ) -> tuple[Strategy, ...]:
        values: Sequence[str | Strategy] = strategies or _BUILTIN_STRATEGIES[:candidates]
        if len(values) != candidates:
            raise ProofTreeError("strategies must contain exactly one entry per candidate")
        resolved: list[Strategy] = []
        names: set[str] = set()
        for value in values:
            if isinstance(value, str):
                strategy = _BUILTIN_STRATEGIES_BY_NAME.get(value)
                if strategy is None:
                    raise ProofTreeError(
                        f"unknown strategy {value!r}; pass a Strategy for custom instructions"
                    )
            elif isinstance(value, Strategy):
                if not value.name.strip() or not value.instructions.strip():
                    raise ProofTreeError("custom strategies require non-empty name and instructions")
                strategy = Strategy(value.name.strip(), value.instructions.strip())
            else:
                raise TypeError("strategies must contain names or Strategy values")
            if strategy.name in names:
                raise ProofTreeError(f"strategy names must be unique: {strategy.name!r}")
            names.add(strategy.name)
            resolved.append(strategy)
        return tuple(resolved)

    @staticmethod
    def _resolve_models(candidates: int, models: Sequence[str | None]) -> tuple[str | None, ...]:
        if not models:
            return (None,) * candidates
        if len(models) == 1:
            models = tuple(models) * candidates
        if len(models) != candidates:
            raise ProofTreeError("models must be empty, a single selector, or one selector per candidate")
        normalized: list[str | None] = []
        for model in models:
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise ProofTreeError("model selectors must be non-empty strings or None")
            normalized.append(model.strip() if isinstance(model, str) else None)
        return tuple(normalized)

    @staticmethod
    def _candidate_prompt(
        goal: str,
        contract: AcceptanceContract,
        strategy: Strategy,
        workspace: Path,
        index: int,
    ) -> str:
        requirements = "\n".join(
            f"- {requirement.id}: {requirement.description} ({'hard' if requirement.hard else 'soft'})"
            for requirement in contract.requirements
        )
        gates = "\n".join(
            f"- {gate.id}: {json.dumps(gate.argv)}; proves={','.join(gate.proves)}"
            for gate in contract.gates
        ) or "- No automatic gates; create objective evidence without editing the contract."
        return f"""You are candidate {index} in an isolated ProofTree exploration.

Goal:
{goal}

Strategy ({strategy.name}):
{strategy.instructions}

Acceptance requirements:
{requirements}

Verifier gates:
{gates}

Workspace invariant:
- Your complete session cwd is {workspace}.
- Read, edit, and run commands only inside this workspace.
- Do not switch branches, edit Git administrative files, or touch the source worktree.
- Implement a complete solution; do not merely propose one.
- Do not weaken, delete, or rewrite verifier inputs to make gates pass.
- Leave the final candidate on disk. ProofTree will independently run the contract after you finish.

Contract digest: {contract.digest}
"""

    def _manifest_path(self, common_dir: Path, run_id: str) -> Path:
        return self.state_dir / _repo_key(common_dir) / "runs" / f"{run_id}.json"

    async def _git_common_dir(self, repo: str | os.PathLike[str]) -> Path:
        candidate = Path(repo).expanduser().resolve()
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(candidate),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise ProofTreeError(
                "cannot resolve Git repository: "
                + (stderr.decode("utf-8", errors="replace").strip() or "unknown Git error")
            )
        return Path(stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()


class ExplorationRun:
    """Live or recovered ProofTree run with deterministic verification and selection."""

    def __init__(
        self,
        *,
        runtime: ProofTreeRuntime,
        id: str,
        goal: str,
        contract: AcceptanceContract,
        candidates: tuple[ProofTreeCandidate, ...],
        created_at: float,
        state: str,
        promoted_candidate_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.id = id
        self.goal = goal
        self.contract = contract
        self.candidates = candidates
        self.created_at = created_at
        self.state = state
        self.promoted_candidate_id = promoted_candidate_id
        self._evaluations: tuple[CandidateEvaluation, ...] | None = None

    async def statuses(self) -> dict[str, ChildStatus]:
        """Return candidate-id keyed child lifecycle states from the host registry."""
        subagents = await self.runtime.rlm_client.list_subagents()
        by_child = {subagent.rlm_child_id: subagent.status for subagent in subagents}
        statuses: dict[str, ChildStatus] = {}
        for candidate in self.candidates:
            value = by_child.get(candidate.child_id)
            if value == "running":
                status: ChildStatus = "running"
            elif value == "completed":
                status = "completed"
            elif value == "error":
                status = "error"
            else:
                status = "missing"
            statuses[candidate.id] = status
        return statuses

    async def wait(
        self,
        *,
        timeout_seconds: float = _DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, ChildStatus]:
        """Wait until every admitted candidate child completes or errors."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            statuses = await self.statuses()
            if all(status in {"completed", "error", "missing"} for status in statuses.values()):
                self.state = "implemented"
                self._persist(statuses=statuses)
                return statuses
            if time.monotonic() >= deadline:
                self._persist(statuses=statuses, error="candidate wait timed out")
                running = [
                    candidate.strategy.name
                    for candidate in self.candidates
                    if statuses[candidate.id] == "running"
                ]
                raise ExplorationTimeout(
                    f"ProofTree run {self.id} timed out; still running: {', '.join(running)}"
                )
            await asyncio.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))

    async def verify(
        self,
        *,
        timeout_seconds: float = _DEFAULT_WAIT_SECONDS,
        max_parallel: int = 2,
    ) -> tuple[CandidateEvaluation, ...]:
        """Wait for implementations, then apply the same contract independently to each."""
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
            raise ValueError("max_parallel must be a positive integer")
        statuses = await self.wait(timeout_seconds=timeout_seconds)
        semaphore = asyncio.Semaphore(max_parallel)

        async def evaluate(candidate: ProofTreeCandidate) -> CandidateEvaluation:
            child_status = statuses[candidate.id]
            if child_status != "completed":
                return CandidateEvaluation(
                    candidate=candidate,
                    child_status=child_status,
                    report=None,
                    patch_bytes=0,
                    failure=f"child status is {child_status}",
                )
            async with semaphore:
                try:
                    report = await self.runtime.proof_runtime.run(candidate.workspace, self.contract)
                    snapshot = await self.runtime.workspace_manager.diff(candidate.workspace)
                    return CandidateEvaluation(
                        candidate=candidate,
                        child_status=child_status,
                        report=report,
                        patch_bytes=len(snapshot.patch),
                        failure=None if report.verified else f"verification status is {report.status}",
                    )
                except Exception as error:
                    return CandidateEvaluation(
                        candidate=candidate,
                        child_status=child_status,
                        report=None,
                        patch_bytes=0,
                        failure=f"verifier error: {error}",
                    )

        self._evaluations = tuple(await asyncio.gather(*(evaluate(candidate) for candidate in self.candidates)))
        self.state = "verified"
        self._persist(statuses=statuses)
        return self._evaluations

    async def best_verified(
        self,
        *,
        timeout_seconds: float = _DEFAULT_WAIT_SECONDS,
        max_parallel: int = 2,
    ) -> "Winner":
        """Select the smallest deterministic score among fully verified candidates."""
        evaluations = self._evaluations or await self.verify(
            timeout_seconds=timeout_seconds,
            max_parallel=max_parallel,
        )
        verified = [evaluation for evaluation in evaluations if evaluation.verified]
        if not verified:
            reasons = "; ".join(
                f"{evaluation.candidate.strategy.name}: {evaluation.failure or 'not verified'}"
                for evaluation in evaluations
            )
            self.state = "no_verified_candidate"
            self._persist(error=reasons)
            raise NoVerifiedCandidate(
                f"ProofTree run {self.id} produced no verified candidate. {reasons}"
            )
        scored: list[tuple[SelectionScore, CandidateEvaluation]] = []
        for evaluation in verified:
            score = evaluation.score
            if score is None:
                raise ProofTreeError("verified candidate has no selection score")
            scored.append((score, evaluation))
        selected = min(scored, key=lambda item: item[0])[1]
        self.state = "selected"
        self._persist(selected_candidate_id=selected.candidate.id)
        return Winner(self, selected)

    async def promote_best(
        self,
        *,
        timeout_seconds: float = _DEFAULT_WAIT_SECONDS,
        max_parallel: int = 2,
        discard_losers: bool = True,
    ) -> ProofTreePromotion:
        """Verify, select, and promote the winner in one guarded call."""
        winner = await self.best_verified(
            timeout_seconds=timeout_seconds,
            max_parallel=max_parallel,
        )
        return await winner.promote(discard_losers=discard_losers)

    async def discard(self) -> tuple[str, ...]:
        """Discard all terminal candidate workspaces without promoting any of them."""
        statuses = await self.statuses()
        running = [candidate.strategy.name for candidate in self.candidates if statuses[candidate.id] == "running"]
        if running:
            raise ProofTreeError(
                "cannot discard workspaces while candidate agents are running: " + ", ".join(running)
            )
        discarded: list[str] = []
        for candidate in self.candidates:
            workspace = await self.runtime.workspace_manager.get(candidate.workspace)
            if workspace.status != "discarded":
                await self.runtime.workspace_manager.discard(workspace)
                discarded.append(candidate.id)
            try:
                await self.runtime.rlm_client.delete_subagent(candidate.child_id)
            except Exception:
                pass
        self.state = "discarded"
        self._persist(statuses=statuses)
        return tuple(discarded)

    def _persist(
        self,
        *,
        statuses: dict[str, ChildStatus] | None = None,
        selected_candidate_id: str | None = None,
        error: str | None = None,
    ) -> None:
        evaluations = {evaluation.candidate.id: evaluation for evaluation in self._evaluations or ()}
        payload = {
            "version": _MANIFEST_VERSION,
            "id": self.id,
            "goal": self.goal,
            "contract_id": self.contract.id,
            "contract_digest": self.contract.digest,
            "repo_root": str(self.contract.repo_root),
            "common_dir": str(self.contract.common_dir),
            "created_at": self.created_at,
            "state": self.state,
            "promoted_candidate_id": self.promoted_candidate_id,
            "selected_candidate_id": selected_candidate_id,
            "error": error,
            "candidates": [
                {
                    "id": candidate.id,
                    "index": candidate.index,
                    "strategy": _strategy_payload(candidate.strategy),
                    "workspace_id": candidate.workspace.id,
                    "workspace_path": str(candidate.workspace.path),
                    "child_id": candidate.child_id,
                    "child_name": candidate.child_name,
                    "child_session_dir": str(candidate.child_session_dir),
                    "child_model": candidate.child_model,
                    "child_cwd": str(candidate.child_cwd),
                    "child_status": statuses.get(candidate.id) if statuses else None,
                    "evaluation": (
                        {
                            "status": evaluation.report.status if evaluation.report else None,
                            "report_path": str(evaluation.report.report_path) if evaluation.report else None,
                            "patch_sha256": evaluation.report.patch_sha256 if evaluation.report else None,
                            "patch_bytes": evaluation.patch_bytes,
                            "failure": evaluation.failure,
                        }
                        if (evaluation := evaluations.get(candidate.id))
                        else None
                    ),
                }
                for candidate in self.candidates
            ],
        }
        _atomic_json(self.runtime._manifest_path(self.contract.common_dir, self.id), payload)


class Winner:
    """A fully verified candidate selected without implementer self-reporting."""

    def __init__(self, run: ExplorationRun, evaluation: CandidateEvaluation) -> None:
        self.run = run
        self.evaluation = evaluation

    @property
    def candidate(self) -> ProofTreeCandidate:
        return self.evaluation.candidate

    @property
    def report(self) -> VerificationReport:
        report = self.evaluation.report
        if report is None:
            raise ProofTreeError("selected candidate has no verification report")
        return report.require_verified()

    @property
    def score(self) -> SelectionScore:
        score = self.evaluation.score
        if score is None:
            raise ProofTreeError("selected candidate has no selection score")
        return score

    async def promote(self, *, discard_losers: bool = True) -> ProofTreePromotion:
        """Promote the attested snapshot and optionally discard every losing branch."""
        if self.run.promoted_candidate_id is not None:
            raise ProofTreeError(
                f"ProofTree run {self.run.id} already promoted candidate {self.run.promoted_candidate_id}"
            )
        promotion = await self.run.runtime.proof_runtime.promote(
            self.candidate.workspace,
            self.report,
        )
        self.run.promoted_candidate_id = self.candidate.id
        self.run.state = "promoted"
        discarded: list[str] = []
        cleanup_failures: list[str] = []
        if discard_losers:
            for candidate in self.run.candidates:
                if candidate.id == self.candidate.id:
                    continue
                try:
                    await self.run.runtime.workspace_manager.discard(candidate.workspace)
                    discarded.append(candidate.id)
                except Exception as error:
                    cleanup_failures.append(f"{candidate.id}: {error}")
        self.run._persist(selected_candidate_id=self.candidate.id)
        return ProofTreePromotion(
            promotion=promotion,
            winner=self.evaluation,
            discarded_candidate_ids=tuple(discarded),
            cleanup_failures=tuple(cleanup_failures),
        )

