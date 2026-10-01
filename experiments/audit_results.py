"""Derive reviewable metrics and check live evidence against the current contracts."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from statistics import mean, median

from dynamic_graph.devtools.fixtures import cases
from dynamic_graph.graph.spec import GraphSpec
from dynamic_graph.graph.validation import validate_graph
from dynamic_graph.planning.prompts import planning_data, resources


def summarize(values):
    return (
        {
            "count": len(values),
            "minimum": min(values),
            "median": median(values),
            "mean": mean(values),
            "maximum": max(values),
        }
        if values
        else None
    )


def main():
    path = Path("experiments/live-20260911T113444Z.json")
    live = json.loads(path.read_text())
    # This report audits the fixed September Chinese-prompt runs, not new routing.
    system, _, _, versions = resources("zh")
    del versions["prompt_language"]  # The historical records predate this field.
    fixtures = {c.name: c for c in cases()}
    rows = []
    for row in live["runs"]:
        root = Path(row["recording_path"])
        case = fixtures[row["case"]]
        snapshot = case.engine("runs")._registry.snapshot(case.policy)
        request = json.loads((root / "planning/request-01.json").read_text())
        assert all(request[k] == v for k, v in versions.items())
        assert request["messages"]["system"] == system
        assert request["messages"]["input_data"] == planning_data(case.goal, snapshot, case.policy)
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]

        def times(kind):
            return [
                datetime.fromisoformat(e["timestamp_utc"]).timestamp()
                for e in events
                if e["event_type"] == kind
            ]

        item = {
            "case": row["case"],
            "repeat": row["repeat"],
            "run_id": row["run_id"],
            "planning_requests": len(list((root / "planning").glob("request-*.json"))),
            "graph_nodes": None,
            "graph_fields": None,
            "current_graph_valid": None,
            "planning_through_last_validation_seconds": None,
            "save_to_compiled_seconds": None,
            "compiled_to_finish_seconds": None,
        }
        if row["graph_valid"]:
            graph = GraphSpec.model_validate_json((root / "graph.json").read_bytes())
            valid = validate_graph(graph, case.goal, snapshot, case.policy).valid
            assert valid
            manifest = json.loads((root / "manifest.json").read_text())
            assert (
                hashlib.sha256((root / "graph.json").read_bytes()).hexdigest()
                == manifest["graph_hash"]
            )
            item.update(
                graph_nodes=len(graph.nodes),
                graph_fields=len(graph.state_fields),
                current_graph_valid=valid,
                planning_through_last_validation_seconds=times("validation_finished")[-1]
                - times("planning_attempt")[0],
                save_to_compiled_seconds=times("compiled")[0] - times("graph_saved")[0],
                compiled_to_finish_seconds=times("run_finished")[-1] - times("compiled")[0],
            )
        rows.append(item)
    totals = {
        key: sum(r["usage"][key] for r in live["runs"])
        for key in ("model_calls", "tool_calls", "known_input_tokens", "known_output_tokens")
    }
    report = {
        "live_report": str(path),
        "versions": versions,
        "initial_requests_match_current_contracts": len(rows),
        "saved_graphs_revalidated": sum(r["current_graph_valid"] is True for r in rows),
        "summary": live["summary"],
        "usage": {**totals, "cost": None},
        "unexpected_blocked": sum(
            any(d["code"] == "PLANNING_BLOCKED" for d in r["diagnostics"]) for r in live["runs"]
        ),
        "failures": [r for r in live["runs"] if not r["caller_accepted"]],
        "metrics": {
            key: summarize([r[key] for r in rows if r[key] is not None])
            for key in (
                "planning_requests",
                "graph_nodes",
                "graph_fields",
                "planning_through_last_validation_seconds",
                "save_to_compiled_seconds",
                "compiled_to_finish_seconds",
            )
        },
        "timing_note": "Event wall-clock intervals include local recording. compiled_to_finish includes finalization; planning timing is available for valid graphs only.",
        "runs": rows,
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src").rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        },
    }
    Path("experiments/final-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "initial_requests_match_current_contracts",
                    "saved_graphs_revalidated",
                    "usage",
                    "metrics",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
