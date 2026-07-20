"""SQL transformation service -- create / show / edit block-based SQL configs.

Native port of keboola-mcp-server's ``create_sql_transformation`` and
``update_sql_transformation`` tools (issue #396). The block/code update
engine lives in :mod:`keboola_agent_cli.services._transformation_ops`;
this module owns:

- component-ID resolution from the project's default backend
  (``verify_token().default_backend``: snowflake / bigquery),
- create-payload shaping (single block "Blocks" with one code "Code",
  statements split via the shared SQL splitter, output-table mapping
  derived from the transformation name via :func:`clean_bucket_name`),
- config fetch with component-ID fallback across the known SQL
  transformation component IDs,
- the edit orchestration (fetch -> normalize -> simplify -> apply ops ->
  re-split -> PUT with change description).
"""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..client import KeboolaClient
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..sync.code_extraction import (
    SQL_TRANSFORMATION_COMPONENTS,
    normalize_blocks_codes_script,
)
from ..sync.sql_split import join_statements, split_statements
from . import _transformation_ops as tf_ops
from .base import BaseService

# Backend (verify_token owner.defaultBackend) -> SQL transformation component.
# Mirrors the MCP server's get_sql_transformation_id_from_sql_dialect().
BACKEND_TO_COMPONENT_ID: dict[str, str] = {
    "snowflake": "keboola.snowflake-transformation",
    "bigquery": "keboola.google-bigquery-transformation",
}

# Preference order when --component-id is omitted on show/edit: the two
# backends kbagent can create come first, then the remaining known SQL
# transformation components (sorted for determinism).
COMPONENT_LOOKUP_ORDER: tuple[str, ...] = (
    "keboola.snowflake-transformation",
    "keboola.google-bigquery-transformation",
    *sorted(
        SQL_TRANSFORMATION_COMPONENTS
        - {"keboola.snowflake-transformation", "keboola.google-bigquery-transformation"}
    ),
)

# Create-payload shaping: the UI/MCP convention is a single block named
# "Blocks" holding one code named "Code" with the split statements.
DEFAULT_BLOCK_NAME = "Blocks"
DEFAULT_CODE_NAME = "Code"

# Maximum bucket-name length accepted by Keboola Storage (MCP parity).
MAX_BUCKET_NAME_LENGTH = 96

# Output bucket stage prefix for created tables (UI convention).
OUTPUT_BUCKET_PREFIX = "out.c-"


def clean_bucket_name(bucket_name: str) -> str:
    """Sanitize a transformation name into a Storage bucket name.

    Exact port of the MCP server's ``clean_bucket_name``:

    - Converts to ASCII (diacritics stripped: ``cesky`` from ``český``).
    - Replaces all whitespace runs with dashes.
    - Removes any character that is not alphanumeric, dash, or underscore.
    - Removes leading underscores.
    - Caps at :data:`MAX_BUCKET_NAME_LENGTH` characters.
    """
    bucket_name = bucket_name.strip()
    bucket_name = unicodedata.normalize("NFKD", bucket_name)
    bucket_name = bucket_name.encode("ascii", "ignore").decode("ascii")
    bucket_name = re.sub(r"\s+", "-", bucket_name)
    bucket_name = re.sub(r"[^a-zA-Z0-9_-]", "", bucket_name)
    bucket_name = re.sub(r"^_+", "", bucket_name)
    return bucket_name[:MAX_BUCKET_NAME_LENGTH]


@dataclass
class ResolvedConfig:
    """A fetched configuration plus the component ID it was found under."""

    component_id: str
    detail: dict[str, Any]


