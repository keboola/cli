"""Hint definitions for the ``semantic-layer`` command group (since v0.41.0).

Mirrors the per-subcommand surface of
:class:`keboola_agent_cli.services.semantic_layer_service.SemanticLayerService`.
The wire-level client is
:class:`keboola_agent_cli.metastore_client.MetastoreClient`; the renderer
construct it directly via ``client_type="metastore"``. The metastore URL
is derived from each project's stack URL automatically
(``connection.`` -> ``metastore.``).
"""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# Note printed on every hint that lists by parallel-fanning across the
# child types. The renderer only emits one ``item_type`` per ``list_items``
# call, but the underlying service makes five such calls in parallel —
# this note ensures the reader understands the single rendered call is
# representative.
_PARALLEL_CHILDREN_NOTE = (
    "The service fans out 5 list_items calls in parallel — one per child "
    "kind: semantic-dataset, semantic-metric, semantic-relationship, "
    "semantic-constraint, semantic-glossary. The rendered snippet shows "
    "one representative call; replicate for each kind in real code."
)


def _make_service(method: str, **extra_args: str) -> ServiceCall:
    """Convenience builder for the service half of a hint step.

    All semantic-layer ServiceCalls hit the same module+class, so factor
    that out.
    """
    args: dict[str, str] = {"alias": "{project}"}
    args.update(extra_args)
    return ServiceCall(
        service_class="SemanticLayerService",
        service_module="semantic_layer_service",
        method=method,
        args=args,
    )


# ── semantic-layer model list ──────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.model.list",
        description="List semantic-layer models in a project",
        steps=[
            HintStep(
                comment="List every `semantic-model` item in the project",
                client=ClientCall(
                    method="list_items",
                    args={"item_type": '"semantic-model"'},
                    client_type="metastore",
                    result_var="models",
                    result_hint="list[dict]",
                ),
                service=_make_service("list_models"),
            ),
        ],
    )
)


# ── semantic-layer model create ────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.model.create",
        description="Create a new semantic-layer model",
        steps=[
            HintStep(
                comment="POST /semantic-model with name + sql_dialect (+ optional description)",
                client=ClientCall(
                    method="post_item",
                    args={
                        "item_type": '"semantic-model"',
                        "name": "{name}",
                        "data": '{"name": {name}, "sql_dialect": {sql_dialect}}',
                    },
                    client_type="metastore",
                    result_var="model",
                    result_hint="dict",
                ),
                service=_make_service(
                    "create_model",
                    name="{name}",
                    description="{description}",
                    sql_dialect="{sql_dialect}",
                ),
            ),
        ],
        notes=[
            "Duplicate model name returns HTTP 500 (normalized to ALREADY_EXISTS).",
        ],
    )
)


# ── semantic-layer model delete ────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.model.delete",
        description="Delete a semantic-layer model (must have no children)",
        steps=[
            HintStep(
                comment="DELETE /semantic-model/{id}; metastore refuses if children exist",
                client=ClientCall(
                    method="delete_item",
                    args={"item_type": '"semantic-model"', "item_id": "{model}"},
                    client_type="metastore",
                    result_var="result",
                ),
                service=_make_service("delete_model", model_name_or_uuid="{model}"),
            ),
        ],
        notes=[
            "The service layer pre-fetches children for an orphan-warning envelope.",
        ],
    )
)


# ── semantic-layer show ────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.show",
        description="Show all entities of a model (datasets, metrics, etc.)",
        steps=[
            HintStep(
                comment=(
                    "Resolve model UUID, then fetch every child type in "
                    "parallel filtered by modelUUID (5 list_items calls)."
                ),
                client=ClientCall(
                    method="list_items",
                    args={
                        "item_type": '"semantic-metric"',
                        "model_uuid": "{model}",
                    },
                    client_type="metastore",
                    result_var="metrics",
                    result_hint="list[dict]",
                ),
                service=_make_service(
                    "show_model",
                    model_name_or_uuid="{model}",
                    type_filter="{type}",
                ),
            ),
        ],
        notes=[
            _PARALLEL_CHILDREN_NOTE,
            "Service layer fans the 5 list_items calls out in parallel (ThreadPoolExecutor max_workers=5).",
            "`--type` collapses the result to a single plural key (dataset->datasets, glossary->glossary, ...).",
        ],
    )
)


