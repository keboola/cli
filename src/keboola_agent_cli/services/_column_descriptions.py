"""Column-description helpers shared by the storage service (issue #624).

Everything here is about ONE question: where does a column description live?
Three conventions coexist, and this module is the single place that knows their
precedence -- native ``definition`` field (what the Keboola UI writes and shows)
first, then the ``KBC.description`` entry the backend mirrors it into (the only
one the MCP server reads), then the flat ``KBC.column.{name}.description`` table
metadata key kbagent wrote before 0.88.0 and nothing else ever read.

Lives in a private module because ``services/storage_service.py`` is already
past its CONTRIBUTING.md size ceiling; the functions take an explicit client so
they stay free of service state.
"""

import logging
from collections.abc import Callable
from typing import Any

from ..errors import KeboolaApiError
from .base import BaseService

logger = logging.getLogger(__name__)

# Legacy pre-0.88.0 convention: flat table-metadata keys carrying per-column
# descriptions. Read nowhere except this CLI -- neither the Keboola UI nor the
# MCP server ever looked at them. Superseded by the native table-definition
# endpoint (#624); kept only so the read fallback and `describe-migrate` can
# still find and convert old entries.
LEGACY_COLUMN_KEY_PREFIX = "KBC.column."
LEGACY_COLUMN_KEY_SUFFIX = ".description"

# Key the backend mirrors a native column description into. Written by the
# platform, not by us; the single place UI/MCP/kbagent all agree on for reads.
COLUMN_DESCRIPTION_METADATA_KEY = "KBC.description"


