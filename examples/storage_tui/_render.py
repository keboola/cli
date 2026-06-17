"""Curses rendering helpers for the Keboola Storage Browser demo.

Pure presentation: every function here takes a curses window plus already-fetched
data and draws it. No API calls, no application state, no token handling happen in
this module -- that all lives in ``app.py``. Splitting the drawing out keeps
``app.py`` focused on navigation and SDK wiring while staying well under the
file-size budget the repo's style guide asks for.

All drawing is defensive about the terminal size: every write is clipped to the
window bounds so a small window never raises ``curses.error``.
"""

from __future__ import annotations

import contextlib
import curses
from collections.abc import Sequence

# Color-pair handles, initialised once by :func:`init_colors`. Kept as a small
# module-level registry (not magic numbers scattered through the draw calls).
PAIR_HEADER = 1
PAIR_FOOTER = 2
PAIR_SELECTED = 3
PAIR_DIM = 4
PAIR_TITLE = 5
PAIR_ERROR = 6


def init_colors() -> None:
    """Initialise the color pairs the renderer uses (no-op without color)."""
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PAIR_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(PAIR_FOOTER, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(PAIR_SELECTED, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(PAIR_DIM, curses.COLOR_CYAN, -1)
    curses.init_pair(PAIR_TITLE, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_ERROR, curses.COLOR_WHITE, curses.COLOR_RED)


def _pair(pair_id: int) -> int:
    """Return the attribute for a color pair, or 0 when color is unavailable."""
    if not curses.has_colors():
        return 0
    return curses.color_pair(pair_id)


def _fit(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` columns, adding an ellipsis when cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def _addstr(win: curses.window, row: int, col: int, text: str, attr: int = 0) -> None:
    """Write ``text`` at (row, col), clipped to the window, swallowing overflow.

    curses raises if you write the bottom-right cell or past the edge; for a
    read-only browser we would rather clip silently than crash the UI.
    """
    height, width = win.getmaxyx()
    if row < 0 or row >= height or col >= width:
        return
    visible = _fit(text, width - col)
    if not visible:
        return
    # Writing the bottom-right cell or past the edge raises curses.error; for a
    # read-only browser we would rather clip silently than crash the UI.
    with contextlib.suppress(curses.error):
        win.addstr(row, col, visible, attr)


def _draw_bar(win: curses.window, row: int, text: str, pair_id: int) -> None:
    """Draw a full-width colored bar (header/footer) on ``row``."""
    height, width = win.getmaxyx()
    if row < 0 or row >= height:
        return
    attr = _pair(pair_id)
    padded = text.ljust(width)[: max(width - 1, 0)]
    _addstr(win, row, 0, padded, attr)


def draw_header(win: curses.window, host: str, breadcrumb: str) -> None:
    """Draw the top bar: the project host (never the token) and breadcrumb."""
    left = f" Keboola Storage Browser  │  {host}"
    bar = left if not breadcrumb else f"{left}  │  {breadcrumb}"
    _draw_bar(win, 0, bar, PAIR_HEADER)


def draw_footer(win: curses.window, hints: str, status: str) -> None:
    """Draw the bottom two lines: an optional status line then the key hints."""
    height, _ = win.getmaxyx()
    if height < 2:
        return
    if status:
        _addstr(win, height - 2, 0, _fit(status, win.getmaxyx()[1] - 1), _pair(PAIR_ERROR))
    _draw_bar(win, height - 1, f" {hints}", PAIR_FOOTER)


def draw_loading(win: curses.window, message: str) -> None:
    """Centre a transient 'Loading...' style message in the body area."""
    height, width = win.getmaxyx()
    row = height // 2
    col = max((width - len(message)) // 2, 0)
    _addstr(win, row, col, message, _pair(PAIR_TITLE) | curses.A_BOLD)


def draw_list_pane(
    win: curses.window,
    *,
    title: str,
    rows: Sequence[str],
    selected: int,
    top: int,
    empty_message: str,
) -> None:
    """Draw a scrollable, single-select list filling the body area.

    Args:
        win: Target window (already cleared by the caller).
        title: Pane title drawn on the first body row.
        rows: Pre-formatted display strings, one per item.
        selected: Index of the highlighted row within ``rows``.
        top: Index of the first visible row (scroll offset).
        empty_message: Shown instead of rows when ``rows`` is empty.
    """
    height, width = win.getmaxyx()
    body_top = 1
    body_bottom = height - 2  # leave header (0) and footer (height-1, height-2)
    _addstr(win, body_top, 1, _fit(title, width - 2), _pair(PAIR_TITLE) | curses.A_BOLD)

    list_top = body_top + 1
    visible_rows = max(body_bottom - list_top, 0)
    if not rows:
        _addstr(win, list_top + 1, 2, empty_message, _pair(PAIR_DIM))
        return

    for offset in range(visible_rows):
        index = top + offset
        if index >= len(rows):
            break
        is_selected = index == selected
        marker = "▶ " if is_selected else "  "
        line = f"{marker}{rows[index]}"
        attr = _pair(PAIR_SELECTED) | curses.A_BOLD if is_selected else 0
        if is_selected:
            line = line.ljust(width - 2)
        _addstr(win, list_top + offset, 1, _fit(line, width - 2), attr)


def draw_table_detail(
    win: curses.window,
    *,
    title: str,
    info_lines: Sequence[str],
    columns: Sequence[str],
    preview_header: Sequence[str],
    preview_rows: Sequence[Sequence[str]],
) -> None:
    """Draw the table-detail view: metadata, column list, and a data preview grid.

    The preview grid is laid out with fixed-width columns sized to the header so
    it reads like a tiny spreadsheet; long values are individually truncated.
    """
    height, width = win.getmaxyx()
    row = 1
    _addstr(win, row, 1, _fit(title, width - 2), _pair(PAIR_TITLE) | curses.A_BOLD)
    row += 1

    for line in info_lines:
        if row >= height - 2:
            return
        _addstr(win, row, 2, _fit(line, width - 3), _pair(PAIR_DIM))
        row += 1

    if columns:
        if row >= height - 2:
            return
        _addstr(win, row, 2, _fit("Columns: " + ", ".join(columns), width - 3))
        row += 1

    row += 1  # spacer before the preview grid
    if not preview_header:
        if row < height - 2:
            _addstr(win, row, 2, "(no data preview available)", _pair(PAIR_DIM))
        return

    col_width = _preview_column_width(preview_header, width)
    _addstr(
        win, row, 2, _format_grid_row(preview_header, col_width), curses.A_BOLD | _pair(PAIR_TITLE)
    )
    row += 1
    for data_row in preview_rows:
        if row >= height - 2:
            break
        _addstr(win, row, 2, _format_grid_row(data_row, col_width))
        row += 1


def _preview_column_width(header: Sequence[str], term_width: int) -> int:
    """Pick a per-column width so the preview grid fits the terminal."""
    column_count = max(len(header), 1)
    usable = max(term_width - 4, column_count)
    return max(min(usable // column_count, 24), 6)


def _format_grid_row(values: Sequence[str], col_width: int) -> str:
    """Render one preview row as space-separated fixed-width cells."""
    cells = [_fit(str(value), col_width).ljust(col_width) for value in values]
    return " ".join(cells)
