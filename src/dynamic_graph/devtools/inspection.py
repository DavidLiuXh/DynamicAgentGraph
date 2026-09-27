"""Read incomplete records without treating an interrupted run as successful."""

from pathlib import Path

from ..graph.schemas import strict_loads


def inspect_run(directory):
    path = Path(directory)
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        return {"recording_status": "incomplete", "reason": "missing_manifest"}
    manifest = strict_loads(manifest_path.read_bytes())
    if not manifest.get("terminal", False):
        return {
            "recording_status": "nonterminal",
            "phase": manifest.get("phase"),
            "reason": "run_may_be_active_or_interrupted",
            "manifest": manifest,
        }
    result_path = path / "result.json"
    if not result_path.exists():
        return {"recording_status": "incomplete", "reason": "missing_result", "manifest": manifest}
    result = strict_loads(result_path.read_bytes())
    if result.get("execution_status") != manifest.get("execution_status"):
        return {
            "recording_status": "inconsistent",
            "reason": "terminal_status_mismatch",
            "manifest": manifest,
        }
    return {"recording_status": "complete", "manifest": manifest, "result": result}
