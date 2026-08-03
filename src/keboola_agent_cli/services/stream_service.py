"""Data Streams (Stream API) service -- OTLP/HTTP source management.

Business logic for the ``kbagent stream`` command group. Wraps the Stream
control-plane API behind a layer that:

- resolves a kbagent project alias to a
  :class:`~keboola_agent_cli.services.base.ResolvedProjectCredentials` via
  :class:`ConfigStore` (the alias is the only handle a caller needs);
- builds a :class:`StreamClient` through an injectable factory (testability);
- assembles a source's full picture for ``stream detail`` -- base + per-signal
  OTLP endpoints, wire protocol, and the destination bucket/tables read from the
  source's sinks;
- **masks the secret embedded in the OTLP endpoint URL by default**, revealing
  it only when the caller explicitly opts in (``reveal=True``). The raw source
  object echoed in ``--json`` output is sanitised the same way so the secret
  never leaks unless revealed.

The Stream API authenticates with the per-project Storage API token, so -- unlike
the ``feature`` group -- no manage token is involved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..config_store import ConfigStore
from ..constants import (
    OTLP_PROTOCOL,
    OTLP_SIGNAL_PATHS,
    STREAM_DEFAULT_BRANCH,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..stream_client import StreamClient, provision_otlp_sinks, stream_task_source_id
from .base import ResolvedProjectCredentials, resolve_project_credentials

logger = logging.getLogger(__name__)

StreamClientFactory = Callable[[str, str], StreamClient]

# Map an OTLP signal sub-path (v1/logs) to the short signal name (logs) used as
# the key in the assembled per-signal endpoint map.
_SIGNAL_NAMES: dict[str, str] = {path: path.split("/")[-1] for path in OTLP_SIGNAL_PATHS}

_SECRET_MASK = "***"


def default_stream_client_factory(stack_url: str, token: str) -> StreamClient:
    """Construct a :class:`StreamClient` bound to ``stack_url`` + ``token``.

    Static-token-only (v1 scope is Storage + Manage); the client's
    ``SESSION_AUTH_FEATURE`` makes a session sentinel fail fast on construction.
    """
    return StreamClient(stack_url=stack_url, token=token)


class StreamService:
    """Business logic for Data Streams sources (list / create / detail / delete)."""

    def __init__(
        self,
        config_store: ConfigStore,
        stream_client_factory: StreamClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._stream_client_factory = stream_client_factory or default_stream_client_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_sources(self, *, alias: str, branch_id: str | None = None) -> dict[str, Any]:
        """List sources in the alias's project (default branch unless overridden)."""
        creds = self._resolve_project(alias)
        branch = branch_id or STREAM_DEFAULT_BRANCH
        client = self._stream_client_factory(creds.stack_url, creds.token)
        try:
            raw = client.list_sources(branch)
            return {
                "alias": alias,
                "branch_id": branch,
                "sources": self._summarise_sources(raw),
            }
        finally:
            client.close()

    def create_source(
        self,
        *,
        alias: str,
        name: str,
        source_type: str = "otlp",
        branch_id: str | None = None,
        if_not_exists: bool = False,
        reveal: bool = False,
        provision_sinks: bool = True,
    ) -> dict[str, Any]:
        """Create a source, poll the async task, and return its assembled detail.

        For an ``otlp`` source, the three standard sinks (logs/metrics/traces)
        are auto-provisioned so data actually lands in Storage -- the raw Stream
        API ``POST /sources`` creates only the bare source, unlike the Keboola
        UI. Pass ``provision_sinks=False`` to skip that and create a bare source.

        With ``if_not_exists`` an existing source matching ``name`` (by name or
        sourceId) is returned untouched with ``status="skipped"`` (its sinks are
        also reconciled when ``provision_sinks`` so a half-set-up source heals).
        """
        creds = self._resolve_project(alias)
        branch = branch_id or STREAM_DEFAULT_BRANCH
        client = self._stream_client_factory(creds.stack_url, creds.token)
        try:
            if if_not_exists:
                existing = self._find_source(client, branch, name)
                if existing is not None:
                    if provision_sinks and source_type == "otlp":
                        self._provision_otlp_sinks(client, branch, existing.get("sourceId", name))
                    detail = self._assemble_detail(client, branch, existing, reveal=reveal)
                    detail.update({"alias": alias, "status": "skipped"})
                    return detail

            task = client.create_source(branch, name=name, source_type=source_type)
            finished = client.wait_for_task(task)
            source_id = self._task_source_id(finished) or name
            if provision_sinks and source_type == "otlp":
                self._provision_otlp_sinks(client, branch, source_id)
            source = client.get_source(branch, source_id)
            detail = self._assemble_detail(client, branch, source, reveal=reveal)
            detail.update({"alias": alias, "status": "created"})
            return detail
        finally:
            client.close()

    def _provision_otlp_sinks(self, client: StreamClient, branch: str, source_id: str) -> None:
        """Create the standard logs/metrics/traces sinks for an OTLP source.

        Delegates to the shared :func:`provision_otlp_sinks` helper so the CLI
        path and the importable :meth:`KeboolaClient.create_stream_source` stay
        in lock-step (idempotent: only missing signals are created).
        """
        provision_otlp_sinks(client, branch, source_id)

    def get_source_detail(
        self,
        *,
        alias: str,
        source_id: str | None = None,
        name: str | None = None,
        branch_id: str | None = None,
        reveal: bool = False,
    ) -> dict[str, Any]:
        """Assemble the full picture for one source (endpoints + destination)."""
        if not source_id and not name:
            raise ConfigError("Provide a source id (positional) or --name.")
        creds = self._resolve_project(alias)
        branch = branch_id or STREAM_DEFAULT_BRANCH
        client = self._stream_client_factory(creds.stack_url, creds.token)
        try:
            if source_id:
                source = client.get_source(branch, source_id)
            else:
                source = self._find_source(client, branch, name or "")
                if source is None:
                    raise KeboolaApiError(
                        message=f"No source named '{name}' in branch '{branch}'.",
                        error_code=ErrorCode.NOT_FOUND,
                        status_code=404,
                    )
            detail = self._assemble_detail(client, branch, source, reveal=reveal)
            detail["alias"] = alias
            return detail
        finally:
            client.close()

    def delete_source(
        self,
        *,
        alias: str,
        source_id: str,
        branch_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a source (async task polled to completion)."""
        creds = self._resolve_project(alias)
        branch = branch_id or STREAM_DEFAULT_BRANCH
        if dry_run:
            return {
                "status": "dry_run",
                "alias": alias,
                "branch_id": branch,
                "source_id": source_id,
            }
        client = self._stream_client_factory(creds.stack_url, creds.token)
        try:
            task = client.delete_source(branch, source_id)
            client.wait_for_task(task)
            return {
                "status": "deleted",
                "alias": alias,
                "branch_id": branch,
                "source_id": source_id,
            }
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_project(self, alias: str) -> ResolvedProjectCredentials:
        """Resolve ``alias`` to its stack URL + token (or raise ConfigError)."""
        return resolve_project_credentials(self._config_store, alias)

    @staticmethod
    def _find_source(client: StreamClient, branch: str, needle: str) -> dict[str, Any] | None:
        """Return the first source matching ``needle`` by sourceId or name."""
        raw = client.list_sources(branch)
        for source in raw.get("sources", []):
            if source.get("sourceId") == needle or source.get("name") == needle:
                return source
        return None

    @staticmethod
    def _task_source_id(task: dict[str, Any]) -> str | None:
        """Extract the created sourceId from a finished task's outputs."""
        return stream_task_source_id(task)

    @staticmethod
    def _summarise_sources(raw: dict[str, Any]) -> list[dict[str, Any]]:
        """Reduce a sources-list payload to the columns `stream list` shows.

        The secret is never part of the list view -- only the base (secret-free)
        endpoint is surfaced.
        """
        out: list[dict[str, Any]] = []
        for source in raw.get("sources", []):
            otlp = source.get("otlp") or {}
            http = source.get("http") or {}
            out.append(
                {
                    "source_id": source.get("sourceId", ""),
                    "name": source.get("name", ""),
                    "type": source.get("type", ""),
                    "description": source.get("description", ""),
                    # baseUrl is documented as the endpoint *without* the secret.
                    "base_endpoint": otlp.get("baseUrl", "") or http.get("url", ""),
                }
            )
        return out

    def _assemble_detail(
        self,
        client: StreamClient,
        branch: str,
        source: dict[str, Any],
        *,
        reveal: bool,
    ) -> dict[str, Any]:
        """Build the rich `stream detail` view for one source.

        Fetches the source's sinks to surface the destination bucket/tables and
        import conditions, computes per-signal OTLP endpoints, and masks the
        secret (in every endpoint and in the echoed raw source) unless
        ``reveal`` is set.
        """
        source_id = source.get("sourceId", "")
        source_type = source.get("type", "")
        otlp = source.get("otlp") or {}
        http = source.get("http") or {}
        secret = otlp.get("secret") or ""
        base_endpoint = otlp.get("baseUrl", "")
        full_endpoint = otlp.get("url", "") or http.get("url", "")

        signal_endpoints: dict[str, str] = {}
        if source_type == "otlp" and full_endpoint:
            root = full_endpoint.rstrip("/")
            for path, signal in _SIGNAL_NAMES.items():
                signal_endpoints[signal] = f"{root}/{path}"

        sinks_raw = client.list_sinks(branch, source_id)
        destination = self._destination_from_sinks(sinks_raw)
        import_conditions = self._import_conditions(source, sinks_raw)

        endpoint_display = self._mask(full_endpoint, secret, reveal)
        signal_display = {
            signal: self._mask(url, secret, reveal) for signal, url in signal_endpoints.items()
        }

        return {
            "branch_id": branch,
            "source_id": source_id,
            "name": source.get("name", ""),
            "type": source_type,
            "description": source.get("description", ""),
            "endpoint": endpoint_display,
            "base_endpoint": base_endpoint,
            "signal_endpoints": signal_display,
            "protocol": OTLP_PROTOCOL if source_type == "otlp" else "",
            "secret_revealed": reveal,
            "destination": destination,
            "import_conditions": import_conditions,
            "sinks": sinks_raw.get("sinks", []),
            # Echo the raw source for `--json` completeness, sanitised so the
            # secret never leaks unless explicitly revealed.
            "source": self._sanitise_source(source, secret, reveal),
        }

    @staticmethod
    def _destination_from_sinks(sinks_raw: dict[str, Any]) -> dict[str, Any]:
        """Extract destination bucket + per-signal table ids from sinks."""
        tables: dict[str, str] = {}
        buckets: list[str] = []
        for sink in sinks_raw.get("sinks", []):
            table = sink.get("table") or {}
            table_id = table.get("tableId", "")
            if not table_id:
                continue
            signals = sink.get("allowedSignals") or []
            key = signals[0] if signals else (sink.get("sinkId") or table_id)
            tables[key] = table_id
            bucket = table_id.rsplit(".", 1)[0] if "." in table_id else ""
            if bucket and bucket not in buckets:
                buckets.append(bucket)
        return {
            "bucket": buckets[0] if len(buckets) == 1 else "",
            "buckets": buckets,
            "tables": tables,
        }

    @staticmethod
    def _import_conditions(
        source: dict[str, Any], sinks_raw: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Surface import/upload conditions if the API exposes them.

        The Stream API manages import triggers (count / size / time) server-side
        and does not always echo them on the source/sink objects. We return
        whatever is present rather than inventing defaults (no silent defaults).
        """
        for candidate in (source.get("import"), source.get("conditions")):
            if isinstance(candidate, dict) and candidate:
                return candidate
        for sink in sinks_raw.get("sinks", []):
            conditions = sink.get("conditions") or (sink.get("table") or {}).get("import")
            if isinstance(conditions, dict) and conditions:
                return conditions
        return None

    @staticmethod
    def _mask(endpoint: str, secret: str, reveal: bool) -> str:
        """Mask the secret substring inside ``endpoint`` unless ``reveal``."""
        if reveal or not endpoint or not secret:
            return endpoint
        return endpoint.replace(secret, _SECRET_MASK)

    @classmethod
    def _sanitise_source(cls, source: dict[str, Any], secret: str, reveal: bool) -> dict[str, Any]:
        """Return a copy of ``source`` with the secret masked unless revealed."""
        if reveal:
            return source
        sanitised = dict(source)
        for key in ("otlp", "http"):
            block = sanitised.get(key)
            if isinstance(block, dict):
                masked = dict(block)
                if masked.get("url"):
                    masked["url"] = cls._mask(masked["url"], secret, reveal)
                if "secret" in masked:
                    masked["secret"] = _SECRET_MASK
                sanitised[key] = masked
        return sanitised
