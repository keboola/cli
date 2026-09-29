"""Counterexample / regression tests from the issue #792 formal-verification pilot.

Issue: https://github.com/keboola/cli/issues/792
Models: ``formal/sync/README.md`` (TLA+ model + Lean model, both mirroring the
real ``sync pull`` / ``sync diff`` / ``sync push`` code line-by-line).

Each test drives the real ``SyncService`` against a mocked/faked Keboola
client, one test per consolidated finding A..K from the pilot (see the
findings table in ``formal/sync/README.md``). Findings A..K deduplicate the
independent hits from the spec (S1..S5), the Lean refutations (F1..F8) and
the TLA+ counterexamples (I1..I12) -- several tools independently rediscovered
the same code paths.

Outcome legend:

- REPRODUCED on current code: the test asserts the SAFE behavior and is
  marked ``@pytest.mark.xfail(strict=True, ...)``. ``strict=True`` means the
  test suite goes RED the moment the behavior is fixed -- that is
  deliberate. To adopt a fix, delete the ``xfail`` marker (and this note in
  the docstring pointing at it); the test then becomes an ordinary
  regression guard.
- NOT reproduced / intentional documented behavior: a plain, unmarked
  regression guard asserting the current (safe or deliberately conservative)
  behavior.

Two simple test doubles are used, matching what the finding needs:

- ``_make_mock_client`` / ``_init_and_pull``: a single-shot ``MagicMock``
  client, for findings that need only one pull followed by one push/diff
  (mirrors the fixtures already used in ``tests/test_sync_service.py``).
- ``FakeApi`` / ``World``: a small stateful, branch-aware in-memory Storage
  API (ported from the pilot's ``scratchpad/replay/harness.py`` scratch
  harness) for findings that need a multi-step story: pull, an out-of-band
  remote/user action, then a second pull/diff/push.
"""

from __future__ import annotations

import copy
import itertools
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest

# ===========================================================================
# Shared fixtures -- single-shot mock client (mirrors test_sync_service.py)
# ===========================================================================

SAMPLE_VERIFY_TOKEN = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)

SAMPLE_BRANCHES = [
    {"id": 12345, "name": "Main", "isDefault": True},
]

SAMPLE_BRANCHES_WITH_DEV = [
    {"id": 12345, "name": "Main", "isDefault": True},
    {"id": 99999, "name": "feature-x", "isDefault": False},
]

SAMPLE_COMPONENTS_NO_ROWS = [
    {
        "id": "keboola.ex-http",
        "type": "extractor",
        "configurations": [
            {
                "id": "cfg-001",
                "name": "My HTTP Extractor",
                "description": "Fetches data",
                "configuration": {
                    "parameters": {"baseUrl": "https://api.example.com"},
                },
                "rows": [],
            }
        ],
    },
]


def _empty_component() -> list:
    """Same component family as SAMPLE_COMPONENTS_NO_ROWS, but no configs at all --
    models "the config was deleted/trashed remotely by another actor"."""
    return [{**SAMPLE_COMPONENTS_NO_ROWS[0], "configurations": []}]


def _make_mock_client(
    verify_token_response: TokenVerifyResponse | None = None,
    components_response: list | None = None,
    branches_response: list | None = None,
) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    if verify_token_response:
        client.verify_token.return_value = verify_token_response
    if components_response is not None:
        client.list_components_with_configs.return_value = components_response
    if branches_response is not None:
        client.list_dev_branches.return_value = branches_response
    return client


def _init_and_pull(
    tmp_config_dir: Path,
    project_root: Path,
    components: list,
) -> ConfigStore:
    """init_sync + pull, returning the ConfigStore with a materialized tree."""
    init_client = _make_mock_client(
        verify_token_response=SAMPLE_VERIFY_TOKEN,
        branches_response=SAMPLE_BRANCHES,
    )
    store = setup_single_project(tmp_config_dir)
    SyncService(
        config_store=store,
        client_factory=lambda url, token: init_client,
    ).init_sync(alias="prod", project_root=project_root)

    pull_client = _make_mock_client(components_response=components)
    SyncService(
        config_store=store,
        client_factory=lambda url, token: pull_client,
    ).pull(alias="prod", project_root=project_root)
    return store


