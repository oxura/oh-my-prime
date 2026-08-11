"""Tiny rlm-compatible kernel shim for Prime Agent."""

from __future__ import annotations

import asyncio
import re
import sys
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness import (
    HarnessEntry,
    HarnessScope,
    HarnessState,
    RefinementEvent,
    get_harness_state,
)

try:
    from ipykernel.comm import Comm
except Exception:  # pragma: no cover - depends on ipykernel version
    Comm = None  # type: ignore[assignment]

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover - only available in kernels
    get_ipython = None  # type: ignore[assignment]

HOST_COMM_TARGET = "host.request"


@dataclass(frozen=True)
class RLMFilesystemCapabilities:
    read: tuple[Path, ...]
    write: tuple[Path, ...]


@dataclass(frozen=True)
class RLMNetworkCapabilities:
    allow: tuple[str, ...]
    deny_by_default: bool


@dataclass(frozen=True)
class RLMSecretCapabilities:
    allow: tuple[str, ...]


@dataclass(frozen=True)
class RLMProcessCapabilities:
    cpu: int | None = None
    memory_bytes: int | None = None
    wall_time_ms: int | None = None
    max_processes: int | None = None


@dataclass(frozen=True)
class RLMCapabilityManifest:
    filesystem: RLMFilesystemCapabilities
    network: RLMNetworkCapabilities
    secrets: RLMSecretCapabilities
    process: RLMProcessCapabilities


@dataclass(frozen=True)
class RLMSpawnHandle:
    rlm_child_id: str
    name: str
    session_dir: Path
    model: str
    cwd: Path
    effort: str
    capabilities: RLMCapabilityManifest


@dataclass(frozen=True)
class RLMModelCost:
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class RLMModel:
    provider: str
    id: str
    name: str
    selector: str
    reasoning: bool
    input: tuple[str, ...]
    context_window: int
    max_tokens: int
    cost: RLMModelCost


@dataclass(frozen=True)
class RLMRouteResolution:
    model: str
    effort: str | None = None


ModelRouteResolver = Callable[
    [str, str, dict[str, Any]],
    Awaitable[RLMRouteResolution],
]
_model_route_resolver: ModelRouteResolver | None = None


def register_model_route_resolver(resolver: ModelRouteResolver | None) -> None:
    """Install the kernel-local resolver used by ``rlm(..., route=...)``."""
    global _model_route_resolver
    if resolver is not None and not callable(resolver):
        raise TypeError("resolver must be callable or None")
    _model_route_resolver = resolver


@dataclass(frozen=True)
class RLMSubagent:
    rlm_child_id: str
    active_session_id: str | None
    session_id: str | None
    session_name: str
    session_dir: Path
    status: str
    usage_tokens: int | None = None


def _install_control_comm_handlers() -> None:
    """Let comm replies arrive on the control channel during an execute_request."""
    if get_ipython is None:
        return
    shell = get_ipython()
    kernel = getattr(shell, "kernel", None)
    comm_manager = getattr(kernel, "comm_manager", None)
    control_handlers = getattr(kernel, "control_handlers", None)
    if comm_manager is None or not isinstance(control_handlers, dict):
        return
    control_handlers.setdefault("comm_msg", comm_manager.comm_msg)
    control_handlers.setdefault("comm_close", comm_manager.comm_close)


def _capability_strings(value: Any, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise RuntimeError(f"rlm.run returned invalid {label}")
    return tuple(value)


def _capability_record(
    value: Any,
    *,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"rlm.run returned invalid {label}")
    allowed = required | (optional or set())
    if set(value) - allowed or not required.issubset(value):
        raise RuntimeError(f"rlm.run returned invalid {label}")
    return value


def _capabilities_from_payload(value: Any) -> RLMCapabilityManifest:
    payload = _capability_record(
        value,
        label="capability manifest",
        required={"filesystem", "network", "secrets", "process"},
    )
    filesystem = _capability_record(
        payload["filesystem"],
        label="filesystem capabilities",
        required={"read", "write"},
    )
    network = _capability_record(
        payload["network"],
        label="network capabilities",
        required={"allow", "deny_by_default"},
    )
    secrets = _capability_record(
        payload["secrets"],
        label="secret capabilities",
        required={"allow"},
    )
    process = _capability_record(
        payload["process"],
        label="process capabilities",
        required=set(),
        optional={"cpu", "memory_bytes", "wall_time_ms", "max_processes"},
    )
    reads = _capability_strings(filesystem["read"], "filesystem read capabilities")
    writes = _capability_strings(filesystem["write"], "filesystem write capabilities")
    domains = _capability_strings(network["allow"], "network capabilities")
    secret_names = _capability_strings(secrets["allow"], "secret capabilities")
    if network["deny_by_default"] is not True:
        raise RuntimeError("rlm.run returned invalid network capabilities")
    if any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in secret_names
    ):
        raise RuntimeError("rlm.run returned invalid secret capabilities")
    limits: dict[str, int | None] = {}
    for key in ("cpu", "memory_bytes", "wall_time_ms", "max_processes"):
        limit = process.get(key)
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise RuntimeError("rlm.run returned invalid process capabilities")
        limits[key] = limit
    return RLMCapabilityManifest(
        filesystem=RLMFilesystemCapabilities(
            read=tuple(Path(path) for path in reads),
            write=tuple(Path(path) for path in writes),
        ),
        network=RLMNetworkCapabilities(
            allow=domains,
            deny_by_default=True,
        ),
        secrets=RLMSecretCapabilities(allow=secret_names),
        process=RLMProcessCapabilities(**limits),
    )


