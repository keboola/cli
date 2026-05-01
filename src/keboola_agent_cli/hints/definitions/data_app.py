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
            "Manage token is read from KBC_MANAGE_API_TOKEN env var or interactive "
            "hidden prompt; never persisted, never logged.",
        ],
    )
)
