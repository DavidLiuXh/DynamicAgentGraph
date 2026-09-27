"""Shared descriptions of the three built-in reducers; no executable handlers."""

BUILTIN_REDUCER_CATALOG = {
    f"{name}@1.0.0": {
        "name": name,
        "version": "1.0.0",
        "kind": "reducer",
        "rule": rule,
        "parallel_safe": parallel_safe,
    }
    for name, rule, parallel_safe in (
        ("builtin.replace", "V=U, config={}, one writer", False),
        (
            "builtin.merge_by_key",
            "V=U object arrays, initial=[], config={key:required string property,on_conflict:error}, parallel safe",
            True,
        ),
        (
            "builtin.merge_map_strict",
            "V=U homogeneous dictionaries, initial={}, config={}, parallel safe",
            True,
        ),
    )
}
