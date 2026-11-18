"""Run lifecycle and LangGraph coordination, without a second graph scheduler."""

import asyncio
import copy
import importlib.metadata
import json
import platform
import re
import time
from contextlib import ExitStack, aclosing
from uuid import uuid4

from .capabilities.registry import Registry
from .contracts import (
    CancellationToken,
    Diagnostic,
    EngineConfig,
    ExecutionPolicy,
    GoalSpec,
    RunResult,
)
from .execution.budget import Budget
from .execution.errors import RunFailure
from .execution.privacy import contains_sensitive
from .graph.compiler import compile_graph
from .graph.nodes import Runtime
from .graph.schemas import canonical, validate_value
from .graph.state import initial_state, unwrap
from .graph.validation import binding_value, validate_graph
from .planning.planner import plan
from .recording.checkpoint import Checkpoint, run_lock
from .recording.local import Recorder


class DynamicGraphEngine:
    def __init__(self, *, config=None, models):
        self.config = config or EngineConfig()
        self.models = models
        self._registry = Registry()
        self._semaphore = asyncio.Semaphore(self.config.max_parallelism)

    def register_tool(self, definition):
        if definition.kind != "tool":
            raise ValueError("register_tool requires a tool definition")
        self._registry.register(definition)

    def register_evaluator(self, definition):
        if definition.kind != "check":
            raise ValueError("register_evaluator requires a check definition")
        self._registry.register(definition)

    def register_reducer(self, definition):
        if definition.kind != "reducer":
            raise ValueError("register_reducer requires a reducer definition")
        self._registry.register(definition)

    def list_capabilities(self, kind=None):
        return self._registry.list(kind)

    def get_capability(self, name, version):
        return next(
            (
                info
                for info in self.list_capabilities()
                if info.name == name and info.version == version
            ),
            None,
        )

    async def run(self, *, goal, policy=None, cancellation_token=None, on_run_started=None):
        return await self._run(goal=goal, policy=policy, cancellation_token=cancellation_token,
                               on_run_started=on_run_started)

    async def run_graph(self, *, goal, graph_spec, policy=None, cancellation_token=None,
                        on_run_started=None):
        """Validate and execute a caller-supplied graph without invoking the planner."""
        from .graph.spec import GraphSpec

        graph_spec = GraphSpec.model_validate(graph_spec.document())
        return await self._run(
            goal=goal, policy=policy, cancellation_token=cancellation_token, graph_spec=graph_spec,
            on_run_started=on_run_started,
        )

    async def _run(self, *, goal, policy=None, cancellation_token=None, graph_spec=None,
                   on_run_started=None):
        with ExitStack() as resources:
            run = _Run(self, goal, policy, cancellation_token, resources=resources,
                       on_run_started=on_run_started)
            return await run.run(graph_spec)

    async def resume(self, *, run_id, policy, cancellation_token=None):
        """Continue an interrupted checkpoint using the original run ID and current policy."""
        try:
            return await self._resume(run_id, policy, cancellation_token)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RunFailure("RESUME_UNAVAILABLE", "Checkpoint could not be restored",
                             phase="reception") from exc

    async def _resume(self, run_id, policy, cancellation_token):
        from .graph.spec import GraphSpec

        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise RunFailure("RESUME_UNAVAILABLE", "Invalid run ID", phase="reception")
        recorder = Recorder(self.config, run_id)
        if recorder.path.is_symlink():
            raise RunFailure("RESUME_UNAVAILABLE", "Invalid run directory", phase="reception")
        with run_lock(recorder.path):
            checkpoint = Checkpoint.load(recorder).document
            if contains_sensitive(checkpoint, self.config.sensitive_values):
                raise RunFailure("CHECKPOINT_RESTRICTED", "Checkpoint contains restricted data", phase="reception")
            if checkpoint.get("terminal") is not None:
                return RunResult.model_validate(checkpoint["terminal"])
            outstanding = sorted(checkpoint["side_effects"])
            if outstanding:
                raise RunFailure(
                    "RESUME_SIDE_EFFECT_UNCERTAIN", "Unconfirmed side-effecting tool calls",
                    phase="reception", node_id=outstanding[0], details={"nodes": outstanding},
                )
            saved_policy = ExecutionPolicy.model_validate(checkpoint["policy"])
            current = ExecutionPolicy.model_validate(policy.model_dump())
            for key in ("allowed_tools", "allowed_side_effect_tools", "allowed_evaluators", "allowed_reducers"):
                setattr(saved_policy, key, sorted(set(getattr(saved_policy, key)) & set(getattr(current, key))))
            for key, value in current.model_dump().items():
                if isinstance(value, (int, float)) and getattr(saved_policy, key) is not None:
                    setattr(saved_policy, key, min(value, getattr(saved_policy, key)))
            if current.max_cost is not None or current.max_tokens is not None:
                raise RunFailure("BUDGET_UNENFORCEABLE", "Strict token or cost limit is unsupported",
                                 phase="reception")
            run = _Run(self, GoalSpec.model_validate(checkpoint["goal"]), saved_policy,
                       cancellation_token, checkpoint=checkpoint)
            if run.snapshot.snapshot_hash != checkpoint["snapshot_hash"]:
                raise RunFailure("RESUME_CAPABILITIES_CHANGED", "Registered capability contract changed",
                                 phase="reception")
            graph = GraphSpec.model_validate(checkpoint["graph"])
            return await run.run(graph)


