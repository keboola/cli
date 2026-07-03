"""Public, in-process library facade for Keboola (issue #415).

A stateless importable surface so any Python consumer -- a Keboola Data App, a
transformation, a hosted service -- can use kbagent's Query Service and Storage
Files *in-process*: no CLI subprocess, no ``kbagent serve`` daemon, no
config-dir, no ``project add`` ceremony.

    from keboola_agent_cli import Client

    with Client(url=KBC_URL, token=KBC_TOKEN) as kbc:
        rows = kbc.query(workspace_id, "SELECT id, name FROM t")   # list[dict]
        meta = kbc.files.upload(b"hello", name="greeting.txt", tags=["x"])
        data = kbc.files.read_bytes(meta.id)                       # bytes
        metas = kbc.files.list(tags=["x"])                         # list[FileEntry]
        job = kbc.run_job("keboola.ex-db-snowflake", "12345", wait=True)  # JobResult

The facade is a thin convenience wrapper over :class:`KeboolaClient`
(``client.py``); it adds the high-level shapes -- ``list[dict]`` rows, ``bytes``
file reads, a stable :class:`FileEntry`, and the typed result models in
``result_models.py`` (:class:`JobResult`, :class:`QueryResult`,
:class:`UploadTableResult`, :class:`ConfigDetailResult`) -- that the CLI used to
assemble inside its service layer. Auth is the storage token passed at
construction (12-factor: read it from ``KBC_TOKEN`` yourself); nothing is
persisted to disk.

For replay-safe job runs (issue #427), pass an ``idempotency_store`` (the facade
is config-dir-free, so the consumer supplies *where* to persist the dedup map --
typically inside its own resume-checkpoint dir) and an ``idempotency_key`` per
``run_job``.

Everything exported here is committed public API and changes follow semver. For
lower-level access (raw Queue/Storage endpoints) reach for the underlying
:class:`KeboolaClient` via :attr:`Client.raw`.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import KeboolaClient, _collect_inline_results
from .constants import (
    DEFAULT_JOB_MODE,
    DEFAULT_JOB_RUN_TIMEOUT,
    DEFAULT_POLL_STRATEGY,
    QUERY_RESULTS_DEFAULT_LIMIT,
    STREAM_DEFAULT_BRANCH,
)
from .errors import ErrorCode, KeboolaApiError
from .result_models import (
    ConfigDetailResult,
    JobResult,
    QueryResult,
    ScopedTokenResult,
    StreamSourceResult,
    UploadTableResult,
)
from .services.job_idempotency_store import JobIdempotencyStore, run_idempotent_job

logger = logging.getLogger(__name__)

# The public ``Files.list`` method shadows the builtin ``list`` inside that
# class body, so ``list[...]`` *annotations* there would resolve to the method
# (under both runtime evaluation and ``ty``). Method *bodies* are unaffected --
# class scope is skipped in function name resolution -- so this alias is only
# needed for annotations inside ``Files``.
_list = list


@dataclass(frozen=True)
class FileEntry:
    """Stable, uniform shape for a Storage File across list and upload results.

    Always carries the same fields regardless of whether the underlying API
    response happened to include a (sometimes-absent) signed download URL.
    Read the bytes with :meth:`Files.read_bytes`, never by branching on a URL.
    ``raw`` is the untouched API dict for fields the facade does not surface.
    """

    id: int
    name: str
    tags: list[str]
    created: str | None
    size_bytes: int | None
    is_permanent: bool
    raw: dict[str, Any]

    @classmethod
    def _from_api(cls, data: dict[str, Any]) -> FileEntry:
        return cls(
            id=int(data["id"]),
            name=data.get("name", ""),
            tags=list(data.get("tags") or []),
            created=data.get("created"),
            size_bytes=data.get("sizeBytes"),
            is_permanent=bool(data.get("isPermanent", False)),
            raw=data,
        )


class Files:
    """Storage Files operations bound to one project (and optional branch).

    Obtained via :attr:`Client.files`; not constructed directly.
    """

    def __init__(self, client: KeboolaClient, branch_id: int | None) -> None:
        self._client = client
        self._branch_id = branch_id

    def list(
        self,
        *,
        tags: _list[str] | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
        since_id: int | None = None,
    ) -> _list[FileEntry]:
        """List Storage Files as uniform :class:`FileEntry` records.

        Args:
            tags: Filter to files carrying all of these tags (AND logic).
            query: Full-text search over the file name.
            limit: Max files to return.
            offset: Pagination offset.
            since_id: Return only files with an ID greater than this.
        """
        raw = self._client.list_files(
            limit=limit,
            offset=offset,
            tags=tags,
            since_id=since_id,
            query=query,
            branch_id=self._branch_id,
        )
        return [FileEntry._from_api(item) for item in raw]

    def upload(
        self,
        source: str | Path | bytes | bytearray,
        *,
        name: str | None = None,
        tags: _list[str] | None = None,
        permanent: bool = False,
    ) -> FileEntry:
        """Upload a local path or in-memory bytes to Storage Files.

        Args:
            source: A filesystem path (``str``/``Path``) or raw ``bytes`` to
                upload. When passing bytes, ``name`` is required (Storage needs
                a file name and there is no path to derive it from).
            name: Storage file name. Defaults to the path basename for path
                sources; required for bytes sources.
            tags: Tags to assign.
            permanent: If True the file is not auto-expired after 15 days.

        Raises:
            ValueError: If ``source`` is bytes and ``name`` is not given.
        """
        if isinstance(source, (bytes, bytearray)):
            if not name:
                raise ValueError("name is required when uploading raw bytes")
            # Reuse the battle-tested prepare + multi-cloud upload path
            # (S3/GCS/Azure, issue #187 streaming) by staging the bytes in a
            # temp dir rather than duplicating _upload_to_cloud. A TemporaryDirectory
            # is auto-removed even if the upload raises (no leaked temp file); the
            # staged file name is irrelevant -- `name` is what Storage records.
            with tempfile.TemporaryDirectory() as tmpdir:
                staged = Path(tmpdir) / "upload"
                staged.write_bytes(bytes(source))
                info = self._client.upload_file(
                    file_path=str(staged),
                    name=name,
                    tags=tags,
                    is_permanent=permanent,
                    branch_id=self._branch_id,
                )
        else:
            info = self._client.upload_file(
                file_path=str(source),
                name=name,
                tags=tags,
                is_permanent=permanent,
                branch_id=self._branch_id,
            )
        return FileEntry._from_api(info)

    def read_bytes(self, file_id: int) -> bytes:
        """Download a Storage File fully into memory and return its bytes.

        Hides the file-info -> signed-URL -> stream dance and transparently
        handles sliced files and gzip. The whole payload is held in RAM, so use
        this for reasonably sized files (results, manifests, small exports); for
        multi-GB tables stream to disk via :attr:`Client.raw` instead.
        """
        info = self._client.get_file_info(file_id, branch_id=self._branch_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "download"
            if info.get("isSliced", False):
                self._client.download_sliced_file(info, str(out))
            else:
                url = info.get("url")
                if not url:
                    raise KeboolaApiError(
                        f"Storage file {file_id} has no download URL.",
                        error_code=ErrorCode.API_ERROR,
                    )
                self._client.download_file(url, str(out))
            return out.read_bytes()

    def delete(self, file_id: int) -> None:
        """Delete a Storage File."""
        self._client.delete_file(file_id, branch_id=self._branch_id)


class Client:
    """Stateless in-process entry point to one Keboola project.

    Holds nothing but the stack URL, token, a single :class:`KeboolaClient`
    (which carries the shared retry/backoff), and an optional idempotency store.
    No config-dir, no ``project add``.

    Args:
        url: Stack URL, e.g. ``https://connection.keboola.com``.
        token: Storage API token for the project.
        branch_id: Dev branch to scope every operation to. ``None`` (default)
            targets production: Storage Files use the production scope and
            :meth:`query` resolves the project's default branch on first use.
        idempotency_store: Optional default :class:`JobIdempotencyStore` used by
            :meth:`run_job` when an ``idempotency_key`` is given (issue #427).
            The facade is config-dir-free, so the consumer decides where the
            dedup map lives. A per-call ``idempotency_store`` overrides this.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        branch_id: int | None = None,
        idempotency_store: JobIdempotencyStore | None = None,
    ) -> None:
        if not url:
            raise ValueError("url is required")
        if not token:
            raise ValueError("token is required")
        self._client = KeboolaClient(stack_url=url, token=token)
        self._resolved_branch_id = branch_id
        self._idempotency_store = idempotency_store
        self.files = Files(self._client, branch_id)

    @property
    def raw(self) -> KeboolaClient:
        """The underlying low-level client, for endpoints the facade omits."""
        return self._client

    def _effective_branch_id(self) -> int:
        """Resolve the branch ID for query submission (caches the default)."""
        if self._resolved_branch_id is not None:
            return self._resolved_branch_id
        for branch in self._client.list_dev_branches():
            if branch.get("isDefault", False):
                self._resolved_branch_id = int(branch["id"])
                return self._resolved_branch_id
        raise KeboolaApiError(
            "No default branch found for this project.",
            error_code=ErrorCode.NOT_FOUND,
        )

    def _run_query(
        self,
        workspace_id: int,
        sql: str,
        *,
        transactional: bool,
        limit: int,
    ) -> QueryResult:
        """Submit SQL, wait for completion, and collect the last result set.

        Shared by :meth:`query` (which returns just the rows) and
        :meth:`query_result` (which returns the full typed shape). Mirrors the
        Query Service inline-results fast path: the rows of the *last*
        result-producing statement win, statements without a result set yield
        nothing, and an over-``limit`` result is capped with a logged warning.
        """
        branch_id = self._effective_branch_id()
        job = self._client.submit_query(
            branch_id=branch_id,
            workspace_id=workspace_id,
            statements=[sql],
            transactional=transactional,
        )
        job_id = str(job.get("queryJobId", job.get("id", "")))
        completed = self._client.wait_for_query_job(job_id)

        result = QueryResult()
        for stmt in completed.get("statements", []):
            num_rows = stmt.get("numberOfRows", stmt.get("resultRows", 0))
            if stmt.get("status") != "completed" or not num_rows:
                continue
            inline = _collect_inline_results(self._client, job_id, str(stmt.get("id", "")), limit)
            col_names = [col.get("name", "") for col in inline.columns]
            result = QueryResult(
                columns=col_names,
                rows=[dict(zip(col_names, row, strict=False)) for row in inline.rows],
                truncated=inline.truncated,
                total_rows=inline.total_rows,
            )
            if inline.truncated:
                logger.warning(
                    "query result truncated to %d rows (warehouse has %s); "
                    "raise limit= to fetch more",
                    result.row_count,
                    inline.total_rows,
                )
        return result

    def query(
        self,
        workspace_id: int,
        sql: str,
        *,
        transactional: bool = False,
        limit: int = QUERY_RESULTS_DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Run SQL in a workspace and return rows as a list of dicts.

        Submits the statement via Query Service, waits for completion, and reads
        results inline (the fast ``/results`` path, no CSV-file materialization).
        Each row is a dict keyed by the result column names exactly as the
        warehouse reports them -- note Snowflake folds unquoted aliases to
        UPPERCASE, so quote aliases if you want lowercase keys. Values are
        returned exactly as the Query Service serializes them and are NOT
        coerced by the facade: for Snowflake every scalar comes back as a JSON
        string (``1`` -> ``"1"``, ``1.5`` -> ``"1.5"``, ``true`` -> ``"true"``),
        with SQL ``NULL`` as ``None``. Cast on the caller side if you need typed
        values.

        When ``sql`` contains multiple statements, the rows of the *last*
        statement that produced a result set are returned (so ``USE ...; SELECT
        ...`` yields the SELECT). Statements without a result set yield ``[]``.

        For column order and truncation metadata, use :meth:`query_result`.

        Args:
            workspace_id: Target workspace ID.
            sql: One or more SQL statements.
            transactional: Wrap the statements in a transaction.
            limit: Max rows to fetch (default ``QUERY_RESULTS_DEFAULT_LIMIT``).
                If the warehouse has more, the result is capped and a warning is
                logged -- raise ``limit`` to fetch more.
        """
        return self._run_query(workspace_id, sql, transactional=transactional, limit=limit).rows

    def query_result(
        self,
        workspace_id: int,
        sql: str,
        *,
        transactional: bool = False,
        limit: int = QUERY_RESULTS_DEFAULT_LIMIT,
    ) -> QueryResult:
        """Run SQL in a workspace and return a typed :class:`QueryResult`.

        Same execution as :meth:`query`, but returns the full tabular shape --
        ``columns`` (in warehouse order), ``rows`` (list of dicts), ``truncated``
        and ``total_rows`` -- instead of just the row list. Use this when you
        need the column ordering or want to detect a ``limit`` cap. The
        string-typing gotcha from :meth:`query` applies to ``rows`` here too.
        """
        return self._run_query(workspace_id, sql, transactional=transactional, limit=limit)

    def run_job(
        self,
        component_id: str,
        config_id: str,
        *,
        config_row_ids: list[str] | None = None,
        variable_values_id: str | None = None,
        branch_id: int | None = None,
        mode: str = DEFAULT_JOB_MODE,
        wait: bool = False,
        timeout: float = DEFAULT_JOB_RUN_TIMEOUT,
        poll_strategy: str = DEFAULT_POLL_STRATEGY,
        idempotency_key: str | None = None,
        force_rerun: bool = False,
        idempotency_store: JobIdempotencyStore | None = None,
    ) -> JobResult:
        """Run a Queue API job and return a typed :class:`JobResult`.

        Creates the job, and -- when ``wait=True`` -- polls until it reaches a
        terminal state (or ``timeout`` elapses). Unlike ``JobService.run_job``
        this thin facade does **not** auto-resolve linked variable values; pass
        ``variable_values_id`` explicitly if the config needs a values row.

        Idempotency (issue #427): pass ``idempotency_key`` (plus an
        ``idempotency_store`` here or on the constructor) to make a replayed call
        safe -- a prior still-running or non-failed job is returned (with
        ``JobResult.idempotent_replay = True``) instead of creating a duplicate.
        A prior *failed* run is re-run; ``force_rerun=True`` always creates a
        fresh job. The Queue API has no server-side idempotency, so this is
        client-side and scoped to the supplied store.

        Args:
            component_id: Component to run, e.g. ``keboola.ex-db-snowflake``.
            config_id: Configuration ID to run.
            config_row_ids: Optional row IDs (omit to run the whole config).
            variable_values_id: Optional explicit values row for linked variables.
            branch_id: Dev branch to run on. Defaults to the client's branch
                (``None`` = production).
            mode: Queue job mode (``run`` | ``debug`` | ``forceRun``).
            wait: If True, poll until the job finishes or ``timeout`` elapses.
            timeout: Max seconds to wait (only used when ``wait=True``).
            poll_strategy: Wait cadence, one of ``VALID_POLL_STRATEGIES``.
            idempotency_key: Client-supplied de-duplication token.
            force_rerun: Ignore any stored entry for ``idempotency_key``.
            idempotency_store: Per-call store override (else the constructor's).

        Raises:
            ValueError: If ``idempotency_key`` is given but no store is available
                (the stateless facade cannot invent a persistence location).
        """
        effective_branch = branch_id if branch_id is not None else self._resolved_branch_id

        def _create() -> dict[str, Any]:
            return self._client.create_job(
                component_id=component_id,
                config_id=config_id,
                config_row_ids=config_row_ids,
                mode=mode,
                branch_id=effective_branch,
                variable_values_id=variable_values_id,
            )

        store = idempotency_store if idempotency_store is not None else self._idempotency_store
        if idempotency_key and store is None:
            raise ValueError(
                "idempotency_key requires an idempotency_store -- pass one to "
                "Client(...) or to run_job(...). The stateless facade has no "
                "config-dir to default it to."
            )

        job, replayed = run_idempotent_job(
            store=store,
            key=idempotency_key,
            component_id=component_id,
            config_id=config_id,
            branch_id=effective_branch,
            force_rerun=force_rerun,
            create=_create,
            fetch=self._client.get_job_detail,
        )
        job_id = str(job.get("id", ""))
        if wait and job_id:
            job = self._client.wait_for_queue_job(
                job_id, max_wait=timeout, poll_strategy=poll_strategy
            )
        if replayed:
            job = {**job, "idempotent_replay": True}
        return JobResult.model_validate(job)

    def config_detail(
        self,
        component_id: str,
        config_id: str,
        *,
        branch_id: int | None = None,
    ) -> ConfigDetailResult:
        """Fetch one configuration's detail as a typed :class:`ConfigDetailResult`.

        Args:
            component_id: Owning component ID.
            config_id: Configuration ID.
            branch_id: Dev branch to read from. Defaults to the client's branch
                (``None`` = production).
        """
        effective_branch = branch_id if branch_id is not None else self._resolved_branch_id
        detail = dict(
            self._client.get_config_detail(component_id, config_id, branch_id=effective_branch)
        )
        detail.setdefault("component_id", component_id)
        if effective_branch is not None:
            detail.setdefault("branch_id", effective_branch)
        return ConfigDetailResult.model_validate(detail)

    def upload_table(
        self,
        table_id: str,
        file_path: str | Path,
        *,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        branch_id: int | None = None,
    ) -> UploadTableResult:
        """Import a CSV into an **existing** Storage table -> :class:`UploadTableResult`.

        Unlike ``StorageService.upload_table`` the facade does **not** auto-create
        a missing bucket/table (it has no config-dir / service context); the
        target table must already exist. Use the CLI (``kbagent storage
        upload-table``) for the auto-create path.

        Args:
            table_id: Target table ID (must exist).
            file_path: Local CSV path.
            incremental: Append rows (True) or full load (False).
            delimiter: CSV column delimiter.
            enclosure: CSV value enclosure character.
            branch_id: Dev branch to target. Defaults to the client's branch
                (``None`` = production).
        """
        effective_branch = branch_id if branch_id is not None else self._resolved_branch_id
        file_size_bytes = Path(file_path).stat().st_size
        results = self._client.upload_table(
            table_id=table_id,
            file_path=str(file_path),
            incremental=incremental,
            delimiter=delimiter,
            enclosure=enclosure,
            branch_id=effective_branch,
        )
        return UploadTableResult.model_validate(
            {
                "table_id": table_id,
                "incremental": incremental,
                "file_size_bytes": file_size_bytes,
                "imported_rows": results.get("importedRowsCount"),
                "warnings": results.get("warnings", []),
            }
        )

    # ------------------------------------------------------------------
    # Device-enrollment primitives (scoped tokens + per-device streams)
    # ------------------------------------------------------------------

    def create_scoped_token(
        self,
        *,
        description: str,
        bucket_permissions: dict[str, str] | None = None,
        component_access: list[str] | None = None,
        can_read_all_file_uploads: bool = False,
        expires_in: int | None = None,
    ) -> ScopedTokenResult:
        """Mint a scoped Storage API token as a typed :class:`ScopedTokenResult`.

        Expresses bucket-level grants (unlike the component-only
        ``raw.create_short_lived_token``): mint the narrow "upload Files + write
        one sink bucket, expiring" token a capture device needs. The acting token
        must carry ``canManageTokens``. ``result.token`` is a one-time reveal --
        persist only ``id`` and ``expires``. See
        :meth:`KeboolaClient.create_scoped_token` for the permission-model notes.
        """
        return ScopedTokenResult.model_validate(
            self._client.create_scoped_token(
                description=description,
                bucket_permissions=bucket_permissions,
                component_access=component_access,
                can_read_all_file_uploads=can_read_all_file_uploads,
                expires_in=expires_in,
            )
        )

    def delete_token(self, token_id: str) -> None:
        """Revoke a Storage API token immediately (active per-device revocation)."""
        self._client.delete_token(token_id)

    def refresh_token(self, token_id: str) -> ScopedTokenResult:
        """Rotate a Storage API token -> typed :class:`ScopedTokenResult`.

        Returns the new token value; the old one becomes immediately invalid.
        """
        return ScopedTokenResult.model_validate(self._client.refresh_token(token_id))

    def create_stream_source(
        self,
        name: str,
        *,
        source_type: str = "otlp",
        description: str = "",
        branch_id: str = STREAM_DEFAULT_BRANCH,
        provision_sinks: bool = True,
    ) -> StreamSourceResult:
        """Create a per-device OTLP stream source as a typed :class:`StreamSourceResult`.

        Async under the hood (the 202 task is polled to completion). For an
        ``otlp`` source ``provision_sinks`` (default) also creates the
        logs/metrics/traces sinks + ``in.c-otlp-<id>`` bucket so data lands and a
        device token can be scoped to write it (``result.sink_bucket_id``).
        ``result.otlp_url`` carries the ingest secret **unmasked** -- reveal to
        the device once, never persist it.
        """
        return StreamSourceResult.model_validate(
            self._client.create_stream_source(
                name,
                source_type=source_type,
                description=description,
                branch_id=branch_id,
                provision_sinks=provision_sinks,
            )
        )

    def get_stream_source(
        self, source_id: str, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> StreamSourceResult:
        """Fetch one stream source's detail as a typed :class:`StreamSourceResult`."""
        return StreamSourceResult.model_validate(
            self._client.get_stream_source(source_id, branch_id=branch_id)
        )

    def list_stream_sources(
        self, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> _list[dict[str, Any]]:
        """List the project's stream sources (raw source objects; find-or-create by name)."""
        return self._client.list_stream_sources(branch_id=branch_id)

    def delete_stream_source(
        self, source_id: str, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> None:
        """Delete a stream source (per-device event-plane revocation)."""
        self._client.delete_stream_source(source_id, branch_id=branch_id)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
