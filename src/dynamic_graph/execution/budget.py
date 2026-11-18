import asyncio
import time

from .errors import RunFailure


class Budget:
    def __init__(self, policy, token):
        self.policy, self.token = policy, token
        self.started = time.monotonic()
        self.deadline = self.started + policy.run_timeout_seconds
        self.lock = asyncio.Lock()
        self.model_calls = self.tool_calls = 0
        self.usage = []
        self.before_dispatch = None

    def check(self):
        if self.token.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() >= self.deadline:
            raise RunFailure("DEADLINE_EXCEEDED", "Run deadline reached")

    async def reserve(self, kind, *, node_id=None, attempt=None, side_effect=False):
        async with self.lock:
            self.check()
            key = "model_calls" if kind == "model" else "tool_calls"
            limit = self.policy.max_model_calls if kind == "model" else self.policy.max_tool_calls
            if getattr(self, key) >= limit:
                raise RunFailure("CALL_BUDGET_EXHAUSTED", "Call budget exhausted")
            setattr(self, key, getattr(self, key) + 1)
            if self.before_dispatch is not None:
                self.before_dispatch(node_id, attempt, side_effect)

    def checkpoint(self):
        return {
            **self.clock(),
            "model_calls": self.model_calls, "tool_calls": self.tool_calls,
            "usage": self.usage,
        }

    def clock(self):
        now = time.monotonic()
        return {
            "elapsed_seconds": now - self.started,
            "remaining_seconds": max(0, self.deadline - now),
        }

    def restore(self, state):
        now = time.monotonic()
        elapsed = state["elapsed_seconds"]
        remaining = min(max(0, self.policy.run_timeout_seconds - elapsed), state["remaining_seconds"])
        self.started = now - elapsed
        self.deadline = now + remaining
        self.model_calls = state["model_calls"]
        self.tool_calls = state["tool_calls"]
        self.usage = state["usage"]

    def summary(self):
        result = {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "duration_seconds": time.monotonic() - self.started,
            "cost": None,
        }
        for key in ("input_tokens", "output_tokens"):
            known = [u.get(key) for u in self.usage]
            result[key] = (
                sum(x for x in known if x is not None)
                if (len(known) == self.model_calls and all(x is not None for x in known))
                else None
            )
            result["known_" + key] = sum(x for x in known if x is not None)
        result["unknown"] = [
            k for k in ("input_tokens", "output_tokens", "cost") if result[k] is None
        ]
        return result