def _svc_with_client(store: ConfigStore, components: list) -> tuple[SyncService, MagicMock]:
    client = _make_mock_client(components_response=components)
    svc = SyncService(config_store=store, client_factory=lambda url, token: client)
    return svc, client


# ===========================================================================
# Shared fixtures -- stateful fake API (ported from scratchpad/replay/harness.py)
# ===========================================================================

PROD = 12345
DEV = 99999
COMP = "keboola.ex-http"


class FakeApi:
    """Branch-aware in-memory Storage API: remote[branch_id][config_id] = cfg.

    Lets a test drive a realistic multi-step story (pull, an out-of-band
    remote/user action, pull/diff/push again) through the real
    ``SyncService`` without re-fetching a static mock response each time.
    """

    def __init__(self) -> None:
        self.remote: dict[int, dict[str, dict[str, Any]]] = {PROD: {}, DEV: {}}
        self._ids = itertools.count(100)
        self.log: list[str] = []
        # Optional hook for encryption-failure scenarios (finding F):
        # signature (project_id, component_id, data) -> dict[str, str].
        self.encrypt_values: Any = None

    def put(self, branch: int, cid: str, name: str, value: str, extra: dict | None = None) -> None:
        params: dict[str, Any] = {"value": value}
        if extra:
            params.update(extra)
        self.remote[branch][cid] = {
            "id": cid,
            "name": name,
            "description": "",
            "configuration": {"parameters": params},
            "rows": [],
            "isDisabled": False,
        }

    def ids(self, branch: int) -> list[str]:
        return sorted(self.remote[branch])

    def client(self) -> MagicMock:
        c = MagicMock()
        c.__enter__ = MagicMock(return_value=c)
        c.__exit__ = MagicMock(return_value=False)
        c.verify_token.return_value = TokenVerifyResponse(
            token_id="t", token_description="d", project_id=258, project_name="P", owner_name="O"
        )
        c.list_dev_branches.return_value = [
            {"id": PROD, "name": "Main", "isDefault": True},
            {"id": DEV, "name": "dev", "isDefault": False},
        ]
        c.list_buckets_with_metadata.return_value = []
        c.list_tables_with_metadata.return_value = []
        c.list_jobs_grouped.return_value = []
        c.list_config_metadata.return_value = []
        c.list_components_with_configs.side_effect = self._list
        c.create_config.side_effect = self._create
        c.update_config.side_effect = self._update
        c.delete_config.side_effect = self._delete
        c.get_config_detail.side_effect = self._detail
        if self.encrypt_values is not None:
            c.encrypt_values.side_effect = self.encrypt_values
        return c

    def _b(self, branch_id: int | None) -> int:
        return PROD if branch_id in (None, 0, PROD) else branch_id

    def _list(self, branch_id: int | None = None, **_: Any) -> list[dict[str, Any]]:
        cfgs = [copy.deepcopy(v) for v in self.remote[self._b(branch_id)].values()]
        return [{"id": COMP, "type": "extractor", "configurations": cfgs}] if cfgs else []

    def _create(
        self,
        component_id: str,
        name: str,
        configuration: dict,
        description: str = "",
        branch_id: int | None = None,
        is_disabled: bool = False,
        **_: Any,
    ) -> dict:
        cid = f"cfg-{next(self._ids)}"
        b = self._b(branch_id)
        self.remote[b][cid] = {
            "id": cid,
            "name": name,
            "description": description,
            "configuration": copy.deepcopy(configuration),
            "rows": [],
            "isDisabled": is_disabled,
        }
        self.log.append(f"CREATE {cid} on {b}")
        return copy.deepcopy(self.remote[b][cid])

    def _update(
        self,
        component_id: str,
        config_id: str,
        name: str | None = None,
        configuration: dict | None = None,
        description: str | None = None,
        change_description: str = "",
        branch_id: int | None = None,
        is_disabled: bool | None = None,
        **_: Any,
    ) -> dict:
        b = self._b(branch_id)
        cfg = self.remote[b][config_id]
        if configuration is not None:
            cfg["configuration"] = copy.deepcopy(configuration)
        if name is not None:
            cfg["name"] = name
        self.log.append(f"UPDATE {config_id} on {b}")
        return copy.deepcopy(cfg)

    def _delete(self, component_id: str, config_id: str, branch_id: int | None = None) -> None:
        b = self._b(branch_id)
        self.remote[b].pop(config_id)
        self.log.append(f"DELETE {config_id} on {b}")

    def _detail(self, component_id: str, config_id: str, branch_id: int | None = None, **_: Any):
        return copy.deepcopy(self.remote[self._b(branch_id)][config_id])


