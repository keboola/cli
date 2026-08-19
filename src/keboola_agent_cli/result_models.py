"""Typed return models for the in-process SDK facade (issue #428).

These pydantic models document the **stable** return shapes of the high-traffic
service / facade operations. They are exported from the package root so a
downstream in-process consumer (a Keboola Data App, a transformation, a hosted
service, the FIIA Scaffold Kit) gets static typing (mypy / IDE autocomplete) and
a semver-versioned contract instead of coupling to undocumented
``dict[str, Any]`` shapes -- a contract change then surfaces at type-check time,
not at runtime against a customer build.

Every model sets ``extra="allow"``: the backing Keboola APIs grow fields across
stack versions, and an SDK contract must not raise when the server returns more
than the documented subset. Extra keys are preserved (reachable via attribute
access and ``model_dump()``), so nothing is lost -- only the **named** fields are
the committed, semver-stable surface. ``populate_by_name=True`` means each model
accepts both the snake_case field name and the raw API key (declared via
``AliasChoices``), so ``Model.model_validate(service_dict)`` works directly on a
service-layer dict without renaming.
"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _ApiResultModel(BaseModel):
    """Base for SDK result contracts: tolerant of unknown / extra API fields.

    Subclasses type only the stable subset; everything else the API returns is
    kept as model extras (``extra="allow"``) rather than dropped or raised on.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class JobResult(_ApiResultModel):
    """Result of running a Queue API job.

    Returned by :meth:`keboola_agent_cli.Client.run_job` and produced by
    ``JobService.run_job``. The named fields below are the committed contract;
    everything else the Queue API returns (``branchId``, ``createdTime``,
    ``startTime``, ``endTime``, ``durationSeconds``, ``runId``, ``url``, ...) is
    preserved as model extras.
    """

    id: str = Field(default="", description="Queue job ID.")
    status: str = Field(
        default="",
        description=(
            "Job status: created | waiting | processing | success | error | warning | terminated."
        ),
    )
    is_finished: bool = Field(
        default=False,
        validation_alias=AliasChoices("isFinished", "is_finished"),
        description="True once the job reached a terminal state.",
    )
    component_id: str = Field(
        default="",
        validation_alias=AliasChoices("component", "componentId", "component_id"),
        description="Component that ran.",
    )
    config_id: str = Field(
        default="",
        validation_alias=AliasChoices("configId", "config", "config_id"),
        description="Configuration that ran.",
    )
    mode: str = Field(default="", description="Job mode: run | debug | forceRun.")
    result: dict[str, Any] | None = Field(
        default=None,
        description="Component result payload (message, import stats, ...) when present.",
    )
    project_alias: str = Field(
        default="",
        description=(
            "Project alias the job ran in (CLI / service path). Empty for the "
            "in-process facade, which is not config-dir aware."
        ),
    )
    resolved_variable_values_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("resolvedVariableValuesId", "resolved_variable_values_id"),
        description="Values row resolved for linked variables, if any.",
    )
    log_tail: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("logTail", "log_tail"),
        description="Trailing job events surfaced on a non-success terminal state (wait mode).",
    )
    idempotent_replay: bool = Field(
        default=False,
        description=(
            "True when this job was returned from a prior run via a matching "
            "idempotency key (issue #427) rather than freshly created -- i.e. no "
            "new side effect was fired."
        ),
    )

    @property
    def succeeded(self) -> bool:
        """True iff the job finished in the ``success`` state."""
        return self.status == "success"

    @property
    def failed(self) -> bool:
        """True iff the job finished in a terminal failure state."""
        return self.status in {"error", "terminated", "cancelled"}


class QueryResult(_ApiResultModel):
    """Tabular result of a workspace SQL query.

    Returned by :meth:`keboola_agent_cli.Client.query_result`. Carries the
    column order and truncation metadata that the plain ``Client.query`` (which
    returns ``list[dict]``) drops. Values are **not** coerced -- for Snowflake
    every scalar comes back as a string (see the ``query()`` docstring / gotchas).
    """

    columns: list[str] = Field(
        default_factory=list, description="Result column names, in warehouse order."
    )
    rows: list[dict[str, Any]] = Field(
        default_factory=list, description="Rows as dicts keyed by column name."
    )
    truncated: bool = Field(
        default=False,
        description="True if the result was capped at the requested ``limit``.",
    )
    total_rows: int | None = Field(
        default=None,
        description="Total rows the warehouse reported for the statement, when known.",
    )

    @property
    def row_count(self) -> int:
        """Number of rows actually returned (after any ``limit`` cap)."""
        return len(self.rows)


