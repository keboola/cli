# kbagent Error Code Reference

All machine-readable codes emitted via `--json` output.  Every code is a member
of `ErrorCode` in `src/keboola_agent_cli/errors.py`.

## Versioning

| Change | Version impact |
|---|---|
| Add a new code | Minor bump |
| Rename or remove a code | Major bump |

## Code catalogue

### Auth / access

| Code | Description |
|---|---|
| `INVALID_TOKEN` | Storage API token is invalid or expired |
| `ACCESS_DENIED` | Token lacks the required permission for this API call |
| `PERMISSION_DENIED` | Operation blocked by the active kbagent permission policy |

### Network / transport

| Code | Description |
|---|---|
| `TIMEOUT` | HTTP request timed out |
| `CONNECTION_ERROR` | TCP-level connection failure |
| `RETRY_EXHAUSTED` | All retry attempts failed (typically after 429/5xx) |

### API / generic

| Code | Description |
|---|---|
| `API_ERROR` | Unexpected HTTP error from the Keboola API |
| `NOT_FOUND` | Requested resource does not exist (404) |
| `ALREADY_EXISTS` | Resource or file already exists and was not overwritten |
| `VALIDATION_ERROR` | Request failed API-side validation |
| `INVALID_ARGUMENT` | Caller supplied an invalid argument value |
| `INVALID_FORMAT` | Input is not in the expected format |
| `USAGE_ERROR` | Incorrect CLI flag combination or missing required argument |
| `MISSING_PARAMETER` | A required parameter was not supplied |
| `UNKNOWN_ERROR` | Catch-all for unclassified errors |

### Configuration

| Code | Description |
|---|---|
| `CONFIG_ERROR` | kbagent config problem (e.g. unknown project alias) |
| `NOT_INITIALIZED` | `.keboola/manifest.json` not found; run `sync init` first |
| `INIT_ERROR` | Error during `sync init` auto-init path |

### Jobs

| Code | Description |
|---|---|
| `QUEUE_JOB_FAILED` | Queue API job finished with status `error` or `warning` |
| `QUEUE_JOB_TIMEOUT` | Polling timed out waiting for a Queue job |
| `STORAGE_JOB_FAILED` | Storage API async job finished in a failed state |
| `STORAGE_JOB_TIMEOUT` | Polling timed out waiting for a Storage async job |
| `QUERY_JOB_FAILED` | Query Service job finished in a failed state |
| `QUERY_JOB_TIMEOUT` | Polling timed out waiting for a Query Service job |

### Variables

| Code | Description |
|---|---|
| `NO_VARIABLE_ROWS` | Linked `keboola.variables` config has no rows (fix: `config variables-set`) |
| `MALFORMED_VARIABLES_ROW` | Variables row returned by the API is missing a usable `id` |

### Storage

| Code | Description |
|---|---|
| `UPLOAD_FAILED` | Cloud storage upload to S3/Azure/GCS failed |
| `EXPORT_EMPTY_MANIFEST` | Sliced export manifest contains no slices |
| `EXPORT_NO_FILE` | Export manifest lists no downloadable file |
| `EXPORT_NO_URL` | Export entry has no download URL |
| `NOT_SLICED` | Attempted a sliced-file operation on a non-sliced file |
| `FILE_NO_URL` | File metadata has no usable download URL |

### I/O

| Code | Description |
|---|---|
| `FILE_NOT_FOUND` | Local file path does not exist |
| `DIR_NOT_FOUND` | Local directory path does not exist |
| `READ_ERROR` | Error reading a local file |
| `WRITE_ERROR` | Error writing a local file |
| `INPUT_ERROR` | Invalid or unparseable input data |

### Lineage

| Code | Description |
|---|---|
| `NODE_NOT_FOUND` | Requested node not found in the lineage graph |

### Sharing

| Code | Description |
|---|---|
| `INVALID_SHARING_TYPE` | Unsupported bucket sharing type |
| `NOT_LINKED_BUCKET` | Bucket is not a linked bucket |

### KAI (AI Service)

| Code | Description |
|---|---|
| `KAI_ERROR` | AI Service request failed |
| `KAI_NOT_ENABLED` | KAI is not enabled on this project |

### Workspace / Query

| Code | Description |
|---|---|
| `MISSING_QUERY` | No SQL query was provided |
| `WORKSPACE_NOT_FOUND` | Workspace not found in the project |

### Sync

| Code | Description |
|---|---|
| `PARENT_CONFIG_NOT_TRACKED` | Row operation references a parent config not in the manifest |

### Encryption

| Code | Description |
|---|---|
| `ENCRYPTION_FAILED` | Secret encryption via the Encryption API failed |
