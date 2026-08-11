from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from workspace import Workspace, WorkspaceManager

from ._memory import EvolutionLab
from ._models import (
    EvolutionError,
    MemoryCandidate,
    ProcessEvidence,
    ReplayCase,
    ReplayCaseResult,
    ReplayPhase,
    ReplayReport,
    ReplaySuite,
)

_SCHEMA_VERSION = 1
_MAX_CASES = 100
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_PREVIEW_BYTES = 4_000
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

_TIMEOUT_ERRORS = (
    (TimeoutError,)
    if asyncio.TimeoutError is TimeoutError
    else (asyncio.TimeoutError, TimeoutError)
)


class _OutputLimitExceeded(RuntimeError):
    pass


class ReplayRuntime:
    """Run baseline/candidate commands in isolated Git workspaces and harnesses."""

    def __init__(
        self,
        lab: EvolutionLab,
        workspace_manager: WorkspaceManager | None = None,
    ) -> None:
        self.lab = lab
        self.workspace_manager = workspace_manager or WorkspaceManager(
            lab.state_root / "replay-workspaces"
        )

    async def create_suite(
        self,
        name: str,
        cases: Sequence[ReplayCase],
        *,
        require_improvement: bool = True,
        minimum_improvements: int = 1,
        repo: str | os.PathLike[str] = ".",
    ) -> ReplaySuite:
        name = self._text(name, "name")
        cases = self._validate_cases(cases)
        if not isinstance(require_improvement, bool):
            raise EvolutionError("require_improvement must be a boolean")
        if (
            not isinstance(minimum_improvements, int)
            or isinstance(minimum_improvements, bool)
            or minimum_improvements < 0
            or minimum_improvements > len(cases)
        ):
            raise EvolutionError(
                "minimum_improvements must be between zero and the case count"
            )
        repo_root, common_dir, _ = await self.lab._repo(repo)
        created_at = self.lab._now_text()
        identity = {
            "name": name,
            "cases": [asdict(case) for case in cases],
            "require_improvement": require_improvement,
            "minimum_improvements": minimum_improvements,
            "created_at": created_at,
            "repo_root": str(repo_root),
        }
        suite_id = f"suite-{hashlib.sha256(self._canonical(identity)).hexdigest()[:24]}"
        suite = ReplaySuite(
            schema_version=_SCHEMA_VERSION,
            id=suite_id,
            name=name,
            cases=cases,
            require_improvement=require_improvement,
            minimum_improvements=minimum_improvements,
            created_at=created_at,
            digest="",
        )
        suite = replace(suite, digest=self._suite_digest(suite))
        path = self._managed_path(self._suite_path(common_dir, suite.id))
        if path.exists():
            existing = self._load_suite_sync(path, suite.id)
            if existing != suite:
                raise EvolutionError(f"replay suite id collision: {suite.id}")
            return existing
        await asyncio.to_thread(
            self._write_json_atomic, path, self._suite_payload(suite)
        )
        return suite

    async def load_suite(
        self,
        suite_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> ReplaySuite:
        self._id(suite_id, "suite")
        _, common_dir, _ = await self.lab._repo(repo)
        return await asyncio.to_thread(
            self._load_suite_sync,
            self._managed_path(self._suite_path(common_dir, suite_id)),
            suite_id,
        )

    async def run(
        self,
        candidate_id: str,
        suite: ReplaySuite | str,
        *,
        phase: ReplayPhase = "replay",
        harness_state_dir: str | os.PathLike[str] | None = None,
        repo: str | os.PathLike[str] = ".",
    ) -> ReplayReport:
        if phase not in {"replay", "shadow"}:
            raise EvolutionError("phase must be replay or shadow")
        candidate = await self.lab.get(candidate_id, repo=repo)
        freshness = await self.lab.freshness(candidate)
        if not freshness.fresh:
            raise EvolutionError(f"candidate is stale: {'; '.join(freshness.reasons)}")
        if candidate.status not in {"candidate", "shadow"}:
            raise EvolutionError(
                f"candidate status {candidate.status!r} cannot run replay"
            )
        if isinstance(suite, str):
            loaded_suite = await self.load_suite(suite, repo=repo)
        elif isinstance(suite, ReplaySuite):
            persisted_suite = await self.load_suite(suite.id, repo=repo)
            supplied_suite = self._validate_suite(suite)
            if supplied_suite != persisted_suite:
                raise EvolutionError(
                    f"replay suite does not match persisted suite: {suite.id}"
                )
            loaded_suite = persisted_suite
        else:
            loaded_suite = self._validate_suite(suite)
        repo_root, common_dir, source_revision = await self.lab._repo(repo)
        if candidate.repo_root != repo_root:
            raise EvolutionError("candidate belongs to another repository")
        if candidate.code_version != source_revision:
            raise EvolutionError("repository changed after candidate creation")
        harness_dir = self._harness_dir(candidate, harness_state_dir)
        shadow_entry = (
            self._attested_shadow_entry(candidate, common_dir)
            if phase == "shadow"
            else None
        )
        started_at = self.lab._now_text()
        run_key = {
            "candidate": candidate.id,
            "revision": candidate.revision,
            "suite": loaded_suite.id,
            "phase": phase,
            "source_revision": source_revision,
            "started_at": started_at,
        }
        report_id = (
            f"replay-{hashlib.sha256(self._canonical(run_key)).hexdigest()[:24]}"
        )
        artifact_dir = self._managed_path(self._report_root(common_dir) / report_id)
        artifact_dir.mkdir(parents=True, exist_ok=False)
        self._managed_path(artifact_dir)
        baseline_workspace: Workspace | None = None
        candidate_workspace: Workspace | None = None
        started_clock = time.monotonic()
        try:
            baseline_workspace = await self.workspace_manager.fork(
                repo_root,
                name=f"{report_id}-baseline",
                base=source_revision,
            )
            candidate_workspace = await self.workspace_manager.fork(
                repo_root,
                name=f"{report_id}-candidate",
                base=source_revision,
            )
            baseline_harness = artifact_dir / "harness-baseline"
            candidate_harness = artifact_dir / "harness-candidate"
            await asyncio.to_thread(
                self._prepare_harnesses,
                candidate,
                harness_dir,
                baseline_harness,
                candidate_harness,
                shadow_entry,
            )
            baseline_harness_path = self._managed_path(
                baseline_harness / "harness_state.json"
            )
            candidate_harness_path = self._managed_path(
                candidate_harness / "harness_state.json"
            )
            baseline_harness_sha256 = await asyncio.to_thread(
                self._managed_sha256, baseline_harness_path
            )
            candidate_harness_sha256 = await asyncio.to_thread(
                self._managed_sha256, candidate_harness_path
            )
            results: list[ReplayCaseResult] = []
            for case in loaded_suite.cases:
                baseline = await self._run_case(
                    case,
                    baseline_workspace.path,
                    baseline_harness,
                    artifact_dir,
                    "baseline",
                    candidate.id,
                    candidate.scope,
                )
                evaluated = await self._run_case(
                    case,
                    candidate_workspace.path,
                    candidate_harness,
                    artifact_dir,
                    "candidate",
                    candidate.id,
                    candidate.scope,
                )
                results.append(
                    ReplayCaseResult(
                        case_id=case.id,
                        title=case.title,
                        weight=case.weight,
                        baseline=baseline,
                        candidate=evaluated,
                        improved=evaluated.passed and not baseline.passed,
                        regressed=baseline.passed and not evaluated.passed,
                    )
                )
            if (
                await asyncio.to_thread(self._managed_sha256, baseline_harness_path)
                != baseline_harness_sha256
                or await asyncio.to_thread(self._managed_sha256, candidate_harness_path)
                != candidate_harness_sha256
            ):
                raise EvolutionError("replay command modified a harness snapshot")
            baseline_score = sum(
                result.weight for result in results if result.baseline.passed
            )
            candidate_score = sum(
                result.weight for result in results if result.candidate.passed
            )
            improvements = tuple(
                result.case_id for result in results if result.improved
            )
            regressions = tuple(
                result.case_id for result in results if result.regressed
            )
            enough_improvement = (
                not loaded_suite.require_improvement
                or len(improvements) >= loaded_suite.minimum_improvements
            )
            status = "passed" if not regressions and enough_improvement else "failed"
            limitations = (
                "Replay commands prove only their declared exit and stdout assertions.",
                "External services and model providers may introduce nondeterminism.",
            )
            finished_at = self.lab._now_text()
            report_path = self._managed_path(artifact_dir / "report.json")
            report = ReplayReport(
                schema_version=_SCHEMA_VERSION,
                id=report_id,
                candidate_id=candidate.id,
                candidate_digest=candidate.digest,
                candidate_revision=candidate.revision,
                suite_id=loaded_suite.id,
                suite_digest=loaded_suite.digest,
                source_revision=source_revision,
                phase=phase,
                status=status,
                baseline_score=baseline_score,
                candidate_score=candidate_score,
                baseline_harness_sha256=baseline_harness_sha256,
                candidate_harness_sha256=candidate_harness_sha256,
                improvements=improvements,
                regressions=regressions,
                case_results=tuple(results),
                artifact_dir=artifact_dir,
                report_path=report_path,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=round((time.monotonic() - started_clock) * 1000),
                limitations=limitations,
                digest="",
            )
            report = replace(report, digest=self._report_digest(report))
            await asyncio.to_thread(
                self._write_json_atomic,
                report_path,
                self._report_payload(report),
            )
            return report
        finally:
            workspaces = [
                item
                for item in (baseline_workspace, candidate_workspace)
                if item is not None
            ]
            if workspaces:
                await asyncio.gather(
                    *(
                        self.workspace_manager.discard(item, repo=repo_root)
                        for item in workspaces
                    ),
                    return_exceptions=True,
                )

    async def load_report(
        self,
        report_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> ReplayReport:
        self._id(report_id, "replay")
        _, common_dir, _ = await self.lab._repo(repo)
        path = self._managed_path(
            self._report_root(common_dir) / report_id / "report.json"
        )
        report = await asyncio.to_thread(self._load_report_sync, path, report_id)
        suite = await self.load_suite(report.suite_id, repo=repo)
        return await asyncio.to_thread(self._validate_report_suite, report, suite)

    async def _run_case(
        self,
        case: ReplayCase,
        workspace: Path,
        harness_dir: Path,
        artifact_dir: Path,
        variant: str,
        candidate_id: str,
        scope: str,
    ) -> ProcessEvidence:
        cwd = (workspace / case.cwd).resolve()
        if not cwd.is_relative_to(workspace.resolve()) or not cwd.is_dir():
            raise EvolutionError(f"replay case {case.id} cwd escapes its workspace")
        artifact_dir = self._managed_path(artifact_dir)
        stdout_path = self._managed_path(artifact_dir / f"{case.id}-{variant}.stdout")
        stderr_path = self._managed_path(artifact_dir / f"{case.id}-{variant}.stderr")
        env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        env.update(case.env or {})
        private_home = self._managed_path(artifact_dir / f"{case.id}-{variant}-home")
        private_tmp = self._managed_path(artifact_dir / f"{case.id}-{variant}-tmp")
        private_home.mkdir(mode=0o700)
        private_tmp.mkdir(mode=0o700)
        env.update(
            {
                "CI": "1",
                "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": str(private_home),
                "USERPROFILE": str(private_home),
                "TMPDIR": str(private_tmp),
                "TEMP": str(private_tmp),
                "TMP": str(private_tmp),
                "EVOLUTION_VARIANT": variant,
                "EVOLUTION_CANDIDATE_ID": candidate_id,
            }
        )
        harness_key = (
            "RLM_HARNESS_STATE_DIR"
            if scope == "local"
            else "RLM_GLOBAL_HARNESS_STATE_DIR"
        )
        env[harness_key] = str(harness_dir)
        started = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        output_error: str | None = None
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *case.argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
            completion = None
            try:
                stdout_task = asyncio.create_task(
                    self._capture(process.stdout, stdout_path)
                )
                stderr_task = asyncio.create_task(
                    self._capture(process.stderr, stderr_path)
                )
                completion = asyncio.gather(process.wait(), stdout_task, stderr_task)
                completed = await asyncio.wait_for(
                    completion,
                    timeout=case.timeout_seconds,
                )
                exit_code = completed[0]
            except _TIMEOUT_ERRORS:
                timed_out = True
                await self._terminate_process(process, stdout_task, stderr_task)
                exit_code = process.returncode
            except _OutputLimitExceeded as error:
                output_error = str(error)
                await self._terminate_process(process, stdout_task, stderr_task)
                exit_code = process.returncode
            except BaseException:
                await self._terminate_process(process, stdout_task, stderr_task)
                raise
            finally:
                if completion is not None:
                    if not completion.done():
                        completion.cancel()
                    await asyncio.gather(completion, return_exceptions=True)
        except FileNotFoundError as error:
            self._managed_path(stdout_path).write_bytes(b"")
            self._managed_path(stderr_path).write_text(str(error), encoding="utf-8")
            output_error = str(error)
        try:
            stdout = await asyncio.to_thread(
                self._managed_path(stdout_path).read_text,
                encoding="utf-8",
                errors="replace",
            )
            stderr = await asyncio.to_thread(
                self._managed_path(stderr_path).read_text,
                encoding="utf-8",
                errors="replace",
            )
            stdout_sha256 = await asyncio.to_thread(self._managed_sha256, stdout_path)
            stderr_sha256 = await asyncio.to_thread(self._managed_sha256, stderr_path)
        except BaseException:
            if process is not None:
                await self._terminate_process(process, stdout_task, stderr_task)
            raise
        passed = (
            not timed_out
            and output_error is None
            and exit_code == case.expected_exit
            and all(expected in stdout for expected in case.stdout_contains)
            and all(forbidden not in stdout for forbidden in case.stdout_excludes)
        )
        return ProcessEvidence(
            variant=variant,
            passed=passed,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            stdout_preview=stdout[:_PREVIEW_BYTES],
            stderr_preview=(f"{output_error}\n" if output_error else "")
            + stderr[:_PREVIEW_BYTES],
        )

    async def _capture(self, stream: asyncio.StreamReader | None, path: Path) -> None:
        path = self._managed_path(path)
        if stream is None:
            path.write_bytes(b"")
            return
        total = 0
        with self._managed_path(path).open("wb") as output:
            while chunk := await stream.read(64 * 1024):
                total += len(chunk)
                if total > _MAX_OUTPUT_BYTES:
                    raise _OutputLimitExceeded(
                        f"process output exceeded {_MAX_OUTPUT_BYTES} bytes"
                    )
                output.write(chunk)

    @staticmethod
    def _kill_process(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                if process.returncode is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @classmethod
    async def _terminate_process(
        cls,
        process: asyncio.subprocess.Process,
        *capture_tasks: asyncio.Task[None] | None,
    ) -> None:
        try:
            cls._kill_process(process)
            await process.wait()
        finally:
            tasks = tuple(task for task in capture_tasks if task is not None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _prepare_harnesses(
        self,
        candidate: MemoryCandidate,
        source_dir: Path,
        baseline_dir: Path,
        candidate_dir: Path,
        shadow_entry: Mapping[str, object] | None,
    ) -> None:
        baseline = self._load_harness(source_dir / "harness_state.json")
        self._write_json_atomic(
            self._managed_path(baseline_dir / "harness_state.json"),
            baseline,
        )
        changed = json.loads(json.dumps(baseline))
        records = changed["entries"]["memory"]
        before = records.get(candidate.target_id)
        if shadow_entry is None:
            timestamp = datetime.now(timezone.utc).isoformat()
            records[candidate.target_id] = ReplayRuntime._harness_entry(
                candidate,
                before,
                status="shadow",
                timestamp=timestamp,
            )
        else:
            records[candidate.target_id] = json.loads(json.dumps(shadow_entry))
        self._write_json_atomic(
            self._managed_path(candidate_dir / "harness_state.json"),
            changed,
        )

    @staticmethod
    def _harness_entry(
        candidate: MemoryCandidate,
        before: object,
        *,
        status: str,
        timestamp: str,
    ) -> dict[str, object]:
        previous = before if isinstance(before, dict) else {}
        created_at = (
            previous.get("created_at")
            if isinstance(previous.get("created_at"), str)
            else timestamp
        )
        version = (
            previous.get("version") if isinstance(previous.get("version"), int) else 0
        )
        metadata = {
            **(
                previous.get("metadata")
                if isinstance(previous.get("metadata"), dict)
                else {}
            ),
            **candidate.metadata,
            "evolution_candidate_id": candidate.id,
            "evolution_candidate_digest": candidate.digest,
            "evolution_status": status,
            "knowledge_type": candidate.category,
            "confidence": candidate.confidence,
            "applies_to": list(candidate.applies_to),
            "evidence": [asdict(item) for item in candidate.evidence],
            "expires_at": candidate.expires_at,
            "code_version": candidate.code_version,
            "dependency_hashes": candidate.dependency_hashes,
        }
        return {
            "id": candidate.target_id,
            "kind": "memory",
            "title": candidate.title,
            "content": candidate.claim,
            "path": candidate.path,
            "scope": candidate.scope,
            "reference": {},
            "arguments": {},
            "metadata": metadata,
            "source": "evolution",
            "created_at": created_at,
            "updated_at": timestamp,
            "version": version + 1,
        }

    @staticmethod
    def _admitted_shadow_entry(
        candidate: MemoryCandidate,
        admitted: Mapping[str, object],
    ) -> dict[str, object]:
        entries = admitted.get("entries")
        memories = entries.get("memory") if isinstance(entries, dict) else None
        if not isinstance(memories, dict):
            raise EvolutionError("candidate shadow harness memory entries are invalid")
        entry = memories.get(candidate.target_id)
        if not isinstance(entry, dict) or entry.get("id") != candidate.target_id:
            raise EvolutionError("candidate shadow target entry is missing or invalid")
        matches = [
            key
            for key, value in memories.items()
            if isinstance(value, dict)
            and (
                value.get("id") == candidate.target_id
                or (
                    isinstance(value.get("metadata"), dict)
                    and value["metadata"].get("evolution_candidate_id") == candidate.id
                )
            )
        ]
        if matches != [candidate.target_id]:
            raise EvolutionError("candidate shadow target entry is ambiguous")
        metadata = entry.get("metadata")
        shadow = candidate.metadata.get("shadow")
        source_digest = (
            shadow.get("source_candidate_digest") if isinstance(shadow, dict) else None
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("evolution_candidate_id") != candidate.id
            or not isinstance(source_digest, str)
            or metadata.get("evolution_candidate_digest") != source_digest
            or metadata.get("evolution_status") != "shadow"
        ):
            raise EvolutionError("candidate shadow target entry lineage is invalid")
        return json.loads(json.dumps(entry))

    @staticmethod
    def _load_harness(path: Path) -> dict[str, object]:
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise EvolutionError(
                    f"harness state is invalid JSON: {path}"
                ) from error
            if not isinstance(payload, dict):
                raise EvolutionError(f"harness state must be an object: {path}")
        else:
            payload = {}
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            entries = {}
            payload["entries"] = entries
        for kind in ("prompt", "memory", "skill", "subagent"):
            if not isinstance(entries.get(kind), dict):
                entries[kind] = {}
        if not isinstance(payload.get("refinements"), list):
            payload["refinements"] = []
        if not isinstance(payload.get("schema"), int):
            payload["schema"] = 1
        return payload

    def _harness_dir(
        self,
        candidate: MemoryCandidate,
        supplied: str | os.PathLike[str] | None,
    ) -> Path:
        if supplied is not None:
            return Path(supplied).expanduser().resolve()
        if candidate.scope == "local":
            raw = os.environ.get("RLM_HARNESS_STATE_DIR")
            if not raw and (session := os.environ.get("RLM_SESSION_DIR")):
                raw = str(Path(session) / "harness")
        else:
            raw = os.environ.get("RLM_GLOBAL_HARNESS_STATE_DIR")
            if not raw:
                agent_dir = os.environ.get(
                    "PRIME_AGENT_CODING_AGENT_DIR"
                ) or os.environ.get("PI_CODING_AGENT_DIR")
                raw = (
                    str(Path(agent_dir).expanduser() / "harness") if agent_dir else None
                )
        if not raw:
            raise EvolutionError(
                "harness_state_dir is required when no matching RLM harness environment is set"
            )
        return Path(raw).expanduser().resolve()

    def _attested_shadow_entry(
        self,
        candidate: MemoryCandidate,
        common_dir: Path,
    ) -> dict[str, object]:
        metadata = candidate.metadata.get("shadow")
        if not isinstance(metadata, dict):
            raise EvolutionError("candidate has no shadow harness metadata")
        raw_path = metadata.get("shadow_state_path")
        expected_hash = metadata.get("shadow_state_sha256")
        source_digest = metadata.get("source_candidate_digest")
        if (
            not isinstance(raw_path, str)
            or not isinstance(expected_hash, str)
            or not isinstance(source_digest, str)
        ):
            raise EvolutionError("candidate shadow harness metadata is invalid")
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        shadow_root = (self.lab.state_root / "shadows").absolute()
        expected_dir = shadow_root / key / candidate.id
        expected_state = expected_dir / "harness_state.json"
        supplied_state = Path(os.path.abspath(Path(raw_path).expanduser()))
        if supplied_state != expected_state:
            raise EvolutionError(
                "candidate shadow harness path is not repository-owned"
            )
        cursor = expected_dir
        while True:
            if cursor.is_symlink():
                raise EvolutionError(
                    f"candidate shadow harness path is a symlink: {cursor}"
                )
            if cursor == shadow_root:
                break
            if not cursor.is_relative_to(shadow_root):
                raise EvolutionError(
                    "candidate shadow harness escapes Evolution Lab state"
                )
            cursor = cursor.parent
        if expected_state.is_symlink() or not expected_state.is_file():
            raise EvolutionError("candidate shadow harness is missing or unsafe")
        try:
            raw = expected_state.read_bytes()
        except OSError as error:
            raise EvolutionError("candidate shadow harness cannot be read") from error
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise EvolutionError("candidate shadow harness hash mismatch")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvolutionError("candidate shadow harness is invalid JSON") from error
        if not isinstance(payload, dict):
            raise EvolutionError("candidate shadow harness must be an object")
        return self._admitted_shadow_entry(candidate, payload)

    def _suite_path(self, common_dir: Path, suite_id: str) -> Path:
        return self._repo_root(common_dir) / "suites" / f"{suite_id}.json"

    def _report_root(self, common_dir: Path) -> Path:
        return self._repo_root(common_dir) / "reports"

    def _repo_root(self, common_dir: Path) -> Path:
        key = hashlib.sha256(
            os.path.normcase(str(common_dir.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        return self.lab.state_root / "replays" / key

    def _managed_path(self, path: Path) -> Path:
        root = Path(os.path.abspath(self.lab.state_root.expanduser()))
        candidate = Path(os.path.abspath(path.expanduser()))
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise EvolutionError(
                f"managed replay path escapes Evolution Lab state: {candidate}"
            ) from error
        cursor = root
        try:
            if cursor.is_symlink():
                raise EvolutionError(f"managed replay path uses a symlink: {cursor}")
            for component in relative.parts:
                cursor /= component
                if cursor.is_symlink():
                    raise EvolutionError(
                        f"managed replay path uses a symlink: {cursor}"
                    )
        except OSError as error:
            raise EvolutionError(
                f"managed replay path is unsafe: {candidate}"
            ) from error
        return candidate

    def _managed_sha256(self, path: Path) -> str:
        return self._sha256(self._managed_path(path))

    @staticmethod
    def _validate_cases(cases: Sequence[ReplayCase]) -> tuple[ReplayCase, ...]:
        if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
            raise EvolutionError("cases must be a sequence of ReplayCase values")
        normalized = tuple(cases)
        if not normalized or len(normalized) > _MAX_CASES:
            raise EvolutionError(f"replay suites require 1 to {_MAX_CASES} cases")
        seen: set[str] = set()
        for case in normalized:
            if not isinstance(case, ReplayCase):
                raise EvolutionError("cases must contain ReplayCase values")
            ReplayRuntime._id(case.id, "case")
            ReplayRuntime._text(case.title, "case title")
            if not case.argv or any(
                not isinstance(value, str) or not value for value in case.argv
            ):
                raise EvolutionError(
                    f"replay case {case.id} argv must be non-empty strings"
                )
            if Path(case.cwd).is_absolute() or ".." in Path(case.cwd).parts:
                raise EvolutionError(
                    f"replay case {case.id} cwd must be workspace-relative"
                )
            if not math_is_finite_positive(case.timeout_seconds):
                raise EvolutionError(
                    f"replay case {case.id} timeout must be positive and finite"
                )
            if not math_is_finite_positive(case.weight):
                raise EvolutionError(
                    f"replay case {case.id} weight must be positive and finite"
                )
            if case.env is not None and (
                not isinstance(case.env, Mapping)
                or len(case.env) > 128
                or any(
                    not isinstance(key, str) or not key or not isinstance(value, str)
                    for key, value in case.env.items()
                )
            ):
                raise EvolutionError(
                    f"replay case {case.id} env must map at most 128 names to strings"
                )
            if case.id in seen:
                raise EvolutionError(f"duplicate replay case id: {case.id}")
            seen.add(case.id)
        return normalized

    @classmethod
    def _validate_suite(cls, suite: ReplaySuite) -> ReplaySuite:
        if (
            not isinstance(suite, ReplaySuite)
            or suite.schema_version != _SCHEMA_VERSION
        ):
            raise EvolutionError("invalid replay suite")
        cls._validate_cases(suite.cases)
        if cls._suite_digest(suite) != suite.digest:
            raise EvolutionError(f"replay suite digest mismatch: {suite.id}")
        return suite

    def _validate_report_suite(
        self,
        report: ReplayReport,
        suite: ReplaySuite,
    ) -> ReplayReport:
        if report.suite_digest != suite.digest:
            raise EvolutionError(f"replay report suite digest mismatch: {report.id}")
        if len(report.case_results) != len(suite.cases):
            raise EvolutionError(f"replay report case count mismatch: {report.id}")
        improvements: list[str] = []
        regressions: list[str] = []
        baseline_score = 0.0
        candidate_score = 0.0
        for case, result in zip(suite.cases, report.case_results, strict=True):
            if (
                result.case_id != case.id
                or result.title != case.title
                or result.weight != case.weight
            ):
                raise EvolutionError(
                    f"replay report case metadata mismatch: {report.id}/{case.id}"
                )
            passed: dict[str, bool] = {}
            for variant, evidence in (
                ("baseline", result.baseline),
                ("candidate", result.candidate),
            ):
                if evidence.variant != variant:
                    raise EvolutionError(
                        f"replay report variant mismatch: {report.id}/{case.id}"
                    )
                stdout_path = self._managed_path(evidence.stdout_path)
                if stdout_path.stat().st_size > _MAX_OUTPUT_BYTES:
                    raise EvolutionError(
                        f"replay output exceeds limit: {report.id}/{case.id}"
                    )
                stdout = self._managed_path(stdout_path).read_text(
                    encoding="utf-8", errors="replace"
                )
                passed[variant] = (
                    not evidence.timed_out
                    and evidence.exit_code == case.expected_exit
                    and all(expected in stdout for expected in case.stdout_contains)
                    and all(
                        forbidden not in stdout for forbidden in case.stdout_excludes
                    )
                )
                if evidence.passed is not passed[variant]:
                    raise EvolutionError(
                        f"replay report outcome mismatch: {report.id}/{case.id}/{variant}"
                    )
            improved = passed["candidate"] and not passed["baseline"]
            regressed = passed["baseline"] and not passed["candidate"]
            if result.improved is not improved or result.regressed is not regressed:
                raise EvolutionError(
                    f"replay report comparison mismatch: {report.id}/{case.id}"
                )
            if passed["baseline"]:
                baseline_score += case.weight
            if passed["candidate"]:
                candidate_score += case.weight
            if improved:
                improvements.append(case.id)
            if regressed:
                regressions.append(case.id)
        enough_improvement = (
            not suite.require_improvement
            or len(improvements) >= suite.minimum_improvements
        )
        status = "passed" if not regressions and enough_improvement else "failed"
        if (
            report.baseline_score != baseline_score
            or report.candidate_score != candidate_score
            or report.improvements != tuple(improvements)
            or report.regressions != tuple(regressions)
            or report.status != status
        ):
            raise EvolutionError(f"replay report decision mismatch: {report.id}")
        return report

    @staticmethod
    def _suite_digest(suite: ReplaySuite) -> str:
        payload = asdict(suite)
        payload["digest"] = None
        return hashlib.sha256(ReplayRuntime._canonical(payload)).hexdigest()

    @staticmethod
    def _report_digest(report: ReplayReport) -> str:
        payload = ReplayRuntime._report_payload(report)
        payload["digest"] = None
        return hashlib.sha256(ReplayRuntime._canonical(payload)).hexdigest()

    @staticmethod
    def _suite_payload(suite: ReplaySuite) -> dict[str, object]:
        return asdict(suite)

    @staticmethod
    def _report_payload(report: ReplayReport) -> dict[str, object]:
        payload = asdict(report)
        payload["artifact_dir"] = str(report.artifact_dir)
        payload["report_path"] = str(report.report_path)
        for result in payload["case_results"]:
            for variant in ("baseline", "candidate"):
                evidence = result[variant]
                evidence["stdout_path"] = str(evidence["stdout_path"])
                evidence["stderr_path"] = str(evidence["stderr_path"])
        return payload

    def _load_suite_sync(
        self,
        path: Path,
        requested_suite_id: str,
    ) -> ReplaySuite:
        path = self._managed_path(path)
        try:
            payload = json.loads(self._managed_path(path).read_text(encoding="utf-8"))
            suite = ReplaySuite(
                schema_version=payload["schema_version"],
                id=payload["id"],
                name=payload["name"],
                cases=tuple(
                    ReplayCase(
                        id=item["id"],
                        title=item["title"],
                        argv=tuple(item["argv"]),
                        cwd=item["cwd"],
                        timeout_seconds=item["timeout_seconds"],
                        expected_exit=item["expected_exit"],
                        stdout_contains=tuple(item["stdout_contains"]),
                        stdout_excludes=tuple(item["stdout_excludes"]),
                        env=dict(item["env"]) if item["env"] is not None else None,
                        weight=item["weight"],
                    )
                    for item in payload["cases"]
                ),
                require_improvement=payload["require_improvement"],
                minimum_improvements=payload["minimum_improvements"],
                created_at=payload["created_at"],
                digest=payload["digest"],
            )
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise EvolutionError(f"replay suite is invalid: {path}") from error
        if suite.id != requested_suite_id or path.name != f"{requested_suite_id}.json":
            raise EvolutionError(
                f"replay suite id does not match ledger path: {requested_suite_id}"
            )
        return self._validate_suite(suite)

    def _load_report_sync(
        self,
        path: Path,
        requested_report_id: str,
    ) -> ReplayReport:
        path = self._managed_path(path)
        try:
            payload = json.loads(self._managed_path(path).read_text(encoding="utf-8"))
            results: list[ReplayCaseResult] = []
            for item in payload["case_results"]:
                evidence: dict[str, ProcessEvidence] = {}
                for variant in ("baseline", "candidate"):
                    raw = item[variant]
                    evidence[variant] = ProcessEvidence(
                        variant=raw["variant"],
                        passed=raw["passed"],
                        exit_code=raw["exit_code"],
                        timed_out=raw["timed_out"],
                        duration_ms=raw["duration_ms"],
                        stdout_path=Path(raw["stdout_path"]),
                        stderr_path=Path(raw["stderr_path"]),
                        stdout_sha256=raw["stdout_sha256"],
                        stderr_sha256=raw["stderr_sha256"],
                        stdout_preview=raw["stdout_preview"],
                        stderr_preview=raw["stderr_preview"],
                    )
                results.append(
                    ReplayCaseResult(
                        case_id=item["case_id"],
                        title=item["title"],
                        weight=item["weight"],
                        baseline=evidence["baseline"],
                        candidate=evidence["candidate"],
                        improved=item["improved"],
                        regressed=item["regressed"],
                    )
                )
            report = ReplayReport(
                schema_version=payload["schema_version"],
                id=payload["id"],
                candidate_id=payload["candidate_id"],
                candidate_digest=payload["candidate_digest"],
                candidate_revision=payload["candidate_revision"],
                suite_id=payload["suite_id"],
                suite_digest=payload["suite_digest"],
                source_revision=payload["source_revision"],
                phase=payload["phase"],
                status=payload["status"],
                baseline_score=payload["baseline_score"],
                baseline_harness_sha256=payload["baseline_harness_sha256"],
                candidate_harness_sha256=payload["candidate_harness_sha256"],
                candidate_score=payload["candidate_score"],
                improvements=tuple(payload["improvements"]),
                regressions=tuple(payload["regressions"]),
                case_results=tuple(results),
                artifact_dir=Path(payload["artifact_dir"]),
                report_path=Path(payload["report_path"]),
                started_at=payload["started_at"],
                finished_at=payload["finished_at"],
                duration_ms=payload["duration_ms"],
                limitations=tuple(payload["limitations"]),
                digest=payload["digest"],
            )
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise EvolutionError(f"replay report is invalid: {path}") from error
        expected_artifact_dir = self._managed_path(path.parent)
        if report.id != requested_report_id or path.parent.name != requested_report_id:
            raise EvolutionError(
                f"replay report id does not match ledger path: {requested_report_id}"
            )
        report_path = self._managed_path(report.report_path)
        artifact_dir = self._managed_path(report.artifact_dir)
        if report_path != path or artifact_dir != expected_artifact_dir:
            raise EvolutionError(
                f"replay report artifact paths are invalid: {report.id}"
            )
        if (
            report.schema_version != _SCHEMA_VERSION
            or ReplayRuntime._report_digest(report) != report.digest
        ):
            raise EvolutionError(f"replay report digest mismatch: {report.id}")
        for result in report.case_results:
            for evidence in (result.baseline, result.candidate):
                for label, output_path, expected_hash in (
                    ("stdout", evidence.stdout_path, evidence.stdout_sha256),
                    ("stderr", evidence.stderr_path, evidence.stderr_sha256),
                ):
                    absolute_output = self._managed_path(output_path)
                    if (
                        not absolute_output.is_file()
                        or not absolute_output.is_relative_to(expected_artifact_dir)
                        or self._managed_sha256(absolute_output) != expected_hash
                    ):
                        raise EvolutionError(
                            "replay "
                            f"{label} evidence is invalid: "
                            f"{report.id}/{result.case_id}"
                        )
        harness_hashes = (
            (
                report.artifact_dir / "harness-baseline" / "harness_state.json",
                report.baseline_harness_sha256,
            ),
            (
                report.artifact_dir / "harness-candidate" / "harness_state.json",
                report.candidate_harness_sha256,
            ),
        )
        for harness_path, expected in harness_hashes:
            harness_path = self._managed_path(harness_path)
            if (
                not harness_path.is_file()
                or not harness_path.is_relative_to(expected_artifact_dir)
                or self._managed_sha256(harness_path) != expected
            ):
                raise EvolutionError(
                    "replay harness hash mismatch: "
                    f"{report.id}/{harness_path.parent.name}"
                )
        return report

    def _write_json_atomic(self, path: Path, payload: object) -> None:
        path = self._managed_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self._managed_path(path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(self._managed_path(Path(temporary_name)), 0o600)
            os.replace(
                self._managed_path(Path(temporary_name)),
                self._managed_path(path),
            )
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise EvolutionError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _id(value: object, kind: str) -> str:
        if not isinstance(value, str) or not re_id(value, kind):
            raise EvolutionError(f"invalid {kind} id")
        return value


def re_id(value: str, kind: str) -> bool:
    if kind == "case":
        return (
            bool(value)
            and len(value) <= 80
            and all(character.isalnum() or character in "_-" for character in value)
        )
    return (
        len(value) == len(kind) + 25
        and value.startswith(f"{kind}-")
        and all(character in "0123456789abcdef" for character in value[len(kind) + 1 :])
    )


def math_is_finite_positive(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
        and value < float("inf")
    )
