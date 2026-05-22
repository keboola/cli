"""Hint definitions for the ``data-app`` command group."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── data-app list ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.list",
        description=(
            "List data apps across one or more registered projects. "
            "Merges the Data Science /apps thin index with each app's "
            "Storage config name (one extra GET per project)."
        ),
        steps=[
            HintStep(
                comment=(
                    "Fetch the thin /apps index from the Data Science API "
                    "(scoped to the token's project) and join with Storage "
                    "config names."
                ),
                client=ClientCall(
                    method="list_apps",
                    args={},
                    client_type="data_science",
                    result_var="apps",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="list_data_apps",
                    args={
                        "aliases": "{project}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses DataScienceClient(stack_url, token) -- new client class "
            "added in 0.27.0; do not confuse with KeboolaClient or AiServiceClient.",
            "Service envelope: {'apps': [...], 'errors': [...]} with one row per app.",
        ],
    )
)


# ── data-app detail ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.detail",
        description=(
            "Show merged Data Science + Storage view of one data app. "
            "Reads the deployment record and the linked Storage config "
            "(the configId on the deployment)."
        ),
        steps=[
            HintStep(
                comment=(
                    "Two GETs: the Data Science deployment record and the "
                    "linked Storage config. Service merges them and redacts "
                    "the encrypted git PAT in human mode."
                ),
                client=ClientCall(
                    method="get_app",
                    args={"app_id": "{app_id}"},
                    client_type="data_science",
                    result_var="app",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="get_data_app",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "config_version_storage and config_version_deployed often differ -- "
            "the deployed pin is stale until the next `data-app deploy`.",
        ],
    )
)


# ── data-app create ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.create",
        description=(
            "Create + configure + deploy a data app in one call. "
            "Encapsulates the §9 redeploy contract so callers cannot pin "
            "to the empty-shell v2."
        ),
        steps=[
            HintStep(
                comment=(
                    "End-to-end: POST /apps shell, encrypt git PAT (private "
                    "repo), PUT Storage config with auto-injected "
                    "parameters.id back-pointer, PATCH deploy with the "
                    "{desiredState, configVersion, restartIfRunning} trio."
                ),
                client=ClientCall(
                    method="create_app",
                    args={
                        "type_": "{type_}",
                        "name": "{name}",
                        "description": "{description}",
                        # Placeholder rendered as a string literal so the
                        # snippet parses as valid Python. The actual shell
                        # body is built by the service in production --
                        # the client-side hint exists for illustration.
                        "config": '"<minimal shell built by the service>"',
                        "branch_id": "{branch}",
                    },
                    client_type="data_science",
                    result_var="shell",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="create_data_app",
                    args={
                        "alias": "{project}",
                        "name": "{name}",
                        "description": "{description}",
                        "slug": "{slug}",
                        "git_repo": "{git_repo}",
                        "git_branch": "{git_branch}",
                        "git_public": "{git_public}",
                        "git_username": "{git_username}",
                        # Git PAT input modes (mutually exclusive). The
                        # service expects either a plaintext PAT (the
                        # service re-encrypts it under THIS project's KMS)
                        # or a project-scoped ciphertext. The renderer
                        # auto-quotes the {git_pat_env} placeholder, so the
                        # rendered snippet becomes ``os.environ["VAR"]``.
                        "git_pat_plaintext": "os.environ[{git_pat_env}]",
                        "git_pat_encrypted": "{git_pat_encrypted}",
                        "auth": "{auth}",
                        "size": "{size}",
                        "auto_suspend_after_seconds": "{auto_suspend}",
                        "type_": "{type_}",
                        "branch_id": "{branch}",
                        "deploy": "not {no_deploy}",
                        "wait": "{wait}",
                        "timeout_seconds": "{timeout}",
                        "keep_on_failure": "{keep_on_failure}",
                        "dry_run": "{dry_run}",
                    },
                ),
            ),
        ],
        notes=[
            "Encryption is per-project KMS -- ciphertext does not cross projects "
            "(writeup §8). Always pass the plaintext PAT via env var so the service "
            "re-encrypts under the target project's key.",
            "On failure between POST and PUT, the orphan shell is cleaned up "
            "automatically unless --keep-on-failure is set.",
            "--dry-run prints all three request bodies without making any API call.",
            "Service-call arguments above include all CLI flags. Drop the "
            "git_* keys when the repo is public (--git-public).",
        ],
    )
)


# ── data-app deploy ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.deploy",
        description=("Deploy the latest Storage config to a data app -- the §9 redeploy contract."),
        steps=[
            HintStep(
                comment=(
                    "GET app -> read configId. GET Storage config -> read "
                    "version. PATCH /apps/{id} with the trio "
                    "{desiredState=running, configVersion, restartIfRunning=true}. "
                    "Sending configVersion alone returns HTTP 422."
                ),
                client=ClientCall(
                    method="patch_app",
                    args={
                        "app_id": "{app_id}",
                        "desired_state": '"running"',
                        "config_version": "{config_version}",
                        "restart_if_running": "True",
                    },
                    client_type="data_science",
                    result_var="deployed",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="deploy_data_app",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "config_version": "{config_version}",
                        "wait": "{wait}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Without --config-version, the service reads the latest from "
            "Storage and pins to it. With --config-version, the caller can "
            "deploy an older version (rollback).",
            "Always sends restart_if_running=True -- HTTP 422 otherwise.",
        ],
    )
)


# ── data-app start ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.start",
        description=(
            "Wake an auto-suspended data app at its currently-pinned "
            "configVersion. Distinct from deploy: does NOT bump the version."
        ),
        steps=[
            HintStep(
                comment=(
                    "PATCH /apps/{id} with {desiredState=running, "
                    "restartIfRunning=true} -- no configVersion -- so the "
                    "platform reuses the currently-pinned version."
                ),
                client=ClientCall(
                    method="patch_app",
                    args={
                        "app_id": "{app_id}",
                        "desired_state": '"running"',
                        "restart_if_running": "True",
                    },
                    client_type="data_science",
                    result_var="deployed",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="start_data_app",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "wait": "{wait}",
                    },
                ),
            ),
        ],
        notes=[
            "Use this to wake an app after autoSuspendAfterSeconds expired. "
            "For a code change, use `data-app deploy` instead.",
        ],
    )
)


# ── data-app stop ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.stop",
        description="Stop a running data app (preserves URL and Storage config).",
        steps=[
            HintStep(
                comment="PATCH /apps/{id} with {desiredState=stopped}.",
                client=ClientCall(
                    method="patch_app",
                    args={
                        "app_id": "{app_id}",
                        "desired_state": '"stopped"',
                    },
                    client_type="data_science",
                    result_var="deployed",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="stop_data_app",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "wait": "{wait}",
                    },
                ),
            ),
        ],
    )
)


# ── data-app delete ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.delete",
        description=(
            "Delete the deployment AND the Storage config (cascade). URL is permanently retired."
        ),
        steps=[
            HintStep(
                comment=(
                    "DELETE /apps/{id}. Returns HTTP 202; cascades to "
                    "Storage config delete. There is no recovery."
                ),
                client=ClientCall(
                    method="delete_app",
                    args={"app_id": "{app_id}"},
                    client_type="data_science",
                    result_var="result",
                    result_hint="None",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="delete_data_app",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                    },
                ),
            ),
        ],
        notes=[
            "If you only want to stop the app temporarily, use `data-app stop` "
            "instead -- it preserves the URL and config.",
        ],
    )
)


# ── data-app password ──────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.password",
        description=(
            "Retrieve the auto-generated simpleAuth password for a "
            "password-gated data app. Requires both Storage and Manage tokens."
        ),
        steps=[
            HintStep(
                comment=(
                    "GET /apps/{id}/password requires both X-StorageApi-Token "
                    "(already on the client) and X-KBC-ManageApiToken (passed "
                    "per-call so it never lives on the persistent client)."
                ),
                client=ClientCall(
                    method="get_app_password",
                    args={
                        "app_id": "{app_id}",
                        "manage_token": 'os.environ["KBC_MANAGE_API_TOKEN"]',
                    },
                    client_type="data_science",
                    result_var="payload",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="get_data_app_password",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        # Same env-var resolution as the client-side hint
                        # above. The literal string ``os.environ[...]`` is
                        # emitted verbatim into the rendered snippet (no
                        # placeholder substitution) so it parses as a
                        # subscript expression, not a comparison op.
                        "manage_token": 'os.environ["KBC_MANAGE_API_TOKEN"]',
                    },
                ),
            ),
        ],
        notes=[
            "Password is auto-generated at app create time and cannot be rotated. "
            "Delete + recreate the app to mint a new one (writeup §11.2).",
            "Manage token is read from an interactive hidden prompt by default "
            "(since kbagent v0.28.0). For non-interactive runners, the calling "
            "kbagent invocation must pass `--allow-env-manage-token` to opt in "
            "to KBC_MANAGE_API_TOKEN env-var resolution. Never persisted, never "
            "logged.",
        ],
    )
)


# ── data-app logs ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.logs",
        description=(
            "Tail container logs for a deployed data app. Returns the "
            "full spin-up trace (git clone, uv install, supervisord, "
            "runtime stack traces) as plain text from the Data Science "
            "/apps/{id}/logs/tail endpoint."
        ),
        steps=[
            HintStep(
                comment=(
                    "GET /apps/{id}/logs/tail. Plain-text response "
                    "(response.text, not response.json). lines and "
                    "since are mutually exclusive on the server."
                ),
                client=ClientCall(
                    method="tail_app_logs",
                    args={
                        "app_id": "{app_id}",
                        "lines": "{lines}",
                        "since": "{since}",
                    },
                    client_type="data_science",
                    result_var="logs",
                    result_hint="str",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="get_app_logs",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "lines": "{lines}",
                        "since": "{since}",
                    },
                ),
            ),
        ],
        notes=[
            "App must be running or recently-stopped; never-started apps "
            "return HTTP 400 'App X is not running'. Run "
            "`kbagent data-app start --project P --app-id ID` first.",
            "Plain-text response (str), not JSON. The service envelope "
            "wraps the text with the request echo (lines_requested, "
            "since_requested) + a lines_returned count.",
            "Default --lines 500. Pass --lines 0 (CLI) or lines=None "
            "(service / client) to opt into the full current container "
            "buffer with no params sent.",
            "The log buffer can echo runtime secrets the app printed to "
            "stdout/stderr (tracebacks, debug os.environ dumps). The "
            "envelope is reproduced verbatim with no masking; consider "
            "secret hygiene before piping --json output into AI agent "
            "context.",
        ],
    )
)


# ── data-app secrets set ──────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.secrets-set",
        description=(
            "Encrypt and write '#'-prefixed secrets to the linked Storage "
            "config. Read-modify-write so unrelated keys under "
            "parameters.dataApp are preserved bit-identical."
        ),
        steps=[
            HintStep(
                comment=(
                    "Resolve configId via /apps/{id}, encrypt every plaintext "
                    "value under THIS project's KMS, then PUT the full config "
                    "with the new secrets sub-dict. Storage merge=True is "
                    "shallow at the top level only -- relying on it would "
                    "clobber sibling keys; read-modify-write is the only "
                    "correct path."
                ),
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": '"keboola.data-apps"',
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    client_type="storage",
                    result_var="current_config",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="set_data_app_secrets",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "secrets": "{secret}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Encryption is per-project KMS -- ciphertext does not cross "
            "projects (writeup §8). Pre-encrypted KBC::* values are rejected "
            "by --secret to prevent stale-ciphertext footguns; pass them via "
            "--secrets-file for advanced flows.",
            "The runtime exposes each key as an env var with '#' stripped, "
            "'-' replaced with '_', and uppercased ('#my-api-key' -> "
            "'MY_API_KEY'). Setting a key whose env-var name collides with "
            "KBC_TOKEN / KBC_URL is silently shadowed at runtime.",
            "Adding a secret bumps the Storage version but the running "
            "container keeps the OLD config until 'data-app deploy' runs.",
        ],
    )
)


# ── data-app secrets list ─────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.secrets-list",
        description=(
            "List the keys in parameters.dataApp.secrets with their derived "
            "runtime env-var names. Never echoes the encrypted ciphertext "
            "and never decrypts."
        ),
        steps=[
            HintStep(
                comment=(
                    "Resolve configId via /apps/{id}, then GET the linked "
                    "Storage config and read parameters.dataApp.secrets."
                ),
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": '"keboola.data-apps"',
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    client_type="storage",
                    result_var="current_config",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="list_data_app_secrets",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Default output omits ciphertext fingerprint; pass --show-fingerprint to include it.",
        ],
    )
)


# ── data-app secrets get ──────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.secrets-get",
        description=(
            "Show ONE key from parameters.dataApp.secrets (key with or "
            "without a leading '#'). An ENCRYPTED secret returns metadata "
            "only -- the Encryption API is one-way and the CLI never echoes "
            "the decrypted value under any branch. A PLAIN (unencrypted) "
            "env-var value returns its literal value (encrypted=false), "
            "which is already visible via config detail."
        ),
        steps=[
            HintStep(
                comment=(
                    "GET the Storage config, look up one key in "
                    "parameters.dataApp.secrets. For an encrypted secret "
                    "return metadata only (the ciphertext fingerprint is the "
                    "first 8 chars of the payload after the KBC::* prefix); "
                    "for a plain value return the value verbatim."
                ),
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": '"keboola.data-apps"',
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    client_type="storage",
                    result_var="current_config",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="get_data_app_secret",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "key": "{key}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "NOT_FOUND on absent key does not enumerate sibling keys -- "
            "avoid leaking neighbour presence to a caller that knows only "
            "one key's name.",
        ],
    )
)


# ── data-app secrets remove ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.secrets-remove",
        description=(
            "Remove one or more app-runtime secrets. Idempotent: removing "
            "a non-existent key is exit 0 with removed=0."
        ),
        steps=[
            HintStep(
                comment=(
                    "Resolve configId, GET the Storage config, drop the "
                    "named keys from parameters.dataApp.secrets, PUT the "
                    "full body back. Same read-modify-write contract as "
                    "secrets set."
                ),
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": '"keboola.data-apps"',
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    client_type="storage",
                    result_var="current_config",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="DataAppService",
                    service_module="data_app_service",
                    method="remove_data_app_secrets",
                    args={
                        "alias": "{project}",
                        "app_id": "{app_id}",
                        "keys": "{key}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Destructive operation: removing a secret can break the running "
            "app at the next deploy if it depends on the value. Run "
            "`data-app deploy` after the remove to roll the new config.",
        ],
    )
)


# ── data-app validate-repo ────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="data-app.validate-repo",
        description=(
            "Pre-flight check that a git repo follows the Keboola data-app "
            "Golden Rule. Walks the repo via the GitHub Contents + Trees "
            "API; emits BLOCKING / WARN / OK with help-doc citations."
        ),
        steps=[
            HintStep(
                comment=(
                    "ONE GET /repos/{owner}/{repo}/git/trees/{ref}?"
                    "recursive=1 to walk the tree, then up to 3 GET "
                    "/repos/.../contents/{path} for setup.sh / "
                    "pyproject.toml / nginx-app port match. Total <=5 "
                    "GitHub calls regardless of repo size. validate-repo "
                    "uses GitHubContentsClient (not a Keboola client); "
                    "prefer the service-layer snippet."
                ),
                client=ClientCall(
                    method="get_tree_recursive",
                    args={
                        "owner": '"<owner>"',
                        "repo": '"<repo>"',
                        "ref": "{git_branch}",
                    },
                    client_type="github",
                    result_var="tree",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="RepoValidateService",
                    service_module="repo_validate_service",
                    method="validate_repo",
                    args={
                        "git_repo": "{git_repo}",
                        "git_branch": "{git_branch}",
                        "git_public": "{git_public}",
                        "type_": "{type_}",
                    },
                ),
            ),
        ],
        notes=[
            "Public GitHub Contents API is 60/hour unauth; pass "
            "--git-pat-env to use a PAT and raise the limit to 5000/hour.",
            "Currently restricted to --type python-js. streamlit / python / "
            "r layouts tracked as a follow-up.",
        ],
    )
)
