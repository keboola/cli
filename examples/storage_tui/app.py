"""Keboola Storage Browser -- a curses TUI demo for the in-process kbagent SDK.

A small, dependency-free terminal app that drills down through a *real* Keboola
project: buckets -> tables -> table detail + data preview. Every screen is backed
by a live Storage API call made through the importable SDK:

    from keboola_agent_cli import Client

    with Client(url=KBC_URL, token=KBC_TOKEN) as kbc:
        buckets = kbc.raw.list_buckets()

This file is the whole application: a thin data layer over the SDK, a curses
controller that owns navigation/state, and a ``main()`` that reads config and
hands control to :func:`curses.wrapper`. Pure drawing lives in ``_render.py``.

Why ``client.raw`` and not the typed facade? The :class:`Client` facade
intentionally exposes only high-level shapes (queries, files, jobs, config
detail) -- bucket/table *listing* is deliberately not on it. The facade documents
that you reach for the underlying :class:`KeboolaClient` via :attr:`Client.raw`
for those raw Storage endpoints, which is exactly what a storage browser needs.

Run it::

    export KBC_URL=https://connection.keboola.com
    export KBC_TOKEN=your-storage-api-token
    python examples/storage_tui/app.py

The token is read from the environment and is never printed, logged, or written
anywhere; only the URL host is ever shown on screen.
"""

from __future__ import annotations

import csv
import curses
import io
import os
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from urllib.parse import urlparse

import _render  # local sibling module (pure curses drawing helpers)

# Importing from the package root mirrors how a real downstream consumer would
# use the SDK: ``from keboola_agent_cli import Client``. When this file is run as
# a loose script (``python examples/storage_tui/app.py``) the package is on the
# path because it is installed in the dev environment (``uv pip install -e .``).
from keboola_agent_cli import Client
from keboola_agent_cli.errors import KeboolaApiError

# --- Config keys (read from the environment; never hardcode a token/URL). -----
ENV_URL = "KBC_URL"
ENV_TOKEN = "KBC_TOKEN"

# --- UI tunables. Kept here as named constants rather than magic numbers. -----
PREVIEW_ROW_LIMIT = 20  # rows pulled for the table data preview
PREVIEW_MAX_COLUMNS = 8  # columns shown in the preview grid (terminal width)
KEY_HINTS = "↑/↓ or j/k move │ →/Enter open │ ←/Esc/⌫ back │ r refresh │ q quit"
ESC_KEY = 27
ENTER_KEYS = (curses.KEY_ENTER, ord("\n"), ord("\r"))
BACK_KEYS = (curses.KEY_LEFT, ESC_KEY, curses.KEY_BACKSPACE, ord("\b"), 127)
UP_KEYS = (curses.KEY_UP, ord("k"))
DOWN_KEYS = (curses.KEY_DOWN, ord("j"))
FORWARD_KEYS = (curses.KEY_RIGHT, *ENTER_KEYS)
QUIT_KEYS = (ord("q"), ord("Q"))
REFRESH_KEYS = (ord("r"), ord("R"))


class View(Enum):
    """Which drill-down level is currently on screen."""

    BUCKETS = auto()
    TABLES = auto()
    TABLE_DETAIL = auto()


@dataclass(frozen=True)
class PreviewData:
    """Parsed CSV data preview: the header row and the data rows, named.

    A dataclass (not a bare tuple) because the two lists are semantically
    distinct -- see CONTRIBUTING.md "Code Quality Patterns".
    """

    header: list[str]
    rows: list[list[str]]


@dataclass
class TableDetail:
    """Parsed, render-ready table detail for the deepest view."""

    title: str
    info_lines: list[str]
    columns: list[str]
    preview_header: list[str]
    preview_rows: list[list[str]]


@dataclass
class BrowserState:
    """All mutable UI state for the browser, kept in one place.

    The two list views (buckets, tables) each track their own selection and
    scroll offset so going back restores where you were.
    """

    view: View = View.BUCKETS
    buckets: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    detail: TableDetail | None = None
    bucket_sel: int = 0
    bucket_top: int = 0
    table_sel: int = 0
    table_top: int = 0
    status: str = ""  # transient message (errors), shown above the footer


def host_only(url: str) -> str:
    """Return just the scheme+host of a stack URL (never any credentials)."""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url


def bucket_label(bucket: dict[str, Any]) -> str:
    """Format a single bucket as a list row, defensively reading optional keys."""
    bucket_id = str(bucket.get("id", "?"))
    stage = str(bucket.get("stage", "")).upper()
    name = str(bucket.get("name", ""))
    description = str(bucket.get("description") or "").strip()
    label = f"[{stage}] {bucket_id}" if stage else bucket_id
    if name and name != bucket_id:
        label = f"{label}  ({name})"
    if description:
        label = f"{label} — {description}"
    return label


