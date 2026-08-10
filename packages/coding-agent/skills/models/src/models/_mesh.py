from __future__ import annotations

import asyncio
import fnmatch
import json
import math
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Sequence, cast

from rlm import RLMModel, rlm

from ._models import (
    Effort,
    ModelMeshError,
    ModelOutcome,
    ModelSelection,
    NoEligibleModel,
    RouteNotFound,
    RoutePolicy,
)


_STATE_VERSION = 1
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.05
_VALID_EFFORTS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
_LOCAL_MARKERS = ("local", "ollama", "lmstudio", "localhost", "vllm")


_DEFAULT_POLICIES = {
    policy.name: policy
    for policy in (
        RoutePolicy(name="fast", effort="low", prefer_low_cost=True, exploration_weight=0.2),
        RoutePolicy(name="code", effort="high"),
        RoutePolicy(name="deep", effort="xhigh", require_reasoning=True),
        RoutePolicy(name="review", effort="high", require_reasoning=True),
        RoutePolicy(name="vision", effort="high", require_vision=True),
        RoutePolicy(name="long-context", effort="high", min_context_window=128_000),
        RoutePolicy(name="private-local", effort="high", require_local=True),
        RoutePolicy(name="max", effort="max", require_reasoning=True, exploration_weight=0.05),
    )
}


