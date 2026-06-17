# Keboola Storage Browser (curses TUI demo)

A self-contained terminal app that showcases the **in-process kbagent Python
SDK** (`from keboola_agent_cli import Client`). It is a drill-down browser over a
*real* Keboola project, built with nothing but Python's stdlib `curses` — no extra
dependencies. Every panel is populated by a live Storage API call routed through
the SDK; there is no mock data anywhere. Point it at a project, arrow through your
**buckets**, drill into a bucket's **tables**, and open a table to see its
columns, row count, and a 20-row **data preview**.

## What it looks like

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Keboola Storage Browser  │  https://connection.keboola.com  │ Buckets / in.c-… │   ← header (host only, never the token)
│                                                                                │
│  Tables in in.c-sales (3)                                                      │   ← pane title
│  ▶ orders        ·  12,840 rows                                                 │   ← selected row (highlighted)
│    customers     ·  3,201 rows                                                  │
│    order_items   ·  48,910 rows                                                 │
│                                                                                │
│                                                                                │
│                                                                                │
│ ↑/↓ or j/k move │ →/Enter open │ ←/Esc/⌫ back │ r refresh │ q quit            │   ← footer (key hints)
└──────────────────────────────────────────────────────────────────────────────┘
```

Drilling one level deeper (Enter on a table) swaps the body for a detail view:
table ID, row count, column list, and a small CSV-derived preview grid. A
centered `Loading…` message is shown while each API call is in flight, and any
API failure is rendered on a status line above the footer instead of crashing the
terminal.

## Requirements

- Python **3.12+**.
- The `keboola-cli` package importable in your environment. From a checkout of
  this repo: `uv pip install -e ".[dev]"` (or `uv sync`).
- A **real Storage API token with read access** to the project you want to
  browse. The token is read from the environment and is **never written, logged,
  or printed** — only the URL *host* is ever shown on screen.

## Run it

```bash
export KBC_URL=https://connection.keboola.com
export KBC_TOKEN=your-storage-api-token
python examples/storage_tui/app.py
```

`KBC_URL` and `KBC_TOKEN` are **required**. If either is missing or empty the app
prints a one-line explanation to stderr and exits with status `1` *before* curses
starts — there is no invented default for the URL, and never a default token.

## Keyboard shortcuts

| Key(s)                 | Action                                              |
| ---------------------- | --------------------------------------------------- |
| `↑` / `↓` or `k` / `j` | Move the selection up / down in a list              |
| `→` or `Enter`         | Drill in (bucket → its tables, table → its detail)  |
| `←` / `Esc` / `⌫`      | Go back one level (no-op at the buckets list)       |
| `r`                    | Refresh the data backing the current view           |
| `q`                    | Quit (curses restores the terminal on the way out)  |

## How it uses the SDK (a teaching example)

The whole point of this demo is to show the **importable, in-process SDK** in a
realistic app. The relevant lines live in `app.py`:

1. **Import the public entry point** — the same line any downstream consumer uses
   (`app.py`, near the top):

   ```python
   from keboola_agent_cli import Client
   from keboola_agent_cli.errors import KeboolaApiError
   ```

2. **Open the client as a context manager** (`main()`), so the underlying HTTP
   client is closed on exit even if curses raises. The token is passed straight in
   and never stored:

   ```python
   with Client(url=url, token=token) as kbc:
       browser = StorageBrowser(kbc, host)
       curses.wrapper(browser.run)
   ```

3. **Use `client.raw` for the Storage listing endpoints.** The typed `Client`
   facade intentionally exposes only high-level shapes (queries, files, jobs,
   config detail) — bucket/table *listing* is deliberately not on it, and the
   facade's own docstring points you to the underlying `KeboolaClient` via
   `Client.raw` for those raw endpoints. The three fetch methods in
   `StorageBrowser` are the entire data layer:

   | Screen          | SDK call                                                    |
   | --------------- | ----------------------------------------------------------- |
   | Buckets list    | `client.raw.list_buckets()`                                 |
   | Tables list     | `client.raw.list_tables(bucket_id=...)`                     |
   | Table detail    | `client.raw.get_table_detail(table_id)`                     |
   | Data preview    | `client.raw.get_table_data_preview(table_id, limit=20)`     |

   `get_table_data_preview` returns a **CSV string**, which the demo parses with
   the stdlib `csv` module (`parse_preview_csv`).

4. **Handle API errors specifically.** Every fetch is wrapped so a
   `KeboolaApiError` is caught and its `.message` shown on the status line — the
   curses UI keeps running and the terminal is always restored (via
   `curses.wrapper`).

### File layout

| File         | Responsibility                                                        |
| ------------ | --------------------------------------------------------------------- |
| `app.py`     | SDK data layer, curses navigation/state controller, and `main()`.     |
| `_render.py` | Pure curses drawing helpers (no API calls, no token, no app state).   |

Both modules are fully type-hinted, use `pathlib`-free stdlib only, and are
ruff-clean at the repo's 100-column line length.