class World:
    """SyncService bound to a FakeApi + a real (tmp) sync working tree."""

    def __init__(self, tmp: Path) -> None:
        self.api = FakeApi()
        self.root = tmp / "project"
        self.root.mkdir(parents=True)
        cfgdir = tmp / "cfg"
        cfgdir.mkdir()
        self.store = setup_single_project(cfgdir)
        self.svc = SyncService(
            config_store=self.store, client_factory=lambda url, token: self.api.client()
        )

    def init(self) -> None:
        self.svc.init_sync(alias="prod", project_root=self.root)

    def pull(self, branch: int | None = None, **kw: Any) -> dict:
        return self.svc.pull(alias="prod", project_root=self.root, branch_override=branch, **kw)

    def diff(self, branch: int | None = None) -> dict:
        return self.svc.diff(alias="prod", project_root=self.root, branch_override=branch)

    def push(self, branch: int | None = None, **kw: Any) -> dict:
        return self.svc.push(alias="prod", project_root=self.root, branch_override=branch, **kw)

    def files(self) -> list[str]:
        return sorted(
            str(p.parent.relative_to(self.root))
            for p in self.root.rglob("_config.yml")
            if "rows" not in p.parts
        )

    def manifest(self) -> list[tuple[int, str, str]]:
        m = load_manifest(self.root)
        return [(c.branch_id, c.id, c.path) for c in m.configurations]

    def config_dir(self, path_fragment: str) -> Path:
        (d,) = (p.parent for p in self.root.rglob("_config.yml") if path_fragment in str(p))
        return d


def changes(d: dict) -> list[tuple[str, str]]:
    return [(c["change_type"], c.get("config_id", "")) for c in d["changes"]]


