from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)
TaskStatus: TypeAlias = Literal[
    "pending", "running", "succeeded", "failed", "blocked", "cancelled"
]
FlowStatus: TypeAlias = Literal[
    "pending", "running", "succeeded", "failed", "cancelled"
]
ArtifactValueType: TypeAlias = Literal[
    "object", "array", "string", "number", "integer", "boolean", "null"
]
Quorum: TypeAlias = Literal["all", "any"] | int
ChildLifecycleCallback: TypeAlias = Callable[
    [str, str, str, int | None, bool], Awaitable[None]
]

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 100
_TASK_STATUSES = frozenset(
    ("pending", "running", "succeeded", "failed", "blocked", "cancelled")
)
_FLOW_STATUSES = frozenset(("pending", "running", "succeeded", "failed", "cancelled"))
_VALUE_TYPES = frozenset(
    ("object", "array", "string", "number", "integer", "boolean", "null")
)


class FlowError(RuntimeError):
    """Base error for durable flow operations."""


class FlowConflict(FlowError):
    """Raised when an optimistic flow update loses a revision race."""


class FlowNotFound(FlowError):
    """Raised when a requested flow or artifact does not exist."""


class FlowBudgetExceeded(FlowError):
    """Raised when execution cannot continue within its flow budget."""


class ArtifactValidationError(FlowError, ValueError):
    """Raised when an artifact is malformed or fails integrity validation."""


class TaskExecutionError(FlowError):
    """Raised when a task executor cannot produce a valid result."""


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_nonempty(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, field_name)


def _plain_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return cast(bool, value)


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return cast(int, value)


def _finite_number(
    value: object, field_name: str, *, positive: bool = False, nonnegative: bool = False
) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if positive and result <= 0:
        raise ValueError(f"{field_name} must be > 0")
    if nonnegative and result < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return result