def collect_legacy_column_entries(
    raw_metadata: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map column name -> full metadata entry for flat KBC.column.*.description keys."""
    out: dict[str, dict[str, Any]] = {}
    for m in raw_metadata:
        key = m.get("key", "")
        if key.startswith(LEGACY_COLUMN_KEY_PREFIX) and key.endswith(LEGACY_COLUMN_KEY_SUFFIX):
            col = key[len(LEGACY_COLUMN_KEY_PREFIX) : -len(LEGACY_COLUMN_KEY_SUFFIX)]
            out[col] = m
    return out


def description_from_column_meta(meta: list[dict[str, Any]]) -> str | None:
    """Return the ``KBC.description`` value from one column's metadata list."""
    for m in meta:
        if m.get("key") == COLUMN_DESCRIPTION_METADATA_KEY:
            return m.get("value") or None
    return None


def native_column_descriptions(table: dict[str, Any]) -> dict[str, str]:
    """Map column name -> description from the native ``definition`` block.

    Alias tables carry no definition of their own; they inherit the source
    table's, which is where the UI reads them from too.
    """
    definition = table.get("definition") or {}
    if not definition and table.get("isAlias"):
        definition = (table.get("sourceTable") or {}).get("definition") or {}
    out: dict[str, str] = {}
    for col in definition.get("columns") or []:
        name = col.get("name")
        desc = (col.get("definition") or {}).get("description")
        if name and desc:
            out[name] = desc
    return out


def visible_column_descriptions(table: dict[str, Any]) -> dict[str, str]:
    """Resolve the description each column shows today (native, then metadata).

    Precedence mirrors the read path in ``get_table_detail`` minus the legacy
    tier -- it answers "would migrating this legacy value overwrite something
    a user can already see?".
    """
    resolved = dict(native_column_descriptions(table))
    column_metadata: dict[str, list[dict[str, Any]]] = table.get("columnMetadata") or {}
    source_column_metadata: dict[str, list[dict[str, Any]]] = (
        (table.get("sourceTable") or {}).get("columnMetadata") or {} if table.get("isAlias") else {}
    )
    for col in table.get("columns") or []:
        if col in resolved:
            continue
        meta_desc = description_from_column_meta(column_metadata.get(col, []))
        if meta_desc is None:
            meta_desc = description_from_column_meta(source_column_metadata.get(col, []))
        if meta_desc:
            resolved[col] = meta_desc
    return resolved


def plan_column_migration(
    table: dict[str, Any],
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute (migratable {col: desc}, skipped entries, deletable metadata entries).

    Rules (#624):
    - orphan (column no longer on the table)  -> skip, reason "orphan", entry NOT deleted
    - target already has a description (native definition or columnMetadata
      KBC.description) that differs                 -> skip, reason "conflict", NOT deleted
    - target has the IDENTICAL description          -> nothing to write, entry IS deleted
    - otherwise                                     -> migrate, entry deleted after the write
    """
    legacy_entries = collect_legacy_column_entries(table.get("metadata") or [])
    table_columns = set(table.get("columns") or [])
    visible = visible_column_descriptions(table)

    migratable: dict[str, str] = {}
    skipped: list[dict[str, Any]] = []
    deletable: list[dict[str, Any]] = []
    for col, entry in legacy_entries.items():
        legacy_value = entry.get("value") or ""
        if col not in table_columns:
            skipped.append({"column": col, "reason": "orphan", "legacy": legacy_value})
            continue
        current = visible.get(col)
        if current == legacy_value:
            # Already mirrored where everyone reads it -- drop the stale copy so
            # a later "clear the description" does not get resurrected from it.
            deletable.append(entry)
            continue
        if current:
            skipped.append(
                {
                    "column": col,
                    "reason": "conflict",
                    "legacy": legacy_value,
                    "current": current,
                }
            )
            continue
        migratable[col] = legacy_value
        deletable.append(entry)
    return migratable, skipped, deletable


def delete_legacy_entries(
    client: Any,
    table_id: str,
    entries: list[dict[str, Any]],
    branch_id: int | None = None,
) -> list[dict[str, Any]]:
    """Delete migrated legacy metadata entries; report failures, never raise.

    The native write is already durable by the time this runs -- a failed
    cleanup leaves a stale flat key behind (harmless, re-migratable) and must
    not turn a successful describe into a failed command.
    """
    failures: list[dict[str, Any]] = []
    for entry in entries:
        key = entry.get("key", "")
        column = key[len(LEGACY_COLUMN_KEY_PREFIX) : -len(LEGACY_COLUMN_KEY_SUFFIX)]
        try:
            client.delete_table_metadata(table_id, entry.get("id"), branch_id=branch_id)
        except Exception as exc:
            msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
            logger.warning("Could not delete legacy metadata %s on %s: %s", key, table_id, msg)
            failures.append({"column": column, "reason": "delete_failed", "error": msg})
    return failures


def write_column_descriptions(
    client: Any,
    table_id: str,
    columns: dict[str, str],
    branch_id: int | None = None,
) -> dict[str, Any]:
    """Write ``columns`` through the native endpoint, migrating legacy siblings.

    Returns ``{"migrated": {col: desc}, "skipped": [...], "result": job}``.
    Raises ``ValueError`` for column names the table does not have -- before
    any write happens.
    """
    table = client.get_table_detail(table_id, branch_id=branch_id)

    # Fail fast: the native endpoint rejects unknown columns anyway, and the old
    # flat write silently accepted typos that nothing ever read.
    table_columns = set(table.get("columns") or [])
    unknown = [c for c in columns if c not in table_columns]
    if unknown:
        raise ValueError(
            f"Unknown column(s) on table '{table_id}': {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(table_columns))}"
        )

    migratable, skipped, deletable = plan_column_migration(table)
    legacy_entries = collect_legacy_column_entries(table.get("metadata") or [])
    # The user's value always wins over a legacy one for the same column; the
    # legacy entry is still cleaned up because the new write supersedes it.
    deletable_ids = {str(entry.get("id")) for entry in deletable}
    for col in columns:
        migratable.pop(col, None)
        skipped = [s for s in skipped if s["column"] != col]
        entry = legacy_entries.get(col)
        if entry is not None and str(entry.get("id")) not in deletable_ids:
            deletable.append(entry)
            deletable_ids.add(str(entry.get("id")))

    payload = {**migratable, **columns}
    result = client.update_table_definition(
        table_id=table_id,
        columns=[{"name": name, "description": desc} for name, desc in payload.items()],
        is_description_system_managed=False,
        branch_id=branch_id,
    )
    skipped.extend(delete_legacy_entries(client, table_id, deletable, branch_id=branch_id))
    return {"migrated": migratable, "skipped": skipped, "result": result}


def migrate_candidates(
    client: Any,
    table_ids: list[str] | None,
    bucket_id: str | None,
    branch_id: int | None,
) -> tuple[list[str], set[str] | None]:
    """Resolve the tables in scope plus (when known) those carrying legacy keys.

    The listing is fetched with ``include="metadata"`` so whole-project runs
    only fetch table details for the few tables that actually have something to
    migrate. The second element is ``None`` when that pre-filter is unavailable
    (explicit ids, or a listing that carried no metadata).
    """
    if table_ids:
        return list(table_ids), None

    listing = client.list_tables(bucket_id=bucket_id, branch_id=branch_id, include="metadata")
    rows = [
        row
        for row in listing
        if not bucket_id or str(row.get("id", "")).startswith(f"{bucket_id}.")
    ]
    candidates = [str(row.get("id", "")) for row in rows]
    if any("metadata" not in row for row in rows):
        return candidates, None
    with_legacy = {
        str(row.get("id", ""))
        for row in rows
        if collect_legacy_column_entries(row.get("metadata") or [])
    }
    return candidates, with_legacy


def migrate_one_table(
    client: Any,
    table_id: str,
    prune_orphans: bool,
    dry_run: bool,
    branch_id: int | None,
    migrated: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    pruned: list[dict[str, Any]],
) -> bool:
    """Migrate one table's legacy keys; append findings to the shared lists.

    Findings are committed only once the table is through -- a table whose
    write raises is reported purely as an error by the caller, never as
    half-migrated.

    Returns True when a write actually happened (always False in dry-run).
    """
    table = client.get_table_detail(table_id, branch_id=branch_id)
    migratable, table_skipped, deletable = plan_column_migration(table)
    legacy_entries = collect_legacy_column_entries(table.get("metadata") or [])

    if dry_run:
        for item in table_skipped:
            skipped.append({"table_id": table_id, **item})
        if migratable:
            migrated.append({"table_id": table_id, "columns": migratable})
        return False

    wrote = False
    if migratable:
        client.update_table_definition(
            table_id=table_id,
            columns=[{"name": name, "description": desc} for name, desc in migratable.items()],
            is_description_system_managed=False,
            branch_id=branch_id,
        )
        wrote = True
        migrated.append({"table_id": table_id, "columns": migratable})
    for item in table_skipped:
        skipped.append({"table_id": table_id, **item})
    for failure in delete_legacy_entries(client, table_id, deletable, branch_id=branch_id):
        skipped.append({"table_id": table_id, **failure})
    if deletable:
        wrote = True

    if prune_orphans:
        orphans = [item["column"] for item in table_skipped if item["reason"] == "orphan"]
        entries = [legacy_entries[col] for col in orphans if col in legacy_entries]
        prune_failures = delete_legacy_entries(client, table_id, entries, branch_id=branch_id)
        failed = {failure["column"] for failure in prune_failures}
        for failure in prune_failures:
            skipped.append({"table_id": table_id, **failure})
        for col in orphans:
            if col not in failed:
                pruned.append({"table_id": table_id, "column": col})
        if entries:
            wrote = True
    return wrote


def migrate_tables(
    client: Any,
    table_ids: list[str] | None,
    bucket_id: str | None,
    prune_orphans: bool,
    dry_run: bool,
    branch_id: int | None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Run the migration over every table in scope, accumulating per-table errors.

    Sequential on purpose -- each write is one storage job and a migration is a
    one-off maintenance task. Returns the raw counters/lists; the service wraps
    them in the response envelope.
    """
    migrated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    tables_migrated = 0

    candidates, with_legacy = migrate_candidates(client, table_ids, bucket_id, branch_id)
    total = len(candidates)
    for index, table_id in enumerate(candidates, start=1):
        if progress_callback is not None:
            progress_callback(table_id, index, total)
        # Tables the listing already proved carry no legacy key need no detail fetch.
        if with_legacy is not None and table_id not in with_legacy:
            continue
        try:
            changed = migrate_one_table(
                client,
                table_id,
                prune_orphans=prune_orphans,
                dry_run=dry_run,
                branch_id=branch_id,
                migrated=migrated,
                skipped=skipped,
                pruned=pruned,
            )
        except Exception as exc:
            msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
            errors.append({"table_id": table_id, "error": msg})
            continue
        if changed:
            tables_migrated += 1

    return {
        "tables_scanned": total,
        "tables_migrated": tables_migrated,
        "migrated": migrated,
        "skipped": skipped,
        "pruned_orphans": pruned,
        "errors": errors,
    }


class ColumnDescriptionsMixin(BaseService):
    """The ``describe-column`` / ``describe-migrate`` half of ``StorageService``.

    Split off so ``storage_service.py`` stays within its file-size budget; the
    methods are plain service methods (project resolution + client lifecycle +
    response envelope) delegating the actual rules to this module's functions.
    """

    def describe_columns(
        self,
        alias: str,
        table_id: str,
        columns: dict[str, str],
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set per-column descriptions on a storage table (native endpoint).

        Writes through ``PUT /v2/storage/branch/{branch}/tables/{id}/definition``
        -- the same call the Keboola UI makes. The backend mirrors each value
        into ``columnMetadata[{col}]`` ``KBC.description`` (typed and untyped
        tables alike) and down to the backend column comment, so one write is
        visible to the UI, the MCP server, and Snowflake/BigQuery. The write
        sets ``isDescriptionSystemManaged=false``, which is what stops the next
        component run's Output Mapping from overwriting the text.

        Any sibling legacy ``KBC.column.{name}.description`` metadata key found
        on the table (the pre-0.88.0 convention, invisible to everything but
        this CLI) is migrated in the SAME write and then deleted -- unless the
        column is gone (orphan) or already shows a different description
        (conflict); those are reported in ``skipped`` and left untouched.

        Args:
            alias: Project alias.
            table_id: Full table ID.
            columns: Mapping of column name -> description text.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, table_id, columns, migrated, skipped,
            result (the completed storage job), message.
        """
        if not columns:
            raise ValueError("At least one column description must be provided.")
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            outcome = write_column_descriptions(client, table_id, columns, branch_id=branch_id)
        finally:
            client.close()

        migrated = outcome["migrated"]
        message = f"Described {len(columns)} column(s) on '{table_id}' in project '{alias}'."
        if migrated:
            message += f" Migrated {len(migrated)} legacy entry(ies)."
        return {
            "project_alias": alias,
            "table_id": table_id,
            "columns": columns,
            "migrated": migrated,
            "skipped": outcome["skipped"],
            "result": outcome["result"],
            "message": message,
        }

    def describe_migrate(
        self,
        alias: str,
        table_ids: list[str] | None = None,
        bucket_id: str | None = None,
        prune_orphans: bool = False,
        dry_run: bool = False,
        branch_id: int | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Migrate legacy flat KBC.column.* descriptions to the native endpoint.

        Scope: explicit ``table_ids``, else all tables of ``bucket_id``, else
        every table in the project. Tables without legacy keys are skipped
        silently (still counted in ``tables_scanned``). Per-table failures are
        collected and never abort the run (error-accumulation convention #11).

        Migration rules are the ones ``describe_columns`` applies opportunistically
        (see ``_plan_column_migration``): conflicts and orphans are reported, not
        overwritten. ``prune_orphans`` additionally deletes the dangling legacy
        entries of columns that no longer exist.

        Tables are processed sequentially -- each write is one storage job, and a
        migration is a one-off maintenance task. Parallelizing per table is
        possible future work if this ever runs on very large projects.

        Args:
            alias: Project alias.
            table_ids: Explicit tables to migrate (mutually exclusive with bucket_id).
            bucket_id: Migrate every table of this bucket.
            prune_orphans: Also delete legacy entries for dropped columns.
            dry_run: Report what would happen; performs no writes at all.
            branch_id: If set, target a specific dev branch.
            progress_callback: Optional ``(table_id, current, total)`` callable
                invoked **before** each table is processed (1-based ``current``).

        Returns:
            Dict with project_alias, dry_run, tables_scanned, tables_migrated,
            migrated, skipped, pruned_orphans, errors, message.
        """
        if table_ids and bucket_id:
            raise ValueError("--table-id and --bucket-id are mutually exclusive.")

        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            outcome = migrate_tables(
                client,
                table_ids,
                bucket_id,
                prune_orphans=prune_orphans,
                dry_run=dry_run,
                branch_id=branch_id,
                progress_callback=progress_callback,
            )
        finally:
            client.close()

        verb = "Would migrate" if dry_run else "Migrated"
        return {
            "project_alias": alias,
            "dry_run": dry_run,
            **outcome,
            "message": (
                f"{verb} {len(outcome['migrated'])} table(s) of "
                f"{outcome['tables_scanned']} scanned in project '{alias}'; "
                f"{len(outcome['skipped'])} column(s) skipped, "
                f"{len(outcome['errors'])} error(s)."
            ),
        }
