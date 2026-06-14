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

The facade is a thin convenience wrapper over :class:`KeboolaClient`
(``client.py``); it adds the high-level shapes -- ``list[dict]`` rows, ``bytes``
file reads, a stable :class:`FileEntry` -- that the CLI used to assemble inside
its service layer. Auth is the storage token passed at construction (12-factor:
read it from ``KBC_TOKEN`` yourself); nothing is persisted to disk.

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
from .constants import QUERY_RESULTS_DEFAULT_LIMIT
from .errors import ErrorCode, KeboolaApiError

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

    Holds nothing but the stack URL, token, and a single :class:`KeboolaClient`
    (which carries the shared retry/backoff). No config-dir, no ``project add``.

    Args:
        url: Stack URL, e.g. ``https://connection.keboola.com``.
        token: Storage API token for the project.
        branch_id: Dev branch to scope every operation to. ``None`` (default)
            targets production: Storage Files use the production scope and
            :meth:`query` resolves the project's default branch on first use.
    """

    def __init__(self, url: str, token: str, *, branch_id: int | None = None) -> None:
        if not url:
            raise ValueError("url is required")
        if not token:
            raise ValueError("token is required")
        self._client = KeboolaClient(stack_url=url, token=token)
        self._resolved_branch_id = branch_id
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

        Args:
            workspace_id: Target workspace ID.
            sql: One or more SQL statements.
            transactional: Wrap the statements in a transaction.
            limit: Max rows to fetch (default ``QUERY_RESULTS_DEFAULT_LIMIT``).
                If the warehouse has more, the result is capped and a warning is
                logged -- raise ``limit`` to fetch more.
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

        rows: list[dict[str, Any]] = []
        for stmt in completed.get("statements", []):
            num_rows = stmt.get("numberOfRows", stmt.get("resultRows", 0))
            if stmt.get("status") != "completed" or not num_rows:
                continue
            inline = _collect_inline_results(self._client, job_id, str(stmt.get("id", "")), limit)
            col_names = [col.get("name", "") for col in inline.columns]
            rows = [dict(zip(col_names, row, strict=False)) for row in inline.rows]
            if inline.truncated:
                logger.warning(
                    "query result truncated to %d rows (warehouse has %s); "
                    "raise limit= to fetch more",
                    len(rows),
                    inline.total_rows,
                )
        return rows

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
