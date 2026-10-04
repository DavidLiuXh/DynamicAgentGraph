"""LangChain transport. No planning, registry access, retry or model selection."""

import asyncio
import copy
import hashlib
import json
import os

from pydantic import BaseModel

from ..graph.schemas import canonical, strict_loads
from .client import ModelCallError, ModelRequest, ModelResponse


class LangChainModelClient:
    def __init__(
        self,
        *,
        model="deepseek-flash",
        api_key=None,
        base_url="https://api.deepseek.com",
        mode="function_calling",
        allow_json_mode=False,
        chat_model=None,
        max_output_tokens=16384,
    ):
        if mode not in {"function_calling", "json_schema", "json_mode"}:
            raise ValueError("Unsupported structured output mode")
        if mode == "json_mode" and not allow_json_mode:
            raise ValueError("json_mode must be explicitly enabled")
        self.mode = mode
        self.model = model
        self.max_output_tokens = max_output_tokens
        if chat_model is None:
            from langchain_openai import ChatOpenAI

            chat_model = ChatOpenAI(
                model=model,
                api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
                base_url=base_url,
                max_retries=0,
                include_response_headers=True,
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
        elif getattr(chat_model, "max_retries", 0) != 0:
            raise ValueError("Provider retries must be disabled")
        self._chat = chat_model

    @property
    def metadata(self):
        return {
            "model": self.model,
            "mode": self.mode,
            "adapter_version": "1.0",
            "max_output_tokens": self.max_output_tokens,
        }

    async def generate(self, request: ModelRequest) -> ModelResponse:
        from langchain_core.messages import HumanMessage, SystemMessage

        original_schema = copy.deepcopy(request.output_schema)
        # Function parameters are objects; JSON Schema and JSON mode carry output
        # values directly and must retain a caller's scalar/array root schema.
        wrapped = self.mode == "function_calling" and original_schema.get("type") != "object"
        if wrapped:
            definitions = original_schema.pop("$defs", None)
            original_schema = {
                "type": "object",
                "properties": {"value": original_schema},
                "required": ["value"],
                "additionalProperties": False,
                **({"$defs": definitions} if definitions else {}),
            }
        schema = {
            **original_schema,
            "title": "StructuredResponse",
            "description": "The required structured response.",
        }
        messages = [
            SystemMessage(request.system_instruction),
            HumanMessage(
                json.dumps(
                    {
                        "task_instruction": request.task_instruction,
                        "input_data": request.input_data,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
        ]
        if self.mode == "json_mode":
            messages.append(HumanMessage("Return JSON matching this schema: " + json.dumps(schema)))
        try:
            runnable = self._chat.with_structured_output(
                schema,
                method=self.mode,
                include_raw=True,
            )
            async with asyncio.timeout(request.timeout_seconds):
                response = await runnable.ainvoke(messages, max_tokens=request.max_output_tokens)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ModelCallError(
                "MODEL_TIMEOUT", "Model request timed out", retryable=True
            ) from exc
        except Exception as exc:
            # Never expose raw provider exceptions: they can embed request bodies or credentials.
            status = getattr(exc, "status_code", None)
            body = getattr(exc, "body", {}) or {}
            code = body.get("code", "") if isinstance(body, dict) else ""
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                code = body["error"].get("code", code)
            if status == 402 or code in {"insufficient_quota", "insufficient_balance"}:
                error, retryable = "MODEL_QUOTA_EXHAUSTED", False
            elif status == 401:
                error, retryable = "MODEL_AUTH_FAILED", False
            elif status == 403:
                error, retryable = "MODEL_PERMISSION_DENIED", False
            elif status == 429:
                error, retryable = "MODEL_RATE_LIMITED", True
            elif status == 400 or isinstance(exc, NotImplementedError):
                error, retryable = "MODEL_STRUCTURED_OUTPUT_UNSUPPORTED", False
            else:
                error, retryable = "MODEL_UNAVAILABLE", status is None or status >= 500
            raise ModelCallError(
                error,
                "Model provider request failed",
                retryable=retryable,
                details={"http_status": status},
            ) from exc
        raw = response.get("raw")
        metadata = getattr(raw, "response_metadata", {}) or {}
        additional = getattr(raw, "additional_kwargs", {}) or {}
        known_usage = getattr(raw, "usage_metadata", None)
        usage = {"input_tokens": None, "output_tokens": None}
        if known_usage:
            usage.update({key: known_usage.get(key) for key in usage})
        headers = metadata.get("headers", {}) or {}
        raw_id = getattr(raw, "id", None)
        request_id = (
            metadata.get("request_id")
            or headers.get("x-request-id")
            or headers.get("x-ds-request-id")
            or metadata.get("id")
        )
        if request_id is None and raw_id and not raw_id.startswith("lc_run"):
            request_id = raw_id
        finish = metadata.get("finish_reason")
        if additional.get("refusal") or finish == "content_filter":
            raise ModelCallError(
                "MODEL_REFUSED",
                "Model refused response",
                usage=usage,
                provider_request_id=request_id,
            )
        if finish in {"length", "max_tokens"}:
            raise ModelCallError(
                "MODEL_RESPONSE_TRUNCATED",
                "Model response was truncated",
                retryable=True,
                usage=usage,
                provider_request_id=request_id,
            )
        payload = response.get("parsed")
        raw_response = getattr(raw, "content", None)
        for call in additional.get("tool_calls", []):
            arguments = call.get("function", {}).get("arguments")
            if isinstance(arguments, str):
                raw_response = arguments
                break
        invalid_calls = getattr(raw, "invalid_tool_calls", []) or []
        if invalid_calls:
            arguments = invalid_calls[0].get("args")
            if isinstance(arguments, str):
                raw_response = arguments
        if not isinstance(raw_response, str):
            raw_response = None
        if invalid_calls or response.get("parsing_error") is not None or payload is None:
            details = {}
            if raw_response:
                try:
                    strict_loads(raw_response)
                except json.JSONDecodeError as error:
                    details["json_syntax"] = {
                        "message": error.msg,
                        "line": error.lineno,
                        "column": error.colno,
                        "position": error.pos,
                    }
                except ValueError:
                    details["json_syntax"] = {"message": "Invalid strict JSON"}
            raise ModelCallError(
                "MODEL_RESPONSE_INVALID",
                "Structured response could not be parsed",
                retryable=True,
                usage=usage,
                provider_request_id=request_id,
                raw_response=raw_response,
                details=details,
            )
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        try:
            tool_calls = additional.get("tool_calls", [])
            if tool_calls:
                if len(tool_calls) != 1:
                    raise ValueError("Expected exactly one structured response")
                arguments = tool_calls[0].get("function", {}).get("arguments")
                if isinstance(arguments, str):
                    strict_loads(arguments)
            elif self.mode == "json_mode" and isinstance(getattr(raw, "content", None), str):
                strict_loads(raw.content)
            if wrapped:
                if not isinstance(payload, dict) or set(payload) != {"value"}:
                    raise ValueError("Invalid structured scalar wrapper")
                payload = payload["value"]
        except ValueError as exc:
            raise ModelCallError(
                "MODEL_RESPONSE_INVALID",
                "Response is not strict JSON",
                retryable=True,
                usage=usage,
                provider_request_id=request_id,
                raw_response=raw_response,
            ) from exc
        # Enforce strict JSON values even when a provider parser returns Python objects.
        try:
            payload = json.loads(json.dumps(payload, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ModelCallError(
                "MODEL_RESPONSE_INVALID",
                "Response is not strict JSON",
                usage=usage,
                provider_request_id=request_id,
            ) from exc
        return ModelResponse(
            payload,
            usage,
            request_id,
            {
                "finish_reason": finish,
                "model": metadata.get("model_name"),
                "mode": self.mode,
                "schema_adapter_version": "1.0",
                "transport_schema_hash": hashlib.sha256(canonical(schema)).hexdigest(),
                "wrapped_root": wrapped,
            },
        )
