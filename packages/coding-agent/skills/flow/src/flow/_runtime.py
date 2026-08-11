from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from rlm import delete_subagent, list_subagents, rlm

from ._blackboard import ArtifactBlackboard
from ._models import (
    ArtifactRecord,
    ArtifactSpec,
    ArtifactValidationError,
    ChildLifecycleCallback,
    FlowBudget,
    FlowBudgetExceeded,
    FlowConflict,
    FlowError,
    FlowNotFound,
    FlowRecord,
    FlowResult,
    JsonValue,
    TaskContext,
    TaskExecution,
    TaskExecutionError,
    TaskPolicy,
    TaskRecord,
    TaskSpec,
)
from ._store import FlowStore

_SCHEMA_VERSION = 1
_MAX_EXECUTOR_OUTPUT_BYTES = 16 * 1024 * 1024
_TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled"})
_TERMINAL_FLOW_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_EXECUTION_REPO: ContextVar[Path | None] = ContextVar(
    "flow_execution_repo", default=None
)


@runtime_checkable
class TaskExecutor(Protocol):
    async def execute(self, context: TaskContext) -> TaskExecution: ...


class _AttemptExecutionError(TaskExecutionError):
    def __init__(self, message: str, *, tokens: int = 0, retryable: bool = True):
        super().__init__(message)
        self.tokens = tokens
        self.retryable = retryable


class _ChildUnsettledError(_AttemptExecutionError):
    def __init__(self, message: str, *, tokens: int = 0):
        super().__init__(message, tokens=tokens, retryable=False)


@dataclass(frozen=True, slots=True)
class _RecoveredAttempt:
    execution: TaskExecution | None = None
    tokens: int = 0
    error: BaseException | None = None
    settled: bool = True


