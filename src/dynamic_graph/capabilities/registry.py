from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..contracts import CapabilityRef, RegistrationError
from ..graph.schemas import canonical, validate_schema
from .catalog import BUILTIN_REDUCER_CATALOG


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    handler: Callable = field(repr=False)
    read_only: bool = True
    idempotent: bool = True
    reentrant: bool = True
    timeout_hint: float | None = None
    kind: str = "tool"


@dataclass(frozen=True)
class EvaluatorDefinition(ToolDefinition):
    kind: str = "check"


@dataclass(frozen=True)
class ReducerDefinition:
    name: str
    version: str
    description: str
    value_schema: dict
    update_schema: dict
    config_schema: dict
    initial_validator: Callable = field(repr=False)
    handler: Callable = field(repr=False)
    parallel_safe: bool = False
    kind: str = "reducer"


def metadata(definition):
    """Select serializable metadata. Nested schemas are borrowed, not copied."""
    return {
        key: value
        for key, value in vars(definition).items()
        if key not in {"handler", "initial_validator"}
    }


def copy_definition(definition):
    """Isolate metadata while retaining the original callable/service identities."""
    return replace(definition, **copy.deepcopy(metadata(definition)))


@dataclass(frozen=True)
class CapabilityInfo:
    name: str
    version: str
    kind: str
    description: str
    _serialized: bytes = field(repr=False)

    def to_dict(self):
        import json

        return json.loads(self._serialized)


@dataclass(frozen=True)
class Snapshot:
    entries: MappingProxyType
    locks: MappingProxyType
    snapshot_hash: str

    def catalog(self):
        return copy.deepcopy([metadata(v) for _, v in sorted(self.entries.items())])


class Registry:
    def __init__(self):
        self._entries = {}
        self._locks = {}
        self._lock = threading.RLock()

    def register(self, definition):
        try:
            ref = CapabilityRef(name=definition.name, version=definition.version)
            if ref.name.startswith(("core.", "builtin.")):
                raise ValueError("Reserved capability namespace")
            if not callable(definition.handler):
                raise ValueError("handler must be callable")
            signature = inspect.signature(definition.handler)
            argc = 3 if isinstance(definition, ReducerDefinition) else 2
            signature.bind(*([None] * argc))
            if isinstance(definition, ReducerDefinition):
                if inspect.iscoroutinefunction(definition.handler):
                    raise ValueError("Reducers must be synchronous")
                if not callable(definition.initial_validator) or inspect.iscoroutinefunction(
                    definition.initial_validator
                ):
                    raise ValueError("initial_validator must be synchronous")
                for schema in (
                    definition.value_schema,
                    definition.update_schema,
                    definition.config_schema,
                ):
                    validate_schema(schema)
            else:
                if definition.kind not in {"tool", "check"}:
                    raise ValueError("Unknown capability kind")
                if not inspect.iscoroutinefunction(definition.handler):
                    raise ValueError("Tool/check handlers must be async")
                for schema in (definition.input_schema, definition.output_schema):
                    validate_schema(schema)
                if definition.input_schema.get("type") != "object":
                    raise ValueError("Tool inputs must be objects")
                if definition.timeout_hint is not None and definition.timeout_hint <= 0:
                    raise ValueError("Timeout hint must be positive")
            with self._lock:
                if ref.key in self._entries:
                    raise ValueError("Capability already registered")
                self._entries[ref.key] = copy_definition(definition)
                self._locks[ref.key] = asyncio.Lock()
        except (TypeError, ValueError) as exc:
            raise RegistrationError(str(exc)) from exc

    def snapshot(self, policy):
        allowed = {
            "tool": policy.allowed_tools,
            "check": policy.allowed_evaluators,
            "reducer": policy.allowed_reducers,
        }
        with self._lock:
            entries = {
                k: copy_definition(v) for k, v in self._entries.items() if k in allowed[v.kind]
            }
            locks = {k: self._locks[k] for k in entries}
        digest = hashlib.sha256(
            canonical([metadata(v) for _, v in sorted(entries.items())])
        ).hexdigest()
        return Snapshot(MappingProxyType(entries), MappingProxyType(locks), digest)

    def list(self, kind=None):
        with self._lock:
            custom = tuple(
                CapabilityInfo(v.name, v.version, v.kind, v.description, canonical(metadata(v)))
                for _, v in sorted(self._entries.items())
                if kind is None or v.kind == kind
            )
        builtins = []
        if kind in {None, "reducer"}:
            for builtin in BUILTIN_REDUCER_CATALOG.values():
                data = {
                    "name": builtin["name"],
                    "version": builtin["version"],
                    "kind": builtin["kind"],
                    "description": builtin["rule"],
                    "parallel_safe": builtin["parallel_safe"],
                    "schema_binding": "specialized to the graph field V/U schemas",
                }
                builtins.append(
                    CapabilityInfo(
                        name=data["name"],
                        version=data["version"],
                        kind=data["kind"],
                        description=data["description"],
                        _serialized=canonical(data),
                    )
                )
        return tuple(sorted((*custom, *builtins), key=lambda info: (info.name, info.version)))
