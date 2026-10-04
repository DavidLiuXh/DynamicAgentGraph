"""Opt-in model regression for a normal information goal with all native tools available."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from dynamic_graph import DynamicGraphEngine, EngineConfig, ExecutionPolicy, GoalSpec, ModelBindings
from dynamic_graph.models.adapters import LangChainModelClient
from dynamic_graph.tools import (
    browser_open_local_page_tool,
    file_read_text_tool,
    file_write_text_tool,
    tavily_search_tool,
    web_fetch_tool,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("DYNAMIC_GRAPH_LIVE_TESTS") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
        reason="Opt-in paid model test; all inputs and tool handlers are synthetic",
    ),
]


@pytest.mark.parametrize("language", ["zh", "en"])
async def test_information_summary_uses_search_evidence_without_extra_actions(tmp_path, language):
    client = LangChainModelClient(model="deepseek-chat")
    engine = DynamicGraphEngine(
        config=EngineConfig(runs_dir=tmp_path, recording_mode="debug"),
        models=ModelBindings(client, client),
    )
    observed = []
    published = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    async def search(data, context):
        rows = [
            {
                "title": "合成北营地",
                "url": "https://example.com/synthetic-camp-north",
                "content": f"合成资料：杭州北营地，{published}公告开放，日票30元，允许帐篷露营。",
                "score": 0.9,
            },
            {
                "title": "合成南营地",
                "url": "https://example.com/synthetic-camp-south",
                "content": f"合成资料：杭州南营地，{published}公告开放，允许露营，费用未公布。",
                "score": 0.8,
            },
        ][: data.get("max_results", 5)]
        observed.extend(rows)
        return {"query": data["query"], "results": rows}

    async def unexpected_action(data, context):
        raise AssertionError(
            "This information goal does not require fetch, file, or browser actions"
        )

    search_tool = replace(tavily_search_tool(api_key="synthetic-test-key"), handler=search)
    tools = [search_tool] + [
        replace(tool, handler=unexpected_action)
        for tool in (
            web_fetch_tool(),
            file_read_text_tool(),
            file_write_text_tool(),
            browser_open_local_page_tool(),
        )
    ]
    for tool in tools:
        engine.register_tool(tool)
    policy = ExecutionPolicy(
        allowed_tools=[f"{tool.name}@{tool.version}" for tool in tools],
        allowed_side_effect_tools=[
            f"{tool.name}@{tool.version}" for tool in tools if not tool.read_only
        ],
    )
    goal = GoalSpec(
        objective=(
            "整理近1个月内最新的杭州露营资源，主要是营地清单，文字说明即可，"
            "列出检索到的相关营地及开放状态、收费、来源；未能核实的信息如实说明。"
            if language == "zh"
            else "Summarize Hangzhou camping resources updated within the last month, focusing on a "
            "campsite list in prose. Include relevant campsites found, availability, fees, and "
            "sources. Clearly explain information that could not be verified."
        ),
        context={"timezone": "Asia/Shanghai", "test_data": "Entirely synthetic model regression"},
    )
    result = await engine.run(goal=goal, policy=policy)
    assert result.execution_status == "COMPLETED", result.diagnostics
    assert result.output_complete and observed and result.outputs["answer"]
    assert result.usage["tool_calls"] >= 1
    delivered = json.dumps(result.outputs, ensure_ascii=False)
    for row in observed:
        assert row["url"] in delivered
    assert result.outputs["limitations"], (
        "Missing fees or unverifiable availability must be disclosed"
    )
