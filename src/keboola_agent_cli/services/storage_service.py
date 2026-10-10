"""Storage service - business logic for bucket and table operations.

Provides direct access to Storage API data including the sharing/linked
bucket metadata (sourceBucket, sourceProject) that thinner listings drop.
"""

import csv
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..constants import STORAGE_BRANCHES_FEATURE
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from ._column_descriptions import ColumnDescriptionsMixin
from ._storage_tables import normalize_table_rows
from ._table_detail import build_table_detail
from .table_usage import collect_table_usage, fetch_usage_components

logger = logging.getLogger(__name__)


def _detect_legacy_branch_storage(client: Any, branch_id: int | None) -> bool:
    """Return True when ``--branch`` is targeting a project without ``storage-branches``.

    Called from branch-aware write paths so the response can warn the user
    that the materialized bucket will not be picked up by the transformation
    runner (which uses the legacy ``out.c-<branch_id>-*`` rewrite scheme on
    such projects). Production-context calls (``branch_id is None``) skip
    the check entirely -- there is no fake-branch behavior to warn about.

    Errors during the verify_token roundtrip degrade gracefully to ``False``.
    The feature flag is purely informational here; we do not want a transient
    network blip to suppress the actual write the user asked for.
    """
    if branch_id is None:
        return False
    try:
        return not client.has_feature(STORAGE_BRANCHES_FEATURE)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not determine project features for legacy-branch-storage check: %s",
            exc,
        )
        return False


def _safe_download_target(base: Path, server_name: str) -> Path:
    """Contain an API-supplied file name under ``base``.

    The Storage API controls the file ``name``; using it verbatim as a write
    path lets a malicious or compromised response escape the user's chosen
    directory (``../../etc/...`` or an absolute path) and overwrite arbitrary
    files with attacker-controlled bytes. We strip leading separators so an
    absolute name cannot override ``base``, preserve legitimate nested
    subpaths, and assert the resolved path stays within ``base``.
    """
    cleaned = server_name.lstrip("/\\").strip() or "download"
    candidate = (base / cleaned).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise KeboolaApiError(
            message=(
                f"Refusing to write outside the target directory: the "
                f"server-provided file name {server_name!r} escapes {base.resolve()}"
            ),
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )
    return candidate


# "name:TYPE" or "name:TYPE(length)" -- type is pass-through to the Keboola
# Storage API, which validates type/length combinations per backend and
# returns clear errors (e.g. "'10' is not valid length for INTEGER"). This
# lets the CLI accept any native backend type (VARCHAR, NUMBER, TIMESTAMP_TZ,
# VARIANT, ...) without maintaining a per-backend whitelist.
_COL_SPEC_RE = re.compile(
    r"^\s*(?P<name>[^:\s][^:]*?)"
    r"\s*:\s*(?P<type>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*(?:\(\s*(?P<length>[0-9][0-9,\s]*)\s*\))?"
    r"\s*$"
)


def _read_csv_header(file_path: str, delimiter: str = ",") -> list[str]:
    """Return column names from the first row of a CSV file.

    Strips leading/trailing whitespace and skips empty fields. Handles
    UTF-8 BOM automatically (utf-8-sig encoding).

    Raises:
        ValueError: If the first row is empty or contains no non-empty fields.
    """
    with open(file_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        header = next(reader, [])
    columns = [col.strip() for col in header if col.strip()]
    if not columns:
        raise ValueError("CSV file has no column headers in the first row.")
    return columns


def _parse_column_spec(
    col_spec: str,
    not_null: set[str],
    defaults: dict[str, str],
) -> dict[str, Any]:
    """Parse a single --column argument into a Storage API column definition.

    Accepted shapes:

    - ``name``                  bare name; implicit STRING default
    - ``name:TYPE``             e.g. ``id:INTEGER``, ``name:STRING``
    - ``name:TYPE(length)``     e.g. ``amount:NUMERIC(18,2)``, ``pk:VARCHAR(40)``

    The TYPE value is uppercased and passed through to the Storage API
    unmodified -- Keboola validates type/length per backend (Snowflake,
    BigQuery, Redshift) and returns precise errors for invalid combinations.
    No per-backend whitelist is maintained here.
    """
    if ":" not in col_spec:
        col_name, col_type, col_length = col_spec.strip(), "STRING", None
    else:
        m = _COL_SPEC_RE.match(col_spec)
        if not m:
            raise ValueError(
                f"Invalid column spec {col_spec!r}. Expected 'name:TYPE' or "
                f"'name:TYPE(length)' (e.g. 'amount:NUMERIC(18,2)', "
                f"'pk:VARCHAR(40)', 'ts:TIMESTAMP_TZ')."
            )
        col_name = m.group("name").strip()
        col_type = m.group("type").upper()
        raw_length = m.group("length")
        col_length = re.sub(r"\s+", "", raw_length) if raw_length is not None else None

    if not col_name:
        raise ValueError(f"Column name is empty in spec {col_spec!r}.")

    definition: dict[str, Any] = {"type": col_type}
    if col_length:
        definition["length"] = col_length
    if col_name in not_null:
        definition["nullable"] = False
    if col_name in defaults:
        definition["default"] = defaults[col_name]
    return {"name": col_name, "definition": definition}


def _parse_default_assignments(defaults: list[str] | None) -> dict[str, str]:
    """Parse ``--default NAME=VALUE`` assignments into a mapping.

    An empty VALUE (e.g. ``--default flag=``) is accepted and produces an
    empty-string default, consistent with how most shells pass trailing
    ``=`` arguments.
    """
    if not defaults:
        return {}
    result: dict[str, str] = {}
    for spec in defaults:
        if "=" not in spec:
            raise ValueError(
                f"Invalid --default {spec!r}. Expected 'NAME=VALUE' (e.g. 'flag=false')."
            )
        name, _, value = spec.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"Invalid --default {spec!r}: empty column name.")
        result[name] = value
    return result


def _build_source(
    source_table_id: str | None,
    source_branch_id: int | None,
) -> dict[str, Any] | None:
    """Build the optional ``source`` object for the tables-definition endpoint.

    Returns ``None`` when no source is requested. ``branchId`` is only included
    when explicitly given; otherwise the API defaults it to the request branch.
    """
    if source_table_id is None:
        return None
    if not source_table_id.strip():
        raise ValueError("--source-table-id must not be empty.")
    source: dict[str, Any] = {"tableId": source_table_id}
    if source_branch_id is not None:
        source["branchId"] = source_branch_id
    return source


