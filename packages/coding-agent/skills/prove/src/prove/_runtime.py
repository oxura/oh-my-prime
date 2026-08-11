from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from workspace import (
    FileChange,
    PromotionResult,
    Workspace,
    WorkspaceDiff,
    WorkspaceManager,
)

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
    VerificationReport,
    WorkspaceDiffAttestation,
)

_CONTRACT_VERSION = 1
_REPORT_VERSION = 2
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PREVIEW_BYTES = 4_000
_MAX_TIMEOUT_SECONDS = 86_400.0
_SAFE_ENV_KEYS = (
    "COLORTERM",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TERM",
    "VIRTUAL_ENV",
    "WINDIR",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_state_dir() -> Path:
    override = os.environ.get("OH_MY_PRIME_STATE_HOME")
    if override:
        return Path(override).expanduser() / "proofs"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = (
        Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    )
    return root / "oh-my-prime" / "proofs"


def _absolute_path(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError
    return Path(os.path.abspath(Path(value).expanduser()))


def _repo_key(common_dir: Path) -> str:
    canonical = os.path.normcase(str(common_dir.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ContractError(f"{label} must match {_ID_PATTERN.pattern!r}")
    return value


def _normalize_texts(values: Iterable[str], label: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"{label} entries must be non-empty strings")
        normalized.append(value.strip())
    return tuple(normalized)


def _contract_payload(
    contract: AcceptanceContract, *, include_digest: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": _CONTRACT_VERSION,
        "id": contract.id,
        "goal": contract.goal,
        "requirements": [
            {
                "id": requirement.id,
                "description": requirement.description,
                "hard": requirement.hard,
            }
            for requirement in contract.requirements
        ],
        "gates": [
            {
                "id": gate.id,
                "title": gate.title,
                "argv": list(gate.argv),
                "kind": gate.kind,
                "required": gate.required,
                "proves": list(gate.proves),
                "cwd": gate.cwd,
                "timeout_seconds": gate.timeout_seconds,
                "candidate_expectation": gate.candidate_expectation,
                "baseline_expectation": gate.baseline_expectation,
            }
            for gate in contract.gates
        ],
        "invariants": list(contract.invariants),
        "constraints": list(contract.constraints),
        "repo_root": str(contract.repo_root),
        "common_dir": str(contract.common_dir),
        "base_commit": contract.base_commit,
        "created_at": contract.created_at,
    }
    if include_digest:
        payload["digest"] = contract.digest
    return payload


def _evidence_payload(evidence: ExecutionEvidence) -> dict[str, object]:
    return {
        "phase": evidence.phase,
        "argv": list(evidence.argv),
        "cwd": str(evidence.cwd),
        "expectation": evidence.expectation,
        "passed": evidence.passed,
        "exit_code": evidence.exit_code,
        "timed_out": evidence.timed_out,
        "started_at": evidence.started_at,
        "finished_at": evidence.finished_at,
        "duration_ms": evidence.duration_ms,
        "stdout_path": str(evidence.stdout_path),
        "stderr_path": str(evidence.stderr_path),
        "stdout_sha256": evidence.stdout_sha256,
        "stderr_sha256": evidence.stderr_sha256,
        "stdout_preview": evidence.stdout_preview,
        "stderr_preview": evidence.stderr_preview,
    }


def _diff_evidence_payload(
    attestation: WorkspaceDiffAttestation,
) -> dict[str, object]:
    return {
        "workspace_id": attestation.workspace_id,
        "base_commit": attestation.base_commit,
        "patch_path": str(attestation.patch_path),
        "patch_sha256": attestation.patch_sha256,
        "files": [
            {
                "status": change.status,
                "path": change.path,
                "previous_path": change.previous_path,
            }
            for change in attestation.files
        ],
    }


def _diff_payload(attestation: WorkspaceDiffAttestation) -> dict[str, object]:
    return {
        "evidence_path": str(attestation.evidence_path),
        "evidence_sha256": attestation.evidence_sha256,
        **_diff_evidence_payload(attestation),
    }


def _report_payload(
    report: VerificationReport, *, include_digest: bool = True
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": _REPORT_VERSION,
        "id": report.id,
        "contract_id": report.contract_id,
        "contract_digest": report.contract_digest,
        "workspace_id": report.workspace_id,
        "status": report.status,
        "patch_sha256": report.patch_sha256,
        "files_changed": report.files_changed,
        "initial_diff": _diff_payload(report.initial_diff),
        "final_diff": _diff_payload(report.final_diff),
        "gate_results": [
            {
                "gate_id": result.gate_id,
                "title": result.title,
                "required": result.required,
                "passed": result.passed,
                "candidate": _evidence_payload(result.candidate),
                "baseline": _evidence_payload(result.baseline)
                if result.baseline
                else None,
                "failure": result.failure,
            }
            for result in report.gate_results
        ],
        "requirement_results": [
            {
                "requirement_id": result.requirement_id,
                "description": result.description,
                "hard": result.hard,
                "status": result.status,
                "gate_ids": list(result.gate_ids),
            }
            for result in report.requirement_results
        ],
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_ms": report.duration_ms,
        "artifact_dir": str(report.artifact_dir),
        "report_path": str(report.report_path),
        "limitations": list(report.limitations),
    }
    if include_digest:
        payload["digest"] = report.digest
    return payload


def make_command_gate(
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
    """Build a deterministic candidate-only command gate."""
    return _make_gate(
        argv,
        id=id,
        title=title,
        kind="command",
        required=required,
        proves=proves,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        candidate_expectation=expectation,
        baseline_expectation=None,
    )


def make_reproducer_gate(
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
    """Build a gate that must fail on the base commit and pass on the candidate."""
    return _make_gate(
        argv,
        id=id,
        title=title,
        kind="reproducer",
        required=required,
        proves=proves,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        candidate_expectation=candidate_expectation,
        baseline_expectation=baseline_expectation,
    )


def _make_gate(
    argv: Sequence[str],
    *,
    id: str,
    title: str | None,
    kind: str,
    required: bool,
    proves: Sequence[str],
    cwd: str,
    timeout_seconds: float,
    candidate_expectation: str,
    baseline_expectation: str | None,
) -> Gate:
    gate_id = _validate_id(id, "gate id")
    if isinstance(argv, (str, bytes)) or not argv:
        raise ContractError(
            "gate argv must be a non-empty sequence of argument strings"
        )
    normalized_argv = tuple(argv)
    if any(
        not isinstance(argument, str) or not argument for argument in normalized_argv
    ):
        raise ContractError("gate argv entries must be non-empty strings")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ContractError("gate title must be a non-empty string or None")
    if kind not in {"command", "reproducer"}:
        raise ContractError(f"unsupported gate kind: {kind!r}")
    if not isinstance(required, bool):
        raise ContractError("gate required must be bool")
    normalized_proves = tuple(_validate_id(value, "requirement id") for value in proves)
    if not isinstance(cwd, str) or not cwd.strip():
        raise ContractError("gate cwd must be a non-empty relative path")
    if Path(cwd).is_absolute():
        raise ContractError("gate cwd must be relative to the verified workspace")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(
        timeout_seconds, bool
    ):
        raise ContractError("gate timeout_seconds must be numeric")
    timeout = float(timeout_seconds)
    if timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
        raise ContractError(
            f"gate timeout_seconds must be in (0, {_MAX_TIMEOUT_SECONDS:g}]"
        )
    if candidate_expectation not in {"success", "failure"}:
        raise ContractError("candidate expectation must be 'success' or 'failure'")
    if baseline_expectation not in {None, "success", "failure"}:
        raise ContractError(
            "baseline expectation must be 'success', 'failure', or None"
        )
    if kind == "reproducer" and baseline_expectation is None:
        raise ContractError("reproducer gates require a baseline expectation")
    if kind == "command" and baseline_expectation is not None:
        raise ContractError("command gates cannot have a baseline expectation")
    return Gate(
        id=gate_id,
        title=(title or gate_id).strip(),
        argv=normalized_argv,
        kind=kind,  # type: ignore[arg-type]
        required=required,
        proves=normalized_proves,
        cwd=cwd.strip(),
        timeout_seconds=timeout,
        candidate_expectation=candidate_expectation,  # type: ignore[arg-type]
        baseline_expectation=baseline_expectation,  # type: ignore[arg-type]
    )


class ProofRuntime:
    """Persistent acceptance contracts and verifier-generated evidence ledgers."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        workspace_manager: WorkspaceManager | None = None,
    ) -> None:
        self.state_dir = (
            Path(state_dir).expanduser().resolve()
            if state_dir
            else _default_state_dir().resolve()
        )
        self.workspace_manager = workspace_manager or WorkspaceManager()

    async def contract(
        self,
        goal: str,
        *,
        requirements: Sequence[str | Requirement] = (),
        gates: Sequence[Gate] = (),
        invariants: Sequence[str] = (),
        constraints: Sequence[str] = (),
        repo: str | os.PathLike[str] = ".",
    ) -> AcceptanceContract:
        """Create and persist a commit-bound acceptance contract."""
        if not isinstance(goal, str) or not goal.strip():
            raise ContractError("goal must be a non-empty string")
        repo_root, common_dir = await self._repo_paths(repo)
        status = await self._git(
            repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        if status:
            raise ContractError("acceptance contracts require a clean source worktree")
        base_commit = (
            (await self._git(repo_root, "rev-parse", "HEAD")).decode("ascii").strip()
        )
        normalized_invariants = _normalize_texts(invariants, "invariant")
        normalized_constraints = _normalize_texts(constraints, "constraint")
        normalized_requirements = self._normalize_requirements(
            goal.strip(), requirements, normalized_invariants, normalized_constraints
        )
        requirement_ids = {requirement.id for requirement in normalized_requirements}
        if len(requirement_ids) != len(normalized_requirements):
            raise ContractError("requirement ids must be unique")

        normalized_gates = tuple(gates) or await self._discover_gates(repo_root)
        gate_ids: set[str] = set()
        completed_gates: list[Gate] = []
        default_proves = tuple(
            requirement.id for requirement in normalized_requirements
        )
        for gate in normalized_gates:
            if not isinstance(gate, Gate):
                raise ContractError(
                    "gates must contain Gate values built by command() or reproducer()"
                )
            _validate_id(gate.id, "gate id")
            if gate.id in gate_ids:
                raise ContractError(f"duplicate gate id: {gate.id}")
            gate_ids.add(gate.id)
            proves = gate.proves or default_proves
            unknown = sorted(set(proves) - requirement_ids)
            if unknown:
                raise ContractError(
                    f"gate {gate.id!r} references unknown requirements: {', '.join(unknown)}"
                )
            completed_gates.append(replace(gate, proves=tuple(proves)))

        identifier = uuid.uuid4().hex
        created_at = _utc_now()
        provisional = AcceptanceContract(
            id=identifier,
            goal=goal.strip(),
            requirements=normalized_requirements,
            gates=tuple(completed_gates),
            invariants=normalized_invariants,
            constraints=normalized_constraints,
            repo_root=repo_root,
            common_dir=common_dir,
            base_commit=base_commit,
            created_at=created_at,
            digest="",
        )
        digest = _sha256_bytes(
            _canonical_json(_contract_payload(provisional, include_digest=False))
        )
        completed = replace(provisional, digest=digest)
        _atomic_json(
            self._contract_path(completed),
            _contract_payload(completed, include_digest=True),
        )
        return completed

    async def load_contract(
        self,
        contract_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> AcceptanceContract:
        """Load and integrity-check a persisted acceptance contract."""
        _validate_id(contract_id, "contract id")
        _, common_dir = await self._repo_paths(repo)
        path = self._repository_dir(common_dir) / "contracts" / f"{contract_id}.json"
        try:
            raw = await asyncio.to_thread(
                self._read_managed_bytes,
                path,
                label=f"acceptance contract {contract_id}",
                error_type=ContractError,
            )
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError as error:
            raise ContractError(
                f"acceptance contract not found: {contract_id}"
            ) from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError(
                f"cannot read acceptance contract {contract_id}: {error}"
            ) from error
        contract = self._contract_from_payload(payload)
        if contract.common_dir != common_dir:
            raise ContractError(
                "acceptance contract belongs to a different Git repository"
            )
        expected = _sha256_bytes(
            _canonical_json(_contract_payload(contract, include_digest=False))
        )
        if contract.digest != expected:
            raise ContractError(f"acceptance contract digest mismatch: {contract.id}")
        return contract

    async def load_report(
        self,
        report_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> VerificationReport:
        """Load and integrity-check a persisted verifier evidence ledger."""
        _validate_id(report_id, "verification report id")
        _, common_dir = await self._repo_paths(repo)
        artifact_dir = self._repository_dir(common_dir) / "runs" / report_id
        report_path = artifact_dir / "report.json"
        try:
            raw = await asyncio.to_thread(
                self._read_managed_bytes,
                report_path,
                label=f"verification report {report_id}",
            )
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError as error:
            raise VerificationError(
                f"verification report not found: {report_id}"
            ) from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VerificationError(
                f"cannot read verification report {report_id}: {error}"
            ) from error
        report = self._report_from_payload(payload)
        if report.id != report_id:
            raise VerificationError("verification report id does not match its path")
        if report.artifact_dir != artifact_dir or report.report_path != report_path:
            raise VerificationError(
                "verification report artifact paths do not match verifier state"
            )
        contract = await self.load_contract(report.contract_id, repo=repo)
        await asyncio.to_thread(
            self._validate_report_integrity,
            report,
            payload,
            contract,
            artifact_dir,
        )
        return report

    async def run(
        self,
        target: str | Workspace,
        contract: AcceptanceContract,
        *,
        fail_fast: bool = False,
    ) -> VerificationReport:
        """Execute a contract in a candidate and persist a complete evidence ledger."""
        if not isinstance(contract, AcceptanceContract):
            raise TypeError("contract must be AcceptanceContract")
        if not isinstance(fail_fast, bool):
            raise TypeError("fail_fast must be bool")
        self._validate_contract_integrity(contract)
        workspace = await self.workspace_manager.get(target, repo=contract.repo_root)
        if workspace.status != "active":
            raise VerificationError(
                f"workspace {workspace.id} cannot be verified from status {workspace.status!r}"
            )
        if workspace.common_dir != contract.common_dir:
            raise VerificationError(
                "workspace and contract belong to different Git repositories"
            )
        if workspace.base_commit != contract.base_commit:
            raise VerificationError(
                "workspace was forked from a different commit than the acceptance contract"
            )

        initial_snapshot = await self.workspace_manager.diff(workspace)
        run_id = uuid.uuid4().hex
        artifact_dir = self._repository_dir(contract.common_dir) / "runs" / run_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        initial_diff = self._persist_diff(
            initial_snapshot, artifact_dir / "initial.patch"
        )
        report_path = artifact_dir / "report.json"
        started_at = _utc_now()
        started_clock = time.monotonic()
        results: list[GateResult] = []
        limitations: list[str] = []

        for gate in contract.gates:
            baseline_evidence: ExecutionEvidence | None = None
            if gate.kind == "reproducer":
                baseline = await self.workspace_manager.fork(
                    contract.repo_root,
                    name=f"baseline-{run_id[:8]}-{gate.id}",
                    base=contract.base_commit,
                )
                try:
                    baseline_evidence = await self._execute_gate(
                        gate,
                        baseline.path,
                        artifact_dir,
                        phase="baseline",
                        expectation=gate.baseline_expectation or "failure",
                    )
                finally:
                    await self.workspace_manager.discard(baseline)

            candidate_evidence = await self._execute_gate(
                gate,
                workspace.path,
                artifact_dir,
                phase="candidate",
                expectation=gate.candidate_expectation,
            )
            passed = candidate_evidence.passed and (
                baseline_evidence is None or baseline_evidence.passed
            )
            failure_parts: list[str] = []
            if baseline_evidence is not None and not baseline_evidence.passed:
                failure_parts.append(
                    self._evidence_failure("baseline", baseline_evidence)
                )
            if not candidate_evidence.passed:
                failure_parts.append(
                    self._evidence_failure("candidate", candidate_evidence)
                )
            result = GateResult(
                gate_id=gate.id,
                title=gate.title,
                required=gate.required,
                passed=passed,
                candidate=candidate_evidence,
                baseline=baseline_evidence,
                failure="; ".join(failure_parts) or None,
            )
            results.append(result)
            if fail_fast and gate.required and not passed:
                skipped = [remaining.id for remaining in contract.gates[len(results) :]]
                if skipped:
                    limitations.append(f"fail_fast skipped gates: {', '.join(skipped)}")
                break

        final_snapshot = await self.workspace_manager.diff(workspace)
        final_diff = self._persist_diff(final_snapshot, artifact_dir / "final.patch")
        candidate_stable = final_snapshot.patch_sha256 == initial_snapshot.patch_sha256
        if not candidate_stable:
            limitations.append(
                "candidate changed during verification; no gate result attests the final snapshot"
            )
        if initial_snapshot.is_empty:
            limitations.append("candidate contains no promotable changes")
        requirement_results = self._requirement_results(contract, results)
        status = self._verification_status(
            contract,
            results,
            requirement_results,
            candidate_stable=candidate_stable,
            has_changes=not initial_snapshot.is_empty,
        )
        finished_at = _utc_now()
        duration_ms = round((time.monotonic() - started_clock) * 1000)
        report = VerificationReport(
            id=run_id,
            contract_id=contract.id,
            contract_digest=contract.digest,
            workspace_id=workspace.id,
            status=status,
            patch_sha256=initial_snapshot.patch_sha256,
            files_changed=len(initial_snapshot.files),
            initial_diff=initial_diff,
            final_diff=final_diff,
            gate_results=tuple(results),
            requirement_results=requirement_results,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            artifact_dir=artifact_dir,
            report_path=report_path,
            limitations=tuple(limitations),
            digest="",
        )
        report = replace(
            report,
            digest=_sha256_bytes(
                _canonical_json(_report_payload(report, include_digest=False))
            ),
        )
        _atomic_json(report_path, _report_payload(report))
        return report

    async def promote(
        self,
        target: str | Workspace,
        report: VerificationReport,
    ) -> PromotionResult:
        """Promote exactly the snapshot attested by a verified report."""
        if not isinstance(report, VerificationReport):
            raise TypeError("report must be VerificationReport")
        report.require_verified()
        workspace = await self.workspace_manager.get(target)
        if workspace.id != report.workspace_id:
            raise VerificationError(
                "verification report belongs to a different workspace"
            )
        expected_artifact_dir = (
            self._repository_dir(workspace.common_dir) / "runs" / report.id
        )
        if (
            report.artifact_dir != expected_artifact_dir
            or report.report_path != expected_artifact_dir / "report.json"
        ):
            raise VerificationError(
                "verification report artifact paths do not match verifier state"
            )
        try:
            raw = await asyncio.to_thread(
                self._read_managed_bytes,
                report.report_path,
                label=f"verification report {report.id}",
            )
            persisted = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VerificationError(
                f"cannot load persisted verification report: {error}"
            ) from error
        if persisted != _report_payload(report):
            raise VerificationError(
                "verification report differs from its persisted evidence ledger"
            )
        validated = await self.load_report(report.id, repo=workspace.repo_root)
        if validated != report:
            raise VerificationError(
                "verification report differs from its persisted evidence ledger"
            )
        return await self.workspace_manager.promote(
            workspace,
            expected_patch_sha256=report.patch_sha256,
        )

    def _repository_dir(self, common_dir: Path) -> Path:
        return self.state_dir / _repo_key(common_dir)

    def _contract_path(self, contract: AcceptanceContract) -> Path:
        return (
            self._repository_dir(contract.common_dir)
            / "contracts"
            / f"{contract.id}.json"
        )

    @staticmethod
    def _persist_diff(
        snapshot: WorkspaceDiff, patch_path: Path
    ) -> WorkspaceDiffAttestation:
        if _sha256_bytes(snapshot.patch) != snapshot.patch_sha256:
            raise VerificationError("workspace returned an inconsistent diff snapshot")
        _atomic_bytes(patch_path, snapshot.patch)
        evidence_path = patch_path.with_suffix(".diff.json")
        provisional = WorkspaceDiffAttestation(
            workspace_id=snapshot.workspace_id,
            base_commit=snapshot.base_commit,
            evidence_path=evidence_path,
            evidence_sha256="",
            patch_path=patch_path,
            patch_sha256=snapshot.patch_sha256,
            files=snapshot.files,
        )
        evidence_sha256 = _sha256_bytes(
            _canonical_json(_diff_evidence_payload(provisional))
        )
        attestation = replace(provisional, evidence_sha256=evidence_sha256)
        _atomic_json(evidence_path, _diff_evidence_payload(attestation))
        return attestation

    def _open_managed_descriptor(
        self,
        path: Path,
        *,
        label: str,
        error_type: type[ProofError] = VerificationError,
    ) -> int:
        root = Path(os.path.abspath(self.state_dir))
        target = Path(os.path.abspath(path))
        try:
            relative = target.relative_to(root)
        except ValueError as error:
            raise error_type(f"{label} is outside verifier state") from error

        current = root
        leaf_metadata: os.stat_result | None = None
        for index, part in enumerate(("", *relative.parts)):
            if index:
                current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError as error:
                raise error_type(f"{label} not found") from error
            except OSError as error:
                raise error_type(
                    f"cannot inspect managed path component for {label}: {error}"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise error_type(
                    f"symlink path component is not allowed for {label}: {current}"
                )
            if current == target:
                leaf_metadata = metadata
            elif not stat.S_ISDIR(metadata.st_mode):
                raise error_type(
                    f"managed path component is not a directory for {label}: {current}"
                )
        if leaf_metadata is None or not stat.S_ISREG(leaf_metadata.st_mode):
            raise error_type(f"{label} must be a regular file")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError as error:
            raise error_type(f"cannot open {label} safely: {error}") from error
        try:
            opened = os.fstat(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise error_type(f"cannot inspect opened {label}: {error}") from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != leaf_metadata.st_dev
            or opened.st_ino != leaf_metadata.st_ino
        ):
            os.close(descriptor)
            raise error_type(f"{label} changed while it was being opened")
        return descriptor

    def _read_managed_bytes(
        self,
        path: Path,
        *,
        label: str,
        error_type: type[ProofError] = VerificationError,
    ) -> bytes:
        descriptor = self._open_managed_descriptor(
            path, label=label, error_type=error_type
        )
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()

    def _managed_file_facts(self, path: Path, *, label: str) -> tuple[str, str]:
        descriptor = self._open_managed_descriptor(path, label=label)
        digest = hashlib.sha256()
        half = _PREVIEW_BYTES // 2
        first = bytearray()
        tail = bytearray()
        complete: bytearray | None = bytearray()
        with os.fdopen(descriptor, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                if len(first) < half:
                    first.extend(chunk[: half - len(first)])
                tail.extend(chunk)
                if len(tail) > half:
                    del tail[:-half]
                if complete is not None:
                    complete.extend(chunk)
                    if len(complete) > _PREVIEW_BYTES:
                        complete = None
        if complete is not None:
            preview = bytes(complete)
        else:
            preview = bytes(first) + b"\n... output truncated ...\n" + bytes(tail)
        return digest.hexdigest(), preview.decode("utf-8", errors="replace")

    @staticmethod
    def _normalize_requirements(
        goal: str,
        requirements: Sequence[str | Requirement],
        invariants: tuple[str, ...],
        constraints: tuple[str, ...],
    ) -> tuple[Requirement, ...]:
        normalized: list[Requirement] = []
        if requirements:
            for index, value in enumerate(requirements, start=1):
                if isinstance(value, Requirement):
                    _validate_id(value.id, "requirement id")
                    if not value.description.strip():
                        raise ContractError(
                            "requirement descriptions must not be empty"
                        )
                    normalized.append(
                        replace(value, description=value.description.strip())
                    )
                elif isinstance(value, str) and value.strip():
                    normalized.append(
                        Requirement(id=f"REQ-{index:03d}", description=value.strip())
                    )
                else:
                    raise ContractError(
                        "requirements must contain non-empty strings or Requirement values"
                    )
        else:
            normalized.append(Requirement(id="GOAL", description=goal))
        normalized.extend(
            Requirement(id=f"INV-{index:03d}", description=value)
            for index, value in enumerate(invariants, start=1)
        )
        normalized.extend(
            Requirement(id=f"CON-{index:03d}", description=value)
            for index, value in enumerate(constraints, start=1)
        )
        return tuple(normalized)

    async def _discover_gates(self, repo_root: Path) -> tuple[Gate, ...]:
        package_json = repo_root / "package.json"
        if package_json.is_file():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                scripts = payload.get("scripts")
                if isinstance(scripts, dict) and isinstance(scripts.get("check"), str):
                    return (
                        make_command_gate(
                            ("npm", "run", "check"),
                            id="repository-check",
                            title="Repository check",
                            timeout_seconds=900,
                        ),
                    )
        return ()

    def _validate_contract_integrity(self, contract: AcceptanceContract) -> None:
        expected = _sha256_bytes(
            _canonical_json(_contract_payload(contract, include_digest=False))
        )
        if contract.digest != expected:
            raise ContractError(f"acceptance contract digest mismatch: {contract.id}")

    def _contract_from_payload(self, payload: object) -> AcceptanceContract:
        if not isinstance(payload, dict) or payload.get("version") != _CONTRACT_VERSION:
            raise ContractError("unsupported acceptance contract format")
        try:
            requirements_payload = payload["requirements"]
            gates_payload = payload["gates"]
            if not isinstance(requirements_payload, list) or not isinstance(
                gates_payload, list
            ):
                raise TypeError
            requirements = tuple(
                Requirement(
                    id=item["id"],
                    description=item["description"],
                    hard=item["hard"],
                )
                for item in requirements_payload
                if isinstance(item, dict)
            )
            gates = tuple(
                Gate(
                    id=item["id"],
                    title=item["title"],
                    argv=tuple(item["argv"]),
                    kind=item["kind"],
                    required=item["required"],
                    proves=tuple(item["proves"]),
                    cwd=item["cwd"],
                    timeout_seconds=float(item["timeout_seconds"]),
                    candidate_expectation=item["candidate_expectation"],
                    baseline_expectation=item["baseline_expectation"],
                )
                for item in gates_payload
                if isinstance(item, dict)
            )
            contract = AcceptanceContract(
                id=payload["id"],
                goal=payload["goal"],
                requirements=requirements,
                gates=gates,
                invariants=tuple(payload["invariants"]),
                constraints=tuple(payload["constraints"]),
                repo_root=Path(payload["repo_root"]).resolve(),
                common_dir=Path(payload["common_dir"]).resolve(),
                base_commit=payload["base_commit"],
                created_at=payload["created_at"],
                digest=payload["digest"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("acceptance contract has invalid fields") from error
        if len(requirements) != len(requirements_payload) or len(gates) != len(
            gates_payload
        ):
            raise ContractError(
                "acceptance contract contains invalid requirement or gate entries"
            )
        self._validate_contract_integrity(contract)
        return contract

    @staticmethod
    def _report_from_payload(payload: object) -> VerificationReport:
        if not isinstance(payload, dict) or payload.get("version") != _REPORT_VERSION:
            raise VerificationError("unsupported verification report format")

        def evidence_from(raw: object) -> ExecutionEvidence:
            if not isinstance(raw, dict):
                raise TypeError
            return ExecutionEvidence(
                phase=raw["phase"],
                argv=tuple(raw["argv"]),
                cwd=_absolute_path(raw["cwd"]),
                expectation=raw["expectation"],
                passed=raw["passed"],
                exit_code=raw["exit_code"],
                timed_out=raw["timed_out"],
                started_at=raw["started_at"],
                finished_at=raw["finished_at"],
                duration_ms=raw["duration_ms"],
                stdout_path=_absolute_path(raw["stdout_path"]),
                stderr_path=_absolute_path(raw["stderr_path"]),
                stdout_sha256=raw["stdout_sha256"],
                stderr_sha256=raw["stderr_sha256"],
                stdout_preview=raw["stdout_preview"],
                stderr_preview=raw["stderr_preview"],
            )

        def diff_from(raw: object) -> WorkspaceDiffAttestation:
            if not isinstance(raw, dict):
                raise TypeError
            file_payloads = raw["files"]
            if not isinstance(file_payloads, list):
                raise TypeError
            files = tuple(
                FileChange(
                    status=item["status"],
                    path=item["path"],
                    previous_path=item["previous_path"],
                )
                for item in file_payloads
                if isinstance(item, dict)
            )
            if len(files) != len(file_payloads):
                raise TypeError
            return WorkspaceDiffAttestation(
                workspace_id=raw["workspace_id"],
                base_commit=raw["base_commit"],
                evidence_path=_absolute_path(raw["evidence_path"]),
                evidence_sha256=raw["evidence_sha256"],
                patch_path=_absolute_path(raw["patch_path"]),
                patch_sha256=raw["patch_sha256"],
                files=files,
            )

        try:
            gate_payloads = payload["gate_results"]
            requirement_payloads = payload["requirement_results"]
            if not isinstance(gate_payloads, list) or not isinstance(
                requirement_payloads, list
            ):
                raise TypeError
            gate_results = tuple(
                GateResult(
                    gate_id=item["gate_id"],
                    title=item["title"],
                    required=item["required"],
                    passed=item["passed"],
                    candidate=evidence_from(item["candidate"]),
                    baseline=(
                        evidence_from(item["baseline"])
                        if item["baseline"] is not None
                        else None
                    ),
                    failure=item["failure"],
                )
                for item in gate_payloads
                if isinstance(item, dict)
            )
            requirement_results = tuple(
                RequirementResult(
                    requirement_id=item["requirement_id"],
                    description=item["description"],
                    hard=item["hard"],
                    status=item["status"],
                    gate_ids=tuple(item["gate_ids"]),
                )
                for item in requirement_payloads
                if isinstance(item, dict)
            )
            report = VerificationReport(
                id=payload["id"],
                contract_id=payload["contract_id"],
                contract_digest=payload["contract_digest"],
                workspace_id=payload["workspace_id"],
                status=payload["status"],
                patch_sha256=payload["patch_sha256"],
                files_changed=payload["files_changed"],
                initial_diff=diff_from(payload["initial_diff"]),
                final_diff=diff_from(payload["final_diff"]),
                gate_results=gate_results,
                requirement_results=requirement_results,
                started_at=payload["started_at"],
                finished_at=payload["finished_at"],
                duration_ms=payload["duration_ms"],
                artifact_dir=_absolute_path(payload["artifact_dir"]),
                report_path=_absolute_path(payload["report_path"]),
                limitations=tuple(payload["limitations"]),
                digest=payload["digest"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise VerificationError("verification report has invalid fields") from error
        if len(gate_results) != len(gate_payloads) or len(requirement_results) != len(
            requirement_payloads
        ):
            raise VerificationError(
                "verification report contains invalid result entries"
            )
        return report

    def _validate_report_integrity(
        self,
        report: VerificationReport,
        payload: object,
        contract: AcceptanceContract,
        artifact_dir: Path,
    ) -> None:
        if payload != _report_payload(report):
            raise VerificationError(
                "verification report is not a canonical verifier ledger"
            )
        if report.contract_digest != contract.digest:
            raise VerificationError(
                "verification report contract digest does not match"
            )
        if (
            report.status not in {"verified", "failed", "incomplete", "error"}
            or not isinstance(report.files_changed, int)
            or not isinstance(report.patch_sha256, str)
            or isinstance(report.files_changed, bool)
            or report.files_changed < 0
            or _SHA256_PATTERN.fullmatch(report.patch_sha256) is None
        ):
            raise VerificationError("verification report decision fields are invalid")
        if len(report.gate_results) > len(contract.gates):
            raise VerificationError("verification report contains unknown gate results")
        diff_evidence: list[tuple[str, WorkspaceDiffAttestation, bytes]] = []
        for name, attestation in (
            ("initial", report.initial_diff),
            ("final", report.final_diff),
        ):
            expected_patch_path = artifact_dir / f"{name}.patch"
            expected_evidence_path = artifact_dir / f"{name}.diff.json"
            if (
                attestation.workspace_id != report.workspace_id
                or attestation.base_commit != contract.base_commit
                or attestation.evidence_path != expected_evidence_path
                or not isinstance(attestation.evidence_sha256, str)
                or _SHA256_PATTERN.fullmatch(attestation.evidence_sha256) is None
                or attestation.patch_path != expected_patch_path
                or not isinstance(attestation.patch_sha256, str)
                or _SHA256_PATTERN.fullmatch(attestation.patch_sha256) is None
            ):
                raise VerificationError(
                    f"verification report {name} diff attestation is invalid"
                )
            for change in attestation.files:
                if (
                    not isinstance(change.status, str)
                    or not change.status
                    or not isinstance(change.path, str)
                    or not change.path
                    or (
                        change.previous_path is not None
                        and (
                            not isinstance(change.previous_path, str)
                            or not change.previous_path
                        )
                    )
                ):
                    raise VerificationError(
                        f"verification report {name} diff file evidence is invalid"
                    )
            evidence_bytes = self._read_managed_bytes(
                attestation.evidence_path,
                label=f"{name} workspace diff attestation",
            )
            try:
                persisted_evidence = json.loads(evidence_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise VerificationError(
                    f"verification report {name} diff attestation is invalid"
                ) from error
            expected_evidence = _diff_evidence_payload(attestation)
            if (
                persisted_evidence != expected_evidence
                or _sha256_bytes(_canonical_json(persisted_evidence))
                != attestation.evidence_sha256
            ):
                raise VerificationError(
                    f"verification report {name} diff attestation is inconsistent"
                )
            patch = self._read_managed_bytes(
                attestation.patch_path,
                label=f"{name} workspace diff evidence",
            )
            if _sha256_bytes(patch) != attestation.patch_sha256 or bool(
                patch
            ) is not bool(attestation.files):
                raise VerificationError(
                    f"verification report {name} diff evidence is inconsistent"
                )
            diff_evidence.append((name, attestation, patch))

        initial_attestation = diff_evidence[0][1]
        initial_patch = diff_evidence[0][2]
        final_patch = diff_evidence[1][2]
        candidate_stable = _sha256_bytes(initial_patch) == _sha256_bytes(final_patch)
        if (
            report.patch_sha256 != _sha256_bytes(initial_patch)
            or report.files_changed != initial_attestation.files_changed
        ):
            raise VerificationError(
                "verification report patch summary is inconsistent with its diff evidence"
            )

        for index, result in enumerate(report.gate_results):
            gate = contract.gates[index]
            if (
                result.gate_id != gate.id
                or result.title != gate.title
                or result.required is not gate.required
            ):
                raise VerificationError(
                    "verification report gate metadata does not match its contract"
                )
            expected_baseline = gate.kind == "reproducer"
            if (result.baseline is not None) is not expected_baseline:
                raise VerificationError(
                    f"verification report gate {gate.id} has invalid baseline evidence"
                )
            evidence_pairs = [
                (result.candidate, "candidate", gate.candidate_expectation)
            ]
            if result.baseline is not None:
                evidence_pairs.append(
                    (result.baseline, "baseline", gate.baseline_expectation)
                )
            for evidence, phase, expectation in evidence_pairs:
                expected_pass = (
                    not evidence.timed_out
                    and evidence.exit_code is not None
                    and (
                        (expectation == "success" and evidence.exit_code == 0)
                        or (expectation == "failure" and evidence.exit_code != 0)
                    )
                )
                if (
                    evidence.phase != phase
                    or evidence.argv != gate.argv
                    or evidence.expectation != expectation
                    or evidence.passed is not expected_pass
                ):
                    raise VerificationError(
                        f"verification report gate {gate.id} evidence is inconsistent"
                    )
                safe_gate_id = re.sub(r"[^A-Za-z0-9_.-]", "-", gate.id)
                for output_path, expected_path, expected_hash, expected_preview in (
                    (
                        evidence.stdout_path,
                        artifact_dir / f"{safe_gate_id}.{phase}.stdout.log",
                        evidence.stdout_sha256,
                        evidence.stdout_preview,
                    ),
                    (
                        evidence.stderr_path,
                        artifact_dir / f"{safe_gate_id}.{phase}.stderr.log",
                        evidence.stderr_sha256,
                        evidence.stderr_preview,
                    ),
                ):
                    if (
                        output_path != expected_path
                        or not isinstance(expected_hash, str)
                        or _SHA256_PATTERN.fullmatch(expected_hash) is None
                    ):
                        raise VerificationError(
                            f"verification report gate {gate.id} output evidence is invalid"
                        )
                    actual_hash, actual_preview = self._managed_file_facts(
                        output_path,
                        label=f"verification report gate {gate.id} output evidence",
                    )
                    if (
                        actual_hash != expected_hash
                        or actual_preview != expected_preview
                    ):
                        raise VerificationError(
                            f"verification report gate {gate.id} output evidence is invalid"
                        )
            expected_result = result.candidate.passed and (
                result.baseline is None or result.baseline.passed
            )
            failure_parts = []
            if result.baseline is not None and not result.baseline.passed:
                failure_parts.append(
                    self._evidence_failure("baseline", result.baseline)
                )
            if not result.candidate.passed:
                failure_parts.append(
                    self._evidence_failure("candidate", result.candidate)
                )
            if result.passed is not expected_result or result.failure != (
                "; ".join(failure_parts) or None
            ):
                raise VerificationError(
                    f"verification report gate {gate.id} decision is inconsistent"
                )

        expected_requirements = self._requirement_results(contract, report.gate_results)
        if report.requirement_results != expected_requirements:
            raise VerificationError(
                "verification report requirement decisions are inconsistent"
            )
        expected_limitations: list[str] = []
        if len(report.gate_results) < len(contract.gates):
            skipped = [gate.id for gate in contract.gates[len(report.gate_results) :]]
            expected_limitations.append(
                f"fail_fast skipped gates: {', '.join(skipped)}"
            )
        if not candidate_stable:
            expected_limitations.append(
                "candidate changed during verification; no gate result attests the final snapshot"
            )
        if not initial_patch:
            expected_limitations.append("candidate contains no promotable changes")
        if report.limitations != tuple(expected_limitations):
            raise VerificationError(
                "verification report limitations are inconsistent with its evidence"
            )
        expected_status = self._verification_status(
            contract,
            report.gate_results,
            expected_requirements,
            candidate_stable=candidate_stable,
            has_changes=bool(initial_patch),
        )
        if report.status != expected_status:
            raise VerificationError(
                "verification report status is inconsistent with its evidence"
            )
        expected_digest = _sha256_bytes(
            _canonical_json(_report_payload(report, include_digest=False))
        )
        if (
            not isinstance(report.digest, str)
            or _SHA256_PATTERN.fullmatch(report.digest) is None
            or report.digest != expected_digest
        ):
            raise VerificationError(f"verification report digest mismatch: {report.id}")

    async def _execute_gate(
        self,
        gate: Gate,
        root: Path,
        artifact_dir: Path,
        *,
        phase: str,
        expectation: str,
    ) -> ExecutionEvidence:
        cwd = (root / gate.cwd).resolve()
        try:
            cwd.relative_to(root.resolve())
        except ValueError as error:
            raise VerificationError(
                f"gate {gate.id!r} cwd escapes the workspace: {gate.cwd}"
            ) from error
        if not cwd.is_dir():
            raise VerificationError(f"gate {gate.id!r} cwd does not exist: {gate.cwd}")

        safe_gate_id = re.sub(r"[^A-Za-z0-9_.-]", "-", gate.id)
        stdout_path = artifact_dir / f"{safe_gate_id}.{phase}.stdout.log"
        stderr_path = artifact_dir / f"{safe_gate_id}.{phase}.stderr.log"
        started_at = _utc_now()
        started_clock = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        environment = {
            key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ
        }
        private_home = artifact_dir / f"{safe_gate_id}.{phase}.home"
        private_tmp = artifact_dir / f"{safe_gate_id}.{phase}.tmp"
        private_home.mkdir(mode=0o700)
        private_tmp.mkdir(mode=0o700)
        environment.update(
            {
                "CI": "1",
                "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "OH_MY_PRIME_VERIFICATION": "1",
                "HOME": str(private_home),
                "USERPROFILE": str(private_home),
                "TMPDIR": str(private_tmp),
                "TEMP": str(private_tmp),
                "TMP": str(private_tmp),
            }
        )
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = await asyncio.create_subprocess_exec(
                    *gate.argv,
                    cwd=cwd,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name != "nt",
                    creationflags=creationflags,
                )
            except OSError as error:
                stderr.write(
                    f"unable to start command: {error}\n".encode(
                        "utf-8", errors="replace"
                    )
                )
                process = None
            if process is not None:
                try:
                    exit_code = await asyncio.wait_for(
                        process.wait(), timeout=gate.timeout_seconds
                    )
                except TimeoutError:
                    timed_out = True
                    await self._terminate_process(process)
                    exit_code = process.returncode

        stdout_sha256, stdout_preview = self._managed_file_facts(
            stdout_path,
            label=f"verification gate {gate.id} stdout evidence",
        )
        stderr_sha256, stderr_preview = self._managed_file_facts(
            stderr_path,
            label=f"verification gate {gate.id} stderr evidence",
        )
        finished_at = _utc_now()
        duration_ms = round((time.monotonic() - started_clock) * 1000)
        passed = (
            not timed_out
            and exit_code is not None
            and (
                (expectation == "success" and exit_code == 0)
                or (expectation == "failure" and exit_code != 0)
            )
        )
        return ExecutionEvidence(
            phase=phase,  # type: ignore[arg-type]
            argv=gate.argv,
            cwd=cwd,
            expectation=expectation,  # type: ignore[arg-type]
            passed=passed,
            exit_code=exit_code,
            timed_out=timed_out,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            stdout_preview=stdout_preview,
            stderr_preview=stderr_preview,
        )

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
            return
        except TimeoutError:
            pass
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        await process.wait()

    @staticmethod
    def _evidence_failure(phase: str, evidence: ExecutionEvidence) -> str:
        if evidence.timed_out:
            return f"{phase} timed out"
        if evidence.exit_code is None:
            return f"{phase} command could not start"
        return f"{phase} expected {evidence.expectation} but exited with code {evidence.exit_code}"

    @staticmethod
    def _requirement_results(
        contract: AcceptanceContract,
        results: Sequence[GateResult],
    ) -> tuple[RequirementResult, ...]:
        results_by_id = {result.gate_id: result for result in results}
        requirement_results: list[RequirementResult] = []
        for requirement in contract.requirements:
            proving_gates = tuple(
                gate.id for gate in contract.gates if requirement.id in gate.proves
            )
            attempted = [
                results_by_id[gate_id]
                for gate_id in proving_gates
                if gate_id in results_by_id
            ]
            if any(result.passed for result in attempted):
                status = "proved"
            elif attempted:
                status = "failed"
            else:
                status = "unproved"
            requirement_results.append(
                RequirementResult(
                    requirement_id=requirement.id,
                    description=requirement.description,
                    hard=requirement.hard,
                    status=status,  # type: ignore[arg-type]
                    gate_ids=proving_gates,
                )
            )
        return tuple(requirement_results)

    @staticmethod
    def _verification_status(
        contract: AcceptanceContract,
        results: Sequence[GateResult],
        requirement_results: Sequence[RequirementResult],
        *,
        candidate_stable: bool,
        has_changes: bool,
    ) -> str:
        if not candidate_stable:
            return "failed"
        if any(result.required and not result.passed for result in results):
            return "failed"
        if any(
            result.hard and result.status == "failed" for result in requirement_results
        ):
            return "failed"
        if not has_changes:
            return "incomplete"
        if not any(gate.required for gate in contract.gates):
            return "incomplete"
        if any(
            result.hard and result.status != "proved" for result in requirement_results
        ):
            return "incomplete"
        return "verified"

    async def _repo_paths(self, repo: str | os.PathLike[str]) -> tuple[Path, Path]:
        candidate = Path(repo).expanduser().resolve()
        root = await self._git(
            candidate, "rev-parse", "--path-format=absolute", "--show-toplevel"
        )
        common = await self._git(
            candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        return (
            Path(root.decode("utf-8", errors="surrogateescape").strip()).resolve(),
            Path(common.decode("utf-8", errors="surrogateescape").strip()).resolve(),
        )

    @staticmethod
    async def _git(repo: Path, *args: str) -> bytes:
        environment = os.environ.copy()
        environment.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo),
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError as error:
            raise ProofError("Git is required for acceptance contracts") from error
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise ProofError(f"git {' '.join(args)} failed: {detail or 'no output'}")
        return stdout
