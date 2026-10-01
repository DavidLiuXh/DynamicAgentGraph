"""Public contracts. No imports from LangGraph or LangChain."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .capabilities.catalog import BUILTIN_REDUCER_CATALOG
from .graph.schemas import ContractModel, SchemaSpec, infer_schema, validate_schema, validate_value


class DynamicGraphError(Exception):
    pass


class ConfigurationError(DynamicGraphError):
    pass


class RegistrationError(DynamicGraphError):
    pass


class SuccessCriterion(ContractModel):
    id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1)


def default_output_schema():
    def item(properties):
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }

    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "evidence": item({"source": {"type": "string"}, "text": {"type": "string"}}),
            "limitations": item({"description": {"type": "string"}}),
        },
        "required": ["answer", "evidence", "limitations"],
        "additionalProperties": False,
    }


class GoalSpec(ContractModel):
    request_id: str | None = Field(default=None, max_length=128)
    parent_run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    objective: str = Field(min_length=1)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    input_schema: SchemaSpec | None = None
    output_schema: SchemaSpec = Field(
        default_factory=lambda: SchemaSpec.model_validate(default_output_schema())
    )

    @model_validator(mode="after")
    def validate_contract(self):
        if not self.objective.strip():
            raise ValueError("objective cannot be whitespace")
        if len({c.id for c in self.success_criteria}) != len(self.success_criteria):
            raise ValueError("Duplicate success criterion ID")
        if self.input_schema is None:
            self.input_schema = SchemaSpec.model_validate(infer_schema(self.inputs))
        for schema in (self.input_schema, self.output_schema):
            validate_schema(schema)
            if schema.type != "object":
                raise ValueError("Goal input/output root must be an object")
        validate_value(self.input_schema, self.inputs)
        return self


class CapabilityRef(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")

    @property
    def key(self):
        return f"{self.name}@{self.version}"


BUILTIN_REDUCERS = list(BUILTIN_REDUCER_CATALOG)


class ExecutionPolicy(ContractModel):
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_side_effect_tools: list[str] = Field(default_factory=list)
    allowed_evaluators: list[str] = Field(default_factory=list)
    allowed_reducers: list[str] = Field(default_factory=lambda: list(BUILTIN_REDUCERS))
    max_nodes: int = Field(default=32, ge=1, le=32)
    max_state_fields: int = Field(default=64, ge=1, le=64)
    max_graph_bytes: int = Field(default=262144, ge=1, le=262144)
    max_schema_depth: int = Field(default=8, ge=1, le=8)
    max_state_bytes: int = Field(default=8388608, ge=1)
    max_result_bytes: int = Field(default=2097152, ge=1)
    max_parallelism: int = Field(default=4, ge=1)
    max_planning_rounds: int = Field(default=3, ge=1)
    max_node_attempts: int = Field(default=2, ge=1)
    max_model_calls: int = Field(default=40, ge=0)
    max_tool_calls: int = Field(default=64, ge=0)
    run_timeout_seconds: float = Field(default=600.0, gt=0)
    node_timeout_seconds: float = Field(default=60.0, gt=0)
    # Reserved contract: Phase 1 rejects non-None values before any model/tool call.
    max_cost: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)


@dataclass(frozen=True)
class EngineConfig:
    runs_dir: Path | str = Path("runs")
    max_parallelism: int = 8
    recording_mode: Literal["minimal", "debug"] = "minimal"
    max_planner_input_bytes: int = 524288
    max_output_tokens: int = 16384
    sensitive_values: tuple[str, ...] = ()  # Restricted both in execution and records.
    redactor: Callable[[JsonValue], JsonValue] | None = None  # Records/public result only.

    def __post_init__(self):
        if (
            self.max_parallelism < 1
            or self.max_output_tokens < 1
            or self.max_planner_input_bytes < 1
        ):
            raise ConfigurationError("Limits must be positive")
        if self.recording_mode not in {"minimal", "debug"}:
            raise ConfigurationError("Unknown recording mode")


class CancellationToken:
    def __init__(self):
        self._event = asyncio.Event()

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()

    async def wait(self):
        await self._event.wait()


@dataclass(frozen=True)
class CallContext:
    run_id: str
    node_id: str
    attempt: int
    deadline: float
    cancellation_token: CancellationToken


class Diagnostic(ContractModel):
    code: str
    phase: str
    message: str
    node_id: str | None = None
    attempt: int | None = None
    path: str = ""
    severity: Literal["error", "warning"] = "error"
    # Reserved advice field; Phase 1 reports NONE. Branch on code in client integrations.
    action: Literal[
        "RETRY_NEW_RUN",
        "PROVIDE_CONTEXT",
        "REGISTER_CAPABILITY",
        "CHANGE_MODEL_CONFIG",
        "REVIEW_POLICY",
        "FIX_EXTENSION",
        "INSPECT_IMPLEMENTATION",
        "NONE",
    ] = "NONE"
    details: dict[str, JsonValue] = Field(default_factory=dict)


class RunResult(ContractModel):
    run_id: str
    request_id: str | None = None
    parent_run_id: str | None = None
    execution_status: Literal["COMPLETED", "FAILED", "CANCELLED"] = "FAILED"
    phase: str = "reception"
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    output_complete: bool = False
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    node_records: list[dict[str, JsonValue]] = Field(default_factory=list)
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    graph_ref: str | None = None
    graph_hash: str | None = None
    recording: dict[str, JsonValue] = Field(default_factory=dict)
