"""Run lifecycle and LangGraph coordination, without a second graph scheduler."""

import asyncio
import copy
import importlib.metadata
import platform
import time
from contextlib import aclosing
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

    async def run(self, *, goal, policy=None, cancellation_token=None):
        return await self._run(goal=goal, policy=policy, cancellation_token=cancellation_token)

    async def _run(self, *, goal, policy=None, cancellation_token=None, graph_spec=None):
        run = _Run(self, goal, policy, cancellation_token)
        return await run.run(graph_spec)


class _Run:
    """Own one run's lifecycle and committed state; scheduling stays in LangGraph."""

    def __init__(self, engine, goal, policy, cancellation_token):
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
            run_id=uuid4().hex,
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

    async def _prepare_graph(self, graph_spec):
        """Record, validate and compile the plan before any business node runs."""
        self.recorder.create()
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
        # Do not enable LangGraph's eager semaphore gate: cancellation leaks queued
        # coroutine objects in 1.2.11. Adapter semaphores cap actual active handlers.
        async with aclosing(
            compiled.graph.astream(
                initial_state(self.report),
                {"recursion_limit": 2 * self.policy.max_nodes + 4},
                stream_mode="values",
            )
        ) as stream:
            async for snapshot_state in stream:
                self.budget.check()
                values = unwrap(snapshot_state)
                self.state = copy.deepcopy(snapshot_state)
                for node_id in self.state.get("__committed_nodes", {}):
                    if self.records[node_id]["commit_state"] != "committed":
                        self.records[node_id].update(
                            {"status": "committed", "commit_state": "committed"}
                        )
                        for artifact in self.artifacts:
                            if artifact["node_id"] == node_id:
                                artifact["commit_state"] = "committed"
                        self.recorder.event("state_committed", node_id=node_id)
                if len(canonical(values)) > self.policy.max_state_bytes:
                    raise RunFailure("STATE_TOO_LARGE", "Committed state exceeds allowed byte size")
        self.result.execution_status = "COMPLETED"
        self.result.phase = "output"

    async def run(self, graph_spec):
        direct_cancel = False
        task = asyncio.create_task(self._execute(graph_spec))
        cancellation = asyncio.create_task(self.token.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancellation},
                timeout=max(0, self.budget.deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
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
