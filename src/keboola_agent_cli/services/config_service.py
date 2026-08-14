"""Configuration listing service - business logic for listing and detailing configs.

Orchestrates multi-project configuration retrieval in parallel, filtering,
aggregation, and full-text search without knowing about CLI or HTTP details.
"""

import copy
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..ai_client import AiServiceClient
from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..json_utils import compute_diff, deep_merge, set_nested_value
from ..models import ComponentDetail, ProjectConfig
from ..sync.code_extraction import normalize_blocks_codes_script
from ..sync.manifest import Manifest, load_manifest, save_manifest
from ..sync.naming import sanitize_name
from ._encryption import collect_secrets, encrypt_secrets_in_config, find_plaintext_secret_keys
from .base import BaseService, ClientFactory, sanitize_unexpected_error
from .workspace_service import find_storage_workspace_for_sandbox_config

AiClientFactory = Callable[[str, str], AiServiceClient]


def _default_ai_client_factory(stack_url: str, token: str) -> AiServiceClient:
    """Default factory: build an ``AiServiceClient`` for the given project.

    Static-token-only (v1 scope is Storage + Manage); the client's
    ``SESSION_AUTH_FEATURE`` makes a session sentinel fail fast on construction.
    """
    return AiServiceClient(stack_url=stack_url, token=token)


logger = logging.getLogger(__name__)


def _infer_component_type(component_id: str) -> str | None:
    """Infer ``componentType`` for Storage API filtering from a component ID.

    The list-components endpoint accepts ``componentType=extractor|writer|
    transformation|application`` as a pre-filter. When the caller already
    provides a fully-qualified ``component_id`` whose prefix encodes the
    type (standard ``keboola.<type>-...`` convention), we pass the hint
    along so the API can skip the unrelated component buckets entirely --
    on projects with many components this can shrink the response
    considerably. Returns ``None`` for custom/unknown layouts (e.g.
    ``kds-team.*``) so we fall back to the unfiltered listing rather than
    sending a guess to the API.
    """
    if not component_id:
        return None
    # keboola.ex-* extractors, keboola.wr-* writers, keboola.app-* applications
    if component_id.startswith("keboola.ex-"):
        return "extractor"
    if component_id.startswith("keboola.wr-"):
        return "writer"
    if component_id.startswith("keboola.app-"):
        return "application"
    # Transformations live under keboola.<backend>-transformation
    # (keboola.snowflake-transformation, keboola.python-transformation, etc.).
    if component_id.startswith("keboola.") and component_id.endswith("-transformation"):
        return "transformation"
    return None


def _default_change_description(command: str, *, has_metadata: bool, has_content: bool) -> str:
    """Build the default config-version ``changeDescription`` for a write.

    Used when the caller does not pass an explicit ``--change-description``.
    ``command`` is the kbagent command that produced the version (e.g.
    ``"config update"``), and the flags describe which parts changed so the
    version-history line reads e.g. ``Updated metadata + configuration via
    kbagent config update``.
    """
    parts = []
    if has_metadata:
        parts.append("metadata")
    if has_content:
        parts.append("configuration")
    return f"Updated {' + '.join(parts)} via kbagent {command}"


