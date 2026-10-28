"""Local deletes are three-way, and a config delete covers its rows (sync group 3).

``compute_changeset`` / ``compute_row_changeset`` call every tracked config or
row whose local file is gone ``deleted``. That was a two-way decision: a
config (or row) edited in the UI since the last pull was ``deleted`` all the
same, and ``push --force`` deleted it together with the remote edit -- the one
place where ``--force`` overrode a conflict (finding force-delete-over-remote-
edit). :func:`settle_deletions` makes the decision three-way (D1):

- the remote equals the recorded baseline of this target: ``deleted`` (push
  deletes it with ``--force``);
- the remote changed since the baseline: ``conflict`` -- ``--force`` enables
  deletions, it never overrides a conflict;
- there is no trustworthy baseline (none recorded, or an unknown one): a
  ``conflict`` too;
- the remote is gone as well: nothing to delete (the engines never list it).

A config's hash leaves its rows out, so a parent delete also weighs its rows
(D2): a row edited, added or deleted on the remote under a config deleted
locally blocks the config delete, even when the config itself is unchanged.
The rows of a blocked config are not deleted either (D3).

:func:`fold_covered_rows` then shapes the push plan (D3): one DELETE of a
config removes its rows, so the plan sends no row DELETEs for the rows of a
config it deletes in the same push -- they used to answer 404 and turn a fully
successful push into errors (finding force-delete-row-errors). They are listed
on the config change as ``covered_rows``. A failed or timed-out config DELETE
therefore never falls back to deleting its rows one by one.

Limits (D9, documented, not fixed): encrypted values are compared through one
placeholder, so a remote edit that changes ONLY an encrypted value is invisible
to the comparison; and the Storage API has no conditional delete, so a remote
edit that lands between the diff and the DELETE is not protected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..sync.branch_scope import UNKNOWN_BASE_HASH
from ..sync.diff_engine import ConfigChange, config_hash
from ..sync.manifest import ManifestConfiguration
from ._sync_models import DELETED_ROWS_KEY

CHANGED_WHY = "it changed remotely since the recorded baseline"
UNKNOWN_WHY = "there is no trustworthy baseline for this target"
ROWS_CHANGED_WHY = (
    "its rows changed remotely since the recorded baseline (a row was edited, added or deleted)"
)


@dataclass(frozen=True)
class DeleteTarget:
    """The branch a diff compares local files with, for the conflict message (D4).

    Attributes:
        branch_id: The target branch id.
        production: The target is the production branch.
        source_tree: The tree push reads local files from.
        target_tree: The target branch's own tree. It differs from
            *source_tree* on the promote path (``push --branch DEV`` with no
            ``dev/`` tree reads ``main/``).
    """

    branch_id: int | None
    production: bool
    source_tree: str
    target_tree: str

    @property
    def label(self) -> str:
        """How the message names the branch."""
        return "production" if self.production else str(self.branch_id)

    @property
    def pull_command(self) -> str:
        """The pull that restores this target's files."""
        if self.production:
            return "kbagent sync pull"
        return f"kbagent sync pull --branch {self.branch_id}"


def delete_conflict_detail(target: DeleteTarget, what: str, why: str) -> str:
    """The ``details`` line of a delete push refuses (D4)."""
    text = (
        f"Cannot delete {what} on branch {target.label}: {why}. --force enables deletions "
        f"but does not override conflicts. Run `{target.pull_command}` and review the "
        f"restored files; to delete the reviewed version, remove them again and run "
        f"`sync push --force`."
    )
    if target.source_tree != target.target_tree:
        text += (
            f" This push reads {target.source_tree}/; pulling branch {target.label} creates "
            f"its own tree {target.target_tree}/, which later pushes to that branch read instead."
        )
    return text


@dataclass(frozen=True)
class DeleteInputs:
    """What :func:`settle_deletions` compares a local delete with.

    Attributes:
        config_bases: ``"{component}/{config}"`` -> the recorded baseline hash.
        row_bases: ``"{component}/{config}/rows/{row}"`` -> the row's baseline.
        target_row_keys: Row keys recorded from the target branch itself; one
            missing on the remote was deleted there since the last pull.
        remote_rows: The target's rows, keyed like *row_bases*.
        remote_only_rows: Rows on the target that no local file or manifest
            entry accounts for (``remote_only_rows[]`` of the diff).
    """

    config_bases: dict[str, str]
    row_bases: dict[str, str]
    target_row_keys: set[str]
    remote_rows: dict[str, dict[str, Any]]
    remote_only_rows: list[dict[str, str]] = field(default_factory=list)


def _row_key(change: ConfigChange) -> str:
    return f"{change.component_id}/{change.parent_config_id}/rows/{change.config_id}"


def _settle_one(change: ConfigChange, inputs: DeleteInputs, target: DeleteTarget) -> None:
    """D1: one local delete, three-way against its baseline."""
    key = _row_key(change) if change.is_row else f"{change.component_id}/{change.config_id}"
    base = (inputs.row_bases if change.is_row else inputs.config_bases).get(key, "")
    unknown = not base or base == UNKNOWN_BASE_HASH
    if not unknown and change.remote_data is not None and config_hash(change.remote_data) == base:
        return
    change.change_type = "conflict"
    change.details = [delete_conflict_detail(target, key, UNKNOWN_WHY if unknown else CHANGED_WHY)]


