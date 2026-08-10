"""Semantic, capability-aware model routes learned from verified outcomes."""

from __future__ import annotations

from typing import Sequence

from rlm import RLMRouteResolution, register_model_route_resolver

from ._mesh import ModelMesh
from ._models import (
    Effort,
    ModelMeshError,
    ModelOutcome,
    ModelSelection,
    NoEligibleModel,
    RouteNotFound,
    RoutePolicy,
)


_default_mesh = ModelMesh()


async def routes() -> dict[str, RoutePolicy]:
    """Return built-in routes overlaid by user configuration."""
    return await _default_mesh.routes()


async def configure(
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
    """Create or replace a durable semantic route policy."""
    return await _default_mesh.configure(
        route,
        candidates=candidates,
        effort=effort,
        require_reasoning=require_reasoning,
        require_vision=require_vision,
        require_local=require_local,
        min_context_window=min_context_window,
        prefer_low_cost=prefer_low_cost,
        exploration_weight=exploration_weight,
    )


async def resolve(
    route: str,
    *,
    task_type: str = "code",
    independent_of: str | ModelSelection | Sequence[str] | None = None,
) -> ModelSelection:
    """Resolve one authenticated exact selector for a semantic route."""
    return await _default_mesh.resolve(
        route,
        task_type=task_type,
        independent_of=independent_of,
    )


async def record(
    selection: ModelSelection,
    *,
    verified: bool,
    duration_ms: int,
) -> ModelOutcome:
    """Record an objective verifier outcome in the local capability matrix."""
    return await _default_mesh.record(
        selection,
        verified=verified,
        duration_ms=duration_ms,
    )


async def _resolve_rlm_route(
    route: str,
    _prompt: str,
    kwargs: dict[str, object],
) -> RLMRouteResolution:
    task_type = kwargs.pop("task_type", route)
    independent_of = kwargs.pop("independent_of", None)
    selection = await _default_mesh.resolve(
        route,
        task_type=task_type,
        independent_of=independent_of,
    )
    return RLMRouteResolution(model=selection.selector, effort=selection.effort)


register_model_route_resolver(_resolve_rlm_route)


__all__ = [
    "Effort",
    "ModelMesh",
    "ModelMeshError",
    "ModelOutcome",
    "ModelSelection",
    "NoEligibleModel",
    "RouteNotFound",
    "RoutePolicy",
    "configure",
    "record",
    "resolve",
    "routes",
]
