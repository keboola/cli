"""The ``warnings[]`` of a ``sync clone`` result (CLI-24).

A clone creates every config of the reference tree fresh in the target project
and push re-points the links between them. Some configs still need an action in
the target before they work, and nothing in the push result says so. This
module lists them, from the configs the clone created (or, for ``--dry-run``,
would create):

- a flow / orchestration task that runs a config which is not in the tree,
- encrypted (``KBC::``) values, which only the reference project can decrypt,
- a data app, which sync creates but never deploys,
- a schedule, which sync never registers with the Scheduler service.

Only the run that creates the configs reports them: a re-run that creates
nothing returns no warnings, so the caller must keep them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..sync.manifest import Manifest, load_manifest
from ._encryption import find_encrypted_secret_paths, find_unencryptable_secret_paths
from ._sync_bindings import task_config_ref
from ._sync_models import FLOW_COMPONENT_ID, ORCHESTRATOR_COMPONENT_ID, SCHEDULER_COMPONENT_ID
from .data_app_service import DATA_APP_COMPONENT_ID

if TYPE_CHECKING:
    from .sync_service import SyncService

_TASK_OWNER_LABELS: dict[str, str] = {
    FLOW_COMPONENT_ID: "Flow",
    ORCHESTRATOR_COMPONENT_ID: "Orchestration",
}

# Where a config's OAuth credentials sit in its local ``_config.yml``. They
# belong to the reference project's OAuth authorization, so the fix is a new
# authorization, not a plaintext value.
_OAUTH_PATH_PREFIX = "_configuration_extra.authorization.oauth_api."


@dataclass
class EncryptedValues:
    """The ``KBC::`` values of one config and its rows, as ``_config.yml`` paths.

    ``secret_keys`` are under a ``#`` key: push encrypts a plaintext put there.
    ``unencryptable_keys`` are under a plain key: push does not encrypt them.
    ``oauth_keys`` are OAuth credentials. A row's paths start with the row path.
    """

    secret_keys: list[str] = field(default_factory=list)
    unencryptable_keys: list[str] = field(default_factory=list)
    oauth_keys: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [*self.secret_keys, *self.unencryptable_keys, *self.oauth_keys]

    def add(self, local_data: dict[str, Any], prefix: str = "") -> None:
        """Add the encrypted paths of one local ``_config.yml`` dict."""
        content = {key: value for key, value in local_data.items() if key != "_keboola"}
        for path in find_encrypted_secret_paths(content):
            self._bucket(path, self.secret_keys).append(f"{prefix}{path}")
        for path in find_unencryptable_secret_paths(content):
            self._bucket(path, self.unencryptable_keys).append(f"{prefix}{path}")

    def _bucket(self, path: str, default: list[str]) -> list[str]:
        return self.oauth_keys if path.startswith(_OAUTH_PATH_PREFIX) else default


@dataclass(frozen=True)
class _ClonedConfig:
    """A config the clone created, as its local ``_config.yml`` describes it."""

    component_id: str
    config_id: str
    name: str
    path: str
    config_file: Path
    data: dict[str, Any]

    @property
    def label(self) -> str:
        return f"'{self.name}' ({self.component_id}/{self.config_id})"

    @property
    def extra(self) -> dict[str, Any]:
        extra = self.data.get("_configuration_extra")
        return extra if isinstance(extra, dict) else {}

    def record(self, change_type: str, message: str, **fields: Any) -> dict[str, Any]:
        return {
            "change_type": change_type,
            "component_id": self.component_id,
            "config_id": self.config_id,
            "path": self.path,
            "message": message,
            **fields,
        }


@dataclass(frozen=True)
class _WarningContext:
    """What every warning of one clone run needs to know."""

    target_alias: str
    branch_override: int | None
    dry_run: bool
    tree_keys: set[tuple[str, str]]
    schedules_per_flow: dict[str, int]

    @property
    def branch_option(self) -> str:
        return f" --branch {self.branch_override}" if self.branch_override is not None else ""


@dataclass(frozen=True)
class _CloneTree:
    """The clone's manifest and the directory its branch's configs live in."""

    manifest: Manifest
    branch_id: int | None
    branch_dir: Path


def _clone_tree(
    service: SyncService, target_alias: str, target_path: Path, branch_override: int | None
) -> _CloneTree:
    manifest = load_manifest(target_path)
    branch_id = service._resolve_branch_id(
        target_alias, manifest, target_path, branch_override=branch_override
    )
    branch_path = service._resolve_source_branch_path(manifest, target_path, branch_id)
    return _CloneTree(manifest=manifest, branch_id=branch_id, branch_dir=target_path / branch_path)


def _added(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in changes if c.get("change_type") == "added"]


def read_encrypted_values(
    service: SyncService,
    *,
    target_alias: str,
    target_path: Path,
    branch_override: int | None,
    changes: list[dict[str, Any]],
) -> dict[tuple[str, str], EncryptedValues]:
    """Read the ``KBC::`` values of each config the clone creates, before the push.

    Push encrypts a plaintext ``#`` value for the target and writes the new
    ciphertext back to the local file. After the push that value would look
    like one copied from the reference, so the values are read first. The dry
    run reads them the same way, from the same diff changes. Keyed by
    ``(component_id, path)`` of the config change.
    """
    added = _added(changes)
    if not added:
        return {}
    tree = _clone_tree(service, target_alias, target_path, branch_override)
    # A row change's path is relative to its parent config's directory.
    row_paths_by_parent: dict[tuple[str, str], list[str]] = {}
    for change in added:
        if change.get("is_row"):
            parent_key = (change["component_id"], str(change.get("parent_config_id", "")))
            row_paths_by_parent.setdefault(parent_key, []).append(change.get("path", ""))

    encrypted: dict[tuple[str, str], EncryptedValues] = {}
    for change in added:
        if change.get("is_row"):
            continue
        config_dir = tree.branch_dir / change.get("path", "")
        local_data = service._read_config_file(config_dir)
        if local_data is None:
            continue
        values = EncryptedValues()
        values.add(local_data)
        parent_key = (change["component_id"], str(change.get("config_id", "")))
        for row_path in row_paths_by_parent.get(parent_key, []):
            row_data = service._read_config_file(config_dir / row_path)
            if row_data is not None:
                values.add(row_data, prefix=f"{row_path}: ")
        if values.keys:
            encrypted[(change["component_id"], change.get("path", ""))] = values
    return encrypted


def collect_clone_warnings(
    service: SyncService,
    *,
    target_alias: str,
    target_path: Path,
    branch_override: int | None,
    changes: list[dict[str, Any]],
    encrypted: dict[tuple[str, str], EncryptedValues],
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Return one warning per follow-up the target project needs after a clone.

    ``changes`` are the diff changes (``--dry-run``) or the push
    ``pushed_details``; only the ``added`` ones count. ``encrypted`` comes
    from :func:`read_encrypted_values`, read before the push. After a push the
    local files carry the target's new ids. For ``--dry-run`` they still carry
    the reference ids, so those messages give no command with an id in it.
    """
    added = [c for c in _added(changes) if not c.get("is_row")]
    if not added:
        return []
    tree = _clone_tree(service, target_alias, target_path, branch_override)
    context = _WarningContext(
        target_alias=target_alias,
        branch_override=branch_override,
        dry_run=dry_run,
        tree_keys={(cfg.component_id, str(cfg.id)) for cfg in tree.manifest.configurations},
        schedules_per_flow=_schedules_per_flow(service, tree),
    )

    warnings: list[dict[str, Any]] = []
    for change in added:
        component_id = change["component_id"]
        path = change.get("path", "")
        config_dir = tree.branch_dir / path
        local_data = service._read_config_file(config_dir)
        if local_data is None:
            continue
        written_id = (local_data.get("_keboola") or {}).get("config_id")
        config = _ClonedConfig(
            component_id=component_id,
            config_id=str(written_id or change.get("config_id", "")),
            name=str(local_data.get("name", "")),
            path=path,
            config_file=config_dir / CONFIG_FILENAME,
            data=local_data,
        )
        warnings.extend(_missing_task_targets(config, context))
        values = encrypted.get((component_id, path))
        if values is not None:
            warnings.append(_encrypted_values_warning(config, values, context))
        if component_id == DATA_APP_COMPONENT_ID:
            warnings.append(_data_app_warning(config, context))
        if component_id == SCHEDULER_COMPONENT_ID:
            warnings.append(_schedule_warning(config, context))
    return warnings


