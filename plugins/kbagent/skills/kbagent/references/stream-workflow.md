# Data Streams (OTLP / OpenTelemetry) workflow

> Audience: a developer or a kbagent agent wiring an OpenTelemetry exporter
> (logs / metrics / traces) into Keboola Data Streams. Goal: create an OTLP
> source and read its endpoint in one command instead of copy-pasting from the
> UI. Reference: https://help.keboola.com/storage/data-streams/opentelemetry/
> (since v0.50.0; closes keboola/cli#357)

## Mental model: two hosts

Data Streams has a **control plane** and a **data plane**:

- **Control plane** `stream.<region>` — derived from the project's Storage URL
  (`connection.<region>` → `stream.<region>`, same scheme as `ai.`/`queue.`).
  This is what `kbagent stream …` calls, authenticated with the **per-project
  Storage token** kbagent already holds. No manage token, no extra prompt.
- **Data plane** `stream-in.<region>/otlp/<projectId>/<sourceName>/<secret>` —
  the actual OTLP/HTTP ingest endpoint your exporter posts to. kbagent does
  **not** derive this; it is returned by the API in `source.otlp.url`, with the
  auth **secret embedded in the URL path** (no header needed).

## Secret handling

The ingest URL carries the secret. Every kbagent surface **masks it by
default** — the `endpoint`, the three per-signal endpoints, and the raw
`source` object in `--json`. Print the real secret only on explicit intent:

```bash
kbagent --json stream detail my-otlp --project P --reveal
```

Treat `--reveal` output as a credential. The intended consumer is a setup
step that exports `OTEL_EXPORTER_OTLP_ENDPOINT` for a daemon/CI — not a log.

## End-to-end: stand up an OTLP source

```bash
# 1. Create the source. For OTLP this also auto-provisions the logs/metrics/traces
#    sinks (bucket in.c-otlp-my-otlp) so data lands. Idempotent + --if-not-exists.
kbagent --json stream create-source --project P --name my-otlp --type otlp --if-not-exists

# 2. Read the wiring (masked). Confirms protocol, per-signal endpoints, and the
#    destination tables.
kbagent stream detail my-otlp --project P

# 3. Reveal the endpoint to configure an exporter.
kbagent --json stream detail my-otlp --project P --reveal
```

The exporter then uses (per the OTLP/HTTP spec, `http/protobuf`):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="<source.otlp.url from --reveal>"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_SERVICE_NAME="my-service"
```

Per-signal endpoints are the base URL + `/v1/logs`, `/v1/traces`, `/v1/metrics`
— most SDKs append these automatically from the base `OTEL_EXPORTER_OTLP_ENDPOINT`.

## Destination tables

The raw Stream API `POST /sources` creates only the bare source — no sinks. So
`kbagent stream create-source --type otlp` (matching the UI) **auto-provisions
three table sinks** — logs / metrics / traces — into bucket
`in.c-otlp-<sourceName>` (tables `logs` / `metrics` / `traces`). Each record is
mapped to `id` (uuid), `datetime` (ingest time), and `body` (the full flattened
OTLP record as JSON); refine the per-signal column mapping in the Keboola UI
afterwards if you want first-class columns. Provisioning is idempotent (a re-run
or `--if-not-exists` only fills missing signals); pass `--no-sinks` for a bare
source. The bucket/tables materialize **lazily on first import** — they appear in
Storage seconds after the first record arrives, not at create time.

## Send data and verify it landed

```bash
# Capture the revealed endpoint into a shell var (don't echo the secret):
EP=$(kbagent --json stream detail my-otlp --project P --reveal | python3 -c \
  'import sys,json; print(json.load(sys.stdin)["data"]["endpoint"])')

# Post a standard OTLP/HTTP JSON logs payload (secret is in the URL path):
curl -s -X POST "$EP/v1/logs" -H 'Content-Type: application/json' --data @logs.json

# Default import trigger is ~1 min / 50 MB / 50k records; data is usually visible
# within ~15 s. Confirm rows landed:
kbagent --json storage table-detail --project P --table-id in.c-otlp-my-otlp.logs   # rowsCount
# Or read them via a workspace:
kbagent workspace load  --project P --workspace-id WS --tables in.c-otlp-my-otlp.logs
kbagent workspace query --project P --workspace-id WS --sql 'SELECT "id","datetime","body" FROM "logs"'
```

## Lifecycle notes

- `create-source` and `delete` are **async**: the API returns a Task and
  kbagent polls it to completion before returning.
- `delete` is destructive — `--dry-run` previews, `--yes`/`--force` skips the
  confirm. `--json` mode never prompts.
- `--branch ID` targets a dev branch; default is the project's default branch.
- Permission classes: `stream.list` / `stream.detail` = read,
  `stream.create-source` = write, `stream.delete` = destructive.
