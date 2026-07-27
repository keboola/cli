"""Component discovery and scaffold generation service.

Provides component search (via AI Service suggestions or Storage API listing),
detailed component inspection, and configuration scaffold generation for
local-first development workflows.
"""

import logging
from collections.abc import Callable
from typing import Any

import yaml

from ..ai_client import AiServiceClient
from ..auth.sentinel import require_static_token
from ..config_store import ConfigStore
from ..constants import SECRET_PLACEHOLDER
from ..errors import ConfigError, KeboolaApiError
from ..models import ComponentDetail, ComponentSuggestion, ProjectConfig
from .base import BaseService, ClientFactory
from .org_service import slugify

logger = logging.getLogger(__name__)

AiClientFactory = Callable[[str, str], AiServiceClient]


def default_ai_client_factory(stack_url: str, token: str) -> AiServiceClient:
    """Create an AiServiceClient with the given stack URL and token.

    Static-token-only (v1 scope is Storage + Manage) -- fails fast on a
    session sentinel rather than sending it as a literal credential.
    """
    require_static_token(token, feature="The Keboola AI Service")
    return AiServiceClient(stack_url=stack_url, token=token)


# --- Component type detection ---

_SQL_TRANSFORMATION_FRAGMENTS = (
    "snowflake-transformation",
    "synapse-transformation",
    "redshift-transformation",
    "bigquery-transformation",
)

_PYTHON_TRANSFORMATION_FRAGMENT = "python-transformation"
_CUSTOM_PYTHON_APP_ID = "kds-team.app-custom-python"
_FLOW_COMPONENT_IDS = ("keboola.flow",)


def _detect_component_category(component_id: str) -> str:
    """Determine scaffold category from component_id.

    Returns one of: sql_transformation, python_transformation,
    custom_python, flow, generic.
    """
    for fragment in _SQL_TRANSFORMATION_FRAGMENTS:
        if fragment in component_id:
            return "sql_transformation"
    if _PYTHON_TRANSFORMATION_FRAGMENT in component_id:
        return "python_transformation"
    if component_id == _CUSTOM_PYTHON_APP_ID:
        return "custom_python"
    if component_id in _FLOW_COMPONENT_IDS:
        return "flow"
    return "generic"


# --- Scaffold file builders ---


def _build_config_yml(detail: ComponentDetail, name: str) -> str:
    """Generate _config.yml content with inline comments.

    Priority for parameters section:
    1. First rootConfigurationExample's parameters key
    2. Schema-derived placeholders from configurationSchema
    3. Empty parameters dict
    """
    lines: list[str] = []

    # Header comments
    lines.append(f"# Component: {detail.component_name} ({detail.component_id})")
    lines.append(f"# Type: {detail.component_type}")
    if detail.documentation_url:
        lines.append(f"# Documentation: {detail.documentation_url}")
    lines.append("#")
    lines.append("# NOTE: config_id will be assigned by Keboola on first push")

    # Version and name
    lines.append("version: 2")
    lines.append(f'name: "{name}"')
    lines.append("description: |")
    lines.append("  TODO: describe this configuration")
    lines.append("")

    # Parameters section
    params = _resolve_parameters(detail)
    if params:
        params_yaml = yaml.dump({"parameters": params}, default_flow_style=False, sort_keys=False)
        # Post-process secret placeholders with inline comments
        processed_lines: list[str] = []
        for line in params_yaml.splitlines():
            if SECRET_PLACEHOLDER in line and "# encrypted by Keboola on push" not in line:
                line = f"{line}  # encrypted by Keboola on push"
            processed_lines.append(line)
        lines.extend(processed_lines)
    else:
        lines.append("parameters: {}")

    # Storage mappings based on component flags
    storage_lines = _build_storage_section(detail)
    if storage_lines:
        lines.append("")
        lines.extend(storage_lines)

    # Configuration rows hint
    if detail.configuration_row_schema:
        lines.append("")
        lines.append("# This component uses configuration rows. Add rows via 'rows/' subdirectory.")

    # _keboola metadata (component_id required for sync push, config_id assigned on first push)
    lines.append("")
    lines.append("_keboola:")
    lines.append(f"  component_id: {detail.component_id}")

    lines.append("")
    return "\n".join(lines)


