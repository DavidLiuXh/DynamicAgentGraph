import asyncio
import time

from pydantic import ValidationError

from ..execution.errors import RunFailure
from ..execution.privacy import contains_sensitive, redact_sensitive
from ..graph.schemas import canonical
from ..graph.validation import validate_graph
from ..models.client import ModelCallError, ModelRequest
from .prompts import planning_data, resources
from .responses import PlanningResponse


async def plan(goal, snapshot, policy, model, config, budget, recorder, semaphore):
    system, repair_template, schema, versions = resources()
    recorder.manifest.update(versions)
    previous, errors = None, []
    last_code = "GRAPH_GENERATION_FAILED"
    for attempt in range(1, policy.max_planning_rounds + 1):
        budget.check()
        data = planning_data(goal, snapshot, policy, previous, errors, attempt)
        if len(canonical([system, schema, data])) > config.max_planner_input_bytes:
            raise RunFailure(
                "PLANNER_CONTEXT_TOO_LARGE",
                "Planner input exceeds configured context bound",
                phase="planning",
            )
        recorder.write(
            f"planning/request-{attempt:02d}.json",
            {
                **versions,
                "model": getattr(model, "metadata", {"model": "unknown"}),
                "attempt": attempt,
                "role": "planner",
                "max_output_tokens": config.max_output_tokens,
                "remaining_model_calls": policy.max_model_calls - budget.model_calls,
                "remaining_seconds": max(0.0, budget.deadline - time.monotonic()),
                "reconstruction_complete": config.recording_mode == "debug",
                "messages": {
                    "system": system,
                    "task_instruction": repair_template if errors else "Generate PlanningResponse.",
                    "input_data": data,
                }
                if config.recording_mode == "debug"
                else None,
            },
        )
        recorder.event("planning_attempt", attempt=attempt)
        response = None
        try:
            async with semaphore:
                await budget.reserve("model")
                response = await model.generate(
                    ModelRequest(
                        role="planner",
                        system_instruction=system,
                        task_instruction=repair_template
                        if errors
                        else "Generate PlanningResponse.",
                        input_data=data,
                        output_schema=schema,
                        max_output_tokens=config.max_output_tokens,
                        timeout_seconds=min(
                            policy.node_timeout_seconds, budget.deadline - time.monotonic()
                        ),
                    )
                )
            budget.usage.append(response.usage)
            recorder.write(
                f"planning/response-{attempt:02d}.json",
                {
                    "usage": response.usage,
                    "provider_request_id": response.provider_request_id,
                    "metadata": response.response_metadata,
                    "status": "returned",
                },
            )
            payload = response.payload
            recorder.write(
                f"planning/attempt-{attempt:02d}.json",
                payload
                if (config.recording_mode == "debug")
                else {"raw_omitted": True, "reconstruction_complete": False},
            )
            if len(canonical(payload)) > policy.max_graph_bytes:
                errors = [
                    {
                        "code": "PLAN_LIMIT_EXCEEDED",
                        "path": "",
                        "message": "Candidate exceeds byte limit",
                    }
                ]
                previous = None
                recorder.write(f"planning/validation-{attempt:02d}.json", errors)
                continue
            previous = redact_sensitive(payload, config.sensitive_values)
            parsed = PlanningResponse.model_validate(payload)
            if parsed.outcome == "blocked":
                recorder.write(
                    f"planning/validation-{attempt:02d}.json", {"valid": True, "outcome": "blocked"}
                )
                raise RunFailure(
                    "PLANNING_BLOCKED",
                    "Planner reported missing information or capability",
                    phase="planning",
                    details={
                        "diagnostics": [d.model_dump(mode="json") for d in parsed.diagnostics]
                    },
                )
            report = validate_graph(parsed.graph, goal, snapshot, policy)
            errors = [
                d.model_dump(mode="json") for d in report.diagnostics if d.severity == "error"
            ]
            if contains_sensitive(parsed.graph.document(), config.sensitive_values):
                errors.append(
                    {
                        "code": "GRAPH_SENSITIVE_LITERAL",
                        "path": "",
                        "message": "Use input references instead of restricted literals",
                    }
                )
            recorder.write(
                f"planning/validation-{attempt:02d}.json",
                {
                    "valid": not errors,
                    "diagnostics": [d.model_dump(mode="json") for d in report.diagnostics] + errors,
                },
            )
            recorder.event("validation_finished", attempt=attempt)
            if not errors:
                return parsed.graph, report, attempt
            last_code = "GRAPH_VALIDATION_FAILED"
        except ValidationError as exc:
            errors = [
                {
                    "code": "INVALID_GRAPH_SPEC",
                    "path": "/" + "/".join(map(str, e["loc"])),
                    "message": e["msg"],
                }
                for e in exc.errors(include_input=False, include_context=False)
            ]
            recorder.write(f"planning/validation-{attempt:02d}.json", errors)
        except ModelCallError as exc:
            budget.usage.append(exc.usage)
            raw_saved = False
            if exc.raw_response is not None:
                if len(exc.raw_response.encode("utf-8")) <= policy.max_graph_bytes:
                    previous = redact_sensitive(exc.raw_response, config.sensitive_values)
                    if config.recording_mode == "debug":
                        recorder.write_text(f"planning/attempt-{attempt:02d}.txt", exc.raw_response)
                        raw_saved = True
                if not raw_saved:
                    recorder.write(
                        f"planning/attempt-{attempt:02d}.json",
                        {"raw_omitted": True, "reconstruction_complete": False},
                    )
            recorder.write(
                f"planning/response-{attempt:02d}.json",
                {
                    "error_code": exc.code,
                    "usage": exc.usage,
                    "provider_request_id": exc.provider_request_id,
                    "raw_omitted": not raw_saved,
                },
            )
            if not exc.retryable:
                raise RunFailure(exc.code, str(exc), phase="planning", details=exc.details) from exc
            if exc.code in {"MODEL_RESPONSE_INVALID", "MODEL_RESPONSE_TRUNCATED"}:
                errors = [
                    {"code": exc.code, "path": "", "message": "Return a complete valid response"}
                ]
            else:
                if attempt < policy.max_planning_rounds:
                    await asyncio.sleep(min(0.1 * 2 ** (attempt - 1), 1.0))
            last_code = "GRAPH_GENERATION_FAILED"
    raise RunFailure(
        last_code,
        "Planner exhausted allowed rounds",
        phase="planning",
        details={"validation_errors": errors},
    )