# ===========================================================================
# A -- remote delete+recreate under the same name/path: pull's stale sweep
#      deletes the freshly-written config, next push deletes it remotely.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 A: pull's stale-entry sweep (sync_service.py ~1062-1094) runs "
        "AFTER the fetch loop has written the new config to the same path "
        "(paths are per-pull, not globally unique). When a remote actor "
        "deletes a config and creates a new one of the same name, the new "
        "config lands on the old path, then the sweep rmtree's that same "
        "path for the vanished old id -- deleting the new files. The next "
        "push then classifies the (still manifest-tracked) new config as "
        "DELETED and destroys it on the remote. Confirmed live via "
        "Lean F8 + TLA I2 (independently found) and replayed against the "
        "real SyncService (scratchpad/replay/r_stale_sweep.py)."
    ),
)
def test_a_recreate_under_same_name_does_not_delete_new_config(tmp_path: Path) -> None:
    """Invariant: a config that a pull just fetched and wrote to disk must
    never be deleted -- locally or remotely -- by that same pull's stale-entry
    sweep, and a subsequent plain push must not delete a live remote config
    nobody removed locally.

    Issue #792 finding A.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()

    # Remote actor: delete "Orders" (cfg-1) and create a NEW config, also
    # named "Orders", so it lands on the same local path.
    w.api.remote[PROD].pop("cfg-1")
    w.api.put(PROD, "cfg-2", "Orders", "b")
    w.pull()

    # Safe expectation: the new config (cfg-2) is still on disk after the
    # pull that fetched it, and a subsequent push never deletes it remotely.
    assert any("orders" in f.lower() for f in w.files()), (
        "the freshly-pulled config was deleted by its own pull"
    )

    push_result = w.push()
    assert push_result.get("deleted", 0) == 0
    assert "cfg-2" in w.api.remote[PROD], "push deleted a live remote config nobody removed locally"


# ===========================================================================
# B -- pull compares only _config.yml; edits to companion files (SQL/code)
#      are silently overwritten, plain or --force, with no conflict.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 B: pull's 'locally modified' guard (sync_service.py:766-790, "
        "_sync_baseline.py:423-445) hashes only _config.yml. A companion "
        "file (transform.sql / code.py / _description.md) that was edited "
        "locally is not detected as modified, so a remote change to the same "
        "transformation silently overwrites the local SQL edit -- plain pull "
        "AND `pull --force` -- with no 'skipped' entry and no SYNC_CONFLICT. "
        "Confirmed via Lean F6 and replayed live (scratchpad/repro/"
        "test_lean_refutations.py::test_R1)."
    ),
)
def test_b_pull_never_overwrites_local_sql_edit(tmp_path: Path) -> None:
    """Invariant: a locally-edited companion file (here: transform.sql on a
    tracked SQL transformation) must never be silently overwritten by pull
    just because _config.yml itself is unchanged -- it must be treated the
    same as an edited _config.yml (skip, or SYNC_CONFLICT under --force).

    Issue #792 finding B.
    """
    w = World(tmp_path)
    comp = "keboola.snowflake-transformation"

    def sql_client_pull(sql_stmt: str) -> None:
        api_client = w.api.client()
        api_client.list_components_with_configs.side_effect = None
        api_client.list_components_with_configs.return_value = [
            {
                "id": comp,
                "type": "transformation",
                "configurations": [
                    {
                        "id": "t1",
                        "name": "My SQL",
                        "description": "",
                        "rows": [],
                        "configuration": {
                            "parameters": {
                                "blocks": [
                                    {"name": "B", "codes": [{"name": "C", "script": [sql_stmt]}]}
                                ]
                            }
                        },
                    }
                ],
            }
        ]
        w.svc = SyncService(config_store=w.store, client_factory=lambda url, token: api_client)

    sql_client_pull("SELECT 1;")
    w.svc.init_sync(alias="prod", project_root=w.root)
    w.svc.pull(alias="prod", project_root=w.root)

    sql_file = next(w.root.rglob("transform.sql"))
    sql_file.write_text(sql_file.read_text().replace("SELECT 1", "SELECT 42 /* my local edit */"))

    sql_client_pull("SELECT 100;")  # remote changed the SQL in the meantime
    w.svc.pull(alias="prod", project_root=w.root)

    assert "SELECT 42" in sql_file.read_text(), (
        "the local SQL edit was silently overwritten by pull"
    )


# ===========================================================================
# C -- remote delete + local edit: pull deletes the locally edited dir.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 C: pull's stale-entry sweep (sync_service.py:1062-1094) runs "
        "an unconditional rmtree for every manifest entry whose remote key "
        "vanished -- it never checks pull_hash against the current file "
        "content. A config edited locally and then deleted remotely is "
        "silently rmtree'd by the next pull (plain or --force), with no "
        "conflict raised even under --force (detect_force_pull_conflicts "
        "only iterates remote configs, _sync_baseline.py:485). Confirmed "
        "via Lean F7 + TLA I5 (independently found) and replayed "
        "(scratchpad/replay/r_misc.py)."
    ),
)
def test_c_pull_never_deletes_locally_edited_dir_on_remote_delete(tmp_path: Path) -> None:
    """Invariant: pull must never destroy a directory carrying an unpushed
    local edit just because the remote config was deleted in the meantime --
    at minimum it should be left alone (or flagged), never silently rmtree'd.

    Issue #792 finding C.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()

    config_dir = w.config_dir("orders")
    edited_file = config_dir / CONFIG_FILENAME
    data = yaml.safe_load(edited_file.read_text())
    data["parameters"]["value"] = "MY-UNPUSHED-EDIT"
    edited_file.write_text(yaml.dump(data, default_flow_style=False))

    w.api.remote[PROD].pop("cfg-1")
    w.pull()

    assert config_dir.exists(), "pull deleted a directory carrying an unpushed local edit"
    assert "MY-UNPUSHED-EDIT" in edited_file.read_text()


