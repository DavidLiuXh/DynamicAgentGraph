from typing import Literal

from pydantic import Field, JsonValue, model_serializer, model_validator

from ..contracts import CapabilityRef
from .schemas import ContractModel, SchemaSpec, pointer_tokens


class LiteralBinding(ContractModel):
    literal: JsonValue

    @model_serializer
    def serialize_literal(self):
        return {"literal": self.literal}


class ReferenceBinding(ContractModel):
    source: Literal["input", "state"]
    field: str | None = None
    pointer: str

    @model_validator(mode="after")
    def check_reference(self):
        pointer_tokens(self.pointer)
        if (self.source == "state") != (self.field is not None):
            raise ValueError("Only state references require a field")
        return self


Binding = LiteralBinding | ReferenceBinding


class ReducerBinding(CapabilityRef):
    config: dict[str, JsonValue] = Field(default_factory=dict)


class StateField(ContractModel):
    description: str | None = None
    value_schema: SchemaSpec
    update_schema: SchemaSpec
    initial: Binding
    reducer: ReducerBinding

    @model_validator(mode="after")
    def check_initial(self):
        if isinstance(self.initial, ReferenceBinding) and self.initial.source != "input":
            raise ValueError("State initialization only accepts input or literal")
        return self


class WriteSpec(ContractModel):
    field: str
    output_pointer: str


class NodeSpec(ContractModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    kind: Literal["llm", "tool", "check"]
    instruction: str | None = Field(
        default=None,
        description="Required nonempty task instruction for llm; omit or null for tool/check.",
    )
    capability: CapabilityRef | None = Field(
        default=None,
        description="Required exact authorized name/version for tool/check; omit or null for llm.",
    )
    model_role: Literal["worker"] | None = Field(
        default=None, description="Must be worker for every llm node; omit or null for tool/check."
    )
    input_schema: SchemaSpec
    input_bindings: dict[str, Binding]
    output_schema: SchemaSpec
    writes: list[WriteSpec]
    depends_on: list[str]

    @model_validator(mode="after")
    def check_kind(self):
        if self.kind == "llm":
            if not self.instruction or self.model_role != "worker" or self.capability is not None:
                raise ValueError("LLM nodes require instruction and worker role only")
        elif self.capability is None or self.instruction is not None or self.model_role is not None:
            raise ValueError("Tool/check nodes require only a capability")
        if self.input_schema.type != "object":
            raise ValueError("Node input schema must be an object")
        return self


class GraphSpec(ContractModel):
    dsl_version: Literal["1.0"]
    state_fields: dict[str, StateField]
    nodes: list[NodeSpec] = Field(min_length=1, max_length=32)
    outputs: dict[str, ReferenceBinding]

    def document(self):
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
