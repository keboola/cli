"""Tests for `commands/_auth_picker.py`.

`TestParseSelection` / `TestParseAliasOverrides` cover the pure parsing
halves (`parse_selection`, `parse_alias_overrides`) -- no I/O involved.

`TestSelectIndices` and `TestRunProjectPicker` cover the interactive half
directly (not through `CliRunner`): `checkbox_select` is monkeypatched on the
`_auth_picker` module so these tests never touch a real terminal or
`prompt_toolkit`'s `Application` (that machinery is `_checkbox_select.py`'s
own responsibility, covered in tests/test_checkbox_select.py). `typer.prompt`
/ `typer.confirm` are monkeypatched the same way to drive the numeric
fallback and the "Edit aliases?" gate without a real stdin.

The full CLI wiring (argument parsing, exit codes, `--json` behavior) is
still exercised through `CliRunner(input=...)` in tests/test_cli_auth.py --
because `CliRunner`'s stdin is never a real TTY, those tests always exercise
the numeric-fallback branch of `_select_indices`, which is why the checkbox
branch needs its own coverage here.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from keboola_agent_cli.commands import _auth_picker
from keboola_agent_cli.commands._auth_picker import (
    _select_indices,
    parse_alias_overrides,
    parse_selection,
    run_project_picker,
)
from keboola_agent_cli.commands._checkbox_select import CheckboxUnavailable
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.services.auth_service import ProjectCandidate, ProjectSelection


def _candidate(**overrides: object) -> ProjectCandidate:
    defaults: dict[str, object] = {
        "project_id": 9840,
        "project_name": "Jirka BQ SOX",
        "role": "admin",
        "default_alias": "jirka-bq-sox",
        "existing_alias": "",
        "registered": False,
    }
    defaults.update(overrides)
    return ProjectCandidate(**defaults)  # type: ignore[arg-type]


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


class TestSelectIndices:
    """`_select_indices` -- the checkbox/numeric-fallback selection layer.

    `checkbox_select` is monkeypatched on the `_auth_picker` module (it is
    imported there by name, so patching the module attribute is what
    `_select_indices` actually calls) rather than driven for real -- the real
    thing is covered end-to-end in tests/test_checkbox_select.py.
    """

    def test_uses_checkbox_result_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: [1])
        candidates = [_candidate(project_id=9840), _candidate(project_id=1234)]
        assert _select_indices(Console(), candidates, {}) == [1]

    def test_preselects_every_unregistered_candidate_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[int]] = {}

        def fake_checkbox_select(items: object, *, title: str = "", preselected=()) -> list[int]:
            captured["preselected"] = list(preselected)
            return list(preselected)

        monkeypatch.setattr(_auth_picker, "checkbox_select", fake_checkbox_select)
        candidates = [
            _candidate(project_id=1, registered=False),
            _candidate(project_id=2, registered=True, existing_alias="already"),
            _candidate(project_id=3, registered=False),
        ]
        result = _select_indices(Console(), candidates, {})
        assert captured["preselected"] == [0, 2]
        assert result == [0, 2]

    def test_cancel_from_checkbox_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: None)
        result = _select_indices(Console(), [_candidate()], {})
        assert result == []

    def test_falls_back_to_numeric_prompt_when_checkbox_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_unavailable(*args: object, **kwargs: object) -> list[int]:
            raise CheckboxUnavailable("no usable terminal")

        monkeypatch.setattr(_auth_picker, "checkbox_select", raise_unavailable)
        monkeypatch.setattr(_auth_picker.typer, "prompt", lambda *args, **kwargs: "1")
        candidates = [_candidate(project_id=9840), _candidate(project_id=1234, project_name="Demo")]
        result = _select_indices(Console(), candidates, {})
        assert result == [0]


class TestRunProjectPicker:
    """`run_project_picker` end-to-end at the Python level (no `CliRunner`).

    `checkbox_select` and `typer.confirm`/`typer.prompt` are monkeypatched so
    these tests pin the "Edit aliases?" gate and the checkbox-driven default
    without needing a real terminal or piped stdin text.
    """

    def test_no_candidates_returns_empty_without_prompting(self) -> None:
        assert run_project_picker(Console(), []) == []

    def test_default_flow_takes_suggested_aliases_without_reprompting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bare-Enter-equivalent path: checkbox accepts the preselected set,
        "Edit aliases?" is declined -- every selection must carry its
        suggested alias, and `_prompt_alias` (typer.prompt) must never run.
        """
        candidates = [
            _candidate(project_id=9840, project_name="Jirka BQ SOX", default_alias="jirka-bq-sox"),
            _candidate(project_id=1234, project_name="Demo", default_alias="demo"),
        ]
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: [0, 1])
        monkeypatch.setattr(
            _auth_picker.typer, "confirm", lambda message, default=False: message != "Edit aliases?"
        )

        def fail_prompt(*args: object, **kwargs: object) -> str:
            raise AssertionError("alias prompt must not run when 'Edit aliases?' is declined")

        monkeypatch.setattr(_auth_picker.typer, "prompt", fail_prompt)

        selections = run_project_picker(Console(), candidates)
        assert {(s.project_id, s.alias) for s in selections} == {
            (9840, "jirka-bq-sox"),
            (1234, "demo"),
        }

    def test_preselects_every_unregistered_candidate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the checkbox's preselected set (every unregistered
        candidate) must survive into what actually gets registered when the
        user accepts it as-is -- this is the redesign's equivalent of the
        old numeric picker's "bare Enter registers everything" guarantee.
        """
        candidates = [
            _candidate(project_id=1, registered=False),
            _candidate(project_id=2, registered=True, existing_alias="already"),
            _candidate(project_id=3, registered=False),
        ]

        def fake_checkbox_select(items: object, *, title: str = "", preselected=()) -> list[int]:
            return list(preselected)

        monkeypatch.setattr(_auth_picker, "checkbox_select", fake_checkbox_select)
        monkeypatch.setattr(
            _auth_picker.typer, "confirm", lambda message, default=False: message != "Edit aliases?"
        )

        selections = run_project_picker(Console(), candidates)
        assert {s.project_id for s in selections} == {1, 3}

    def test_edit_aliases_reprompts_per_selected_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidates = [
            _candidate(project_id=9840, project_name="Jirka BQ SOX", default_alias="jirka-bq-sox")
        ]
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: [0])
        monkeypatch.setattr(_auth_picker.typer, "confirm", lambda message, default=False: True)
        monkeypatch.setattr(_auth_picker.typer, "prompt", lambda *args, **kwargs: "custom-alias")

        selections = run_project_picker(Console(), candidates)
        assert selections == [ProjectSelection(project_id=9840, alias="custom-alias")]

    def test_checkbox_cancel_registers_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: None)
        selections = run_project_picker(Console(), [_candidate()])
        assert selections == []

    def test_declining_final_confirmation_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: [0])
        monkeypatch.setattr(_auth_picker.typer, "confirm", lambda message, default=False: False)
        selections = run_project_picker(Console(), [_candidate()])
        assert selections == []

    def test_assume_yes_skips_final_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_auth_picker, "checkbox_select", lambda *args, **kwargs: [0])
        seen_messages: list[str] = []

        def fake_confirm(message: str, default: bool = False) -> bool:
            seen_messages.append(message)
            return False  # would decline everything if actually asked

        monkeypatch.setattr(_auth_picker.typer, "confirm", fake_confirm)
        selections = run_project_picker(Console(), [_candidate()], assume_yes=True)
        assert len(selections) == 1
        assert "Register these 1 project(s)?" not in seen_messages
