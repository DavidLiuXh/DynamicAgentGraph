"""Explicit single paid search. Load local credentials and record only a safe summary."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from dynamic_graph import CancellationToken
from dynamic_graph.contracts import CallContext
from dynamic_graph.execution.errors import ToolCallError
from dynamic_graph.execution.privacy import contains_sensitive
from dynamic_graph.graph.schemas import validate_value
from dynamic_graph.tools import tavily_search_tool


async def main():
    root = Path(__file__).resolve().parents[1]
    config = dict(
        line.split("=", 1)
        for line in (root / ".env").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    key = os.environ.get("TAVILY_API_KEY") or config.get("TAVILY_API_KEY")
    tool = tavily_search_tool(api_key=key)
    data = {"query": "LangGraph official graph API documentation", "max_results": 2}
    validate_value(tool.input_schema, data)
    started = time.monotonic()
    context = CallContext(
        run_id="tavily-compatibility",
        node_id="search",
        attempt=1,
        deadline=started + 30,
        cancellation_token=CancellationToken(),
    )
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "tool": f"{tool.name}@{tool.version}",
        "search_depth": "basic",
        "input": data,
        "attempts": 1,
    }
    try:
        output = await tool.handler(data, context)
        validate_value(tool.output_schema, output)
        if contains_sensitive(output, (key,)):
            raise ValueError("Search output contains restricted data")
        report.update(
            status="passed",
            result_count=len(output["results"]),
            results=[
                {"title": item["title"], "url": item["url"], "score": item["score"]}
                for item in output["results"]
            ],
        )
    except ToolCallError as exc:
        report.update(status="failed", error=str(exc), retryable=exc.retryable)
    except (TimeoutError, ValueError):
        report.update(status="failed", error="Deadline or response contract check failed")
    report["duration_seconds"] = time.monotonic() - started
    path = root / "experiments/tavily-smoke-result.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
