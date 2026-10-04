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
            "V=U object arrays, initial=[], config={key:required string property,on_conflict:error}, parallel safe. "
            "The key identifies an item: repeated keys must have identical whole objects, both within one update "
            "and across updates; different objects sharing a key fail. Use replace for a single writer's complete list "
            "when items need not have unique identities.",
            True,
        ),
        (
            "builtin.merge_map_strict",
            "V=U homogeneous dictionaries, initial={}, config={}, parallel safe. "
            "Repeated keys must have identical values; conflicting values fail.",
            True,
        ),
    )
}