# ===========================================================================
# D -- `sync push --branch dev` (promote) re-creates the same config on
#      every push instead of being idempotent.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 D: when the target dev branch has no materialized subtree, "
        "push promotes main/ as the read source (KFR-07), but "
        "stamp_created_config's writeback only matches an existing manifest "
        "entry by (branch_id, component_id, path) (_sync_writeback.py:"
        "147-151). The promoted config's entry is written with branchId=dev, "
        "leaving the ORIGINAL main-branch entry (branchId=prod) untouched -- "
        "so it never resolves on dev and stays 'added' forever. Every "
        "`sync push --branch dev` after the first creates ANOTHER dev copy "
        "of the same config. Found via TLA I6/I1 and replayed "
        "(scratchpad/replay/r_misc.py). Whether the intent is 'promote once, "
        "then track on dev' is a product decision (see the finding's action "
        "column), but duplicating on every push is not intended."
    ),
)
def test_d_promote_push_is_idempotent(tmp_path: Path) -> None:
    """Invariant: promoting a production-only config to a dev branch via
    `sync push --branch <dev>` must be idempotent -- a second promote push
    with no further local change must not create a second dev copy of the
    same config.

    Issue #792 finding D.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()

    first = w.push(branch=DEV)
    assert first.get("created", 0) == 1
    assert len(w.api.remote[DEV]) == 1

    second = w.push(branch=DEV)
    assert second.get("created", 0) == 0, "a second promote push created another dev copy"
    assert len(w.api.remote[DEV]) == 1, "dev branch now holds more than one copy of the same config"


# ===========================================================================
# E -- an untracked file carrying `_keboola.config_id` (a `config new --push
#      --output-dir` scaffold, or an adopted orphan) is diffed 2-way, so
#      push silently overwrites a remote edit made after the scaffold was
#      written -- and two copies of it both "adopt" the same remote id.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 E: classify_untracked ADOPTs a file carrying "
        "`_keboola.config_id` (branch_scope.py:275-276), but base_hashes is "
        "built from in-tree manifest entries only (sync_service.py:"
        "1441-1452). With no base, compute_changeset's 2-way fallback turns "
        "ANY difference into 'modified' (diff_engine.py:388-391) -- so a "
        "remote edit made after the scaffold was written (e.g. in the web "
        "UI) is silently reverted by the next `sync push`, no conflict "
        "shown. Found via TLA I11 and replayed "
        "(scratchpad/replay/r_adopt_lost_update.py)."
    ),
)
def test_e_adopted_scaffold_push_does_not_overwrite_remote_edit(tmp_path: Path) -> None:
    """Invariant: a `sync push` must never silently revert a remote edit made
    to a config in between that config's on-disk scaffold being written and
    the next push, even when the local file is untracked-but-adopts-by-id.

    Issue #792 finding E.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()

    # Emulate the `config new --push --output-dir` scaffold shape: a file on
    # disk carrying the config id, with NO manifest entry (#644).
    m = load_manifest(w.root)
    m.configurations = []
    save_manifest(w.root, m)

    # The config is edited remotely (e.g. in the web UI) after the scaffold
    # was written, before the next push.
    w.api.put(PROD, "cfg-1", "Orders", "EDITED-IN-UI")

    w.push()

    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "EDITED-IN-UI", (
        "push silently reverted a remote edit made to an adopted-by-id scaffold config"
    )


