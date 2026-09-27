"""G01: independent experiments against the installed LangGraph runtime."""

import asyncio
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph


def test_dynamic_cell_initialization_and_distinct_update_type():
    calls = []

    def merge(previous, incoming):
        if incoming["kind"] == "init":
            assert previous == {}
            return {"kind": "value", "value": incoming["value"]}
        assert previous["kind"] == "value"
        key, value = incoming["value"]
        calls.append(key)
        return {"kind": "value", "value": {**previous["value"], key: value}}

    state = TypedDict("DynamicState", {"index": Annotated[dict, merge]})
    builder = StateGraph(state)
    builder.add_node("a", lambda _: {"index": {"kind": "update", "value": ["a", 1]}})
    builder.add_edge(START, "a")
    builder.add_edge("a", END)
    result = builder.compile().invoke({"index": {"kind": "init", "value": {}}})
    assert result["index"]["value"] == {"a": 1}
    assert calls == ["a"]


@pytest.mark.parametrize("list_form, expected", [(True, 1), (False, 2)])
async def test_unequal_branches_all_join_vs_independent_edges(list_form, expected):
    class State(TypedDict):
        a: int
        b: int

    calls = []
    builder = StateGraph(State)
    builder.add_node("short", lambda _: {"a": 1})
    builder.add_node("long1", lambda _: {})
    builder.add_node("long2", lambda _: {"b": 2})

    async def join(state):
        calls.append(dict(state))
        return {}

    builder.add_node("join", join)
    builder.add_edge(START, "short")
    builder.add_edge(START, "long1")
    builder.add_edge("long1", "long2")
    if list_form:
        builder.add_edge(["short", "long2"], "join")
    else:
        builder.add_edge("short", "join")
        builder.add_edge("long2", "join")
    builder.add_edge("join", END)
    await builder.compile().ainvoke({"a": 0, "b": 0})
    assert len(calls) == expected
    assert calls[-1] == {"a": 1, "b": 2}


async def test_returned_is_not_committed_on_parallel_failure():
    class State(TypedDict):
        good: int
        bad: int

    returned = asyncio.Event()

    async def good(_):
        returned.set()
        return {"good": 1}

    async def bad(_):
        await returned.wait()
        raise RuntimeError("injected failure")

    builder = StateGraph(State)
    builder.add_node("good_node", good)
    builder.add_node("bad_node", bad)
    builder.add_edge(START, "good_node")
    builder.add_edge(START, "bad_node")
    builder.add_edge(["good_node", "bad_node"], END)
    values = []
    with pytest.raises(RuntimeError, match="injected failure"):
        async for value in builder.compile().astream({"good": 0, "bad": 0}, stream_mode="values"):
            values.append(value)
    assert returned.is_set()
    assert values == [{"good": 0, "bad": 0}]


@pytest.mark.parametrize("cancel", [False, True])
async def test_runtime_concurrency_and_cancellation(cancel):
    class State(TypedDict):
        value: int

    active = peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def work(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
            return {}
        finally:
            active -= 1

    builder = StateGraph(State)
    for index in range(5):
        name = f"n{index}"
        builder.add_node(name, work)
        builder.add_edge(START, name)
        builder.add_edge(name, END)
    task = asyncio.create_task(
        builder.compile().ainvoke({"value": 0}, {} if cancel else {"max_concurrency": 2})
    )
    await entered.wait()
    await asyncio.sleep(0.05)
    assert peak == (5 if cancel else 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await task
    assert active == 0
