"""Hint definitions for config commands (list, detail, search, rename)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── config list ────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.list",
        description="List configurations from connected projects",
        steps=[
            HintStep(
                comment=(
                    "List all components with their configurations. "
                    "When --include-rows is set, switch to "
                    "list_components_with_configs(include=configuration,rows) "
                    "so each config carries its full body and rows "
                    "(significantly larger payload)."
                ),
                client=ClientCall(
                    method="list_components_with_configs",
                    args={
                        "component_type": "{component_type}",
                        "branch_id": "{branch}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="list_configs",
                    args={
                        "aliases": "{project}",
                        "component_type": "{component_type}",
                        "component_id": "{component_id}",
                        "branch_id": "{branch}",
                        "include_rows": "{include_rows}",
                    },
                ),
            ),
        ],
        notes=[
            "Each component in the response has a 'configurations' list.",
            "Service layer returns {'configs': [...], 'errors': [...]} with flattened results.",
            "Without --include-rows the call uses list_components (summary only: "
            "name/description/component/last_modified/folder) -- the default.",
            "With --include-rows the service switches to list_components_with_configs "
            "(include=configuration,rows) so each row includes the full body. Payload "
            "size grows proportionally to configuration complexity -- use only when "
            "you need the bodies.",
        ],
    )
)

# ── config detail ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.detail",
        description=(
            "Show detailed information about one or many configurations. "
            "With --config-id: single-config mode (unchanged shape). "
            "Without --config-id: bulk mode -- every config under "
            "--component-id, optionally across many projects."
        ),
        steps=[
            HintStep(
                comment=(
                    "Single-config mode (--config-id set): fetch the config detail. "
                    "The state field is already part of the response; --with-state "
                    "triggers an explicit get_config_state call to refresh it."
                ),
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="detail",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="get_config_detail",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                        "with_state": "{with_state}",
                    },
                ),
            ),
            HintStep(
                comment=(
                    "Bulk mode (no --config-id): list components with configs and "
                    "filter to --component-id. One HTTP request per project returns "
                    "every config body + rows (+ state when include_state=True). "
                    "For many projects, use ConfigService.get_config_detail with "
                    "aliases=[...] for parallel fan-out via _run_parallel."
                ),
                client=ClientCall(
                    method="list_components_with_configs",
                    args={
                        "branch_id": "{branch}",
                        "include_state": "{with_state}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="get_config_detail",
                    args={
                        "alias": "{project}",
                        "aliases": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "None",
                        "branch_id": "{branch}",
                        "with_state": "{with_state}",
                    },
                ),
            ),
        ],
        notes=[
            "Single-config JSON shape preserved exactly for backward compat "
            "(callers parsing detail.id, detail.configuration, etc. are unaffected).",
            "Bulk mode returns {'configs': [...], 'errors': [...]} with per-row "
            "project_alias -- identical envelope to config list, storage tables.",
            "--with-state in bulk mode: include=state is added to the listing call "
            "(still a single request per project, no N+1).",
            "--config-id + multiple --project is rejected (exit 2) -- a single config "
            "lives in one project.",
        ],
    )
)

# ── config search ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.search",
        description="Search through configuration bodies across projects",
        steps=[
            HintStep(
                comment="Search configurations for a pattern",
                client=ClientCall(
                    method="list_components",
                    args={
                        "component_type": "{component_type}",
                        "branch_id": "{branch}",
                    },
                    result_var="components",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="search_configs",
                    args={
                        "query": "{query}",
                        "aliases": "{project}",
                        "component_type": "{component_type}",
                        "component_id": "{component_id}",
                        "ignore_case": "{ignore_case}",
                        "use_regex": "{regex}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer returns raw components — you need to search through "
            "configuration JSON bodies yourself.",
            "Service layer does the full-text search and returns "
            "{'matches': [...], 'errors': [...], 'stats': {...}}.",
        ],
    )
)

# ── config rename ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.rename",
        description="Rename a configuration (update name via API + local sync dir)",
        steps=[
            HintStep(
                comment="Rename configuration via API",
                client=ClientCall(
                    method="update_config",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "name": "{name}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="rename_config",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "name": "{name}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Only the name is updated; configuration content is unchanged.",
            "If a local sync directory exists, the folder is renamed and "
            "manifest.json is updated automatically.",
        ],
    )
)

# ── config variables-set ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.variables-set",
        description="Assign variables to a config (auto-creates keboola.variables if absent)",
        steps=[
            HintStep(
                comment="Set variable values on a config; creates or updates the backing keboola.variables",
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="parent",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="VariablesService",
                    service_module="variables_service",
                    method="set_variables",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "variables": "{variable}",
                        "replace": "{replace}",
                        "variables_id": "{variables_id}",
                        "values_id": "{values_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "--var takes KEY=VALUE, repeatable. Prefix KEY with # to auto-encrypt as secret.",
            "Without --variables-id, a sibling keboola.variables config named "
            "'<parent-name>-vars' is created on first call and the parent is linked.",
            "Subsequent calls update the same default row; --replace overwrites instead of merging.",
        ],
    )
)

# ── config variables-get ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.variables-get",
        description="Read variable values attached to a config",
        steps=[
            HintStep(
                comment="Resolve variables_id + values_id from parent, fetch the row",
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="parent",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="VariablesService",
                    service_module="variables_service",
                    method="get_variables",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Response: {'linked': bool, 'variables_id': str|None, 'values_id': str|None, "
            "'values': {name: value}}.",
            "linked=False means the parent has no variables_id set.",
        ],
    )
)

# ── config variables-clear ─────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.variables-clear",
        description="Unlink variables from a config (underlying keboola.variables is NOT deleted)",
        steps=[
            HintStep(
                comment="Strip variables_id + variables_values_id from parent config",
                client=ClientCall(
                    method="update_config",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "configuration": "<parent config with variables_id/variables_values_id removed>",
                        "branch_id": "{branch}",
                        "change_description": "Unlinked variables via kbagent",
                    },
                    result_var="result",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="VariablesService",
                    service_module="variables_service",
                    method="clear_variables",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Clear unlinks only -- the keboola.variables config remains in the project "
            "(may be shared). Use 'kbagent config delete' to remove it explicitly.",
        ],
    )
)

# ── config metadata-list ───────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.metadata-list",
        description="List all metadata entries on a configuration",
        steps=[
            HintStep(
                comment="List configuration metadata",
                client=ClientCall(
                    method="list_config_metadata",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="entries",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="list_config_metadata",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=["Each entry has: id, key, value, provider, timestamp."],
    )
)

# ── config get-metadata ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.get-metadata",
        description="Read a single metadata value by key from a configuration",
        steps=[
            HintStep(
                comment="Get single metadata value",
                client=ClientCall(
                    method="list_config_metadata",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="entries",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="get_config_metadata_value",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "key": "{key}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=["Raises NOT_FOUND (exit 1) if key is absent."],
    )
)

# ── config set-metadata ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.set-metadata",
        description="Set (upsert) a metadata key/value on a configuration",
        steps=[
            HintStep(
                comment="Upsert metadata entry on configuration",
                client=ClientCall(
                    method="set_config_metadata",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "entries": "[({key}, {value})]",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="set_config_metadata",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "key": "{key}",
                        "value": "{value}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ── config delete-metadata ─────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.delete-metadata",
        description="Delete a configuration metadata entry by its numeric ID",
        steps=[
            HintStep(
                comment="Delete metadata entry by ID",
                client=ClientCall(
                    method="delete_config_metadata",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "metadata_id": "{metadata_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="_",
                    result_hint="None",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="delete_config_metadata",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "metadata_id": "{metadata_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=["Use metadata-list first to find the numeric metadata_id."],
    )
)

# ── config set-folder ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.set-folder",
        description="Set the folder (KBC.configuration.folderName) on a configuration",
        steps=[
            HintStep(
                comment="Write KBC.configuration.folderName metadata",
                client=ClientCall(
                    method="set_config_metadata",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "entries": "[('KBC.configuration.folderName', {name})]",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="set_config_folder",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "folder_name": "{name}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Folder names appear in the Keboola UI to group configurations.",
            "config list already shows folder names in the 'folder' column.",
        ],
    )
)

# ── config set-default-bucket ──────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.row-create",
        description="Create a new configuration row under a parent configuration",
        steps=[
            HintStep(
                comment="Create configuration row via Storage API POST",
                client=ClientCall(
                    method="create_config_row",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "name": "{name}",
                        "configuration": "{configuration}",
                        "description": "{description}",
                        "is_disabled": "{is_disabled}",
                        "branch_id": "{branch}",
                    },
                    result_var="row",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="create_config_row",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "name": "{name}",
                        "description": "{description}",
                        "configuration": "{configuration}",
                        "is_disabled": "{is_disabled}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "configuration defaults to {} if omitted.",
            "The returned dict includes the new row 'id' assigned by the API.",
            "Rows are sub-units of a configuration; one config may have many rows.",
            "is_disabled=True creates the row in disabled state (excluded from job runs).",
        ],
    )
)

# ── config row-update ──────────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.row-update",
        description="Update an existing configuration row (metadata and/or content)",
        steps=[
            HintStep(
                comment=(
                    "Fetch current row config if --merge or --set is used, then write back. "
                    "For --dry-run, compare and return diff without writing."
                ),
                client=ClientCall(
                    method="update_config_row",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "row_id": "{row_id}",
                        "name": "{name}",
                        "description": "{description}",
                        "configuration": "{configuration}",
                        "is_disabled": "{is_disabled}",
                        "branch_id": "{branch}",
                    },
                    result_var="row",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="update_config_row",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "row_id": "{row_id}",
                        "name": "{name}",
                        "description": "{description}",
                        "configuration": "{configuration}",
                        "set_paths": "{set}",
                        "merge": "{merge}",
                        "dry_run": "{dry_run}",
                        "is_disabled": "{is_disabled}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "--set PATH=VALUE is repeatable; implies --merge (fetches current row first).",
            "--merge deep-merges the provided configuration into the existing row config.",
            "--dry-run returns {'dry_run': True, 'changes': [...], 'old_configuration': {...}, "
            "'new_configuration': {...}} without writing.",
            "--is-disabled / --is-enabled toggle the row's enabled state (mutually exclusive).",
            "At least one of --name, --description, --configuration, --set, --is-disabled, "
            "or --is-enabled must be provided.",
        ],
    )
)

# ── config row-delete ──────────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.row-delete",
        description="Delete a configuration row by ID",
        steps=[
            HintStep(
                comment="Delete configuration row via Storage API DELETE",
                client=ClientCall(
                    method="delete_config_row",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "row_id": "{row_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="_",
                    result_hint="None",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="delete_config_row",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "row_id": "{row_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Destructive: irreversible deletion of the row from the Storage API.",
            "404 from API surfaces as KeboolaApiError(NOT_FOUND); deleting a "
            "non-existent row is treated as an error, not idempotent success.",
            "Branch-aware: pass branch_id to delete from a dev branch.",
        ],
    )
)

# ── config oauth-url ───────────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.oauth-url",
        description=(
            "Generate a short-lived OAuth authorization URL for a component configuration. "
            "The user opens this URL in a browser to grant access."
        ),
        steps=[
            HintStep(
                comment=(
                    "Create a short-lived component-scoped Storage API token, "
                    "then build https://external.keboola.com/oauth/index.html URL."
                ),
                client=ClientCall(
                    method="get_oauth_url",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "redirect_url": "{redirect_url}",
                    },
                    result_var="url",
                    result_hint="str",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="get_oauth_url",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "redirect_url": "{redirect_url}",
                    },
                ),
            ),
        ],
        notes=[
            "Response: {'url': str, 'component_id': str, 'config_id': str, 'project_alias': str}.",
            "The short-lived token embedded in the URL expires in 1 hour.",
            "Only applicable to OAuth-requiring components (e.g. keboola.ex-google-drive, "
            "keboola.ex-google-analytics-v4, keboola.ex-gmail).",
            "Call this AFTER creating the configuration, not before.",
            "redirect_url adds a returnUrl query param so the OAuth wizard returns to a "
            "custom URL after the flow completes.",
            "Requires a MASTER Storage API token on the project (canManageTokens). "
            "Non-master tokens fail with MISSING_MASTER_TOKEN (exit 3) on a fail-fast "
            "pre-flight check before any HTTP write happens.",
        ],
    )
)

# ── config set-default-bucket ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="config.set-default-bucket",
        description="Set or clear configuration.storage.output.default_bucket on a config",
        steps=[
            HintStep(
                comment="Fetch current config so we can read-modify-write the storage.output branch",
                client=ClientCall(
                    method="get_config_detail",
                    args={
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="current",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="set_default_bucket",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "bucket": "{bucket}",
                        "clear": "{clear}",
                        "dry_run": "{dry_run}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "--bucket and --clear are mutually exclusive; exactly one is required.",
            "Setting to the current value (or clearing when already absent) is a no-op: "
            "service returns {'changed': False} without writing.",
            "Clear leaves an empty 'storage.output' dict if all sibling keys were already absent -- "
            "this is intentional and matches the inverse of set_nested_value's parent-creation behavior.",
            "Bucket IDs are validated server-side; expect a Storage API error for malformed values.",
        ],
    )
)

# ── config new --push (one-shot remote create) ────────────────────────────────
#
# Scaffold-only mode (no --push) has no hint -- it's a local filesystem
# operation with no API mapping. The hint emits only when --push is set; the
# command layer guards the emit_hint() call accordingly.

HintRegistry.register(
    CommandHint(
        cli_command="config.new",
        description=(
            "Create a new configuration remotely via the Storage API "
            "(`config new --push`). Scaffold-only mode is a local filesystem "
            "operation and not represented in this hint."
        ),
        steps=[
            HintStep(
                comment="Create configuration via Storage API POST",
                client=ClientCall(
                    method="create_config",
                    args={
                        "component_id": "{component_id}",
                        "name": "{name}",
                        "configuration": "{configuration}",
                        "description": "{description}",
                        "branch_id": "{branch}",
                    },
                    result_var="config",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="create_config",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "name": "{name}",
                        "description": "{description}",
                        "configuration": "{configuration}",
                        "branch_id": "{branch}",
                        "dry_run": "{dry_run}",
                        "validate": "not {no_validate}",
                    },
                ),
            ),
        ],
        notes=[
            "configuration defaults to {} (empty shell) when omitted -- FIIA's "
            "'create-then-patch' pattern. Validation auto-skips for the empty case.",
            "Without --no-validate, the body is validated against the component's "
            "AI Service JSON schema before POSTing (fail-closed: exit 5 on mismatch). "
            "Skips gracefully when the AI Service has no schema for the component.",
            "The returned dict includes the new config 'id' assigned by the API "
            "plus 'project_alias', 'branch_id', and 'validation_status' annotations.",
            "The client-mode snippet bypasses validation; the service-mode snippet runs it.",
        ],
    )
)
