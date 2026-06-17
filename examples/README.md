# Examples

Runnable, self-contained programs that show how to build **on top of** kbagent.

| Example | Surface it demonstrates | What it does |
|---|---|---|
| [`storage_tui/`](storage_tui/) | **In-process Python SDK** (`from keboola_agent_cli import Client`) | A curses terminal app that browses a real project's Storage (buckets → tables → preview), driven entirely through the importable `Client` and `Client.raw`. |

Each example is independent and not part of the installed wheel — clone the repo
to run them.

## Which integration surface should I build on?

kbagent exposes three programmable surfaces; the examples here target the first.
See the comparison and decision guide in **[docs/sdk.md](../docs/sdk.md)**:

- **In-process Python SDK** — you're already a Python process with the token
  (a Keboola Data App, a transformation, a hosted service). Lowest latency,
  typed. → [docs/sdk.md](../docs/sdk.md), demoed by `storage_tui/`.
- **REST API (`kbagent serve`)** — a different process or language (JS, Go, a
  Web UI, a Slack bot). → [docs/build-your-own-client.md](../docs/build-your-own-client.md).
- **CLI (`kbagent ...`)** — a shell or an AI agent that shells out. →
  [README](../README.md), `kbagent --help`.

## Running an example

Every example reads its Keboola credentials from the environment (12-factor) and
never writes a token anywhere:

```bash
export KBC_URL=https://connection.keboola.com
export KBC_TOKEN=your-storage-api-token        # read access is enough for storage_tui
python examples/storage_tui/app.py
```

You need kbagent importable in the environment — `uv pip install -e .` from the
repo root, or `pip install keboola-cli`.
