# Programming with kbagent CLI as a Python SDK

kbagent is not just a command-line tool -- it is also a Python SDK. When you
install kbagent (`uv tool install keboola-agent-cli`), you get importable
Python modules that you can use directly in your scripts.

Use `--hint` on any command to see how to do the same thing in Python:

```bash
kbagent --hint client config list --project myproj
kbagent --hint service job run --project myproj --component-id keboola.ex-http --config-id 123
```

## Two layers: client vs service

### Client layer (`--hint client`)

Direct API calls with explicit URL and token. No dependency on CLI config.

```python
import os
from keboola_agent_cli.client import KeboolaClient

client = KeboolaClient(
    base_url="https://connection.eu-central-1.keboola.com",
    token=os.environ["KBC_STORAGE_TOKEN"],
)
try:
    # List all components with their configurations
    components = client.list_components()

    # Get a specific config
    detail = client.get_config_detail("keboola.ex-db-snowflake", "12345")

    # List tables in a bucket
    tables = client.list_tables(bucket_id="in.c-demo")

    # Run a job via Queue API
    job = client.create_job(
        component_id="keboola.ex-http",
        config_id="456",
    )
finally:
    client.close()
```

**When to use**: standalone scripts, CI/CD pipelines, Lambda functions,
or any context where you manage tokens and URLs yourself.

**Available clients**:
- `KeboolaClient` -- Storage API + Queue API + Encryption API
- `ManageClient` -- Manage API (organization operations, token management)
- `AiServiceClient` -- AI Service API (component search, schema summaries)

### Service layer (`--hint service`)

Higher-level abstraction that uses CLI config for project resolution.

```python
from pathlib import Path
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.services.config_service import ConfigService

# Explicit path to config directory (no CWD magic)
store = ConfigStore(config_dir=Path("/Users/you/.kbagent"))
service = ConfigService(config_store=store)

# Use project aliases -- service resolves URL + token from config
result = service.list_configs(aliases=["myproj"])
# Returns: {"configs": [...], "errors": [...]}

# Query all projects at once (parallel execution)
result = service.list_configs()  # aliases=None means all projects
```

**When to use**: scripts that work with multiple projects, need automatic
branch resolution, or want error accumulation (one project fails, others
continue).

**Key difference from client layer**: you pass project **aliases** (like
"myproj") instead of URLs and tokens. The service resolves them from the
same config file that `kbagent` uses.

## Comparison table

| Feature | Client layer | Service layer |
|---------|-------------|---------------|
| Authentication | Explicit URL + token | Project alias from config |
| Multi-project | Manual loop | Built-in parallel execution |
| Branch resolution | Explicit branch_id | Auto-resolves active branch |
| Error handling | Exceptions | Error accumulation (partial success) |
| Dependencies | None (just URL + token) | Requires CLI config (`kbagent project add`) |
| Config path | N/A | Explicit `config_dir=Path(...)` |

## Available services

| Service class | Import from | Key methods |
|---------------|-------------|-------------|
| `ConfigService` | `services.config_service` | `list_configs`, `get_config_detail`, `search_configs` |
| `StorageService` | `services.storage_service` | `list_buckets`, `list_tables`, `upload_table`, `download_table` |
| `JobService` | `services.job_service` | `list_jobs`, `get_job_detail`, `run_job` |
| `BranchService` | `services.branch_service` | `list_branches`, `create_branch`, `delete_branch` |
| `WorkspaceService` | `services.workspace_service` | `create_workspace`, `execute_query`, `load_tables` |
| `SharingService` | `services.sharing_service` | `list_shared`, `share`, `link`, `unlink` |
| `LineageService` | `services.lineage_service` | `get_lineage` |
| `ComponentService` | `services.component_service` | `list_components`, `get_component_detail` |
| `EncryptService` | `services.encrypt_service` | `encrypt` |
| `OrgService` | `services.org_service` | `setup_organization` |

## Common patterns

### Poll a job until completion

```python
import time
job = client.create_job(component_id="keboola.ex-http", config_id="123")
while not job.get("isFinished"):
    time.sleep(5)
    job = client.get_job_detail(str(job["id"]))
print(f"Job finished with status: {job['status']}")
```

Or with the service layer (handles polling internally):

```python
result = job_service.run_job(
    alias="myproj",
    component_id="keboola.ex-http",
    config_id="123",
    wait=True,
    timeout=300.0,
)
```

### Upload a CSV file to a table

```python
result = storage_service.upload_table(
    alias="myproj",
    table_id="in.c-demo.my-table",
    file_path="/tmp/data.csv",
    incremental=True,
)
```

### Execute SQL in a workspace

```python
result = workspace_service.execute_query(
    alias="myproj",
    workspace_id=12345,
    sql="SELECT * FROM my_table LIMIT 10",
)
# result contains query results as dicts
```

### Search across all project configurations

```python
result = config_service.search_configs(
    query="snowflake",
    ignore_case=True,
)
# result = {"matches": [...], "errors": [...], "stats": {...}}
```

## Security notes

- Never hardcode tokens in scripts. Use `os.environ["KBC_STORAGE_TOKEN"]`.
- The client automatically masks tokens in error messages.
- Built-in retry logic handles 429/5xx errors with exponential backoff.
- ConfigStore files use 0600 permissions to protect stored tokens.