# ── semantic-layer validate ────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.validate",
        description="Validate a semantic-layer model (basic + --deep)",
        steps=[
            HintStep(
                comment=(
                    "Same parallel child fetch as `show`. Basic checks are "
                    "in-memory only; --deep additionally fetches every "
                    "dataset's Snowflake schema in parallel via "
                    "StorageService.get_table_detail."
                ),
                client=ClientCall(
                    method="list_items",
                    args={
                        "item_type": '"semantic-metric"',
                        "model_uuid": "{model}",
                    },
                    client_type="metastore",
                    result_var="metrics",
                    result_hint="list[dict]",
                ),
                service=_make_service(
                    "validate_model",
                    model_name_or_uuid="{model}",
                    deep="{deep}",
                ),
            ),
        ],
        notes=[
            _PARALLEL_CHILDREN_NOTE,
            "Basic checks: DUPLICATE, DANGLING_RELATIONSHIP, DANGLING_METRIC, "
            "SUM_ON_PCT, CONSTRAINT_ORPHAN, SEVERITY_SUFFIX.",
            "--deep adds: PHANTOM_FIELD, METRIC_PHANTOM, AGG_ON_STRING via "
            "real Snowflake column probing.",
        ],
    )
)


# ── semantic-layer export ──────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.export",
        description="Snapshot a model + every child entity to JSON",
        steps=[
            HintStep(
                comment=(
                    "Parallel child fetch (5 list_items) -> snapshot envelope -> "
                    "atomic os.open(O_NOFOLLOW, 0o644) write."
                ),
                client=ClientCall(
                    method="list_items",
                    args={
                        "item_type": '"semantic-metric"',
                        "model_uuid": "{model}",
                    },
                    client_type="metastore",
                    result_var="metrics",
                    result_hint="list[dict]",
                ),
                service=_make_service(
                    "export_model",
                    model_name_or_uuid="{model}",
                    output_path="Path({output})",
                ),
            ),
        ],
        notes=[
            _PARALLEL_CHILDREN_NOTE,
            "Default output path: ./sl_export_{model_name}_{YYYYMMDD_HHMMSS}.json.",
            "File is self-describing -- replayable by `import` and `promote`.",
        ],
    )
)


# ── semantic-layer diff ────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.diff",
        description="Diff two snapshots (project<->project, project<->file, file<->file)",
        steps=[
            HintStep(
                comment=(
                    "Resolve each side (live project = parallel child fetch; "
                    "file = read+parse JSON), then per-type added/removed/changed "
                    "with `diff_keys` for changed."
                ),
                client=ClientCall(
                    method="list_items",
                    args={
                        "item_type": '"semantic-metric"',
                        "model_uuid": "{model_a}",
                    },
                    client_type="metastore",
                    result_var="metrics_a",
                    result_hint="list[dict]",
                ),
                service=_make_service(
                    "diff",
                    project_a="{project_a}",
                    project_b="{project_b}",
                    model_a="{model_a}",
                    model_b="{model_b}",
                    file_a="Path({file_a})",
                    file_b="Path({file_b})",
                ),
            ),
        ],
        notes=[
            _PARALLEL_CHILDREN_NOTE,
            "Both sides are loaded independently — replicate the call for "
            "model_b too. File-backed sides skip this client call and parse JSON instead.",
            "Ignored keys: modelUUID, createdAt, lastUpdated, revision.",
        ],
    )
)


# ── semantic-layer add (one per entity type) ───────────────────────


