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
| `MISSING_MASTER_TOKEN` | Operation requires a master (admin) Storage token (e.g. `config oauth-url` pre-flight); maps to exit 3 |
| `UNAUTHORIZED` | `kbagent serve` rejected the request's Bearer token |

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
| `UNEXPECTED_ERROR` | Per-project fallback in a multi-project command: that project raised an error carrying no code of its own. Other projects in the same run still report their own outcome |
| `HTTP_ERROR` | Generic HTTP-layer error in the `kbagent serve` envelope (HTTPException passthrough) |
| `INTERNAL_ERROR` | Uncaught exception inside a `kbagent serve` route handler (HTTP 500) |

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
| `JOB_TIMEOUT_TERMINATED` | `job run --wait` timeout expired and the remote job was successfully cancelled (exit 7; distinct from `QUEUE_JOB_TIMEOUT`, where the remote may still be running) |

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
| `VARIABLE_LINK_UNRESOLVED` | `sync push` could not resolve a transformation's variables link to a tracked config |
| `SYNC_CONFLICT` | `sync pull --force` aborted: local and remote both changed since the last pull (`details.conflicts` lists them) |

### Encryption

| Code | Description |
|---|---|
| `ENCRYPTION_FAILED` | Secret encryption via the Encryption API failed |

### Flow

| Code | Description |
|---|---|
| `SCHEDULE_DELETE_FAILED` | Deleting or deregistering a flow schedule failed |
| `INVALID_FLOW_DEFINITION` | Flow definition failed schema or semantic validation |

### Data Apps

| Code | Description |
|---|---|
| `DATA_APP_BUILD_FAILED` | Data app deploy/start poll loop ended in a failed build or setup state |
| `DATA_APP_DEPLOY_TIMEOUT` | Polling timed out waiting for a data app deploy or start |
| `DATA_APP_INVALID_GIT` | Data app git repository configuration is invalid or inaccessible |
| `DATA_APP_INVALID_SECRET` | Data app secret key or value failed validation |
| `DATA_APP_INVALID_REPO` | Reserved: repository failed data-app validation (not currently emitted; `validate-repo` reports findings with exit 1) |
| `DATA_APP_REPO_VALIDATION_BLOCKING` | Reserved: blocking repo-validation findings (not currently emitted; `validate-repo` reports findings with exit 1) |

### Developer Portal

| Code | Description |
|---|---|
| `DP_LOGIN_FAILED` | Developer Portal login failed (bad credentials or unexpected auth response) |
| `DP_MFA_REQUIRED` | Developer Portal account requires MFA, which the CLI login flow does not support |
| `DP_APP_NOT_FOUND` | Developer Portal app not found under the vendor |
| `DP_PUBLISH_REQUIREMENTS_MISSING` | App is missing required fields for publishing (fix via `dev-portal patch` first) |
| `DP_ICON_UPLOAD_FAILED` | Uploading the app icon to the Developer Portal failed |

### Programmatic Auth (browser login)

| Code | Description |
|---|---|
| `AUTH_NOT_SUPPORTED_ON_STACK` | Browser login is not enabled on this Keboola stack, or the code path only understands static Storage tokens (`kbc-session://` sentinel rejected) |
| `AUTH_FLOW_TIMEOUT` | The PKCE callback or device-authorization poll deadline elapsed before the user completed login |
| `AUTH_FLOW_DENIED` | The user (or the authorization server) denied the login request |
| `AUTH_FLOW_EXPIRED` | The device code / authorization code expired before it was used |
| `AUTH_BROWSER_UNAVAILABLE` | No usable browser was found for the PKCE loopback flow (falls back to device authorization) |
| `AUTH_STATE_MISMATCH` | The PKCE callback's `state` parameter did not match the one generated at login start |
| `SESSION_EXPIRED` | The programmatic-auth session's refresh token expired or was revoked; run `kbagent auth login` again |
| `SESSION_NOT_FOUND` | No programmatic-auth session is persisted for this stack; run `kbagent auth login` |
| `AUTH_MFA_INVALID` | `auth login-password` hit an MFA factor it cannot resolve without a browser (e.g. WebAuthn-only) -- use `kbagent auth login` for that account instead |

### Billing (Pay-As-You-Go)

| Code | Description |
|---|---|
| `PAYG_NOT_AVAILABLE` | The project does not have the `pay-as-you-go` feature, so it has no credit balance; the billing host may not even resolve on this stack |
