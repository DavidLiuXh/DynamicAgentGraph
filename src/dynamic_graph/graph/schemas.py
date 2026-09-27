"""Finite JSON Schema language, strict values and conservative assignment proofs."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


JsonType = Literal["object", "array", "string", "integer", "number", "boolean", "null"]


class SchemaSpec(ContractModel):
    type: JsonType | list[JsonType] | None = None
    properties: dict[str, SchemaSpec] | None = None
    required: list[str] | None = None
    additionalProperties: bool | SchemaSpec | None = None
    items: SchemaSpec | None = None
    enum: list[JsonValue] | None = None
    minimum: float | int | None = None
    maximum: float | int | None = None
    exclusiveMinimum: float | int | None = None
    exclusiveMaximum: float | int | None = None
    minLength: int | None = Field(default=None, ge=0)
    maxLength: int | None = Field(default=None, ge=0)
    minItems: int | None = Field(default=None, ge=0)
    maxItems: int | None = Field(default=None, ge=0)
    description: str | None = None
    defs: dict[str, SchemaSpec] | None = Field(default=None, alias="$defs")
    ref: str | None = Field(default=None, alias="$ref")

    def document(self):
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class SchemaError(ValueError):
    def __init__(self, message, path="", code="INVALID_SCHEMA"):
        super().__init__(message)
        self.path, self.code = path, code


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def strict_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
    )


def pointer_tokens(pointer):
    if pointer == "":
        return []
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise SchemaError("Invalid JSON Pointer", pointer, "INVALID_BINDING")
    return [s.replace("~1", "/").replace("~0", "~") for s in pointer[1:].split("/")]


def resolve(value, pointer):
    """Read a JSON Pointer without copying; callers own any isolation they need."""
    for token in pointer_tokens(pointer):
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise SchemaError("Invalid array index", pointer, "INVALID_BINDING")
            try:
                value = value[int(token)]
            except IndexError as exc:
                raise SchemaError("Missing array item", pointer, "INVALID_BINDING") from exc
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise SchemaError("Missing pointer value", pointer, "INVALID_BINDING")
    return value


def validate_schema(schema, max_depth=8):
    document = schema.document() if isinstance(schema, SchemaSpec) else schema
    SchemaSpec.model_validate(document)
    canonical(document)
    Draft202012Validator.check_schema(document)

    visited = 0

    def walk(node, depth, stack):
        nonlocal visited
        visited += 1
        if visited > 4096:
            raise SchemaError("Expanded schema exceeds node limit")
        if depth > max_depth:
            raise SchemaError("Schema depth limit exceeded")
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/$defs/") or ref in stack:
                raise SchemaError("Only nonrecursive local $defs references are allowed")
            if set(node) - {"$ref", "description", "$defs"}:
                raise SchemaError("$ref siblings are not supported")
            for child in node.get("$defs", {}).values():
                walk(child, depth + 1, stack)
            walk(resolve(document, ref[1:]), depth + 1, stack | {ref})
            return
        kind = node.get("type")
        types = kind if isinstance(kind, list) else [kind]
        if isinstance(kind, list) and (len(kind) != 2 or "null" not in kind or len(set(kind)) != 2):
            raise SchemaError("Only a base type plus null union is supported")
        if kind is None:
            raise SchemaError("An explicit type or local reference is required")
        if "object" in types:
            if "additionalProperties" not in node or node["additionalProperties"] is True:
                raise SchemaError("Objects require false or typed additionalProperties")
            props = node.get("properties", {})
            if not set(node.get("required", [])).issubset(props):
                raise SchemaError("required must name declared properties")
            for child in props.values():
                walk(child, depth + 1, stack)
            if isinstance(node["additionalProperties"], dict):
                walk(node["additionalProperties"], depth + 1, stack)
        elif set(node) & {"properties", "required", "additionalProperties"}:
            raise SchemaError("Object keywords require object type")
        if "array" in types:
            if "items" not in node:
                raise SchemaError("Arrays require an items schema")
            walk(node["items"], depth + 1, stack)
        elif set(node) & {"items", "minItems", "maxItems"}:
            raise SchemaError("Array keywords require array type")
        if "string" not in types and set(node) & {"minLength", "maxLength"}:
            raise SchemaError("String constraints require string type")
        if not set(types) & {"integer", "number"} and set(node) & {
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        }:
            raise SchemaError("Numeric constraints require a numeric type")
        for lower, upper in [
            ("minLength", "maxLength"),
            ("minItems", "maxItems"),
            ("minimum", "maximum"),
        ]:
            if lower in node and upper in node and node[lower] > node[upper]:
                raise SchemaError("Contradictory bounds")
        if "enum" in node:
            if not node["enum"]:
                raise SchemaError("Empty enum")
            base = {k: v for k, v in node.items() if k != "enum"}
            for value in node["enum"]:
                validate_value(base, value)
        for child in node.get("$defs", {}).values():
            walk(child, depth + 1, stack)

    walk(document, 0, set())
    return document


def validate_value(schema, value):
    """Check a value without modifying or copying it; raise on contract violations."""
    document = schema.document() if isinstance(schema, SchemaSpec) else schema
    canonical(value)
    errors = sorted(Draft202012Validator(document).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        path = "/" + "/".join(
            str(x).replace("~", "~0").replace("/", "~1") for x in error.absolute_path
        )
        # Do not embed the failing data in diagnostics.
        raise SchemaError(f"Value violates {error.validator}", path, "TYPE_MISMATCH")


def expand(schema):
    document = schema.document() if isinstance(schema, SchemaSpec) else schema
    validate_schema(document)

    def visit(node):
        if "$ref" in node:
            return visit(resolve(document, node["$ref"][1:]))
        result = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            if key == "properties":
                result[key] = {name: visit(child) for name, child in value.items()}
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                result[key] = visit(value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    return visit(document)


def pointer_schema(schema, pointer):
    node = expand(schema)
    for token in pointer_tokens(pointer):
        if node.get("type") == "object" and token in node.get("required", []):
            node = node["properties"][token]
        elif node.get("type") == "array" and re.fullmatch(r"0|[1-9][0-9]*", token):
            if int(token) >= node.get("minItems", 0):
                raise SchemaError(
                    "Array item is not guaranteed present", pointer, "INVALID_BINDING"
                )
            node = node["items"]
        else:
            raise SchemaError("Pointer value is not guaranteed present", pointer, "INVALID_BINDING")
    return node


def compatible(source, target):
    """Prove source values are accepted by target; uncertainty is a rejection."""
    return compatible_expanded(expand(source), expand(target))


def equivalent(source, target):
    """Validate/expand each schema once, then prove assignment in both directions."""
    source, target = expand(source), expand(target)
    return compatible_expanded(source, target) and compatible_expanded(target, source)


def _numeric_bound(schema, *, lower):
    """Return the tightest numeric endpoint and whether that endpoint is excluded."""
    if lower:
        inclusive_key, exclusive_key, default = "minimum", "exclusiveMinimum", -math.inf
    else:
        inclusive_key, exclusive_key, default = "maximum", "exclusiveMaximum", math.inf
    inclusive = schema.get(inclusive_key, default)
    if exclusive_key in schema:
        exclusive = schema[exclusive_key]
        tighter = exclusive >= inclusive if lower else exclusive <= inclusive
        if tighter:
            return exclusive, True
    return inclusive, False


def compatible_expanded(source, target):
    """Compare already validated, reference-free schemas; internal callers only."""

    if "enum" in source:
        try:
            for value in source["enum"]:
                validate_value(target, value)
            return True
        except SchemaError:
            return False
    if "enum" in target:
        return False
    source_types = source["type"] if isinstance(source["type"], list) else [source["type"]]
    target_types = target["type"] if isinstance(target["type"], list) else [target["type"]]
    if any(
        x not in target_types and not (x == "integer" and "number" in target_types)
        for x in source_types
    ):
        return False
    for low, high in [("minLength", "maxLength"), ("minItems", "maxItems")]:
        if source.get(low, 0) < target.get(low, 0) or source.get(high, math.inf) > target.get(
            high, math.inf
        ):
            return False
    for lower in (True, False):
        source_limit, source_exclusive = _numeric_bound(source, lower=lower)
        target_limit, target_exclusive = _numeric_bound(target, lower=lower)
        extends_past_target = source_limit < target_limit if lower else source_limit > target_limit
        includes_excluded_endpoint = (
            source_limit == target_limit and target_exclusive and not source_exclusive
        )
        if extends_past_target or includes_excluded_endpoint:
            return False
    if "array" in source_types and not compatible_expanded(source["items"], target["items"]):
        return False
    if "object" in source_types:
        source_properties, target_properties = (
            source.get("properties", {}),
            target.get("properties", {}),
        )
        if not set(target.get("required", [])).issubset(source.get("required", [])):
            return False
        source_extra, target_extra = source["additionalProperties"], target["additionalProperties"]
        for key, child in source_properties.items():
            target_child = target_properties.get(key, target_extra)
            if target_child is False or not compatible_expanded(child, target_child):
                return False
        if source_extra is not False:
            if target_extra is False or not compatible_expanded(source_extra, target_extra):
                return False
            if any(
                k not in source_properties and not compatible_expanded(source_extra, v)
                for k, v in target_properties.items()
            ):
                return False
    return True


def infer_schema(value):
    if value is None:
        raise SchemaError("Null input requires an explicit schema", code="INPUT_SCHEMA_REQUIRED")
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        canonical(value)
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: infer_schema(v) for k, v in value.items()},
            "required": sorted(value),
            "additionalProperties": False,
        }
    if isinstance(value, list) and value:
        schemas = [infer_schema(v) for v in value]
        if all(s == schemas[0] for s in schemas):
            return {"type": "array", "items": schemas[0]}
    raise SchemaError("Ambiguous input needs an explicit schema", code="INPUT_SCHEMA_REQUIRED")
