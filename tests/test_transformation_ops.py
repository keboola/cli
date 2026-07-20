"""Tests for the transformation block/code ops engine (issue #396).

Pure-engine tests for services/_transformation_ops.py: every op happy
path, unknown-ID errors (with valid IDs listed), ID renumbering after
structural ops, str_replace scoping/not-found cases, sequential batch
semantics, and raw<->simplified round trips through the SQL statement
splitter.
"""

import copy

import pytest

from keboola_agent_cli.services import _transformation_ops as tf_ops


def make_params() -> dict:
    """Simplified parameters with 2 blocks / 3 codes (script as SQL text)."""
    return {
        "blocks": [
            {
                "name": "First",
                "codes": [
                    {"name": "load", "script": "SELECT 1;\n\nSELECT 2;"},
                    {"name": "clean", "script": "DELETE FROM t;"},
                ],
            },
            {
                "name": "Second",
                "codes": [
                    {"name": "report", "script": 'CREATE TABLE "r" AS SELECT * FROM t;'},
                ],
            },
        ]
    }


def apply(params: dict, raw_ops: list[dict]) -> tf_ops.BatchResult:
    """Parse and apply raw op dicts in one go."""
    return tf_ops.apply_ops(params, tf_ops.parse_ops(raw_ops))


class TestParseOps:
    def test_parse_valid_ops(self) -> None:
        ops = tf_ops.parse_ops(
            [
                {"op": "add_block", "block": {"name": "B", "codes": []}},
                {"op": "remove_block", "block_id": "b0"},
                {"op": "str_replace", "search_for": "a", "replace_with": "b"},
            ]
        )
        assert [o.op for o in ops] == ["add_block", "remove_block", "str_replace"]
        assert ops[0].position == "end"  # default

    def test_parse_unknown_op(self) -> None:
        with pytest.raises(ValueError, match="Invalid operation"):
            tf_ops.parse_ops([{"op": "explode_block", "block_id": "b0"}])

    def test_parse_missing_field(self) -> None:
        with pytest.raises(ValueError, match="Invalid operation"):
            tf_ops.parse_ops([{"op": "rename_block", "block_id": "b0"}])

    def test_parse_str_replace_code_id_requires_block_id(self) -> None:
        with pytest.raises(ValueError, match="code_id must be None if block_id is None"):
            tf_ops.parse_ops(
                [{"op": "str_replace", "code_id": "b0.c0", "search_for": "a", "replace_with": "b"}]
            )

    def test_parse_invalid_position(self) -> None:
        with pytest.raises(ValueError, match="Invalid operation"):
            tf_ops.parse_ops(
                [{"op": "add_block", "block": {"name": "B", "codes": []}, "position": "middle"}]
            )


class TestAddIds:
    def test_ids_are_positional(self) -> None:
        params = make_params()
        tf_ops.add_ids(params)
        assert params["blocks"][0]["id"] == "b0"
        assert params["blocks"][1]["id"] == "b1"
        assert params["blocks"][0]["codes"][0]["id"] == "b0.c0"
        assert params["blocks"][0]["codes"][1]["id"] == "b0.c1"
        assert params["blocks"][1]["codes"][0]["id"] == "b1.c0"

    def test_empty_blocks_ok(self) -> None:
        assert tf_ops.add_ids({"blocks": []}) == {"blocks": []}


class TestAddBlock:
    def test_add_block_end(self) -> None:
        result = apply(
            make_params(),
            [
                {
                    "op": "add_block",
                    "block": {"name": "Third", "codes": [{"name": "c", "script": "SELECT 3;"}]},
                }
            ],
        )
        assert [b["name"] for b in result.params["blocks"]] == ["First", "Second", "Third"]
        assert result.params["blocks"][2]["id"] == "b2"
        assert result.params["blocks"][2]["codes"][0]["id"] == "b2.c0"
        assert result.messages == ['Added block with name "Third"']
        assert result.structural is True

    def test_add_block_start_renumbers(self) -> None:
        result = apply(
            make_params(),
            [{"op": "add_block", "block": {"name": "Zero", "codes": []}, "position": "start"}],
        )
        assert [b["name"] for b in result.params["blocks"]] == ["Zero", "First", "Second"]
        # IDs re-derived: the new block is b0, the old b0 became b1.
        assert result.params["blocks"][0]["id"] == "b0"
        assert result.params["blocks"][1]["id"] == "b1"
        assert result.params["blocks"][1]["codes"][0]["id"] == "b1.c0"

    def test_add_block_empty_name(self) -> None:
        with pytest.raises(ValueError, match="block name cannot be empty"):
            apply(make_params(), [{"op": "add_block", "block": {"name": "   ", "codes": []}}])


