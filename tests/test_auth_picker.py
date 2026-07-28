"""Tests for `commands/_auth_picker.py` -- the pure parsing halves of the
interactive project picker (`parse_selection`, `parse_alias_overrides`).

`run_project_picker` itself (the interactive half) is exercised indirectly
through `CliRunner(input=...)` in tests/test_cli_auth.py -- it needs a real
`ProjectCandidate` list and `typer.prompt`/`typer.confirm` wiring that only
make sense behind the full CLI invocation.
"""

from __future__ import annotations

import pytest

from keboola_agent_cli.commands._auth_picker import parse_alias_overrides, parse_selection
from keboola_agent_cli.errors import ConfigError


class TestParseSelection:
    def test_empty_string_selects_nothing(self) -> None:
        assert parse_selection("", 5) == []

    def test_none_selects_nothing(self) -> None:
        assert parse_selection("none", 5) == []
        assert parse_selection("NONE", 5) == []
        assert parse_selection("  none  ", 5) == []

    def test_all_selects_every_index(self) -> None:
        assert parse_selection("all", 3) == [0, 1, 2]
        assert parse_selection("ALL", 3) == [0, 1, 2]

    def test_star_selects_every_index(self) -> None:
        assert parse_selection("*", 3) == [0, 1, 2]

    def test_single_number(self) -> None:
        assert parse_selection("1", 5) == [0]

    def test_comma_separated(self) -> None:
        assert parse_selection("1,3", 5) == [0, 2]

    def test_space_separated(self) -> None:
        assert parse_selection("1 3", 5) == [0, 2]

    def test_inclusive_range(self) -> None:
        assert parse_selection("1-3", 5) == [0, 1, 2]

    def test_mixed_ranges_and_singles(self) -> None:
        assert parse_selection("1-3, 5 7", 8) == [0, 1, 2, 4, 6]

    def test_dedup(self) -> None:
        assert parse_selection("1,1,2,1-2", 5) == [0, 1]

    def test_ascending_order_regardless_of_input_order(self) -> None:
        assert parse_selection("5,1,3", 5) == [0, 2, 4]

    def test_out_of_range_high_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            parse_selection("6", 5)

    def test_out_of_range_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            parse_selection("0", 5)

    def test_reversed_range_raises(self) -> None:
        with pytest.raises(ValueError, match="start must not be greater than end"):
            parse_selection("3-1", 5)

    def test_garbage_token_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_selection("abc", 5)

    def test_garbage_range_token_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_selection("1-abc", 5)

    def test_partial_range_missing_side_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_selection("1-", 5)


class TestParseAliasOverrides:
    def test_happy_path(self) -> None:
        result = parse_alias_overrides(["9840=jirka-bq-sox", "1234=demo"])
        assert result == {9840: "jirka-bq-sox", 1234: "demo"}

    def test_empty_sequence(self) -> None:
        assert parse_alias_overrides([]) == {}

    def test_missing_equals_raises(self) -> None:
        with pytest.raises(ConfigError, match="expected ID=ALIAS"):
            parse_alias_overrides(["9840"])

    def test_non_integer_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="not a valid project id"):
            parse_alias_overrides(["abc=demo"])

    def test_duplicate_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate --alias"):
            parse_alias_overrides(["9840=demo", "9840=other"])

    def test_invalid_alias_format_raises(self) -> None:
        with pytest.raises(ConfigError):
            parse_alias_overrides(["9840=bad alias with spaces"])

    def test_invalid_alias_path_traversal_raises(self) -> None:
        with pytest.raises(ConfigError):
            parse_alias_overrides(["9840=../etc"])
