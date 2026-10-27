# kbagent Error Code Reference

All machine-readable codes emitted via `--json` output.  Every code is a member
of `ErrorCode` in `src/keboola_agent_cli/errors.py`.

This page is the one list of error codes, their exit codes and their error types *(since vNEXT)*. Other documents link here. `ERROR_CODE_TABLE` in `src/keboola_agent_cli/errors.py` holds the same pairs, and `make check-error-codes` fails when the two disagree ([CLI spec E1, E2](cli-interface-spec.md#E1)).

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | Success. Also a prompt declined at a terminal. |
| 1 | General failure, or a partial failure: at least one item or project failed. |
| 2 | Usage or validation error. |
| 3 | Authentication error. |
| 4 | Network error, or a local wait that timed out while the server job keeps running. |
| 5 | Configuration error, for example no project resolves (`CONFIG_ERROR`). |
| 6 | Denied by a permission policy (`PERMISSION_DENIED`), or a refused random-code confirmation. |
| 7 | The job was terminated after its timeout (`JOB_TIMEOUT_TERMINATED`). |
| 130 | Interrupted (Ctrl-C). |

A command that fails with an error code exits with the code's `Exit` value below. A command that processes several items or projects exits 1 when one of them failed, and each failed item carries its own `error_code` ([CLI spec E4, E5](cli-interface-spec.md#E4)).

## Error types

`error.error_type` in the `--json` error envelope is a broad category of the code: `api`, `authentication`, `authorization`, `configuration`, `conflict`, `network`, `not_found` or `validation`. The `Type` column below gives it per code.

## Versioning

| Change | Version impact |
|---|---|
| Add a new code | Minor bump |
| Rename or remove a code | Breaking change: the old code keeps working for at least 28 days, then a minor release removes it ([CLI spec C3, C8](cli-interface-spec.md#C8)) |
| Change the exit code or the type of a code | Breaking change ([CLI spec C1](cli-interface-spec.md#C1)) |

## Code catalogue

### Auth / access

| Code | Exit | Type | Description |
|---|---|---|---|
| `INVALID_TOKEN` | 3 | `authentication` | Storage API token is invalid or expired |
| `ACCESS_DENIED` | 1 | `api` | Token lacks the required permission for this API call |
| `PERMISSION_DENIED` | 6 | `authorization` | Operation blocked by the active kbagent permission policy |
| `MISSING_MASTER_TOKEN` | 3 | `authentication` | Operation requires a master (admin) Storage token (e.g. `token create`, `config oauth-url` pre-flight, and a `semantic-layer` WRITE against a non-admin token -- `semantic-layer` reads work with any valid token since PSGO-282; the Metastore's opaque 401 `Failed to create project scope` was its master-token gate pre-PSGO-282 and is reclassified to this code as a safety net); maps to exit 3 |
| `UNAUTHORIZED` | 1 | `api` | `kbagent serve` rejected the request's Bearer token |
| `AUTH_REJECTED` | 3 | `authentication` | Upstream answered HTTP 401 but its own error text did not blame the credential. Same exit 3 as `INVALID_TOKEN`; rotating the token is not the fix. (The Metastore's `Failed to create project scope` 401 has a known cause and lands on `MISSING_MASTER_TOKEN` instead) |

### Network / transport

| Code | Exit | Type | Description |
|---|---|---|---|
| `TIMEOUT` | 4 | `network` | HTTP request timed out |
| `CONNECTION_ERROR` | 4 | `network` | TCP-level connection failure |
| `RETRY_EXHAUSTED` | 4 | `network` | All retry attempts failed (typically after 429/5xx) |

### API / generic

| Code | Exit | Type | Description |
|---|---|---|---|
| `API_ERROR` | 1 | `api` | Unexpected HTTP error from the Keboola API |
| `NOT_FOUND` | 1 | `not_found` | Requested resource does not exist (404) |
| `ALREADY_EXISTS` | 1 | `conflict` | Resource or file already exists and was not overwritten |
| `VALIDATION_ERROR` | 2 | `validation` | Request failed API-side validation |
| `INVALID_ARGUMENT` | 2 | `validation` | Caller supplied an invalid argument value |
| `INVALID_FORMAT` | 2 | `api` | Input is not in the expected format |
| `USAGE_ERROR` | 2 | `validation` | Incorrect CLI flag combination or missing required argument |
| `CONFIRMATION_REQUIRED` | 2 | `validation` | A command that asks for confirmation ran in human mode without a terminal and without `--yes`. Nothing was changed. Run the command again with `--yes`. An AI agent asks the user first (since vNEXT) |
| `MISSING_PARAMETER` | 2 | `validation` | A required parameter was not supplied |
| `UNKNOWN_ERROR` | 1 | `api` | Catch-all for unclassified errors |
| `UNEXPECTED_ERROR` | 1 | `api` | Per-project fallback in a multi-project command: that project raised an error carrying no code of its own. Other projects in the same run still report their own outcome |
| `HTTP_ERROR` | 1 | `api` | Generic HTTP-layer error in the `kbagent serve` envelope (HTTPException passthrough) |
| `INTERNAL_ERROR` | 1 | `api` | Uncaught exception inside a `kbagent serve` route handler (HTTP 500), or a crash of a CLI command under `--json` (exit 1) |
| `INTERRUPTED` | 130 | `api` | The command was interrupted under `--json`: Ctrl+C (exit 130) or an aborted prompt (exit 1) |

### Configuration

| Code | Exit | Type | Description |
|---|---|---|---|
| `CONFIG_ERROR` | 5 | `configuration` | kbagent config problem (e.g. unknown project alias) |
| `NOT_INITIALIZED` | 1 | `api` | `.keboola/manifest.json` not found; run `sync init` first |
| `INIT_ERROR` | 1 | `api` | Error during `sync init` auto-init path |
| `FEATURE_NOT_ENABLED` | 5 | `configuration` | A client-side pre-flight found the project lacks a required feature flag (e.g. `branches-merge-requests` for `merge-request` writes); the server would answer an opaque 403/404 |

### Jobs

| Code | Exit | Type | Description |
|---|---|---|---|
| `QUEUE_JOB_FAILED` | 1 | `api` | Queue API job finished with status `error` or `warning` |
| `QUEUE_JOB_TIMEOUT` | 4 | `api` | Polling timed out waiting for a Queue job |
| `STORAGE_JOB_FAILED` | 1 | `api` | Storage API async job finished in a failed state |
| `STORAGE_JOB_TIMEOUT` | 4 | `api` | Polling timed out waiting for a Storage async job |
| `QUERY_JOB_FAILED` | 1 | `api` | Query Service job finished in a failed state |
| `QUERY_JOB_TIMEOUT` | 1 | `api` | Polling timed out waiting for a Query Service job |
| `JOB_TIMEOUT_TERMINATED` | 7 | `api` | `job run --wait` timeout expired and the remote job was successfully cancelled (exit 7; distinct from `QUEUE_JOB_TIMEOUT`, where the remote may still be running) |

### Variables

| Code | Exit | Type | Description |
|---|---|---|---|
| `NO_VARIABLE_ROWS` | 1 | `api` | Linked `keboola.variables` config has no rows (fix: `config variables-set`) |
| `MALFORMED_VARIABLES_ROW` | 1 | `api` | Variables row returned by the API is missing a usable `id` |

### Storage

| Code | Exit | Type | Description |
|---|---|---|---|
| `UPLOAD_FAILED` | 1 | `api` | Cloud storage upload to S3/Azure/GCS failed |
| `EXPORT_EMPTY_MANIFEST` | 1 | `api` | Sliced export manifest contains no slices |
| `EXPORT_NO_FILE` | 1 | `api` | Export manifest lists no downloadable file |
| `EXPORT_NO_URL` | 1 | `api` | Export entry has no download URL |
| `NOT_SLICED` | 1 | `api` | Attempted a sliced-file operation on a non-sliced file |
| `FILE_NO_URL` | 1 | `api` | File metadata has no usable download URL |

### I/O

| Code | Exit | Type | Description |
|---|---|---|---|
| `FILE_NOT_FOUND` | 2 | `not_found` | Local file path does not exist |
| `DIR_NOT_FOUND` | 1 | `api` | Local directory path does not exist |
| `READ_ERROR` | 1 | `api` | Error reading a local file |
| `WRITE_ERROR` | 1 | `api` | Error writing a local file |
| `INPUT_ERROR` | 2 | `api` | Invalid or unparseable input data |

### Lineage

| Code | Exit | Type | Description |
|---|---|---|---|
| `NODE_NOT_FOUND` | 1 | `api` | Requested node not found in the lineage graph |

### Sharing

| Code | Exit | Type | Description |
|---|---|---|---|
| `INVALID_SHARING_TYPE` | 1 | `api` | Unsupported bucket sharing type |
| `NOT_LINKED_BUCKET` | 1 | `api` | Bucket is not a linked bucket |

### KAI (AI Service)

| Code | Exit | Type | Description |
|---|---|---|---|
| `KAI_ERROR` | 1 | `api` | AI Service request failed |
| `KAI_NOT_ENABLED` | 1 | `api` | KAI is not enabled on this project |

### Workspace / Query

| Code | Exit | Type | Description |
|---|---|---|---|
| `MISSING_QUERY` | 2 | `api` | No SQL query was provided |
| `WORKSPACE_NOT_FOUND` | 1 | `api` | Workspace not found in the project |
| `WORKSPACE_LOAD_COPY_TOO_LARGE` | 1 | `validation` | `workspace load` refused to COPY a table over the 1 GB guard without confirmation; re-run with `--force` (or `--load-type clone` where the backend supports it) |

### Sync

| Code | Exit | Type | Description |
|---|---|---|---|
| `PARENT_CONFIG_NOT_TRACKED` | 1 | `api` | Row operation references a parent config not in the manifest |
| `VARIABLE_LINK_UNRESOLVED` | 1 | `api` | `sync push` could not resolve a transformation's variables link to a tracked config |
| `LINK_UNRESOLVED` | 1 | `api` | `sync push` could not re-point a shared-code row id or a task `configRowIds` entry to a row created in the same push; the id keeps its old value |
| `SYNC_CONFLICT` | 1 | `conflict` | `sync pull --force` aborted: local and remote both changed since the last pull (`details.conflicts` lists them) |
| `SYNC_LEGACY_BOUNDARY` | 1 | `conflict` | `sync push` refused one config: the working tree predates statement-boundary tracking, so pushing it would merge separate SQL statements into one. Run `sync pull` first |

### Encryption

| Code | Exit | Type | Description |
|---|---|---|---|
| `ENCRYPTION_FAILED` | 1 | `api` | Secret encryption via the Encryption API failed |

### Flow

| Code | Exit | Type | Description |
|---|---|---|---|
| `SCHEDULE_DELETE_FAILED` | 1 | `api` | Deleting or deregistering a flow schedule failed |
| `INVALID_FLOW_DEFINITION` | 1 | `api` | Flow definition failed schema or semantic validation |

### Data Apps

| Code | Exit | Type | Description |
|---|---|---|---|
| `DATA_APP_BUILD_FAILED` | 1 | `api` | Data app deploy/start poll loop ended in a failed build or setup state |
| `DATA_APP_DEPLOY_TIMEOUT` | 1 | `api` | Polling timed out waiting for a data app deploy or start |
| `DATA_APP_INVALID_GIT` | 1 | `api` | Data app git repository configuration is invalid or inaccessible |
| `DATA_APP_INVALID_SECRET` | 2 | `api` | Data app secret key or value failed validation |
| `DATA_APP_INVALID_REPO` | 1 | `api` | Reserved: repository failed data-app validation (not currently emitted; `validate-repo` reports findings with exit 1) |
| `DATA_APP_REPO_VALIDATION_BLOCKING` | 1 | `api` | Reserved: blocking repo-validation findings (not currently emitted; `validate-repo` reports findings with exit 1) |

### Developer Portal

| Code | Exit | Type | Description |
|---|---|---|---|
| `DP_LOGIN_FAILED` | 1 | `authentication` | Developer Portal login failed (bad credentials or unexpected auth response) |
| `DP_MFA_REQUIRED` | 1 | `authentication` | Developer Portal account requires MFA, which the CLI login flow does not support |
| `DP_APP_NOT_FOUND` | 1 | `not_found` | Developer Portal app not found under the vendor |
| `DP_PUBLISH_REQUIREMENTS_MISSING` | 1 | `validation` | App is missing required fields for publishing (fix via `dev-portal patch` first) |
| `DP_ICON_UPLOAD_FAILED` | 1 | `api` | Uploading the app icon to the Developer Portal failed |
| `OAUTH_IDENTITY_MISSING` | 5 | `configuration` | `kbagent oauth`: no token is stored in the keychain for the stack, or the keychain item cannot be read. Run `kbagent oauth identity add --stack HOST` (since vNEXT) |
| `OAUTH_TOKEN_TOO_BROAD` | 6 | `authorization` | `kbagent oauth`: the Manage token is not an application token whose only scope is `oauth:manage` (or it is expired or disabled). kbagent refuses it; there is no override (since vNEXT) |
| `OAUTH_KEYCHAIN_UNPROTECTED` | 6 | `authorization` | `kbagent oauth`: the keychain item trusts an application, so macOS no longer asks before releasing the token (someone clicked "Always Allow"). Store the token again with `kbagent oauth identity add` (since vNEXT) |
| `OAUTH_PREFLIGHT_FAILED` | 2 | `validation` | `kbagent oauth rotate-azure-secret`: a read-only check failed before any write (wrong app for the component, app not readable in the signed-in tenant, `az` or `op` not signed in). Nothing was changed (since vNEXT) |
| `OAUTH_ROTATION_INCOMPLETE` | 1 | `api` | `kbagent oauth rotate-azure-secret`: a write failed after the new secret was created. `details` names the stage and the new key ID; the message says what to clean up. The old secret stays valid (since vNEXT) |

### Programmatic Auth (browser login)

| Code | Exit | Type | Description |
|---|---|---|---|
| `AUTH_NOT_SUPPORTED_ON_STACK` | 5 | `configuration` | Browser login is not enabled on this Keboola stack, or the code path only understands static Storage tokens (`kbc-session://` sentinel rejected) |
| `AUTH_FLOW_TIMEOUT` | 4 | `authentication` | The PKCE callback or device-authorization poll deadline elapsed before the user completed login |
| `AUTH_FLOW_DENIED` | 3 | `authentication` | The user (or the authorization server) denied the login request |
| `AUTH_FLOW_EXPIRED` | 1 | `authentication` | The device code / authorization code expired before it was used |
| `AUTH_BROWSER_UNAVAILABLE` | 1 | `authentication` | No usable browser was found for the PKCE loopback flow (falls back to device authorization) |
| `AUTH_STATE_MISMATCH` | 1 | `authentication` | The PKCE callback's `state` parameter did not match the one generated at login start |
| `SESSION_EXPIRED` | 3 | `authentication` | The programmatic-auth session's refresh token expired or was revoked; run `kbagent auth login` again |
| `SESSION_NOT_FOUND` | 3 | `authentication` | No programmatic-auth session is persisted for this stack; run `kbagent auth login` |
| `AUTH_MFA_INVALID` | 3 | `authentication` | `auth login-password` hit an MFA factor it cannot resolve without a browser (e.g. WebAuthn-only) -- use `kbagent auth login` for that account instead |

### Billing (Pay-As-You-Go)

| Code | Exit | Type | Description |
|---|---|---|---|
| `PAYG_NOT_AVAILABLE` | 1 | `configuration` | The project does not have the `pay-as-you-go` feature, so it has no credit balance; the billing host may not even resolve on this stack |

### Merge requests

| Code | Exit | Type | Description |
|---|---|---|---|
| `MR_NOT_READY_TO_MERGE` | 1 | `conflict` | Merge answered 409 with `storage.mergeRequests.notReadyToMerge`: a project merge lock is held, the MR is in a state that cannot merge, or another MR in the project is already processing. Transient -- retryable |
| `MR_MERGE_CONFLICT` | 1 | `conflict` | Merge answered 409 with `storage.mergeRequests.validation` (or, on older stacks, no code): configurations changed on both branches; `details.api_error_params.errors` lists them. Not retryable -- resolve the conflicts and merge again |

### Updates

| Code | Exit | Type | Description |
|---|---|---|---|
| `UPDATED_ACROSS_MAJOR` | 6 | `authorization` | After an automatic update to a higher major version (for example from 0.x to 1.x), the first write or destructive command was refused once, with exit code 6. Every later command runs. Reads before that command run and get the same code as a `warnings[]` entry. Admin commands and an update started with `kbagent update --major` do not refuse. See [migrate-to-1.0.md](migrate-to-1.0.md) |
| `UPDATED_ACROSS_BREAKING` | 1 | `api` | Only a `warnings[]` entry, in the first command after an update, never an error exit: the update crossed versions with `BREAKING:` changelog notes. `versions` lists them. `kbagent changelog --full` shows the notes |