def _rows_changed(
    component_id: str, config_id: str, changes: list[ConfigChange], inputs: DeleteInputs
) -> bool:
    """D2: a remote change to any row of a config deleted locally."""
    prefix = f"{component_id}/{config_id}/rows/"
    if any(
        c.is_row
        and c.component_id == component_id
        and c.parent_config_id == config_id
        and c.change_type in ("conflict", "remote_modified", "remote_deleted")
        for c in changes
    ):
        return True  # a row edited remotely (or a row delete that conflicts)
    if any(
        r["component_id"] == component_id and r["parent_config_id"] == config_id
        for r in inputs.remote_only_rows
    ):
        return True  # a row added remotely
    return any(
        key.startswith(prefix) and key not in inputs.remote_rows for key in inputs.target_row_keys
    )  # a row deleted remotely


def settle_deletions(
    changes: list[ConfigChange], inputs: DeleteInputs, target: DeleteTarget
) -> None:
    """Make every local delete in *changes* three-way, rows included (D1-D3). Mutates *changes*."""
    for change in changes:
        if change.change_type == "deleted":
            _settle_one(change, inputs, target)
    blocked: set[tuple[str, str]] = set()
    for change in changes:
        # A config delete: no local data (its file is gone), not a row.
        if change.is_row or change.local_data is not None:
            continue
        if change.change_type not in ("deleted", "conflict"):
            continue
        if change.change_type == "deleted" and _rows_changed(
            change.component_id, change.config_id, changes, inputs
        ):
            change.change_type = "conflict"
            key = f"{change.component_id}/{change.config_id}"
            change.details = [delete_conflict_detail(target, key, ROWS_CHANGED_WHY)]
        if change.change_type == "conflict":
            blocked.add((change.component_id, change.config_id))
    for change in changes:
        if not change.is_row or change.change_type != "deleted":
            continue
        parent = (change.component_id, change.parent_config_id)
        if parent in blocked:
            change.change_type = "conflict"
            why = f"its configuration {parent[0]}/{parent[1]} cannot be deleted (see its conflict)"
            change.details = [delete_conflict_detail(target, _row_key(change), why)]


def track_source_deletions(
    entry: ManifestConfiguration,
    remote_rows: dict[str, dict[str, Any]],
    tracked_row_keys: set[str],
    row_bases: dict[str, str],
) -> None:
    """Track the target rows a source-tree entry's pulls deleted (promote path, D7).

    *entry* is an entry of another branch than the target (production, read
    from ``main/`` by ``push --branch DEV``). A row its pulls removed because
    production deleted it, still on the target, is tracked with the row's last
    production baseline: the row diff calls it ``deleted`` (local file gone),
    and :func:`settle_deletions` keeps it ``deleted`` only while the target's
    row equals that baseline. A row nothing in the source ever represented is
    not tracked: it is a remote-only row (D5). No baseline recorded: unknown,
    so a conflict -- never one made up from the current listing.
    """
    deleted = entry.metadata.get(DELETED_ROWS_KEY)
    if not isinstance(deleted, dict):
        return
    for row_id, base in deleted.items():
        key = f"{entry.component_id}/{entry.id}/rows/{row_id}"
        if key in remote_rows and key not in tracked_row_keys:
            tracked_row_keys.add(key)
            row_bases[key] = str(base or "") or UNKNOWN_BASE_HASH


def _is_config_delete(change: dict[str, Any]) -> bool:
    return change["change_type"] == "deleted" and not change.get("is_row")


def fold_covered_rows(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The changes push plans, a config delete standing for its rows (D2, D3).

    A row ``deleted`` under a config ``deleted`` in the same changeset is not
    planned on its own: the config's DELETE removes it. It is recorded on the
    config change as ``covered_rows`` (``config_id``, ``config_name``,
    ``path``), so a dry run and the real push count the config once and still
    say which rows go with it. A config delete with any row change push does
    not apply (a conflict, a remote change) is turned into a ``conflict``:
    deleting the config would delete that row too. ``sync diff`` already
    decides this (:func:`settle_deletions`); the plan never relies on it.
    """
    held = {
        (c["component_id"], c.get("parent_config_id", ""))
        for c in changes
        if c.get("is_row") and c["change_type"] not in ("added", "modified", "deleted")
    }
    parents: dict[tuple[str, str], dict[str, Any]] = {}
    blocked: set[tuple[str, str]] = set()
    for change in changes:
        if _is_config_delete(change):
            key = (change["component_id"], change["config_id"])
            if key in held:
                change["change_type"] = "conflict"
                blocked.add(key)
            else:
                parents[key] = change
    planned: list[dict[str, Any]] = []
    for change in changes:
        row_delete = bool(change.get("is_row")) and change["change_type"] == "deleted"
        key = (change["component_id"], change.get("parent_config_id", ""))
        if row_delete and key in blocked:
            change["change_type"] = "conflict"  # never deleted on its own either (D3)
        parent = parents.get(key) if row_delete else None
        if parent is None:
            planned.append(change)
            continue
        parent.setdefault("covered_rows", []).append(
            {
                "config_id": change.get("config_id", ""),
                "config_name": change.get("config_name", ""),
                "path": change.get("path", ""),
            }
        )
    return planned