class TestRemoveBlock:
    def test_remove_block_renumbers(self) -> None:
        result = apply(make_params(), [{"op": "remove_block", "block_id": "b0"}])
        assert [b["name"] for b in result.params["blocks"]] == ["Second"]
        # Former b1 is now b0 (IDs re-derived after structural change).
        assert result.params["blocks"][0]["id"] == "b0"
        assert result.params["blocks"][0]["codes"][0]["id"] == "b0.c0"
        assert result.structural is True

    def test_remove_block_unknown_id_lists_valid(self) -> None:
        with pytest.raises(
            ValueError, match=r"Block with id 'b9' does not exist. Valid block ids: b0, b1"
        ):
            apply(make_params(), [{"op": "remove_block", "block_id": "b9"}])


class TestRenameBlock:
    def test_rename_block(self) -> None:
        result = apply(
            make_params(), [{"op": "rename_block", "block_id": "b1", "block_name": "Renamed"}]
        )
        assert result.params["blocks"][1]["name"] == "Renamed"
        assert result.structural is False

    def test_rename_block_empty_name(self) -> None:
        with pytest.raises(ValueError, match="block name cannot be empty"):
            apply(make_params(), [{"op": "rename_block", "block_id": "b0", "block_name": " "}])

    def test_rename_block_unknown_id(self) -> None:
        with pytest.raises(ValueError, match="Block with id 'b7' does not exist"):
            apply(make_params(), [{"op": "rename_block", "block_id": "b7", "block_name": "X"}])


class TestAddCode:
    def test_add_code_end(self) -> None:
        result = apply(
            make_params(),
            [
                {
                    "op": "add_code",
                    "block_id": "b1",
                    "code": {"name": "extra", "script": "SELECT 9;"},
                }
            ],
        )
        codes = result.params["blocks"][1]["codes"]
        assert [c["name"] for c in codes] == ["report", "extra"]
        assert codes[1]["id"] == "b1.c1"
        assert result.messages == ['Added code with name "extra"']
        assert result.structural is True

    def test_add_code_start_renumbers(self) -> None:
        result = apply(
            make_params(),
            [
                {
                    "op": "add_code",
                    "block_id": "b0",
                    "code": {"name": "init", "script": "SET x = 1;"},
                    "position": "start",
                }
            ],
        )
        codes = result.params["blocks"][0]["codes"]
        assert [c["name"] for c in codes] == ["init", "load", "clean"]
        assert [c["id"] for c in codes] == ["b0.c0", "b0.c1", "b0.c2"]

    def test_add_code_unknown_block(self) -> None:
        with pytest.raises(ValueError, match="Block with id 'b5' does not exist"):
            apply(
                make_params(),
                [
                    {
                        "op": "add_code",
                        "block_id": "b5",
                        "code": {"name": "x", "script": "SELECT 1;"},
                    }
                ],
            )

    def test_add_code_empty_name(self) -> None:
        with pytest.raises(ValueError, match="code name cannot be empty"):
            apply(
                make_params(),
                [{"op": "add_code", "block_id": "b0", "code": {"name": "", "script": "SELECT 1;"}}],
            )


class TestRemoveCode:
    def test_remove_code_renumbers(self) -> None:
        result = apply(make_params(), [{"op": "remove_code", "block_id": "b0", "code_id": "b0.c0"}])
        codes = result.params["blocks"][0]["codes"]
        assert [c["name"] for c in codes] == ["clean"]
        # Former b0.c1 renumbered to b0.c0.
        assert codes[0]["id"] == "b0.c0"

    def test_remove_code_unknown_id_lists_valid(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"Code with id 'b0.c9' in block 'b0' does not exist. Valid code ids: b0.c0, b0.c1",
        ):
            apply(make_params(), [{"op": "remove_code", "block_id": "b0", "code_id": "b0.c9"}])


class TestRenameCode:
    def test_rename_code(self) -> None:
        result = apply(
            make_params(),
            [{"op": "rename_code", "block_id": "b1", "code_id": "b1.c0", "code_name": "summary"}],
        )
        assert result.params["blocks"][1]["codes"][0]["name"] == "summary"

    def test_rename_code_empty_name(self) -> None:
        with pytest.raises(ValueError, match="code name cannot be empty"):
            apply(
                make_params(),
                [{"op": "rename_code", "block_id": "b0", "code_id": "b0.c0", "code_name": ""}],
            )