def _string_tuple(
    value: object, field_name: str, *, unique: bool = True
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for item in value:
        _nonempty(item, f"{field_name} item")
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{field_name} must contain unique values")
    return cast(tuple[str, ...], value)


def _path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a pathlib.Path")
    return value


def validate_json_value(value: object, *, max_depth: int = MAX_JSON_DEPTH) -> JsonValue:
    """Validate the strict JSON data model without numeric or key coercion."""
    if type(max_depth) is not int or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if current is None or type(current) in (bool, int, str):
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ArtifactValidationError("JSON numbers must be finite")
            continue
        if type(current) is list:
            if depth >= max_depth and current:
                raise ArtifactValidationError(f"JSON nesting exceeds {max_depth}")
            stack.extend((item, depth + 1) for item in current)
            continue
        if type(current) is dict:
            if depth >= max_depth and current:
                raise ArtifactValidationError(f"JSON nesting exceeds {max_depth}")
            for key, item in current.items():
                if type(key) is not str:
                    raise ArtifactValidationError("JSON object keys must be strings")
                stack.append((item, depth + 1))
            continue
        raise ArtifactValidationError(
            f"unsupported JSON value type: {type(current).__name__}"
        )
    return cast(JsonValue, value)


def canonical_json_bytes(value: object, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    """Return deterministic UTF-8 JSON after strict type and size validation."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ArtifactValidationError(
            f"value cannot be encoded as canonical JSON: {exc}"
        ) from exc
    if len(encoded) > max_bytes:
        raise ArtifactValidationError(
            f"canonical JSON is {len(encoded)} bytes, exceeding the {max_bytes}-byte limit"
        )
    return encoded


def decode_canonical_json(data: bytes, *, max_bytes: int = MAX_JSON_BYTES) -> JsonValue:
    """Decode strict canonical JSON, rejecting duplicate keys and alternate encodings."""
    if not isinstance(data, bytes):
        raise ArtifactValidationError("JSON input must be bytes")
    if len(data) > max_bytes:
        raise ArtifactValidationError(f"JSON input exceeds the {max_bytes}-byte limit")

    def pairs_hook(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactValidationError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        raise ArtifactValidationError(f"invalid JSON number: {token}")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except ArtifactValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ArtifactValidationError(f"invalid JSON: {exc}") from exc
    validated = validate_json_value(value)
    if canonical_json_bytes(validated, max_bytes=max_bytes) != data:
        raise ArtifactValidationError("JSON is not in canonical form")
    return validated


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    for key in value:
        _nonempty(key, f"{field_name} key")
    return cast(Mapping[str, object], value)


def _json_mapping(value: object, field_name: str) -> Mapping[str, JsonValue]:
    mapping = _mapping(value, field_name)
    canonical_json_bytes(dict(mapping))
    return cast(Mapping[str, JsonValue], mapping)


def _require_keys(
    data: Mapping[str, object], expected: frozenset[str], type_name: str
) -> None:
    actual = frozenset(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"invalid {type_name} fields; missing={missing}, extra={extra}"
        )


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    name: str
    value_type: ArtifactValueType = "object"
    required_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.name, "artifact name")
        if not isinstance(self.value_type, str) or self.value_type not in _VALUE_TYPES:
            raise ValueError(f"unsupported artifact value_type: {self.value_type!r}")
        _string_tuple(self.required_keys, "required_keys")
        if self.required_keys and self.value_type != "object":
            raise ValueError("required_keys are only valid for object artifacts")

    def validate(self, value: object) -> JsonValue:
        validated = validate_json_value(value)
        matches = {
            "object": type(value) is dict,
            "array": type(value) is list,
            "string": type(value) is str,
            "number": type(value) in (int, float),
            "integer": type(value) is int,
            "boolean": type(value) is bool,
            "null": value is None,
        }[self.value_type]
        if not matches:
            raise ArtifactValidationError(
                f"artifact {self.name!r} must have JSON type {self.value_type}"
            )
        if type(value) is dict:
            missing = [key for key in self.required_keys if key not in value]
            if missing:
                raise ArtifactValidationError(
                    f"artifact {self.name!r} is missing required keys: {missing}"
                )
        return validated

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "value_type": self.value_type,
            "required_keys": list(self.required_keys),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ArtifactSpec:
        _require_keys(
            data, frozenset(("name", "value_type", "required_keys")), "ArtifactSpec"
        )
        required = data["required_keys"]
        if not isinstance(required, list):
            raise TypeError("ArtifactSpec.required_keys must be a list")
        return cls(
            name=cast(str, data["name"]),
            value_type=cast(ArtifactValueType, data["value_type"]),
            required_keys=tuple(cast(list[str], required)),
        )


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    max_attempts: int = 1
    timeout_seconds: float = 900.0
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        _integer(self.max_attempts, "max_attempts", minimum=1)
        _finite_number(self.timeout_seconds, "timeout_seconds", positive=True)
        _finite_number(self.backoff_seconds, "backoff_seconds", nonnegative=True)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "backoff_seconds": self.backoff_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TaskPolicy:
        _require_keys(
            data,
            frozenset(("max_attempts", "timeout_seconds", "backoff_seconds")),
            "TaskPolicy",
        )
        return cls(
            cast(int, data["max_attempts"]),
            cast(float, data["timeout_seconds"]),
            cast(float, data["backoff_seconds"]),
        )


@dataclass(frozen=True, slots=True)
class FlowBudget:
    max_attempts: int = 100
    max_failures: int = 10
    max_tokens: int | None = None
    wall_time_seconds: float = 3600.0

    def __post_init__(self) -> None:
        _integer(self.max_attempts, "max_attempts", minimum=1)
        _integer(self.max_failures, "max_failures", minimum=1)
        if self.max_tokens is not None:
            _integer(self.max_tokens, "max_tokens", minimum=1)
        _finite_number(self.wall_time_seconds, "wall_time_seconds", positive=True)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "max_attempts": self.max_attempts,
            "max_failures": self.max_failures,
            "max_tokens": self.max_tokens,
            "wall_time_seconds": self.wall_time_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FlowBudget:
        _require_keys(
            data,
            frozenset(
                ("max_attempts", "max_failures", "max_tokens", "wall_time_seconds")
            ),
            "FlowBudget",
        )
        return cls(
            max_attempts=cast(int, data["max_attempts"]),
            max_failures=cast(int, data["max_failures"]),
            max_tokens=cast(int | None, data["max_tokens"]),
            wall_time_seconds=cast(float, data["wall_time_seconds"]),
        )


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    title: str
    prompt: str
    requires: tuple[str, ...] = ()
    quorum: Quorum = "all"
    worker: str = "agent"
    route: str | None = None
    isolated: bool = False
    policy: TaskPolicy = TaskPolicy()
    consumes: tuple[str, ...] = ()
    produces: tuple[ArtifactSpec, ...] = ()
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.id, "task id")
        _nonempty(self.title, "task title")
        _nonempty(self.prompt, "task prompt")
        dependencies = _string_tuple(self.requires, "requires")
        if self.id in dependencies:
            raise ValueError("a task cannot depend on itself")
        if self.quorum not in ("all", "any") and (
            type(self.quorum) is not int or self.quorum <= 0
        ):
            raise ValueError("quorum must be 'all', 'any', or a positive integer")
        _nonempty(self.worker, "worker")
        _optional_nonempty(self.route, "route")
        _plain_bool(self.isolated, "isolated")
        if not isinstance(self.policy, TaskPolicy):
            raise TypeError("policy must be a TaskPolicy")
        _string_tuple(self.consumes, "consumes")
        if not isinstance(self.produces, tuple) or any(
            not isinstance(spec, ArtifactSpec) for spec in self.produces
        ):
            raise TypeError("produces must be a tuple of ArtifactSpec values")
        produced_names = [spec.name for spec in self.produces]
        if len(produced_names) != len(set(produced_names)):
            raise ValueError("produces artifact names must be unique")
        _string_tuple(self.resources, "resources")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "requires": list(self.requires),
            "quorum": self.quorum,
            "worker": self.worker,
            "route": self.route,
            "isolated": self.isolated,
            "policy": self.policy.to_dict(),
            "consumes": list(self.consumes),
            "produces": [spec.to_dict() for spec in self.produces],
            "resources": list(self.resources),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TaskSpec:
        expected = frozenset(
            (
                "id",
                "title",
                "prompt",
                "requires",
                "quorum",
                "worker",
                "route",
                "isolated",
                "policy",
                "consumes",
                "produces",
                "resources",
            )
        )
        _require_keys(data, expected, "TaskSpec")
        requires, consumes, produces, resources = (
            data["requires"],
            data["consumes"],
            data["produces"],
            data["resources"],
        )
        if not all(
            isinstance(value, list)
            for value in (requires, consumes, produces, resources)
        ):
            raise ValueError("TaskSpec tuple fields must be encoded as lists")
        policy = _mapping(data["policy"], "policy")
        return cls(
            id=cast(str, data["id"]),
            title=cast(str, data["title"]),
            prompt=cast(str, data["prompt"]),
            requires=tuple(cast(list[str], requires)),
            quorum=cast(Quorum, data["quorum"]),
            worker=cast(str, data["worker"]),
            route=cast(str | None, data["route"]),
            isolated=cast(bool, data["isolated"]),
            policy=TaskPolicy.from_dict(policy),
            consumes=tuple(cast(list[str], consumes)),
            produces=tuple(
                ArtifactSpec.from_dict(_mapping(item, "produce spec"))
                for item in cast(list[object], produces)
            ),
            resources=tuple(cast(list[str], resources)),
        )


@dataclass(frozen=True, slots=True)
class TaskExecution:
    outputs: Mapping[str, JsonValue]
    tokens: int = 0
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        outputs = _json_mapping(self.outputs, "outputs")
        metadata = _json_mapping(self.metadata, "metadata")
        _integer(self.tokens, "tokens")
        canonical_json_bytes(
            {
                "outputs": dict(outputs),
                "tokens": self.tokens,
                "metadata": dict(metadata),
            }
        )
        object.__setattr__(self, "outputs", MappingProxyType(dict(outputs)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(metadata)))


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    schema_version: int
    id: str
    flow_id: str
    producer_task_id: str
    name: str
    value_type: ArtifactValueType
    path: Path
    sha256: str
    created_at: str

    def __post_init__(self) -> None:
        _integer(self.schema_version, "schema_version", minimum=1)
        _nonempty(self.id, "artifact id")
        _nonempty(self.flow_id, "flow id")
        _nonempty(self.producer_task_id, "producer task id")
        _nonempty(self.name, "artifact name")
        if not isinstance(self.value_type, str) or self.value_type not in _VALUE_TYPES:
            raise ValueError(f"unsupported artifact value_type: {self.value_type!r}")
        _path(self.path, "artifact path")
        if self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError("artifact path must be a safe relative path")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        _nonempty(self.created_at, "created_at")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "flow_id": self.flow_id,
            "producer_task_id": self.producer_task_id,
            "name": self.name,
            "value_type": self.value_type,
            "path": str(self.path),
            "sha256": self.sha256,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ArtifactRecord:
        expected = frozenset(
            (
                "schema_version",
                "id",
                "flow_id",
                "producer_task_id",
                "name",
                "value_type",
                "path",
                "sha256",
                "created_at",
            )
        )
        _require_keys(data, expected, "ArtifactRecord")
        path = data["path"]
        if not isinstance(path, str):
            raise TypeError("ArtifactRecord.path must be a string")
        return cls(
            cast(int, data["schema_version"]),
            cast(str, data["id"]),
            cast(str, data["flow_id"]),
            cast(str, data["producer_task_id"]),
            cast(str, data["name"]),
            cast(ArtifactValueType, data["value_type"]),
            Path(path),
            cast(str, data["sha256"]),
            cast(str, data["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class TaskRecord:
    spec: TaskSpec
    status: TaskStatus = "pending"
    attempts: int = 0
    artifact_ids: tuple[str, ...] = ()
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    available_at: float = 0.0
    child_id: str | None = None
    child_name: str | None = None
    child_status: str | None = None
    child_usage_tokens: int | None = None
    child_settled: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, TaskSpec):
            raise TypeError("spec must be a TaskSpec")
        if not isinstance(self.status, str) or self.status not in _TASK_STATUSES:
            raise ValueError(f"invalid task status: {self.status!r}")
        _integer(self.attempts, "attempts")
        _string_tuple(self.artifact_ids, "artifact_ids")
        _optional_nonempty(self.error, "error")
        _optional_nonempty(self.started_at, "started_at")
        _optional_nonempty(self.finished_at, "finished_at")
        _finite_number(self.available_at, "available_at", nonnegative=True)
        _optional_nonempty(self.child_id, "child_id")
        _optional_nonempty(self.child_name, "child_name")
        if self.child_status not in (None, "running", "completed", "error", "deleted"):
            raise ValueError(f"invalid child_status: {self.child_status!r}")
        if self.child_usage_tokens is not None:
            _integer(self.child_usage_tokens, "child_usage_tokens")
        if self.child_settled is not None:
            _plain_bool(self.child_settled, "child_settled")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "spec": self.spec.to_dict(),
            "status": self.status,
            "attempts": self.attempts,
            "artifact_ids": list(self.artifact_ids),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "available_at": self.available_at,
            "child_id": self.child_id,
            "child_name": self.child_name,
            "child_status": self.child_status,
            "child_usage_tokens": self.child_usage_tokens,
            "child_settled": self.child_settled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TaskRecord:
        legacy = frozenset(
            (
                "spec",
                "status",
                "attempts",
                "artifact_ids",
                "error",
                "started_at",
                "finished_at",
                "available_at",
            )
        )
        lifecycle = frozenset(
            (
                "child_id",
                "child_name",
                "child_status",
                "child_usage_tokens",
                "child_settled",
            )
        )
        actual = frozenset(data)
        if actual not in (legacy, legacy | lifecycle):
            _require_keys(data, legacy | lifecycle, "TaskRecord")
        artifacts = data["artifact_ids"]
        if not isinstance(artifacts, list):
            raise TypeError("TaskRecord.artifact_ids must be a list")
        return cls(
            spec=TaskSpec.from_dict(_mapping(data["spec"], "spec")),
            status=cast(TaskStatus, data["status"]),
            attempts=cast(int, data["attempts"]),
            artifact_ids=tuple(cast(list[str], artifacts)),
            error=cast(str | None, data["error"]),
            started_at=cast(str | None, data["started_at"]),
            finished_at=cast(str | None, data["finished_at"]),
            available_at=cast(float, data["available_at"]),
            child_id=cast(str | None, data.get("child_id")),
            child_name=cast(str | None, data.get("child_name")),
            child_status=cast(str | None, data.get("child_status")),
            child_usage_tokens=cast(int | None, data.get("child_usage_tokens")),
            child_settled=cast(bool | None, data.get("child_settled")),
        )


@dataclass(frozen=True, slots=True)
class FlowRecord:
    schema_version: int
    id: str
    revision: int
    goal: str
    repo_root: Path
    status: FlowStatus
    budget: FlowBudget
    tasks: tuple[TaskRecord, ...]
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    total_attempts: int = 0
    total_failures: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        _integer(self.schema_version, "schema_version", minimum=1)
        _nonempty(self.id, "flow id")
        _integer(self.revision, "revision")
        _nonempty(self.goal, "goal")
        _path(self.repo_root, "repo_root")
        if not isinstance(self.status, str) or self.status not in _FLOW_STATUSES:
            raise ValueError(f"invalid flow status: {self.status!r}")
        if not isinstance(self.budget, FlowBudget):
            raise TypeError("budget must be a FlowBudget")
        if not isinstance(self.tasks, tuple) or any(
            not isinstance(task, TaskRecord) for task in self.tasks
        ):
            raise TypeError("tasks must be a tuple of TaskRecord values")
        ids = [task.spec.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for task in self.tasks:
            unknown = set(task.spec.requires) - known
            if unknown:
                raise ValueError(
                    f"task {task.spec.id!r} has unknown dependencies: {sorted(unknown)}"
                )
        self._validate_acyclic()
        _nonempty(self.created_at, "created_at")
        _nonempty(self.updated_at, "updated_at")
        _optional_nonempty(self.started_at, "started_at")
        _optional_nonempty(self.finished_at, "finished_at")
        _integer(self.total_attempts, "total_attempts")
        _integer(self.total_failures, "total_failures")
        _integer(self.total_tokens, "total_tokens")

    def _validate_acyclic(self) -> None:
        graph = {task.spec.id: task.spec.requires for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependencies must form an acyclic graph")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in graph:
            visit(task_id)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "revision": self.revision,
            "goal": self.goal,
            "repo_root": str(self.repo_root),
            "status": self.status,
            "budget": self.budget.to_dict(),
            "tasks": [task.to_dict() for task in self.tasks],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_attempts": self.total_attempts,
            "total_failures": self.total_failures,
            "total_tokens": self.total_tokens,
        }
        canonical_json_bytes(result)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FlowRecord:
        expected = frozenset(
            (
                "schema_version",
                "id",
                "revision",
                "goal",
                "repo_root",
                "status",
                "budget",
                "tasks",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "total_attempts",
                "total_failures",
                "total_tokens",
            )
        )
        _require_keys(data, expected, "FlowRecord")
        repo_root, tasks = data["repo_root"], data["tasks"]
        if not isinstance(repo_root, str) or not isinstance(tasks, list):
            raise TypeError(
                "FlowRecord.repo_root must be a string and tasks must be a list"
            )
        return cls(
            schema_version=cast(int, data["schema_version"]),
            id=cast(str, data["id"]),
            revision=cast(int, data["revision"]),
            goal=cast(str, data["goal"]),
            repo_root=Path(repo_root),
            status=cast(FlowStatus, data["status"]),
            budget=FlowBudget.from_dict(_mapping(data["budget"], "budget")),
            tasks=tuple(TaskRecord.from_dict(_mapping(item, "task")) for item in tasks),
            created_at=cast(str, data["created_at"]),
            updated_at=cast(str, data["updated_at"]),
            started_at=cast(str | None, data["started_at"]),
            finished_at=cast(str | None, data["finished_at"]),
            total_attempts=cast(int, data["total_attempts"]),
            total_failures=cast(int, data["total_failures"]),
            total_tokens=cast(int, data["total_tokens"]),
        )


@dataclass(frozen=True, slots=True)
class FlowResult:
    flow_id: str
    status: FlowStatus
    tasks: tuple[TaskRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]
    started_at: str | None
    finished_at: str | None
    total_attempts: int
    total_failures: int
    total_tokens: int

    def __post_init__(self) -> None:
        _nonempty(self.flow_id, "flow_id")
        if not isinstance(self.status, str) or self.status not in _FLOW_STATUSES:
            raise ValueError(f"invalid flow status: {self.status!r}")
        if not isinstance(self.tasks, tuple) or any(
            not isinstance(task, TaskRecord) for task in self.tasks
        ):
            raise ValueError("tasks must be a tuple of TaskRecord values")
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(item, ArtifactRecord) for item in self.artifacts
        ):
            raise ValueError("artifacts must be a tuple of ArtifactRecord values")
        _optional_nonempty(self.started_at, "started_at")
        _optional_nonempty(self.finished_at, "finished_at")
        _integer(self.total_attempts, "total_attempts")
        _integer(self.total_failures, "total_failures")
        _integer(self.total_tokens, "total_tokens")


@dataclass(frozen=True, slots=True)
class TaskContext:
    flow_id: str
    spec: TaskSpec
    attempt: int
    inputs: Mapping[str, JsonValue]
    artifact_dir: Path
    require_token_usage: bool = False
    child_lifecycle: ChildLifecycleCallback | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _nonempty(self.flow_id, "flow_id")
        if not isinstance(self.spec, TaskSpec):
            raise TypeError("spec must be a TaskSpec")
        _integer(self.attempt, "attempt", minimum=1)
        inputs = _json_mapping(self.inputs, "inputs")
        _path(self.artifact_dir, "artifact_dir")
        _plain_bool(self.require_token_usage, "require_token_usage")
        if self.child_lifecycle is not None and not callable(self.child_lifecycle):
            raise TypeError("child_lifecycle must be callable or None")
        object.__setattr__(self, "inputs", MappingProxyType(dict(inputs)))
