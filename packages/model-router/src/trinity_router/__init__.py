"""TRINITY Model Router — public API."""
from .circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, CircuitState
from .complexity import ComplexityResult, compute_complexity
from .limits import BudgetExceededError
from .metrics import router_metrics
from .providers import (
    ProviderModel,
    get_eligible_models,
    rank_by_cost,
    select_cheapest_capable,
)
from .quality_gate import QualityGateResult, evaluate as evaluate_quality
from .retry import FailureClass, classify_failure
from .router import (
    ModelRouter,
    ProviderUnavailableError,
    QualityNotEntitledError,
    RequestRejectedByProviderError,
    RoutingRequest,
    RoutingResult,
    RouterStreamChunk,
)
from .selector import TaskType

__all__ = [
    "ModelRouter",
    "RoutingRequest",
    "RoutingResult",
    "RouterStreamChunk",
    "TaskType",
    "BudgetExceededError",
    "ProviderUnavailableError",
    "QualityNotEntitledError",
    "RequestRejectedByProviderError",
    "FailureClass",
    "classify_failure",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitState",
    "ProviderModel",
    "select_cheapest_capable",
    "get_eligible_models",
    "rank_by_cost",
    "ComplexityResult",
    "compute_complexity",
    "router_metrics",
    "QualityGateResult",
    "evaluate_quality",
]
