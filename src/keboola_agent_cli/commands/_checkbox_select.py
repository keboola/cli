"""Generic inline arrow-key + spacebar checkbox picker for terminal commands.

Deliberately has no knowledge of projects, aliases, or any other domain
concept -- it takes a list of `CheckboxItem` labels and returns the indices
the user checked. This exists because the original project picker made users
*type* numbers and ranges ("1-3, 5"), which is worse UX than arrows + space
for no real benefit now that `prompt-toolkit>=3.0` is already a direct
dependency (see `commands/repl.py` for the other place this repo already
uses it).

Split into two halves on purpose:

- `CheckboxState` is a pure cursor/checked-set state machine with no I/O and
  no `prompt_toolkit` dependency at all, so its wraparound/toggle/invert
  logic is trivially unit-testable.
- `checkbox_select` wires that state machine to a `prompt_toolkit`
  `Application` for the actual terminal rendering and key handling.

`checkbox_select` takes `input`/`output` seams so it can be driven headlessly
in tests via `prompt_toolkit.input.create_pipe_input()` +
`prompt_toolkit.output.DummyOutput()`. Building the `Application` OUTSIDE an
app session captures the real stdin unless `input`/`output` are passed
directly into the constructor -- `create_app_session(input=...)` does NOT
redirect it. Do not "simplify" this by moving `input`/`output` into a
`with create_app_session(...)` block; that variant hangs forever under a
non-interactive test runner.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

# Deliberately kept under 80 columns. The previous single-line version was 103
# characters, which overflowed even a 100-column terminal: the `Window` below
# does not wrap by default, so prompt_toolkit emitted raw cursor-movement
# escapes mid-word and the hint line rendered as garbage. `wrap_lines=True` on
# the Window now makes an over-narrow terminal wrap cleanly instead of
# corrupting, but the short form means it normally never has to. `[i] invert`
# is left unadvertised on purpose to buy that margin -- the key stays bound.
_FOOTER = "\n[up/dn or j/k] move  [space] toggle  [a] all  [enter] ok  [q] cancel"

_STYLE = Style.from_dict({"cursor-line": "bold", "title": "bold", "footer": "italic"})


@dataclass(frozen=True)
class CheckboxItem:
    """One selectable row in the checkbox list.

    `hint` and `tag` are rendered as dim trailing text (e.g. the alias a
    project would register under, and "already registered"); they never
    carry meaning on their own -- state (checked or not) is always shown via
    the `[x]`/`[ ]` marker, never via color alone, so it survives a terminal
    that drops styling.
    """

    label: str
    hint: str = ""
    tag: str = ""


class CheckboxUnavailable(RuntimeError):
    """Raised when no usable interactive terminal is available for the picker.

    Callers are expected to catch this and fall back to a non-interactive
    (or simpler line-based) selection method -- it is not a bug, it is the
    documented escape hatch for a piped/non-TTY invocation.
    """


class CheckboxState:
    """Pure cursor + checked-set state machine. No I/O, no `prompt_toolkit`.

    Kept separate from `checkbox_select` so every transition (wraparound,
    toggle, select-all, invert) is unit-testable without a terminal.
    """

    def __init__(self, count: int, checked: Iterable[int] = ()) -> None:
        self._count = count
        self._cursor = 0
        self._checked: set[int] = {index for index in checked if 0 <= index < count}

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def checked(self) -> frozenset[int]:
        return frozenset(self._checked)

    def move(self, delta: int) -> None:
        """Move the cursor by `delta`, wrapping around. No-op when empty."""
        if self._count == 0:
            return
        self._cursor = (self._cursor + delta) % self._count

    def toggle(self) -> None:
        """Toggle the checked state of the row under the cursor."""
        if self._count == 0:
            return
        self._checked.symmetric_difference_update({self._cursor})

    def set_all(self, value: bool) -> None:
        """Check (`value=True`) or uncheck (`value=False`) every row."""
        self._checked = set(range(self._count)) if value else set()

    def invert(self) -> None:
        """Flip every row's checked state."""
        self._checked = set(range(self._count)) - self._checked

    def result(self) -> list[int]:
        """Checked indices, sorted ascending."""
        return sorted(self._checked)


def _stdio_is_tty() -> bool:
    return (
        hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )


def _render_lines(
    items: Sequence[CheckboxItem], state: CheckboxState, title: str
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    if title:
        lines.append(("class:title", f"{title}\n"))
    for index, item in enumerate(items):
        cursor_marker = ">" if index == state.cursor else " "
        check_marker = "x" if index in state.checked else " "
        text = f"{cursor_marker} [{check_marker}] {item.label}"
        if item.hint:
            text += f"  {item.hint}"
        if item.tag:
            text += f"  ({item.tag})"
        style = "class:cursor-line" if index == state.cursor else ""
        lines.append((style, text + "\n"))
    lines.append(("class:footer", _FOOTER))
    return lines


def checkbox_select(
    items: Sequence[CheckboxItem],
    *,
    title: str = "",
    preselected: Iterable[int] = (),
    input: Any = None,
    output: Any = None,
) -> list[int] | None:
    """Inline arrow-key/space checkbox over `items`.

    Returns the checked indices (sorted ascending), or `None` if the user
    cancelled (`ctrl-c` / `escape` / `q`) -- callers must treat both `None`
    and `[]` as "nothing selected", they exist as distinct outcomes only so
    a caller that cares about the distinction (logging, telemetry) can have
    it; this picker itself does not need to.

    Raises `CheckboxUnavailable` when neither `input` nor `output` is
    supplied and stdin/stdout is not a real terminal -- checked before any
    `Application` is constructed, so callers never hang on a piped stdin.

    `input`/`output` are test seams: pass `prompt_toolkit.input.create_pipe_input()`
    output and `prompt_toolkit.output.DummyOutput()` to drive this headlessly.
    """
    if input is None and output is None and not _stdio_is_tty():
        raise CheckboxUnavailable("No usable interactive terminal for the checkbox picker.")

    state = CheckboxState(len(items), checked=preselected)

    key_bindings = KeyBindings()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event: Any) -> None:
        state.move(-1)

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event: Any) -> None:
        state.move(1)

    @key_bindings.add(" ")
    def _toggle(event: Any) -> None:
        state.toggle()

    @key_bindings.add("a")
    def _toggle_all(event: Any) -> None:
        state.set_all(len(state.checked) < len(items))

    @key_bindings.add("i")
    def _invert(event: Any) -> None:
        state.invert()

    @key_bindings.add("enter")
    def _accept(event: Any) -> None:
        event.app.exit(result=state.result())

    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    @key_bindings.add("q")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    control = FormattedTextControl(lambda: _render_lines(items, state, title))
    application: Application[list[int] | None] = Application(
        # wrap_lines: a terminal narrower than the longest row must wrap
        # instead of letting prompt_toolkit emit raw cursor-movement escapes
        # mid-line (which renders as visible garbage). Project names are
        # user-supplied and unbounded, so this is not only about the footer.
        layout=Layout(Window(content=control, wrap_lines=True)),
        key_bindings=key_bindings,
        style=_STYLE,
        full_screen=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
    return application.run()