def _spawn_handle_from_payload(payload: Any) -> RLMSpawnHandle:
    if not isinstance(payload, dict):
        raise RuntimeError("rlm.run returned an invalid spawn handle")
    child_id = payload.get("rlm_child_id")
    name = payload.get("name")
    session_dir = payload.get("session_dir")
    model = payload.get("model")
    cwd = payload.get("cwd")
    effort = payload.get("effort")
    capabilities = payload.get("capabilities")
    if not all(
        isinstance(value, str) and value
        for value in (child_id, name, session_dir, model, cwd, effort)
    ) or effort not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise RuntimeError("rlm.run returned an invalid spawn handle")
    return RLMSpawnHandle(
        rlm_child_id=child_id,
        name=name,
        session_dir=Path(session_dir),
        model=model,
        cwd=Path(cwd),
        effort=effort,
        capabilities=_capabilities_from_payload(capabilities),
    )


async def host_request(
    request_type: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send a typed request to the Prime Agent host and await its reply.

    This is the kernel side of the generic host bridge: Python skills call
    ``await host_request("<type>", {...})`` and the TypeScript host dispatches
    on the type. Raises RuntimeError when the host reports an error or when no
    handler for the type is registered in this session.
    """
    if not isinstance(request_type, str) or not request_type:
        raise TypeError("request_type must be a non-empty str")
    if payload is not None and not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict or None, got {type(payload).__name__}")
    if Comm is None:
        raise RuntimeError("Jupyter comm support is unavailable in this kernel")
    _install_control_comm_handlers()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    comm = Comm(target_name=HOST_COMM_TARGET, primary=False)

    def _on_msg(msg: dict[str, Any]) -> None:
        content = msg.get("content", {})
        reply = content.get("data", {}) if isinstance(content, dict) else {}
        if not isinstance(reply, dict):
            return

        status = reply.get("status")
        if status == "ok":

            def _resolve_result() -> None:
                if not future.done():
                    future.set_result({k: v for k, v in reply.items() if k != "status"})
                    comm.close()

            loop.call_soon_threadsafe(_resolve_result)
            return
        if status == "error":
            message = reply.get("error") or f"host request {request_type} failed"

            def _resolve_error() -> None:
                if not future.done():
                    future.set_exception(RuntimeError(str(message)))
                    comm.close()

            loop.call_soon_threadsafe(_resolve_error)
            return

        unexpected = (
            f"host request {request_type} returned unexpected status: {status!r}"
        )

        def _resolve_unexpected() -> None:
            if not future.done():
                future.set_exception(RuntimeError(unexpected))
                comm.close()

        loop.call_soon_threadsafe(_resolve_unexpected)

    comm.on_msg(_on_msg)
    # request_type goes last so a payload "type" key cannot reroute the request.
    comm.open(data={**(payload or {}), "type": request_type})
    return await future


async def run(prompt: str, **kwargs: Any) -> RLMSpawnHandle:
    """Spawn a recursive Prime Agent child and return once its task is admitted.

    ``model`` selects an exact ``provider/model`` selector. ``route`` asks the
    installed Model Mesh resolver for a selector. ``cwd`` selects an existing
    working directory and ``capabilities`` bounds the child runtime.
    """
    if not isinstance(prompt, str):
        raise TypeError(f"prompt must be str, got {type(prompt).__name__}")
    spawn_kwargs = dict(kwargs)
    route = spawn_kwargs.pop("route", None)
    if route is not None:
        if not isinstance(route, str) or not route.strip():
            raise TypeError("route must be a non-empty str")
        if "model" in spawn_kwargs:
            raise ValueError("rlm.run accepts either route or model, not both")
        if _model_route_resolver is None:
            raise RuntimeError(
                "No Model Mesh route resolver is installed; enable the bundled models skill "
                "or pass an exact model selector"
            )
        resolution = await _model_route_resolver(route.strip(), prompt, spawn_kwargs)
        if (
            not isinstance(resolution, RLMRouteResolution)
            or not isinstance(resolution.model, str)
            or not resolution.model
            or (
                resolution.effort is not None and not isinstance(resolution.effort, str)
            )
        ):
            raise RuntimeError("Model Mesh returned an invalid route resolution")
        spawn_kwargs["model"] = resolution.model
        if resolution.effort is not None:
            spawn_kwargs.setdefault("effort", resolution.effort)
    payload = await host_request("rlm.run", {"prompt": prompt, "kwargs": spawn_kwargs})
    return _spawn_handle_from_payload(payload)


def _model_from_payload(payload: Any) -> RLMModel:
    if not isinstance(payload, dict):
        raise RuntimeError("rlm.find_models returned an invalid model entry")
    provider = payload.get("provider")
    model_id = payload.get("id")
    name = payload.get("name")
    selector = payload.get("selector")
    reasoning = payload.get("reasoning")
    input_types = payload.get("input")
    context_window = payload.get("contextWindow")
    max_tokens = payload.get("maxTokens")
    cost = payload.get("cost")
    valid_strings = all(
        isinstance(value, str) and value
        for value in (provider, model_id, name, selector)
    )
    valid_input = (
        isinstance(input_types, list)
        and bool(input_types)
        and all(value in {"text", "image"} for value in input_types)
    )
    valid_limits = (
        isinstance(context_window, int)
        and not isinstance(context_window, bool)
        and context_window > 0
        and isinstance(max_tokens, int)
        and not isinstance(max_tokens, bool)
        and max_tokens > 0
    )
    cost_keys = ("input", "output", "cacheRead", "cacheWrite")
    valid_cost = isinstance(cost, dict) and all(
        isinstance(cost.get(key), (int, float))
        and not isinstance(cost.get(key), bool)
        and cost[key] >= 0
        for key in cost_keys
    )
    if not (
        valid_strings
        and isinstance(reasoning, bool)
        and valid_input
        and valid_limits
        and valid_cost
    ):
        raise RuntimeError("rlm.find_models returned an invalid model entry")
    return RLMModel(
        provider=provider,
        id=model_id,
        name=name,
        selector=selector,
        reasoning=reasoning,
        input=tuple(input_types),
        context_window=context_window,
        max_tokens=max_tokens,
        cost=RLMModelCost(
            input=float(cost["input"]),
            output=float(cost["output"]),
            cache_read=float(cost["cacheRead"]),
            cache_write=float(cost["cacheWrite"]),
        ),
    )


async def find_models(query: str = "", limit: int = 8) -> list[RLMModel]:
    """Search a bounded list of models backed by active user credentials."""
    if not isinstance(query, str):
        raise TypeError(f"query must be str, got {type(query).__name__}")
    if not isinstance(limit, int):
        raise TypeError(f"limit must be int, got {type(limit).__name__}")
    payload = await host_request("rlm.find_models", {"query": query, "limit": limit})
    models = payload.get("models")
    if not isinstance(models, list):
        raise RuntimeError("rlm.find_models returned an invalid models list")
    return [_model_from_payload(model) for model in models]


def _subagent_from_payload(
    payload: Any, operation: str = "rlm.list_subagents"
) -> RLMSubagent:
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation} returned an invalid subagent entry")
    child_id = payload.get("rlm_child_id")
    active_session_id = payload.get("active_session_id")
    session_id = payload.get("session_id")
    session_name = payload.get("session_name")
    session_dir = payload.get("session_dir")
    status = payload.get("status")
    usage_tokens = payload.get("usage_tokens")
    if not isinstance(child_id, str) or not child_id:
        raise RuntimeError(f"{operation} entry is missing rlm_child_id")
    if active_session_id is not None and not isinstance(active_session_id, str):
        raise RuntimeError(f"{operation} entry has invalid active_session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise RuntimeError(f"{operation} entry has invalid session_id")
    if not isinstance(session_name, str) or not session_name:
        raise RuntimeError(f"{operation} entry is missing session_name")
    if not isinstance(session_dir, str) or not session_dir:
        raise RuntimeError(f"{operation} entry is missing session_dir")
    if status not in {"running", "completed", "error"}:
        raise RuntimeError(f"{operation} entry has invalid status")
    if usage_tokens is not None and (
        isinstance(usage_tokens, bool)
        or not isinstance(usage_tokens, int)
        or usage_tokens < 0
    ):
        raise RuntimeError(f"{operation} entry has invalid usage_tokens")
    return RLMSubagent(
        rlm_child_id=child_id,
        active_session_id=active_session_id,
        session_id=session_id,
        session_name=session_name,
        session_dir=Path(session_dir),
        status=status,
        usage_tokens=usage_tokens,
    )


async def list_subagents() -> list[RLMSubagent]:
    """List direct RLM children retained by the current parent session."""
    payload = await host_request("rlm.list_subagents")
    entries = payload.get("subagents")
    if not isinstance(entries, list):
        raise RuntimeError("rlm.list_subagents returned an invalid subagents registry")
    return [_subagent_from_payload(entry) for entry in entries]


async def delete_subagent(target: str | RLMSubagent) -> RLMSubagent:
    """Delete one running or retained direct child from the current parent session."""
    if isinstance(target, RLMSubagent):
        selector = target.rlm_child_id
    elif isinstance(target, str):
        selector = target.strip()
        if not selector:
            raise ValueError("target must not be empty")
    else:
        raise TypeError(
            f"target must be str or RLMSubagent, got {type(target).__name__}"
        )
    payload = await host_request("rlm.delete_subagent", {"target": selector})
    return _subagent_from_payload(payload.get("subagent"), "rlm.delete_subagent")


class _HarnessProxy:
    """Resolve the harness state against the current environment on every access.

    The kernel forkserver preimports rlm in a template process before per-session
    env vars exist; a state bound at import time would freeze that (env-less)
    resolution into every forked kernel. Resolving per access picks up the env
    applied after fork. Resolution must never raise (a failure inside the kernel
    namespace would take down the kernel). When the local store is genuinely
    unconfigured (no session env, e.g. --no-session) reads see an empty view but
    local writes raise instructively instead of vanishing on kernel exit; any
    other resolution failure degrades to a shared in-memory store until local
    resolution starts succeeding.
    """

    _fallback: HarnessState | None = None
    _unpersisted: HarnessState | None = None

    def _resolve(self) -> HarnessState:
        try:
            return get_harness_state()
        except RuntimeError as exc:
            if "Local harness state requires" in str(exc):
                if _HarnessProxy._unpersisted is None:
                    _HarnessProxy._unpersisted = HarnessState(
                        in_memory=True,
                        local_write_error=(
                            f"{exc} This session has no persistent local harness store. "
                            "Configure a local store; cross-session activation requires Evolution Lab."
                        ),
                    )
                return _HarnessProxy._unpersisted
            return self._degraded()
        except Exception:  # pragma: no cover - harness access must never raise
            return self._degraded()

    @staticmethod
    def _degraded() -> HarnessState:
        if _HarnessProxy._fallback is None:
            _HarnessProxy._fallback = HarnessState(in_memory=True)
        return _HarnessProxy._fallback

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        return repr(self._resolve())


_harness_state = _HarnessProxy()


class _RLMCallable:
    harness = _harness_state
    get_harness_state = staticmethod(get_harness_state)

    async def run(self, prompt: str, **kwargs: Any) -> RLMSpawnHandle:
        return await run(prompt, **kwargs)

    async def find_models(self, query: str = "", limit: int = 8) -> list[RLMModel]:
        return await find_models(query, limit)

    async def list_subagents(self) -> list[RLMSubagent]:
        return await list_subagents()

    async def delete_subagent(self, target: str | RLMSubagent) -> RLMSubagent:
        return await delete_subagent(target)

    async def __call__(self, prompt: str, **kwargs: Any) -> RLMSpawnHandle:
        return await run(prompt, **kwargs)


rlm = _RLMCallable()
harness = _harness_state


class _CallableModule(types.ModuleType):
    async def __call__(self, prompt: str, **kwargs: Any) -> RLMSpawnHandle:
        return await run(prompt, **kwargs)


sys.modules[__name__].__class__ = _CallableModule

__all__ = [
    "HarnessEntry",
    "HarnessScope",
    "HarnessState",
    "McpIntegration",
    "McpToolError",
    "NotEnabled",
    "RLMCapabilityManifest",
    "RLMFilesystemCapabilities",
    "RLMModel",
    "RLMModelCost",
    "RLMNetworkCapabilities",
    "RLMProcessCapabilities",
    "RLMRouteResolution",
    "RLMSecretCapabilities",
    "RLMSpawnHandle",
    "RLMSubagent",
    "RefinementEvent",
    "delete_subagent",
    "find_models",
    "get_harness_state",
    "harness",
    "host_request",
    "list_subagents",
    "register_model_route_resolver",
    "rlm",
    "run",
]

# Lazily re-export the MCP base class. Kept lazy so `import rlm` never requires
# the optional `mcp` SDK — only integration packages that subclass it do.
_LAZY_MCP = {"McpIntegration", "McpToolError", "NotEnabled"}


def __getattr__(name: str) -> Any:
    if name in _LAZY_MCP:
        from . import mcp_base

        return getattr(mcp_base, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
