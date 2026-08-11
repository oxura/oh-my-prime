"""Durable typed task graphs for recursive Oh My Prime workflows."""

from __future__ import annotations

import builtins
import os
from collections.abc import Sequence

from ._blackboard import ArtifactBlackboard
from ._models import (
    ArtifactRecord,
    ArtifactSpec,
    ArtifactValidationError,
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
from ._runtime import Flow, FlowRuntime, RlmTaskExecutor, TaskExecutor
from ._store import FlowStore

_default_runtime = FlowRuntime()


async def create(
    goal: str,
    *,
    repo: str | os.PathLike[str] = ".",
    budget: FlowBudget | None = None,
    max_parallel: int | None = None,
) -> Flow:
    """Create and durably persist an empty task graph."""
    return await _default_runtime.create(
        goal,
        repo=repo,
        budget=budget,
        max_parallel=max_parallel,
    )


async def load(
    flow_id: str,
    *,
    repo: str | os.PathLike[str] = ".",
) -> Flow:
    """Recover a task graph without rerunning completed tasks."""
    return await _default_runtime.load(flow_id, repo=repo)


async def list(
    *,
    repo: str | os.PathLike[str] | None = None,
    statuses: Sequence[str] = (),
    limit: int = 200,
) -> builtins.list[FlowRecord]:
    """List durable flow records, optionally filtered by repository and status."""
    return await _default_runtime.list(repo=repo, statuses=statuses, limit=limit)


__all__ = [
    "ArtifactBlackboard",
    "ArtifactRecord",
    "ArtifactSpec",
    "ArtifactValidationError",
    "Flow",
    "FlowBudget",
    "FlowBudgetExceeded",
    "FlowConflict",
    "FlowError",
    "FlowNotFound",
    "FlowRecord",
    "FlowResult",
    "FlowRuntime",
    "FlowStore",
    "JsonValue",
    "RlmTaskExecutor",
    "TaskContext",
    "TaskExecution",
    "TaskExecutionError",
    "TaskExecutor",
    "TaskPolicy",
    "TaskRecord",
    "TaskSpec",
    "create",
    "list",
    "load",
]
