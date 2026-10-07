"""Framework-independent, single-call model contract."""

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue


@dataclass(frozen=True)
class ModelRequest:
    role: str
    system_instruction: str
    task_instruction: str
    input_data: JsonValue
    output_schema: dict
    max_output_tokens: int = 16384
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class ModelResponse:
    payload: JsonValue
    usage: dict = field(default_factory=lambda: {"input_tokens": None, "output_tokens": None})
    provider_request_id: str | None = None
    response_metadata: dict = field(default_factory=dict)


class ModelCallError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable=False,
        usage=None,
        provider_request_id=None,
        details=None,
        raw_response=None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.usage = usage or {"input_tokens": None, "output_tokens": None}
        self.provider_request_id = provider_request_id
        self.details = details or {}
        self.raw_response = raw_response


class ModelClient(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class FakeModelClient:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, ModelResponse) else ModelResponse(response)


@dataclass(frozen=True)
class ModelBindings:
    planner: ModelClient
    worker: ModelClient


def expanded_output_budget(current: int, model: ModelClient) -> int:
    """One bounded recovery budget, respecting a client's explicit transport cap."""
    ceiling = 32768
    metadata = getattr(model, "metadata", {})
    cap = metadata.get("max_output_tokens") if isinstance(metadata, dict) else None
    if type(cap) is int and cap > 0:
        ceiling = min(ceiling, cap)
    return max(current, min(current * 2, ceiling))
