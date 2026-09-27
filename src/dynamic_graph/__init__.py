"""Goal-to-graph execution library."""

__version__ = "0.1.0"

from .api import DynamicGraphEngine
from .capabilities.registry import EvaluatorDefinition, ReducerDefinition, ToolDefinition
from .contracts import CancellationToken, EngineConfig, ExecutionPolicy, GoalSpec, RunResult
from .models.client import (
    FakeModelClient,
    ModelBindings,
    ModelCallError,
    ModelRequest,
    ModelResponse,
)

__all__ = [
    "CancellationToken",
    "DynamicGraphEngine",
    "EngineConfig",
    "EvaluatorDefinition",
    "ExecutionPolicy",
    "FakeModelClient",
    "GoalSpec",
    "ModelBindings",
    "ModelCallError",
    "ModelRequest",
    "ModelResponse",
    "ReducerDefinition",
    "RunResult",
    "ToolDefinition",
]
