"""Explicit paid benchmark. Sequential execution stops immediately on quota exhaustion."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from dynamic_graph.devtools.fixtures import cases
from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.planning.prompts import resources


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--case", action="append")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    cfg = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
    client = LangChainModelClient(api_key=cfg["DEEPSEEK_API_KEY"])
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "model": client.metadata,
        "versions": resources()[3],
        "runs": [],
        "quota_exhausted": False,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(f"experiments/live-{stamp}.json")
    selected = [case for case in cases() if not args.case or case.name in args.case]
    for case in selected:
        for repeat in range(args.repetitions):
            engine = case.engine(Path("runs") / f"live-{stamp}", client)
            if args.debug:
                from dataclasses import replace

                engine.config = replace(engine.config, recording_mode="debug")
            result = await engine.run(goal=case.goal, policy=case.policy)
            run_dir = Path(result.recording["path"])
            first = run_dir / "planning/validation-01.json"
            first_valid = bool(
                first.exists()
                and isinstance(data := json.loads(first.read_text()), dict)
                and data.get("valid")
                and data.get("outcome") != "blocked"
            )
            row = {
                "case": case.name,
                "category": case.category,
                "repeat": repeat + 1,
                "run_id": result.run_id,
                "first_valid": first_valid,
                "graph_valid": result.graph_ref is not None,
                "status": result.execution_status,
                "caller_accepted": case.accepts(result),
                "usage": result.usage,
                "diagnostics": [d.model_dump(mode="json") for d in result.diagnostics],
                "recording_path": str(run_dir),
            }
            report["runs"].append(row)
            report["quota_exhausted"] = any(
                d.code == "MODEL_QUOTA_EXHAUSTED" for d in result.diagnostics
            )
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in (
                            "case",
                            "repeat",
                            "first_valid",
                            "graph_valid",
                            "status",
                            "caller_accepted",
                        )
                    }
                ),
                flush=True,
            )
            if report["quota_exhausted"]:
                print("MODEL_QUOTA_EXHAUSTED: stop development and notify user.", flush=True)
                return
    rows = report["runs"]
    report["summary"] = {
        "runs": len(rows),
        "first_valid": sum(r["first_valid"] for r in rows),
        "graph_valid": sum(r["graph_valid"] for r in rows),
        "completed": sum(r["status"] == "COMPLETED" for r in rows),
        "caller_accepted": sum(r["caller_accepted"] for r in rows),
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(path), **report["summary"]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