def _schedules_per_flow(service: SyncService, tree: _CloneTree) -> dict[str, int]:
    """Count the schedules in the tree's branch per flow id they run."""
    counts: dict[str, int] = {}
    target_branch = tree.branch_id or 0
    for cfg in tree.manifest.configurations:
        if cfg.component_id != SCHEDULER_COMPONENT_ID or cfg.branch_id != target_branch:
            continue
        local_data = service._read_config_file(tree.branch_dir / cfg.path) or {}
        target = (local_data.get("_configuration_extra") or {}).get("target")
        if isinstance(target, dict) and target.get("componentId") == FLOW_COMPONENT_ID:
            flow_id = str(target.get("configurationId", ""))
            counts[flow_id] = counts.get(flow_id, 0) + 1
    return counts


def _missing_task_targets(config: _ClonedConfig, context: _WarningContext) -> list[dict[str, Any]]:
    """Warn for each flow / orchestration task that runs a config outside the tree."""
    owner = _TASK_OWNER_LABELS.get(config.component_id)
    tasks = config.extra.get("tasks")
    if owner is None or not isinstance(tasks, list):
        return []
    warnings: list[dict[str, Any]] = []
    for task_entry in tasks:
        ref = task_config_ref(config.component_id, task_entry)
        if ref is None or ref in context.tree_keys:
            continue
        target_component_id, target_config_id = ref
        task_name = task_entry.get("name", "")
        task_id = task_entry.get("id", "")
        message = (
            f"{owner} {config.label}, task '{task_name}' (id {task_id}), runs "
            f"{target_component_id}/{target_config_id}, which is not in the cloned tree. The "
            "target project has no such config, so the task fails. Create the config in the "
            "target and point the task at it, or remove the task."
        )
        warnings.append(
            config.record(
                "missing_task_target",
                message,
                task_id=task_id,
                task_name=task_name,
                target_component_id=target_component_id,
                target_config_id=target_config_id,
            )
        )
    return warnings