def _register_add_hint(entity: str, **extra_service_args: str) -> None:
    """Register an `add.<entity>` hint with the standard POST shape."""
    type_slug = f"semantic-{entity}"
    HintRegistry.register(
        CommandHint(
            cli_command=f"semantic-layer.add.{entity}",
            description=f"Add a {entity} to a semantic-layer model",
            steps=[
                HintStep(
                    comment=(
                        f"Resolve modelUUID, then POST /{type_slug} with the validated payload."
                    ),
                    client=ClientCall(
                        method="post_item",
                        args={
                            "item_type": f'"{type_slug}"',
                            "name": "{name}",
                            "data": '{"modelUUID": "<resolved>"}',
                        },
                        client_type="metastore",
                        result_var="created",
                        result_hint="dict",
                    ),
                    service=_make_service(
                        f"add_{entity}",
                        model_name_or_uuid="{model}",
                        **extra_service_args,
                    ),
                ),
            ],
        )
    )


_register_add_hint(
    "metric",
    name="{name}",
    sql="{sql}",
    dataset="{dataset}",
    description="{description}",
)
_register_add_hint(
    "dataset",
    name="{name}",
    table_id="{table_id}",
    description="{description}",
    grain="{grain}",
    primary_key="{primary_key}",
    deep_fields="{deep_fields}",
)
_register_add_hint(
    "relationship",
    name="{name}",
    from_="{from_}",
    to="{to}",
    on="{on}",
    type_="{type_}",
)
_register_add_hint(
    "constraint",
    name="{name}",
    constraint_type="{constraint_type}",
    rule="{rule}",
    metrics="{metrics}",
    severity="{severity}",
)
# glossary uses `term`, not `name`.
HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.add.glossary",
        description="Add a glossary term to a semantic-layer model",
        steps=[
            HintStep(
                comment="Resolve modelUUID, then POST /semantic-glossary {term, definition}.",
                client=ClientCall(
                    method="post_item",
                    args={
                        "item_type": '"semantic-glossary"',
                        "name": "{term}",
                        "data": '{"term": {term}, "modelUUID": "<resolved>"}',
                    },
                    client_type="metastore",
                    result_var="created",
                    result_hint="dict",
                ),
                service=_make_service(
                    "add_glossary",
                    model_name_or_uuid="{model}",
                    term="{term}",
                    definition="{definition}",
                ),
            ),
        ],
        notes=[
            "Outer envelope `name` must equal `term` -- the service handles this.",
        ],
    )
)


# ── semantic-layer edit (DELETE+POST with rollback) ────────────────


def _register_edit_hint(entity: str, **extra_service_args: str) -> None:
    """Register an `edit.<entity>` hint -- DELETE old + POST new."""
    type_slug = f"semantic-{entity}"
    HintRegistry.register(
        CommandHint(
            cli_command=f"semantic-layer.edit.{entity}",
            description=f"Edit a {entity} via DELETE+POST with rollback",
            steps=[
                HintStep(
                    comment=(
                        f"DELETE /{type_slug}/<old_id>, then POST the new "
                        "payload. On POST failure, re-POST original_attrs to "
                        "roll back; the rollback success/failure is reported "
                        "in the envelope."
                    ),
                    client=ClientCall(
                        method="delete_item",
                        args={
                            "item_type": f'"{type_slug}"',
                            "item_id": '"<resolved-old-id>"',
                        },
                        client_type="metastore",
                        result_var="deleted",
                    ),
                    service=_make_service(
                        f"edit_{entity}",
                        model_name_or_uuid="{model}",
                        **extra_service_args,
                    ),
                ),
            ],
            notes=[
                "metastore exposes no PATCH -- DELETE+POST is the only edit shape.",
                "edit_metric rename cascades through constraints whose `metrics[]` "
                "includes the old name (DELETE old constraint + POST new).",
            ],
        )
    )