def _row_count_text(table: dict[str, Any]) -> str:
    """Human row-count for a table dict, tolerating a missing/None count."""
    count = table.get("rowsCount")
    if count is None:
        return "? rows"
    try:
        return f"{int(count):,} rows"
    except (TypeError, ValueError):
        return f"{count} rows"


def table_label(table: dict[str, Any]) -> str:
    """Format a single table as a list row."""
    table_id = str(table.get("id", "?"))
    name = str(table.get("name", "")) or table_id
    return f"{name}  ·  {_row_count_text(table)}"


def _column_names(detail: dict[str, Any]) -> list[str]:
    """Extract column names from a table-detail dict (names or definition objects).

    The Storage API returns ``columns`` as a list of names, but with newer typed
    tables a richer ``definition.columns`` (objects with a ``name``) may be
    present. Prefer the plain list and fall back gracefully.
    """
    columns = detail.get("columns")
    if isinstance(columns, list) and all(isinstance(item, str) for item in columns):
        return [str(item) for item in columns]
    definition = detail.get("definition")
    if isinstance(definition, dict):
        def_columns = definition.get("columns")
        if isinstance(def_columns, list):
            return [str(col.get("name", "")) for col in def_columns if isinstance(col, dict)]
    return [str(item) for item in columns] if isinstance(columns, list) else []


def parse_preview_csv(csv_text: str, max_columns: int) -> PreviewData:
    """Parse a Storage data-preview CSV string into a header row and data rows.

    Uses the stdlib ``csv`` module (the preview comes back as a CSV string).
    Columns are capped to ``max_columns`` so a wide table still fits the grid.
    Returns an empty :class:`PreviewData` for empty input.
    """
    if not csv_text.strip():
        return PreviewData(header=[], rows=[])
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return PreviewData(header=[], rows=[])
    header = rows[0][:max_columns]
    data_rows = [row[:max_columns] for row in rows[1:]]
    return PreviewData(header=header, rows=data_rows)


def build_table_detail(
    detail: dict[str, Any], preview_csv: str, *, max_columns: int
) -> TableDetail:
    """Combine a table-detail dict and a preview CSV into render-ready state."""
    table_id = str(detail.get("id", "?"))
    name = str(detail.get("name", "")) or table_id
    columns = _column_names(detail)
    info_lines = [
        f"Table ID : {table_id}",
        f"Rows     : {_row_count_text(detail)}",
        f"Columns  : {len(columns)}",
    ]
    bucket = detail.get("bucket")
    if isinstance(bucket, dict) and bucket.get("id"):
        info_lines.append(f"Bucket   : {bucket['id']}")
    created = detail.get("created")
    if created:
        info_lines.append(f"Created  : {created}")
    preview = parse_preview_csv(preview_csv, max_columns)
    return TableDetail(
        title=f"Table: {name}",
        info_lines=info_lines,
        columns=columns,
        preview_header=preview.header,
        preview_rows=preview.rows,
    )


def clamp_scroll(selected: int, top: int, visible_rows: int) -> int:
    """Return a new scroll offset so ``selected`` stays within the visible window."""
    if visible_rows <= 0:
        return top
    if selected < top:
        return selected
    if selected >= top + visible_rows:
        return selected - visible_rows + 1
    return top


def _visible_rows(stdscr: curses.window) -> int:
    """Number of list rows that fit between the title and the footer."""
    height, _ = stdscr.getmaxyx()
    # body starts at row 2 (after header row 0 + title row 1); footer takes 2 rows
    return max(height - 4, 0)


