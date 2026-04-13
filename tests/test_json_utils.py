"""Tests for keboola_agent_cli.json_utils (deep_merge, set_nested_value, compute_diff)."""

import pytest

from keboola_agent_cli.json_utils import (
    compute_diff,
    deep_merge,
    get_nested_value,
    set_nested_value,
)


class TestDeepMerge:
    """Tests for deep_merge()."""

    def test_flat_merge(self) -> None:
        target = {"a": 1, "b": 2}
        source = {"b": 3, "c": 4}
        result = deep_merge(target, source)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge_preserves_siblings(self) -> None:
        """The core scenario from keboola/mcp-server#468."""
        target = {
            "parameters": {
                "user": "admin",
                "project": "proj1",
                "tables": {"old": "data"},
                "dimensions": ["dim1"],
            }
        }
        source = {"parameters": {"tables": {"new": "data"}}}
        result = deep_merge(target, source)

        assert result["parameters"]["user"] == "admin"
        assert result["parameters"]["project"] == "proj1"
        assert result["parameters"]["dimensions"] == ["dim1"]
        # deep_merge merges dict+dict recursively, so both keys are present
        assert result["parameters"]["tables"] == {"old": "data", "new": "data"}

    def test_deeply_nested(self) -> None:
        target = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}
        source = {"a": {"b": {"c": 99}}}
        result = deep_merge(target, source)
        assert result == {"a": {"b": {"c": 99, "d": 2}, "e": 3}}

    def test_list_replaced_not_merged(self) -> None:
        target = {"items": [1, 2, 3]}
        source = {"items": [4, 5]}
        result = deep_merge(target, source)
        assert result["items"] == [4, 5]

    def test_new_keys_added(self) -> None:
        target = {"a": 1}
        source = {"b": {"c": 2}}
        result = deep_merge(target, source)
        assert result == {"a": 1, "b": {"c": 2}}

    def test_non_mutating(self) -> None:
        target = {"a": {"b": 1}}
        source = {"a": {"c": 2}}
        original_target = {"a": {"b": 1}}
        deep_merge(target, source)
        assert target == original_target

    def test_empty_source(self) -> None:
        target = {"a": 1}
        assert deep_merge(target, {}) == {"a": 1}

    def test_empty_target(self) -> None:
        source = {"a": 1}
        assert deep_merge({}, source) == {"a": 1}

    def test_scalar_overwrites_dict(self) -> None:
        target = {"a": {"b": 1}}
        source = {"a": "string"}
        result = deep_merge(target, source)
        assert result == {"a": "string"}

    def test_dict_overwrites_scalar(self) -> None:
        target = {"a": "string"}
        source = {"a": {"b": 1}}
        result = deep_merge(target, source)
        assert result == {"a": {"b": 1}}


class TestGetNestedValue:
    """Tests for get_nested_value()."""

    def test_simple_path(self) -> None:
        obj = {"a": {"b": 42}}
        assert get_nested_value(obj, "a.b") == 42

    def test_top_level(self) -> None:
        obj = {"key": "val"}
        assert get_nested_value(obj, "key") == "val"

    def test_list_index(self) -> None:
        obj = {"items": [10, 20, 30]}
        assert get_nested_value(obj, "items.1") == 20

    def test_missing_key_raises(self) -> None:
        with pytest.raises(KeyError):
            get_nested_value({"a": 1}, "b")


class TestSetNestedValue:
    """Tests for set_nested_value()."""

    def test_set_existing_key(self) -> None:
        obj = {"a": {"b": 1, "c": 2}}
        result = set_nested_value(obj, "a.b", 99)
        assert result == {"a": {"b": 99, "c": 2}}

    def test_create_intermediate_dicts(self) -> None:
        result = set_nested_value({}, "a.b.c", "new")
        assert result == {"a": {"b": {"c": "new"}}}

    def test_preserves_siblings(self) -> None:
        """Same scenario as MCP bug — set a nested key, siblings stay."""
        obj = {
            "parameters": {
                "user": "admin",
                "project": "p1",
                "tables": {"old": "data"},
            }
        }
        result = set_nested_value(obj, "parameters.tables", {"new": "data"})
        assert result["parameters"]["user"] == "admin"
        assert result["parameters"]["project"] == "p1"
        assert result["parameters"]["tables"] == {"new": "data"}

    def test_non_mutating(self) -> None:
        obj = {"a": {"b": 1}}
        set_nested_value(obj, "a.b", 2)
        assert obj == {"a": {"b": 1}}

    def test_set_list_element(self) -> None:
        obj = {"items": [10, 20, 30]}
        result = set_nested_value(obj, "items.1", 99)
        assert result == {"items": [10, 99, 30]}


class TestComputeDiff:
    """Tests for compute_diff()."""

    def test_no_changes(self) -> None:
        d = {"a": 1, "b": {"c": 2}}
        assert compute_diff(d, d) == []

    def test_value_change(self) -> None:
        old = {"host": "old.example.com"}
        new = {"host": "new.example.com"}
        changes = compute_diff(old, new)
        assert len(changes) == 1
        assert "host:" in changes[0]
        assert "old.example.com" in changes[0]
        assert "new.example.com" in changes[0]

    def test_added_key(self) -> None:
        old = {"a": 1}
        new = {"a": 1, "b": 2}
        changes = compute_diff(old, new)
        assert len(changes) == 1
        assert "(absent) -> 2" in changes[0]

    def test_removed_key(self) -> None:
        old = {"a": 1, "b": 2}
        new = {"a": 1}
        changes = compute_diff(old, new)
        assert len(changes) == 1
        assert "-> (absent)" in changes[0]

    def test_nested_changes(self) -> None:
        old = {"db": {"host": "old", "port": 5432}}
        new = {"db": {"host": "new", "port": 5432}}
        changes = compute_diff(old, new)
        assert len(changes) == 1
        assert "db.host:" in changes[0]