class UploadTableResult(_ApiResultModel):
    """Result of importing a CSV into a Storage table.

    Returned by :meth:`keboola_agent_cli.Client.upload_table` and produced by
    ``StorageService.upload_table``. The ``auto_created_*`` flags are only set by
    the service path (which can create a missing bucket/table); the in-process
    facade requires the table to exist and leaves them ``False``.
    """

    table_id: str = Field(default="", description="Target table ID.")
    incremental: bool = Field(default=False, description="True = rows appended; False = full load.")
    imported_rows: int | None = Field(
        default=None,
        validation_alias=AliasChoices("imported_rows", "importedRowsCount"),
        description="Rows imported, when the backend reports a count.",
    )
    file_size_bytes: int | None = Field(
        default=None, description="Size of the uploaded CSV on disk."
    )
    warnings: list[Any] = Field(
        default_factory=list, description="Import warnings surfaced by Storage."
    )
    auto_created_bucket: bool = Field(
        default=False, description="True if the service path created the bucket."
    )
    auto_created_table: bool = Field(
        default=False, description="True if the service path created the table."
    )
    project_alias: str = Field(
        default="", description="Project alias (service path; empty for the facade)."
    )


class SyncPushResult(_ApiResultModel):
    """Result of a GitOps ``sync push``.

    Documents the shape of ``SyncService.push`` and is embedded in
    ``CloneResult`` (issue #426). ``status`` is ``pushed`` | ``no_changes`` |
    ``dry_run``; the counters and ``pushed_details`` describe what changed.
    """

    status: str = Field(default="", description="pushed | no_changes | dry_run.")
    created: int = Field(default=0, description="Configs/rows created.")
    updated: int = Field(default=0, description="Configs/rows updated.")
    deleted: int = Field(default=0, description="Configs/rows deleted.")
    errors: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-change failures (change_type, component_id, config_id, message).",
    )
    pushed_details: list[dict[str, Any]] = Field(
        default_factory=list, description="One entry per applied change."
    )
    name_drift_warnings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Local-vs-remote name drift warnings, when surfaced.",
    )

    @property
    def ok(self) -> bool:
        """True iff the push completed with no per-change errors."""
        return not self.errors


class CloneResult(_ApiResultModel):
    """Result of a ``sync clone`` composite (issue #426).

    Returned by ``SyncService.clone_project``. ``status`` is ``cloned`` (configs
    created), ``no_changes`` (idempotent re-run -- nothing left to create), or
    ``dry_run``. The embedded ``push`` is the underlying ``SyncPushResult``.
    """

    status: str = Field(default="", description="cloned | no_changes | dry_run.")
    target_alias: str = Field(default="", description="Project the clone was pushed into.")
    target_dir: str = Field(default="", description="Where the clone was materialised.")
    created: int = Field(default=0, description="Configs/rows created in the target.")
    bucket_rewrites: int = Field(
        default=0, description="Table references rewritten by the bucket_map override."
    )
    variable_overrides: int = Field(default=0, description="keboola.variables values overridden.")
    renamed_instances: int = Field(
        default=0, description="Config paths renamed by the instance_rename override."
    )
    flow_task_remaps: int = Field(
        default=0, description="keboola.flow task configIds remapped reference->ULID."
    )
    push: SyncPushResult | None = Field(
        default=None, description="The underlying sync push result (None for dry_run)."
    )
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-change push errors, if any."
    )

    @property
    def ok(self) -> bool:
        """True iff the clone completed with no errors."""
        return not self.errors


class ConfigDetailResult(_ApiResultModel):
    """Detail of a single configuration.

    Returned by :meth:`keboola_agent_cli.Client.config_detail` and produced by
    ``ConfigService.get_config_detail`` in single-config mode. ``id`` is the
    configuration ID (Storage returns it under ``id``); the full Storage detail
    (``created``, ``isDisabled``, ``state``, ...) is preserved as extras.
    """

    id: str = Field(default="", description="Configuration ID.")
    name: str = Field(default="", description="Configuration name.")
    description: str = Field(default="", description="Configuration description.")
    version: int | None = Field(
        default=None,
        validation_alias=AliasChoices("version", "currentVersion"),
        description="Current configuration version.",
    )
    configuration: dict[str, Any] = Field(
        default_factory=dict, description="The configuration body."
    )
    rows: list[dict[str, Any]] = Field(
        default_factory=list, description="Config rows, when the config has any."
    )
    component_id: str = Field(
        default="",
        validation_alias=AliasChoices("component_id", "componentId"),
        description="Owning component ID.",
    )
    project_alias: str = Field(
        default="", description="Project alias (service path; empty for the facade)."
    )
    branch_id: int | None = Field(
        default=None, description="Dev branch ID the detail was read from (None = production)."
    )


