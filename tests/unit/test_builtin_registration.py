import pytest

from dynamic_graph import DynamicGraphEngine, FakeModelClient, ModelBindings
from dynamic_graph.contracts import ConfigurationError, RegistrationError
from dynamic_graph.tools import tavily_search_tool

KEY = "tvly-" + "testcredential" * 3


def engine():
    fake = FakeModelClient([])
    return DynamicGraphEngine(models=ModelBindings(fake, fake))


def test_register_all_packaged_capabilities_is_idempotent(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    instance = engine()
    reducers = instance.list_capabilities("reducer")
    instance.register_builtin_capabilities()
    assert {(info.name, info.version, info.kind) for info in instance.list_capabilities()} == {
        ("builtin.replace", "1.0.0", "reducer"),
        ("builtin.merge_by_key", "1.0.0", "reducer"),
        ("builtin.merge_map_strict", "1.0.0", "reducer"),
        ("tavily.search", "1.0.0", "tool"),
    }
    catalog = [info.to_dict() for info in instance.list_capabilities()]
    monkeypatch.delenv("TAVILY_API_KEY")
    instance.register_builtin_capabilities()
    assert [info.to_dict() for info in instance.list_capabilities()] == catalog
    assert instance.list_capabilities("reducer") == reducers
    assert instance.list_capabilities("check") == ()
    assert KEY not in repr(catalog)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_configuration_leaves_catalog_unchanged(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TAVILY_API_KEY", key)
    instance = engine()
    before = instance.list_capabilities()
    with pytest.raises(ConfigurationError, match="TAVILY_API_KEY"):
        instance.register_builtin_capabilities()
    assert instance.list_capabilities() == before
    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    instance.register_builtin_capabilities()
    assert instance.get_capability("tavily.search", "1.0.0") is not None


def test_existing_registration_is_preserved_and_other_instances_are_isolated(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    instance = engine()
    tool = tavily_search_tool(api_key=KEY)
    instance.register_tool(tool)
    before = instance.list_capabilities()
    instance.register_builtin_capabilities()
    assert instance.list_capabilities() == before
    assert engine().get_capability("tavily.search", "1.0.0") is None
    # The new convenience API does not relax the existing explicit registration contract.
    with pytest.raises(RegistrationError, match="already registered"):
        instance.register_tool(tool)
