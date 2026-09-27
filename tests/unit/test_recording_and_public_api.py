import json
import subprocess
import sys
from pathlib import Path

import pytest

from dynamic_graph import EngineConfig, ModelCallError
from dynamic_graph.recording.local import Recorder, restrict_windows_acl


@pytest.mark.parametrize("errno_value", [13, 28])
def test_recording_failure_preserves_os_reason(monkeypatch, tmp_path, errno_value):
    from dynamic_graph.execution.errors import RunFailure

    recorder = Recorder(EngineConfig(runs_dir=tmp_path), "r")
    recorder.create()

    def fail(*args, **kwargs):
        raise OSError(errno_value, "Injected filesystem failure")

    monkeypatch.setattr("dynamic_graph.recording.local.os.replace", fail)
    with pytest.raises(RunFailure) as error:
        recorder.write("result.json", {})
    assert error.value.code == "RECORDING_FAILED"
    assert error.value.details == {"errno": errno_value, "operation": "atomic_write"}
    assert not list(recorder.path.glob("*.tmp"))


def test_inspection_never_reports_abandoned_or_inconsistent_run_as_complete(tmp_path):
    from dynamic_graph.devtools.inspection import inspect_run

    assert inspect_run(tmp_path)["reason"] == "missing_manifest"
    manifest = {"terminal": False, "phase": "execution"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert inspect_run(tmp_path)["recording_status"] == "nonterminal"
    manifest.update(terminal=True, execution_status="FAILED")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert inspect_run(tmp_path)["reason"] == "missing_result"
    (tmp_path / "result.json").write_text('{"execution_status":"COMPLETED"}')
    assert inspect_run(tmp_path)["reason"] == "terminal_status_mismatch"
    (tmp_path / "result.json").write_text('{"execution_status":"FAILED"}')
    assert inspect_run(tmp_path)["recording_status"] == "complete"


def test_public_imports_do_not_require_langgraph_or_langchain():
    code = """
import sys
class Deny:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("langgraph", "langchain")):
            raise AssertionError("Public import crossed framework boundary: " + fullname)
sys.meta_path.insert(0, Deny())
from dynamic_graph import DynamicGraphEngine, GoalSpec, ModelBindings, FakeModelClient
from dynamic_graph.contracts import RunResult
from dynamic_graph.models.client import ModelRequest, ModelResponse
assert GoalSpec(objective="Example").objective == "Example"
"""
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("mode", ["minimal", "debug"])
async def test_invalid_json_recording_obeys_policy(setup_run, mode, tmp_path):
    secret = "sk-" + "c" * 32
    failure = ModelCallError(
        "MODEL_RESPONSE_INVALID", "Bad JSON", raw_response='{"value": "' + secret, retryable=True
    )
    engine, goal, policy, _ = setup_run(
        [failure], config=EngineConfig(runs_dir=tmp_path, recording_mode=mode)
    )
    policy.max_planning_rounds = 1
    result = await engine.run(goal=goal, policy=policy)
    path = Path(result.recording["path"])
    if mode == "debug":
        assert (path / "planning/attempt-01.txt").exists()
    else:
        assert not (path / "planning/attempt-01.txt").exists()
        assert json.loads((path / "planning/attempt-01.json").read_text())["raw_omitted"]
    for file in path.rglob("*"):
        if file.is_file():
            assert secret not in file.read_text()


async def test_transport_failure_does_not_fabricate_candidate(setup_run):
    engine, goal, policy, _ = setup_run([ModelCallError("MODEL_UNAVAILABLE", "No response")])
    result = await engine.run(goal=goal, policy=policy)
    path = Path(result.recording["path"])
    assert not list((path / "planning").glob("attempt-*"))
    assert (path / "planning/response-01.json").exists()


async def test_oversized_prompt_stops_before_request(setup_run, tmp_path):
    engine, goal, policy, model = setup_run(
        [], config=EngineConfig(runs_dir=tmp_path, max_planner_input_bytes=20)
    )
    result = await engine.run(goal=goal, policy=policy)
    assert result.diagnostics[-1].code == "PLANNER_CONTEXT_TOO_LARGE" and not model.requests


def test_atomic_files_are_private_and_graph_redaction_cannot_change_semantics(tmp_path, reference):
    from dynamic_graph.execution.errors import RunFailure
    from dynamic_graph.graph.spec import GraphSpec

    recorder = Recorder(
        EngineConfig(runs_dir=tmp_path, sensitive_values=("restricted phrase",)), "r"
    )
    recorder.create()
    reference["nodes"][2]["instruction"] = "restricted phrase"
    with pytest.raises(RunFailure, match="restricted literal"):
        recorder.save_graph(GraphSpec.model_validate(reference), 1)
    assert not (recorder.path / "graph.json").exists()
    assert (recorder.path.stat().st_mode & 0o777) == 0o700
    assert ((recorder.path / "manifest.json").stat().st_mode & 0o777) == 0o600


def test_windows_acl_command_is_bound_to_current_sid(monkeypatch, tmp_path):
    calls = []

    class Response:
        stdout = '"domain\\user","S-1-5-21-123"\n'

    def run(args, **kwargs):
        calls.append(args)
        return Response()

    monkeypatch.setattr(subprocess, "run", run)
    restrict_windows_acl(tmp_path)
    assert calls[-1] == [
        "icacls",
        str(tmp_path),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-21-123:(OI)(CI)F",
    ]


async def test_business_acceptance_remains_with_caller(setup_run):
    from dynamic_graph.contracts import SuccessCriterion

    engine, goal, policy, _ = setup_run()
    goal.success_criteria = [
        SuccessCriterion(id="ten_sources", description="Find at least ten sources")
    ]
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    assert len(result.outputs["findings"]) < 10  # Caller rejects the business outcome.
    assert "goal_status" not in result.model_dump()


@pytest.mark.parametrize(
    "value,sensitive_values",
    [
        ({"nested": [{"authorization": "Bearer example"}]}, ()),
        ({"message": "sk-" + "x" * 32}, ()),
        ({"message": "contains company-private phrase"}, ("company-private",)),
    ],
)
def test_restricted_data_detection_matches_builtin_redaction(value, sensitive_values):
    from dynamic_graph.execution.privacy import contains_sensitive, redact_sensitive

    assert contains_sensitive(value, sensitive_values)
    redacted = redact_sensitive(value, sensitive_values)
    assert not contains_sensitive(redacted, sensitive_values)
    assert contains_sensitive(value, sensitive_values)  # Caller data stays intact.


@pytest.mark.parametrize("stage", ["goal", "node"])
async def test_custom_redactor_cannot_disable_restricted_data_check(setup_run, tmp_path, stage):
    engine, goal, policy, model = setup_run(
        config=EngineConfig(
            runs_dir=tmp_path, sensitive_values=("restricted-marker",), redactor=lambda value: value
        )
    )
    if stage == "goal":
        goal.objective = "restricted-marker"
    else:
        model.responses = iter([next(model.responses), {"report": "restricted-marker"}])
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "FAILED"
    expected = "INVALID_GOAL_SPEC" if stage == "goal" else "NODE_OUTPUT_INVALID"
    assert result.diagnostics[-1].code == expected
    assert "restricted-marker" not in result.model_dump_json()
    if stage == "goal":
        assert not model.requests


async def test_public_and_saved_result_use_the_same_single_redaction(setup_run, tmp_path):
    def redactor(value):
        if isinstance(value, dict) and "execution_status" in value and "outputs" in value:
            value["outputs"]["report"] += " [recorded]"
        return value

    engine, goal, policy, _ = setup_run(config=EngineConfig(runs_dir=tmp_path, redactor=redactor))
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED"
    assert result.outputs["report"] == "summary [recorded]"
    saved = json.loads((Path(result.recording["path"]) / "result.json").read_text())
    assert saved == result.model_dump(mode="json")
