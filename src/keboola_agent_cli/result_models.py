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