class ScopedTokenResult(_ApiResultModel):
    """A freshly minted / rotated scoped Storage API token (issue: device enrollment).

    Returned by :meth:`keboola_agent_cli.Client.create_scoped_token` and
    :meth:`~keboola_agent_cli.Client.refresh_token`. ``token`` is a **one-time**
    secret reveal -- hand it to the consumer once and persist only ``id`` (for
    :meth:`~keboola_agent_cli.Client.delete_token` / ``refresh_token``) and
    ``expires``. The raw grant details (``bucketPermissions``, ``componentAccess``)
    are preserved as model extras.
    """

    id: str = Field(default="", description="Token ID (use with delete_token / refresh_token).")
    token: str = Field(
        default="", description="The token secret -- revealed once; never persist it."
    )
    description: str = Field(default="", description="Human-readable token description.")
    expires: str | None = Field(
        default=None, description="ISO expiry timestamp; None = never expires."
    )
    can_read_all_file_uploads: bool = Field(
        default=False,
        validation_alias=AliasChoices("canReadAllFileUploads", "can_read_all_file_uploads"),
        description="True if the token may read files uploaded by other tokens.",
    )


class TokenListEntryResult(_ApiResultModel):
    """One Storage API token as listed by :meth:`keboola_agent_cli.Client.list_tokens`.

    Deliberately carries **no** secret field. `create_scoped_token` is the one
    and only reveal in this SDK; a listing that returned live values would
    break that contract for every token in the project at once, so the facade
    drops the field before validating even when the API includes it (projects
    with the ``force-decrypted-token`` feature do). Everything else the API
    reports -- ``bucketPermissions``, ``componentAccess``, the remaining
    ``can*`` grants -- is preserved as model extras.
    """

    id: str = Field(default="", description="Token ID (use with delete_token / refresh_token).")
    description: str = Field(default="", description="Human-readable token description.")
    created: str | None = Field(default=None, description="ISO creation timestamp.")
    expires: str | None = Field(
        default=None, description="ISO expiry timestamp; None = never expires."
    )
    is_expired: bool = Field(
        default=False,
        validation_alias=AliasChoices("isExpired", "is_expired"),
        description="True once the token is past its expiry.",
    )
    is_master_token: bool = Field(
        default=False,
        validation_alias=AliasChoices("isMasterToken", "is_master_token"),
        description="True for the project's master token (cannot be deleted).",
    )


class StreamSourceResult(_ApiResultModel):
    """A per-device Data Streams (OTLP) source (issue: device enrollment).

    Returned by :meth:`keboola_agent_cli.Client.create_stream_source` and
    :meth:`~keboola_agent_cli.Client.get_stream_source`. ``otlp_url`` embeds the
    ingest secret **unmasked** -- hand it to the device once, never persist it.
    ``sink_bucket_id`` is the ``in.c-otlp-<id>`` bucket the auto-provisioned sinks
    write to (grant a device token ``write`` on it); ``None`` when the source was
    created with ``provision_sinks=False`` or is not OTLP. The raw Stream source
    object is preserved under the ``source`` extra.
    """

    id: str = Field(
        default="",
        validation_alias=AliasChoices("id", "sourceId", "source_id"),
        description="Source ID (per-device; use with get/delete_stream_source).",
    )
    source_id: str = Field(
        default="",
        validation_alias=AliasChoices("source_id", "sourceId"),
        description="Source ID (alias of id, as the Stream API names it).",
    )
    name: str = Field(default="", description="Human-readable source name.")
    type: str = Field(default="", description="Source type: otlp | http.")
    description: str = Field(default="", description="Source description.")
    branch_id: str = Field(default="default", description="Branch the source lives in.")
    otlp_url: str = Field(
        default="",
        validation_alias=AliasChoices("otlp_url", "otlpUrl"),
        description="OTLP ingest endpoint URL WITH embedded secret (unmasked; reveal once).",
    )
    otlp_secret: str = Field(
        default="",
        validation_alias=AliasChoices("otlp_secret", "otlpSecret"),
        description="The ingest secret embedded in otlp_url.",
    )
    base_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("base_endpoint", "baseUrl"),
        description="Secret-free base endpoint.",
    )
    sink_bucket_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sink_bucket_id", "sinkBucketId"),
        description="in.c-otlp-<id> sink bucket (grant a device token write on it); None if none.",
    )