def _find_matches_in_json(
    obj: Any,
    match_fn: Any,
    path: str = "",
) -> list[str]:
    """Recursively walk a JSON-like object and return paths where match_fn(str_value) is True."""
    paths: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            paths.extend(_find_matches_in_json(value, match_fn, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_path = f"{path}[{i}]"
            paths.extend(_find_matches_in_json(item, match_fn, child_path))
    elif isinstance(obj, str):
        if match_fn(obj):
            paths.append(path)
    else:
        # Numbers, booleans -- convert to string for matching
        if obj is not None and match_fn(str(obj)):
            paths.append(path)
    return paths


class ConfigService(BaseService):
    """Business logic for listing and inspecting Keboola configurations.

    Supports multi-project aggregation: queries multiple projects in parallel
    using ThreadPoolExecutor, collects results, and reports per-project errors
    without stopping others.

    Uses dependency injection for config_store, client_factory, and
    ai_client_factory (the last one is only exercised by ``create_config``
    when the AI Service component-schema lookup runs).
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        ai_client_factory: AiClientFactory | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._ai_client_factory = ai_client_factory or _default_ai_client_factory

    def _fetch_project_configs(
        self,
        alias: str,
        project: ProjectConfig,
        component_type: str | None = None,
        component_id: str | None = None,
        branch_id: int | None = None,
        include_rows: bool = False,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Fetch configurations for a single project (runs in a worker thread).

        Creates its own KeboolaClient, fetches components and configs, then
        closes the client. Returns either (alias, configs_list, True) on success
        or (alias, error_dict) on failure. The 3-tuple convention is required
        by _run_parallel() which uses tuple length to distinguish success/error.

        When ``include_rows=True``, switches to the richer
        ``list_components_with_configs`` endpoint (``include=configuration,rows``)
        so each returned row carries the full ``configuration`` and ``rows``
        bodies. The payload is noticeably larger — use only when the caller
        actually needs the bodies (e.g. audit dashboards, bulk review).
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = branch_id or project.active_branch_id

            if include_rows:
                components = client.list_components_with_configs(
                    branch_id=effective_branch_id,
                    component_type=component_type,
                )
            else:
                components = client.list_components(
                    component_type=component_type,
                    branch_id=effective_branch_id,
                )

            # Fetch folder metadata (requires branch ID — search endpoint is branch-only)
            folder_map: dict[str, str] = {}
            try:
                # Use effective branch, active branch, or find the default branch ID
                folder_branch_id = effective_branch_id
                if not folder_branch_id:
                    # Fetch default branch ID from dev-branches endpoint
                    branches = client.list_dev_branches()
                    default = next((b for b in branches if b.get("isDefault")), None)
                    if default:
                        folder_branch_id = default["id"]
                if folder_branch_id:
                    result = client.list_config_folder_metadata(branch_id=folder_branch_id)
                    folder_map = result if isinstance(result, dict) else {}
            except Exception:
                pass  # graceful fallback if search endpoint unavailable

            configs: list[dict[str, Any]] = []
            for component in components:
                comp_id = component.get("id", "")
                comp_name = component.get("name", "")
                comp_type = component.get("type", "")

                # Apply component_id filter if specified
                if component_id and comp_id != component_id:
                    continue

                configurations = component.get("configurations", [])
                for cfg in configurations:
                    # Extract last-modified info from currentVersion
                    current_version = cfg.get("currentVersion", {})
                    creator_token = current_version.get("creatorToken", {})
                    cfg_id = str(cfg.get("id", ""))

                    entry: dict[str, Any] = {
                        "project_alias": alias,
                        "component_id": comp_id,
                        "component_name": comp_name,
                        "component_type": comp_type,
                        "config_id": cfg_id,
                        "config_name": cfg.get("name", ""),
                        "config_description": cfg.get("description", ""),
                        "last_modified": current_version.get("created", ""),
                        "last_modified_by": creator_token.get("description", ""),
                        "last_change_description": current_version.get("changeDescription", ""),
                        "folder": folder_map.get(f"{comp_id}/{cfg_id}", ""),
                    }

                    # When --include-rows, attach the full configuration + rows
                    # bodies under their API keys so callers can work with the
                    # same shape as `config detail`.
                    if include_rows:
                        entry["configuration"] = cfg.get("configuration", {})
                        entry["rows"] = cfg.get("rows", [])

                    configs.append(entry)
            return (alias, configs, True)
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
            logger.debug("Unexpected error listing configs for %s: %s", alias, exc)
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": sanitize_unexpected_error(exc),
                },
            )
        finally:
            client.close()

    def list_configs(
        self,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        component_id: str | None = None,
        branch_id: int | None = None,
        include_rows: bool = False,
    ) -> dict[str, Any]:
        """List configurations across one or multiple projects.

        Queries each resolved project for components and their configurations
        in parallel, flattens them into a unified list. Per-project errors are
        collected but do not stop other projects from being queried.

        Args:
            aliases: Project aliases to query. None means all projects.
            component_type: Optional filter by component type
                (extractor, writer, transformation, application).
            component_id: Optional filter by specific component ID
                (e.g. keboola.ex-db-snowflake).
            branch_id: If set, list configs from a specific dev branch.
                       If None, uses each project's active branch (if any).
            include_rows: When True, switches to
                ``list_components_with_configs`` (``include=configuration,rows``)
                so each returned row includes the full configuration body and
                config rows. Payload is noticeably larger -- reach for this only
                when the full bodies are actually needed (e.g. bulk audits).

        Returns:
            Dict with keys:
                - "configs": list of config dicts with project_alias,
                  component_id, component_name, component_type,
                  config_id, config_name, config_description.
                  With ``include_rows=True`` also: configuration, rows.
                - "errors": list of error dicts with project_alias,
                  error_code, message

        Raises:
            ConfigError: If a specified alias is not found (before querying).
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_configs(
                alias,
                project,
                component_type,
                component_id,
                branch_id=branch_id,
                include_rows=include_rows,
            )

        successes, errors = self._run_parallel(projects, worker)

        # Flatten configs from all successful projects
        all_configs: list[dict[str, Any]] = []
        for _alias, configs, _ok in successes:
            all_configs.extend(configs)

        # Sort for deterministic output
        all_configs.sort(key=lambda c: (c["project_alias"], c["component_id"], c["config_id"]))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"configs": all_configs, "errors": errors}

    def get_config_detail(
        self,
        alias: str,
        component_id: str,
        config_id: str | None = None,
        branch_id: int | None = None,
        with_state: bool = False,
        aliases: list[str] | None = None,
        include_sandbox_annotation: bool = False,
    ) -> dict[str, Any]:
        """Get detailed information about one or many configurations.

        Two modes, switched by ``config_id`` and ``aliases``:

        * **Single-config mode** (``config_id`` given, single ``alias``):
          returns the full config detail dict from the API, flattened with
          ``project_alias`` + ``branch_id``. Shape unchanged for backward
          compatibility. When ``with_state=True``, the ``state`` key
          returned inline by ``get_config_detail`` is normalised to a dict
          (Storage API already embeds ``state`` in the detail response --
          there is no separate endpoint to refresh it with, so no extra
          HTTP call is made).
        * **Bulk mode** (``config_id`` is None): returns
          ``{"configs": [...], "errors": [...]}`` -- every configuration of
          the given ``component_id`` across one or more projects, each row
          tagged with ``project_alias``. Uses the component listing endpoint
          (one request per project, not one per config) so 100+ configs come
          back in a single round-trip per project. When ``with_state=True``,
          ``include=state`` is added so each config's state is embedded in
          the same request (no N+1 state fetches).

        Args:
            alias: Project alias (single-project). When ``aliases`` is given,
                this argument is ignored.
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: Optional configuration ID. When omitted, enables
                bulk mode and returns every config under ``component_id``.
            branch_id: If set, read from a specific dev branch. Only valid
                with a single project; otherwise ``None`` falls back to each
                project's active branch.
            with_state: When True, attach the runtime state dict under the
                ``state`` key. Single mode: reuses the ``state`` field
                already embedded in the detail response (no extra HTTP
                call). Bulk mode: embeds state inline via ``include=state``
                on the listing call (one request per project, regardless
                of config count).
            aliases: Multi-project bulk form. When a list is given,
                ``config_id`` must be None and ``branch_id`` must be None.
                Returns ``{"configs": [...], "errors": [...]}`` with every
                row tagged by ``project_alias``.
            include_sandbox_annotation: Opt-in enrichment for
                ``component_id == "keboola.sandboxes"`` in single-config
                mode. When True, the response gains a
                ``sandbox_annotation`` block with ``sandbox_service_id``
                (the misleading ``configuration.parameters.id``) and
                ``storage_workspace_id`` (the actual Storage workspace ID,
                resolved via an extra ``GET /v2/storage/workspaces``).
                Default False to keep this method a clean API wrapper for
                programmatic callers (closes #312 -- HTTP/REST parity gap
                left by #304). Bulk mode is N+1-sensitive (one extra HTTP
                round-trip per config), so the flag is silently ignored
                there.

        Returns:
            Dict. Shape depends on mode:
              * Single: full API detail + ``project_alias`` + ``branch_id``
                (+ ``state`` when ``with_state=True``).
              * Bulk: ``{"configs": [...], "errors": [...]}`` where each
                config row has ``project_alias``, ``branch_id``,
                ``component_id``, ``config_id``, ``name``, ``description``,
                ``configuration``, ``rows``, ``currentVersion`` (+ ``state``
                when ``with_state=True``).

        Raises:
            ConfigError: If a specified alias is not found, or if
                ``aliases`` is combined with ``config_id``/``branch_id``.
            KeboolaApiError: If the API call fails (single mode only;
                bulk mode surfaces per-project failures in ``errors``).
        """
        # --- Bulk mode (fan-out across one or more projects) ----------------
        if config_id is None:
            if aliases is None:
                aliases = [alias]
            # --branch only makes sense for a single project -- a dev branch ID
            # is per-project, so reject it when callers fan out. Single-project
            # bulk still honors --branch.
            if branch_id is not None and len(aliases) != 1:
                raise ConfigError(
                    "--branch is only valid with exactly one --project "
                    "(branch IDs are per-project)."
                )
            return self._get_config_detail_bulk(
                aliases=aliases,
                component_id=component_id,
                branch_id=branch_id,
                with_state=with_state,
            )

        # --- Single-config mode (unchanged shape) ---------------------------
        # Multi-project + explicit config_id is not a supported combination;
        # reject it explicitly instead of silently using the first alias.
        if aliases is not None and len(aliases) > 1:
            raise ConfigError(
                "Passing multiple --project aliases requires omitting "
                "--config-id (bulk mode returns all configs per project)."
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_config_detail(
                component_id, config_id, branch_id=effective_branch_id
            )
            if with_state:
                # Storage API embeds ``state`` inline in the detail
                # response -- there is no standalone state endpoint to
                # refresh with (see get_config_state docstring). Normalise
                # the field to a dict so callers can rely on the shape.
                detail.setdefault("state", {})
                if not isinstance(detail["state"], dict):
                    detail["state"] = {}
            # Sandbox annotation enrichment (issue #312 / #304 HTTP parity).
            # Opt-in (default off) so existing programmatic consumers keep
            # the unchanged shape. The extra ``list_workspaces`` HTTP call
            # is intentional: there is no per-config sandbox→workspace
            # endpoint, and reusing the same client keeps retry/backoff +
            # branch routing consistent with the detail call above.
            sandbox_annotation: dict[str, Any] | None = None
            if include_sandbox_annotation and component_id == "keboola.sandboxes":
                sandbox_service_id = (
                    (detail.get("configuration") or {}).get("parameters", {}).get("id")
                )
                try:
                    workspaces = client.list_workspaces(branch_id=effective_branch_id)
                    storage_workspace_id = find_storage_workspace_for_sandbox_config(
                        workspaces, config_id
                    )
                except KeboolaApiError:
                    # Best-effort: do not fail the detail fetch just because
                    # the workspace listing endpoint hiccuped -- the
                    # annotation is a UX nicety, not a contract. The caller
                    # still gets the raw detail.
                    storage_workspace_id = None
                sandbox_annotation = {
                    "sandbox_service_id": sandbox_service_id,
                    "storage_workspace_id": storage_workspace_id,
                    "note": (
                        "`parameters.id` in a keboola.sandboxes config is the "
                        "sandbox-service internal ID, NOT the Storage workspace ID. "
                        "Use `storage_workspace_id` with `kbagent workspace detail "
                        "--workspace-id ...`."
                    ),
                }
        finally:
            client.close()

        detail["project_alias"] = alias
        detail["branch_id"] = effective_branch_id
        if sandbox_annotation is not None:
            detail["sandbox_annotation"] = sandbox_annotation
        return detail

    def _get_config_detail_bulk(
        self,
        aliases: list[str],
        component_id: str,
        branch_id: int | None,
        with_state: bool,
    ) -> dict[str, Any]:
        """Fan-out helper for bulk-mode ``get_config_detail``.

        Fetches every configuration of ``component_id`` across one or many
        projects in parallel. One HTTP request per project (not per config);
        state -- when requested -- rides on the same request via
        ``include=state``. Per-project failures are captured in ``errors``
        without aborting other projects.
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_component_configs(
                alias,
                project,
                component_id=component_id,
                branch_id=branch_id,
                with_state=with_state,
            )

        successes, errors = self._run_parallel(projects, worker)

        all_configs: list[dict[str, Any]] = []
        for _alias, configs, _ok in successes:
            all_configs.extend(configs)

        # Stable sort: alias, then config_id for deterministic output.
        all_configs.sort(key=lambda c: (c["project_alias"], str(c.get("config_id", ""))))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"configs": all_configs, "errors": errors}

    def _fetch_project_component_configs(
        self,
        alias: str,
        project: ProjectConfig,
        component_id: str,
        branch_id: int | None = None,
        with_state: bool = False,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Worker for bulk ``get_config_detail``: fetch all configs of a component.

        Uses ``list_components_with_configs(include=configuration,rows[,state])``
        -- a single request returns every configuration body, rows, and
        optionally state. Filters to ``component_id`` in memory.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = branch_id or project.active_branch_id
            # Pre-filter to the matching component bucket when the
            # component_id prefix encodes a known type (keboola.ex-*,
            # keboola.wr-*, keboola.*-transformation, keboola.app-*). The
            # API then returns a smaller payload; we still filter by the
            # exact component_id in memory below to handle same-type peers.
            components = client.list_components_with_configs(
                branch_id=effective_branch_id,
                component_type=_infer_component_type(component_id),
                include_state=with_state,
            )

            configs: list[dict[str, Any]] = []
            for component in components:
                if component.get("id") != component_id:
                    continue
                comp_name = component.get("name", "")
                comp_type = component.get("type", "")
                for cfg in component.get("configurations", []):
                    cfg_id = str(cfg.get("id", ""))
                    entry: dict[str, Any] = {
                        "project_alias": alias,
                        "branch_id": effective_branch_id,
                        "component_id": component_id,
                        "component_name": comp_name,
                        "component_type": comp_type,
                        "config_id": cfg_id,
                        "name": cfg.get("name", ""),
                        "description": cfg.get("description", ""),
                        "configuration": cfg.get("configuration", {}),
                        "rows": cfg.get("rows", []),
                        "rowsSortOrder": cfg.get("rowsSortOrder", []),
                        "version": cfg.get("version"),
                        "isDisabled": cfg.get("isDisabled", False),
                        "isDeleted": cfg.get("isDeleted", False),
                        "changeDescription": cfg.get("changeDescription", ""),
                        "created": cfg.get("created", ""),
                        "currentVersion": cfg.get("currentVersion", {}),
                        "creatorToken": cfg.get("creatorToken", {}),
                    }
                    if with_state:
                        entry["state"] = cfg.get("state", {})
                    configs.append(entry)
            return (alias, configs, True)
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
            logger.debug("Unexpected error fetching component configs for %s: %s", alias, exc)
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": sanitize_unexpected_error(exc),
                },
            )
        finally:
            client.close()

    def _encrypt_secrets_before_write(
        self,
        client: Any,
        project: ProjectConfig,
        component_id: str,
        configuration: dict[str, Any] | None,
        *,
        allow_plaintext_fallback: bool,
    ) -> dict[str, Any] | None:
        """Encrypt ``#``-prefixed secrets in *configuration* before it is written.

        The Storage API stores configuration JSON verbatim -- it does **not**
        encrypt ``#``-prefixed values server-side. Unless the client pre-encrypts
        them via the Encryption API, secrets land in Storage as plaintext
        (readable in every config version, re-exposed on every read, and handed
        live to sync actions like ``testConnection``). This mirrors the
        encrypt-before-write contract already enforced by ``sync push`` and the
        variables path. See issue #378.

        Fail-closed by default: a failed or un-scopable encryption raises
        :class:`KeboolaApiError` (``ENCRYPTION_FAILED``) rather than writing
        plaintext. ``allow_plaintext_fallback=True`` downgrades that to a logged
        warning (bootstrap/debug only).

        The ``project_id`` needed for the Encryption API scope is read from the
        stored project config and falls back to ``verify_token`` (covers configs
        added before project_id was persisted and env-synthesized projects). It
        is resolved only when secrets are actually present, so secret-free writes
        skip the extra round-trip entirely.
        """
        if not configuration:
            return configuration

        secrets: dict[str, str] = {}
        collect_secrets(configuration, "", secrets)
        if not secrets:
            return configuration

        project_id = project.project_id or client.verify_token().project_id
        if not project_id:
            # Secrets present but the Encryption API call cannot be scoped.
            # Fail closed rather than silently write plaintext.
            if allow_plaintext_fallback:
                # GHSA-7jrf: name the exact secret key-paths written in PLAINTEXT
                # (keys only -- `secrets` maps flattened path -> value -- never the
                # values), consistent with the encryption-failure warning in
                # `encrypt_secrets_in_config`. No plaintext-write path stays silent.
                logger.warning(
                    "Cannot resolve project_id for %s; --allow-plaintext-on-encrypt-failure "
                    "is set, so %d secret value(s) are being written in PLAINTEXT: %s.",
                    component_id,
                    len(secrets),
                    ", ".join(sorted(secrets)) or "(unable to enumerate)",
                )
                return configuration
            raise KeboolaApiError(
                message=(
                    f"Cannot resolve project_id for {component_id} to encrypt "
                    f"secrets. Refusing to write plaintext secrets. Use "
                    f"--allow-plaintext-on-encrypt-failure to override."
                ),
                status_code=0,
                error_code=ErrorCode.ENCRYPTION_FAILED,
            )

        return encrypt_secrets_in_config(
            client,
            project_id,
            component_id,
            configuration,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )

    def update_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str | None = None,
        description: str | None = None,
        configuration: dict[str, Any] | None = None,
        set_paths: list[tuple[str, Any]] | None = None,
        merge: bool = False,
        dry_run: bool = False,
        change_description: str | None = None,
        branch_id: int | None = None,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Update a configuration's metadata and/or content.

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID to update.
            name: New name (if None, not changed).
            description: New description (if None, not changed).
            configuration: Full configuration dict to set/merge.
            set_paths: List of (path, value) tuples for targeted updates
                       (e.g. ``[("parameters.tables", {...})]``).
            merge: If True, deep-merge *configuration* or *set_paths* into
                   the existing config instead of replacing.  When using
                   *set_paths* merge is always implied.
            change_description: Text stored as the new config version's
                   ``changeDescription`` (the version-history audit line). When
                   ``None`` a default is generated from what changed.
            dry_run: If True, compute and return the diff without applying.
            branch_id: If set, update in a specific dev branch.
                       If None, uses the project's active branch (if any).
            allow_plaintext_fallback: If True, write ``#``-secrets as plaintext
                when the Encryption API fails instead of raising. DANGEROUS --
                see :meth:`_encrypt_secrets_before_write`.

        Returns:
            Dict with the updated configuration from the API.
            When *dry_run* is True the dict contains ``"dry_run": True``
            and a ``"changes"`` list instead of the API response.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        has_content = configuration is not None or bool(set_paths)
        has_metadata = name is not None or description is not None

        if not has_content and not has_metadata:
            raise KeboolaApiError(
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                message=(
                    "At least one of --name, --description, --configuration, "
                    "--configuration-file, or --set must be provided."
                ),
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            final_config: dict[str, Any] | None = None
            normalizations: list[dict[str, Any]] = []

            if has_content:
                final_config = self._resolve_configuration(
                    client=client,
                    component_id=component_id,
                    config_id=config_id,
                    configuration=configuration,
                    set_paths=set_paths,
                    merge=merge,
                    branch_id=effective_branch_id,
                )
                # Defense-in-depth: Storage API silently accepts a string
                # for parameters.blocks[].codes[].script but the runtime
                # validator rejects it ("Expected array, got string"),
                # turning the broken push into a delayed, hard-to-attribute
                # job-time crash. See issue #245.
                final_config, normalizations = normalize_blocks_codes_script(
                    component_id, final_config
                )

            change_desc = change_description or _default_change_description(
                "config update", has_metadata=has_metadata, has_content=has_content
            )

            if dry_run:
                current = client.get_config_detail(
                    component_id, config_id, branch_id=effective_branch_id
                )
                old_cfg = current.get("configuration", {})
                new_cfg = final_config if final_config is not None else old_cfg
                changes = compute_diff(old_cfg, new_cfg)
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "branch_id": effective_branch_id,
                    "changes": changes,
                    "change_description": change_desc,
                    "old_configuration": old_cfg,
                    "new_configuration": new_cfg,
                    "normalizations": normalizations,
                }

            # Encrypt #-prefixed secrets before they reach Storage (issue #378).
            # Only on a real write -- dry-run keeps plaintext so the diff stays
            # readable and deterministic (ciphertext is non-deterministic).
            if final_config is not None:
                final_config = self._encrypt_secrets_before_write(
                    client,
                    project,
                    component_id,
                    final_config,
                    allow_plaintext_fallback=allow_plaintext_fallback,
                )

            result = client.update_config(
                component_id=component_id,
                config_id=config_id,
                name=name,
                description=description,
                configuration=final_config,
                change_description=change_desc,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        result["normalizations"] = normalizations
        # Surface a plaintext-on-encrypt-failure fallback structurally (not just
        # via the stderr warning) so --json consumers see the leaked key-paths.
        # find_plaintext_secret_keys returns [] when encryption succeeded.
        result["plaintext_written"] = (
            find_plaintext_secret_keys(final_config) if final_config else []
        )
        return result

    def _resolve_configuration(
        self,
        client: Any,
        component_id: str,
        config_id: str,
        configuration: dict[str, Any] | None,
        set_paths: list[tuple[str, Any]] | None,
        merge: bool,
        branch_id: int | None,
    ) -> dict[str, Any]:
        """Build the final configuration dict by merging/setting paths.

        When *merge* is True or *set_paths* are given, the current
        configuration is fetched from the API and changes are applied
        on top of it (deep-merge for dicts, replace for scalars/lists).
        """
        needs_current = merge or bool(set_paths)

        if needs_current:
            current_detail = client.get_config_detail(component_id, config_id, branch_id=branch_id)
            current_cfg: dict[str, Any] = current_detail.get("configuration", {})
            if isinstance(current_cfg, str):
                current_cfg = json.loads(current_cfg)
        else:
            current_cfg = {}

        if set_paths:
            result = current_cfg
            for path, value in set_paths:
                result = set_nested_value(result, path, value)
            if configuration:
                result = deep_merge(result, configuration)
            return result

        if merge and configuration:
            return deep_merge(current_cfg, configuration)

        # Full replace (no merge, no set_paths)
        return configuration if configuration is not None else current_cfg

    def set_default_bucket(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        bucket: str | None,
        clear: bool = False,
        dry_run: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set or clear ``configuration.storage.output.default_bucket``.

        Read-modify-write: fetches the current configuration, edits the single
        nested key, and PUTs the full body back. Sibling keys under
        ``storage.output`` (and the rest of the configuration) are preserved.

        Args:
            alias: Project alias.
            component_id: Component ID.
            config_id: Configuration ID.
            bucket: Bucket ID to set (e.g. ``"in.c-preferred-name"``).
                Required when *clear* is False.
            clear: If True, remove the ``default_bucket`` key. Mutually
                exclusive with *bucket*.
            dry_run: If True, return a diff without writing.
            branch_id: Dev branch override; falls back to the project's
                active branch when None.

        Returns:
            On a real write: the API response with ``project_alias`` and
            ``branch_id`` annotations attached.
            On a no-op (set with same value, clear when key absent):
            ``{"changed": False, ...}`` -- no API write.
            On dry_run: ``{"dry_run": True, "changes": [...], ...}``
            mirroring :py:meth:`update_config`'s dry-run shape.

        Raises:
            KeboolaApiError: For validation failures (both/neither flag,
                empty bucket) and underlying API errors.
            ConfigError: When the alias is unknown.
        """
        if clear and bucket is not None:
            raise KeboolaApiError(
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                message="Pass exactly one of --bucket or --clear, not both.",
            )
        if not clear and not bucket:
            raise KeboolaApiError(
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                message="Pass --bucket BUCKET_ID or --clear.",
            )
        if bucket is not None:
            bucket = bucket.strip()
            if not bucket:
                raise KeboolaApiError(
                    status_code=400,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    message="--bucket cannot be empty.",
                )

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            current_detail = client.get_config_detail(
                component_id, config_id, branch_id=effective_branch_id
            )
            current_cfg: dict[str, Any] = current_detail.get("configuration", {}) or {}
            if isinstance(current_cfg, str):
                current_cfg = json.loads(current_cfg)

            storage = current_cfg.get("storage")
            output = storage.get("output") if isinstance(storage, dict) else None
            existing = output.get("default_bucket") if isinstance(output, dict) else None

            # Semantic no-op: short-circuit before doing any tree mutation.
            # Catches: clear-when-already-absent, set-to-same-value.
            target = None if clear else bucket
            if existing == target:
                return {
                    "changed": False,
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "branch_id": effective_branch_id,
                    "default_bucket": existing,
                }

            # Tolerate storage=None or storage.output=None (raw-mode JSON edits can
            # serialize nulls where the API would omit the key entirely).
            new_cfg = copy.deepcopy(current_cfg)
            if not isinstance(new_cfg.get("storage"), dict):
                new_cfg["storage"] = {}
            if not isinstance(new_cfg["storage"].get("output"), dict):
                new_cfg["storage"]["output"] = {}

            if clear:
                new_cfg["storage"]["output"].pop("default_bucket", None)
            else:
                new_cfg["storage"]["output"]["default_bucket"] = bucket

            if dry_run:
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "branch_id": effective_branch_id,
                    "changes": compute_diff(current_cfg, new_cfg),
                    "old_configuration": current_cfg,
                    "new_configuration": new_cfg,
                }

            change_desc = (
                "Cleared storage.output.default_bucket via kbagent config set-default-bucket"
                if clear
                else f"Set storage.output.default_bucket={bucket} via kbagent config set-default-bucket"
            )
            result = client.update_config(
                component_id=component_id,
                config_id=config_id,
                configuration=new_cfg,
                change_description=change_desc,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        result["default_bucket"] = target
        return result

    def delete_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a configuration from a project.

        Args:
            alias: Project alias.
            component_id: The component ID (e.g. keboola.python-transformation-v2).
            config_id: The configuration ID to delete.
            branch_id: If set, delete from a specific dev branch.
                       If None, uses the project's active branch (if any).

        Returns:
            Dict with deletion confirmation details.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        # Use active branch if no explicit branch_id given
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_config(
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        return {
            "status": "deleted",
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "branch_id": effective_branch_id,
        }

    def rename_config(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str,
        branch_id: int | None = None,
        directory: Path | None = None,
    ) -> dict[str, Any]:
        """Rename a configuration (update name via API + rename local sync dir).

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID to rename.
            name: The new configuration name.
            branch_id: If set, rename in a specific dev branch.
                       If None, uses the project's active branch (if any).
            directory: Optional sync working directory. If a manifest exists
                       here and tracks this config, the local directory is
                       renamed and the manifest path is updated.

        Returns:
            Dict with old name, new name, and optional sync rename details.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Fetch current state to get old name
            current = client.get_config_detail(
                component_id, config_id, branch_id=effective_branch_id
            )
            old_name = current.get("name", "")

            # Update name via API
            client.update_config(
                component_id=component_id,
                config_id=config_id,
                name=name,
                change_description=f"Renamed via kbagent config rename: {old_name} -> {name}",
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result: dict[str, Any] = {
            "status": "renamed",
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "old_name": old_name,
            "new_name": name,
            "branch_id": effective_branch_id,
        }

        # Attempt local sync directory rename if applicable
        sync_result = self._rename_sync_directory(
            directory=directory,
            component_id=component_id,
            config_id=config_id,
            new_name=name,
        )
        if sync_result:
            result["sync"] = sync_result

        return result

    def _rename_sync_directory(
        self,
        directory: Path | None,
        component_id: str,
        config_id: str,
        new_name: str,
    ) -> dict[str, str] | None:
        """Rename the local sync directory for a config if a manifest tracks it.

        Returns a dict with old_path/new_path on success, or None if no
        sync directory was found or rename was not needed.
        """
        if directory is None:
            return None

        from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME

        manifest_path = directory / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
        if not manifest_path.exists():
            return None

        try:
            manifest = load_manifest(directory)
        except (FileNotFoundError, ValueError):
            return None

        # Find the config entry in the manifest
        target_cfg = None
        for cfg in manifest.configurations:
            if cfg.component_id == component_id and cfg.id == config_id:
                target_cfg = cfg
                break

        if target_cfg is None:
            return None

        # Compute new path using the naming template
        old_path = target_cfg.path
        old_basename = old_path.rsplit("/", 1)[-1] if "/" in old_path else old_path
        new_basename = sanitize_name(new_name)

        if old_basename == new_basename:
            return None  # No rename needed

        # Build new path: replace only the last segment (config name)
        if "/" in old_path:
            parent = old_path.rsplit("/", 1)[0]
            new_path = f"{parent}/{new_basename}"
        else:
            new_path = new_basename

        # Collision detection: if target already exists, append numeric suffix
        branch_dir = self._find_sync_branch_dir(manifest, directory)
        if branch_dir is None:
            return None

        target_dir = branch_dir / new_path
        if target_dir.exists():
            counter = 2
            while (branch_dir / f"{new_path}-{counter}").exists():
                counter += 1
            new_path = f"{new_path}-{counter}"
            target_dir = branch_dir / new_path

        # Perform the rename
        source_dir = branch_dir / old_path
        if not source_dir.exists():
            # Directory doesn't exist locally, just update manifest
            target_cfg.path = new_path
            target_cfg.metadata.pop("pull_hash", None)
            target_cfg.metadata.pop("pull_config_hash", None)
            save_manifest(directory, manifest)
            return {"old_path": old_path, "new_path": new_path, "method": "manifest_only"}

        # Try git mv first for cleaner history, fall back to shutil.move
        method = self._move_directory(source_dir, target_dir)

        # Update manifest
        target_cfg.path = new_path
        target_cfg.metadata.pop("pull_hash", None)
        target_cfg.metadata.pop("pull_config_hash", None)
        save_manifest(directory, manifest)

        # Clean up empty parent directories
        parent_dir = source_dir.parent
        while parent_dir != branch_dir and parent_dir.exists():
            if not any(parent_dir.iterdir()):
                parent_dir.rmdir()
                parent_dir = parent_dir.parent
            else:
                break

        return {"old_path": old_path, "new_path": new_path, "method": method}

    @staticmethod
    def _move_directory(source: Path, target: Path) -> str:
        """Move a directory, using git mv if in a git repo, else shutil.move."""
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["git", "mv", str(source), str(target)],
                cwd=source.parent,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return "git_mv"
        except FileNotFoundError:
            pass  # git not installed
        shutil.move(str(source), str(target))
        return "shutil_move"

    @staticmethod
    def _find_sync_branch_dir(manifest: Manifest, project_root: Path) -> Path | None:
        """Find the branch directory within a sync project root."""
        if not manifest.branches:
            return None
        # Use the first branch (typically "main")
        branch_path = manifest.branches[0].path
        branch_dir = project_root / branch_path
        return branch_dir if branch_dir.exists() else None

    def _resolve_metadata_branch_id(
        self, project: ProjectConfig, client: Any, branch_id: int | None
    ) -> int:
        """Resolve the branch ID required by the config metadata API.

        Config metadata endpoints only support the branch-aware route
        (/v2/storage/branch/{id}/...). This method resolves the effective
        branch: explicit arg → active branch → default branch from API.

        Raises ConfigError if no default branch can be found.
        """
        effective = branch_id or project.active_branch_id
        if effective:
            return int(effective)
        try:
            branches = client.list_dev_branches()
        except KeboolaApiError as exc:
            raise ConfigError(
                f"Could not list branches to resolve metadata branch: {exc.message}. "
                "Pass --branch explicitly."
            ) from exc
        except Exception as exc:
            raise ConfigError(
                f"Unexpected error listing branches for metadata route: {exc}. "
                "Pass --branch explicitly."
            ) from exc
        default = next((b for b in branches if b.get("isDefault")), None)
        if default:
            return int(default["id"])
        raise ConfigError(
            "Could not determine a branch for config metadata. "
            "Set an active branch with 'kbagent branch use' or pass --branch."
        )

    def list_config_metadata(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List all metadata entries on a configuration.

        Returns:
            Dict with project_alias, component_id, config_id, branch_id,
            and a key-sorted metadata list.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = self._resolve_metadata_branch_id(project, client, branch_id)
            entries = client.list_config_metadata(
                component_id, config_id, branch_id=effective_branch_id
            )
            return {
                "project_alias": alias,
                "component_id": component_id,
                "config_id": config_id,
                "branch_id": effective_branch_id,
                "metadata": sorted(entries, key=lambda e: e.get("key", "")),
            }
        finally:
            client.close()

    def get_config_metadata_value(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        key: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get a single metadata value by key.

        Raises KeboolaApiError(NOT_FOUND) if the key is absent.
        """
        result = self.list_config_metadata(alias, component_id, config_id, branch_id=branch_id)
        for entry in result["metadata"]:
            if entry.get("key") == key:
                return {
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "branch_id": result["branch_id"],
                    "key": key,
                    "value": entry.get("value"),
                    "metadata_id": entry.get("id"),
                }
        raise KeboolaApiError(
            message=f"Metadata key '{key}' not found on config '{component_id}/{config_id}'.",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )

    def set_config_metadata(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        key: str,
        value: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set a single metadata key/value on a configuration (upsert)."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = self._resolve_metadata_branch_id(project, client, branch_id)
            result = client.set_config_metadata(
                component_id, config_id, entries=[(key, value)], branch_id=effective_branch_id
            )
            return {
                "project_alias": alias,
                "component_id": component_id,
                "config_id": config_id,
                "branch_id": effective_branch_id,
                "key": key,
                "value": value,
                "result": result,
                "message": (
                    f"Metadata '{key}' set on config '{component_id}/{config_id}' in project '{alias}'."
                ),
            }
        finally:
            client.close()

    def delete_config_metadata(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        metadata_id: int | str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a metadata entry by its numeric ID."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = self._resolve_metadata_branch_id(project, client, branch_id)
            client.delete_config_metadata(
                component_id, config_id, metadata_id, branch_id=effective_branch_id
            )
            return {
                "project_alias": alias,
                "component_id": component_id,
                "config_id": config_id,
                "branch_id": effective_branch_id,
                "metadata_id": metadata_id,
                "message": (
                    f"Metadata ID {metadata_id} deleted from config "
                    f"'{component_id}/{config_id}' in project '{alias}'."
                ),
            }
        finally:
            client.close()

    def set_config_folder(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        folder_name: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set the folder name on a configuration (KBC.configuration.folderName)."""
        result = self.set_config_metadata(
            alias,
            component_id,
            config_id,
            key="KBC.configuration.folderName",
            value=folder_name,
            branch_id=branch_id,
        )
        result["folder"] = folder_name
        result["message"] = (
            f"Folder '{folder_name}' set on config '{component_id}/{config_id}' in project '{alias}'."
        )
        return result

    def search_configs(
        self,
        query: str,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        component_id: str | None = None,
        ignore_case: bool = False,
        use_regex: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Search through configuration bodies across projects.

        Fetches all configurations (including the full JSON body) and searches
        for a query string. Reports which configs match and WHERE in the JSON
        tree the match was found.

        Args:
            query: Search string (plain substring or regex).
            aliases: Project aliases to query. None means all projects.
            component_type: Optional filter by component type.
            component_id: Optional filter by specific component ID.
            ignore_case: If True, match case-insensitively.
            use_regex: If True, interpret query as a regular expression.
            branch_id: If set, search configs from a specific dev branch.
                       If None, uses each project's active branch (if any).

        Returns:
            Dict with "matches", "errors", and "stats" keys.
        """
        # Compile the match function once
        if use_regex:
            flags = re.IGNORECASE if ignore_case else 0
            pattern = re.compile(query, flags)
            match_fn = lambda s: pattern.search(s) is not None  # noqa: E731
        elif ignore_case:
            query_lower = query.lower()
            match_fn = lambda s: query_lower in s.lower()  # noqa: E731
        else:
            match_fn = lambda s: query in s  # noqa: E731

        projects = self.resolve_projects(aliases)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, dict[str, Any], bool] | tuple[str, dict[str, str]]:
            return self._search_project_configs(
                alias, project, match_fn, component_type, component_id, branch_id=branch_id
            )

        successes, errors = self._run_parallel(projects, worker)

        all_matches: list[dict[str, Any]] = []
        total_configs = 0
        for _alias, result, _ok in successes:
            all_matches.extend(result["matches"])
            total_configs += result["configs_searched"]

        all_matches.sort(key=lambda m: (m["project_alias"], m["component_id"], m["config_id"]))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "matches": all_matches,
            "errors": errors,
            "stats": {
                "projects_searched": len(successes),
                "configs_searched": total_configs,
                "matches_found": len(all_matches),
            },
        }

    def _search_project_configs(
        self,
        alias: str,
        project: ProjectConfig,
        match_fn: Any,
        component_type: str | None = None,
        component_id: str | None = None,
        branch_id: int | None = None,
    ) -> tuple[str, dict[str, Any], bool] | tuple[str, dict[str, str]]:
        """Search configs in a single project (worker thread).

        Uses ``list_components_with_configs`` (``include=configuration,rows``)
        so that row-level configuration (Snowflake writer rows, DB extractor
        tables, Google Sheets sheets, etc.) is included in the search tree.
        Without ``rows``, the API returns only the top-level configuration
        body, and searches for row-only properties always miss (see #196).
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_branch_id = branch_id or project.active_branch_id
            components = client.list_components_with_configs(
                branch_id=effective_branch_id,
                component_type=component_type,
            )
            matches: list[dict[str, Any]] = []
            configs_searched = 0

            for component in components:
                comp_id = component.get("id", "")
                comp_name = component.get("name", "")
                comp_type = component.get("type", "")

                if component_id and comp_id != component_id:
                    continue

                for cfg in component.get("configurations", []):
                    configs_searched += 1
                    match_locations = _find_matches_in_json(cfg, match_fn)

                    if match_locations:
                        matches.append(
                            {
                                "project_alias": alias,
                                "component_id": comp_id,
                                "component_name": comp_name,
                                "component_type": comp_type,
                                "config_id": str(cfg.get("id", "")),
                                "config_name": cfg.get("name", ""),
                                "config_description": cfg.get("description", ""),
                                "match_locations": match_locations,
                                "match_count": len(match_locations),
                            }
                        )

            return (alias, {"matches": matches, "configs_searched": configs_searched}, True)
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

    # ── config row-create ──────────────────────────────────────────────────────

    def create_config_row(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        name: str,
        description: str = "",
        configuration: dict[str, Any] | None = None,
        is_disabled: bool = False,
        branch_id: int | None = None,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Create a new configuration row.

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID the row belongs to.
            name: Row name (required by Storage API).
            description: Optional row description.
            configuration: Row-level configuration dict. Defaults to empty dict.
            is_disabled: Create the row in disabled state (excluded from job runs).
            branch_id: If set, create in a specific dev branch. Falls back to
                the project's active branch when None.
            allow_plaintext_fallback: If True, write ``#``-secrets as plaintext
                when the Encryption API fails instead of raising. DANGEROUS --
                see :meth:`_encrypt_secrets_before_write`.

        Returns:
            The created row dict from the API (includes the new 'id').

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id
        client = self._client_factory(project.stack_url, project.token)
        try:
            # Encrypt #-prefixed secrets before they reach Storage (issue #378).
            row_config = self._encrypt_secrets_before_write(
                client,
                project,
                component_id,
                configuration if configuration is not None else {},
                allow_plaintext_fallback=allow_plaintext_fallback,
            )
            result = client.create_config_row(
                component_id=component_id,
                config_id=config_id,
                name=name,
                configuration=row_config if row_config is not None else {},
                description=description,
                is_disabled=is_disabled,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        # Structurally surface any plaintext-fallback leak (empty when encrypted).
        result["plaintext_written"] = find_plaintext_secret_keys(row_config) if row_config else []
        return result

    # ── config create (one-shot remote create via `config new --push`) ─────────

    def create_config(
        self,
        alias: str,
        component_id: str,
        name: str,
        description: str = "",
        configuration: dict[str, Any] | None = None,
        branch_id: int | None = None,
        dry_run: bool = False,
        validate: bool = True,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Create a new configuration via the Storage API (one-shot remote).

        Backs the ``kbagent config new --push`` lifecycle path. When a body is
        passed explicitly (via ``configuration``), the body is validated
        against the component's AI Service JSON schema before POSTing (unless
        ``validate=False``); on validation failure raises ``ConfigError``.
        When no body is passed (default = ``{}``), validation is auto-skipped
        because empty shells almost always fail component schemas that require
        parameters -- this is FIIA's "empty shell, patch later" pattern.

        Args:
            alias: Project alias.
            component_id: The component ID.
            name: Configuration name (required by Storage API).
            description: Optional description.
            configuration: Configuration body dict. ``None`` => default empty
                shell ``{}`` and auto-skipped validation.
            branch_id: If set, create in a specific dev branch. Falls back to
                the project's active branch when None.
            dry_run: If True, return the planned POST envelope (including
                validation result) without calling the Storage API.
            validate: If True (default), validate ``configuration`` against
                the component schema when a body is explicitly provided.
            allow_plaintext_fallback: If True, write ``#``-secrets as plaintext
                when the Encryption API fails instead of raising. DANGEROUS --
                see :meth:`_encrypt_secrets_before_write`.

        Returns:
            The created configuration dict from the API (includes the new
            ``id``) annotated with ``project_alias``, ``branch_id``, and
            ``validation_status``. When ``dry_run`` the dict contains
            ``dry_run: True`` plus the planned POST fields and validation
            envelope, with no API call.

        Raises:
            ConfigError: If the alias is not found, or if validation runs
                and fails on a real (non-dry-run) create.
            KeboolaApiError: If the Storage API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id

        body_was_explicit = configuration is not None
        effective_config: dict[str, Any] = configuration if body_was_explicit else {}

        # Validation only runs when the caller passed an explicit body (i.e.
        # ``configuration`` is not ``None``). When no body is passed at all,
        # the effective body defaults to ``{}`` and validation auto-skips --
        # most component schemas require parameters and would reject ``{}``,
        # which would block FIIA's "empty shell, then patch via
        # ``config update``" pattern. An *explicit* ``configuration={}`` IS
        # validated (and typically fails for the same reason) -- pass
        # ``--no-validate`` or omit ``--configuration`` to skip.
        if validate and body_was_explicit:
            validation_status, validation_errors = self._validate_config_body(
                project, component_id, effective_config
            )
        else:
            validation_status = "skipped"
            validation_errors = []

        if validation_status == "failed" and not dry_run:
            joined = "\n  - ".join(validation_errors)
            raise ConfigError(
                f"Configuration body failed schema validation for '{component_id}':\n  - {joined}"
            )

        if dry_run:
            return {
                "dry_run": True,
                "project_alias": alias,
                "component_id": component_id,
                "name": name,
                "description": description,
                "configuration": effective_config,
                "branch_id": effective_branch_id,
                "validation_status": validation_status,
                "validation_errors": validation_errors,
            }

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Encrypt #-prefixed secrets before they reach Storage (issue #378).
            encrypted_config = self._encrypt_secrets_before_write(
                client,
                project,
                component_id,
                effective_config,
                allow_plaintext_fallback=allow_plaintext_fallback,
            )
            result = client.create_config(
                component_id=component_id,
                name=name,
                configuration=encrypted_config if encrypted_config is not None else {},
                description=description,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        result["validation_status"] = validation_status
        # Symmetric with the dry-run envelope. On a successful real create
        # ``validation_status`` is always "ok" or "skipped" so the list is
        # empty -- but we annotate it anyway so JSON consumers can rely on
        # the key being present.
        result["validation_errors"] = validation_errors
        # Structurally surface any plaintext-fallback leak (empty when encrypted).
        result["plaintext_written"] = (
            find_plaintext_secret_keys(encrypted_config) if encrypted_config else []
        )
        return result

    def _validate_config_body(
        self,
        project: ProjectConfig,
        component_id: str,
        body: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """Validate a configuration body against the component's JSON schema.

        The schema describes the contents of the body's ``parameters`` key, so
        that section is what gets validated; sibling keys (``storage``,
        ``runtime``, ``authorization``) are not covered by it and are left
        alone. A body with no ``parameters`` key is validated as a whole
        (keboola.flow-style configurations). Reported error paths are prefixed
        with ``parameters.`` so they point at the section the caller must fix.
        Unwrapping affects validation only -- the POSTed body is never altered.

        Returns:
            ``("ok", [])`` when the body matches the schema.
            ``("failed", [errors])`` when validation reports issues.
            ``("skipped", [])`` when the AI Service has no schema for this
            component or the lookup itself fails (graceful fallback so a
            missing schema does not block a create).
        """
        # Local import to keep ``jsonschema`` out of the cold-start path of
        # commands that never call ``create_config``.
        import jsonschema

        ai_client = self._ai_client_factory(project.stack_url, project.token)
        try:
            try:
                raw = ai_client.get_component_detail(component_id)
            except KeboolaApiError:
                return ("skipped", [])
        finally:
            ai_client.close()

        try:
            detail = ComponentDetail(**raw)
        except (TypeError, ValueError):
            return ("skipped", [])

        schema = detail.configuration_schema
        if not schema:
            return ("skipped", [])

        # A component's ``configurationSchema`` describes the CONTENTS of the
        # ``parameters`` key -- NOT the whole configuration object (issue #587).
        # A writer schema says ``required: ["db"]`` while the configuration it
        # describes is ``{"parameters": {"db": ...}, "runtime": {...}}``, so the
        # body has to be unwrapped before it is validated. Validating the whole
        # object inverted every outcome: a correct configuration was rejected
        # ("<root>: 'db' is a required property") while a body missing the
        # ``parameters`` wrapper validated clean.
        #
        # Configurations that carry no ``parameters`` key at all are validated
        # whole: for keboola.flow, ``phases`` / ``tasks`` ARE the configuration
        # root and the schema describes that root, so there is nothing to
        # unwrap. This also keeps any future parameters-less component working
        # exactly as it does today.
        unwrapped = "parameters" in body
        target = body["parameters"] if unwrapped else body

        try:
            validator = jsonschema.Draft7Validator(schema)
            errors: list[str] = []
            for err in validator.iter_errors(target):
                segments = ["parameters"] if unwrapped else []
                segments.extend(str(p) for p in err.absolute_path)
                path = ".".join(segments) or "<root>"
                errors.append(f"{path}: {err.message}")
        except jsonschema.SchemaError:
            # Component schema itself is malformed -- don't block the create.
            return ("skipped", [])
        except Exception:
            # iter_errors() can also raise late (e.g. ``UnknownType`` for an
            # unknown ``type`` keyword) when the schema is invalid in a way
            # the constructor accepted. Treat that as "skipped" too -- a
            # broken component schema must not block a real create.
            logger.warning(
                "Schema validation for component %s raised during iter_errors; treating as skipped",
                component_id,
                exc_info=True,
            )
            return ("skipped", [])

        if errors:
            return ("failed", errors)
        return ("ok", [])

    # ── config row-update ──────────────────────────────────────────────────────

    def update_config_row(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        row_id: str,
        name: str | None = None,
        description: str | None = None,
        configuration: dict[str, Any] | None = None,
        set_paths: list[tuple[str, Any]] | None = None,
        merge: bool = False,
        dry_run: bool = False,
        change_description: str | None = None,
        is_disabled: bool | None = None,
        branch_id: int | None = None,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Update an existing configuration row.

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID.
            row_id: The row ID to update.
            name: New row name (if None, not changed).
            description: New description (if None, not changed).
            configuration: Full configuration dict to set/merge.
            set_paths: List of (path, value) tuples for targeted updates.
            merge: If True, deep-merge *configuration* into the existing row
                   config instead of replacing.
            change_description: Text stored as the new row version's
                   ``changeDescription``. When ``None`` a default is generated
                   from what changed.
            dry_run: If True, compute and return the diff without applying.
            is_disabled: When True, disable the row; when False, enable it;
                   when None, leave the current state unchanged.
            branch_id: If set, update in a specific dev branch. Falls back to
                the project's active branch when None.
            allow_plaintext_fallback: If True, write ``#``-secrets as plaintext
                when the Encryption API fails instead of raising. DANGEROUS --
                see :meth:`_encrypt_secrets_before_write`.

        Returns:
            Dict with the updated row from the API.
            When *dry_run* is True the dict contains ``"dry_run": True``
            and a ``"changes"`` list instead of the API response.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails or no changes are requested.
        """
        has_content = configuration is not None or bool(set_paths)
        has_metadata = name is not None or description is not None or is_disabled is not None

        if not has_content and not has_metadata:
            raise KeboolaApiError(
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                message=(
                    "At least one of --name, --description, --configuration, --set, "
                    "--is-disabled, or --is-enabled must be provided."
                ),
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id
        client = self._client_factory(project.stack_url, project.token)

        try:
            final_config: dict[str, Any] | None = None

            if has_content:
                final_config = self._resolve_row_configuration(
                    client=client,
                    component_id=component_id,
                    config_id=config_id,
                    row_id=row_id,
                    configuration=configuration,
                    set_paths=set_paths,
                    merge=merge,
                    branch_id=effective_branch_id,
                )

            change_desc = change_description or _default_change_description(
                "config row-update", has_metadata=has_metadata, has_content=has_content
            )

            if dry_run:
                current_row = client.get_config_row(
                    component_id, config_id, row_id, branch_id=effective_branch_id
                )
                old_cfg = current_row.get("configuration", {})
                if isinstance(old_cfg, str):
                    old_cfg = json.loads(old_cfg) if old_cfg else {}
                new_cfg = final_config if final_config is not None else old_cfg
                changes = compute_diff(old_cfg, new_cfg)
                if is_disabled is not None:
                    old_state = bool(current_row.get("isDisabled", False))
                    if old_state != is_disabled:
                        changes.append(f"isDisabled: {old_state} -> {is_disabled}")
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "component_id": component_id,
                    "config_id": config_id,
                    "row_id": row_id,
                    "branch_id": effective_branch_id,
                    "changes": changes,
                    "change_description": change_desc,
                    "old_configuration": old_cfg,
                    "new_configuration": new_cfg,
                }

            # Encrypt #-prefixed secrets before they reach Storage (issue #378).
            # Real write only -- dry-run returned above with plaintext diff.
            if final_config is not None:
                final_config = self._encrypt_secrets_before_write(
                    client,
                    project,
                    component_id,
                    final_config,
                    allow_plaintext_fallback=allow_plaintext_fallback,
                )

            result = client.update_config_row(
                component_id=component_id,
                config_id=config_id,
                row_id=row_id,
                name=name,
                description=description,
                configuration=final_config,
                is_disabled=is_disabled,
                change_description=change_desc,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["branch_id"] = effective_branch_id
        # Structurally surface any plaintext-fallback leak (empty when encrypted).
        result["plaintext_written"] = (
            find_plaintext_secret_keys(final_config) if final_config else []
        )
        return result

    def _resolve_row_configuration(
        self,
        client: Any,
        component_id: str,
        config_id: str,
        row_id: str,
        configuration: dict[str, Any] | None,
        set_paths: list[tuple[str, Any]] | None,
        merge: bool,
        branch_id: int | None,
    ) -> dict[str, Any]:
        """Build the final row configuration dict by merging/setting paths.

        Mirrors ``_resolve_configuration`` but operates on a row's config.
        """
        needs_current = merge or bool(set_paths)

        if needs_current:
            current_row = client.get_config_row(
                component_id, config_id, row_id, branch_id=branch_id
            )
            current_cfg: dict[str, Any] = current_row.get("configuration", {})
            if isinstance(current_cfg, str):
                current_cfg = json.loads(current_cfg) if current_cfg else {}
        else:
            current_cfg = {}

        if set_paths:
            result = current_cfg
            for path, value in set_paths:
                result = set_nested_value(result, path, value)
            if configuration:
                result = deep_merge(result, configuration)
            return result

        if merge and configuration:
            return deep_merge(current_cfg, configuration)

        return configuration if configuration is not None else current_cfg

    # ── config row-delete ──────────────────────────────────────────────────────

    def delete_config_row(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete a configuration row.

        Args:
            alias: Project alias.
            component_id: The component ID.
            config_id: The configuration ID the row belongs to.
            row_id: The row ID to delete.
            branch_id: If set, delete from a specific dev branch. Falls back
                to the project's active branch when None.

        Returns:
            Dict with ``deleted: True`` plus identifiers (``project_alias``,
            ``component_id``, ``config_id``, ``row_id``, ``branch_id``).

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails (e.g. row not found = 404).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch_id = branch_id or project.active_branch_id
        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_config_row(
                component_id=component_id,
                config_id=config_id,
                row_id=row_id,
                branch_id=effective_branch_id,
            )
        finally:
            client.close()

        return {
            "deleted": True,
            "project_alias": alias,
            "component_id": component_id,
            "config_id": config_id,
            "row_id": row_id,
            "branch_id": effective_branch_id,
        }

    # ── config oauth-url ───────────────────────────────────────────────────────

    def get_oauth_url(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        redirect_url: str | None = None,
    ) -> dict[str, Any]:
        """Generate an OAuth authorization URL for a component configuration.

        Creates a short-lived Storage API token scoped to the component and
        builds the URL the user must open in a browser to grant OAuth access.

        Args:
            alias: Project alias.
            component_id: The component ID (e.g. 'keboola.ex-google-drive').
            config_id: The configuration ID to authorize.
            redirect_url: Optional URL the OAuth wizard returns to after the
                flow completes (passed as the ``returnUrl`` query param).

        Returns:
            Dict with 'url', 'component_id', 'config_id', 'project_alias',
            and ``redirect_url`` when provided.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            # Pre-flight: minting a short-lived component-scoped child token
            # via POST /v2/storage/tokens requires `canManageTokens`, which only
            # master tokens carry by default. Without this guard the Storage
            # API returns a vague 500 "Application error" that misleads
            # operators into thinking the OAuth wizard is broken.
            info = client.get_project_info()
            if not info.get("isMasterToken", False):
                raise KeboolaApiError(
                    status_code=403,
                    error_code=ErrorCode.MISSING_MASTER_TOKEN,
                    message=(
                        f"`config oauth-url` requires a master Storage API token "
                        f"on project '{alias}'. The current token "
                        f"(id={info.get('id', '?')}, "
                        f"description='{info.get('description', '?')}') is not a "
                        f"master token, so it cannot mint the short-lived "
                        f"component-scoped child token the OAuth wizard expects. "
                        f"Either re-add the project with a master token "
                        f"(`kbagent project edit --project {alias} --token <MASTER>`) "
                        f"or open the OAuth flow via the Keboola UI."
                    ),
                )

            url = client.get_oauth_url(
                component_id=component_id,
                config_id=config_id,
                redirect_url=redirect_url,
            )
        finally:
            client.close()

        result: dict[str, Any] = {
            "url": url,
            "component_id": component_id,
            "config_id": config_id,
            "project_alias": alias,
        }
        if redirect_url:
            result["redirect_url"] = redirect_url
        return result