_register_edit_hint(
    "metric",
    current_name="{name}",
    new_name="{new_name}",
    new_sql="{new_sql}",
    new_dataset="{new_dataset}",
    new_description="{new_description}",
)
_register_edit_hint(
    "dataset",
    current_name="{name}",
    new_name="{new_name}",
    new_description="{new_description}",
    new_grain="{new_grain}",
)
_register_edit_hint(
    "constraint",
    current_name="{name}",
    new_name="{new_name}",
    new_rule="{new_rule}",
    new_constraint_type="{new_constraint_type}",
    new_severity="{new_severity}",
    new_metrics="{new_metrics}",
)
_register_edit_hint(
    "relationship",
    current_name="{name}",
    new_name="{new_name}",
    new_from="{new_from}",
    new_to="{new_to}",
    new_on="{new_on}",
    new_type="{new_type}",
)
# glossary identity is `term`.
HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.edit.glossary",
        description="Edit a glossary term via DELETE+POST with rollback",
        steps=[
            HintStep(
                comment=(
                    "DELETE /semantic-glossary/<old_id>, then POST the new "
                    "{term, definition} payload. Renaming the term is "
                    "destructive for downstream consumers joining on the "
                    "term string."
                ),
                client=ClientCall(
                    method="delete_item",
                    args={
                        "item_type": '"semantic-glossary"',
                        "item_id": '"<resolved-old-id>"',
                    },
                    client_type="metastore",
                    result_var="deleted",
                ),
                service=_make_service(
                    "edit_glossary",
                    model_name_or_uuid="{model}",
                    current_term="{term}",
                    new_term="{new_term}",
                    new_definition="{new_definition}",
                ),
            ),
        ],
        notes=[
            "--new-term is destructive -- pass --yes to bypass the TTY confirm.",
        ],
    )
)


# ── semantic-layer remove ──────────────────────────────────────────


def _register_remove_hint(entity: str, id_key: str = "name") -> None:
    """Register a `remove.<entity>` hint."""
    type_slug = f"semantic-{entity}"
    HintRegistry.register(
        CommandHint(
            cli_command=f"semantic-layer.remove.{entity}",
            description=f"Remove a {entity} (destructive)",
            steps=[
                HintStep(
                    comment=(
                        f"Resolve target by {id_key}, then DELETE /{type_slug}/<id>. "
                        "For metric, pre-scan constraints whose metrics[] "
                        "includes the target name for an orphan-warning envelope."
                    ),
                    client=ClientCall(
                        method="delete_item",
                        args={
                            "item_type": f'"{type_slug}"',
                            "item_id": '"<resolved-id>"',
                        },
                        client_type="metastore",
                        result_var="result",
                    ),
                    service=_make_service(
                        "remove_item",
                        model_name_or_uuid="{model}",
                        kind=f'"{entity}"',
                        name="{name}" if id_key == "name" else "{term}",
                    ),
                ),
            ],
            notes=[
                "The CLI calls preview_remove first to populate the orphan-warning envelope.",
                "Non-TTY without --yes refuses with exit 2 (the warning is always printed).",
            ],
        )
    )


_register_remove_hint("metric")
_register_remove_hint("dataset")
_register_remove_hint("constraint")
_register_remove_hint("relationship")
_register_remove_hint("glossary", id_key="term")


# ── semantic-layer import ──────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.import",
        description="Replay a snapshot into a project (skip-on-conflict by default)",
        steps=[
            HintStep(
                comment=(
                    "Load snapshot JSON, then iterate _PUSH_ORDER "
                    "(datasets, metrics, relationships, glossary, constraints) "
                    "and POST each item; rewrite modelUUID to the target. "
                    "Conflicting items are SKIPPED by default; --overwrite "
                    "triggers DELETE+POST."
                ),
                client=ClientCall(
                    method="post_item",
                    args={
                        "item_type": '"semantic-metric"',
                        "name": '"<per-item name>"',
                        "data": '{"modelUUID": "<target>"}',
                    },
                    client_type="metastore",
                    result_var="created",
                ),
                service=_make_service(
                    "import_snapshot",
                    file="Path({file})",
                    model_name_or_uuid="{model}",
                    types="{types}",
                    dry_run="{dry_run}",
                    overwrite="{overwrite}",
                ),
            ),
        ],
        notes=[
            "Default is skip-on-conflict (additive). Pass --overwrite for DELETE+POST.",
            "--dry-run plans counts without any write call.",
            "Loop the rendered post_item across each item_type in PUSH_ORDER: "
            "semantic-dataset, semantic-metric, semantic-relationship, "
            "semantic-glossary, semantic-constraint.",
        ],
    )
)


