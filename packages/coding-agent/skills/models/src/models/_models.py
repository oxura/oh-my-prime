from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rlm import RLMModel


Effort = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class ModelMeshError(RuntimeError):
    """Base error for semantic model routing."""


class RouteNotFound(ModelMeshError):
    """Raised when a semantic route is not configured."""


class NoEligibleModel(ModelMeshError):
    """Raised when no authenticated model satisfies a route policy."""


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """Capability, ordering, and compute policy for one semantic route."""

    name: str
    candidates: tuple[str, ...] = ()
    effort: Effort | None = None
    require_reasoning: bool = False
    require_vision: bool = False
    require_local: bool = False
    min_context_window: int = 0
    prefer_low_cost: bool = False
    exploration_weight: float = 0.15


@dataclass(frozen=True, slots=True)
class ModelOutcome:
    """Verified outcome aggregate for a route/task/model tuple."""

    trials: int = 0
    verified: int = 0
    total_duration_ms: int = 0
    last_used_at: str | None = None

    @property
    def verified_rate(self) -> float:
        return self.verified / self.trials if self.trials else 0.0

    @property
    def mean_duration_ms(self) -> float | None:
        return self.total_duration_ms / self.trials if self.trials else None


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """An authenticated exact selector chosen for a semantic route."""

    route: str
    task_type: str
    model: RLMModel
    effort: Effort | None
    score: float
    reason: str
    prior: ModelOutcome

    @property
    def selector(self) -> str:
        return self.model.selector
