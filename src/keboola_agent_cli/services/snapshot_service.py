"""Table snapshot service -- create, list, inspect, delete, restore-as-new-table.

Business logic for the ``kbagent storage snapshot-*`` / ``table-from-snapshot``
commands (issue #512). Wraps the Storage API table-snapshot endpoints behind
kbagent-alias resolution and an injectable :class:`KeboolaClient` factory:

- ``POST /v2/storage/tables/{id}/snapshots``  -- create (async storage job)
- ``GET  /v2/storage/tables/{id}/snapshots``  -- list (sync)
- ``GET  /v2/storage/snapshots/{id}``         -- detail (sync)
- ``DELETE /v2/storage/snapshots/{id}``       -- delete (sync)
- ``POST /v2/storage/buckets/{id}/tables-async`` with ``snapshotId`` --
  create a NEW table from a snapshot (async storage job)

Endpoint contract verified against the reference PHP client
(``keboola/storage-api-php-client``: ``createTableSnapshot``,
``listTableSnapshots``, ``getSnapshot``, ``deleteSnapshot``,
``createTableFromSnapshot``). Restore goes through the classic
``tables-async`` import endpoint -- NOT ``tables-definition`` -- which is why
this is a dedicated command instead of a flag on ``storage create-table``.

Lives in its own module because ``storage_service.py`` is already past its
file-size hard ceiling (CONTRIBUTING.md); mirrors the ``TokenService`` layout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..errors import ConfigError, KeboolaApiError
from .base import ResolvedProjectCredentials, resolve_project_credentials

logger = logging.getLogger(__name__)

KeboolaClientFactory = Callable[[str, str], KeboolaClient]


def default_snapshot_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Construct a :class:`KeboolaClient` bound to ``stack_url`` + ``token``."""
    return KeboolaClient(stack_url=stack_url, token=token)


class SnapshotService:
    """Business logic for table snapshot create / list / detail / delete / restore."""

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: KeboolaClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or default_snapshot_client_factory

    def create_snapshot(
        self,
        *,
        alias: str,
        table_id: str,
        description: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Snapshot ``table_id`` and return the new snapshot's ID.

        Returns:
            Dict with 'project_alias', 'table_id', 'branch_id', 'snapshot_id'.
        """
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            results = client.create_table_snapshot(
                table_id=table_id,
                description=description,
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "table_id": table_id,
            "branch_id": branch_id,
            "snapshot_id": results.get("id"),
            "description": description,
        }

    def list_snapshots(
        self,
        *,
        alias: str,
        table_id: str,
        limit: int | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List snapshots of ``table_id`` (newest first, as returned by the API).

        Returns:
            Dict with 'project_alias', 'table_id', 'branch_id', 'count',
            'snapshots' (raw API snapshot dicts).
        """
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            snapshots = client.list_table_snapshots(
                table_id=table_id,
                limit=limit,
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "table_id": table_id,
            "branch_id": branch_id,
            "count": len(snapshots),
            "snapshots": snapshots,
        }

    def get_snapshot(self, *, alias: str, snapshot_id: str) -> dict[str, Any]:
        """Fetch one snapshot's detail (includes the source table object).

        Returns:
            Dict with 'project_alias', 'snapshot' (raw API snapshot dict).
        """
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            snapshot = client.get_snapshot(snapshot_id)
        finally:
            client.close()
        return {"project_alias": alias, "snapshot": snapshot}

    def delete_snapshots(
        self,
        *,
        alias: str,
        snapshot_ids: list[str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one or more snapshots. Batch-tolerant: accumulates errors per ID.

        Returns:
            Dict with 'project_alias', 'deleted', 'failed', 'dry_run' (and
            'would_delete' when dry_run).
        """
        creds = self._resolve_project(alias)

        deleted: list[str] = []
        failed: list[dict[str, Any]] = []
        would_delete: list[str] = []

        client = self._client_factory(creds.stack_url, creds.token)
        try:
            for sid in snapshot_ids:
                if dry_run:
                    would_delete.append(sid)
                    continue
                try:
                    client.delete_snapshot(sid)
                    deleted.append(sid)
                except KeboolaApiError as exc:
                    failed.append({"id": sid, "error": exc.message})
        finally:
            client.close()

        result: dict[str, Any] = {
            "project_alias": alias,
            "deleted": deleted,
            "failed": failed,
            "dry_run": dry_run,
        }
        if dry_run:
            result["would_delete"] = would_delete
        return result

    def create_table_from_snapshot(
        self,
        *,
        alias: str,
        bucket_id: str,
        snapshot_id: str,
        name: str,
        branch_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a NEW table in ``bucket_id`` from snapshot ``snapshot_id``.

        The snapshot's data, columns, and primary key are restored into a
        fresh table named ``name`` (required -- the API rejects an empty
        name). The destination bucket must already exist; restoring over an
        existing table name fails with the API's duplicate-table error --
        this service adds no overwrite semantics on top.

        Returns:
            Dict with 'project_alias', 'bucket_id', 'snapshot_id', 'name',
            'branch_id', 'dry_run', and (when applied) 'table' (raw job
            results incl. the created table 'id') + 'table_id'.
        """
        if not bucket_id.strip():
            raise ConfigError("table-from-snapshot requires a non-empty --bucket-id.")
        if not str(snapshot_id).strip():
            raise ConfigError("table-from-snapshot requires a non-empty --snapshot-id.")
        if not name.strip():
            raise ConfigError("table-from-snapshot requires a non-empty --name.")

        creds = self._resolve_project(alias)

        if dry_run:
            return {
                "project_alias": alias,
                "bucket_id": bucket_id,
                "snapshot_id": snapshot_id,
                "name": name,
                "branch_id": branch_id,
                "dry_run": True,
            }

        client = self._client_factory(creds.stack_url, creds.token)
        try:
            table = client.create_table_from_snapshot(
                bucket_id=bucket_id,
                snapshot_id=snapshot_id,
                name=name,
                branch_id=branch_id,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "bucket_id": bucket_id,
            "snapshot_id": snapshot_id,
            "name": name,
            "branch_id": branch_id,
            "dry_run": False,
            "table": table,
            "table_id": table.get("id"),
        }

    def _resolve_project(self, alias: str) -> ResolvedProjectCredentials:
        """Resolve ``alias`` to its stack URL + token (or raise ConfigError)."""
        return resolve_project_credentials(self._config_store, alias)
