# --hint Mode: Use kbagent as a Python SDK

The `--hint` flag generates equivalent Python code for any CLI command, without
executing it. This lets you use `kbagent` not just as a CLI tool, but as a
programming reference and SDK.

## Quick start

```bash
# Show how to call the API directly (client layer)
kbagent --hint client config list --project myproj

# Show how to use the service layer (with CLI config)
kbagent --hint service config list --project myproj
```

## Two modes

### `--hint client` (direct API calls)

Generates code using `KeboolaClient` with explicit URL and token.
Best for: standalone scripts, CI/CD, when you don't want CLI config dependency.

```python
import os
from keboola_agent_cli.client import KeboolaClient

client = KeboolaClient(
    base_url="https://connection.eu-central-1.keboola.com",
    token=os.environ["KBC_STORAGE_TOKEN"],
)
try:
    components = client.list_components()
finally:
    client.close()
```

### `--hint service` (service layer with CLI config)

Generates code using the service layer, which reads project configuration
from the same `config.json` that `kbagent` uses. Best for: scripts that
work with multiple projects, need branch resolution, or want error accumulation.

```python
from pathlib import Path
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.services.config_service import ConfigService

store = ConfigStore(config_dir=Path("/path/to/.kbagent"))
service = ConfigService(config_store=store)
result = service.list_configs(aliases=["myproj"])
```

The `config_dir` path is always explicit -- no hidden CWD resolution.
The generated code uses the actual path from your current CLI configuration.

## What it does NOT do

- Does NOT execute any API calls
- Does NOT include real tokens (always uses `os.environ[...]`)
- Does NOT trigger auto-update checks
- Does NOT require a valid token (only needs project config for URL resolution)

## Supported commands

All API-backed commands support `--hint` (45 commands total), including:
- `config list/detail/search`
- `storage buckets/tables/files` and all CRUD operations
- `job list/detail/run` (including poll loop pattern for `--wait`)
- `branch list/create/delete`
- `workspace create/list/detail/delete/load/query`
- `sharing list/share/unshare/link/unlink`
- `component list/detail`
- `encrypt values`
- `lineage show`
- `org setup`

Local-only commands (`project add/list/remove`, `branch use/reset`, etc.) do
not support `--hint` because they don't make API calls.

## Examples

### Multi-step command (job run with polling)

```bash
kbagent --hint client job run --project myproj \
  --component-id keboola.ex-http --config-id 123 --wait
```

Generates code with a polling loop:

```python
job = client.create_job(component_id="keboola.ex-http", config_id="123")
while not job.get("isFinished"):
    time.sleep(5.0)
    job = client.get_job_detail(job_id=str(job["id"]))
```

### Manage API command

```bash
kbagent --hint client org setup --org-id 42 --url https://connection.keboola.com
```

Generates code with `ManageClient` and the correct token env var:

```python
from keboola_agent_cli.manage_client import ManageClient

manage_client = ManageClient(
    base_url="https://connection.keboola.com",
    token=os.environ["KBC_MANAGE_API_TOKEN"],
)
```
