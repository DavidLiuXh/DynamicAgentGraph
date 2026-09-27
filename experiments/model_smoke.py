"""Explicit paid compatibility probe. Never prints credentials or raw provider errors."""

import asyncio
import json
from pathlib import Path

from dynamic_graph.devtools.fixtures import cases
from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.models.client import ModelCallError, ModelRequest
from dynamic_graph.planning.responses import PlanningResponse


async def main():
    cfg = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
    client = LangChainModelClient(api_key=cfg["DEEPSEEK_API_KEY"])
    graph = json.loads(Path("tests/fixtures/reference_graph.json").read_text())
    samples = [
        {
            "response_version": "1.0",
            "outcome": "blocked",
            "graph": None,
            "diagnostics": [
                {
                    "reason_code": "CAPABILITY_GAP",
                    "message": "No search capability is available",
                    "related_input_paths": [],
                    "missing_information": [],
                    "required_capability_description": ["Read-only document search"],
                }
            ],
        },
        {"response_version": "1.0", "outcome": "graph", "graph": graph, "diagnostics": []},
    ]
    for name in ("evidence_counts", "nullable_source"):
        case = next(c for c in cases() if c.name == name)
        document = case.graph.document()
        if name == "nullable_source":
            for key in ("value_schema", "update_schema"):
                schema = document["state_fields"]["metadata"][key]
                schema["$defs"] = {"Author": schema["properties"]["author"]}
                schema["properties"]["author"] = {"$ref": "#/$defs/Author"}
        samples.append(
            {"response_version": "1.0", "outcome": "graph", "graph": document, "diagnostics": []}
        )
    results = []
    for sample in samples:
        try:
            response = await client.generate(
                ModelRequest(
                    "planner",
                    "Return exactly the supplied JSON as the structured response.",
                    "Preserve all fields, types and values.",
                    sample,
                    PlanningResponse.model_json_schema(),
                    timeout_seconds=60.0,
                )
            )
            validated = PlanningResponse.model_validate(response.payload)
            results.append(
                {
                    "outcome": validated.outcome,
                    "usage": response.usage,
                    "valid": True,
                    "roundtrip_equal": response.payload == sample,
                    "response_metadata": response.response_metadata,
                    "provider_request_id": response.provider_request_id,
                }
            )
        except ModelCallError as exc:
            results.append({"error_code": exc.code, "details": exc.details})
            if exc.code == "MODEL_QUOTA_EXHAUSTED":
                break
        except Exception as exc:
            results.append({"error_type": type(exc).__name__})
    Path("experiments/model_smoke_result.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == "__main__":
    asyncio.run(main())