def _encrypted_values_warning(
    config: _ClonedConfig, values: EncryptedValues, context: _WarningContext
) -> dict[str, Any]:
    """Warn once per config that holds ``KBC::`` values, listing the keys (never the values)."""
    count = len(values.keys)
    parts = [
        (
            f"{config.label} holds {count} encrypted value(s) copied as-is from the "
            "reference project. The target project cannot decrypt them."
        )
    ]
    if values.secret_keys:
        listed = ", ".join(values.secret_keys)
        parts.append(
            f"Put the plaintext of these into {config.config_file} (a row's key into that "
            f"row's file) and run `kbagent sync push`, which encrypts them for the target: "
            f"{listed}. `kbagent config clone --target-project` handles this for a single "
            "config with `--secret PATH=VALUE`."
        )
    if values.unencryptable_keys:
        listed = ", ".join(values.unencryptable_keys)
        parts.append(
            f"These are not under a `#` key, so push does not encrypt them: {listed}. Encrypt "
            f"each value with `kbagent encrypt values --project {context.target_alias} "
            f"--component-id {config.component_id}` and set the result in the file."
        )
    if values.oauth_keys:
        listed = ", ".join(values.oauth_keys)
        parts.append(
            f"These are OAuth credentials of the reference project: {listed}. Authorize the "
            "config again in the target project (`kbagent config oauth-url` gives the link)."
        )
    return config.record(
        "encrypted_values_copied",
        " ".join(parts),
        keys=values.keys,
        secret_keys=values.secret_keys,
        unencryptable_keys=values.unencryptable_keys,
        oauth_keys=values.oauth_keys,
    )


def _data_app_warning(config: _ClonedConfig, context: _WarningContext) -> dict[str, Any]:
    """Warn that sync creates the data app but does not deploy it."""
    parameters = config.data.get("parameters")
    app_id = str(parameters.get("id", "")) if isinstance(parameters, dict) else ""
    message = f"sync clone does not deploy data app {config.label}."
    if not context.dry_run and app_id:
        message += (
            f" Deploy it with `kbagent data-app deploy --project {context.target_alias} "
            f"--app-id {app_id}{context.branch_option}`."
        )
    else:
        message += " Deploy it with `kbagent data-app deploy` after the clone."
    return config.record("data_app_not_deployed", message, app_id=app_id)


def _schedule_warning(config: _ClonedConfig, context: _WarningContext) -> dict[str, Any]:
    """Report the schedule as not active: sync never registers it with the Scheduler service.

    The activation command is given only for a real clone of a schedule whose
    flow is in the tree, because only then is the flow id the target's own.
    """
    schedule = config.extra.get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    target = config.extra.get("target")
    flow_id = (
        str(target.get("configurationId", ""))
        if isinstance(target, dict) and target.get("componentId") == FLOW_COMPONENT_ID
        else ""
    )
    message = (
        f"sync clone does not activate schedule {config.label}: it is not registered with the "
        "Scheduler service, so the target project starts no jobs from it."
    )
    flow_in_tree = (FLOW_COMPONENT_ID, flow_id) in context.tree_keys
    if not context.dry_run and flow_in_tree and schedule.get("cronTab"):
        message += _schedule_hint(schedule, flow_id, context)
    return config.record("schedule_not_active", message, active=False)


def _schedule_hint(schedule: dict[str, Any], flow_id: str, context: _WarningContext) -> str:
    """Return how to activate a cloned schedule of a flow.

    ``flow schedule`` updates the first schedule it finds for a flow, so the
    command is given only when the flow has exactly one schedule.
    """
    flow_schedules = context.schedules_per_flow.get(flow_id, 0)
    if flow_schedules > 1:
        return (
            f" {flow_schedules} cloned schedules run flow {flow_id}, and `kbagent flow schedule` "
            "updates only one schedule per flow. Activate these schedules in the Keboola UI."
        )
    timezone = schedule.get("timezone", "")
    options = f" --timezone {timezone}" if timezone else ""
    if schedule.get("state") == "disabled":
        options += " --disabled"
    cron_tab = schedule["cronTab"]
    return (
        f" To register it, run `kbagent flow schedule --project {context.target_alias} "
        f"--flow-id {flow_id} --cron '{cron_tab}'{options}{context.branch_option}`, which "
        "updates this schedule."
    )