# ===========================================================================
# F -- a push aborted by ENCRYPTION_FAILED leaves the manifest unsaved, so a
#      change it already applied (a resurrect CREATE) is re-applied by the
#      retry, duplicating it.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 F: push() re-raises ENCRYPTION_FAILED instead of continuing "
        "(sync_service.py:1842-1852), but save_manifest only runs after the "
        "whole Phase A/B/C/D loop completes (:1946) -- so a CREATE applied "
        "to the remote before the failing change is never recorded in the "
        "manifest. Re-running push after the encryption problem is fixed "
        "creates that same config again, duplicating it. This is a model "
        "trace only in the original TLA pilot (I1(b), not previously "
        "replayed against real SyncService); replayed here directly by "
        "making the second config's secret fail Encryption API mock."
    ),
)
def test_f_aborted_push_does_not_duplicate_already_created_config(tmp_path: Path) -> None:
    """Invariant: retrying a push after an ENCRYPTION_FAILED abort must not
    re-apply a change (here: a resurrect CREATE) that the aborted push had
    already sent to the remote before it failed.

    Issue #792 finding F.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a", extra={"#token": "ok-secret"})
    w.api.put(PROD, "cfg-2", "Contacts", "b", extra={"#token": "will-fail"})
    w.init()
    w.pull()

    # Remote deletes cfg-1 (resurrect precondition): local file untouched.
    w.api.remote[PROD].pop("cfg-1")
    # Local edit to cfg-2's secret triggers the encryption failure below.
    contacts_file = w.config_dir("contacts") / CONFIG_FILENAME
    data = yaml.safe_load(contacts_file.read_text())
    data["parameters"]["#token"] = "FAIL_MARKER"
    contacts_file.write_text(yaml.dump(data, default_flow_style=False))

    def flaky_encrypt(project_id: int, component_id: str, data: dict[str, str]) -> dict[str, str]:
        if "FAIL_MARKER" in data.values():
            raise RuntimeError("Encryption API unavailable")
        return {k: f"KBC::Encrypted=={v}" for k, v in data.items()}

    w.api.encrypt_values = flaky_encrypt

    with pytest.raises(KeboolaApiError) as exc_info:
        w.push()
    assert exc_info.value.error_code == ErrorCode.ENCRYPTION_FAILED

    creates_before_retry = [line for line in w.api.log if line.startswith("CREATE")]

    # Fix the encryption problem and retry, exactly as a user would.
    contacts_file.write_text(
        yaml.dump(
            {**data, "parameters": {**data["parameters"], "#token": "fixed-secret"}},
            default_flow_style=False,
        )
    )
    w.api.encrypt_values = lambda project_id, component_id, data: {
        k: f"KBC::Encrypted=={v}" for k, v in data.items()
    }
    w.push()

    creates_after_retry = [line for line in w.api.log if line.startswith("CREATE")]
    # Safe expectation: the resurrect CREATE for "Orders" happened exactly
    # once across both push attempts, not once per attempt.
    assert len(creates_before_retry) == 1, "expected exactly one CREATE before the encryption abort"
    assert len(creates_after_retry) == len(creates_before_retry), (
        "retrying the push after the encryption fix duplicated the already-applied resurrect CREATE: "
        f"log={w.api.log}"
    )


# ===========================================================================
# G -- `sync push` deletes a remote config with no `--force`, contradicting
#      the CLI's own help text ("--force: allow deletion of remote configs
#      removed locally"). Product decision (soft-delete to trash since
#      0.89.0, so it is recoverable) -- xfail per the task's own note.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 G: SyncService.push() accepts `force` but never reads it in "
        "its body (grep sync_service.py:1574-1966) -- only pull()'s conflict "
        "guard does. The CLI help text for `sync push --force` "
        "(commands/sync.py:982-986) promises 'Allow deletion of remote "
        "configs that were removed locally', implying a plain push should "
        "NOT delete, but push deletes a remote config the instant its local "
        "directory is missing, force=True or not. This is a PRODUCT "
        "DECISION (the delete is soft, into the Storage trash, since "
        "0.89.0, so it is recoverable via `sync restore`) -- kept xfail per "
        "issue #792 rather than resolved either way here."
    ),
)
def test_g_push_without_force_does_not_delete_remote_config(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """CLI-contract invariant: `sync push` (no --force) must not delete a
    remote config just because its local directory was removed -- --force
    is documented as the gate for that.

    Issue #792 finding G.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

    manifest = load_manifest(project_root)
    cfg = next(c for c in manifest.configurations if c.component_id == "keboola.ex-http")
    config_dir = project_root / "main" / cfg.path
    (config_dir / CONFIG_FILENAME).unlink()

    svc, client = _svc_with_client(store, SAMPLE_COMPONENTS_NO_ROWS)
    push_result = svc.push(alias="prod", project_root=project_root, force=False)

    client.delete_config.assert_not_called()
    assert push_result.get("deleted", 0) == 0