class TestSetCode:
    def test_set_code(self) -> None:
        result = apply(
            make_params(),
            [
                {
                    "op": "set_code",
                    "block_id": "b0",
                    "code_id": "b0.c1",
                    "script": "TRUNCATE TABLE t;",
                }
            ],
        )
        assert result.params["blocks"][0]["codes"][1]["script"] == "TRUNCATE TABLE t;"
        assert result.messages == ["Changed code with id 'b0.c1' in block 'b0'"]

    def test_set_code_empty_script(self) -> None:
        with pytest.raises(ValueError, match="script cannot be empty"):
            apply(
                make_params(),
                [{"op": "set_code", "block_id": "b0", "code_id": "b0.c0", "script": "  "}],
            )

    def test_set_code_unknown_code(self) -> None:
        with pytest.raises(ValueError, match=r"Code with id 'b1\.c4' in block 'b1' does not exist"):
            apply(
                make_params(),
                [{"op": "set_code", "block_id": "b1", "code_id": "b1.c4", "script": "SELECT 1;"}],
            )


class TestAddScript:
    def test_add_script_end_joins_with_space(self) -> None:
        result = apply(
            make_params(),
            [{"op": "add_script", "block_id": "b0", "code_id": "b0.c1", "script": "SELECT 3;"}],
        )
        assert result.params["blocks"][0]["codes"][1]["script"] == "DELETE FROM t; SELECT 3;"

    def test_add_script_start(self) -> None:
        result = apply(
            make_params(),
            [
                {
                    "op": "add_script",
                    "block_id": "b0",
                    "code_id": "b0.c1",
                    "script": "SET x = 1;",
                    "position": "start",
                }
            ],
        )
        assert result.params["blocks"][0]["codes"][1]["script"] == "SET x = 1; DELETE FROM t;"

    def test_add_script_to_empty_script(self) -> None:
        params = make_params()
        params["blocks"][0]["codes"][0]["script"] = ""
        result = apply(
            params,
            [{"op": "add_script", "block_id": "b0", "code_id": "b0.c0", "script": "SELECT 1;"}],
        )
        assert result.params["blocks"][0]["codes"][0]["script"] == "SELECT 1;"

    def test_add_script_empty_script(self) -> None:
        with pytest.raises(ValueError, match="script cannot be empty"):
            apply(
                make_params(),
                [{"op": "add_script", "block_id": "b0", "code_id": "b0.c0", "script": " "}],
            )


class TestStrReplace:
    def test_replace_whole_transformation_counts_occurrences(self) -> None:
        params = make_params()
        result = apply(
            params, [{"op": "str_replace", "search_for": "SELECT", "replace_with": "select"}]
        )
        # "SELECT 1;\n\nSELECT 2;" has 2 + "CREATE TABLE ... SELECT" has 1.
        assert result.messages == ['Replaced 3 occurrences of "SELECT" in the transformation']
        assert result.params["blocks"][0]["codes"][0]["script"] == "select 1;\n\nselect 2;"

    def test_replace_block_scope(self) -> None:
        result = apply(
            make_params(),
            [{"op": "str_replace", "block_id": "b0", "search_for": "t;", "replace_with": "t2;"}],
        )
        assert result.params["blocks"][0]["codes"][1]["script"] == "DELETE FROM t2;"
        # Block b1 untouched.
        assert "FROM t;" in result.params["blocks"][1]["codes"][0]["script"]
        assert result.messages == ['Replaced 1 occurrence of "t;" in block "b0"']

    def test_replace_code_scope(self) -> None:
        result = apply(
            make_params(),
            [
                {
                    "op": "str_replace",
                    "block_id": "b0",
                    "code_id": "b0.c0",
                    "search_for": "2",
                    "replace_with": "22",
                }
            ],
        )
        assert result.params["blocks"][0]["codes"][0]["script"] == "SELECT 1;\n\nSELECT 22;"
        assert result.messages == ['Replaced 1 occurrence of "2" in code "b0.c0", block "b0"']

    def test_replace_not_found(self) -> None:
        with pytest.raises(
            ValueError, match='Search string "nonexistent" not found in the transformation'
        ):
            apply(
                make_params(),
                [{"op": "str_replace", "search_for": "nonexistent", "replace_with": "x"}],
            )

    def test_replace_empty_search(self) -> None:
        with pytest.raises(ValueError, match="search string is empty"):
            apply(make_params(), [{"op": "str_replace", "search_for": "", "replace_with": "x"}])

    def test_replace_search_equals_replace(self) -> None:
        with pytest.raises(ValueError, match="search string and replace string are the same"):
            apply(
                make_params(),
                [{"op": "str_replace", "search_for": "same", "replace_with": "same"}],
            )

    def test_replace_unknown_block_id(self) -> None:
        with pytest.raises(ValueError, match="Block with id 'b8' does not exist"):
            apply(
                make_params(),
                [{"op": "str_replace", "block_id": "b8", "search_for": "a", "replace_with": "b"}],
            )

    def test_replace_block_without_codes(self) -> None:
        params = {"blocks": [{"name": "Empty", "codes": []}]}
        with pytest.raises(ValueError, match='No scripts found in block "b0"'):
            apply(
                params,
                [{"op": "str_replace", "block_id": "b0", "search_for": "a", "replace_with": "b"}],
            )