class _Run:
    """Own one run's lifecycle and committed state; scheduling stays in LangGraph."""

    def __init__(self, engine, goal, policy, cancellation_token, *, resources=None,
                 on_run_started=None, checkpoint=None):
        # Revalidate at the boundary and freeze copies, including nested caller data.
        self.goal = GoalSpec.model_validate(
            goal.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        self.policy = ExecutionPolicy.model_validate((policy or ExecutionPolicy()).model_dump())
        self.token = cancellation_token or CancellationToken()
        self.config = engine.config
        self.models = engine.models
        self.budget = Budget(self.policy, self.token)
        self.result = RunResult(
            run_id=checkpoint["run_id"] if checkpoint is not None else uuid4().hex,
            request_id=self.goal.request_id,
            parent_run_id=self.goal.parent_run_id,
        )
        self.recorder = Recorder(self.config, self.result.run_id)
        self.snapshot = engine._registry.snapshot(self.policy)
        self.recorder.manifest.update(
            {
                "snapshot_hash": self.snapshot.snapshot_hash,
                "python_version": platform.python_version(),
                "dependencies": {
                    name: importlib.metadata.version(name)
                    for name in (
                        "dynamic-agent-graph",
                        "langgraph",
                        "langchain-core",
                        "langchain-openai",
                        "pydantic",
                    )
                },
                "model_bindings": {
                    role: getattr(getattr(self.models, role), "metadata", {"model": "unknown"})
                    for role in ("planner", "worker")
                },
                "recording_mode": self.config.recording_mode,
            }
        )
        self.state = {}
        self.spec = None
        self.report = None
        self.records = {}
        self.artifacts = []
        self.engine_semaphore = engine._semaphore
        self.on_run_started = on_run_started
        self.resources = resources
        self.restoring = checkpoint is not None
        self.checkpoint = Checkpoint(self.recorder, checkpoint) if self.restoring else None
        if self.restoring:
            self.state = checkpoint["state"]
            self.budget.restore(checkpoint["budget"])
            self.recorder.manifest = json.loads((self.recorder.path / "manifest.json").read_bytes())
            self.recorder.manifest["terminal"] = False
            self.recorder.manifest.pop("execution_status", None)
            with (self.recorder.path / "events.jsonl").open("rb") as events:
                for line in events:
                    try:
                        self.recorder.seq = max(self.recorder.seq, json.loads(line)["seq"])
                    except (ValueError, KeyError):
                        continue

    def _before_dispatch(self, node_id, attempt, side_effect):
        attempts = {**self.checkpoint.document["attempts"], node_id: attempt}
        effects = dict(self.checkpoint.document["side_effects"])
        if side_effect:
            effects[node_id] = attempt
        self.checkpoint.save(attempts=attempts, side_effects=effects, budget=self.budget.checkpoint())

    def _commit_checkpoint(self):
        committed = set(self.state.get("__committed_nodes", {}))
        self.checkpoint.save(
            state=self.state, budget=self.budget.checkpoint(),
            records={key: self.records[key] for key in committed},
            artifacts=[entry for entry in self.artifacts if entry["node_id"] in committed],
            side_effects={key: value for key, value in self.checkpoint.document["side_effects"].items()
                          if key not in committed},
        )

    async def _prepare_graph(self, graph_spec):
        """Record, validate and compile the plan before any business node runs."""
        if self.restoring:
            self.recorder.event("run_resumed")
        else:
            self.recorder.create()
            if self.on_run_started is not None:
                self.resources.enter_context(run_lock(self.recorder.path))
            self.recorder.event("run_started")
        if contains_sensitive(
            self.goal.model_dump(mode="json", by_alias=True), self.config.sensitive_values
        ):
            raise RunFailure(
                "INVALID_GOAL_SPEC",
                "Goal contains credentials or restricted data",
                phase="reception",
            )
        if contains_sensitive(self.snapshot.catalog(), self.config.sensitive_values):
            raise RunFailure(
                "CAPABILITY_NOT_SUPPORTED",
                "Capability metadata contains restricted data",
                phase="reception",
            )
        if not self.restoring:
            self.recorder.write(
                "goal.json", self.goal.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            self.recorder.write("policy.json", self.policy.model_dump(mode="json"))
            self.recorder.write(
                "capability_snapshot.json",
                {"snapshot_hash": self.snapshot.snapshot_hash, "capabilities": self.snapshot.catalog()},
            )
        if self.policy.max_cost is not None or self.policy.max_tokens is not None:
            raise RunFailure(
                "BUDGET_UNENFORCEABLE",
                "Strict cost/token budgets are not supported in Phase 1",
                phase="reception",
            )
        self.budget.check()
        self.result.phase = "planning"
        self.recorder.phase(self.result.phase)
        if graph_spec is None:
            self.spec, self.report, attempt = await plan(
                self.goal,
                self.snapshot,
                self.policy,
                self.models.planner,
                self.config,
                self.budget,
                self.recorder,
                self.engine_semaphore,
            )
        else:
            self.spec = copy.deepcopy(graph_spec)
            self.report = validate_graph(self.spec, self.goal, self.snapshot, self.policy)
            attempt = 0
            if not self.report.valid:
                raise RunFailure(
                    "GRAPH_VALIDATION_FAILED",
                    "Reference graph failed validation",
                    phase="validation",
                    details={
                        "validation_errors": [
                            d.model_dump(mode="json") for d in self.report.diagnostics
                        ]
                    },
                )
        self.result.diagnostics.extend(
            d for d in self.report.diagnostics if d.severity == "warning"
        )
        self.result.phase = "saving"
        self.recorder.phase(self.result.phase)
        self.result.graph_ref, self.result.graph_hash = self.recorder.save_graph(self.spec, attempt)
        for node in self.spec.nodes:
            self.records[node.id] = {
                "node_id": node.id,
                "status": "pending",
                "attempts": 0,
                "commit_state": "uncommitted",
            }
        if self.restoring:
            self.records.update(self.checkpoint.document["records"])
            self.artifacts.extend(self.checkpoint.document["artifacts"])
            for node_id, attempts in self.checkpoint.document["attempts"].items():
                self.records[node_id]["attempts"] = attempts
        runtime = Runtime(
            goal=self.goal,
            policy=self.policy,
            config=self.config,
            snapshot=self.snapshot,
            report=self.report,
            models=self.models,
            budget=self.budget,
            recorder=self.recorder,
            engine_semaphore=self.engine_semaphore,
            run_semaphore=asyncio.Semaphore(
                min(self.policy.max_parallelism, self.config.max_parallelism)
            ),
            records=self.records,
            artifacts=self.artifacts,
        )
        self.result.phase = "compilation"
        self.recorder.phase(self.result.phase)
        try:
            compiled = compile_graph(self.spec, self.goal, self.snapshot, self.report, runtime)
        except RunFailure:
            raise
        except Exception as exc:
            raise RunFailure(
                "COMPILATION_FAILED",
                "Graph compilation failed",
                phase="compilation",
                details={"exception_type": type(exc).__name__},
            ) from exc
        self.recorder.write("compilation.json", compiled.summary)
        self.recorder.event("compiled")
        return compiled

    async def _execute(self, graph_spec):
        compiled = await self._prepare_graph(graph_spec)
        self.result.phase = "execution"
        self.recorder.phase(self.result.phase)
        seed = initial_state(self.report)
        if self.restoring and self.state:
            seed = {
                **{key: {"kind": "restore", "value": value} for key, value in unwrap(self.state).items()},
                "__committed_nodes": self.state["__committed_nodes"],
            }
        if self.on_run_started is not None:
            self.checkpoint = Checkpoint(self.recorder, {
                "version": 1, "run_id": self.result.run_id, "goal": self.goal.model_dump(mode="json"),
                "graph": self.spec.document(), "snapshot_hash": self.snapshot.snapshot_hash,
                "policy": self.policy.model_dump(mode="json"), "attempts": {}, "side_effects": {},
            })
            self._commit_checkpoint()
            self.on_run_started(self.result.run_id)
        if self.checkpoint is not None:
            self.budget.before_dispatch = self._before_dispatch
        # Do not enable LangGraph's eager semaphore gate: cancellation leaks queued
        # coroutine objects in 1.2.11. Adapter semaphores cap actual active handlers.
        async with aclosing(
            compiled.graph.astream(
                seed,
                {"recursion_limit": 2 * self.policy.max_nodes + 4},
                stream_mode="values",
            )
        ) as stream:
            async for snapshot_state in stream:
                self.budget.check()
                values = unwrap(snapshot_state)
                if len(canonical(values)) > self.policy.max_state_bytes:
                    raise RunFailure("STATE_TOO_LARGE", "Committed state exceeds allowed byte size")
                self.state = copy.deepcopy(snapshot_state)
                newly_committed = []
                for node_id in self.state.get("__committed_nodes", {}):
                    if self.records[node_id]["commit_state"] != "committed":
                        self.records[node_id].update(
                            {"status": "committed", "commit_state": "committed"}
                        )
                        for artifact in self.artifacts:
                            if artifact["node_id"] == node_id:
                                artifact["commit_state"] = "committed"
                        newly_committed.append(node_id)
                if self.checkpoint is not None:
                    self._commit_checkpoint()
                for node_id in newly_committed:
                    self.recorder.event("state_committed", node_id=node_id)
        self.result.execution_status = "COMPLETED"
        self.result.phase = "output"

    async def _sample_active_time(self):
        while True:
            await asyncio.sleep(1)
            if self.checkpoint is not None and self.budget.before_dispatch is not None:
                self.checkpoint.save_clock(self.budget.clock())

    async def run(self, graph_spec):
        direct_cancel = False
        task = asyncio.create_task(self._execute(graph_spec))
        cancellation = asyncio.create_task(self.token.wait())
        clock = (asyncio.create_task(self._sample_active_time())
                 if self.on_run_started is not None or self.restoring else None)
        pending = {task, cancellation}
        if clock is not None:
            pending.add(clock)
        try:
            done, _ = await asyncio.wait(
                pending,
                timeout=max(0, self.budget.deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if clock is not None and clock in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await clock
            elif task in done:
                await task
            elif cancellation in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.result.execution_status = "CANCELLED"
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise RunFailure(
                    "DEADLINE_EXCEEDED", "Run deadline exceeded", phase=self.result.phase
                )
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.result.execution_status = "CANCELLED"
            direct_cancel = not self.token.cancelled
        except RunFailure as exc:
            self.result.execution_status = "FAILED"
            self.result.phase = exc.phase
            self.result.diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    phase=exc.phase,
                    node_id=exc.node_id,
                    message=str(exc),
                    details=exc.details,
                )
            )
        except Exception as exc:
            self.result.execution_status = "FAILED"
            self.result.diagnostics.append(
                Diagnostic(
                    code="INTERNAL_ERROR",
                    phase=self.result.phase,
                    message="Unexpected implementation error",
                    details={"exception_type": type(exc).__name__},
                )
            )
        finally:
            cancellation.cancel()
            if clock is not None:
                clock.cancel()
                await asyncio.gather(clock, return_exceptions=True)
            await asyncio.gather(cancellation, return_exceptions=True)
        self._collect_outputs()
        self._finish()
        if direct_cancel:
            raise asyncio.CancelledError
        return self.result

    def _collect_outputs(self):
        """Expose only outputs whose writers reached a committed superstep."""
        if self.spec is None or self.report is None or not self.state:
            return
        committed = set(self.state.get("__committed_nodes", {}))
        values = unwrap(self.state)
        for name, binding in self.spec.outputs.items():
            writers = self.report.writers.get(binding.field, set())
            if not writers.issubset(committed):
                continue
            try:
                self.result.outputs[name] = binding_value(binding, self.goal.inputs, values)
            except (KeyError, ValueError):
                continue
        try:
            validate_value(self.goal.output_schema, self.result.outputs)
            self.result.output_complete = True
        except (ValueError, TypeError):
            self.result.output_complete = False
        if len(canonical(self.result.outputs)) > self.policy.max_result_bytes:
            self.result.outputs, self.result.output_complete = {}, False
            self.result.execution_status = "FAILED"
            self.result.diagnostics.append(
                Diagnostic(
                    code="RESULT_TOO_LARGE",
                    phase="output",
                    message="Output exceeds result size limit; see artifacts",
                )
            )

    def _finish(self):
        """Finalize public records and persist a terminal outcome, even after failure."""
        for record in self.records.values():
            if record["status"] == "pending":
                record.update({"status": "skipped", "reason": "run_terminated_before_start"})
        self.result.node_records = list(self.records.values())
        self.result.artifacts = self.artifacts
        self.result.usage = self.budget.summary()
        self.result.recording = {"status": "complete", "path": str(self.recorder.path)}
        # Untrusted model diagnostics and extension errors must also be safe in the
        # in-memory public result, not only in its persisted copy.
        self.result = RunResult.model_validate(
            self.recorder.redact(self.result.model_dump(mode="json"))
        )
        try:
            self.recorder.manifest.update(
                {
                    "phase": self.result.phase,
                    "terminal": True,
                    "execution_status": self.result.execution_status,
                }
            )
            self.recorder.event("run_finished")
            # The public result has already been redacted; save those exact values.
            self.recorder.write("result.json", self.result.model_dump(mode="json"), exact=True)
            self.recorder.write("manifest.json", self.recorder.manifest)
            if self.checkpoint is not None:
                if self.result.execution_status == "CANCELLED":
                    self.checkpoint.save(budget=self.budget.checkpoint())
                else:
                    self.checkpoint.save(terminal=self.result.model_dump(mode="json"))
        except (RunFailure, OSError) as exc:
            self.result.execution_status, self.result.phase = "FAILED", "finalization"
            self.result.recording["status"] = "failed"
            self.result.diagnostics.append(
                Diagnostic(
                    code="RECORDING_FAILED",
                    phase="finalization",
                    message="Run recording could not be completed",
                    details=getattr(exc, "details", {"errno": getattr(exc, "errno", None)}),
                )
            )
