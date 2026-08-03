"""Tests for `commands/_checkbox_select.py` -- the generic arrow-key +
spacebar checkbox picker.

Split to mirror the module: `TestCheckboxState` exercises the pure state
machine with no I/O at all, `TestCheckboxSelect` drives the real
`prompt_toolkit`-backed `checkbox_select` headlessly.

Every `checkbox_select` test MUST pass `input`/`output` through
`create_pipe_input()` / `DummyOutput()` directly into `checkbox_select`'s
seam. Building the underlying `Application` any other way (e.g. wrapping the
call in `create_app_session(input=...)`) captures the real stdin instead and
hangs forever under a non-interactive test runner -- if a test here ever
hangs, that is the first thing to check, not a flaky timeout.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from keboola_agent_cli.commands import _checkbox_select as checkbox_select_module
from keboola_agent_cli.commands._checkbox_select import (
    CheckboxItem,
    CheckboxState,
    CheckboxUnavailable,
    checkbox_select,
)


class TestCheckboxState:
    def test_starts_at_cursor_zero_nothing_checked(self) -> None:
        state = CheckboxState(3)
        assert state.cursor == 0
        assert state.checked == frozenset()

    def test_preselected_honoured(self) -> None:
        state = CheckboxState(3, checked=[0, 2])
        assert state.checked == frozenset({0, 2})

    def test_preselected_out_of_range_is_ignored(self) -> None:
        state = CheckboxState(3, checked=[0, 5, -1])
        assert state.checked == frozenset({0})

    def test_move_down_wraps_around(self) -> None:
        state = CheckboxState(3)
        state.move(1)
        state.move(1)
        state.move(1)
        assert state.cursor == 0

    def test_move_up_wraps_around(self) -> None:
        state = CheckboxState(3)
        state.move(-1)
        assert state.cursor == 2

    def test_move_on_empty_is_noop(self) -> None:
        state = CheckboxState(0)
        state.move(1)
        assert state.cursor == 0

    def test_toggle_on_empty_is_noop(self) -> None:
        state = CheckboxState(0)
        state.toggle()
        assert state.checked == frozenset()

    def test_toggle_checks_then_unchecks(self) -> None:
        state = CheckboxState(3)
        state.toggle()
        assert state.checked == frozenset({0})
        state.toggle()
        assert state.checked == frozenset()

    def test_toggle_only_affects_row_under_cursor(self) -> None:
        state = CheckboxState(3)
        state.move(1)
        state.toggle()
        assert state.checked == frozenset({1})
        assert state.cursor == 1

    def test_set_all_true(self) -> None:
        state = CheckboxState(3)
        state.set_all(True)
        assert state.checked == frozenset({0, 1, 2})

    def test_set_all_false(self) -> None:
        state = CheckboxState(3, checked=[0, 1, 2])
        state.set_all(False)
        assert state.checked == frozenset()

    def test_set_all_on_empty_is_noop(self) -> None:
        state = CheckboxState(0)
        state.set_all(True)
        assert state.checked == frozenset()

    def test_invert(self) -> None:
        state = CheckboxState(3, checked=[0])
        state.invert()
        assert state.checked == frozenset({1, 2})

    def test_invert_twice_is_identity(self) -> None:
        state = CheckboxState(4, checked=[1, 3])
        state.invert()
        state.invert()
        assert state.checked == frozenset({1, 3})

    def test_result_sorted_ascending_regardless_of_toggle_order(self) -> None:
        state = CheckboxState(5)
        for index in (3, 0, 4):
            state.move(index - state.cursor)
            state.toggle()
        assert state.result() == [0, 3, 4]

    def test_result_empty_when_count_zero(self) -> None:
        assert CheckboxState(0).result() == []


def _items(count: int) -> list[CheckboxItem]:
    return [CheckboxItem(label=f"item{i}") for i in range(count)]


class TestCheckboxSelect:
    def test_move_toggle_toggle_accept(self) -> None:
        # space (toggle 0), down, space (toggle 1), enter.
        with create_pipe_input() as pipe_input:
            pipe_input.send_text(" \x1b[B \r")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result == [0, 1]

    def test_bare_enter_accepts_preselected(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\r")
            result = checkbox_select(
                _items(3), preselected=[0, 2], input=pipe_input, output=DummyOutput()
            )
        assert result == [0, 2]

    def test_toggle_all_key_checks_everything(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("a\r")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result == [0, 1, 2]

    def test_toggle_all_twice_unchecks_everything(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("aa\r")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result == []

    def test_invert_from_none_checks_everything(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("i\r")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result == [0, 1, 2]

    def test_vi_style_jk_movement(self) -> None:
        # j (down), space (toggle 1), enter.
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("j \r")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result == [1]

    def test_cancel_via_ctrl_c_returns_none(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\x03")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result is None

    def test_cancel_via_q_returns_none(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("q")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result is None

    def test_cancel_ignores_any_prior_toggles(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text(" \x03")
            result = checkbox_select(_items(3), input=pipe_input, output=DummyOutput())
        assert result is None

    def test_empty_items_accept_immediately(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\r")
            result = checkbox_select([], input=pipe_input, output=DummyOutput())
        assert result == []

    def test_raises_checkbox_unavailable_without_tty_or_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _NonTtyStream:
            def isatty(self) -> bool:
                return False

        monkeypatch.setattr(checkbox_select_module.sys, "stdin", _NonTtyStream())
        monkeypatch.setattr(checkbox_select_module.sys, "stdout", _NonTtyStream())
        with pytest.raises(CheckboxUnavailable):
            checkbox_select(_items(3))