# ===========================================================================
# H -- a config deleted remotely by another actor is silently re-created
#      (no warning) by the next push, even with no local change.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 H: compute_changeset routes ANY local entry whose remote_key "
        "is absent into 'added' (diff_engine.py:355), regardless of whether "
        "the id was previously tracked. A tracked config deleted/trashed "
        "remotely by another actor -- with NO local change at all -- is "
        "silently recreated under a brand-new id on the next push, with no "
        "warning distinguishing it from a genuinely new config. Confirmed "
        "via spec S2, Lean F2 and TLA I11b (three independent hits) and "
        "replayed (scratchpad/repro/test_lean_refutations.py::test_R6, "
        "scratchpad/replay/r_misc.py)."
    ),
)
def test_h_push_does_not_silently_resurrect_deleted_config(tmp_path: Path) -> None:
    """Invariant: push must not silently POST a fresh create for a config
    another actor deleted remotely when the local user made no change to it
    at all -- it should surface a warning/orphan report instead of a
    business-as-usual CREATE.

    Issue #792 finding H.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()

    w.api.remote[PROD].pop("cfg-1")  # another actor deletes it; local untouched

    result = w.push()

    assert result.get("created", 0) == 0, (
        "push silently recreated a remotely-deleted, locally-untouched config"
    )
    assert "cfg-1" not in w.api.remote[PROD]


# ===========================================================================
# I -- moving/renaming a config's directory by hand (`mv` / `git mv`) is
#      seen as DELETE + CREATE, minting a brand-new remote config id.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 I: the old path's manifest entry has no file left, so it is "
        "classified 'deleted'; the moved directory carries the same "
        "_keboola.config_id but a same-tree claim is always CREATE by "
        "design (branch_scope.py:273-274, the #482/#497 fork-by-copy "
        "protection). A hand `mv`/`git mv` of a tracked config directory "
        "therefore destroys the original remote config id (job history, "
        "schedules, flow references) and mints a new one -- with no warning "
        "that `config rename --directory` was the supported path. Confirmed "
        "via Lean F3 and replayed "
        "(scratchpad/repro/test_lean_refutations.py::test_R4)."
    ),
)
def test_i_moving_config_dir_is_not_delete_plus_create(tmp_path: Path) -> None:
    """Invariant: renaming a tracked config's directory on disk (outside of
    `config rename --directory`) must not be classified as delete-the-old
    plus create-a-new-id -- the config's identity should survive a plain
    filesystem move.

    Issue #792 finding I.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()

    original_dir = w.config_dir("orders")
    shutil.move(str(original_dir), str(original_dir.parent / "orders-renamed"))

    diff_result = w.diff()
    change_types = sorted(c["change_type"] for c in diff_result["changes"])

    assert change_types != ["added", "deleted"], (
        "a plain directory move was classified as delete + create"
    )