ModelSource = Callable[[str, int], Awaitable[list[RLMModel]]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state_path() -> Path:
    override = os.environ.get("OH_MY_PRIME_MODEL_MESH_STATE")
    if override:
        return Path(override).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return root / "oh-my-prime" / "model-mesh.json"


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


def _lock_nonblocking(descriptor: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _route_payload(policy: RoutePolicy) -> dict[str, object]:
    return {
        "name": policy.name,
        "candidates": list(policy.candidates),
        "effort": policy.effort,
        "require_reasoning": policy.require_reasoning,
        "require_vision": policy.require_vision,
        "require_local": policy.require_local,
        "min_context_window": policy.min_context_window,
        "prefer_low_cost": policy.prefer_low_cost,
        "exploration_weight": policy.exploration_weight,
    }



def _parse_outcome_key(value: str) -> tuple[str, str, str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ModelMeshError(f"invalid Model Mesh outcome key: {value!r}") from error
    if (
        not isinstance(decoded, list)
        or len(decoded) != 3
        or not all(isinstance(item, str) and item for item in decoded)
    ):
        raise ModelMeshError(f"invalid Model Mesh outcome key: {value!r}")
    return decoded[0], decoded[1], decoded[2]

def _outcome_key(route: str, task_type: str, selector: str) -> str:
    return json.dumps((route, task_type, selector), separators=(",", ":"))


def _is_local(model: RLMModel) -> bool:
    text = f"{model.provider}/{model.id}/{model.name}".lower()
    return any(marker in text for marker in _LOCAL_MARKERS)


class ModelMesh:
    """Capability-aware semantic routes learned from verifier outcomes."""

    def __init__(
        self,
        state_path: str | os.PathLike[str] | None = None,
        *,
        model_source: ModelSource | None = None,
    ) -> None:
        self.state_path = Path(state_path).expanduser().resolve() if state_path else _default_state_path().resolve()
        self.model_source = model_source or rlm.find_models
        self._process_lock = asyncio.Lock()

    async def routes(self) -> dict[str, RoutePolicy]:
        """Return built-in routes overlaid by durable user configuration."""
        async with self._locked_state() as state:
            return self._policies_from_state(state)

    async def configure(
        self,
        route: str,
        *,
        candidates: Sequence[str] | None = None,
        effort: Effort | None = None,
        require_reasoning: bool | None = None,
        require_vision: bool | None = None,
        require_local: bool | None = None,
        min_context_window: int | None = None,
        prefer_low_cost: bool | None = None,
        exploration_weight: float | None = None,
    ) -> RoutePolicy:
        """Create or replace a semantic route policy atomically."""
        name = self._normalize_route_name(route)
        async with self._locked_state(write=True) as state:
            policies = self._policies_from_state(state)
            base = policies.get(name, RoutePolicy(name=name))
            policy = replace(
                base,
                candidates=(
                    self._normalize_candidate_patterns(candidates)
                    if candidates is not None
                    else base.candidates
                ),
                effort=self._normalize_effort(effort) if effort is not None else base.effort,
                require_reasoning=(
                    self._require_bool(require_reasoning, "require_reasoning")
                    if require_reasoning is not None
                    else base.require_reasoning
                ),
                require_vision=(
                    self._require_bool(require_vision, "require_vision")
                    if require_vision is not None
                    else base.require_vision
                ),
                require_local=(
                    self._require_bool(require_local, "require_local")
                    if require_local is not None
                    else base.require_local
                ),
                min_context_window=(
                    self._normalize_context_window(min_context_window)
                    if min_context_window is not None
                    else base.min_context_window
                ),
                prefer_low_cost=(
                    self._require_bool(prefer_low_cost, "prefer_low_cost")
                    if prefer_low_cost is not None
                    else base.prefer_low_cost
                ),
                exploration_weight=(
                    self._normalize_exploration_weight(exploration_weight)
                    if exploration_weight is not None
                    else base.exploration_weight
                ),
            )
            routes = state.setdefault("routes", {})
            if not isinstance(routes, dict):
                raise ModelMeshError("Model Mesh state has invalid routes")
            routes[name] = _route_payload(policy)
            return policy

    async def resolve(
        self,
        route: str,
        *,
        task_type: str = "code",
        independent_of: str | ModelSelection | Sequence[str] | None = None,
    ) -> ModelSelection:
        """Resolve one authenticated exact model using capabilities and verified history."""
        route_name = self._normalize_route_name(route)
        task = self._normalize_task_type(task_type)
        excluded = self._normalize_exclusions(independent_of)
        async with self._locked_state() as state:
            policies = self._policies_from_state(state)
            policy = policies.get(route_name)
            if policy is None:
                raise RouteNotFound(f"unknown model route: {route_name}")
            outcomes = self._outcomes_from_state(state)
        catalog = await self.model_source("", 100)
        eligible = self._eligible_models(policy, catalog, excluded)
        if not eligible:
            exclusions = f" (excluding {', '.join(sorted(excluded))})" if excluded else ""
            raise NoEligibleModel(
                f"no authenticated model satisfies route {route_name!r}{exclusions}"
            )
        total_trials = sum(
            outcome.trials
            for key, outcome in outcomes.items()
            if _parse_outcome_key(key)[:2] == (route_name, task)
        )
        ranked: list[tuple[float, str, RLMModel, ModelOutcome, str]] = []
        for model, explicit_priority in eligible:
            prior = outcomes.get(_outcome_key(route_name, task, model.selector), ModelOutcome())
            score, reason = self._score(
                policy,
                model,
                prior,
                total_trials=total_trials,
                explicit_priority=explicit_priority,
            )
            ranked.append((score, model.selector, model, prior, reason))
        ranked.sort(key=lambda item: item[1])
        score, _, model, prior, reason = max(ranked, key=lambda item: item[0])
        return ModelSelection(
            route=route_name,
            task_type=task,
            model=model,
            effort=policy.effort,
            score=score,
            reason=reason,
            prior=prior,
        )

    async def record(
        self,
        selection: ModelSelection,
        *,
        verified: bool,
        duration_ms: int,
    ) -> ModelOutcome:
        """Update the local capability matrix from an objective verifier outcome."""
        if not isinstance(selection, ModelSelection):
            raise TypeError("selection must be ModelSelection")
        if not isinstance(verified, bool):
            raise TypeError("verified must be bool")
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        key = _outcome_key(selection.route, selection.task_type, selection.selector)
        async with self._locked_state(write=True) as state:
            outcomes = self._outcomes_from_state(state)
            previous = outcomes.get(key, ModelOutcome())
            updated = ModelOutcome(
                trials=previous.trials + 1,
                verified=previous.verified + int(verified),
                total_duration_ms=previous.total_duration_ms + duration_ms,
                last_used_at=_utc_now(),
            )
            raw_outcomes = state.setdefault("outcomes", {})
            if not isinstance(raw_outcomes, dict):
                raise ModelMeshError("Model Mesh state has invalid outcomes")
            raw_outcomes[key] = asdict(updated)
            return updated

    def _eligible_models(
        self,
        policy: RoutePolicy,
        catalog: Sequence[RLMModel],
        excluded: set[str],
    ) -> list[tuple[RLMModel, int | None]]:
        explicit_matches: dict[str, int] = {}
        if policy.candidates:
            for model in catalog:
                selector = model.selector.lower()
                for index, pattern in enumerate(policy.candidates):
                    if fnmatch.fnmatchcase(selector, pattern.lower()):
                        explicit_matches[model.selector] = index
                        break
        eligible: list[tuple[RLMModel, int | None]] = []
        for model in catalog:
            if model.selector in excluded:
                continue
            if policy.candidates and model.selector not in explicit_matches:
                continue
            if policy.require_reasoning and not model.reasoning:
                continue
            if policy.require_vision and "image" not in model.input:
                continue
            if policy.require_local and not _is_local(model):
                continue
            if model.context_window < policy.min_context_window:
                continue
            eligible.append((model, explicit_matches.get(model.selector)))
        return eligible

    @staticmethod
    def _score(
        policy: RoutePolicy,
        model: RLMModel,
        prior: ModelOutcome,
        *,
        total_trials: int,
        explicit_priority: int | None,
    ) -> tuple[float, str]:
        bayesian_success = (prior.verified + 1) / (prior.trials + 2)
        exploration = policy.exploration_weight * math.sqrt(
            math.log(total_trials + 2) / (prior.trials + 1)
        )
        score = bayesian_success * 100 + exploration * 100
        reasons = [f"verified posterior={bayesian_success:.3f}", f"exploration={exploration:.3f}"]
        if explicit_priority is not None:
            priority_bonus = max(0.0, 30.0 - explicit_priority * 2.0)
            score += priority_bonus
            reasons.append(f"configured priority={explicit_priority + 1}")
        keyword_bonus = ModelMesh._route_keyword_bonus(policy.name, model)
        score += keyword_bonus
        if keyword_bonus:
            reasons.append(f"route affinity=+{keyword_bonus:.1f}")
        context_bonus = min(8.0, math.log2(max(model.context_window, 1) / 8_000 + 1))
        score += context_bonus
        if policy.prefer_low_cost:
            combined_cost = model.cost.input + model.cost.output
            cost_penalty = math.log1p(combined_cost) * 4
            score -= cost_penalty
            reasons.append(f"cost penalty=-{cost_penalty:.1f}")
        return score, "; ".join(reasons)

    @staticmethod
    def _route_keyword_bonus(route: str, model: RLMModel) -> float:
        text = f"{model.id} {model.name}".lower()
        keywords = {
            "fast": ("mini", "flash", "haiku", "nano", "lite", "small"),
            "code": ("codex", "code", "coder"),
            "deep": ("opus", "pro", "reason", "o3", "o4", "gpt-5"),
            "review": ("opus", "sonnet", "reason", "gpt-5"),
            "max": ("opus", "pro", "max", "gpt-5"),
        }.get(route, ())
        return 12.0 if any(keyword in text for keyword in keywords) else 0.0

    @asynccontextmanager
    async def _locked_state(self, *, write: bool = False) -> AsyncIterator[dict[str, object]]:
        async with self._process_lock:
            lock_path = self.state_path.with_suffix(f"{self.state_path.suffix}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _LOCK_TIMEOUT_SECONDS
            acquired = False
            try:
                while not _lock_nonblocking(descriptor):
                    if loop.time() >= deadline:
                        raise ModelMeshError("timed out waiting for Model Mesh state lock")
                    await asyncio.sleep(_LOCK_RETRY_SECONDS)
                acquired = True
                state = self._read_state()
                yield state
                if write:
                    _atomic_json(self.state_path, state)
            finally:
                try:
                    if acquired:
                        _unlock(descriptor)
                finally:
                    os.close(descriptor)

    def _read_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {"version": _STATE_VERSION, "routes": {}, "outcomes": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelMeshError(f"cannot read Model Mesh state: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            raise ModelMeshError("unsupported Model Mesh state format")
        return payload

    def _policies_from_state(self, state: dict[str, object]) -> dict[str, RoutePolicy]:
        policies = dict(_DEFAULT_POLICIES)
        raw_routes = state.get("routes", {})
        if not isinstance(raw_routes, dict):
            raise ModelMeshError("Model Mesh state has invalid routes")
        for name, raw in raw_routes.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                raise ModelMeshError("Model Mesh state contains an invalid route")
            try:
                policy = RoutePolicy(
                    name=self._normalize_route_name(raw["name"]),
                    candidates=self._normalize_candidate_patterns(raw["candidates"]),
                    effort=(self._normalize_effort(raw["effort"]) if raw["effort"] is not None else None),
                    require_reasoning=self._require_bool(raw["require_reasoning"], "require_reasoning"),
                    require_vision=self._require_bool(raw["require_vision"], "require_vision"),
                    require_local=self._require_bool(raw["require_local"], "require_local"),
                    min_context_window=self._normalize_context_window(raw["min_context_window"]),
                    prefer_low_cost=self._require_bool(raw["prefer_low_cost"], "prefer_low_cost"),
                    exploration_weight=self._normalize_exploration_weight(raw["exploration_weight"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ModelMeshError(f"Model Mesh route {name!r} is invalid") from error
            if policy.name != name:
                raise ModelMeshError(f"Model Mesh route key/name mismatch: {name!r}")
            policies[name] = policy
        return policies

    @staticmethod
    def _outcomes_from_state(state: dict[str, object]) -> dict[str, ModelOutcome]:
        raw_outcomes = state.get("outcomes", {})
        if not isinstance(raw_outcomes, dict):
            raise ModelMeshError("Model Mesh state has invalid outcomes")
        outcomes: dict[str, ModelOutcome] = {}
        for key, raw in raw_outcomes.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                raise ModelMeshError("Model Mesh state contains an invalid outcome")
            _parse_outcome_key(key)
            trials = raw.get("trials")
            verified = raw.get("verified")
            total_duration_ms = raw.get("total_duration_ms")
            last_used_at = raw.get("last_used_at")
            if (
                not isinstance(trials, int)
                or isinstance(trials, bool)
                or not isinstance(verified, int)
                or isinstance(verified, bool)
                or not isinstance(total_duration_ms, int)
                or isinstance(total_duration_ms, bool)
                or trials < 0
                or verified < 0
                or verified > trials
                or total_duration_ms < 0
                or (last_used_at is not None and not isinstance(last_used_at, str))
            ):
                raise ModelMeshError(f"Model Mesh outcome {key!r} is invalid")
            outcomes[key] = ModelOutcome(
                trials=trials,
                verified=verified,
                total_duration_ms=total_duration_ms,
                last_used_at=last_used_at,
            )
        return outcomes

    @staticmethod
    def _normalize_route_name(route: object) -> str:
        if not isinstance(route, str) or not route.strip():
            raise RouteNotFound("route must be a non-empty string")
        name = route.strip().lower()
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in name):
            raise RouteNotFound(f"invalid route name: {route!r}")
        return name

    @staticmethod
    def _normalize_task_type(task_type: object) -> str:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ModelMeshError("task_type must be a non-empty string")
        return task_type.strip().lower()

    @staticmethod
    def _normalize_candidate_patterns(candidates: object) -> tuple[str, ...]:
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ModelMeshError("candidates must be a sequence of selector patterns")
        normalized: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                raise ModelMeshError("candidate selector patterns must be non-empty strings")
            normalized.append(candidate.strip())
        if len(set(normalized)) != len(normalized):
            raise ModelMeshError("candidate selector patterns must be unique")
        return tuple(normalized)

    @staticmethod
    def _normalize_effort(effort: object) -> Effort:
        if not isinstance(effort, str) or effort not in _VALID_EFFORTS:
            raise ModelMeshError(f"effort must be one of: {', '.join(sorted(_VALID_EFFORTS))}")
        return cast(Effort, effort)

    @staticmethod
    def _normalize_context_window(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ModelMeshError("min_context_window must be a non-negative integer")
        return value

    @staticmethod
    def _normalize_exploration_weight(value: object) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ModelMeshError("exploration_weight must be numeric")
        normalized = float(value)
        if normalized < 0 or normalized > 2:
            raise ModelMeshError("exploration_weight must be between 0 and 2")
        return normalized

    @staticmethod
    def _require_bool(value: object, name: str) -> bool:
        if not isinstance(value, bool):
            raise ModelMeshError(f"{name} must be bool")
        return value

    @staticmethod
    def _normalize_exclusions(
        value: str | ModelSelection | Sequence[str] | None,
    ) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, ModelSelection):
            return {value.selector}
        if isinstance(value, str):
            return {value}
        if not isinstance(value, Sequence):
            raise ModelMeshError("independent_of must be a selector, selection, sequence, or None")
        selectors: set[str] = set()
        for selector in value:
            if not isinstance(selector, str) or not selector.strip():
                raise ModelMeshError("independent_of selectors must be non-empty strings")
            selectors.add(selector.strip())
        return selectors
