"""In-place BigQuery clustering change (``storage clustering-set``).

Lives in a private module because ``services/storage_service.py`` is past its
file-size budget (CONTRIBUTING.md > "File-size budgets").
"""

from typing import Any

from ..constants import (
    BIGQUERY_CLUSTERING_MAX_FIELDS,
    CLUSTERING_UPDATE_NOT_ALLOWED_CODE,
    STORAGE_JOB_MAX_WAIT,
)
from ..effective_branch import resolve_branch
from ..errors import ErrorCode, KeboolaApiError
from ._storage_jobs import resolve_wait_timeout, with_job_timeout_hint
from .base import BaseService


def _invalid(message: str) -> KeboolaApiError:
    return KeboolaApiError(
        message=message,
        status_code=400,
        error_code=ErrorCode.INVALID_ARGUMENT,
        retryable=False,
    )


def validate_clustering_request(fields: list[str] | None, clear: bool) -> list[str] | None:
    """Return the requested clustering fields, or ``None`` for ``--clear``.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` for a request Storage would refuse
            or that names no change.
    """
    if fields and clear:
        raise _invalid("--clustering-field and --clear are mutually exclusive; pass one of them.")
    if clear:
        return None
    if not fields:
        raise _invalid("Pass --clustering-field COL (repeatable) or --clear.")
    if any(not field.strip() for field in fields):
        raise _invalid("--clustering-field must not be blank.")
    duplicates = sorted({field for field in fields if fields.count(field) > 1})
    if duplicates:
        raise _invalid(f"--clustering-field names a column twice: {', '.join(duplicates)}.")
    if len(fields) > BIGQUERY_CLUSTERING_MAX_FIELDS:
        raise _invalid(
            f"--clustering-field takes at most {BIGQUERY_CLUSTERING_MAX_FIELDS} columns, "
            f"got {len(fields)}."
        )
    return list(fields)


def stored_clustering(table: dict[str, Any]) -> list[str] | None:
    """The table's own clustering fields, or ``None`` when it has none.

    Reads the table's own ``definition`` only: an alias must not report the
    clustering of its source table.
    """
    definition = table.get("definition")
    clustering = definition.get("clustering") if isinstance(definition, dict) else None
    fields = clustering.get("fields") if isinstance(clustering, dict) else None
    return [str(field) for field in fields] if isinstance(fields, list) and fields else None


def _not_allowed_as_invalid_argument(exc: KeboolaApiError) -> KeboolaApiError:
    """Storage's clustering refusal is a usage error: another table or other columns fix it."""
    if exc.status_code == 422 and exc.details.get("api_error_code") == (
        CLUSTERING_UPDATE_NOT_ALLOWED_CODE
    ):
        return KeboolaApiError(
            message=exc.message,
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
            details=exc.details,
        )
    return exc


class TableClusteringMixin(BaseService):
    """The ``storage clustering-set`` half of ``StorageService``."""

    def set_table_clustering(
        self,
        alias: str,
        table_id: str,
        fields: list[str] | None,
        clear: bool,
        branch_id: int | None = None,
        dry_run: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Set or remove the clustering of a BigQuery table in place.

        Storage enqueues a job even when the clustering does not change, so the
        stored clustering is read first and an equal request returns
        ``status: "unchanged"`` without a write.

        Raises:
            ValueError: ``timeout`` is zero, negative, NaN or infinite.
            KeboolaApiError: ``INVALID_ARGUMENT`` for a bad request or Storage's
                clustering refusal (``details.api_error_code`` kept);
                ``STORAGE_JOB_TIMEOUT`` (not retryable) naming the follow-up commands.
        """
        max_wait = resolve_wait_timeout(timeout, STORAGE_JOB_MAX_WAIT)
        requested = validate_clustering_request(fields, clear)
        branch_id = resolve_branch(self._config_store, alias, branch_id)
        project = self.resolve_projects([alias])[alias]
        client = self._client_factory(project.stack_url, project.token)
        base = {"project_alias": alias, "table_id": table_id, "branch_id": branch_id}
        try:
            table = client.get_table_detail(table_id, branch_id=branch_id)
            before = stored_clustering(table)
            if dry_run:
                return {
                    "dry_run": True,
                    **base,
                    "clustering_before": before,
                    "clustering": requested,
                    "would_change": bool(table.get("isAlias")) or before != requested,
                }
            if not table.get("isAlias") and before == requested:
                return {
                    **base,
                    "clustering_before": before,
                    "clustering": before,
                    "status": "unchanged",
                    "job_id": None,
                    "result": None,
                    "message": f"Clustering of table '{table_id}' is already as requested.",
                }
            try:
                job = client.update_table_definition(
                    table_id,
                    clustering=requested,
                    clustering_set=True,
                    branch_id=branch_id,
                    max_wait=max_wait,
                )
            except KeboolaApiError as exc:
                raise with_job_timeout_hint(
                    _not_allowed_as_invalid_argument(exc), alias, table_id, branch_id
                ) from exc
            after = stored_clustering(client.get_table_detail(table_id, branch_id=branch_id))
        finally:
            client.close()
        action = "removed from" if requested is None else "set on"
        return {
            **base,
            "clustering_before": before,
            "clustering": after,
            "status": "updated",
            "job_id": job.get("id"),
            "result": job,
            "message": f"Clustering {action} table '{table_id}' in project '{alias}'.",
        }