# ===========================================================================
# J -- `sync pull --branch dev` reports untouched production configs as
#      "removed" because the stale-entry sweep is unscoped by branch tree.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#792 J: pull()'s stale-entry sweep (sync_service.py ~1063) compares "
        "the freshly-fetched keys against the FULL old manifest."
        "configurations, not scoped to the branch just pulled. A `sync pull "
        "--branch dev` after a production pull reports every untouched "
        "production entry as 'removed' -- the exact label sync-workflow.md "
        "documents as meaning 'the config was genuinely deleted on the "
        "remote', which it was not. Confirmed via spec S4."
    ),
)
def test_j_branch_scoped_pull_does_not_report_other_branch_configs_removed(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Invariant: pulling a different branch than the one the manifest
    currently reflects must never report an untouched, still-live config on
    the ORIGINAL branch as `"removed"` -- that label is documented to mean
    the remote genuinely deleted it.

    Issue #792 finding J.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

    # Dev branch fetch does not carry keboola.ex-http/cfg-001 at all (a
    # different component entirely) -- production's cfg-001 is untouched and
    # still live, just not part of THIS fetch.
    dev_components = [
        {
            "id": "keboola.snowflake-transformation",
            "type": "transformation",
            "configurations": [
                {
                    "id": "cfg-dev-only",
                    "name": "Dev Only",
                    "description": "",
                    "configuration": {"parameters": {}},
                    "rows": [],
                }
            ],
        }
    ]
    dev_client = _make_mock_client(
        components_response=dev_components,
        branches_response=SAMPLE_BRANCHES_WITH_DEV,
    )
    dev_svc = SyncService(config_store=store, client_factory=lambda url, token: dev_client)
    result = dev_svc.pull(alias="prod", project_root=project_root, branch_override=99999)

    removed = [d for d in result["details"] if d["action"] == "removed"]
    assert removed == [], (
        "production's cfg-001 was reported 'removed' by a dev-branch pull that never touched production"
    )


# ===========================================================================
# K -- a cosmetic local edit (raw-hash change, not a semantic change) blocks
#      pull from ever applying a real remote change. Documented, intentional
#      conservative behavior -- NOT a safety violation, so this is a plain
#      (unmarked) regression guard on the safe half of the trade-off.
# ===========================================================================


def test_k_cosmetic_edit_is_conservative_not_unsafe(tmp_config_dir: Path, tmp_path: Path) -> None:
    """NOT a safety violation: pull's raw-file-hash check is documented,
    intentional, conservative behavior ("Pull protects local edits:
    locally-modified files are skipped by default",
    plugins/kbagent/skills/kbagent/references/sync-workflow.md). A purely
    cosmetic edit (here: an appended YAML comment, which changes the raw
    file hash but not `config_hash`) is still treated as "locally modified"
    and the file is preserved rather than overwritten with the real remote
    change underneath it.

    This regression test asserts the SAFE half of that trade-off: pull
    never silently discards/corrupts local content, even when it happens to
    be byte-different-but-semantically-identical to the last pulled base.
    The liveness cost (a real remote change is stuck until the cosmetic
    edit is reverted, or `--theirs` is used) is a deliberate, documented
    trade-off, not the safety property this pilot models -- issue #792
    finding K (spec S3, TLA I12).
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, SAMPLE_COMPONENTS_NO_ROWS)

    manifest = load_manifest(project_root)
    cfg = next(c for c in manifest.configurations if c.component_id == "keboola.ex-http")
    config_file = project_root / "main" / cfg.path / CONFIG_FILENAME
    original_bytes = config_file.read_bytes()

    # Purely cosmetic edit: append a YAML comment. Parses to the identical
    # dict (config_hash unchanged) but the raw file hash changes.
    config_file.write_bytes(original_bytes + b"\n# cosmetic comment, no semantic change\n")

    # Remote genuinely changes in the meantime.
    base_component: dict[str, Any] = SAMPLE_COMPONENTS_NO_ROWS[0]
    base_config: dict[str, Any] = base_component["configurations"][0]
    changed_remote = [
        {
            **base_component,
            "configurations": [
                {
                    **base_config,
                    "configuration": {
                        "parameters": {"baseUrl": "https://real-remote-change.example.com"}
                    },
                }
            ],
        }
    ]
    svc, _ = _svc_with_client(store, changed_remote)
    result = svc.pull(alias="prod", project_root=project_root)

    # Safe: the cosmetically-edited file is preserved verbatim, never
    # silently clobbered by the remote write.
    after = config_file.read_text(encoding="utf-8")
    assert "cosmetic comment" in after
    detail = next(d for d in result["details"] if d["component_id"] == "keboola.ex-http")
    assert detail["action"] == "skipped"
    assert detail["reason"] == "locally modified"