# ── semantic-layer promote ─────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.promote",
        description="Promote a model from one project to another (additive + overwrite)",
        steps=[
            HintStep(
                comment=(
                    "Open two MetastoreClients (src + tgt). Parallel-fetch "
                    "children on each. Classify items as NEW / IDENTICAL / "
                    "CHANGED. POST NEW; DELETE+POST CHANGED; skip IDENTICAL. "
                    "Items only in target are NEVER deleted (additive only)."
                ),
                client=ClientCall(
                    method="list_items",
                    args={
                        "item_type": '"semantic-metric"',
                        "model_uuid": '"<from_model_uuid>"',
                    },
                    client_type="metastore",
                    result_var="src_metrics",
                ),
                service=_make_service(
                    "promote_model",
                    from_project="{from_project}",
                    to_project="{to_project}",
                    from_model="{from_model}",
                    to_model="{to_model}",
                    types="{types}",
                    dry_run="{dry_run}",
                ),
            ),
        ],
        notes=[
            "Two clients held in try/finally; both close even on error. The "
            "rendered snippet constructs only one client — instantiate a second "
            "MetastoreClient for the target project with its own token.",
            "Deep-equality compare strips modelUUID + timestamps (revision, createdAt, lastUpdated).",
        ],
    )
)


# ── semantic-layer build ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.build",
        description="Heuristic greenfield builder from a list of storage tables",
        steps=[
            HintStep(
                comment=(
                    "Fetch every tableId's schema in parallel (StorageService."
                    "get_table_detail). Synthesise one dataset (with role-"
                    "classified fields), one COUNT(*) metric, and one "
                    "glossary entry per table. Validate locally. If clean, "
                    "POST in dependency order."
                ),
                client=ClientCall(
                    method="post_item",
                    args={
                        "item_type": '"semantic-dataset"',
                        "name": '"<derived-from-tableId>"',
                        "data": '{"modelUUID": "<target>"}',
                    },
                    client_type="metastore",
                    result_var="created",
                ),
                service=_make_service(
                    "build_model",
                    table_ids="{tables}",
                    model_name="{name}",
                    model_name_or_uuid="{model}",
                    dry_run="{dry_run}",
                    keep_on_failure="{keep_on_failure}",
                    output_path="Path({output})",
                ),
            ),
        ],
        notes=[
            "Response carries `fallback_used: 'heuristic'` -- no AI Service "
            "JSON-generation endpoint exists yet. Use as a scaffold, iterate "
            "via `add` / `edit`.",
            "Refuses to push if local validation surfaces errors (returns "
            "VALIDATION_ERROR with the error list in details).",
            "After the dataset POST, loop through semantic-metric, "
            "semantic-relationship, semantic-glossary, and semantic-constraint "
            "in that order.",
            "On push failure the service deletes every successfully-POSTed "
            "child in reverse PUSH_ORDER and deletes the model itself when "
            "we created it. Pass `--keep-on-failure` to preserve the partial "
            "state for forensic inspection (mirrors `data-app create`).",
        ],
    )
)


# ── semantic-layer token --encrypt ─────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="semantic-layer.token",
        description="Encrypt the project token for a transformation user_properties",
        steps=[
            HintStep(
                comment=(
                    "Build `{'#metastore_token': <project token>}` from the "
                    "already-stored Storage token, then delegate to the "
                    "EncryptService (same path as `encrypt values`)."
                ),
                client=ClientCall(
                    method="encrypt_values",
                    args={
                        "component_id": "{component_id}",
                        "data": '{"#metastore_token": "<project token>"}',
                    },
                    result_var="encrypted",
                    result_hint="dict",
                ),
                service=_make_service(
                    "encrypt_token",
                    component_id="{component_id}",
                ),
            ),
        ],
        notes=[
            "Uses the Encryption API (encryption.keboola.com).",
            "Output is a `KBC::ProjectSecure...` ciphertext ready to paste "
            "into the transformation's `user_properties` block.",
            "Classified `write` (same blast radius as `encrypt.values`).",
        ],
    )
)