class RlmTaskExecutor:
    """Execute one task in a direct RLM child with a file-only result contract."""

    def __init__(
        self,
        *,
        spawn: Callable[..., object] = rlm,
        list_children: Callable[..., object] = list_subagents,
        delete_child: Callable[..., object] = delete_subagent,
        poll_seconds: float = 0.25,
        max_output_bytes: int = _MAX_EXECUTOR_OUTPUT_BYTES,
    ) -> None:
        if (
            not callable(spawn)
            or not callable(list_children)
            or not callable(delete_child)
        ):
            raise TypeError("RLM lifecycle functions must be callable")
        if (
            not isinstance(poll_seconds, (int, float))
            or isinstance(poll_seconds, bool)
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds must be positive")
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        self._spawn = spawn
        self._list_children = list_children
        self._delete_child = delete_child
        self._poll_seconds = float(poll_seconds)
        self._max_output_bytes = max_output_bytes

    async def execute(self, context: TaskContext) -> TaskExecution:
        artifact_dir = context.artifact_dir.resolve()
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            artifact_dir.chmod(0o700)
        except OSError:
            pass
        output_path = artifact_dir / "outputs.json"
        repo = (_EXECUTION_REPO.get() or Path.cwd()).resolve()
        route = context.spec.route or "code"
        name = _child_name(context)
        child_id: str | None = None
        try:
            await self._delete_existing(name)
            output_path.unlink(missing_ok=True)
            prompt = self._prompt(context, output_path)
            admitted = self._spawn(
                prompt,
                name=name,
                route=route,
                task_type=context.spec.worker,
                cwd=str(repo),
                capabilities={
                    "filesystem": {
                        "read": [str(repo), str(artifact_dir)],
                        "write": [str(repo), str(artifact_dir)],
                    }
                },
            )
            handle = await admitted if inspect.isawaitable(admitted) else admitted
            child_id = _nonempty_attr(handle, "rlm_child_id")
            await self._notify(context, child_id, name, "running", None, False)
            child = await self._wait_for_terminal(child_id)
            status = str(getattr(child, "status", "")).lower()
            try:
                tokens = (
                    self._usage_tokens(child, required=context.require_token_usage) or 0
                )
            except _AttemptExecutionError:
                await self._notify(context, child_id, name, status, None, True)
                await self._cleanup_terminal(child_id)
                raise
            await self._notify(context, child_id, name, status, tokens, True)
            if status == "error":
                detail = getattr(child, "error", None) or getattr(
                    child, "last_error", None
                )
                suffix = f": {detail}" if detail else ""
                cleanup_error = await self._cleanup_terminal(child_id)
                cleanup_suffix = (
                    f"; child cleanup failed: {cleanup_error}" if cleanup_error else ""
                )
                raise _AttemptExecutionError(
                    f"direct RLM child {child_id!r} failed{suffix}{cleanup_suffix}",
                    tokens=tokens,
                )
            try:
                outputs = self._read_outputs(output_path, context.spec)
            except Exception as error:
                cleanup_error = await self._cleanup_terminal(child_id)
                suffix = (
                    f"; child cleanup failed: {cleanup_error}" if cleanup_error else ""
                )
                raise _AttemptExecutionError(
                    f"{error}{suffix}", tokens=tokens
                ) from error
            cleanup_error = await self._cleanup_terminal(child_id)
            metadata: dict[str, JsonValue] = {}
            if cleanup_error is not None:
                metadata["child_cleanup_error"] = cleanup_error
            return TaskExecution(outputs=outputs, tokens=tokens, metadata=metadata)
        except asyncio.CancelledError as cancelled:
            settled, tokens, discovered_id = await self._settle_cancelled_child(
                child_id, name
            )
            if discovered_id is not None:
                try:
                    await self._notify(
                        context,
                        discovered_id,
                        name,
                        "deleted" if settled else "running",
                        tokens,
                        settled,
                    )
                except Exception as error:
                    raise _ChildUnsettledError(
                        f"could not persist child settlement: {error}",
                        tokens=tokens or 0,
                    ) from error
            if not settled:
                raise _ChildUnsettledError(
                    f"direct RLM child {discovered_id or name!r} may still be running",
                    tokens=tokens or 0,
                ) from cancelled
            if context.require_token_usage and tokens is None:
                raise _ChildUnsettledError(
                    "cancelled RLM child usage could not be measured"
                ) from cancelled
            raise
        except _AttemptExecutionError:
            raise
        except Exception as error:
            settled, tokens, discovered_id = await self._settle_cancelled_child(
                child_id, name
            )
            if discovered_id is not None:
                await self._notify(
                    context,
                    discovered_id,
                    name,
                    "deleted" if settled else "running",
                    tokens,
                    settled,
                )
            if not settled:
                raise _ChildUnsettledError(
                    f"task failed and direct RLM child {discovered_id or name!r} may still be running",
                    tokens=tokens or 0,
                ) from error
            if (
                context.require_token_usage
                and discovered_id is not None
                and tokens is None
            ):
                raise _AttemptExecutionError(
                    f"task failed and RLM child usage could not be measured: {error}",
                    retryable=False,
                ) from error
            raise _AttemptExecutionError(
                f"task {context.spec.id!r} attempt {context.attempt} failed: {error}",
                tokens=tokens or 0,
            ) from error

    async def recover_attempt(
        self, context: TaskContext, task: TaskRecord
    ) -> _RecoveredAttempt:
        name = task.child_name or _child_name(context)
        if task.child_status == "completed" and task.child_settled is True:
            try:
                tokens = self._persisted_tokens(
                    task.child_usage_tokens,
                    required=context.require_token_usage,
                )
            except _AttemptExecutionError as error:
                return _RecoveredAttempt(error=error)
            try:
                outputs = self._read_outputs(
                    context.artifact_dir / "outputs.json", context.spec
                )
            except Exception as error:  # noqa: BLE001 - persisted output validation is a trust boundary
                return _RecoveredAttempt(
                    tokens=tokens,
                    error=_AttemptExecutionError(str(error), tokens=tokens),
                )
            return _RecoveredAttempt(
                execution=TaskExecution(outputs=outputs, tokens=tokens),
                tokens=tokens,
            )
        children = await self._children()
        child = self._matching_child(children, task.child_id, name)
        if child is not None and (
            (
                task.child_id is not None
                and getattr(child, "rlm_child_id", None) != task.child_id
            )
            or (
                task.child_name is not None
                and getattr(child, "session_name", None) != task.child_name
            )
        ):
            return _RecoveredAttempt(
                error=_ChildUnsettledError(
                    "persisted RLM child identity does not match the lifecycle registry"
                ),
                settled=False,
            )
        if child is None:
            if context.require_token_usage and task.child_usage_tokens is None:
                return _RecoveredAttempt(
                    error=_AttemptExecutionError(
                        "interrupted RLM child usage could not be measured",
                        retryable=False,
                    )
                )
            return _RecoveredAttempt(tokens=task.child_usage_tokens or 0)
        child_id = _nonempty_attr(child, "rlm_child_id")
        status = str(getattr(child, "status", "")).lower()
        if status in {"completed", "error"}:
            try:
                tokens = (
                    self._usage_tokens(child, required=context.require_token_usage) or 0
                )
            except _AttemptExecutionError as error:
                tokens = error.tokens
                await self._notify(context, child_id, name, status, None, True)
                await self._cleanup_terminal(child_id)
                return _RecoveredAttempt(tokens=tokens, error=error)
            await self._notify(context, child_id, name, status, tokens, True)
            cleanup_error = await self._cleanup_terminal(child_id)
            if status == "error":
                detail = getattr(child, "error", None) or getattr(
                    child, "last_error", None
                )
                suffix = f": {detail}" if detail else ""
                return _RecoveredAttempt(
                    tokens=tokens,
                    error=_AttemptExecutionError(
                        f"recovered RLM child failed{suffix}", tokens=tokens
                    ),
                )
            try:
                outputs = self._read_outputs(
                    context.artifact_dir / "outputs.json", context.spec
                )
            except Exception as error:  # noqa: BLE001 - persisted output validation is a trust boundary
                return _RecoveredAttempt(
                    tokens=tokens,
                    error=_AttemptExecutionError(str(error), tokens=tokens),
                )
            metadata: dict[str, JsonValue] = {}
            if cleanup_error is not None:
                metadata["child_cleanup_error"] = cleanup_error
            return _RecoveredAttempt(
                execution=TaskExecution(
                    outputs=outputs, tokens=tokens, metadata=metadata
                ),
                tokens=tokens,
            )
        if status != "running":
            return _RecoveredAttempt(
                error=_ChildUnsettledError(
                    f"direct RLM child {child_id!r} has unknown status {status!r}"
                ),
                settled=False,
            )
        tokens = self._usage_tokens(child, required=False)
        settled, cleanup_error = await self._delete_and_confirm(child_id)
        await self._notify(
            context,
            child_id,
            name,
            "deleted" if settled else "running",
            tokens,
            settled,
        )
        if not settled:
            return _RecoveredAttempt(
                tokens=tokens or 0,
                error=_ChildUnsettledError(
                    f"interrupted RLM child {child_id!r} may still be running",
                    tokens=tokens or 0,
                ),
                settled=False,
            )
        if context.require_token_usage and tokens is None:
            return _RecoveredAttempt(
                error=_AttemptExecutionError(
                    "interrupted RLM child usage could not be measured",
                    retryable=False,
                )
            )
        error = (
            TaskExecutionError(f"recovered interrupted attempt: {cleanup_error}")
            if cleanup_error
            else None
        )
        return _RecoveredAttempt(tokens=tokens or 0, error=error)

    async def _notify(
        self,
        context: TaskContext,
        child_id: str,
        name: str,
        status: str,
        tokens: int | None,
        settled: bool,
    ) -> None:
        if context.child_lifecycle is not None:
            await context.child_lifecycle(child_id, name, status, tokens, settled)

    async def _children(self) -> Sequence[object]:
        result = self._list_children()
        children = await result if inspect.isawaitable(result) else result
        if not isinstance(children, Sequence):
            raise TaskExecutionError("rlm.list_subagents returned an invalid result")
        return children

    @staticmethod
    def _matching_child(
        children: Sequence[object], child_id: str | None, name: str
    ) -> object | None:
        if child_id is not None:
            match = next(
                (
                    child
                    for child in children
                    if getattr(child, "rlm_child_id", None) == child_id
                ),
                None,
            )
            if match is not None:
                return match
        return next(
            (
                child
                for child in children
                if getattr(child, "session_name", None) == name
            ),
            None,
        )

    async def _delete_existing(self, name: str) -> None:
        for child in await self._children():
            if getattr(child, "session_name", None) != name:
                continue
            child_id = _nonempty_attr(child, "rlm_child_id")
            raise _ChildUnsettledError(
                f"existing direct RLM child {child_id!r} must be recovered before admission"
            )

    async def _wait_for_terminal(self, child_id: str) -> object:
        missing_polls = 0
        while True:
            children = await self._children()
            child = self._matching_child(children, child_id, "")
            if child is None:
                missing_polls += 1
                if missing_polls >= 4:
                    raise TaskExecutionError(
                        f"direct RLM child {child_id!r} disappeared"
                    )
            else:
                missing_polls = 0
                status_value = getattr(child, "status", "")
                status = str(status_value).lower()
                if status in {"completed", "error"}:
                    return child
                if status != "running":
                    raise TaskExecutionError(
                        f"direct RLM child {child_id!r} has unknown status {status_value!r}"
                    )
            await asyncio.sleep(self._poll_seconds)

    @staticmethod
    def _usage_tokens(child: object, *, required: bool) -> int | None:
        value = getattr(child, "usage_tokens", None)
        if value is None:
            if required:
                raise _AttemptExecutionError(
                    "completed RLM child usage could not be measured",
                    retryable=False,
                )
            return None
        if type(value) is not int or value < 0:
            raise _AttemptExecutionError(
                "RLM child returned invalid usage_tokens", retryable=False
            )
        return value

    @staticmethod
    def _persisted_tokens(value: int | None, *, required: bool) -> int:
        if value is None:
            if required:
                raise _AttemptExecutionError(
                    "persisted RLM child usage could not be measured",
                    retryable=False,
                )
            return 0
        if type(value) is not int or value < 0:
            raise _AttemptExecutionError(
                "persisted RLM child usage is invalid", retryable=False
            )
        return value

    async def _cleanup_terminal(self, child_id: str) -> str | None:
        try:
            deleted = self._delete_child(child_id)
            if inspect.isawaitable(deleted):
                await deleted
            return None
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - lifecycle callbacks are plugin boundaries
            return _bounded_error(error)

    async def _delete_and_confirm(self, child_id: str) -> tuple[bool, str | None]:
        cleanup_error: str | None = None
        try:
            deleted = self._delete_child(child_id)
            if inspect.isawaitable(deleted):
                await deleted
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - lifecycle callbacks are plugin boundaries
            cleanup_error = _bounded_error(error)
        try:
            children = await self._children()
        except Exception as error:  # noqa: BLE001 - registry backends are plugin boundaries
            detail = cleanup_error or _bounded_error(error)
            return False, detail
        child = self._matching_child(children, child_id, "")
        if child is None:
            return True, cleanup_error
        status = str(getattr(child, "status", "")).lower()
        return status in {"completed", "error"}, cleanup_error

    async def _settle_cancelled_child(
        self, child_id: str | None, name: str
    ) -> tuple[bool, int | None, str | None]:
        try:
            children = await self._children()
        except Exception:  # noqa: BLE001 - registry failure means settlement is unknown
            return False, None, child_id
        child = self._matching_child(children, child_id, name)
        if child is None:
            return True, None, child_id
        discovered_id = _nonempty_attr(child, "rlm_child_id")
        tokens = self._usage_tokens(child, required=False)
        status = str(getattr(child, "status", "")).lower()
        if status in {"completed", "error"}:
            await self._cleanup_terminal(discovered_id)
            return True, tokens, discovered_id
        settled, _ = await self._delete_and_confirm(discovered_id)
        return settled, tokens, discovered_id

    def _read_outputs(
        self, output_path: Path, spec: TaskSpec
    ) -> Mapping[str, JsonValue]:
        try:
            metadata = output_path.lstat()
        except FileNotFoundError as error:
            raise TaskExecutionError(
                "RLM child completed without outputs.json"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise TaskExecutionError("outputs.json must be a regular non-symlink file")
        if metadata.st_size > self._max_output_bytes:
            raise TaskExecutionError("outputs.json exceeds the configured size limit")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(output_path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise TaskExecutionError("outputs.json changed while it was opened")
            chunks: list[bytes] = []
            remaining = self._max_output_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload_bytes = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(payload_bytes) > self._max_output_bytes:
            raise TaskExecutionError("outputs.json exceeds the configured size limit")

        def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
            result: dict[str, JsonValue] = {}
            for key, value in pairs:
                if key in result:
                    raise TaskExecutionError(
                        f"outputs.json contains duplicate key {key!r}"
                    )
                result[key] = value
            return result

        def reject_constant(token: str) -> None:
            raise TaskExecutionError(f"outputs.json contains invalid number {token!r}")

        try:
            payload = json.loads(
                payload_bytes.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TaskExecutionError("outputs.json is not valid UTF-8 JSON") from error
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise TaskExecutionError("outputs.json must be a JSON object")
        expected = {artifact.name for artifact in spec.produces}
        actual = set(payload)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise TaskExecutionError(
                f"outputs.json keys do not match declared outputs; missing={missing}, unexpected={unexpected}"
            )
        return payload

    @staticmethod
    def _prompt(context: TaskContext, output_path: Path) -> str:
        schema = {
            artifact.name: {
                "type": artifact.value_type,
                "required_keys": list(artifact.required_keys),
            }
            for artifact in context.spec.produces
        }
        inputs = json.dumps(
            dict(context.inputs),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        contract = json.dumps(
            schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        isolation = (
            "This task requests an isolated execution trajectory. Do not coordinate with other children."
            if context.spec.isolated
            else "Work independently and do not rely on prose replies as task output."
        )
        return (
            f"Flow goal task: {context.spec.title}\n\n"
            f"{context.spec.prompt}\n\n"
            f"Attempt: {context.attempt}\n"
            f"Typed inputs (canonical JSON): {inputs}\n"
            f"Required output object schema: {contract}\n\n"
            f"{isolation}\n"
            "Completion contract (mandatory): write one UTF-8 JSON object whose keys are exactly the "
            "declared output names to the absolute path below. Values must match their declared JSON "
            "types and required object keys. Create a temporary file in the same directory, flush and "
            "fsync it, then commit it with os.replace; never write the destination incrementally. Do not "
            "report success unless that atomic replace has completed.\n"
            f"OUTPUT_PATH={output_path}\n"
        )


class FlowRuntime:
    """Owner of durable flow state, artifact storage, and executor coordination."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        executor: TaskExecutor | None = None,
        max_parallel: int = 4,
    ) -> None:
        if (
            not isinstance(max_parallel, int)
            or isinstance(max_parallel, bool)
            or max_parallel <= 0
        ):
            raise ValueError("max_parallel must be a positive integer")
        self.state_dir = (
            Path(state_dir).expanduser().resolve()
            if state_dir is not None
            else _default_state_dir()
        )
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = FlowStore(self.state_dir)
        self.blackboard = ArtifactBlackboard(self.state_dir / "blackboard")
        self.executor = executor or RlmTaskExecutor()
        if not isinstance(self.executor, TaskExecutor):
            raise TypeError("executor must implement TaskExecutor")
        self.max_parallel = max_parallel
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._active_runs: dict[str, asyncio.Task[FlowResult]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._run_leases: dict[str, tuple[int, Path]] = {}

    async def create(
        self,
        goal: str,
        *,
        repo: str | os.PathLike[str] = ".",
        budget: FlowBudget | None = None,
        max_parallel: int | None = None,
    ) -> Flow:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        parallel = (
            self.max_parallel
            if max_parallel is None
            else _positive_parallel(max_parallel)
        )
        repo_root = Path(repo).expanduser().resolve()
        now = _now()
        record = FlowRecord(
            schema_version=_SCHEMA_VERSION,
            id=uuid.uuid4().hex,
            revision=1,
            goal=goal.strip(),
            repo_root=repo_root,
            status="pending",
            budget=budget or FlowBudget(),
            tasks=(),
            created_at=now,
            updated_at=now,
        )
        persisted = self.store.create(record)
        return Flow(self, persisted.id, max_parallel=parallel)

    async def load(
        self,
        flow_id: str,
        *,
        repo: str | os.PathLike[str] = ".",
    ) -> Flow:
        record = self.store.get(flow_id)
        expected_repo = Path(repo).expanduser().resolve()
        if record.repo_root.resolve() != expected_repo:
            raise FlowNotFound(
                f"flow {flow_id!r} does not belong to repository {expected_repo}"
            )
        active = self._active_runs.get(flow_id)
        if active is not None and not active.done():
            return Flow(self, record.id)
        lease = await asyncio.to_thread(
            _acquire_flow_lease,
            _flow_lease_path(self.state_dir, flow_id),
        )
        try:
            lock = self._lock(flow_id)
            async with lock:
                latest = self.store.get(flow_id)
                latest = await Flow(self, latest.id)._recover_interrupted(latest)
        finally:
            await asyncio.to_thread(_release_flow_lease, lease)
        return Flow(self, latest.id)

    async def list(
        self,
        *,
        repo: str | os.PathLike[str] | None = None,
        statuses: Sequence[str] = (),
        limit: int = 200,
    ) -> list[FlowRecord]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        selected_statuses = tuple(statuses)
        unknown = set(selected_statuses) - {
            "pending",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        }
        if unknown:
            raise ValueError(f"unknown flow statuses: {sorted(unknown)}")
        repo_root = Path(repo).expanduser().resolve() if repo is not None else None
        records = self.store.list()
        filtered = [
            record
            for record in records
            if (repo_root is None or record.repo_root.resolve() == repo_root)
            and (not selected_statuses or record.status in selected_statuses)
        ]
        filtered.sort(
            key=lambda item: (
                -datetime.fromisoformat(item.updated_at).timestamp(),
                item.id,
            )
        )
        return filtered[:limit]

    def _lock(self, flow_id: str) -> asyncio.Lock:
        lock = self._locks.get(flow_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[flow_id] = lock
        return lock

    async def _start_run(self, flow: Flow) -> asyncio.Task[FlowResult]:
        async with self._guard:
            current = self._active_runs.get(flow.id)
            if current is not None and not current.done():
                return current
            lease = await asyncio.to_thread(
                _acquire_flow_lease,
                _flow_lease_path(self.state_dir, flow.id),
            )
            try:
                cancel_event = asyncio.Event()
                task = asyncio.create_task(
                    flow._drive(cancel_event),
                    name=f"flow-{flow.id[:12]}",
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(_release_flow_lease, lease)
                raise
            except Exception:
                await asyncio.to_thread(_release_flow_lease, lease)
                raise
            self._run_leases[flow.id] = lease
            self._active_runs[flow.id] = task
            self._cancel_events[flow.id] = cancel_event
            task.add_done_callback(
                lambda completed, flow_id=flow.id: self._run_finished(
                    flow_id, completed
                )
            )
            return task

    def _run_finished(self, flow_id: str, completed: asyncio.Task[FlowResult]) -> None:
        if self._active_runs.get(flow_id) is completed:
            self._active_runs.pop(flow_id, None)
            self._cancel_events.pop(flow_id, None)
            lease = self._run_leases.pop(flow_id, None)
            if lease is not None:
                _release_flow_lease(lease)


class Flow:
    def __init__(
        self, runtime: FlowRuntime, flow_id: str, *, max_parallel: int | None = None
    ) -> None:
        self.runtime = runtime
        self.id = flow_id
        self.blackboard = runtime.blackboard
        self.max_parallel = max_parallel or runtime.max_parallel

    async def task(
        self,
        title: str | TaskSpec,
        *,
        prompt: str = "",
        id: str | None = None,
        requires: Sequence[str] = (),
        quorum: str | int = "all",
        worker: str = "agent",
        route: str | None = None,
        isolated: bool = False,
        policy: TaskPolicy | None = None,
        consumes: Sequence[str] = (),
        produces: Sequence[ArtifactSpec] = (),
        resources: Sequence[str] = (),
    ) -> TaskSpec:
        if isinstance(title, TaskSpec):
            if (
                prompt
                or id is not None
                or requires
                or quorum != "all"
                or worker != "agent"
                or route is not None
                or isolated
                or policy is not None
                or consumes
                or produces
                or resources
            ):
                raise ValueError(
                    "TaskSpec cannot be combined with task field arguments"
                )
            spec = title
        else:
            if not isinstance(title, str) or not title.strip():
                raise ValueError("title must be a non-empty string")
            spec = TaskSpec(
                id=id or _stable_id(title),
                title=title.strip(),
                prompt=prompt or title.strip(),
                requires=tuple(requires),
                quorum=quorum,
                worker=worker,
                route=route,
                isolated=isolated,
                policy=policy or TaskPolicy(),
                consumes=tuple(consumes),
                produces=tuple(produces),
                resources=tuple(resources),
            )
        lock = self.runtime._lock(self.id)
        async with lock:
            record = self.runtime.store.get(self.id)
            if record.status != "pending":
                raise FlowConflict("tasks may only be added while a flow is pending")
            if any(existing.spec.id == spec.id for existing in record.tasks):
                raise FlowConflict(f"duplicate task id {spec.id!r}")
            known = {existing.spec.id for existing in record.tasks}
            missing = [
                dependency for dependency in spec.requires if dependency not in known
            ]
            if missing:
                raise FlowNotFound(
                    f"task {spec.id!r} has unknown dependencies: {missing}"
                )
            now = _now()
            self.runtime.store.update(
                replace(
                    record,
                    tasks=record.tasks + (TaskRecord(spec=spec),),
                    updated_at=now,
                ),
                expected_revision=record.revision,
            )
        return spec

    async def map(
        self,
        title: str,
        items: Sequence[str],
        *,
        prompt: Callable[[str], str] | str,
        id_prefix: str | None = None,
        worker: str = "agent",
        route: str | None = None,
        isolated: bool = False,
        policy: TaskPolicy | None = None,
        requires: Sequence[str] = (),
        quorum: str | int = "all",
        consumes: Sequence[str] = (),
        produces: Sequence[ArtifactSpec] = (),
        resources: Callable[[str], Sequence[str]] | Sequence[str] = (),
    ) -> tuple[TaskSpec, ...]:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        item_values = tuple(items)
        if not all(isinstance(item, str) and item.strip() for item in item_values):
            raise ValueError("map items must be non-empty strings")
        prefix = id_prefix or _slug(title)
        created: list[TaskSpec] = []
        for index, item in enumerate(item_values):
            task_prompt = prompt(item) if callable(prompt) else prompt.format(item=item)
            task_resources = resources(item) if callable(resources) else resources
            digest = hashlib.sha256(
                json.dumps(
                    [title, item, index], ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()[:10]
            task_id = f"{_slug(prefix)[:36]}-{index + 1}-{_slug(item)[:24]}-{digest}"
            created.append(
                await self.task(
                    f"{title}: {item}",
                    id=task_id,
                    prompt=task_prompt,
                    worker=worker,
                    route=route,
                    isolated=isolated,
                    policy=policy,
                    requires=requires,
                    quorum=quorum,
                    consumes=consumes,
                    produces=produces,
                    resources=task_resources,
                )
            )
        return tuple(created)

    async def run(self) -> FlowResult:
        task = await self.runtime._start_run(self)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            event = self.runtime._cancel_events.get(self.id)
            if event is not None:
                event.set()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def cancel(self) -> FlowResult:
        active = self.runtime._active_runs.get(self.id)
        if active is not None and not active.done():
            event = self.runtime._cancel_events.get(self.id)
            if event is not None:
                event.set()
            return await asyncio.shield(active)
        lease = await asyncio.to_thread(
            _acquire_flow_lease,
            _flow_lease_path(self.runtime.state_dir, self.id),
        )
        try:
            lock = self.runtime._lock(self.id)
            async with lock:
                record = self.runtime.store.get(self.id)
                if any(task.status == "running" for task in record.tasks):
                    record = await self._recover_interrupted(record)
                if record.status not in _TERMINAL_FLOW_STATUSES:
                    now = _now()
                    tasks = tuple(
                        task
                        if task.status in _TERMINAL_TASK_STATUSES
                        else replace(
                            task,
                            status="cancelled",
                            error="flow cancelled",
                            finished_at=now,
                        )
                        for task in record.tasks
                    )
                    record = self.runtime.store.update(
                        replace(
                            record,
                            status="cancelled",
                            tasks=tasks,
                            updated_at=now,
                            finished_at=now,
                        ),
                        expected_revision=record.revision,
                    )
        finally:
            await asyncio.to_thread(_release_flow_lease, lease)
        return await self._result(record)

    async def status(self) -> FlowRecord:
        return self.runtime.store.get(self.id)

    async def artifacts(
        self,
        *,
        name: str | None = None,
        producer_task_id: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        record = self.runtime.store.get(self.id)
        committed = {
            artifact_id
            for task in record.tasks
            if task.status == "succeeded"
            for artifact_id in task.artifact_ids
        }
        candidates = await self.blackboard.query(
            self.id,
            name=name,
            producer_task_id=producer_task_id,
        )
        return tuple(artifact for artifact in candidates if artifact.id in committed)

    async def _recover_interrupted(self, record: FlowRecord) -> FlowRecord:
        unsafe = False
        fatal = False
        for original in tuple(
            task for task in record.tasks if task.status == "running"
        ):
            latest = self.runtime.store.get(record.id)
            task = _task_by_id(latest, original.spec.id)
            if task.status != "running":
                continue
            if not isinstance(self.runtime.executor, RlmTaskExecutor):
                updated = replace(
                    task,
                    status="pending",
                    error="recovered interrupted attempt",
                    started_at=None,
                    finished_at=None,
                    available_at=0.0,
                )
                record = self._persist_task(latest, updated)
                continue
            context = TaskContext(
                flow_id=latest.id,
                spec=task.spec,
                attempt=task.attempts,
                inputs={},
                artifact_dir=_private_attempt_dir(
                    self.runtime.state_dir,
                    latest.id,
                    task.spec.id,
                    task.attempts,
                ),
                require_token_usage=latest.budget.max_tokens is not None,
                child_lifecycle=self._child_lifecycle_callback(
                    task.spec.id, task.attempts
                ),
            )
            try:
                outcome = await self.runtime.executor.recover_attempt(context, task)
            except Exception as error:  # noqa: BLE001 - executor recovery is a plugin boundary
                outcome = _RecoveredAttempt(
                    error=_ChildUnsettledError(
                        f"could not settle interrupted child: {error}"
                    ),
                    settled=False,
                )
            latest = self.runtime.store.get(record.id)
            task = _task_by_id(latest, task.spec.id)
            if not outcome.settled:
                unsafe = True
                record = self._persist_task(
                    latest,
                    replace(
                        task,
                        status="failed",
                        error=_bounded_error(
                            outcome.error
                            or _ChildUnsettledError(
                                "interrupted child settlement is uncertain"
                            )
                        ),
                        finished_at=_now(),
                        available_at=0.0,
                        child_settled=False,
                    ),
                    total_tokens=latest.total_tokens + outcome.tokens,
                    total_failures=latest.total_failures + 1,
                )
                continue
            if outcome.execution is not None:
                try:
                    artifacts = await self._publish_outputs(context, outcome.execution)
                except Exception as error:  # noqa: BLE001 - artifact backends are plugin boundaries
                    outcome = _RecoveredAttempt(
                        tokens=outcome.tokens,
                        error=_AttemptExecutionError(
                            f"recovered output validation failed: {error}",
                            tokens=outcome.tokens,
                        ),
                    )
                else:
                    latest = self.runtime.store.get(record.id)
                    task = _task_by_id(latest, task.spec.id)
                    record = self._persist_task(
                        latest,
                        replace(
                            task,
                            status="succeeded",
                            artifact_ids=tuple(artifact.id for artifact in artifacts),
                            error=None,
                            finished_at=_now(),
                            available_at=0.0,
                            child_settled=True,
                        ),
                        total_tokens=latest.total_tokens + outcome.tokens,
                    )
                    await self.blackboard.commit_attempt(
                        record.id,
                        task.spec.id,
                        task.attempts,
                        tuple(artifact.id for artifact in artifacts),
                    )
                    continue
            latest = self.runtime.store.get(record.id)
            task = _task_by_id(latest, task.spec.id)
            retryable = not isinstance(outcome.error, _AttemptExecutionError) or (
                outcome.error.retryable
            )
            retry = retryable and task.attempts < task.spec.policy.max_attempts
            fatal = fatal or not retryable
            record = self._persist_task(
                latest,
                replace(
                    task,
                    status="pending" if retry else "failed",
                    error=_bounded_error(
                        outcome.error
                        or TaskExecutionError("recovered interrupted attempt")
                    ),
                    started_at=None if retry else task.started_at,
                    finished_at=None if retry else _now(),
                    available_at=0.0,
                    child_settled=True,
                ),
                total_tokens=latest.total_tokens + outcome.tokens,
                total_failures=latest.total_failures + 1,
            )
        record = self.runtime.store.get(record.id)
        for task in record.tasks:
            if task.status == "succeeded":
                await self.blackboard.commit_attempt(
                    record.id,
                    task.spec.id,
                    task.attempts,
                    task.artifact_ids,
                )
        if unsafe or fatal:
            now = _now()
            tasks = tuple(
                task
                if task.status in _TERMINAL_TASK_STATUSES
                else replace(
                    task,
                    status="failed",
                    error=(
                        "flow recovery failed closed because child settlement "
                        "or usage could not be confirmed"
                    ),
                    finished_at=now,
                    available_at=0.0,
                )
                for task in record.tasks
            )
            return self._persist(record, status="failed", tasks=tasks, finished_at=now)
        if record.status == "running":
            return self._persist(record, status="pending", finished_at=None)
        return record

    def _child_lifecycle_callback(
        self, task_id: str, attempt: int
    ) -> ChildLifecycleCallback:
        async def persist(
            child_id: str,
            child_name: str,
            child_status: str,
            usage_tokens: int | None,
            settled: bool,
        ) -> None:
            while True:
                latest = self.runtime.store.get(self.id)
                task = _task_by_id(latest, task_id)
                if task.attempts != attempt or task.status != "running":
                    raise FlowConflict(
                        "child lifecycle no longer matches the running attempt"
                    )
                updated = replace(
                    task,
                    child_id=child_id,
                    child_name=child_name,
                    child_status=child_status,
                    child_usage_tokens=usage_tokens,
                    child_settled=settled,
                )
                try:
                    self._persist_task(latest, updated)
                    return
                except FlowConflict:
                    continue

        return persist

    async def _drive(self, cancel_event: asyncio.Event) -> FlowResult:
        lock = self.runtime._lock(self.id)
        async with lock:
            record = self.runtime.store.get(self.id)
            if record.status == "running":
                raise FlowConflict(
                    f"flow {self.id!r} is already running in another executor"
                )
            if record.status in _TERMINAL_FLOW_STATUSES:
                return await self._result(record)
            now = _now()
            record = self.runtime.store.update(
                replace(
                    record,
                    status="running",
                    started_at=record.started_at or now,
                    updated_at=now,
                    finished_at=None,
                ),
                expected_revision=record.revision,
            )
            live: dict[str, asyncio.Task[TaskExecution]] = {}
            live_contexts: dict[str, TaskContext] = {}
            try:
                while True:
                    record = self.runtime.store.get(self.id)
                    if cancel_event.is_set():
                        record = await self._settle_cancel(record, live)
                        return await self._result(record)

                    record = await self._apply_completions(record, live, live_contexts)
                    budget_error = _budget_error(record, has_live=bool(live))
                    if budget_error is not None:
                        record = await self._fail_budget(record, live, budget_error)
                        return await self._result(record)
                    if _all_terminal(record.tasks):
                        record = self._finish(record)
                        return await self._result(record)

                    record = self._block_impossible(record)
                    if _all_terminal(record.tasks):
                        record = self._finish(record)
                        return await self._result(record)

                    launched = False
                    busy_resources = {
                        resource
                        for task_id in live
                        for resource in _task_by_id(record, task_id).spec.resources
                    }
                    for task_record in record.tasks:
                        if len(live) >= self.max_parallel:
                            break
                        if (
                            task_record.status != "pending"
                            or task_record.available_at > time.time()
                        ):
                            continue
                        if task_record.attempts >= task_record.spec.policy.max_attempts:
                            record = self._terminal_attempts_exhausted(
                                record, task_record.spec.id
                            )
                            launched = True
                            continue
                        if not _dependency_ready(task_record, record.tasks):
                            continue
                        if busy_resources.intersection(task_record.spec.resources):
                            continue
                        if record.total_attempts >= record.budget.max_attempts:
                            break
                        try:
                            context = await self._context(record, task_record)
                        except (FlowError, OSError, TypeError, ValueError) as error:
                            record = self.runtime.store.get(self.id)
                            task_record = _task_by_id(record, task_record.spec.id)
                            record = self._persist_task(
                                record,
                                replace(
                                    task_record,
                                    status="blocked",
                                    error=_bounded_error(error),
                                    finished_at=_now(),
                                    available_at=0.0,
                                ),
                            )
                            launched = True
                            continue
                        record = self.runtime.store.get(self.id)
                        task_record = _task_by_id(record, task_record.spec.id)
                        if task_record.status != "pending":
                            continue
                        now = _now()
                        rlm_attempt = isinstance(self.runtime.executor, RlmTaskExecutor)
                        updated_task = replace(
                            task_record,
                            status="running",
                            attempts=task_record.attempts + 1,
                            artifact_ids=(),
                            error=None,
                            started_at=now,
                            finished_at=None,
                            available_at=0.0,
                            child_id=None,
                            child_name=_child_name(context) if rlm_attempt else None,
                            child_status=None,
                            child_usage_tokens=None,
                            child_settled=not rlm_attempt,
                        )
                        record = self._persist_task(
                            record,
                            updated_task,
                            total_attempts=record.total_attempts + 1,
                        )
                        context = replace(context, attempt=updated_task.attempts)
                        execution = asyncio.create_task(
                            self._execute(context),
                            name=f"flow-{self.id[:8]}-{task_record.spec.id}",
                        )
                        live[task_record.spec.id] = execution
                        live_contexts[task_record.spec.id] = context
                        busy_resources.update(task_record.spec.resources)
                        launched = True

                    if live:
                        timeout = _next_wait(record)
                        done, _ = await asyncio.wait(
                            tuple(live.values()),
                            timeout=timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done and _wall_expired(record):
                            continue
                        continue

                    pending = [
                        task for task in record.tasks if task.status == "pending"
                    ]
                    if not pending:
                        record = self._finish(record)
                        return await self._result(record)
                    future = [
                        task.available_at
                        for task in pending
                        if task.available_at > time.time()
                    ]
                    if future:
                        delay = max(0.0, min(future) - time.time())
                        wall_left = max(0.0, _wall_deadline(record) - time.time())
                        try:
                            await asyncio.wait_for(
                                cancel_event.wait(), timeout=min(delay, wall_left)
                            )
                        except TimeoutError:
                            pass
                        continue
                    if not launched:
                        now = _now()
                        stalled = tuple(
                            replace(
                                task,
                                status="blocked",
                                error="dependency graph cannot make progress",
                                finished_at=now,
                            )
                            if task.status == "pending"
                            else task
                            for task in record.tasks
                        )
                        record = self._persist(record, tasks=stalled)
            except asyncio.CancelledError:
                await self._settle_cancel(record, live)
                raise
            except Exception:
                for execution in live.values():
                    execution.cancel()
                if live:
                    await asyncio.gather(*live.values(), return_exceptions=True)
                raise

    async def _execute(self, context: TaskContext) -> TaskExecution:
        token = _EXECUTION_REPO.set(self.runtime.store.get(context.flow_id).repo_root)
        execution = asyncio.create_task(self.runtime.executor.execute(context))
        try:
            try:
                return await asyncio.wait_for(
                    execution,
                    timeout=context.spec.policy.timeout_seconds,
                )
            except asyncio.CancelledError:
                execution.cancel()
                (settled,) = await asyncio.gather(execution, return_exceptions=True)
                if isinstance(settled, _ChildUnsettledError):
                    raise settled
                raise
            except TimeoutError as error:
                task = _task_by_id(
                    self.runtime.store.get(context.flow_id), context.spec.id
                )
                tokens = task.child_usage_tokens or 0
                retryable = not (
                    context.require_token_usage
                    and task.child_id is not None
                    and task.child_usage_tokens is None
                )
                raise _AttemptExecutionError(
                    f"task attempt timed out after {context.spec.policy.timeout_seconds:g} seconds",
                    tokens=tokens,
                    retryable=retryable,
                ) from error
        finally:
            _EXECUTION_REPO.reset(token)

    async def _apply_completions(
        self,
        record: FlowRecord,
        live: dict[str, asyncio.Task[TaskExecution]],
        contexts: dict[str, TaskContext],
    ) -> FlowRecord:
        completed_ids = sorted(
            task_id for task_id, execution in live.items() if execution.done()
        )
        for task_id in completed_ids:
            execution_task = live.pop(task_id)
            context = contexts.pop(task_id)
            record = self.runtime.store.get(self.id)
            task_record = _task_by_id(record, task_id)
            measured_tokens = 0
            try:
                execution = execution_task.result()
                measured_tokens = execution.tokens
                artifacts = await self._publish_outputs(context, execution)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - executor plugins are an isolation boundary
                if isinstance(error, _AttemptExecutionError):
                    measured_tokens = error.tokens
                    retryable = error.retryable
                else:
                    retryable = True
                record = self.runtime.store.get(self.id)
                task_record = _task_by_id(record, task_id)
                now = _now()
                failures = record.total_failures + 1
                retry = (
                    retryable
                    and task_record.child_settled is not False
                    and task_record.attempts < task_record.spec.policy.max_attempts
                )
                updated = replace(
                    task_record,
                    status="pending" if retry else "failed",
                    error=_bounded_error(error),
                    started_at=None if retry else task_record.started_at,
                    finished_at=None if retry else now,
                    available_at=(
                        time.time() + task_record.spec.policy.backoff_seconds
                        if retry
                        else 0.0
                    ),
                )
                record = self._persist_task(
                    record,
                    updated,
                    total_failures=failures,
                    total_tokens=record.total_tokens + measured_tokens,
                )
            else:
                record = self.runtime.store.get(self.id)
                task_record = _task_by_id(record, task_id)
                now = _now()
                artifact_ids = tuple(artifact.id for artifact in artifacts)
                updated = replace(
                    task_record,
                    status="succeeded",
                    artifact_ids=artifact_ids,
                    error=None,
                    finished_at=now,
                    available_at=0.0,
                )
                record = self._persist_task(
                    record,
                    updated,
                    total_tokens=record.total_tokens + measured_tokens,
                )
                await self.blackboard.commit_attempt(
                    context.flow_id,
                    context.spec.id,
                    context.attempt,
                    artifact_ids,
                )
        return record

    async def _publish_outputs(
        self,
        context: TaskContext,
        execution: TaskExecution,
    ) -> tuple[ArtifactRecord, ...]:
        outputs = execution.outputs
        expected = {spec.name for spec in context.spec.produces}
        actual = set(outputs)
        if actual != expected:
            raise ArtifactValidationError(
                f"task output names differ from declaration; missing={sorted(expected - actual)}, "
                f"unexpected={sorted(actual - expected)}"
            )
        for spec in context.spec.produces:
            _validate_artifact_value(spec, outputs[spec.name])
        records: list[ArtifactRecord] = []
        for spec in context.spec.produces:
            records.append(
                await self.blackboard.stage(
                    context.flow_id,
                    context.spec.id,
                    context.attempt,
                    spec,
                    outputs[spec.name],
                )
            )
        return tuple(records)

    async def _context(self, record: FlowRecord, task: TaskRecord) -> TaskContext:
        inputs: dict[str, JsonValue] = {}
        by_dependency = {candidate.spec.id: candidate for candidate in record.tasks}
        for name in task.spec.consumes:
            candidates = await self.blackboard.query(record.id, name=name)
            by_id = {artifact.id: artifact for artifact in candidates}
            selected: list[ArtifactRecord] = []
            for dependency_id in task.spec.requires:
                dependency = by_dependency[dependency_id]
                if dependency.status != "succeeded":
                    continue
                selected.extend(
                    by_id[artifact_id]
                    for artifact_id in dependency.artifact_ids
                    if artifact_id in by_id
                )
            if not selected:
                raise ArtifactValidationError(
                    f"task {task.spec.id!r} consumes unavailable artifact {name!r}"
                )
            values: list[JsonValue] = []
            for artifact in selected:
                _, value = await self.blackboard.get(artifact.id)
                values.append(value)
            inputs[name] = values[0] if len(values) == 1 else values
        artifact_dir = _private_attempt_dir(
            self.runtime.state_dir,
            record.id,
            task.spec.id,
            task.attempts + 1,
        )
        attempt = task.attempts + 1
        return TaskContext(
            flow_id=record.id,
            spec=task.spec,
            attempt=attempt,
            inputs=inputs,
            artifact_dir=artifact_dir,
            require_token_usage=record.budget.max_tokens is not None,
            child_lifecycle=(
                self._child_lifecycle_callback(task.spec.id, attempt)
                if isinstance(self.runtime.executor, RlmTaskExecutor)
                else None
            ),
        )

    def _block_impossible(self, record: FlowRecord) -> FlowRecord:
        now = _now()
        changed = False
        tasks: list[TaskRecord] = []
        for task in record.tasks:
            if task.status == "pending" and _dependency_impossible(task, record.tasks):
                task = replace(
                    task,
                    status="blocked",
                    error="dependency quorum is impossible",
                    finished_at=now,
                    available_at=0.0,
                )
                changed = True
            tasks.append(task)
        return self._persist(record, tasks=tuple(tasks)) if changed else record

    def _terminal_attempts_exhausted(
        self, record: FlowRecord, task_id: str
    ) -> FlowRecord:
        task = _task_by_id(record, task_id)
        return self._persist_task(
            record,
            replace(
                task,
                status="failed",
                error="task attempt limit exhausted",
                finished_at=_now(),
                available_at=0.0,
            ),
        )

    async def _fail_budget(
        self,
        record: FlowRecord,
        live: dict[str, asyncio.Task[TaskExecution]],
        error: FlowBudgetExceeded,
    ) -> FlowRecord:
        for execution in live.values():
            execution.cancel()
        if live:
            await asyncio.gather(*live.values(), return_exceptions=True)
        record = self.runtime.store.get(self.id)
        now = _now()
        tasks = tuple(
            task
            if task.status in _TERMINAL_TASK_STATUSES
            else replace(
                task,
                status="failed",
                error=str(error),
                finished_at=now,
                available_at=0.0,
            )
            for task in record.tasks
        )
        cancelled_tokens = sum(
            (_task_by_id(record, task_id).child_usage_tokens or 0) for task_id in live
        )
        return self._persist(
            record,
            status="failed",
            tasks=tasks,
            finished_at=now,
            total_tokens=record.total_tokens + cancelled_tokens,
        )

    async def _settle_cancel(
        self,
        record: FlowRecord,
        live: dict[str, asyncio.Task[TaskExecution]],
    ) -> FlowRecord:
        for execution in live.values():
            execution.cancel()
        results: list[object] = []
        if live:
            results = list(await asyncio.gather(*live.values(), return_exceptions=True))
        record = self.runtime.store.get(self.id)
        unsettled_ids = {
            task_id
            for task_id, result in zip(live, results, strict=False)
            if isinstance(result, _ChildUnsettledError)
        }
        unsettled_ids.update(
            task.spec.id
            for task in record.tasks
            if task.status == "running" and task.child_settled is False
        )
        now = _now()
        tasks = tuple(
            task
            if task.status in _TERMINAL_TASK_STATUSES
            else replace(
                task,
                status="failed" if task.spec.id in unsettled_ids else "cancelled",
                error=(
                    "flow cancellation could not confirm child settlement"
                    if task.spec.id in unsettled_ids
                    else "flow cancelled"
                ),
                finished_at=now,
                available_at=0.0,
            )
            for task in record.tasks
        )
        cancelled_tokens = sum(
            (_task_by_id(record, task_id).child_usage_tokens or 0) for task_id in live
        )
        return self._persist(
            record,
            status="failed" if unsettled_ids else "cancelled",
            tasks=tasks,
            finished_at=now,
            total_tokens=record.total_tokens + cancelled_tokens,
        )

    def _finish(self, record: FlowRecord) -> FlowRecord:
        succeeded = all(task.status == "succeeded" for task in record.tasks)
        now = _now()
        return self._persist(
            record,
            status="succeeded" if succeeded else "failed",
            finished_at=now,
        )

    def _persist_task(
        self, record: FlowRecord, updated: TaskRecord, **changes: object
    ) -> FlowRecord:
        tasks = tuple(
            updated if task.spec.id == updated.spec.id else task
            for task in record.tasks
        )
        return self._persist(record, tasks=tasks, **changes)

    def _persist(self, record: FlowRecord, **changes: object) -> FlowRecord:
        candidate = replace(record, updated_at=_now(), **changes)
        return self.runtime.store.update(candidate, expected_revision=record.revision)

    async def _result(self, record: FlowRecord) -> FlowResult:
        committed = {
            artifact_id
            for task in record.tasks
            if task.status == "succeeded"
            for artifact_id in task.artifact_ids
        }
        artifacts = tuple(
            artifact
            for artifact in await self.blackboard.query(record.id)
            if artifact.id in committed
        )
        return FlowResult(
            flow_id=record.id,
            status=record.status,
            tasks=record.tasks,
            artifacts=artifacts,
            started_at=record.started_at,
            finished_at=record.finished_at,
            total_attempts=record.total_attempts,
            total_failures=record.total_failures,
            total_tokens=record.total_tokens,
        )


def _flow_lease_path(state_dir: Path, flow_id: str) -> Path:
    digest = hashlib.sha256(flow_id.encode("utf-8")).hexdigest()
    return state_dir / "leases" / f"{digest}.lock"


def _acquire_flow_lease(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise FlowConflict(f"flow lease path is a symlink: {path}")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise FlowConflict(f"cannot open flow lease: {path}") from error
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise FlowConflict(f"flow is already leased: {path}") from error
        raise FlowConflict(f"cannot acquire flow lease: {path}") from error
    return descriptor, path


def _release_flow_lease(lease: tuple[int, Path]) -> None:
    descriptor, _ = lease
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _default_state_dir() -> Path:
    override = os.environ.get("OH_MY_PRIME_STATE_HOME")
    if override:
        return (Path(override).expanduser() / "flow").resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = (
        Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    )
    return (root / "oh-my-prime" / "flow").resolve()


def _positive_parallel(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("max_parallel must be a positive integer")
    return value


def _stable_id(title: str) -> str:
    digest = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:12]
    return f"{_slug(title)[:44]}-{digest}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "task"


def _child_name(context: TaskContext) -> str:
    digest = hashlib.sha256(
        f"{context.flow_id}\0{context.spec.id}\0{context.attempt}".encode()
    ).hexdigest()[:10]
    return f"flow-{_slug(context.spec.id)[:28]}-{context.attempt}-{digest}"[:64]


def _nonempty_attr(value: object, name: str) -> str:
    found = getattr(value, name, None)
    if not isinstance(found, str) or not found:
        raise TaskExecutionError(f"RLM admission returned no {name}")
    return found


def _private_attempt_dir(root: Path, flow_id: str, task_id: str, attempt: int) -> Path:
    flow_key = hashlib.sha256(flow_id.encode("utf-8")).hexdigest()
    task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    safe_root = root.resolve()
    base = safe_root / "task-runs"
    target = base / flow_key / task_key / f"attempt-{attempt}"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = target.resolve()
    if resolved != target or safe_root not in resolved.parents:
        raise FlowError("task artifact directory escaped runtime state root")
    try:
        resolved.chmod(0o700)
    except OSError:
        pass
    return resolved


def _task_by_id(record: FlowRecord, task_id: str) -> TaskRecord:
    for task in record.tasks:
        if task.spec.id == task_id:
            return task
    raise FlowNotFound(f"task {task_id!r} is not part of flow {record.id!r}")


def _quorum_required(task: TaskRecord) -> int:
    requires = len(task.spec.requires)
    if requires == 0:
        return 0
    if task.spec.quorum == "all":
        return requires
    if task.spec.quorum == "any":
        return 1
    return int(task.spec.quorum)


def _dependency_counts(
    task: TaskRecord, tasks: Sequence[TaskRecord]
) -> tuple[int, int]:
    by_id = {candidate.spec.id: candidate for candidate in tasks}
    succeeded = 0
    possible = 0
    for dependency_id in task.spec.requires:
        dependency = by_id.get(dependency_id)
        if dependency is None:
            continue
        if dependency.status == "succeeded":
            succeeded += 1
            possible += 1
        elif dependency.status in {"pending", "running"}:
            possible += 1
    return succeeded, possible


def _dependency_ready(task: TaskRecord, tasks: Sequence[TaskRecord]) -> bool:
    succeeded, _ = _dependency_counts(task, tasks)
    return succeeded >= _quorum_required(task)


def _dependency_impossible(task: TaskRecord, tasks: Sequence[TaskRecord]) -> bool:
    _, possible = _dependency_counts(task, tasks)
    return possible < _quorum_required(task)


def _all_terminal(tasks: Sequence[TaskRecord]) -> bool:
    return all(task.status in _TERMINAL_TASK_STATUSES for task in tasks)


def _wall_expired(record: FlowRecord) -> bool:
    return record.started_at is not None and time.time() >= _wall_deadline(record)


def _wall_deadline(record: FlowRecord) -> float:
    if record.started_at is None:
        return time.time() + record.budget.wall_time_seconds
    return (
        datetime.fromisoformat(record.started_at).timestamp()
        + record.budget.wall_time_seconds
    )


def _budget_error(record: FlowRecord, *, has_live: bool) -> FlowBudgetExceeded | None:
    incomplete = any(
        task.status not in _TERMINAL_TASK_STATUSES for task in record.tasks
    )
    if _wall_expired(record):
        return FlowBudgetExceeded("flow wall-time budget exhausted")
    if (
        incomplete
        and not has_live
        and record.total_attempts >= record.budget.max_attempts
    ):
        return FlowBudgetExceeded("flow attempt budget exhausted")
    if incomplete and record.total_failures >= record.budget.max_failures:
        return FlowBudgetExceeded("flow failure budget exhausted")
    if record.budget.max_tokens is not None and (
        record.total_tokens > record.budget.max_tokens
        or (incomplete and record.total_tokens >= record.budget.max_tokens)
    ):
        return FlowBudgetExceeded("flow token budget exhausted")
    return None


def _next_wait(record: FlowRecord) -> float:
    if record.started_at is None:
        return 0.25
    return max(0.0, min(0.25, _wall_deadline(record) - time.time()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_error(error: BaseException) -> str:
    message = f"{type(error).__name__}: {error}"
    return message if len(message) <= 4_096 else message[:4_093] + "..."


def _validate_artifact_value(spec: ArtifactSpec, value: JsonValue) -> None:
    expected_types: dict[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    expected = expected_types.get(spec.value_type)
    if expected is None:
        raise ArtifactValidationError(
            f"unsupported artifact value type {spec.value_type!r}"
        )
    if spec.value_type in {"number", "integer"} and isinstance(value, bool):
        valid = False
    else:
        valid = isinstance(value, expected)
    if not valid:
        raise ArtifactValidationError(
            f"artifact {spec.name!r} must have JSON type {spec.value_type!r}"
        )
    if spec.required_keys:
        if not isinstance(value, dict):
            raise ArtifactValidationError(
                f"artifact {spec.name!r} declares required keys but is not an object"
            )
        missing = [key for key in spec.required_keys if key not in value]
        if missing:
            raise ArtifactValidationError(
                f"artifact {spec.name!r} is missing required keys {missing}"
            )
    try:
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ArtifactValidationError(
            f"artifact {spec.name!r} is not canonical JSON data"
        ) from error
