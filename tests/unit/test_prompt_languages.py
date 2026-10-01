import hashlib
from importlib.resources import files

import pytest

from dynamic_graph.planning.prompts import prompt_language, resources


@pytest.mark.parametrize(
    "objective, expected",
    [
        ("收集资料并生成报告", "zh"),
        ("彙整資料與來源", "zh"),
        ("Search LangGraph 文档", "zh"),
        ("查询𠮷字的来源", "zh"),
        ("Collect sources and generate a report", "en"),
        ("Résumer les sources", "en"),
        ("123 — 🔍", "en"),
    ],
)
def test_language_follows_objective(objective, expected):
    assert prompt_language(objective) == expected


def test_bilingual_resources_share_contracts_and_record_selected_hashes():
    zh_system, zh_repair, zh_schema, zh_versions = resources("zh")
    en_system, en_repair, en_schema, en_versions = resources("en")
    root = files("dynamic_graph.planning") / "prompts"
    assert zh_system == (root / "planner_system_v1.txt").read_text(encoding="utf-8")
    assert zh_repair == (root / "planner_repair_v1.txt").read_text(encoding="utf-8")
    assert zh_system != en_system and zh_repair != en_repair
    assert zh_schema == en_schema
    for system, repair, versions, language in (
        (zh_system, zh_repair, zh_versions, "zh"),
        (en_system, en_repair, en_versions, "en"),
    ):
        assert versions["prompt_language"] == language
        assert versions["prompt_hash"] == hashlib.sha256(system.encode()).hexdigest()
        assert versions["repair_hash"] == hashlib.sha256(repair.encode()).hexdigest()
    for key in ("prompt_version", "schema_hash", "dsl_version", "examples_hash"):
        assert zh_versions[key] == en_versions[key]


def test_unsupported_resource_language_is_rejected():
    with pytest.raises(ValueError, match="zh or en"):
        resources("fr")