class TestBatchSemantics:
    def test_ops_apply_sequentially(self) -> None:
        """A later op sees the effect of an earlier op in the same batch."""
        result = apply(
            make_params(),
            [
                {"op": "set_code", "block_id": "b0", "code_id": "b0.c0", "script": "SELECT 99;"},
                {"op": "str_replace", "search_for": "99", "replace_with": "100"},
            ],
        )
        assert result.params["blocks"][0]["codes"][0]["script"] == "SELECT 100;"

    def test_mid_batch_added_block_not_addressable(self) -> None:
        """Elements added within a batch carry no ID until the next batch."""
        with pytest.raises(ValueError, match="Block with id 'b2' does not exist"):
            apply(
                make_params(),
                [
                    {"op": "add_block", "block": {"name": "New", "codes": []}},
                    {"op": "rename_block", "block_id": "b2", "block_name": "Oops"},
                ],
            )

    def test_ids_stick_to_elements_within_batch(self) -> None:
        """After removing b0 mid-batch, 'b1' still addresses the original b1."""
        result = apply(
            make_params(),
            [
                {"op": "remove_block", "block_id": "b0"},
                {"op": "rename_block", "block_id": "b1", "block_name": "Still Second"},
            ],
        )
        assert result.params["blocks"][0]["name"] == "Still Second"
        assert result.params["blocks"][0]["id"] == "b0"  # re-derived at batch end

    def test_input_not_mutated(self) -> None:
        params = make_params()
        original = copy.deepcopy(params)
        apply(params, [{"op": "rename_block", "block_id": "b0", "block_name": "Changed"}])
        assert params == original

    def test_structural_flag_false_for_content_ops(self) -> None:
        result = apply(
            make_params(),
            [{"op": "rename_block", "block_id": "b0", "block_name": "X"}],
        )
        assert result.structural is False

    def test_missing_blocks_key_defaults_to_empty(self) -> None:
        result = apply({}, [{"op": "add_block", "block": {"name": "B", "codes": []}}])
        assert [b["name"] for b in result.params["blocks"]] == ["B"]


class TestRoundTrip:
    def test_raw_to_simplified_joins_statements(self) -> None:
        raw = {
            "blocks": [
                {"name": "B", "codes": [{"name": "c", "script": ["SELECT 1;", "SELECT 2;"]}]}
            ]
        }
        simplified = tf_ops.raw_to_simplified(raw)
        assert simplified["blocks"][0]["codes"][0]["script"] == "SELECT 1;\n\nSELECT 2;"

    def test_raw_to_simplified_keeps_string_script(self) -> None:
        raw = {"blocks": [{"name": "B", "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}]}]}
        simplified = tf_ops.raw_to_simplified(raw)
        assert simplified["blocks"][0]["codes"][0]["script"] == "SELECT 1; SELECT 2;"

    def test_simplified_to_raw_splits_statements(self) -> None:
        simplified = {
            "blocks": [
                {
                    "id": "b0",
                    "name": "B",
                    "codes": [{"id": "b0.c0", "name": "c", "script": "SELECT 1;\nSELECT 'a;b';"}],
                }
            ]
        }
        raw = tf_ops.simplified_to_raw(simplified)
        # Statement split respects string literals; synthetic ids stripped.
        assert raw["blocks"][0]["codes"][0]["script"] == ["SELECT 1;", "SELECT 'a;b';"]
        assert "id" not in raw["blocks"][0]
        assert "id" not in raw["blocks"][0]["codes"][0]

    def test_full_round_trip_preserves_statements(self) -> None:
        raw = {
            "blocks": [
                {
                    "name": "B",
                    "codes": [
                        {"name": "c", "script": ["SELECT 1;", "-- note\nSELECT 2;"]},
                    ],
                }
            ]
        }
        round_tripped = tf_ops.simplified_to_raw(tf_ops.raw_to_simplified(raw))
        assert round_tripped["blocks"][0]["codes"][0]["script"] == [
            "SELECT 1;",
            "-- note\nSELECT 2;",
        ]

    def test_edit_pipeline_split_integration(self) -> None:
        """set_code with multi-statement SQL lands as multiple raw statements."""
        result = apply(
            make_params(),
            [
                {
                    "op": "set_code",
                    "block_id": "b0",
                    "code_id": "b0.c0",
                    "script": "CREATE TABLE x AS SELECT 1; INSERT INTO x VALUES (2);",
                }
            ],
        )
        raw = tf_ops.simplified_to_raw(result.params)
        assert raw["blocks"][0]["codes"][0]["script"] == [
            "CREATE TABLE x AS SELECT 1;",
            "INSERT INTO x VALUES (2);",
        ]