def _resolve_parameters(detail: ComponentDetail) -> dict[str, Any]:
    """Extract parameters from examples or schema, applying secret masking."""
    # Priority 1: examples
    if detail.root_configuration_examples:
        first_example = detail.root_configuration_examples[0]
        raw_params = first_example.get("parameters", {})
        if raw_params:
            return _mask_secrets(raw_params)

    # Priority 2: schema
    schema = detail.configuration_schema
    if schema and schema.get("properties"):
        params_schema = schema.get("properties", {}).get("parameters", {})
        if params_schema and params_schema.get("properties"):
            return _generate_from_schema(params_schema)
        # If parameters is not nested, try top-level properties
        return _generate_from_schema(schema)

    # Priority 3: empty
    return {}


def _mask_secrets(obj: Any) -> Any:
    """Recursively replace secret values with SECRET_PLACEHOLDER."""
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            if key.startswith("#") or (isinstance(value, str) and value == "<secret>"):
                result[key] = SECRET_PLACEHOLDER
            else:
                result[key] = _mask_secrets(value)
        return result
    if isinstance(obj, list):
        return [_mask_secrets(item) for item in obj]
    return obj


def _generate_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Generate placeholder values from a JSON schema."""
    properties = schema.get("properties", {})
    result: dict[str, Any] = {}

    for prop_name, prop_schema in properties.items():
        prop_type = prop_schema.get("type", "string")

        if prop_name.startswith("#"):
            result[prop_name] = SECRET_PLACEHOLDER
        elif prop_type == "string":
            result[prop_name] = prop_schema.get("default", "")
        elif prop_type == "integer" or prop_type == "number":
            result[prop_name] = prop_schema.get("default", 0)
        elif prop_type == "boolean":
            result[prop_name] = prop_schema.get("default", False)
        elif prop_type == "array":
            result[prop_name] = prop_schema.get("default", [])
        elif prop_type == "object":
            nested = prop_schema.get("properties")
            if nested:
                result[prop_name] = _generate_from_schema(prop_schema)
            else:
                result[prop_name] = prop_schema.get("default", {})
        else:
            result[prop_name] = ""

    return result


def _build_storage_section(detail: ComponentDetail) -> list[str]:
    """Generate storage input/output mapping skeleton based on component flags."""
    flags = detail.component_flags
    lines: list[str] = []
    has_input = "genericDockerUI-tableInput" in flags
    has_output = "genericDockerUI-tableOutput" in flags

    if not has_input and not has_output:
        return lines

    lines.append("storage:")

    if has_input:
        lines.append("  input:")
        lines.append("    tables:")
        lines.append('      - source: "in.c-bucket.table"')
        lines.append('        destination: "input.csv"')

    if has_output:
        lines.append("  output:")
        lines.append("    tables:")
        lines.append('      - source: "output.csv"')
        lines.append('        destination: "out.c-bucket.table"')

    return lines


def _build_transform_sql(name: str) -> str:
    """Generate SQL transformation boilerplate."""
    return (
        "/* ===== BLOCK: 001-main ===== */\n"
        "/* ===== CODE: 001-query ===== */\n"
        "\n"
        "-- TODO: write your SQL transformation here\n"
        "-- Input tables are available as temporary tables\n"
        "-- Output tables will be created from SELECT results\n"
        "\n"
        "SELECT 1;\n"
    )


def _build_transform_py(name: str) -> str:
    """Generate Python transformation boilerplate."""
    return (
        "# ===== BLOCK: 001-main =====\n"
        "# ===== CODE: 001-script =====\n"
        "\n"
        "from keboola.component import CommonInterface\n"
        "\n"
        "ci = CommonInterface()\n"
        "\n"
        "# Read input tables\n"
        '# input_table = ci.get_input_table_definition_by_name("input.csv")\n'
        "# df = pd.read_csv(input_table.full_path)\n"
        "\n"
        "# Write output tables\n"
        '# output_table = ci.create_out_table_definition("output.csv")\n'
        "# df.to_csv(output_table.full_path, index=False)\n"
        "\n"
        'print("Transformation complete")\n'
    )


def _build_code_py() -> str:
    """Generate custom Python application boilerplate."""
    return (
        "import logging\n"
        "from keboola.component import CommonInterface\n"
        "\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "\n"
        "ci = CommonInterface()\n"
        "params = ci.configuration.parameters\n"
        "\n"
        "# TODO: implement your application logic here\n"
        "\n"
        'logging.info("Application complete")\n'
    )


def _build_pyproject_toml(component_id: str, name: str, packages: list[str] | None = None) -> str:
    """Generate pyproject.toml for custom Python apps."""
    slugified_name = slugify(name)
    deps_lines = ""
    if packages:
        formatted = ",\n".join(f'    "{pkg}"' for pkg in packages)
        deps_lines = f"\ndependencies = [\n{formatted},\n]\n"
    else:
        deps_lines = "\ndependencies = [\n    # Add your dependencies here\n]\n"

    return (
        "[project]\n"
        f'name = "{slugified_name}"\n'
        'version = "1.0.0"\n'
        'requires-python = ">=3.11"\n'
        f"{deps_lines}"
    )


def _build_flow_config_yml(name: str, component_id: str = "keboola.flow") -> str:
    """Generate a conditional-flow (keboola.flow) configuration YAML skeleton.

    IDs are strings; phases carry next[].goto transitions (a phase id or null)
    and tasks are typed (job/notification/variable).
    """
    lines = [
        f'name: "{name}"',
        "description: |",
        "  TODO: describe this flow",
        "phases:",
        '  - id: "phase-1"',
        '    name: "Phase 1"',
        "    next:",
        '      - id: "default"',
        "        goto: null",
        "tasks:",
        '  - id: "task-1"',
        '    name: "Task 1"',
        '    phase: "phase-1"',
        "    enabled: true",
        "    task:",
        "      type: job",
        '      componentId: "keboola.ex-http"',
        '      configId: "TODO"',
        "      mode: run",
    ]
    return "\n".join(lines) + "\n"


class ComponentService(BaseService):
    """Business logic for component discovery and scaffold generation.

    Supports two discovery modes:
    - AI-powered search via AiServiceClient (natural language query)
    - Storage API listing via KeboolaClient (component type filter)

    Scaffold generation creates ready-to-use configuration files based on
    component schema, examples, and type-specific templates.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        ai_client_factory: AiClientFactory | None = None,
    ) -> None:
        super().__init__(config_store, client_factory)
        self._ai_client_factory = ai_client_factory or default_ai_client_factory

    def list_components(
        self,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """List or search components across projects.

        Two modes of operation:
        - With ``query``: Uses AI Service to suggest components matching a
          natural language description. Enriches each suggestion with detail
          from get_component_detail(). Runs against first/default project.
        - Without ``query``: Uses Storage API list_components() across all
          resolved projects in parallel, returning unique components.

        Args:
            aliases: Project aliases to query. None means all projects.
            component_type: Optional filter by component type
                (extractor, writer, transformation, application).
            query: Natural language search query for AI-powered discovery.

        Returns:
            Dict with keys:
                - "components": list of component dicts
                - "errors": list of error dicts
        """
        if query:
            return self._list_via_ai(aliases, component_type, query)
        return self._list_via_storage(aliases, component_type)

    def get_component_detail(self, alias: str, component_id: str) -> dict[str, Any]:
        """Fetch detailed component documentation via AI Service.

        Args:
            alias: Project alias (used to derive stack URL and token).
            component_id: The component identifier (e.g. 'keboola.ex-aws-s3').

        Returns:
            Dict with component detail including schema summary,
            examples count, and full documentation.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the AI Service call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        ai_client = self._ai_client_factory(project.stack_url, project.token)
        try:
            raw = ai_client.get_component_detail(component_id)
        finally:
            ai_client.close()

        detail = ComponentDetail(**raw)

        # Build schema summary
        schema = detail.configuration_schema
        schema_properties = schema.get("properties", {}) if schema else {}
        schema_required = schema.get("required", []) if schema else []

        return {
            "component_id": detail.component_id,
            "component_name": detail.component_name,
            "component_type": detail.component_type,
            "categories": detail.component_categories,
            "flags": detail.component_flags,
            "description": detail.description,
            "long_description": detail.long_description,
            "documentation_url": detail.documentation_url,
            "schema_summary": {
                "property_count": len(schema_properties),
                "required_count": len(schema_required),
                "has_row_schema": bool(detail.configuration_row_schema),
            },
            "examples_count": len(detail.root_configuration_examples),
            "row_examples_count": len(detail.row_configuration_examples),
            "project_alias": alias,
        }

    def get_config_examples(self, alias: str | None, component_id: str) -> dict[str, Any]:
        """Fetch root and row configuration example bodies for a component.

        Ports the MCP ``get_config_examples`` tool (issue #393): the AI Service
        component detail already carries ``rootConfigurationExamples`` /
        ``rowConfigurationExamples``; this method surfaces the full bodies that
        :meth:`get_component_detail` deliberately reduces to counts (its
        contract is a summary and stays unchanged).

        Args:
            alias: Project alias. When None, the first available project is
                used (only the stack URL and token are needed).
            component_id: The component identifier (e.g. 'keboola.ex-google-drive').

        Returns:
            Dict with keys ``component_id``, ``root_examples`` (list of dicts),
            and ``row_examples`` (list of dicts).

        Raises:
            ConfigError: If the alias is not found or no projects are configured.
            KeboolaApiError: If the AI Service call fails.
        """
        projects = self.resolve_projects([alias] if alias else None)
        if not projects:
            raise ConfigError(
                "No projects configured. Use 'kbagent project add' to connect a project first."
            )
        resolved_alias = alias or next(iter(projects))
        project = projects[resolved_alias]

        ai_client = self._ai_client_factory(project.stack_url, project.token)
        try:
            raw = ai_client.get_component_detail(component_id)
        finally:
            ai_client.close()

        detail = ComponentDetail(**raw)
        return {
            "component_id": detail.component_id,
            "root_examples": detail.root_configuration_examples,
            "row_examples": detail.row_configuration_examples,
        }

    def run_sync_action(
        self,
        alias: str,
        component_id: str,
        action: str,
        config_id: str | None = None,
        row_id: str | None = None,
        branch_id: int | None = None,
        config_data_override: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a synchronous component action (issue #395, MCP ``run_sync_action`` port).

        Builds the ``configData`` payload and delegates the POST to
        :meth:`KeboolaClient.run_sync_action`. When ``config_data_override`` is
        given it is sent verbatim (no config fetch). Otherwise the root
        configuration is fetched (honoring ``branch_id``) and, when ``row_id``
        is given, the row configuration is SHALLOW-merged over it at the top
        level only -- exactly like the MCP tool: a row-level ``parameters`` or
        ``storage`` key REPLACES the root key wholesale (never deep-merged),
        so e.g. a row ``storage.input`` replaces the root ``storage.input``.

        Args:
            alias: Project alias (resolves stack URL + token).
            component_id: Component identifier (e.g. 'keboola.ex-db-mysql').
            action: Sync action name (freeform; component-defined).
            config_id: Configuration ID to build configData from. Required
                unless ``config_data_override`` is provided.
            row_id: Optional configuration row ID to shallow-merge over root.
            branch_id: Optional dev branch ID (config fetch + action call).
            config_data_override: Explicit configData dict; sent verbatim.
            timeout: Optional per-request timeout in seconds for the action call.

        Returns:
            Dict with keys ``component_id``, ``action``, and ``result`` (the
            opaque action response -- dict or list, action-specific).

        Raises:
            ConfigError: If the alias is unknown, or neither ``config_id`` nor
                ``config_data_override`` is provided.
            KeboolaApiError: If any API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            if config_data_override is not None:
                config_data = config_data_override
            else:
                if config_id is None:
                    raise ConfigError(
                        "Either a configuration ID or explicit config data is required "
                        "to run a sync action."
                    )
                root = client.get_config_detail(component_id, config_id, branch_id=branch_id)
                root_configuration = root.get("configuration") or {}
                row_configuration: dict[str, Any] = {}
                if row_id is not None:
                    row = client.get_config_row(
                        component_id, config_id, row_id, branch_id=branch_id
                    )
                    row_configuration = row.get("configuration") or {}
                # SHALLOW top-level merge (MCP parity): row keys replace root
                # keys wholesale; do NOT deep-merge.
                config_data = {
                    "parameters": {
                        **root_configuration.get("parameters", {}),
                        **row_configuration.get("parameters", {}),
                    },
                    "storage": {
                        **root_configuration.get("storage", {}),
                        **row_configuration.get("storage", {}),
                    },
                }
            result = client.run_sync_action(
                component_id,
                action,
                config_data,
                branch_id=branch_id,
                timeout=timeout,
            )
        finally:
            client.close()

        return {
            "component_id": component_id,
            "action": action,
            "result": result,
        }

    def generate_scaffold(
        self,
        alias: str,
        component_id: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Generate configuration scaffold files for a component.

        Fetches component detail from AI Service, then generates appropriate
        configuration files based on component type and schema.

        Args:
            alias: Project alias (used to derive stack URL and token).
            component_id: The component identifier.
            name: Configuration name. If None, defaults to
                "{component_name} Configuration".

        Returns:
            Dict with scaffold metadata and generated files list.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the AI Service call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        ai_client = self._ai_client_factory(project.stack_url, project.token)
        try:
            raw = ai_client.get_component_detail(component_id)
        finally:
            ai_client.close()

        detail = ComponentDetail(**raw)

        config_name = name or f"{detail.component_name} Configuration"
        category = _detect_component_category(component_id)

        # Build directory path
        dir_name = slugify(config_name)
        directory = f"{detail.component_type}/{component_id}/{dir_name}"

        # Generate files based on category
        files = self._generate_files(detail, config_name, category)

        return {
            "component_id": component_id,
            "component_name": detail.component_name,
            "component_type": detail.component_type,
            "config_name": config_name,
            "directory": directory,
            "documentation_url": detail.documentation_url,
            "files": files,
        }

    # --- Private helpers ---

    def _list_via_ai(
        self,
        aliases: list[str] | None,
        component_type: str | None,
        query: str,
    ) -> dict[str, Any]:
        """Search components via AI Service suggestions.

        Uses first/default project for AI queries, then enriches each
        suggestion with component detail.
        """
        projects = self.resolve_projects(aliases)
        # Use first project for AI queries
        first_alias = next(iter(projects))
        project = projects[first_alias]

        ai_client = self._ai_client_factory(project.stack_url, project.token)
        components: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        try:
            suggestions_raw = ai_client.suggest_components(query)
            suggestions = [ComponentSuggestion(**s) for s in suggestions_raw]

            for suggestion in suggestions:
                try:
                    raw_detail = ai_client.get_component_detail(suggestion.component_id)
                    detail = ComponentDetail(**raw_detail)

                    # Apply component_type filter if provided
                    if component_type and detail.component_type != component_type:
                        continue

                    components.append(
                        {
                            "component_id": detail.component_id,
                            "component_name": detail.component_name,
                            "component_type": detail.component_type,
                            "categories": detail.component_categories,
                            "description": detail.description,
                            "score": suggestion.score,
                        }
                    )
                except KeboolaApiError as exc:
                    logger.debug(
                        "Failed to fetch detail for %s: %s",
                        suggestion.component_id,
                        exc.message,
                    )
                    errors.append(
                        {
                            "component_id": suggestion.component_id,
                            "error_code": exc.error_code,
                            "message": exc.message,
                        }
                    )
                except Exception as exc:
                    logger.debug(
                        "Unexpected error fetching detail for %s: %s",
                        suggestion.component_id,
                        exc,
                    )
                    errors.append(
                        {
                            "component_id": suggestion.component_id,
                            "error_code": "UNEXPECTED_ERROR",
                            "message": str(exc),
                        }
                    )
        except KeboolaApiError as exc:
            errors.append(
                {
                    "project_alias": first_alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "project_alias": first_alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                }
            )
        finally:
            ai_client.close()

        return {"components": components, "errors": errors}

    def _list_via_storage(
        self,
        aliases: list[str] | None,
        component_type: str | None,
    ) -> dict[str, Any]:
        """List components via Storage API across projects in parallel."""
        projects = self.resolve_projects(aliases)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
            client = self._client_factory(project.stack_url, project.token)
            try:
                raw_components = client.list_components(component_type=component_type)
                result: list[dict[str, Any]] = []
                for comp in raw_components:
                    result.append(
                        {
                            "component_id": comp.get("id", ""),
                            "component_name": comp.get("name", ""),
                            "component_type": comp.get("type", ""),
                            "categories": comp.get("categories", []),
                            "description": comp.get("description", ""),
                        }
                    )
                return (alias, result, True)
            except KeboolaApiError as exc:
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": exc.error_code,
                        "message": exc.message,
                    },
                )
            except Exception as exc:
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": "UNEXPECTED_ERROR",
                        "message": str(exc),
                    },
                )
            finally:
                client.close()

        successes, errors = self._run_parallel(projects, worker)

        # Deduplicate components across projects by component_id
        seen: dict[str, dict[str, Any]] = {}
        for _alias, components, _ok in successes:
            for comp in components:
                comp_id = comp["component_id"]
                if comp_id not in seen:
                    seen[comp_id] = comp

        unique_components = sorted(seen.values(), key=lambda c: c["component_id"])
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"components": unique_components, "errors": errors}

    def _generate_files(
        self,
        detail: ComponentDetail,
        config_name: str,
        category: str,
    ) -> list[dict[str, str]]:
        """Generate scaffold files based on component category.

        Returns a list of file dicts with path, content, and description.
        """
        files: list[dict[str, str]] = []

        if category == "flow":
            files.append(
                {
                    "path": "_config.yml",
                    "content": _build_flow_config_yml(config_name, detail.component_id),
                    "description": "Conditional flow (keboola.flow) configuration",
                }
            )
            return files

        # All other categories get a _config.yml
        files.append(
            {
                "path": "_config.yml",
                "content": _build_config_yml(detail, config_name),
                "description": "Configuration file",
            }
        )

        if category == "sql_transformation":
            files.append(
                {
                    "path": "transform.sql",
                    "content": _build_transform_sql(config_name),
                    "description": "SQL transformation code",
                }
            )
        elif category == "python_transformation":
            files.append(
                {
                    "path": "transform.py",
                    "content": _build_transform_py(config_name),
                    "description": "Python transformation code",
                }
            )
        elif category == "custom_python":
            files.append(
                {
                    "path": "code.py",
                    "content": _build_code_py(),
                    "description": "Custom Python application code",
                }
            )
            files.append(
                {
                    "path": "pyproject.toml",
                    "content": _build_pyproject_toml(detail.component_id, config_name),
                    "description": "Python project configuration",
                }
            )

        return files
