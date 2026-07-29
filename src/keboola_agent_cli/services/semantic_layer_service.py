"""Semantic-layer service — business logic for ``kbagent semantic-layer``.

Composes :class:`MetastoreClient` primitives into the high-level operations
exposed by the CLI: model resolution, show/validate/export/diff (read),
add/edit/import/promote/build (write), remove (destructive), and the
token-encryption helper.

All API calls go through this service; the command layer only formats inputs
and outputs. Following the project's BaseService pattern, the metastore
client is created via an injected ``metastore_client_factory`` so unit tests
can swap in a :class:`unittest.mock.MagicMock`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from ..auth.sentinel import require_static_token
from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..metastore_client import MetastoreClient, SemanticType
from ..models import ProjectConfig
from . import _semantic_layer_reference_data as _refdata
from ._semantic_layer_cascade import cascade_delete_model as _cascade_delete_model_impl
from ._semantic_layer_crud import REMOVE_KINDS as _REMOVE_KINDS_HELPER
from ._semantic_layer_crud import code_metric as _code_metric_helper
from ._semantic_layer_crud import delete_then_post as _delete_then_post_helper
from ._semantic_layer_crud import edit_metric_with_cascade as _edit_metric_helper
from ._semantic_layer_crud import edit_simple as _edit_simple_helper
from ._semantic_layer_crud import find_target_for_remove as _find_target_for_remove
from ._semantic_layer_crud import scan_orphan_constraints as _scan_orphan_constraints
from ._semantic_layer_crud import validate_constraint_attrs as _validate_constraint_attrs
from ._semantic_layer_internals import _normalize_field_type, collect_side_from_file
from ._semantic_layer_internals import build_export_snapshot as _build_export_snapshot
from ._semantic_layer_internals import default_export_path as _default_export_path
from ._semantic_layer_internals import diff_one_type as _diff_one_type_helper
from ._semantic_layer_internals import (
    enrich_schemas_with_workspace_types as _enrich_schemas_with_workspace_types,
)
from ._semantic_layer_internals import fetch_table_schemas as _fetch_table_schemas
from ._semantic_layer_internals import heuristic_generate_model as _heuristic_generate_helper
from ._semantic_layer_internals import push_built_model as _push_built_model
from ._semantic_layer_internals import resolve_model_uuid as _resolve_model_uuid
from ._semantic_layer_internals import run_import_loop as _run_import_loop
from ._semantic_layer_internals import run_promote_loop as _run_promote_loop
from ._semantic_layer_internals import (
    synthesize_role_classified_fields as _synthesize_role_classified_fields,
)
from ._semantic_layer_internals import unpack_attrs_with_id as _unpack_attrs_with_id
from ._semantic_layer_internals import unpack_children_by_plural as _unpack_children_by_plural
from ._semantic_layer_internals import validate_basic as _validate_basic_helper
from ._semantic_layer_internals import validate_deep as _validate_deep_helper
from ._semantic_layer_internals import write_snapshot_to_file as _write_snapshot_to_file
from ._semantic_layer_lookup import run_get_context as _run_get_context_helper
from ._semantic_layer_lookup import run_search_context as _run_search_context_helper
from .base import BaseService, ClientFactory
from .encrypt_service import EncryptService
from .storage_service import StorageService
from .workspace_service import WorkspaceService

# Constraint name regex enforced by the metastore server.
CONSTRAINT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Constraint type enum (closed set on the server).
CONSTRAINT_TYPES: tuple[str, ...] = (
    "inequality",
    "equality",
    "range",
    "composition",
    "exclusion",
    "temporal",
    "conditional",
)

# Constraint severity enum (3 values; the 4-band health suffix lives in
# the name, not severity).
CONSTRAINT_SEVERITIES: tuple[str, ...] = ("error", "warning", "info")

# Role heuristics for `add dataset --deep-fields`.
_KEY_PREFIXES = ("PK_", "FK_")
_TIMESTAMP_NAMES = ("_DATE", "DATE_", "INS_DT", "UPD_DT")
_MEASURE_TOKENS = (
    "AMOUNT",
    "VALUE",
    "TOTAL",
    "REVENUE",
    "COST",
    "PRICE",
    "RATE",
    "PCT",
    "PERCENT",
    "COUNT",
    "QTY",
    "QUANTITY",
)
# Normalized field types (see ``_normalize_field_type``) that represent a
# numeric quantity, and thus qualify a measure-named column as a `measure`.
_NUMERIC_NORMALIZED_TYPES = ("integer", "decimal")
# Normalized field types that represent a point in time.
_TEMPORAL_NORMALIZED_TYPES = ("date", "datetime")


def _derive_fqn(table_id: str) -> str:
    """Compute ``"KEBOOLA"."<schema>"."<table>"`` from a Keboola tableId.

    The Keboola Snowflake mapping is ``"KEBOOLA"."<bucket-path>"."<table>"``;
    we split off the last segment as the table and quote both remaining
    pieces as a single schema string.
    """
    if '"' in table_id:
        # Reject double-quotes in tableIds at the service boundary: the FQN
        # is stored verbatim in the metastore and pasted into Snowflake SQL by
        # downstream consumers. A `"` inside a segment would terminate a quoted
        # identifier early and let an attacker steer parsing — defense in depth
        # even though Keboola Storage would reject the bucket/table at creation.
        raise KeboolaApiError(
            message=(
                f"tableId {table_id!r} contains a double-quote, which is "
                "rejected at the FQN-derivation boundary because the FQN is "
                "pasted into Snowflake SQL by downstream consumers."
            ),
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    parts = table_id.split(".")
    if len(parts) < 2:
        raise KeboolaApiError(
            message=f"tableId {table_id!r} must contain at least one dot.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    schema = ".".join(parts[:-1])
    table = parts[-1]
    return f'"KEBOOLA"."{schema}"."{table}"'


def _classify_field_role(name: str, basetype: str) -> str:
    """Apply the documented role heuristic to (column name, basetype).

    The type test runs through ``_normalize_field_type`` rather than matching
    raw type names, so it is warehouse-agnostic: Snowflake ``NUMBER``,
    BigQuery ``INT64`` / ``FLOAT64`` / ``NUMERIC`` and the Keboola basetypes
    (``INTEGER`` / ``NUMERIC`` / ``FLOAT``) all resolve to the same normalized
    families. Before this, ``NUMERIC`` (Keboola's own basetype for BigQuery
    and Snowflake decimals) was absent from the numeric whitelist, so numeric
    measure columns silently fell through to ``dimension``.
    """
    upper = name.upper()
    if any(upper.startswith(prefix) for prefix in _KEY_PREFIXES):
        return "key"
    normalized = _normalize_field_type(basetype)
    # A DATE / TIMESTAMP column is a timestamp regardless of its name; the
    # name tokens stay as a fallback for untyped columns (basetype missing).
    if normalized in _TEMPORAL_NORMALIZED_TYPES or any(tok in upper for tok in _TIMESTAMP_NAMES):
        return "timestamp"
    if normalized in _NUMERIC_NORMALIZED_TYPES and any(tok in upper for tok in _MEASURE_TOKENS):
        return "measure"
    return "dimension"


# Mapping from CLI singular ``--type`` filter to the wire-level
# ``semantic-<type>`` slug. Centralized so commands stay free of magic strings.
TYPE_ALIAS: dict[str, SemanticType] = {
    "dataset": "semantic-dataset",
    "metric": "semantic-metric",
    "relationship": "semantic-relationship",
    "constraint": "semantic-constraint",
    "glossary": "semantic-glossary",
}

# Accepted ``--type`` values for ``semantic-layer schema``: every child type
# from TYPE_ALIAS plus the model envelope itself. Kept as a separate dict
# (rather than adding ``model`` to TYPE_ALIAS in place) because ``show
# --type model`` has no plural payload key in ``_PLURAL_BY_TYPE`` — widening
# TYPE_ALIAS would let ``show`` accept ``model`` and then KeyError while
# rendering. Insertion order is the canonical ``--all`` fetch order.
SCHEMA_TYPE_ALIAS: dict[str, SemanticType] = {
    "model": "semantic-model",
    **TYPE_ALIAS,
}

# Child-types order used everywhere we fan out per-type fetches.
CHILD_TYPES: tuple[SemanticType, ...] = (
    "semantic-dataset",
    "semantic-metric",
    "semantic-relationship",
    "semantic-constraint",
    "semantic-glossary",
)

# Plural keys used in snapshot envelopes and accepted by ``--types`` filters
# on ``import`` and ``promote``. Must mirror the keys produced by
# :meth:`SemanticLayerService.export`.
PLURAL_TYPES: tuple[str, ...] = (
    "datasets",
    "metrics",
    "relationships",
    "constraints",
    "glossary",
)


def _validate_types_filter(types: list[str] | None) -> set[str] | None:
    """Validate ``--types`` against the known plural list, return a set or None.

    Closes the silent no-op trap where a typo like ``--types BOGUS`` would
    filter every type out and emit zero imports without an error.
    """
    if not types:
        return None
    unknown = [t for t in types if t not in PLURAL_TYPES]
    if unknown:
        raise KeboolaApiError(
            message=(
                f"--types {unknown!r} not recognised. Must be a subset of {list(PLURAL_TYPES)}."
            ),
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    return set(types)


logger = logging.getLogger(__name__)


MetastoreClientFactory = Callable[[str, str], MetastoreClient]


def default_metastore_client_factory(stack_url: str, token: str) -> MetastoreClient:
    """Build a :class:`MetastoreClient` for the given project.

    Static-token-only: the Metastore Service is not wired for bearer sessions
    (v1 scope is Storage + Manage), so the client's ``SESSION_AUTH_FEATURE``
    makes a session sentinel fail fast on construction.
    """
    return MetastoreClient(stack_url=stack_url, token=token)


class SemanticLayerService(BaseService):
    """Business logic for the semantic-layer command group.

    Inherits multi-project resolution (``resolve_projects``) and the
    parallel worker scaffold (``_run_parallel``) from :class:`BaseService`.
    Adds a dedicated :class:`MetastoreClient` factory so command-layer
    operations can target the metastore without polluting the Storage API
    client.

    Cross-project operations (``promote``) hold **two** clients in a
    ``try/finally`` and close both even on failure.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        metastore_client_factory: MetastoreClientFactory | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._metastore_factory: MetastoreClientFactory = (
            metastore_client_factory or default_metastore_client_factory
        )

    # Helpers (used by every subcommand).
    def _resolve_one_project(self, alias: str) -> ProjectConfig:
        """Resolve a single project alias to its ``ProjectConfig`` or raise.

        :meth:`BaseService.resolve_projects` already raises ``ConfigError`` for
        missing aliases, so we just unwrap.
        """
        return self.resolve_projects([alias])[alias]

    def _new_metastore_client(self, project: ProjectConfig) -> MetastoreClient:
        """Build a fresh metastore client. Caller is responsible for ``close()``."""
        return self._metastore_factory(project.stack_url, project.token)

    def _resolve_model(
        self, client: MetastoreClient, model_name_or_uuid: str | None
    ) -> tuple[str, dict[str, Any]]:
        """Resolve a model selector via :func:`._semantic_layer_internals.resolve_model_uuid`."""
        return _resolve_model_uuid(client, model_name_or_uuid)

    # Phase 3 — Read commands.
    def list_models(self, alias: str) -> dict[str, Any]:
        """List all semantic-layer models for a project.

        Returns:
            Dict with ``project`` and ``models`` (list of
            ``{id, name, description, sql_dialect}``).
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            raw = client.list_items("semantic-model")

        models: list[dict[str, Any]] = []
        for item in raw:
            attrs = item.get("attributes") or {}
            models.append(
                {
                    "id": item.get("id", ""),
                    "name": attrs.get("name", ""),
                    "description": attrs.get("description", ""),
                    "sql_dialect": attrs.get("sql_dialect", ""),
                }
            )
        return {"project": alias, "models": models}

    def search_context(
        self,
        alias: str,
        patterns: list[str] | None = None,
        type_filter: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Project-wide glob search; see :func:`_semantic_layer_lookup.run_search_context`."""
        return _run_search_context_helper(
            open_client=lambda: self._new_metastore_client(self._resolve_one_project(alias)),
            alias=alias,
            child_types=CHILD_TYPES,
            type_alias=TYPE_ALIAS,
            patterns=patterns,
            type_filter=type_filter,
            limit=limit,
        )

    def get_context(self, alias: str, context_id: str) -> dict[str, Any]:
        """Single-id lookup; see :func:`_semantic_layer_lookup.run_get_context`."""
        return _run_get_context_helper(
            open_client=lambda: self._new_metastore_client(self._resolve_one_project(alias)),
            alias=alias,
            child_types=CHILD_TYPES,
            context_id=context_id,
        )

    def get_schema(self, alias: str, types: list[str]) -> dict[str, Any]:
        """Fetch the server-side JSON Schema for one or more semantic types.

        ``types`` holds CLI-singular names (``metric``, ``model``, ...) as
        accepted by :data:`SCHEMA_TYPE_ALIAS`. Unknown names fail fast
        before any network call. Duplicates are collapsed (first occurrence
        wins the ordering). Multiple types fan out in parallel, mirroring
        :meth:`_fetch_children_parallel`.

        Returns:
            ``{"project": alias, "schemas": [{"type": <requested singular>,
            "schema": <raw server schema dict>}]}`` in requested order.
        """
        requested = list(dict.fromkeys(types))
        unknown = [t for t in requested if t not in SCHEMA_TYPE_ALIAS]
        if unknown:
            raise ConfigError(
                f"Unknown semantic type(s): {', '.join(unknown)}. "
                f"Valid types: {', '.join(SCHEMA_TYPE_ALIAS)}."
            )
        if not requested:
            raise ConfigError(
                f"No semantic types requested. Valid types: {', '.join(SCHEMA_TYPE_ALIAS)}."
            )

        project = self._resolve_one_project(alias)
        results: dict[str, dict[str, Any]] = {}
        with self._new_metastore_client(project) as client:
            if len(requested) == 1:
                results[requested[0]] = self._fetch_resolved_schema(
                    client, SCHEMA_TYPE_ALIAS[requested[0]]
                )
            else:
                with ThreadPoolExecutor(max_workers=len(requested)) as pool:
                    future_to_type = {
                        pool.submit(self._fetch_resolved_schema, client, SCHEMA_TYPE_ALIAS[t]): t
                        for t in requested
                    }
                    errors: list[Exception] = []
                    for future in future_to_type:
                        try:
                            results[future_to_type[future]] = future.result()
                        # Future.result() re-raises arbitrary worker exceptions
                        # (KeboolaApiError, httpx errors, ...); collect and
                        # surface the first rather than masking the others.
                        except Exception as exc:
                            errors.append(exc)
                    if errors:
                        raise errors[0]
        return {
            "project": alias,
            "schemas": [
                {
                    "type": t,
                    "schema": results[t]["schema"],
                    "schema_version": results[t]["schema_version"],
                }
                for t in requested
            ],
        }

    @staticmethod
    def _fetch_resolved_schema(client: MetastoreClient, wire_type: SemanticType) -> dict[str, Any]:
        """Fetch the actual JSON Schema for a type, resolving the default version.

        Live metastore behavior (2026-07): the bare ``/api/v1/schema/{type}``
        endpoint returns only a ``{"versions": [...]}`` listing (metadata, no
        schema body); the real JSON Schema lives at ``/{version}``. The
        upstream MCP tool passes the bare listing through -- an upstream gap
        we deliberately do NOT mirror: this resolves ``isDefault`` (falling
        back to the first entry) and fetches the versioned document. If the
        server someday returns the schema directly (no ``versions`` key),
        it is passed through unchanged.
        """
        body = client.get_schema(wire_type)
        versions = body.get("versions")
        if not isinstance(versions, list) or not versions:
            return {"schema": body, "schema_version": None}
        default = next(
            (v for v in versions if v.get("isDefault")),
            versions[0],
        )
        version_id = str(default.get("version", ""))
        resolved = client.get_schema(wire_type, version=version_id) if version_id else body
        return {"schema": resolved, "schema_version": version_id or None}

    # Internal helpers (model-scoped fetches).

    @staticmethod
    def _fetch_children_parallel(
        client: MetastoreClient,
        model_uuid: str,
    ) -> dict[SemanticType, list[dict[str, Any]]]:
        """Fetch all child entity types in parallel, filtered to one model.

        Each result is the raw item list (full ``{type, id, attributes, meta}``).
        Errors propagate to the caller (we re-raise the first encountered).
        """
        results: dict[SemanticType, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            future_to_type = {pool.submit(client.list_items, t, model_uuid): t for t in CHILD_TYPES}
            errors: list[Exception] = []
            for future in future_to_type:
                try:
                    results[future_to_type[future]] = future.result()
                # Future.result() re-raises arbitrary worker exceptions
                # (KeboolaApiError, httpx errors, etc.); collect and
                # surface the first one rather than masking the others.
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]
        return results

    # ------------------------------------------------------------------
    # Phase 3 — show
    # ------------------------------------------------------------------

    # Plural-key alias used by show_model's --type filter.
    _PLURAL_BY_TYPE: ClassVar[dict[str, str]] = {
        "dataset": "datasets",
        "metric": "metrics",
        "relationship": "relationships",
        "constraint": "constraints",
        "glossary": "glossary",
    }

    def show_model(
        self,
        alias: str,
        model_name_or_uuid: str | None = None,
        type_filter: str | None = None,
    ) -> dict[str, Any]:
        """Return the entities in a model. See ``--type`` for filtering."""
        if type_filter is not None and type_filter not in TYPE_ALIAS:
            raise KeboolaApiError(
                message=(
                    f"Invalid --type {type_filter!r}. Must be one of: "
                    f"{', '.join(sorted(TYPE_ALIAS))}."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
            )

        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, model_attrs = self._resolve_model(client, model_name_or_uuid)
            raw_by_type = self._fetch_children_parallel(client, model_uuid)

        result: dict[str, Any] = {
            "project": alias,
            "model": {"id": model_uuid, "name": model_attrs.get("name", "")},
            **_unpack_children_by_plural(raw_by_type),
        }
        if type_filter is not None:
            plural = self._PLURAL_BY_TYPE[type_filter]
            for k in set(self._PLURAL_BY_TYPE.values()) - {plural}:
                result.pop(k, None)
        return result

    # ------------------------------------------------------------------
    # Phase 3 — validate (+ --deep)
    # ------------------------------------------------------------------

    def validate_model(
        self,
        alias: str,
        model_name_or_uuid: str | None = None,
        deep: bool = False,
    ) -> dict[str, Any]:
        """Validate a semantic-layer model (basic + optional --deep).

        Returns ``{errors, warnings, deep, valid, model, project}``. See
        :func:`._semantic_layer_internals.validate_basic` and
        :func:`._semantic_layer_internals.validate_deep` for the full
        check inventory.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, model_attrs = self._resolve_model(client, model_name_or_uuid)
            raw_by_type = self._fetch_children_parallel(client, model_uuid)

        datasets = _unpack_attrs_with_id(raw_by_type.get("semantic-dataset", []))
        metrics = _unpack_attrs_with_id(raw_by_type.get("semantic-metric", []))
        relationships = _unpack_attrs_with_id(raw_by_type.get("semantic-relationship", []))
        constraints = _unpack_attrs_with_id(raw_by_type.get("semantic-constraint", []))
        glossary = _unpack_attrs_with_id(raw_by_type.get("semantic-glossary", []))

        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        _validate_basic_helper(
            datasets=datasets,
            metrics=metrics,
            relationships=relationships,
            constraints=constraints,
            glossary=glossary,
            errors=errors,
            warnings=warnings,
        )

        if deep:
            self._validate_deep(
                alias=alias,
                datasets=datasets,
                metrics=metrics,
                errors=errors,
                warnings=warnings,
            )

        return {
            "project": alias,
            "model": {"id": model_uuid, "name": model_attrs.get("name", "")},
            "errors": errors,
            "warnings": warnings,
            "deep": deep,
            "valid": len(errors) == 0,
        }

    # -- validate internals --------------------------------------------

    # Pure in-memory validation lives in
    # :func:`._semantic_layer_internals.validate_basic` (imported above as
    # ``_validate_basic_helper``); call it directly from ``validate_model``
    # / ``build_model`` rather than via a thin wrapper.

    def _validate_deep(
        self,
        *,
        alias: str,
        datasets: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
        errors: list[dict[str, str]],
        warnings: list[dict[str, str]],
    ) -> None:
        """Add deep checks that require a Snowflake schema fetch per dataset.

        Thin delegate to :func:`._semantic_layer_internals.validate_deep`.
        """
        storage = StorageService(
            config_store=self._config_store, client_factory=self._client_factory
        )
        _validate_deep_helper(
            alias=alias,
            storage=storage,
            datasets=datasets,
            metrics=metrics,
            errors=errors,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Phase 3 — export
    # ------------------------------------------------------------------

    def export_model(
        self,
        alias: str,
        model_name_or_uuid: str | None = None,
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        """Export a model + every child entity to a self-describing JSON file.

        Each child item is stored in its full server shape
        (``{type, id, attributes, meta}``) so the file is replayable by
        ``import`` and ``promote`` without further negotiation.

        Args:
            alias: Source project alias.
            model_name_or_uuid: Selector for the source model.
            output_path: Where to write the snapshot. Defaults to
                ``./sl_export_{model_name}_{YYYYMMDD_HHMMSS}.json``.

        Returns:
            ``{path, exported_at, project, model, datasets, metrics,
            relationships, constraints, glossary, counts}``.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, model_attrs = self._resolve_model(client, model_name_or_uuid)
            raw_by_type = self._fetch_children_parallel(client, model_uuid)

        snapshot = _build_export_snapshot(
            alias=alias,
            model_uuid=model_uuid,
            model_attrs=model_attrs,
            raw_by_type=raw_by_type,
        )
        if output_path is None:
            output_path = _default_export_path(str(model_attrs.get("name", "model")))
        _write_snapshot_to_file(snapshot, output_path)

        result = dict(snapshot)
        result["path"] = str(output_path)
        result["counts"] = {
            "datasets": len(snapshot["datasets"]),
            "metrics": len(snapshot["metrics"]),
            "relationships": len(snapshot["relationships"]),
            "constraints": len(snapshot["constraints"]),
            "glossary": len(snapshot["glossary"]),
        }
        return result

    # ------------------------------------------------------------------
    # Phase 3 — diff
    # ------------------------------------------------------------------

    # Diff helpers live in ._semantic_layer_internals (DIFF_IGNORED_KEYS,
    # compare_attrs, diff_one_type, collect_side_from_file) -- imported
    # there so this orchestrator file fits the CONTRIBUTING.md budget.

    def diff(
        self,
        *,
        project_a: str | None = None,
        project_b: str | None = None,
        model_a: str | None = None,
        model_b: str | None = None,
        file_a: Path | None = None,
        file_b: Path | None = None,
    ) -> dict[str, Any]:
        """Diff two semantic-layer snapshots.

        Each "side" is either a live project (resolve + fetch) or a file
        produced by :meth:`export_model`. The diff is structural and
        per-entity-type: ``added | removed | changed`` lists. The
        identification key is ``name`` for every type except glossary,
        which uses ``term``.

        Returns:
            ``{left, right, datasets, metrics, relationships, constraints,
            glossary}`` where each per-type entry is
            ``{added: [name], removed: [name], changed: [{name, diff_keys}]}``.
        """
        left = self._collect_side(project=project_a, model=model_a, file=file_a)
        right = self._collect_side(project=project_b, model=model_b, file=file_b)

        result: dict[str, Any] = {"left": left["ref"], "right": right["ref"]}
        for type_key, id_key in (
            ("datasets", "name"),
            ("metrics", "name"),
            ("relationships", "name"),
            ("constraints", "name"),
            ("glossary", "term"),
        ):
            result[type_key] = _diff_one_type_helper(
                left["data"].get(type_key, []),
                right["data"].get(type_key, []),
                id_key=id_key,
            )
        return result

    def _collect_side(
        self,
        *,
        project: str | None,
        model: str | None,
        file: Path | None,
    ) -> dict[str, Any]:
        """Resolve one side of a diff to bare ``attributes`` lists.

        Live-project path stays here (needs ``self.show_model``); the
        file-path branch delegates to
        :func:`._semantic_layer_internals.collect_side_from_file`.
        """
        if project is not None:
            data = self.show_model(alias=project, model_name_or_uuid=model)
            return {
                "ref": {"source": "project", "ref": project, "model": data.get("model", {})},
                "data": data,
            }
        if file is None:
            raise KeboolaApiError(
                message="Internal: diff side has no project and no file.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        return collect_side_from_file(file)

    # Per-type diff lives in :func:`._semantic_layer_internals.diff_one_type`
    # (imported above as ``_diff_one_type_helper``); call it directly.
    # ``compare_attrs`` is used inside that helper and not separately here.

    # ------------------------------------------------------------------
    # Phase 4 — Model lifecycle (create / delete)
    # ------------------------------------------------------------------

    def create_model(
        self,
        alias: str,
        name: str,
        description: str = "",
        sql_dialect: str = "Snowflake",
    ) -> dict[str, Any]:
        """Create a semantic-layer model and return the server-stored item."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            data: dict[str, Any] = {"name": name, "sql_dialect": sql_dialect}
            if description:
                data["description"] = description
            created = client.post_item("semantic-model", name=name, data=data)
        return {"project": alias, "model": created}

    def delete_model(
        self,
        alias: str,
        model_name_or_uuid: str,
    ) -> dict[str, Any]:
        """Delete a semantic-layer model and cascade-delete its children.

        Thin orchestrator: resolve project + model, fetch children, and
        forward to :func:`_cascade_delete_model_impl`. Cascade semantics
        (reverse :data:`PUSH_ORDER`, per-child try/except, parent
        preserved on any failure with ``details.cascade`` envelope) live
        in the helper to keep this file under the 1500 LOC ceiling.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, model_attrs = self._resolve_model(client, model_name_or_uuid)
            children = self._fetch_children_parallel(client, model_uuid)
            return _cascade_delete_model_impl(
                client,
                alias=alias,
                model_uuid=model_uuid,
                model_attrs=model_attrs,
                children=children,
            )

    # ------------------------------------------------------------------
    # Phase 4 — add subcommands
    # ------------------------------------------------------------------

    def add_metric(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        name: str,
        sql: str,
        dataset: str,
        description: str = "",
        assume_yes: bool = False,
        is_tty: bool = False,
        confirm_cb: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """Create a metric. The ``dataset`` argument is a tableId.

        If the tableId is not in the model's datasets, warn and require
        an interactive confirmation. ``--yes`` (assume_yes) skips the
        prompt. In non-TTY contexts we refuse with VALIDATION_ERROR
        rather than silently push a broken metric.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            datasets = client.list_items("semantic-dataset", model_uuid)
            ds_tids = {(d.get("attributes") or {}).get("tableId", "") for d in datasets}

            if dataset not in ds_tids:
                msg = (
                    f"Metric dataset {dataset!r} is not a tableId in this model "
                    f"(known: {sorted(t for t in ds_tids if t)})."
                )
                if not assume_yes:
                    if not is_tty:
                        raise KeboolaApiError(
                            message=msg + " Pass --yes to bypass.",
                            error_code=ErrorCode.VALIDATION_ERROR,
                        )
                    if confirm_cb is None or not confirm_cb(msg + " Create anyway?"):
                        raise KeboolaApiError(
                            message="Aborted by user.",
                            error_code=ErrorCode.VALIDATION_ERROR,
                        )

            data: dict[str, Any] = {
                "name": name,
                "sql": sql,
                "dataset": dataset,
                "modelUUID": model_uuid,
            }
            if description:
                data["description"] = description
            return client.post_item("semantic-metric", name=name, data=data)

    def add_dataset(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        name: str,
        table_id: str,
        description: str = "",
        grain: str = "",
        primary_key: list[str] | None = None,
        deep_fields: bool = False,
    ) -> dict[str, Any]:
        """Create a dataset, auto-deriving ``fqn`` from the tableId.

        With ``deep_fields=True``, fetches the storage column schema and
        synthesises a ``fields[]`` array with role heuristics.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            data: dict[str, Any] = {
                "name": name,
                "tableId": table_id,
                "fqn": _derive_fqn(table_id),
                "modelUUID": model_uuid,
            }
            if description:
                data["description"] = description
            if grain:
                data["grain"] = grain
            if primary_key:
                data["primaryKey"] = list(primary_key)
            if deep_fields:
                storage = StorageService(
                    config_store=self._config_store,
                    client_factory=self._client_factory,
                )
                fields = _synthesize_role_classified_fields(
                    storage, alias, table_id, _classify_field_role
                )
                if fields:
                    data["fields"] = fields
            return client.post_item("semantic-dataset", name=name, data=data)

    def add_relationship(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        name: str,
        from_: str,
        to: str,
        on: str,
        type_: str,
    ) -> dict[str, Any]:
        """Create a relationship. ``from``/``to`` are tableIds; ``type``='left'|'inner'."""
        if type_ not in ("left", "inner"):
            raise KeboolaApiError(
                message=f"--type must be 'left' or 'inner', got {type_!r}.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            data = {
                "name": name,
                "from": from_,
                "to": to,
                "on": on,
                "type": type_,
                "modelUUID": model_uuid,
            }
            return client.post_item("semantic-relationship", name=name, data=data)

    def add_constraint(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        name: str,
        constraint_type: str,
        rule: str,
        metrics: list[str],
        severity: str = "warning",
    ) -> dict[str, Any]:
        """Create a constraint after validating every field locally."""
        _validate_constraint_attrs(
            name_re=CONSTRAINT_NAME_RE,
            constraint_types=CONSTRAINT_TYPES,
            severities=CONSTRAINT_SEVERITIES,
            name=name,
            constraint_type=constraint_type,
            severity=severity,
        )

        # METRICS exist in model
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            existing = client.list_items("semantic-metric", model_uuid)
            existing_names = {(m.get("attributes") or {}).get("name", "") for m in existing}
            missing = [m for m in metrics if m not in existing_names]
            if missing:
                raise KeboolaApiError(
                    message=(
                        f"Constraint references metric(s) not in model: {missing}. "
                        f"Known metrics: {sorted(existing_names - {''})}."
                    ),
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
            data = {
                "name": name,
                "constraintType": constraint_type,
                "rule": rule,
                "metrics": list(metrics),
                "severity": severity,
                "modelUUID": model_uuid,
            }
            return client.post_item("semantic-constraint", name=name, data=data)

    def add_glossary(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        term: str,
        definition: str = "",
    ) -> dict[str, Any]:
        """Create a glossary term. Outer envelope ``name`` must equal ``term``."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            data: dict[str, Any] = {"term": term, "modelUUID": model_uuid}
            if definition:
                data["definition"] = definition
            return client.post_item("semantic-glossary", name=term, data=data)

    # ------------------------------------------------------------------
    # Reference data — dimension-member records (e.g. a Chart of Accounts).
    # Thin delegators; logic lives in :mod:`._semantic_layer_reference_data`.
    # ------------------------------------------------------------------

    def list_reference_data(
        self,
        alias: str,
        model_name_or_uuid: str | None = None,
    ) -> dict[str, Any]:
        """List reference-data records (optionally scoped to one model)."""
        return _refdata.run_list(
            lambda: self._new_metastore_client(self._resolve_one_project(alias)),
            alias,
            model_name_or_uuid,
        )

    def get_reference_data(
        self,
        alias: str,
        *,
        record_id: str | None = None,
        dimension: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one reference-data record by ``record_id`` or by ``dimension``.

        ``dimension`` is the project-unique key, so no model is needed.
        """
        return _refdata.run_get(
            lambda: self._new_metastore_client(self._resolve_one_project(alias)),
            alias,
            record_id=record_id,
            dimension=dimension,
        )

    def set_reference_data(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        dimension: str,
        members: list[dict[str, Any]],
        dataset_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace (by model + dimension) a reference-data record."""
        return _refdata.run_set(
            lambda: self._new_metastore_client(self._resolve_one_project(alias)),
            alias,
            model_name_or_uuid,
            dimension=dimension,
            members=members,
            dataset_id=dataset_id,
            description=description,
        )

    def delete_reference_data(self, alias: str, record_id: str) -> dict[str, Any]:
        """Delete a reference-data record by UUID (server-side soft-delete)."""
        return _refdata.run_delete(
            lambda: self._new_metastore_client(self._resolve_one_project(alias)),
            alias,
            record_id,
        )

    # ------------------------------------------------------------------
    # Phase 4 — edit (DELETE-then-POST with rollback + rename cascade)
    # ------------------------------------------------------------------

    # Thin delegates to ._semantic_layer_crud helpers -- bodies live
    # there so this orchestrator stays under the services budget.
    _code_metric = staticmethod(_code_metric_helper)
    _delete_then_post = staticmethod(_delete_then_post_helper)

    def edit_metric(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        current_name: str,
        new_name: str | None = None,
        new_sql: str | None = None,
        new_dataset: str | None = None,
        new_description: str | None = None,
        assume_yes: bool = False,
        is_tty: bool = False,
        confirm_cb: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """Edit a metric via DELETE+POST with rename-cascade on constraints.

        Returns:
            ``{updated: item, cascaded_constraints: [...], rollback: None|{...}}``.

        The cascade body lives in
        :func:`._semantic_layer_crud.edit_metric_with_cascade` -- this
        method just resolves credentials + the model UUID and
        delegates.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            return _edit_metric_helper(
                client,
                model_uuid=model_uuid,
                current_name=current_name,
                new_name=new_name,
                new_sql=new_sql,
                new_dataset=new_dataset,
                new_description=new_description,
                assume_yes=assume_yes,
                is_tty=is_tty,
                confirm_cb=confirm_cb,
            )

    def edit_dataset(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        current_name: str,
        new_name: str | None = None,
        new_description: str | None = None,
        new_grain: str | None = None,
    ) -> dict[str, Any]:
        """Edit a dataset (DELETE+POST). Renames do NOT cascade for datasets."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            return _edit_simple_helper(
                client,
                "semantic-dataset",
                items=client.list_items("semantic-dataset", model_uuid),
                id_key="name",
                current_key=current_name,
                overrides={
                    "name": new_name,
                    "description": new_description,
                    "grain": new_grain,
                },
                not_found_label="Dataset",
            )

    def edit_constraint(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        current_name: str,
        new_name: str | None = None,
        new_rule: str | None = None,
        new_constraint_type: str | None = None,
        new_severity: str | None = None,
        new_metrics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Edit a constraint (DELETE+POST). Validates new attrs locally first."""
        _validate_constraint_attrs(
            name_re=CONSTRAINT_NAME_RE,
            constraint_types=CONSTRAINT_TYPES,
            severities=CONSTRAINT_SEVERITIES,
            name=new_name,
            constraint_type=new_constraint_type,
            severity=new_severity,
        )

        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            if new_metrics is not None:
                existing = client.list_items("semantic-metric", model_uuid)
                existing_names = {(m.get("attributes") or {}).get("name", "") for m in existing}
                missing = [m for m in new_metrics if m not in existing_names]
                if missing:
                    raise KeboolaApiError(
                        message=(f"--new-metrics references metric(s) not in model: {missing}."),
                        error_code=ErrorCode.VALIDATION_ERROR,
                    )
            return _edit_simple_helper(
                client,
                "semantic-constraint",
                items=client.list_items("semantic-constraint", model_uuid),
                id_key="name",
                current_key=current_name,
                overrides={
                    "name": new_name,
                    "rule": new_rule,
                    "constraintType": new_constraint_type,
                    "severity": new_severity,
                    "metrics": list(new_metrics) if new_metrics is not None else None,
                },
                not_found_label="Constraint",
            )

    def edit_relationship(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        current_name: str,
        new_name: str | None = None,
        new_from: str | None = None,
        new_to: str | None = None,
        new_on: str | None = None,
        new_type: str | None = None,
    ) -> dict[str, Any]:
        """Edit a relationship (DELETE+POST). Validates ``--new-type`` locally.

        Relationships are not referenced by any other entity, so no
        cascade is needed -- the result is shaped identically to
        :meth:`edit_dataset` for consistency.
        """
        if new_type is not None and new_type not in ("left", "inner"):
            raise KeboolaApiError(
                message=f"--new-type must be 'left' or 'inner', got {new_type!r}.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            return _edit_simple_helper(
                client,
                "semantic-relationship",
                items=client.list_items("semantic-relationship", model_uuid),
                id_key="name",
                current_key=current_name,
                overrides={
                    "name": new_name,
                    "from": new_from,
                    "to": new_to,
                    "on": new_on,
                    "type": new_type,
                },
                not_found_label="Relationship",
            )

    def edit_glossary(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        current_term: str,
        new_term: str | None = None,
        new_definition: str | None = None,
    ) -> dict[str, Any]:
        """Edit a glossary term (DELETE+POST).

        Renaming via ``--new-term`` is destructive for downstream
        consumers that join on the literal term string (the term IS the
        identity for a glossary entry). The CLI layer warns + gates
        behind ``--yes``; this method just executes.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            return _edit_simple_helper(
                client,
                "semantic-glossary",
                items=client.list_items("semantic-glossary", model_uuid),
                id_key="term",
                current_key=current_term,
                overrides={"term": new_term, "definition": new_definition},
                not_found_label="Glossary term",
            )

    # ------------------------------------------------------------------
    # Phase 5 — remove (destructive, orphan-warning before delete)
    # ------------------------------------------------------------------

    # REMOVE_KINDS lives in ._semantic_layer_crud; re-bound here so
    # subclasses can override the accepted-kinds set without forking
    # the helper.
    _REMOVE_KINDS = _REMOVE_KINDS_HELPER

    def preview_remove(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        """Return what `remove` would do (orphan list) without deleting.

        Always called before the actual delete so the command layer can
        echo the warning even when --yes skips the prompt.
        """
        if kind not in self._REMOVE_KINDS:
            raise KeboolaApiError(
                message=(
                    f"remove kind must be one of {'|'.join(self._REMOVE_KINDS)}, got {kind!r}."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            target, _, _ = _find_target_for_remove(
                client,
                kind=kind,
                model_uuid=model_uuid,
                name=name,
                type_alias=TYPE_ALIAS,
            )
            orphan_constraints = (
                _scan_orphan_constraints(client, model_uuid=model_uuid, metric_name=name)
                if kind == "metric"
                else []
            )
            return {
                "kind": kind,
                "id": target["id"],
                "name": name,
                "orphaned_constraints": orphan_constraints,
            }

    def remove_item(
        self,
        alias: str,
        model_name_or_uuid: str | None,
        *,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        """Delete a single child entity. Returns the removed item descriptor."""
        if kind not in self._REMOVE_KINDS:
            raise KeboolaApiError(
                message=(
                    f"remove kind must be one of {'|'.join(self._REMOVE_KINDS)}, got {kind!r}."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            target, type_slug, _ = _find_target_for_remove(
                client,
                kind=kind,
                model_uuid=model_uuid,
                name=name,
                type_alias=TYPE_ALIAS,
            )
            orphan_constraints = (
                _scan_orphan_constraints(client, model_uuid=model_uuid, metric_name=name)
                if kind == "metric"
                else []
            )
            client.delete_item(type_slug, target["id"])
            return {
                "removed": {"type": type_slug, "id": target["id"], "name": name},
                "orphaned_constraints": orphan_constraints,
            }

    # ------------------------------------------------------------------
    # Phase 6 — import (replay a snapshot, optionally overwrite)
    # ------------------------------------------------------------------

    # Push order for both `import` and `promote` lives in
    # ._semantic_layer_internals.PUSH_ORDER.

    def import_snapshot(
        self,
        alias: str,
        file: Path,
        *,
        model_name_or_uuid: str | None = None,
        types: list[str] | None = None,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Replay a snapshot produced by :meth:`export_model` into a project.

        Args:
            alias: Target project alias.
            file: Snapshot JSON file path.
            model_name_or_uuid: Target model selector. If different from the
                snapshot's model, every item's ``modelUUID`` is rewritten.
            types: Filter to a subset of types. ``None`` = all types.
            dry_run: When True, plan and return the action counts without
                hitting any write API.
            overwrite: When True, DELETE+POST conflicting items by name.
                Default (False) skips conflicts.

        Returns:
            ``{imported: {<type>: {created, skipped, overwritten, failed}}}``.
        """
        try:
            snapshot = json.loads(file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise KeboolaApiError(
                message=f"Cannot read --file {file}: {exc}",
                error_code=ErrorCode.READ_ERROR,
            ) from exc
        except json.JSONDecodeError as exc:
            raise KeboolaApiError(
                message=f"File {file} is not valid JSON: {exc}",
                error_code=ErrorCode.INVALID_FORMAT,
            ) from exc

        return self.import_snapshot_from_dict(
            alias,
            snapshot=snapshot,
            model_name_or_uuid=model_name_or_uuid,
            types=types,
            dry_run=dry_run,
            overwrite=overwrite,
        )

    def import_snapshot_from_dict(
        self,
        alias: str,
        *,
        snapshot: dict[str, Any],
        model_name_or_uuid: str | None = None,
        types: list[str] | None = None,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Replay an in-memory snapshot dict (sibling of :meth:`import_snapshot`).

        Used by the REST router ``POST /semantic-layer/import``, where the
        snapshot arrives inline in the JSON body — there is no file to
        read. The wire-level shape must match what :meth:`export_model`
        produces (``{datasets, metrics, relationships, constraints,
        glossary}`` with full server item envelopes).
        """
        if not isinstance(snapshot, dict):
            raise KeboolaApiError(
                message="Snapshot must be a JSON object.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )

        type_filter = _validate_types_filter(types)

        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            model_uuid, _ = self._resolve_model(client, model_name_or_uuid)
            existing_by_type = self._fetch_children_parallel(client, model_uuid)

            imported = _run_import_loop(
                client,
                snapshot=snapshot,
                target_model_uuid=model_uuid,
                existing_by_type=existing_by_type,
                type_filter=type_filter,
                dry_run=dry_run,
                overwrite=overwrite,
            )
            return {
                "target_project": alias,
                "target_model": model_uuid,
                "source_model": (snapshot.get("model") or {}).get("id", ""),
                "dry_run": dry_run,
                "overwrite": overwrite,
                "imported": imported,
            }

    # ------------------------------------------------------------------
    # Phase 6 — promote (cross-project copy)
    # ------------------------------------------------------------------

    def promote_model(
        self,
        *,
        from_project: str,
        to_project: str,
        from_model: str | None = None,
        to_model: str | None = None,
        types: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Promote a model's entities from one project to another.

        Holds two metastore clients in a try/finally and closes both on
        exit even on error. Items are classified as NEW (not in target),
        IDENTICAL (all attributes equal after stripping modelUUID), or
        CHANGED (differs). Default behaviour: import NEW + overwrite
        CHANGED. IDENTICAL items are skipped. Target items absent from
        source are NEVER deleted.

        Returns:
            ``{from_project, to_project, dry_run, datasets: {new,
            overwritten, identical, failed}, metrics: {...}, ...}``.
        """
        projects = self.resolve_projects([from_project, to_project])
        if from_project not in projects:
            raise ConfigError(f"Source project '{from_project}' not found.")
        if to_project not in projects:
            raise ConfigError(f"Target project '{to_project}' not found.")

        type_filter = _validate_types_filter(types)

        with (
            self._new_metastore_client(projects[from_project]) as src_client,
            self._new_metastore_client(projects[to_project]) as tgt_client,
        ):
            src_uuid, _ = self._resolve_model(src_client, from_model)
            tgt_uuid, _ = self._resolve_model(tgt_client, to_model)

            src_children = self._fetch_children_parallel(src_client, src_uuid)
            tgt_children = self._fetch_children_parallel(tgt_client, tgt_uuid)

            per_type_stats = _run_promote_loop(
                tgt_client,
                src_children=src_children,
                tgt_children=tgt_children,
                target_model_uuid=tgt_uuid,
                type_filter=type_filter,
                dry_run=dry_run,
            )
            return {
                "from_project": from_project,
                "to_project": to_project,
                "from_model": src_uuid,
                "to_model": tgt_uuid,
                "dry_run": dry_run,
                **per_type_stats,
            }

    # ------------------------------------------------------------------
    # Phase 7 — build (AI-assisted / heuristic greenfield)
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_types_workspace(workspace: WorkspaceService, alias: str, backend: str) -> int | None:
        """Auto-pick a workspace able to read ``backend`` types via the Query
        Service.

        Prefers a Query-Service-compatible, read-only workspace whose backend
        matches the table's (so ``INFORMATION_SCHEMA`` runs in the right
        dialect). Returns its ID, or ``None`` if the project has none.
        """
        try:
            listing = workspace.list_workspaces(aliases=[alias], qs_compatible_only=True)
        except (KeboolaApiError, ConfigError):
            return None
        candidates = listing.get("workspaces", [])
        for ws in candidates:
            if not backend or (ws.get("backend", "") or "").lower() == backend.lower():
                ws_id = ws.get("id")
                return int(ws_id) if ws_id is not None else None
        return None

    def build_model(
        self,
        alias: str,
        *,
        table_ids: list[str],
        model_name: str | None = None,
        model_name_or_uuid: str | None = None,
        dry_run: bool = False,
        keep_on_failure: bool = False,
        output_path: Path | None = None,
        types_workspace_id: int | None = None,
        auto_resolve_types: bool = False,
    ) -> dict[str, Any]:
        """Build (or update) a semantic-layer model from a list of tableIds.

        Implementation note: the AI Service client (`ai_client.py`) currently
        exposes only `suggest_components` for natural-language component
        search; there is no endpoint that returns arbitrary structured JSON.
        Rather than degrade silently, this method falls back to a
        DETERMINISTIC HEURISTIC builder: it fetches the storage schema for
        every tableId in parallel, synthesises a dataset per table with
        role-classified fields, suggests one COUNT(*)-style metric per
        table, and seeds a small glossary. The intended behaviour is "best
        starting point, then iterate via `add`/`edit`" — not "ready-to-ship
        model". This fallback is documented in gotchas.md.

        Validation runs locally against the generated model BEFORE any POST.
        If validation surfaces any errors, the operation is refused with
        VALIDATION_ERROR + the full error list in details so callers can
        iterate.

        On push failure, ``push_built_model`` rolls back every successfully
        POSTed child in reverse PUSH_ORDER and deletes the model itself if
        we created it during this call (issue #295). Pass
        ``keep_on_failure=True`` to skip cleanup and preserve the partial
        state for forensic inspection (mirrors ``data-app create``).
        """
        if not table_ids:
            raise KeboolaApiError(
                message="--tables must contain at least one tableId.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )

        # Fetch schemas in parallel via in-process StorageService.
        storage = StorageService(
            config_store=self._config_store, client_factory=self._client_factory
        )
        schemas_by_tid, fetch_errors = _fetch_table_schemas(storage, alias, table_ids)

        # Opt-in: backfill column types that Storage does not carry locally
        # (alias / linked-bucket tables) by querying the warehouse
        # INFORMATION_SCHEMA via the supplied workspace. Without this, such
        # tables reach the heuristic with empty basetypes and every field is
        # classified as `dimension`.
        type_resolution_errors: list[dict[str, str]] = []
        if types_workspace_id is not None or auto_resolve_types:
            workspace = WorkspaceService(
                config_store=self._config_store, client_factory=self._client_factory
            )
            if types_workspace_id is not None:
                # Explicit workspace: use it for every backend.
                def resolve_workspace_id(_backend: str) -> int | None:
                    return types_workspace_id
            else:
                # Auto mode (UI / server default): pick a Query-Service-capable
                # read-only workspace per backend, cached across tables.
                ws_cache: dict[str, int | None] = {}

                def resolve_workspace_id(backend: str) -> int | None:
                    if backend not in ws_cache:
                        ws_cache[backend] = self._pick_types_workspace(workspace, alias, backend)
                    return ws_cache[backend]

            type_resolution_errors = _enrich_schemas_with_workspace_types(
                schemas_by_tid=schemas_by_tid,
                alias=alias,
                resolve_workspace_id=resolve_workspace_id,
                execute_query=workspace.execute_query,
            )

        # Generate model JSON (heuristic). The internals helper takes the
        # FQN-derivation and role-classification callables as kwargs so its
        # module stays free of import-time coupling to this module.
        generated = _heuristic_generate_helper(
            schemas=schemas_by_tid,
            model_name=model_name or "kbagent_build_model",
            derive_fqn=_derive_fqn,
            classify_role=_classify_field_role,
        )

        # Validate locally.
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        _validate_basic_helper(
            datasets=generated["datasets"],
            metrics=generated["metrics"],
            relationships=generated["relationships"],
            constraints=generated["constraints"],
            glossary=generated["glossary"],
            errors=errors,
            warnings=warnings,
        )

        # Write output file (if requested).
        if output_path is not None:
            _write_snapshot_to_file(generated, output_path)

        result: dict[str, Any] = {
            "project": alias,
            "dry_run": dry_run,
            "keep_on_failure": keep_on_failure,
            "fallback_used": "heuristic",  # no AI endpoint shipped yet
            "fetch_errors": fetch_errors,
            "type_resolution_errors": type_resolution_errors,
            "generated": generated,
            "validation": {"errors": errors, "warnings": warnings},
            "validated": len(errors) == 0,
        }
        if output_path is not None:
            result["output_path"] = str(output_path)

        if dry_run:
            return result

        if errors:
            raise KeboolaApiError(
                message=(
                    f"Generated model failed local validation ({len(errors)} errors). "
                    "Refusing to push. Inspect with --dry-run + --output to iterate."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
                details={"validation": result["validation"]},
            )

        # Push to the metastore in dependency order.
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            counts, model_uuid, model_item = _push_built_model(
                client,
                generated=generated,
                model_name_or_uuid=model_name_or_uuid,
                resolve_model_fn=self._resolve_model,
                keep_on_failure=keep_on_failure,
            )
            result["model"] = {"id": model_uuid, "item": model_item}
            result["created"] = counts
            return result

    # ------------------------------------------------------------------
    # Phase 8 — token --encrypt
    # ------------------------------------------------------------------

    def encrypt_token(self, alias: str, component_id: str) -> dict[str, Any]:
        """Encrypt the project's storage token for `user_properties`.

        Reads the project's own token from ``ProjectConfig`` (no config.json
        digging) and passes it through the existing :class:`EncryptService`
        as ``{"#metastore_token": token}``.

        Returns:
            ``{"encrypted": {"#metastore_token": "KBC::ProjectSecure..."},
            "component_id": C, "project": alias}``.
        """
        project = self._resolve_one_project(alias)
        require_static_token(project.token, feature="semantic-layer token --encrypt")
        # Reuse the production EncryptService factory so the token never
        # leaves the in-process Storage API path.
        encrypt = EncryptService(
            config_store=self._config_store, client_factory=self._client_factory
        )
        encrypted = encrypt.encrypt(
            alias=alias,
            component_id=component_id,
            input_data={"#metastore_token": project.token},
        )
        return {
            "project": alias,
            "component_id": component_id,
            "encrypted": encrypted,
        }