def _build_bigquery_layout(
    time_partitioning_type: str | None,
    time_partitioning_field: str | None,
    time_partitioning_expiration_ms: str | None,
    range_partitioning_field: str | None,
    range_partitioning_start: str | None,
    range_partitioning_end: str | None,
    range_partitioning_interval: str | None,
    clustering_fields: list[str] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Assemble the BigQuery partition/clustering objects from typed flags.

    Mirrors the shapes the Storage API already expects (connection's
    ``BigqueryCreateTableDefinitionRequest``):

    - ``timePartitioning`` ``{"type", "field"?, "expirationMs"?}`` -- ``type`` required.
    - ``rangePartitioning`` ``{"field", "range": {"start", "end", "interval"}}`` --
      all four required together; range bounds are **strings** in the API.
    - ``clustering`` ``{"fields": [...]}``.

    ``timePartitioning`` and ``rangePartitioning`` are mutually exclusive
    (BigQuery allows only one partitioning kind). Returns ``(None, None, None)``
    when nothing is requested.

    Raises:
        ValueError: incomplete/conflicting partitioning flags.
    """
    has_time = any(
        v is not None
        for v in (time_partitioning_type, time_partitioning_field, time_partitioning_expiration_ms)
    )
    range_parts = (
        range_partitioning_field,
        range_partitioning_start,
        range_partitioning_end,
        range_partitioning_interval,
    )
    has_range = any(v is not None for v in range_parts)

    if has_time and has_range:
        raise ValueError(
            "--time-partitioning-* and --range-partitioning-* are mutually exclusive; "
            "BigQuery supports only one partitioning kind per table."
        )

    time_partitioning: dict[str, Any] | None = None
    if has_time:
        if not time_partitioning_type:
            raise ValueError(
                "--time-partitioning-type is required when any --time-partitioning-* "
                "flag is set (e.g. DAY, HOUR, MONTH, YEAR)."
            )
        time_partitioning = {"type": time_partitioning_type}
        if time_partitioning_field is not None:
            time_partitioning["field"] = time_partitioning_field
        if time_partitioning_expiration_ms is not None:
            time_partitioning["expirationMs"] = time_partitioning_expiration_ms

    range_partitioning: dict[str, Any] | None = None
    if has_range:
        if not all(v is not None for v in range_parts):
            raise ValueError(
                "--range-partitioning requires all of --range-partitioning-field, "
                "--range-partitioning-start, --range-partitioning-end and "
                "--range-partitioning-interval."
            )
        range_partitioning = {
            "field": range_partitioning_field,
            "range": {
                "start": range_partitioning_start,
                "end": range_partitioning_end,
                "interval": range_partitioning_interval,
            },
        }

    clustering: dict[str, Any] | None = None
    if clustering_fields:
        clustering = {"fields": list(clustering_fields)}

    return time_partitioning, range_partitioning, clustering


def _ensure_bucket_exists_in_branch(
    client: Any,
    bucket_id: str,
    branch_id: int | None,
) -> bool:
    """Ensure ``bucket_id`` exists in the target branch; materialize it on 404.

    Keboola dev branches have an isolated storage namespace: a production
    bucket is visible to a branch for READS (transparent fallback to the
    main branch) but a branch-scoped WRITE against an unmaterialized bucket
    returns ``Bucket not found``. This helper mirrors ``EnsureBucketExists``
    from the official Go CLI (keboola-as-code: pkg/lib/operation/project/
    remote/table/import/operation.go) -- on 404, it creates the bucket in
    the branch with the same ``stage`` + ``name`` so the subsequent write
    has a target.

    On projects with the "branched storage" feature enabled, the
    transformation runner's output mapping (``keboola/output-mapping``,
    ``Storage/BucketCreator::checkDevBucketMetadata``) requires the bucket
    to carry ``KBC.createdBy.branch.id`` metadata equal to the current
    branch id; without it the runner aborts with "bucket is not assigned
    to any development branch." Storage API does not auto-populate the
    field on ``POST /branch/{id}/buckets``, so we set it explicitly right
    after creation. Failure to write the metadata is logged but does not
    abort the operation -- the table-create call still happens, and on
    branched-storage projects it will surface the assignment error there.
    Closes #224.

    No-op when ``branch_id`` is None (production writes do not need
    materialization).

    Returns:
        True if the bucket was auto-created, False otherwise.
    """
    if branch_id is None:
        return False

    try:
        client.get_bucket_detail(bucket_id, branch_id=branch_id)
        return False
    except KeboolaApiError as exc:
        if exc.status_code != 404:
            raise

    parts = bucket_id.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Cannot materialize bucket {bucket_id!r} in branch {branch_id}: "
            f"expected 'stage.c-name' format."
        )
    stage, slug = parts
    bucket_name = slug.removeprefix("c-")
    client.create_bucket(stage=stage, name=bucket_name, branch_id=branch_id)
    logger.info(
        "Auto-materialized bucket %s in branch %s (stage=%s, name=%s)",
        bucket_id,
        branch_id,
        stage,
        bucket_name,
    )
    try:
        client.set_bucket_metadata(
            bucket_id=bucket_id,
            entries=[("KBC.createdBy.branch.id", str(branch_id))],
            provider="system",
            branch_id=branch_id,
        )
    except KeboolaApiError as exc:
        logger.warning(
            "Auto-materialized bucket %s in branch %s but failed to set "
            "KBC.createdBy.branch.id metadata (%s); transformation runners "
            "with branched-storage may reject writes to this bucket.",
            bucket_id,
            branch_id,
            exc,
        )
    return True


def _write_columns_sidecar(output_dir: str, columns: list[str]) -> None:
    """Write a _columns.csv sidecar listing the table's column order.

    Storage exports slices without a header row; the column list comes from
    the table metadata. Writing a tiny sidecar here lets downstream tools
    (DuckDB read_csv, polars scan_csv) reconstruct the schema without
    querying Storage. Using the ``_`` prefix matches the _manifest.json
    convention that pyarrow/Spark/Hive use for "skip when reading as a
    dataset".
    """
    sidecar = Path(output_dir) / "_columns.csv"
    with sidecar.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(columns)


def _prepend_csv_header(file_path: str, columns: list[str]) -> None:
    """Prepend a CSV header row to an existing file.

    Streams the original body into a temp file so that multi-GB CSV exports
    never sit in RAM at once (issue #187: the old read_bytes() peaked at the
    full file size). Uses CSV quoting to match Keboola's RFC4180 format.
    """
    import io
    import shutil
    import tempfile

    writer_buf = io.StringIO()
    writer = csv.writer(writer_buf, quoting=csv.QUOTE_ALL)
    writer.writerow(columns)
    header_line = writer_buf.getvalue().encode("utf-8")

    p = Path(file_path)
    # Temp file sits next to the target so shutil.move is a cheap rename on
    # the same filesystem.
    with tempfile.NamedTemporaryFile(
        dir=p.parent, prefix=p.name + ".", suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(header_line)
        with p.open("rb") as src:
            shutil.copyfileobj(src, tmp, length=1024 * 1024)
    tmp_path.replace(p)


class StorageService(ColumnDescriptionsMixin):
    """Business logic for storage bucket and table operations.

    Supports multi-project parallel queries for listing operations.
    """

    def list_buckets(
        self,
        aliases: list[str] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List storage buckets from one or more projects.

        Includes sharing/linked bucket metadata (sourceBucket, sourceProject).

        Args:
            aliases: Project aliases to query. If None, queries all.
            branch_id: If set, list buckets from a specific dev branch.

        Returns:
            Dict with 'buckets' list and 'errors' list.
        """
        projects = self.resolve_projects(aliases)

        def _worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, Any]]:
            return self._fetch_buckets(alias, project, branch_id=branch_id)

        successes, errors = self._run_parallel(projects, _worker)

        buckets: list[dict[str, Any]] = []
        for result in successes:
            alias = result[0]
            for bucket in result[1]:
                entry: dict[str, Any] = {
                    "project_alias": alias,
                    "id": bucket.get("id", ""),
                    "display_name": bucket.get("displayName", bucket.get("name", "")),
                    "stage": bucket.get("stage", ""),
                    "backend": bucket.get("backend", ""),
                    # API may return null for these on empty buckets; coerce
                    # to 0 (dict.get default only fires when key is missing).
                    "rows_count": bucket.get("rowsCount") or 0,
                    "data_size_bytes": bucket.get("dataSizeBytes") or 0,
                    "description": bucket.get("description", ""),
                    "is_linked": False,
                    "source_project_id": None,
                    "source_project_name": "",
                    "source_bucket_id": "",
                }

                # Enrich with sharing info
                source = bucket.get("sourceBucket")
                if source:
                    entry["is_linked"] = True
                    src_project = source.get("project", {})
                    entry["source_project_id"] = src_project.get("id")
                    entry["source_project_name"] = src_project.get("name", "")
                    entry["source_bucket_id"] = source.get("id", "")

                buckets.append(entry)

        return {"buckets": buckets, "errors": errors}

    def get_bucket_detail(
        self,
        alias: str,
        bucket_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed bucket info including tables and sharing metadata.

        For linked buckets, includes the backend-native direct access path.

        The output adapts to the bucket's backend:
        - Snowflake -> ``snowflake_database``/``snowflake_schema``/per-table
          ``snowflake_path`` quoted with ``"..."``.
        - BigQuery  -> ``bigquery_dataset``/per-table ``bigquery_path`` quoted
          with backticks. ``bigquery_project`` is included only when the API
          surfaces it (``databaseName`` field) -- BigQuery's GCP project ID
          is not always discoverable from Storage API alone.

        Backend-agnostic keys ``sql_dialect`` and per-table ``sql_path`` are
        always present, so callers can use the right path without branching
        on backend themselves.

        Args:
            alias: Project alias.
            bucket_id: Bucket ID (e.g. 'in.c-db').
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with bucket detail, tables, and resolved direct-access paths.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            token_info = client.verify_token()
            bucket = client.get_bucket_detail(bucket_id, branch_id=branch_id)
        finally:
            client.close()

        project_id = token_info.project_id
        source = bucket.get("sourceBucket")

        # KBC.description in the metadata array takes precedence over the
        # native description field (which can only be set at creation time
        # via the Storage API; user updates go through the metadata endpoint).
        raw_metadata: list[dict[str, Any]] = bucket.get("metadata", [])
        metadata_description = ""
        for m in raw_metadata:
            if m.get("key") == "KBC.description" and m.get("provider") == "user":
                metadata_description = m.get("value", "") or ""
                break
        description = metadata_description or bucket.get("description", "")

        backend = (bucket.get("backend") or "").lower()
        result: dict[str, Any] = {
            "project_alias": alias,
            "project_id": project_id,
            "bucket_id": bucket.get("id", ""),
            "display_name": bucket.get("displayName", ""),
            "stage": bucket.get("stage", ""),
            "description": description,
            "backend": bucket.get("backend", ""),
            "is_linked": source is not None,
            "metadata": raw_metadata,
        }

        # backendPath shape is backend-specific:
        # - Snowflake: ["SAPI_<project_id>", "<bucket_id>"]   (database, schema)
        # - BigQuery:  ["<dataset_name>"]                      (dataset only;
        #              GCP project ID is NOT included)
        # We must NOT reconstruct identifiers ourselves (e.g. f"sapi_{id}")
        # because the actual case and naming differ across stacks; quoted
        # identifiers are case-sensitive on both Snowflake and BigQuery.
        backend_path = bucket.get("backendPath", []) or []

        if source:
            src_project = source.get("project", {})
            src_project_id = src_project.get("id")
            src_bucket_id = source.get("id", "")
            result["source_project_id"] = src_project_id
            result["source_project_name"] = src_project.get("name", "")
            result["source_bucket_id"] = src_bucket_id
        else:
            src_project_id = None
            src_bucket_id = ""
            result["source_project_id"] = None
            result["source_project_name"] = ""
            result["source_bucket_id"] = ""

        result["sql_dialect"] = backend or "snowflake"

        if backend == "bigquery":
            # BigQuery: dataset comes from backendPath[0]; GCP project may be
            # surfaced via databaseName but is often empty on Storage API.
            # When project is unknown we emit dataset-qualified paths only --
            # callers that need fully-qualified paths must supply the project
            # themselves (it's typically the workspace project for BYODB or
            # the Keboola-managed project name).
            dataset = backend_path[0] if backend_path else (bucket.get("path") or "")
            bq_project = bucket.get("databaseName") or ""
            result["bigquery_project"] = bq_project
            result["bigquery_dataset"] = dataset
            sql_db = bq_project
            sql_schema = dataset
            quote_open = "`"
            quote_close = "`"
        else:
            # Snowflake (and unknown backends, treated as Snowflake-style for
            # backwards compatibility with the original 0.1.x behaviour).
            if len(backend_path) >= 2:
                sf_db = backend_path[0]
                sf_schema = backend_path[1]
            elif source:
                sf_db = f"sapi_{src_project_id}"
                sf_schema = src_bucket_id
            else:
                sf_db = f"sapi_{project_id}"
                sf_schema = bucket.get("id", "")
            result["snowflake_database"] = sf_db
            result["snowflake_schema"] = sf_schema
            sql_db = sf_db
            sql_schema = sf_schema
            quote_open = '"'
            quote_close = '"'

        tables: list[dict[str, Any]] = []
        for table in bucket.get("tables", []):
            table_name = table.get("name", "")
            entry: dict[str, Any] = {
                "id": table.get("id", ""),
                "name": table_name,
                "display_name": table.get("displayName", table_name),
                "is_alias": table.get("isAlias", False),
            }
            if backend == "bigquery":
                if sql_db and sql_schema:
                    bq_path = (
                        f"{quote_open}{sql_db}{quote_close}."
                        f"{quote_open}{sql_schema}{quote_close}."
                        f"{quote_open}{table_name}{quote_close}"
                    )
                elif sql_schema:
                    bq_path = (
                        f"{quote_open}{sql_schema}{quote_close}."
                        f"{quote_open}{table_name}{quote_close}"
                    )
                else:
                    bq_path = ""
                entry["bigquery_path"] = bq_path
                entry["sql_path"] = bq_path
            else:
                sf_path = (
                    f"{quote_open}{sql_db}{quote_close}."
                    f"{quote_open}{sql_schema}{quote_close}."
                    f"{quote_open}{table_name}{quote_close}"
                )
                entry["snowflake_path"] = sf_path
                entry["sql_path"] = sf_path
            tables.append(entry)

        result["tables"] = tables
        result["table_count"] = len(tables)

        return result

    def get_table_detail(
        self,
        alias: str,
        table_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed info about a storage table including columns.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with table metadata, columns, size info and the raw Storage API
            ``definition`` (the BigQuery partition/cluster layout lives there).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            table = client.get_table_detail(table_id, branch_id=branch_id)
        finally:
            client.close()

        return build_table_detail(alias, table_id, table)

    def list_tables(
        self,
        aliases: list[str] | None = None,
        bucket_id: str | None = None,
        branch_id: int | None = None,
        include_usage: bool = False,
    ) -> dict[str, Any]:
        """List tables from one or more projects (in parallel).

        When ``bucket_id`` is specified with multiple projects, the filter is
        applied independently in each project -- a missing bucket in a given
        project is recorded as a per-project error without aborting the others.

        Args:
            aliases: Project aliases to query. If None, queries all.
            bucket_id: Optional bucket ID filter applied per project.
            branch_id: If set, target a specific dev branch
                (only valid with a single project).
            include_usage: When True, also report which configurations read or
                write each table (``used_by``). Costs one extra component
                listing per project -- not per table -- but that listing
                carries every configuration body, so it is the expensive call
                in a big project. Off by default.

        Returns:
            Dict with 'tables' list (each row tagged with ``project_alias``)
            and 'errors' list (per-project failures). With ``include_usage``
            every table row also carries a ``used_by`` list (empty when
            nothing references it, or when the usage scan itself failed).
        """
        projects = self.resolve_projects(aliases)

        def _worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]] | tuple[str, dict[str, Any]]:
            return self._fetch_tables(
                alias,
                project,
                bucket_id=bucket_id,
                branch_id=branch_id,
                include_usage=include_usage,
                usage_branch_id=branch_id,
            )

        successes, errors = self._run_parallel(projects, _worker)

        tables: list[dict[str, Any]] = []
        for alias, raw_tables, components in successes:
            usage = (
                collect_table_usage(components, [t.get("id", "") for t in raw_tables])
                if include_usage
                else None
            )
            tables.extend(normalize_table_rows(alias, raw_tables, usage))

        return {"tables": tables, "errors": errors}

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_bucket(
        self,
        alias: str,
        stage: str,
        name: str,
        description: str | None = None,
        backend: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new storage bucket.

        Args:
            alias: Project alias.
            stage: Bucket stage — must be "in" or "out".
            description: Optional bucket description.
            backend: Optional backend type.
            branch_id: If set, create bucket in a specific dev branch.

        Returns:
            Dict with created bucket details.

        Raises:
            ValueError: If stage is not "in" or "out".
        """
        stage = stage.lower()
        if stage not in ("in", "out"):
            raise ValueError(f"Invalid stage '{stage}'. Must be 'in' or 'out'.")

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            bucket = client.create_bucket(
                stage=stage,
                name=name,
                description=description,
                backend=backend,
                branch_id=branch_id,
            )
            legacy_branch_storage = _detect_legacy_branch_storage(client, branch_id)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "id": bucket.get("id", ""),
            "display_name": bucket.get("displayName", bucket.get("name", "")),
            "stage": bucket.get("stage", ""),
            "backend": bucket.get("backend", ""),
            "description": bucket.get("description", ""),
            "legacy_branch_storage": legacy_branch_storage,
        }

    def create_table(
        self,
        alias: str,
        bucket_id: str,
        name: str,
        columns: list[str] | None = None,
        primary_key: list[str] | None = None,
        branch_id: int | None = None,
        not_null_columns: list[str] | None = None,
        defaults: list[str] | None = None,
        if_not_exists: bool = False,
        source_table_id: str | None = None,
        source_branch_id: int | None = None,
        time_partitioning_type: str | None = None,
        time_partitioning_field: str | None = None,
        time_partitioning_expiration_ms: str | None = None,
        range_partitioning_field: str | None = None,
        range_partitioning_start: str | None = None,
        range_partitioning_end: str | None = None,
        range_partitioning_interval: str | None = None,
        clustering_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new table with typed columns, or by copying a source table.

        Column specs accept base Keboola types (STRING, INTEGER, NUMERIC,
        FLOAT, BOOLEAN, DATE, TIMESTAMP) *and* native backend types with
        optional length/precision. Examples: ``amount:NUMERIC(18,2)``,
        ``pk:VARCHAR(40)``, ``ts:TIMESTAMP_TZ``, ``meta:VARIANT``. The Keboola
        Storage API derives ``basetype`` automatically and returns precise
        errors for invalid type/length pairs per backend.

        Exactly one of ``columns`` or ``source_table_id`` must be given. With
        ``source_table_id`` (BigQuery only) the new table's schema is derived
        from the source table and its rows are copied into the requested
        partition/clustering layout -- the supported way to repartition a
        populated BigQuery table, then flip it into place with
        ``swap_tables``. The partition/clustering flags also apply to a normal
        ``columns`` create (BigQuery only).

        When ``branch_id`` targets a dev branch and the bucket has not been
        materialized there yet, this method auto-creates it (mirrors the
        official Go CLI's ``EnsureBucketExists`` pattern). The response
        surfaces this via ``auto_created_bucket``.

        Args:
            alias: Project alias.
            bucket_id: Target bucket ID (e.g. ``in.c-my-bucket``).
            name: Table name.
            columns: List of column specs -- ``name``, ``name:TYPE``, or
                ``name:TYPE(length)``. Forbidden together with
                ``source_table_id``.
            primary_key: Optional list of primary-key column names.
            branch_id: If set, create the table in this dev branch and
                auto-materialize the bucket when missing.
            not_null_columns: Column names to mark NOT NULL (``nullable=false``
                in the API definition). Not valid in source mode.
            defaults: ``NAME=VALUE`` assignments for DEFAULT expressions
                (e.g. ``is_admin=false``, ``amount=0``). Boolean defaults
                must be lowercase per Keboola API validation. Not valid in
                source mode.
            source_table_id: Storage table ID to copy from (BigQuery only).
            source_branch_id: Optional branch the source is resolved in.
            time_partitioning_type/field/expiration_ms: BigQuery time
                partitioning (``type`` required when any is set).
            range_partitioning_field/start/end/interval: BigQuery integer-range
                partitioning (all four required together). Mutually exclusive
                with time partitioning.
            clustering_fields: BigQuery clustering columns.

        Returns:
            Dict with table details and ``auto_created_bucket`` flag.
            ``action`` is ``"created"`` on a fresh create. When
            ``if_not_exists`` is set and the table already existed,
            ``action`` is ``"skipped"`` and ``columns`` / ``primary_key`` /
            ``name`` report the EXISTING table's actual schema (not the
            request); the caller's requested values are mirrored under
            ``requested_columns`` / ``requested_primary_key``, and
            ``schema_drift`` is ``True`` when the two diverge.

        Raises:
            ValueError: Malformed column spec or ``--default`` assignment;
                ``--not-null`` / ``--default`` references an unknown column;
                ``columns`` and ``source`` both/neither given;
                ``source_branch_id`` given without ``source_table_id``;
                incomplete or conflicting partitioning flags; or BigQuery-only
                features requested on a non-BigQuery backend.
        """
        not_null_set = set(not_null_columns or [])
        defaults_map = _parse_default_assignments(defaults)

        # --source-branch-id only qualifies a source table; on its own it would
        # be silently dropped (source mode never activates). Fail fast instead.
        if source_branch_id is not None and source_table_id is None:
            raise ValueError("--source-branch-id requires --source-table-id.")

        source = _build_source(source_table_id, source_branch_id)
        time_partitioning, range_partitioning, clustering = _build_bigquery_layout(
            time_partitioning_type,
            time_partitioning_field,
            time_partitioning_expiration_ms,
            range_partitioning_field,
            range_partitioning_start,
            range_partitioning_end,
            range_partitioning_interval,
            clustering_fields,
        )
        uses_bigquery_features = (
            source is not None
            or time_partitioning is not None
            or range_partitioning is not None
            or clustering is not None
        )
        has_columns = bool(columns)

        # Exactly one of columns / source. The Storage API forbids both and
        # requires at least one; fail fast with a clear message.
        if source is not None and has_columns:
            raise ValueError(
                "--column must not be combined with --source-table-id; in source "
                "mode the column definition is derived from the source table."
            )
        if source is None and not has_columns:
            raise ValueError(
                "create-table requires either --column (one or more) or "
                "--source-table-id (copy from an existing BigQuery table)."
            )

        # not-null / default attach to --column definitions; they have no
        # meaning when columns are derived from a source table.
        if source is not None and not_null_columns:
            raise ValueError(
                "--not-null is not valid with --source-table-id (columns are "
                "derived from the source table)."
            )
        if source is not None and defaults:
            raise ValueError(
                "--default is not valid with --source-table-id (columns are "
                "derived from the source table)."
            )

        parsed_columns = (
            [_parse_column_spec(col_spec, not_null_set, defaults_map) for col_spec in columns]
            if columns
            else []
        )

        # Reject attribute references to columns not actually defined. Without
        # this check a typo like `--not-null pk --column pkey:VARCHAR(40)`
        # silently has no effect -- failing fast surfaces the bug.
        col_names = {c["name"] for c in parsed_columns}
        unknown_not_null = sorted(not_null_set - col_names)
        if unknown_not_null:
            raise ValueError(
                f"--not-null references unknown column(s): {', '.join(unknown_not_null)}. "
                f"Defined columns: {', '.join(sorted(col_names))}."
            )
        unknown_defaults = sorted(set(defaults_map.keys()) - col_names)
        if unknown_defaults:
            raise ValueError(
                f"--default references unknown column(s): {', '.join(unknown_defaults)}. "
                f"Defined columns: {', '.join(sorted(col_names))}."
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        target_table_id = f"{bucket_id}.{name}"
        try:
            # BigQuery pre-flight guard: source copy + partition/clustering are
            # BigQuery-only. When any is requested, verify the project's backend
            # before issuing the create so a non-BigQuery project fails fast with
            # a clear message instead of a late driver-side 422.
            if uses_bigquery_features:
                backend = (client.verify_token().default_backend or "").lower()
                if backend != "bigquery":
                    raise ValueError(
                        f"Project backend is '{backend or 'unknown'}'; "
                        "--source-table-id and partition/clustering flags "
                        "require a BigQuery backend."
                    )

            auto_created_bucket = _ensure_bucket_exists_in_branch(client, bucket_id, branch_id)
            try:
                results = client.create_table(
                    bucket_id=bucket_id,
                    name=name,
                    columns=parsed_columns if columns else None,
                    primary_key=primary_key,
                    branch_id=branch_id,
                    source=source,
                    time_partitioning=time_partitioning,
                    range_partitioning=range_partitioning,
                    clustering=clustering,
                )
            except KeboolaApiError as exc:
                # IF-NOT-EXISTS: if the create failed because the table
                # already has the same display name AND the table at the
                # expected id resolves, treat as a successful skip. A
                # different table with the same display name still
                # surfaces the original error (the user has a real
                # conflict to resolve).
                if (
                    if_not_exists
                    and exc.error_code == ErrorCode.STORAGE_JOB_FAILED
                    and "already has the same display name" in (exc.message or "")
                ):
                    try:
                        existing = client.get_table_detail(target_table_id, branch_id=branch_id)
                    except KeboolaApiError:
                        existing = None
                    if existing is not None:
                        # Report the EXISTING table's actual schema, not the
                        # caller's request. A caller relying on the skipped
                        # envelope to discover the real shape must not be handed
                        # a re-echo of its own args (keboola/cli#349). The
                        # requested values are preserved under `requested_*` so
                        # the caller can still see the divergence, and
                        # `schema_drift` flags when the existing table differs.
                        requested_columns = [c["name"] for c in parsed_columns]
                        requested_primary_key = primary_key or []
                        actual_columns = existing.get("columns", [])
                        actual_primary_key = existing.get("primaryKey", [])
                        schema_drift = set(actual_columns) != set(requested_columns) or set(
                            actual_primary_key
                        ) != set(requested_primary_key)
                        return {
                            "project_alias": alias,
                            "table_id": target_table_id,
                            "name": existing.get("name", name),
                            "bucket_id": bucket_id,
                            "primary_key": actual_primary_key,
                            "columns": actual_columns,
                            "requested_primary_key": requested_primary_key,
                            "requested_columns": requested_columns,
                            "schema_drift": schema_drift,
                            "auto_created_bucket": auto_created_bucket,
                            "legacy_branch_storage": _detect_legacy_branch_storage(
                                client, branch_id
                            ),
                            "action": "skipped",
                            "skip_reason": "table already exists",
                            # Keep the JSON envelope shape identical to the
                            # "created" path; the existing table's layout is not
                            # re-derived here, so the source/layout keys are null.
                            "source_table_id": None,
                            "source_branch_id": None,
                            "time_partitioning": None,
                            "range_partitioning": None,
                            "clustering": None,
                        }
                raise
            legacy_branch_storage = _detect_legacy_branch_storage(client, branch_id)
        finally:
            client.close()

        # In columns mode the requested column names are authoritative. In source
        # mode the schema is derived from the source, so surface whatever columns
        # the completed create job reports (a list of names or column dicts).
        if columns:
            result_columns = [c["name"] for c in parsed_columns]
        else:
            raw_columns = results.get("columns") or []
            result_columns = [c["name"] if isinstance(c, dict) else c for c in raw_columns]

        return {
            "project_alias": alias,
            "table_id": results.get("id", target_table_id),
            "name": name,
            "bucket_id": bucket_id,
            "primary_key": primary_key or [],
            "columns": result_columns,
            "auto_created_bucket": auto_created_bucket,
            "legacy_branch_storage": legacy_branch_storage,
            "action": "created",
            "source_table_id": source_table_id,
            "source_branch_id": source_branch_id,
            "time_partitioning": time_partitioning,
            "range_partitioning": range_partitioning,
            "clustering": clustering,
        }

    def upload_table(
        self,
        alias: str,
        table_id: str,
        file_path: str,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        auto_create: bool = True,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a CSV file into a storage table.

        When auto_create is True (default), auto-creates the bucket and/or
        table if they don't exist. Columns are inferred as STRING from the CSV
        header row. Pass auto_create=False to require the table to exist.

        Args:
            alias: Project alias.
            table_id: Target table ID.
            file_path: Local path to the CSV file.
            incremental: Append rows (True) or full load (False).
            delimiter: CSV column delimiter.
            enclosure: CSV value enclosure character.
            auto_create: Auto-create bucket and table if missing.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with import results plus auto_created_bucket / auto_created_table flags.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        file_size_bytes = Path(file_path).stat().st_size

        auto_created_bucket = False
        auto_created_table = False

        client = self._client_factory(project.stack_url, project.token)
        try:
            if auto_create:
                parts = table_id.split(".")
                if len(parts) == 3:
                    stage, bucket_slug, table_name = parts
                    bucket_id = f"{stage}.{bucket_slug}"
                    bucket_name = bucket_slug.removeprefix("c-")

                    # Ensure bucket exists
                    try:
                        client.get_bucket_detail(bucket_id, branch_id=branch_id)
                    except KeboolaApiError as exc:
                        if exc.status_code == 404:
                            client.create_bucket(
                                stage=stage,
                                name=bucket_name,
                                branch_id=branch_id,
                            )
                            auto_created_bucket = True
                            logger.info("Auto-created bucket %s", bucket_id)
                        else:
                            raise

                    # Ensure table exists
                    existing = client.list_tables(
                        bucket_id=bucket_id,
                        branch_id=branch_id,
                    )
                    if not any(t.get("name") == table_name for t in existing):
                        columns = _read_csv_header(file_path, delimiter=delimiter)
                        client.create_table(
                            bucket_id=bucket_id,
                            name=table_name,
                            columns=[
                                {"name": col, "definition": {"type": "STRING"}} for col in columns
                            ],
                            primary_key=None,
                            branch_id=branch_id,
                        )
                        auto_created_table = True
                        logger.info("Auto-created table %s (%d columns)", table_id, len(columns))

            results = client.upload_table(
                table_id=table_id,
                file_path=file_path,
                incremental=incremental,
                delimiter=delimiter,
                enclosure=enclosure,
                branch_id=branch_id,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "table_id": table_id,
            "incremental": incremental,
            "file_size_bytes": file_size_bytes,
            "imported_rows": results.get("importedRowsCount"),
            "warnings": results.get("warnings", []),
            "auto_created_bucket": auto_created_bucket,
            "auto_created_table": auto_created_table,
        }

    # ------------------------------------------------------------------
    # Download / export operations
    # ------------------------------------------------------------------

    def download_table(
        self,
        alias: str,
        table_id: str,
        output_path: str | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        branch_id: int | None = None,
        keep_slices: bool = False,
        *,
        where_column: str | None = None,
        where_operator: str = "eq",
        where_values: list[str] | None = None,
        changed_since: str | None = None,
        changed_until: str | None = None,
    ) -> dict[str, Any]:
        """Export a storage table to a local CSV file.

        Optional ``where_*`` / ``changed_*`` arguments filter the exported rows
        server-side (forwarded verbatim to ``export_table_async``).

        Uses the async export flow: export-async -> poll job -> get file
        info -> download from cloud URL. Handles gzip decompression
        transparently.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            output_path: Local file path (default mode) or directory
                (``keep_slices=True``) to write to. Defaults to
                ``<table>.csv`` / ``<alias>/<table_id>.csv/`` respectively.
            columns: Optional list of column names to export.
            limit: Optional max number of rows to export.
            branch_id: If set, target a specific dev branch.
            keep_slices: If True, write each slice as its own file inside
                the output directory and preserve the manifest. The CSV
                header is written to a sidecar ``_columns.csv`` rather than
                prepended to any slice so no slice has to be rewritten
                end-to-end (which would defeat the memory story). For most
                analytical tools the slices themselves are header-less
                parts of a single logical CSV; headers come from catalog
                metadata, not the slice bodies.

        Returns:
            Dict with export metadata. With ``keep_slices`` the result also
            contains ``slice_count`` and ``slices`` (list of {path, size}).
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        table_name = table_id.rsplit(".", 1)[-1] if "." in table_id else table_id
        if not output_path:
            output_path = f"{alias}/{table_id}.csv" if keep_slices else f"{table_name}.csv"

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Step 0: Get table columns for CSV header
            table_detail = client.list_tables(include="columns", branch_id=branch_id)
            table_columns = columns  # Use explicit columns if specified
            if not table_columns:
                for t in table_detail:
                    if t.get("id") == table_id:
                        table_columns = t.get("columns", [])
                        break

            # Step 1: Start async export and wait for completion
            job = client.export_table_async(
                table_id=table_id,
                columns=columns,
                limit=limit,
                branch_id=branch_id,
                where_column=where_column,
                where_operator=where_operator,
                where_values=where_values,
                changed_since=changed_since,
                changed_until=changed_until,
            )

            # Step 2: Get file info from job results
            file_info = job.get("results", {}).get("file", {})
            file_id = file_info.get("id")
            if not file_id:
                raise KeboolaApiError(
                    message="Export job completed but no file ID in results",
                    status_code=500,
                    error_code=ErrorCode.EXPORT_NO_FILE,
                    retryable=False,
                )

            # Step 3: Get download URL (branch-scoped if exporting from dev branch)
            file_detail = client.get_file_info(file_id, branch_id=branch_id)
            download_url = file_detail.get("url")
            if not download_url:
                raise KeboolaApiError(
                    message=f"No download URL for file {file_id}",
                    status_code=500,
                    error_code=ErrorCode.EXPORT_NO_URL,
                    retryable=False,
                )

            # Step 4a: Sliced-directory mode -- each slice is its own file.
            # This is the OOM-safe-by-default path for analytical workflows
            # (DuckDB, polars, Spark read a directory natively).
            if keep_slices:
                if not file_detail.get("isSliced"):
                    raise KeboolaApiError(
                        message=(
                            "--keep-slices is only meaningful for tables that "
                            "Storage exports as multiple slices; this export "
                            "produced a single file. Re-run without --keep-slices."
                        ),
                        status_code=400,
                        error_code=ErrorCode.NOT_SLICED,
                        retryable=False,
                    )
                slice_info = client.download_sliced_file_to_dir(file_detail, output_path)
                # Sidecar with the column order so downstream readers can
                # reconstruct the header without hitting Storage.
                _write_columns_sidecar(slice_info["output_dir"], table_columns or [])
                return {
                    "project_alias": alias,
                    "table_id": table_id,
                    "output_path": slice_info["output_dir"],
                    "file_size_bytes": slice_info["total_bytes"],
                    "columns": table_columns or [],
                    "limit": limit,
                    "keep_slices": True,
                    "slice_count": slice_info["slice_count"],
                    "slices": slice_info["slices"],
                }

            # Step 4b: Default mode -- concat into a single CSV file.
            if file_detail.get("isSliced"):
                bytes_written = client.download_sliced_file(file_detail, output_path)
            else:
                bytes_written = client.download_file(download_url, output_path)

            # Step 5: Prepend CSV header row
            if table_columns:
                _prepend_csv_header(output_path, table_columns)
                # Recalculate size after adding header
                bytes_written = Path(output_path).stat().st_size
        finally:
            client.close()

        return {
            "project_alias": alias,
            "table_id": table_id,
            "output_path": str(Path(output_path).resolve()),
            "file_size_bytes": bytes_written,
            "columns": table_columns or [],
            "limit": limit,
            "keep_slices": False,
        }

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete_tables(
        self,
        alias: str,
        table_ids: list[str],
        dry_run: bool = False,
        force: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete one or more storage tables.

        Batch-tolerant: accumulates errors per table, one failure does not
        stop other deletes.

        Args:
            alias: Project alias.
            table_ids: List of table IDs to delete.
            dry_run: If True, only report what would be deleted.
            force: If True, cascade-delete tables and all their aliases.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with 'deleted', 'failed', 'dry_run', 'project_alias',
            and optionally 'would_delete'.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "deleted": [],
                "failed": [],
                "would_delete": list(table_ids),
                "dry_run": True,
                "project_alias": alias,
            }

        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for tid in table_ids:
                try:
                    client.delete_table(tid, branch_id=branch_id, force=force)
                    deleted.append(tid)
                except KeboolaApiError as exc:
                    failed.append({"id": tid, "error": exc.message})
        finally:
            client.close()

        return {
            "deleted": deleted,
            "failed": failed,
            "dry_run": False,
            "project_alias": alias,
        }

    def truncate_tables(
        self,
        alias: str,
        table_ids: list[str],
        dry_run: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Truncate one or more storage tables (delete all rows; preserve schema).

        Batch-tolerant: per-table errors accumulate; one missing table does
        not abort the batch. ``rows_before`` is captured from
        ``get_table_detail`` so callers can confirm the operation had a
        non-trivial effect. The table definition (columns, types, primary
        key, descriptions, sharing edges, dependents) is preserved.

        Args:
            alias: Project alias.
            table_ids: List of table IDs to truncate.
            dry_run: If True, capture rows_before but do NOT truncate.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with 'truncated', 'failed', 'dry_run', 'project_alias',
            and (when dry_run) 'would_truncate'. Each ``truncated[]`` entry
            carries ``{table_id, rows_before, rows_after, branch_id}``.
            The Storage API truncate endpoint is uniformly async-via-job
            on every branch (verified live 2026-05-11 on
            connection.europe-west3.gcp.keboola.com); the client polls
            the queued job to completion before returning, so
            ``rows_after`` is always 0 on success.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        truncated: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        would_truncate: list[dict[str, Any]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for tid in table_ids:
                try:
                    table = client.get_table_detail(tid, branch_id=branch_id)
                except KeboolaApiError as exc:
                    failed.append({"id": tid, "error": exc.message})
                    continue

                # rowsCount is a Storage API integer field but the API does
                # not guarantee it is always present or coercible (legacy
                # tables, alias views over recently-truncated sources).
                # Treat any non-int as 0 rather than letting ValueError tear
                # down the whole batch.
                raw_rows = table.get("rowsCount")
                try:
                    rows_before = int(raw_rows) if raw_rows is not None else 0
                except (ValueError, TypeError):
                    rows_before = 0

                if dry_run:
                    would_truncate.append(
                        {
                            "table_id": tid,
                            "rows_before": rows_before,
                            "branch_id": branch_id,
                        }
                    )
                    continue

                try:
                    client.truncate_table(tid, branch_id=branch_id)
                except KeboolaApiError as exc:
                    failed.append({"id": tid, "error": exc.message})
                    continue

                truncated.append(
                    {
                        "table_id": tid,
                        "rows_before": rows_before,
                        "rows_after": 0,
                        "branch_id": branch_id,
                    }
                )
        finally:
            client.close()

        result: dict[str, Any] = {
            "truncated": truncated,
            "failed": failed,
            "dry_run": dry_run,
            "project_alias": alias,
        }
        if dry_run:
            result["would_truncate"] = would_truncate
        return result

    def add_column(
        self,
        alias: str,
        table_id: str,
        column: str,
        not_null: bool = False,
        default: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Add a single column to an existing table (synchronous).

        Parses the ``name:TYPE(length)`` column spec (the same grammar as
        ``storage create-table --column``) into a Storage API column definition
        and POSTs it to the synchronous add-column endpoint.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            column: Column spec, e.g. ``status:VARCHAR(20)`` or a bare ``notes``
                (implicit STRING).
            not_null: If True, the new column is NOT NULL (the backend rejects
                this unless the table is empty or a default is supplied).
            default: Optional default value for the new column.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with the added column name, its definition, table_id, and alias.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        col_name = column.split(":", 1)[0].strip()
        not_null_set = {col_name} if not_null else set()
        defaults = {col_name: default} if default is not None else {}
        parsed = _parse_column_spec(column, not_null_set, defaults)

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.add_column(
                table_id,
                name=parsed["name"],
                definition=parsed["definition"],
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "table_id": table_id,
            "column": parsed["name"],
            "definition": parsed["definition"],
            "project_alias": alias,
        }

    def delete_columns(
        self,
        alias: str,
        table_id: str,
        columns: list[str],
        dry_run: bool = False,
        force: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete one or more columns from a storage table.

        Batch-tolerant: accumulates errors per column, one failure does not
        stop other deletes. Each delete is async and waits for completion.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            columns: List of column names to delete.
            dry_run: If True, only report what would be deleted.
            force: If True, also delete from aliased tables.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with 'deleted', 'failed', 'dry_run', 'project_alias',
            'table_id', and optionally 'would_delete'.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "deleted": [],
                "failed": [],
                "would_delete": list(columns),
                "dry_run": True,
                "project_alias": alias,
                "table_id": table_id,
            }

        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for col in columns:
                try:
                    client.delete_column(table_id, col, branch_id=branch_id, force=force)
                    deleted.append(col)
                except KeboolaApiError as exc:
                    failed.append({"column": col, "error": exc.message})
        finally:
            client.close()

        return {
            "deleted": deleted,
            "failed": failed,
            "dry_run": False,
            "project_alias": alias,
            "table_id": table_id,
        }

    def swap_tables(
        self,
        alias: str,
        table_id: str,
        target_table_id: str,
        branch_id: int | None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Swap two storage tables (branch-scoped; branch_id mandatory).

        After the swap, the two tables exchange physical positions. Aliases
        are NOT transferred -- they keep pointing at the same physical
        position and therefore expose the OTHER table's data after the swap.
        This is the documented behavior of the Storage API; the service
        layer does not try to rewrite alias targets.

        ``branch_id`` is mandatory and the service raises ConfigError before
        any HTTP call when it is None. Any branch is accepted, INCLUDING the
        default/production branch -- a default-branch swap is the supported
        way to retype a production table (dev-branch merge does not propagate
        storage schema, so a swap done in a dev branch never reaches prod).

        Args:
            alias: Project alias.
            table_id: Full ID of the first table.
            target_table_id: Full ID of the second table.
            branch_id: Branch ID (must not be None; any branch accepted, including the default/production branch).
            dry_run: If True, only report what would be swapped.

        Returns:
            Dict with 'project_alias', 'branch_id', 'table_id',
            'target_table_id', 'dry_run', and (when not dry-run) 'response'.

        Raises:
            ConfigError: If branch_id is None.
            KeboolaApiError: If the API call fails.
        """
        if branch_id is None:
            raise ConfigError(
                "swap-tables requires a branch. Set one with "
                "'kbagent branch use --project <P> --branch <ID>' or pass "
                "--branch <ID> directly. Any branch works, including the "
                "default/production branch."
            )

        if table_id == target_table_id:
            raise ConfigError(
                "swap-tables requires two different tables; "
                f"--table-id and --target-table-id are both '{table_id}'."
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "project_alias": alias,
                "branch_id": branch_id,
                "table_id": table_id,
                "target_table_id": target_table_id,
                "dry_run": True,
            }

        client = self._client_factory(project.stack_url, project.token)
        try:
            response = client.swap_tables(
                table_id=table_id,
                target_table_id=target_table_id,
                branch_id=branch_id,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "table_id": table_id,
            "target_table_id": target_table_id,
            "dry_run": False,
            "response": response,
        }

    def clone_table(
        self,
        alias: str,
        table_id: str,
        branch_id: int | None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pull (clone) a production table into a dev branch (branch required).

        On ``storage-branches`` projects a dev branch reads production tables
        transparently until the first write, so mutating a table's schema in
        the branch (e.g. ``swap_tables`` or a column drop) first needs a
        branch-local copy of the production table. This materializes that copy
        from the default branch. The pull is one-way (default -> branch); the
        service raises ConfigError before any HTTP call when ``branch_id`` is
        None.

        Args:
            alias: Project alias.
            table_id: Full ID of the table to pull into the branch.
            branch_id: Target dev branch ID (must not be None).
            dry_run: If True, only report what would be pulled.

        Returns:
            Dict with 'project_alias', 'branch_id', 'table_id', 'dry_run',
            and (when not dry-run) 'response'.

        Raises:
            ConfigError: If branch_id is None.
            KeboolaApiError: If the API call fails.
        """
        if branch_id is None:
            raise ConfigError(
                "clone-table requires a dev branch. Set one with "
                "'kbagent branch use --project <P> --branch <ID>' or pass "
                "--branch <ID> directly. The pull is one-way: default -> branch."
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "project_alias": alias,
                "branch_id": branch_id,
                "table_id": table_id,
                "dry_run": True,
            }

        client = self._client_factory(project.stack_url, project.token)
        try:
            response = client.pull_table(
                table_id=table_id,
                branch_id=branch_id,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "branch_id": branch_id,
            "table_id": table_id,
            "dry_run": False,
            "response": response,
        }

    def delete_buckets(
        self,
        alias: str,
        bucket_ids: list[str],
        force: bool = False,
        dry_run: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete one or more storage buckets.

        Protections:
        - Linked buckets (sourceBucket set) are blocked with a helpful message.
        - Shared buckets (sharing field set) are blocked unless --force is used.
        - Without force, non-empty buckets fail at the API level.

        Batch-tolerant: accumulates errors per bucket.

        Args:
            alias: Project alias.
            bucket_ids: List of bucket IDs to delete.
            force: Force delete even if bucket has tables or is shared.
            dry_run: If True, only report what would be deleted.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with 'deleted', 'failed', 'dry_run', 'project_alias',
            and optionally 'would_delete'.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        would_delete: list[str] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for bid in bucket_ids:
                # Check bucket metadata for linked/shared protections
                try:
                    bucket = client.get_bucket_detail(bid, branch_id=branch_id)
                except KeboolaApiError as exc:
                    failed.append({"id": bid, "error": exc.message})
                    continue

                # Linked bucket protection
                if bucket.get("sourceBucket"):
                    failed.append(
                        {
                            "id": bid,
                            "error": (
                                f"Bucket '{bid}' is a linked bucket. "
                                "Use 'kbagent sharing unlink' to remove it."
                            ),
                        }
                    )
                    continue

                # Shared bucket protection (unless force)
                if bucket.get("sharing") and not force:
                    failed.append(
                        {
                            "id": bid,
                            "error": (
                                f"Bucket '{bid}' is shared to other projects. "
                                "Use --force to delete anyway, or 'kbagent sharing unshare' first."
                            ),
                        }
                    )
                    continue

                if dry_run:
                    would_delete.append(bid)
                    continue

                try:
                    client.delete_bucket(bid, force=force, branch_id=branch_id)
                    deleted.append(bid)
                except KeboolaApiError as exc:
                    failed.append({"id": bid, "error": exc.message})
        finally:
            client.close()

        result: dict[str, Any] = {
            "deleted": deleted,
            "failed": failed,
            "dry_run": dry_run,
            "project_alias": alias,
        }
        if dry_run:
            result["would_delete"] = would_delete
        return result

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def list_files(
        self,
        alias: str,
        limit: int = 20,
        offset: int = 0,
        tags: list[str] | None = None,
        since_id: int | None = None,
        query: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List Storage Files from a project.

        Args:
            alias: Project alias.
            limit: Max number of files.
            offset: Pagination offset.
            tags: Filter by tags (AND logic).
            since_id: Return only files newer than this ID.
            query: Full-text search on file name.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias and list of files.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            files = client.list_files(
                limit=limit,
                offset=offset,
                tags=tags,
                since_id=since_id,
                query=query,
                branch_id=branch_id,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "files": files,
            "count": len(files),
        }

    def upload_file(
        self,
        alias: str,
        file_path: str,
        name: str | None = None,
        tags: list[str] | None = None,
        is_permanent: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to Storage Files.

        Args:
            alias: Project alias.
            file_path: Local path to the file.
            name: Custom filename (defaults to local basename).
            tags: Optional list of tags.
            is_permanent: If True, file is not auto-deleted.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with file metadata.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        file_size_bytes = Path(file_path).stat().st_size

        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.upload_file(
                file_path=file_path,
                name=name,
                tags=tags,
                is_permanent=is_permanent,
                branch_id=branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["file_size_bytes"] = file_size_bytes
        return result

    def get_file_info(
        self,
        alias: str,
        file_id: int,
    ) -> dict[str, Any]:
        """Get Storage File metadata.

        Args:
            alias: Project alias.
            file_id: Storage file ID.

        Returns:
            File resource dict.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            file_info = client.get_file_info(file_id)
        finally:
            client.close()

        file_info["project_alias"] = alias
        return file_info

    def download_file(
        self,
        alias: str,
        file_id: int | None = None,
        tags: list[str] | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Download a Storage File to local disk.

        Supports download by file ID or by tags (downloads latest matching file).
        Handles both sliced and non-sliced files.

        Args:
            alias: Project alias.
            file_id: Storage file ID (mutually exclusive with tags).
            tags: Download latest file matching these tags.
            output_path: Local output path (defaults to file's name).

        Returns:
            Dict with download metadata.
        """
        from ..errors import KeboolaApiError

        if not file_id and not tags:
            raise ValueError("Either --file-id or --tag must be provided")

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Resolve file ID from tags if needed
            if not file_id:
                files = client.list_files(limit=1, tags=tags)
                if not files:
                    tag_str = ", ".join(tags or [])
                    raise KeboolaApiError(
                        message=f"No files found matching tags: {tag_str}",
                        status_code=404,
                        error_code=ErrorCode.FILE_NOT_FOUND,
                        retryable=False,
                    )
                file_id = files[0]["id"]

            file_detail = client.get_file_info(file_id)
            file_name = file_detail.get("name", f"file_{file_id}")

            # Auto-detect parquet: sliced + filename ends in .parquet. Such
            # files must be saved as individual slices, not concatenated --
            # each parquet slice has its own footer and concatenation produces
            # an invalid file.
            is_sliced = bool(file_detail.get("isSliced"))
            is_parquet = is_sliced and file_name.endswith(".parquet")

            if is_parquet:
                # --output is the user's own choice (trusted); without it the
                # slice dir is derived from the API-controlled name, so contain
                # it under CWD to block path traversal.
                if output_path:
                    effective_output = output_path
                else:
                    effective_output = str(_safe_download_target(Path.cwd(), f"{file_name}.d"))
                slice_info = client.download_sliced_file_to_dir(file_detail, effective_output)
                result: dict[str, Any] = {
                    "project_alias": alias,
                    "file_id": file_id,
                    "file_name": file_name,
                    "output_path": slice_info["output_dir"],
                    "file_size_bytes": slice_info["total_bytes"],
                    "is_sliced": True,
                    "slice_count": slice_info["slice_count"],
                    "slices": slice_info["slices"],
                }
                return result

            if output_path and Path(output_path).is_dir():
                # Caller passed a directory (e.g. the REST file-download endpoint);
                # save inside it under the file's own name. The name comes from the
                # API, so contain it under the directory to block path traversal.
                effective_output = str(_safe_download_target(Path(output_path), file_name))
            elif output_path:
                # Explicit --output file path: the user's own choice (trusted).
                effective_output = output_path
            else:
                # No --output: the API-controlled name becomes the path; contain
                # it under CWD so a malicious name (../../, absolute) cannot escape.
                effective_output = str(_safe_download_target(Path.cwd(), file_name))
            if is_sliced:
                bytes_written = client.download_sliced_file(file_detail, effective_output)
            else:
                download_url = file_detail.get("url")
                if not download_url:
                    raise KeboolaApiError(
                        message=f"No download URL for file {file_id}",
                        status_code=500,
                        error_code=ErrorCode.FILE_NO_URL,
                        retryable=False,
                    )
                bytes_written = client.download_file(download_url, effective_output)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "file_id": file_id,
            "file_name": file_name,
            "output_path": str(Path(effective_output).resolve()),
            "file_size_bytes": bytes_written,
            "is_sliced": is_sliced,
        }

    def delete_files(
        self,
        alias: str,
        file_ids: list[int],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one or more Storage Files.

        Batch-tolerant: accumulates errors per file.

        Args:
            alias: Project alias.
            file_ids: List of file IDs to delete.
            dry_run: If True, only report what would be deleted.

        Returns:
            Dict with deleted, failed, dry_run lists.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        deleted: list[int] = []
        failed: list[dict[str, Any]] = []
        would_delete: list[int] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for fid in file_ids:
                if dry_run:
                    would_delete.append(fid)
                    continue
                try:
                    client.delete_file(fid)
                    deleted.append(fid)
                except KeboolaApiError as exc:
                    failed.append({"id": fid, "error": exc.message})
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

    def tag_file(
        self,
        alias: str,
        file_id: int,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add and/or remove tags on a Storage File.

        Args:
            alias: Project alias.
            file_id: Storage file ID.
            add_tags: Tags to add.
            remove_tags: Tags to remove.

        Returns:
            Dict with operation results.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        added: list[str] = []
        removed: list[str] = []
        errors: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for tag in add_tags or []:
                try:
                    client.tag_file(file_id, tag)
                    added.append(tag)
                except KeboolaApiError as exc:
                    errors.append({"tag": tag, "action": "add", "error": exc.message})

            for tag in remove_tags or []:
                try:
                    client.untag_file(file_id, tag)
                    removed.append(tag)
                except KeboolaApiError as exc:
                    errors.append({"tag": tag, "action": "remove", "error": exc.message})
        finally:
            client.close()

        return {
            "project_alias": alias,
            "file_id": file_id,
            "added": added,
            "removed": removed,
            "errors": errors,
        }

    def load_file_to_table(
        self,
        alias: str,
        file_id: int,
        table_id: str,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Load an existing Storage File into a table.

        Triggers import-async with dataFileId. Useful for importing files
        that are already in Storage (uploaded by components or file-upload).

        Args:
            alias: Project alias.
            file_id: Storage file ID to import.
            table_id: Target table ID.
            incremental: Append rows (True) or full load (False).
            delimiter: CSV column delimiter.
            enclosure: CSV value enclosure character.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with import results.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            job = client.import_table_async(
                table_id=table_id,
                file_id=file_id,
                incremental=incremental,
                delimiter=delimiter,
                enclosure=enclosure,
                branch_id=branch_id,
            )
        finally:
            client.close()

        results = job.get("results", {})
        return {
            "project_alias": alias,
            "file_id": file_id,
            "table_id": table_id,
            "incremental": incremental,
            "imported_rows": results.get("importedRowsCount"),
            "warnings": results.get("warnings", []),
        }

    def unload_table_to_file(
        self,
        alias: str,
        table_id: str,
        columns: list[str] | None = None,
        limit: int | None = None,
        tags: list[str] | None = None,
        download: bool = False,
        output_path: str | None = None,
        branch_id: int | None = None,
        file_type: str = "csv",
        keep_slices: bool = False,
    ) -> dict[str, Any]:
        """Export a table to a Storage File.

        Creates a file in Storage that can be downloaded or used by other
        components. Optionally tags the output file and downloads it locally.

        Args:
            alias: Project alias.
            table_id: Table ID to export.
            columns: Optional list of column names.
            limit: Optional max rows.
            tags: Tags to apply to the exported file.
            download: If True, also download the file locally (CSV only for now).
            output_path: Local output path (only used when download=True).
            branch_id: If set, target a specific dev branch.
            file_type: "csv" (default) or "parquet". Parquet output is sliced;
                use the returned file_id with 'kbagent storage file-detail' to
                work with the slices directly. Parquet + download is not
                supported yet (slices cannot be concatenated into a single
                valid parquet file).

        Returns:
            Dict with export metadata and file info.
        """
        from ..errors import KeboolaApiError

        if file_type not in ("csv", "parquet"):
            raise KeboolaApiError(
                message=f"file_type must be 'csv' or 'parquet', got {file_type!r}",
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Step 1: Export table async
            job = client.export_table_async(
                table_id=table_id,
                columns=columns,
                limit=limit,
                branch_id=branch_id,
                file_type=file_type,
            )

            # Step 2: Get file ID from job results
            file_info = job.get("results", {}).get("file", {})
            file_id = file_info.get("id")
            if not file_id:
                raise KeboolaApiError(
                    message="Export job completed but no file ID in results",
                    status_code=500,
                    error_code=ErrorCode.EXPORT_NO_FILE,
                    retryable=False,
                )

            # Step 3: Tag the exported file (branch-scoped if on dev branch)
            for tag in tags or []:
                client.tag_file(file_id, tag, branch_id=branch_id)

            # Step 4: Get full file detail (branch-scoped if on dev branch)
            file_detail = client.get_file_info(file_id, branch_id=branch_id)

            result: dict[str, Any] = {
                "project_alias": alias,
                "table_id": table_id,
                "file_id": file_id,
                "file_name": file_detail.get("name"),
                "file_size_bytes": file_detail.get("sizeBytes"),
                "is_sliced": file_detail.get("isSliced", False),
                "tags": file_detail.get("tags", []),
                "file_type": file_type,
            }

            # Step 5: Download if requested
            if download:
                table_short = table_id.rsplit(".", 1)[-1]

                if file_type == "parquet":
                    # Parquet slices cannot be concatenated -- save each as its
                    # own file in a directory (manifest.json is also preserved).
                    # Default layout mirrors Keboola's project+table addressing:
                    #   ./{project_alias}/{table_id}.parquet/
                    # which stays unambiguous across multiple exports and reads
                    # natively as a Parquet dataset in pyarrow/Spark/DuckDB.
                    effective_output = output_path or f"{alias}/{table_id}.parquet"
                    slice_info = client.download_sliced_file_to_dir(file_detail, effective_output)
                    result["downloaded"] = True
                    result["output_path"] = slice_info["output_dir"]
                    result["downloaded_bytes"] = slice_info["total_bytes"]
                    result["slice_count"] = slice_info["slice_count"]
                    result["slices"] = slice_info["slices"]
                elif keep_slices and file_detail.get("isSliced"):
                    # Preserve slices as a directory (parallel to parquet layout)
                    effective_output = output_path or f"{alias}/{table_id}.csv"
                    slice_info = client.download_sliced_file_to_dir(file_detail, effective_output)
                    result["downloaded"] = True
                    result["output_path"] = slice_info["output_dir"]
                    result["downloaded_bytes"] = slice_info["total_bytes"]
                    result["slice_count"] = slice_info["slice_count"]
                    result["slices"] = slice_info["slices"]
                    result["keep_slices"] = True
                else:
                    if keep_slices:
                        raise KeboolaApiError(
                            message=(
                                "--keep-slices requires a sliced export; this "
                                "file is a single non-sliced CSV. Drop the flag."
                            ),
                            status_code=400,
                            error_code=ErrorCode.NOT_SLICED,
                            retryable=False,
                        )
                    effective_output = output_path or f"{table_short}.csv"

                    if file_detail.get("isSliced"):
                        bytes_written = client.download_sliced_file(file_detail, effective_output)
                    else:
                        download_url = file_detail.get("url")
                        if not download_url:
                            raise KeboolaApiError(
                                message=f"No download URL for file {file_id}",
                                status_code=500,
                                error_code=ErrorCode.FILE_NO_URL,
                                retryable=False,
                            )
                        bytes_written = client.download_file(download_url, effective_output)

                    result["downloaded"] = True
                    result["output_path"] = str(Path(effective_output).resolve())
                    result["downloaded_bytes"] = bytes_written
            else:
                result["downloaded"] = False
        finally:
            client.close()

        return result

    # ------------------------------------------------------------------
    # Describe (metadata write) methods
    # ------------------------------------------------------------------

    def describe_bucket(
        self,
        alias: str,
        bucket_id: str,
        description: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set the KBC.description metadata on a storage bucket.

        Idempotent upsert: re-running with a different value overwrites the
        existing entry (Keboola metadata POST is upsert-by-key).

        Args:
            alias: Project alias.
            bucket_id: Bucket ID (e.g. 'in.c-my-bucket').
            description: Human-readable description text.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, bucket_id, description, result, message.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.set_bucket_metadata(
                bucket_id=bucket_id,
                entries=[("KBC.description", description)],
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "bucket_id": bucket_id,
            "description": description,
            "result": result,
            "message": f"Description set on bucket '{bucket_id}' in project '{alias}'.",
        }

    def describe_table(
        self,
        alias: str,
        table_id: str,
        description: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set the KBC.description metadata on a storage table.

        Idempotent upsert: re-running with a different value overwrites.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. 'in.c-bucket.table').
            description: Human-readable description text.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, table_id, description, result, message.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.set_table_metadata(
                table_id=table_id,
                entries=[("KBC.description", description)],
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "table_id": table_id,
            "description": description,
            "result": result,
            "message": f"Description set on table '{table_id}' in project '{alias}'.",
        }

    def describe_batch(
        self,
        alias: str,
        from_file: Path,
        branch_id: int | None = None,
        progress_callback: Callable[[str, str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Apply bucket, table, and column descriptions from a YAML file.

        YAML schema::

            buckets:
              in.c-my-bucket: "Bucket description"
            tables:
              in.c-my-bucket.my-table: "Table description"
            columns:
              in.c-my-bucket.my-table:
                col1: "Column 1 description"
                col2: "Column 2 description"

        All sections are optional: absent, ``None`` (a bare ``buckets:`` key)
        and empty (``[]``, ``''``, ``{}``) sections are silently skipped.
        Within each section the operations are applied in order.

        The document's shape is validated up front (see
        ``_describe_batch_input``): a section with a wrong NON-EMPTY shape, a
        null description, or two keys colliding after string coercion raises
        ``ValueError`` before the first write, so the file is rejected whole
        rather than half-applied.

        Once application starts, a failure in one item does not skip the
        remaining items — all per-item *API* results (success and error) are
        collected and returned.

        Args:
            alias: Project alias.
            from_file: Path to a YAML file with the schema above.
            branch_id: If set, target a specific dev branch.
            progress_callback: Optional ``(obj_type, obj_id, current, total)``
                callable invoked **before** each item is processed. ``obj_type``
                is ``"bucket"``, ``"table"``, or ``"columns"``; ``current`` is
                1-based; ``total`` is the total number of items across all
                sections. Used by the CLI to render a Rich progress indicator
                in human mode; JSON mode leaves it unset.

        Returns:
            Dict with project_alias, applied, errors, applied_count, error_count.
        """
        from ..errors import KeboolaApiError
        from ._describe_batch_input import parse_describe_batch_file

        parsed = parse_describe_batch_file(from_file)

        applied: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        total = parsed.total
        current = 0

        for bucket_id, desc in parsed.buckets.items():
            current += 1
            if progress_callback is not None:
                progress_callback("bucket", bucket_id, current, total)
            try:
                self.describe_bucket(alias, bucket_id, desc, branch_id=branch_id)
                applied.append({"type": "bucket", "id": bucket_id, "description": desc})
                logger.debug("describe_batch bucket %s: ok", bucket_id)
            except Exception as exc:
                msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
                errors.append({"type": "bucket", "id": bucket_id, "error": msg})

        for table_id, desc in parsed.tables.items():
            current += 1
            if progress_callback is not None:
                progress_callback("table", table_id, current, total)
            try:
                self.describe_table(alias, table_id, desc, branch_id=branch_id)
                applied.append({"type": "table", "id": table_id, "description": desc})
            except Exception as exc:
                msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
                errors.append({"type": "table", "id": table_id, "error": msg})

        for table_id, col_map in parsed.columns.items():
            current += 1
            if progress_callback is not None:
                progress_callback("columns", table_id, current, total)
            try:
                self.describe_columns(alias, table_id, col_map, branch_id=branch_id)
                applied.append({"type": "columns", "id": table_id, "columns": col_map})
            except Exception as exc:
                msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
                errors.append({"type": "columns", "id": table_id, "error": msg})

        return {
            "project_alias": alias,
            "applied": applied,
            "errors": errors,
            "applied_count": len(applied),
            "error_count": len(errors),
        }

    # ------------------------------------------------------------------
    # Parallel workers
    # ------------------------------------------------------------------

    def _fetch_buckets(
        self,
        alias: str,
        project: ProjectConfig,
        branch_id: int | None = None,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, Any]]:
        """Fetch buckets for a single project (worker for _run_parallel).

        Returns a 3-tuple on success (alias, buckets, True) or a 2-tuple
        (alias, error_dict) on failure, matching the _run_parallel protocol.
        """
        from ..errors import KeboolaApiError

        client = self._client_factory(project.stack_url, project.token)
        try:
            buckets = client.list_buckets(include="linkedBuckets", branch_id=branch_id)
            return (alias, buckets, True)
        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        finally:
            client.close()

    def _fetch_tables(
        self,
        alias: str,
        project: ProjectConfig,
        bucket_id: str | None = None,
        branch_id: int | None = None,
        include_usage: bool = False,
        usage_branch_id: int | None = None,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]] | tuple[str, dict[str, Any]]:
        """Fetch tables for a single project (worker for _run_parallel).

        Per-project failures (e.g. bucket not found in this project, invalid
        token) are returned as error tuples so other projects still complete.
        Returns a 3-tuple on success (alias, tables, components) or a 2-tuple
        (alias, error_dict) on failure, matching the _run_parallel protocol,
        which tells the two apart by tuple LENGTH -- so the third slot can
        carry the component listing rather than a success sentinel. It is
        empty unless ``include_usage`` asked for a usage scan.
        """
        from ..errors import KeboolaApiError

        client = self._client_factory(project.stack_url, project.token)
        try:
            tables = client.list_tables(bucket_id=bucket_id, branch_id=branch_id)
            components = (
                fetch_usage_components(client, alias, usage_branch_id) if include_usage else []
            )
            return (alias, tables, components)
        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        finally:
            client.close()