class StorageBrowser:
    """Owns the curses loop, navigation, and the SDK-backed data fetches.

    The :class:`Client` is injected so the loop never constructs it (and never
    touches the token): ``main`` builds the client inside a ``with`` block and
    hands it in.
    """

    def __init__(self, client: Client, host: str) -> None:
        self._client = client
        self._host = host
        self._state = BrowserState()

    # --- SDK-backed data fetches. ---------------------------------------------
    # All three use ``self._client.raw`` -- the underlying KeboolaClient -- because
    # the typed Client facade deliberately omits bucket/table listing.

    def _fetch_buckets(self) -> None:
        """Load buckets via ``client.raw.list_buckets()``."""
        self._state.buckets = self._client.raw.list_buckets()
        self._state.bucket_sel = 0
        self._state.bucket_top = 0

    def _fetch_tables(self, bucket_id: str) -> None:
        """Load tables for one bucket via ``client.raw.list_tables(bucket_id)``."""
        self._state.tables = self._client.raw.list_tables(bucket_id=bucket_id)
        self._state.table_sel = 0
        self._state.table_top = 0

    def _fetch_table_detail(self, table_id: str) -> None:
        """Load detail + a small data preview for one table via the raw client.

        ``get_table_detail`` yields columns/row-count; ``get_table_data_preview``
        returns a CSV string we parse with the stdlib ``csv`` module.
        """
        detail = self._client.raw.get_table_detail(table_id)
        preview_csv = self._client.raw.get_table_data_preview(table_id, limit=PREVIEW_ROW_LIMIT)
        self._state.detail = build_table_detail(
            detail, preview_csv, max_columns=PREVIEW_MAX_COLUMNS
        )

    def _run_with_loading(self, stdscr: curses.window, message: str, action: Any) -> bool:
        """Show a loading frame, run a fetch ``action``, surface API errors.

        Returns True on success, False if a :class:`KeboolaApiError` was caught
        (the message is stored in the status line instead of crashing curses).
        """
        self._state.status = ""
        self._draw_loading(stdscr, message)
        try:
            action()
        except KeboolaApiError as exc:
            self._state.status = f"API error: {exc.message}"
            return False
        return True

    # --- Drawing dispatch. ----------------------------------------------------

    def _breadcrumb(self) -> str:
        """Breadcrumb string for the header, reflecting the current drill path."""
        state = self._state
        if state.view is View.BUCKETS:
            return "Buckets"
        bucket_id = self._selected_bucket_id() or "?"
        if state.view is View.TABLES:
            return f"Buckets / {bucket_id}"
        table_id = self._selected_table_id() or "?"
        return f"Buckets / {bucket_id} / {table_id}"

    def _draw_loading(self, stdscr: curses.window, message: str) -> None:
        """Render a full frame whose body is just a centered loading message."""
        stdscr.erase()
        _render.draw_header(stdscr, self._host, self._breadcrumb())
        _render.draw_loading(stdscr, message)
        _render.draw_footer(stdscr, KEY_HINTS, self._state.status)
        stdscr.refresh()

    def _draw(self, stdscr: curses.window) -> None:
        """Render the current view in full."""
        stdscr.erase()
        _render.draw_header(stdscr, self._host, self._breadcrumb())
        state = self._state
        if state.view is View.BUCKETS:
            self._draw_buckets(stdscr)
        elif state.view is View.TABLES:
            self._draw_tables(stdscr)
        else:
            self._draw_detail(stdscr)
        _render.draw_footer(stdscr, KEY_HINTS, state.status)
        stdscr.refresh()

    def _draw_buckets(self, stdscr: curses.window) -> None:
        """Draw the buckets list pane."""
        state = self._state
        rows = [bucket_label(bucket) for bucket in state.buckets]
        _render.draw_list_pane(
            stdscr,
            title=f"Buckets ({len(rows)})",
            rows=rows,
            selected=state.bucket_sel,
            top=state.bucket_top,
            empty_message="No buckets in this project.",
        )

    def _draw_tables(self, stdscr: curses.window) -> None:
        """Draw the tables list pane for the selected bucket."""
        state = self._state
        rows = [table_label(table) for table in state.tables]
        bucket_id = self._selected_bucket_id() or ""
        _render.draw_list_pane(
            stdscr,
            title=f"Tables in {bucket_id} ({len(rows)})",
            rows=rows,
            selected=state.table_sel,
            top=state.table_top,
            empty_message="This bucket has no tables.",
        )

    def _draw_detail(self, stdscr: curses.window) -> None:
        """Draw the table-detail + preview pane."""
        detail = self._state.detail
        if detail is None:
            _render.draw_loading(stdscr, "No table selected.")
            return
        _render.draw_table_detail(
            stdscr,
            title=detail.title,
            info_lines=detail.info_lines,
            columns=detail.columns,
            preview_header=detail.preview_header,
            preview_rows=detail.preview_rows,
        )

    # --- Selection helpers. ---------------------------------------------------

    def _selected_bucket_id(self) -> str | None:
        """ID of the highlighted bucket, or None when the list is empty."""
        state = self._state
        if not state.buckets:
            return None
        bucket = state.buckets[state.bucket_sel]
        return str(bucket.get("id")) if bucket.get("id") is not None else None

    def _selected_table_id(self) -> str | None:
        """ID of the highlighted table, or None when the list is empty."""
        state = self._state
        if not state.tables:
            return None
        table = state.tables[state.table_sel]
        return str(table.get("id")) if table.get("id") is not None else None

    # --- Input handling. ------------------------------------------------------

    def _move_selection(self, stdscr: curses.window, delta: int) -> None:
        """Move the selection in the active list view by ``delta`` and re-scroll."""
        state = self._state
        visible = _visible_rows(stdscr)
        if state.view is View.BUCKETS and state.buckets:
            state.bucket_sel = max(0, min(state.bucket_sel + delta, len(state.buckets) - 1))
            state.bucket_top = clamp_scroll(state.bucket_sel, state.bucket_top, visible)
        elif state.view is View.TABLES and state.tables:
            state.table_sel = max(0, min(state.table_sel + delta, len(state.tables) - 1))
            state.table_top = clamp_scroll(state.table_sel, state.table_top, visible)

    def _drill_in(self, stdscr: curses.window) -> None:
        """Enter the next level down from the current view."""
        state = self._state
        if state.view is View.BUCKETS:
            bucket_id = self._selected_bucket_id()
            if bucket_id is None:
                return
            if self._run_with_loading(
                stdscr, "Loading tables…", lambda: self._fetch_tables(bucket_id)
            ):
                state.view = View.TABLES
        elif state.view is View.TABLES:
            table_id = self._selected_table_id()
            if table_id is None:
                return
            if self._run_with_loading(
                stdscr, "Loading table detail…", lambda: self._fetch_table_detail(table_id)
            ):
                state.view = View.TABLE_DETAIL

    def _go_back(self) -> None:
        """Pop one level up; from buckets, back is a no-op."""
        state = self._state
        if state.view is View.TABLE_DETAIL:
            state.view = View.TABLES
            state.detail = None
        elif state.view is View.TABLES:
            state.view = View.BUCKETS
            state.tables = []

    def _refresh(self, stdscr: curses.window) -> None:
        """Re-fetch the data backing the current view."""
        state = self._state
        if state.view is View.BUCKETS:
            self._run_with_loading(stdscr, "Refreshing buckets…", self._fetch_buckets)
        elif state.view is View.TABLES:
            bucket_id = self._selected_bucket_id()
            if bucket_id is not None:
                self._run_with_loading(
                    stdscr, "Refreshing tables…", lambda: self._fetch_tables(bucket_id)
                )
        else:
            table_id = self._selected_table_id()
            if table_id is not None:
                self._run_with_loading(
                    stdscr,
                    "Refreshing detail…",
                    lambda: self._fetch_table_detail(table_id),
                )

    def _handle_key(self, stdscr: curses.window, key: int) -> bool:
        """Process one keypress. Returns False when the user asked to quit."""
        if key in QUIT_KEYS:
            return False
        if key in UP_KEYS:
            self._move_selection(stdscr, -1)
        elif key in DOWN_KEYS:
            self._move_selection(stdscr, 1)
        elif key in FORWARD_KEYS:
            self._drill_in(stdscr)
        elif key in BACK_KEYS:
            self._go_back()
        elif key in REFRESH_KEYS:
            self._refresh(stdscr)
        return True

    def run(self, stdscr: curses.window) -> None:
        """Curses entry point: initial load, then the event loop."""
        curses.curs_set(0)
        stdscr.keypad(True)
        _render.init_colors()
        self._run_with_loading(stdscr, "Loading buckets…", self._fetch_buckets)
        running = True
        while running:
            self._draw(stdscr)
            key = stdscr.getch()
            running = self._handle_key(stdscr, key)


def _require_env(name: str) -> str:
    """Read a required env var, failing fast with a clear stderr message.

    We deliberately do NOT invent a default (especially not for the token): a
    missing value is a hard, explained exit *before* curses starts so the error
    is visible on a normal terminal.
    """
    try:
        value = os.environ[name]
    except KeyError:
        print(
            f"error: required environment variable {name} is not set.\n"
            f"  export {ENV_URL}=https://connection.keboola.com\n"
            f"  export {ENV_TOKEN}=your-storage-api-token",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if not value.strip():
        print(f"error: environment variable {name} is empty.", file=sys.stderr)
        raise SystemExit(1)
    return value


def main() -> None:
    """Read config from the environment and launch the curses browser.

    The token is read here and passed straight into the SDK context manager; it
    is never printed, logged, or stored. Only the URL host reaches the screen.
    """
    url = _require_env(ENV_URL)
    token = _require_env(ENV_TOKEN)
    host = host_only(url)

    # The SDK as a context manager: the underlying HTTP client is closed on exit
    # even if curses raises. curses.wrapper guarantees the terminal is restored.
    with Client(url=url, token=token) as kbc:
        browser = StorageBrowser(kbc, host)
        curses.wrapper(browser.run)


if __name__ == "__main__":
    main()