class TransformationService(BaseService):
    """Business logic for the ``kbagent transformation`` command group.

    Receives ``ConfigStore`` and a ``client_factory`` via dependency
    injection (see :class:`keboola_agent_cli.services.base.BaseService`).
    """

    # ---- create -----------------------------------------------------

    def create(
        self,
        alias: str,
        *,
        name: str,
        sql: str,
        created_tables: list[str] | None = None,
        component_id: str | None = None,
        description: str = "",
        branch_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a new SQL transformation configuration.

        Args:
            alias: Project alias.
            name: Transformation name (also drives the output bucket name).
            sql: SQL text; split into one statement per ``script[]`` element.
            created_tables: Table names created by the SQL (``CREATE TABLE``);
                each is mapped to ``out.c-<clean_bucket_name(name)>.<table>``.
            component_id: Explicit SQL transformation component ID; when
                omitted, derived from the project's default backend.
            description: Configuration description.
            branch_id: Optional dev-branch ID.
            dry_run: When True, return the would-be payload without POSTing.

        Returns:
            Result dict with the shaped configuration payload and -- unless
            ``dry_run`` -- the created ``config_id`` and ``version``.

        Raises:
            ValueError: If the SQL contains no statements.
            ConfigError: If the project backend has no SQL transformation
                component and no explicit ``component_id`` was given.
            KeboolaApiError: On API failure.
        """
        statements = split_statements(sql)
        if not statements:
            raise ValueError("SQL contains no statements (empty input)")

        project = self.resolve_projects([alias])[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            resolved_component_id = component_id or self._component_id_from_backend(client, alias)

            configuration = _build_create_configuration(
                name=name,
                statements=statements,
                created_tables=created_tables or [],
            )

            result: dict[str, Any] = {
                "project_alias": alias,
                "component_id": resolved_component_id,
                "name": name,
                "description": description,
                "branch_id": branch_id,
                "configuration": configuration,
                "dry_run": dry_run,
            }
            if dry_run:
                return result

            created = client.create_config(
                component_id=resolved_component_id,
                name=name,
                configuration=configuration,
                description=description,
                branch_id=branch_id,
            )
            result["config_id"] = str(created.get("id", ""))
            result["version"] = created.get("version")
            return result
        finally:
            client.close()

    # ---- show -------------------------------------------------------

    def show(
        self,
        alias: str,
        *,
        config_id: str,
        component_id: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a SQL transformation and render its block/code tree.

        When ``component_id`` is omitted, the known SQL transformation
        component IDs are tried in :data:`COMPONENT_LOOKUP_ORDER` until the
        configuration is found.

        Returns:
            Dict with ``config_id``, ``component_id``, ``name``, ``blocks``
            (each block ``{id, name, codes:[{id, name, script, script_text}]}``
            with synthetic positional IDs ``b{i}`` / ``b{i}.c{j}``) and
            ``storage``.
        """
        project = self.resolve_projects([alias])[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            resolved = self._fetch_config(client, config_id, component_id, branch_id)
        finally:
            client.close()

        configuration = copy.deepcopy(resolved.detail.get("configuration") or {})
        # Normalize legacy string-shaped scripts so the view (and the JSON
        # contract: script is always a statement array) is stable.
        configuration, _ = normalize_blocks_codes_script(resolved.component_id, configuration)

        return {
            "project_alias": alias,
            "config_id": config_id,
            "component_id": resolved.component_id,
            "name": resolved.detail.get("name", ""),
            "description": resolved.detail.get("description", ""),
            "version": resolved.detail.get("version"),
            "blocks": _blocks_view(configuration.get("parameters") or {}),
            "storage": configuration.get("storage") or {},
        }

    # ---- edit -------------------------------------------------------

    def edit(
        self,
        alias: str,
        *,
        config_id: str,
        ops: list[dict[str, Any]],
        change_description: str,
        component_id: str | None = None,
        storage: dict[str, Any] | None = None,
        branch_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply a batch of block/code operations to a SQL transformation.

        Operations are validated and applied sequentially against the
        simplified structure (see :mod:`._transformation_ops`); the result
        is re-split into statement arrays and PUT with the given change
        description. ``storage``, when provided, replaces
        ``configuration.storage`` wholesale.

        Args:
            alias: Project alias.
            config_id: Configuration ID to edit.
            ops: Raw operation dicts (each with an ``op`` key). May be empty
                when only ``storage`` is being replaced.
            change_description: Human-readable change summary (required by
                the Storage API versioning UX).
            component_id: Explicit component ID; auto-detected when omitted.
            storage: Full replacement for ``configuration.storage``.
            branch_id: Optional dev-branch ID.
            dry_run: When True, compute and return the result without PUT.

        Returns:
            Result dict with ``operations_applied`` messages, the resulting
            ``blocks`` view, and -- unless ``dry_run`` -- the new ``version``.

        Raises:
            ValueError: On invalid ops (schema or against current structure).
            KeboolaApiError: On API failure (including config not found).
        """
        parsed_ops = tf_ops.parse_ops(ops)

        project = self.resolve_projects([alias])[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            resolved = self._fetch_config(client, config_id, component_id, branch_id)

            configuration = copy.deepcopy(resolved.detail.get("configuration") or {})
            messages: list[str] = []
            structural = False

            if parsed_ops:
                # Normalize legacy string scripts to arrays first so the
                # raw -> simplified -> raw round trip is lossless.
                configuration, _ = normalize_blocks_codes_script(
                    resolved.component_id, configuration
                )
                existing_params = configuration.get("parameters") or {}
                simplified = tf_ops.raw_to_simplified(existing_params)
                batch = tf_ops.apply_ops(simplified, parsed_ops)
                messages = batch.messages
                structural = batch.structural

                # Preserve non-blocks parameter keys; replace blocks with the
                # re-split raw shape (synthetic IDs stripped).
                new_params = dict(existing_params)
                new_params["blocks"] = tf_ops.simplified_to_raw(batch.params)["blocks"]
                configuration["parameters"] = new_params

            if storage is not None:
                configuration["storage"] = storage

            result: dict[str, Any] = {
                "project_alias": alias,
                "config_id": config_id,
                "component_id": resolved.component_id,
                "change_description": change_description,
                "operations_applied": messages,
                "structural_change": structural,
                "storage_replaced": storage is not None,
                "blocks": _blocks_view(configuration.get("parameters") or {}),
                "dry_run": dry_run,
            }
            if dry_run:
                return result

            updated = client.update_config(
                component_id=resolved.component_id,
                config_id=config_id,
                configuration=configuration,
                change_description=change_description,
                branch_id=branch_id,
            )
            result["version"] = updated.get("version")
            return result
        finally:
            client.close()

    # ---- private helpers ---------------------------------------------

    def _component_id_from_backend(self, client: KeboolaClient, alias: str) -> str:
        """Derive the SQL transformation component from the project backend."""
        verify = client.verify_token()
        backend = (verify.default_backend or "").lower()
        resolved = BACKEND_TO_COMPONENT_ID.get(backend)
        if resolved is None:
            supported = ", ".join(sorted(BACKEND_TO_COMPONENT_ID))
            raise ConfigError(
                f"Project '{alias}' has default backend '{backend}', which has no "
                f"SQL transformation component mapping (supported: {supported}). "
                "Pass --component-id explicitly."
            )
        return resolved

    def _fetch_config(
        self,
        client: KeboolaClient,
        config_id: str,
        component_id: str | None,
        branch_id: int | None,
    ) -> ResolvedConfig:
        """Fetch config detail, trying known SQL components when ID omitted."""
        if component_id is not None:
            detail = client.get_config_detail(component_id, config_id, branch_id=branch_id)
            return ResolvedConfig(component_id=component_id, detail=detail)

        for candidate in COMPONENT_LOOKUP_ORDER:
            try:
                detail = client.get_config_detail(candidate, config_id, branch_id=branch_id)
                return ResolvedConfig(component_id=candidate, detail=detail)
            except KeboolaApiError as exc:
                if exc.status_code == 404 or exc.error_code == ErrorCode.NOT_FOUND:
                    continue
                raise

        tried = ", ".join(COMPONENT_LOOKUP_ORDER)
        raise KeboolaApiError(
            message=(
                f"Configuration '{config_id}' was not found under any SQL "
                f"transformation component (tried: {tried}). If it belongs to a "
                "different component, pass --component-id explicitly; for "
                "Python/R transformations use 'kbagent config update'."
            ),
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
        )


def _build_create_configuration(
    *,
    name: str,
    statements: list[str],
    created_tables: list[str],
) -> dict[str, Any]:
    """Shape the create payload (MCP create_transformation_configuration port).

    Single block "Blocks" with one code "Code" carrying the split
    statements; each created table maps to
    ``out.c-<clean_bucket_name(name)>.<table>`` in the output mapping.
    """
    output_tables: list[dict[str, Any]] = []
    if created_tables:
        destination_bucket = f"{OUTPUT_BUCKET_PREFIX}{clean_bucket_name(name)}"
        output_tables = [
            {"source": table, "destination": f"{destination_bucket}.{table}"}
            for table in created_tables
        ]

    return {
        "parameters": {
            "blocks": [
                {
                    "name": DEFAULT_BLOCK_NAME,
                    "codes": [{"name": DEFAULT_CODE_NAME, "script": statements}],
                }
            ]
        },
        "storage": {
            "input": {"tables": []},
            "output": {"tables": output_tables},
        },
    }


def _blocks_view(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the block/code tree with synthetic positional IDs.

    IDs are derived by index walk exactly like the MCP server's
    ``add_ids``: blocks ``b{i}``, codes ``b{i}.c{j}``. Each code carries
    both the raw statement array (``script``) and the joined SQL text
    (``script_text``).
    """
    blocks_out: list[dict[str, Any]] = []
    for bidx, block in enumerate(parameters.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        codes_out: list[dict[str, Any]] = []
        for cidx, code in enumerate(block.get("codes") or []):
            if not isinstance(code, dict):
                continue
            script = code.get("script")
            if isinstance(script, list):
                script_list = [s for s in script if isinstance(s, str)]
            elif isinstance(script, str):
                script_list = [script]
            else:
                script_list = []
            codes_out.append(
                {
                    "id": f"b{bidx}.c{cidx}",
                    "name": code.get("name", ""),
                    "script": script_list,
                    "script_text": join_statements(script_list),
                }
            )
        blocks_out.append(
            {
                "id": f"b{bidx}",
                "name": block.get("name", ""),
                "codes": codes_out,
            }
        )
    return blocks_out
