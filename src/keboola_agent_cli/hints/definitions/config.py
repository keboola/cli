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
                        "config_id": None,
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
                    result_var=None,
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
