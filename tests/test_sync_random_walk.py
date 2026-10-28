"""Seeded random-walk (model-based) test of ``kbagent sync`` hunting data loss.

Why: the #792 formal pilot modelled a config as ONE file and so missed the
#917 bug (an edit to ``transform.sql`` after a create was invisible). This
walk treats a config as the SET of files pull writes -- ``_config.yml``,
``_description.md``, ``transform.sql`` / ``transform.py`` / ``code.py`` /
``pyproject.toml`` and ``rows/*/_config.yml`` -- across four components
(ex-http with rows, snowflake + python transformations, custom python app)
and two branch trees (``main/`` = production, ``dev/`` = a dev branch that
started as a copy of production).

How: the real ``SyncService`` runs against ``FakeApi``, a branch-aware
in-memory Storage API (configs, rows, description, isDisabled). A seed
expands into a fixed list of steps (``generate_steps``): local edits of a
random file, new config dirs (copied, ids stripped), deletes, moves, toggles;
remote (UI) edits, creates, deletes, renames, toggles, row add/delete; and
``pull`` / ``pull --force`` / ``pull --theirs`` / ``diff`` / ``push`` /
``push --force`` / ``push --branch DEV`` / ``pull --branch DEV``. Documented
refusals (``SyncConflictError``, ``ConfigError``, ``KeboolaApiError``) are
legal outcomes; any other exception is a finding.

Every value the walk writes carries a unique token (``tk<n>z``), so a
config's content compares the same way on disk and on the remote
(``local_projection`` / ``remote_projection``), independent of formatting.
Invariants checked after every step (``Sim.step``):

1. no lost edit -- a local edit not yet delivered to its tree's branch is
   still on disk, except after ``pull --theirs`` of that tree;
2./3./5. diff is truthful -- after every pull/push, ``sync diff`` lists every
   config whose local and remote content differ (a pending edit, an edit
   push failed to deliver, a remote change) and lists nothing as changed
   whose content is equal (phantom drift);
4. no remote clobber -- a push never overwrites or deletes a remote change
   made since the tree last pulled that branch, and a plain push deletes
   nothing.

Known findings are steered around by ``MASKS`` so the CI walk stays green;
each has a shrunk, deterministic ``xfail(strict=True)`` test at the bottom.

Not modelled: git branching, folders, variables / shared code / flows,
schedules, data apps, secrets/encryption, SQL workspaces, storage metadata,
transient API failures or retries, and concurrent processes.

Hunting locally::

    SYNC_WALK_SEEDS=500 SYNC_WALK_STEPS=30 uv run pytest tests/test_sync_random_walk.py -k walk -n 8
    SYNC_WALK_UNMASK=all SYNC_WALK_SEEDS=50 uv run pytest tests/test_sync_random_walk.py -k walk
"""

from __future__ import annotations

import contextlib
import copy
import itertools
import json
import os
import random
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError, SyncConflictError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService

PROD = 12345
DEV = 99999

HTTP = "keboola.ex-http"
SQL = "keboola.snowflake-transformation"
PYT = "keboola.python-transformation-v2"
APP = "kds-team.app-custom-python"
COMPONENT_TYPES = {
    HTTP: "extractor",
    SQL: "transformation",
    PYT: "transformation",
    APP: "application",
}


class FakeApi:
    """Branch-aware in-memory Storage API with several components and rows.

    ``remote[branch][(component_id, config_id)] = config`` where a config has
    the shape the Storage API lists (``id``, ``name``, ``description``,
    ``configuration``, ``rows``, ``isDisabled``).
    """

    def __init__(self) -> None:
        self.remote: dict[int, dict[tuple[str, str], dict[str, Any]]] = {PROD: {}, DEV: {}}
        self._ids = itertools.count(1000)

    def new_id(self) -> str:
        return str(next(self._ids))

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
        c.create_config_row.side_effect = self._create_row
        c.update_config_row.side_effect = self._update_row
        c.delete_config_row.side_effect = self._delete_row
        c.get_config_row.side_effect = self._get_row
        return c

    @staticmethod
    def _b(branch_id: int | None) -> int:
        return PROD if branch_id in (None, 0, PROD) else int(branch_id)

    def _list(self, branch_id: int | None = None, **_: Any) -> list[dict[str, Any]]:
        by_comp: dict[str, list[dict[str, Any]]] = {}
        for (comp, _cid), cfg in sorted(self.remote[self._b(branch_id)].items()):
            by_comp.setdefault(comp, []).append(copy.deepcopy(cfg))
        return [
            {"id": comp, "type": COMPONENT_TYPES[comp], "configurations": cfgs}
            for comp, cfgs in sorted(by_comp.items())
        ]

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
        cid = self.new_id()
        cfg = {
            "id": cid,
            "name": name,
            "description": description or "",
            "configuration": copy.deepcopy(configuration),
            "rows": [],
            "isDisabled": bool(is_disabled),
        }
        self.remote[self._b(branch_id)][(component_id, cid)] = cfg
        return copy.deepcopy(cfg)

    def _cfg(self, component_id: str, config_id: str, branch_id: int | None) -> dict:
        try:
            return self.remote[self._b(branch_id)][(component_id, str(config_id))]
        except KeyError:
            raise KeboolaApiError(
                f"Configuration {config_id} not found", 404, ErrorCode.NOT_FOUND
            ) from None

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
        cfg = self._cfg(component_id, config_id, branch_id)
        if configuration is not None:
            cfg["configuration"] = copy.deepcopy(configuration)
        if name is not None:
            cfg["name"] = name
        if description is not None:
            cfg["description"] = description
        if is_disabled is not None:
            cfg["isDisabled"] = bool(is_disabled)
        return copy.deepcopy(cfg)

    def _delete(self, component_id: str, config_id: str, branch_id: int | None = None) -> None:
        self._cfg(component_id, config_id, branch_id)
        self.remote[self._b(branch_id)].pop((component_id, str(config_id)))

    def _detail(self, component_id: str, config_id: str, branch_id: int | None = None, **_: Any):
        return copy.deepcopy(self._cfg(component_id, config_id, branch_id))

    def _create_row(
        self,
        component_id: str,
        config_id: str,
        name: str,
        configuration: dict,
        description: str = "",
        is_disabled: bool = False,
        branch_id: int | None = None,
        **_: Any,
    ) -> dict:
        cfg = self._cfg(component_id, config_id, branch_id)
        row = {
            "id": self.new_id(),
            "name": name,
            "description": description or "",
            "configuration": copy.deepcopy(configuration),
            "isDisabled": bool(is_disabled),
        }
        cfg["rows"].append(row)
        return copy.deepcopy(row)

    def _row(self, component_id: str, config_id: str, row_id: str, branch_id: int | None) -> dict:
        cfg = self._cfg(component_id, config_id, branch_id)
        rows = [r for r in cfg["rows"] if r["id"] == str(row_id)]
        if not rows:
            raise KeboolaApiError(f"Row {row_id} not found", 404, ErrorCode.NOT_FOUND)
        return rows[0]

    def _update_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        name: str | None = None,
        configuration: dict | None = None,
        description: str | None = None,
        is_disabled: bool | None = None,
        change_description: str = "",
        branch_id: int | None = None,
        **_: Any,
    ) -> dict:
        row = self._row(component_id, config_id, row_id, branch_id)
        if configuration is not None:
            row["configuration"] = copy.deepcopy(configuration)
        if name is not None:
            row["name"] = name
        if description is not None:
            row["description"] = description
        if is_disabled is not None:
            row["isDisabled"] = bool(is_disabled)
        return copy.deepcopy(row)

    def _delete_row(
        self, component_id: str, config_id: str, row_id: str, branch_id: int | None = None, **_: Any
    ) -> None:
        cfg = self._cfg(component_id, config_id, branch_id)
        cfg["rows"] = [r for r in cfg["rows"] if r["id"] != str(row_id)]

    def _get_row(
        self, component_id: str, config_id: str, row_id: str, branch_id: int | None = None, **_: Any
    ) -> dict:
        return copy.deepcopy(self._row(component_id, config_id, row_id, branch_id))


@dataclass(frozen=True)
class ServiceFixture:
    """A SyncService bound to *api* and the sync working tree it operates on."""

    svc: SyncService
    root: Path


def make_service(tmp: Path, api: FakeApi) -> ServiceFixture:
    root = tmp / "project"
    root.mkdir(parents=True)
    cfgdir = tmp / "cfg"
    cfgdir.mkdir()
    store = setup_single_project(cfgdir)

    def client_factory(url: str, token: str) -> Any:
        return api.client()

    svc = SyncService(config_store=store, client_factory=client_factory)
    return ServiceFixture(svc=svc, root=root)


# ===========================================================================
# Projection: what a config "says", read the same way from disk and from API
# ===========================================================================

TOKEN_RE = re.compile(r"tk\d+z")


def tokens(text: Any) -> tuple[str, ...]:
    return tuple(TOKEN_RE.findall(text if isinstance(text, str) else json.dumps(text)))


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# slot -> file that holds it on disk
SLOT_FILE = {
    "value": "_config.yml",
    "name": "_config.yml",
    "desc": "_description.md",
    "sql": "transform.sql",
    "py": "transform.py",
    "pkg": "pyproject.toml",
    "code": "code.py",
}
COMPONENT_SLOTS = {
    HTTP: ("value", "desc", "name"),
    SQL: ("sql", "desc", "name"),
    PYT: ("py", "pkg", "desc", "name"),
    APP: ("code", "pkg", "desc", "name"),
}


def local_projection(config_dir: Path) -> dict[str, Any]:
    data = read_yaml(config_dir / "_config.yml")
    comp = (data.get("_keboola") or {}).get("component_id", "")
    params = data.get("parameters") or {}
    proj: dict[str, Any] = {
        "name": data.get("name", ""),
        "disabled": bool(data.get("is_disabled", False)),
    }
    desc_file = config_dir / "_description.md"
    proj["desc"] = tokens(_read(desc_file) if desc_file.exists() else data.get("description", ""))
    if comp == HTTP:
        proj["value"] = tokens(str(params.get("value", "")))
        rows_dir = config_dir / "rows"
        if rows_dir.is_dir():
            for row_file in sorted(rows_dir.glob("*/_config.yml")):
                row = read_yaml(row_file)
                rparams = row.get("parameters") or {}
                proj[f"row:{row.get('name', '')}"] = tokens(str(rparams.get("value", "")))
    for slot in ("sql", "py", "code"):
        if slot in COMPONENT_SLOTS.get(comp, ()):
            proj[slot] = tokens(_read(config_dir / SLOT_FILE[slot]))
    if "pkg" in COMPONENT_SLOTS.get(comp, ()):
        deps = re.search(r"(?ms)^dependencies = \[\n(.*?)^\]", _read(config_dir / "pyproject.toml"))
        proj["pkg"] = tokens(deps.group(1) if deps else "")
    return proj


def remote_projection(comp: str, cfg: dict[str, Any]) -> dict[str, Any]:
    params = (cfg.get("configuration") or {}).get("parameters") or {}
    proj: dict[str, Any] = {
        "name": cfg.get("name", ""),
        "disabled": bool(cfg.get("isDisabled", False)),
        "desc": tokens(cfg.get("description") or ""),
    }
    if comp == HTTP:
        proj["value"] = tokens(str(params.get("value", "")))
        for row in cfg.get("rows", []):
            rparams = (row.get("configuration") or {}).get("parameters") or {}
            proj[f"row:{row.get('name', '')}"] = tokens(str(rparams.get("value", "")))
    if comp in (SQL,):
        proj["sql"] = tokens(params.get("blocks", []))
    if comp == PYT:
        proj["py"] = tokens(params.get("blocks", []))
    if comp == APP:
        proj["code"] = tokens(params.get("code", ""))
    if comp in (PYT, APP):
        proj["pkg"] = tokens(params.get("packages", []))
    return proj


def config_dirs(tree: Path) -> list[Path]:
    """Every config directory (not row dirs) in one branch tree."""
    if not tree.is_dir():
        return []
    return sorted(
        p.parent for p in tree.rglob("_config.yml") if "rows" not in p.relative_to(tree).parts[:-1]
    )


def dir_identity(config_dir: Path) -> tuple[str, str]:
    meta = read_yaml(config_dir / "_config.yml").get("_keboola") or {}
    return str(meta.get("component_id", "")), str(meta.get("config_id", "") or "")


# ===========================================================================
# The walk
# ===========================================================================

TREE_BRANCH = {"main": PROD, "dev": DEV}
BRANCH_TREE = {PROD: "main", DEV: "dev"}

# (action, weight)
ACTIONS: list[tuple[str, int]] = [
    ("local_edit", 30),
    ("local_new", 4),
    ("local_delete", 3),
    ("local_move", 1),
    ("local_toggle", 2),
    ("local_desc_clear", 1),
    ("remote_edit", 16),
    ("remote_create", 3),
    ("remote_delete", 2),
    ("remote_toggle", 2),
    ("remote_rename", 2),
    ("remote_row_add", 1),
    ("remote_row_delete", 1),
    ("pull", 10),
    ("pull_force", 5),
    ("pull_theirs", 1),
    ("diff", 5),
    ("push", 12),
    ("push_force", 2),
    ("push_dev", 3),
    ("pull_dev", 2),
]

Step = tuple[str, float, float, float]

REFUSALS = (SyncConflictError, ConfigError, KeboolaApiError)


class Violation(AssertionError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.step_index: int | None = None


def generate_steps(seed: int, n: int, actions: list[tuple[str, int]] | None = None) -> list[Step]:
    rng = random.Random(seed)
    table = actions or ACTIONS
    names = [a for a, _ in table]
    weights = [w for _, w in table]
    return [
        (rng.choices(names, weights)[0], rng.random(), rng.random(), rng.random()) for _ in range(n)
    ]


def _pick(seq: list[Any], f: float) -> Any:
    return seq[min(int(f * len(seq)), len(seq) - 1)] if seq else None


class Sim:
    """A sync working tree + fake remote + the shadow model of intent."""

    def __init__(self, tmp: Path, masks: frozenset[str] = frozenset()) -> None:
        # Known findings whose trigger the walk steers around (see MASKS).
        self.masks = masks
        self.api = FakeApi()
        fixture = make_service(tmp, self.api)
        self.svc, self.root = fixture.svc, fixture.root
        self._tok = itertools.count(1)
        # Plain (no --branch) commands target production: there is no
        # `branch use` in this model.
        self.home = PROD
        # True once the tree has a baseline of the dev branch (pull or push
        # --branch DEV). Before that a promote push overwrites the dev branch
        # by design, so dev-side UI edits are not tracked as intent.
        self.dev_synced = False
        self.on_dev = False  # the manifest was last re-targeted to DEV
        # local intent: token -> (tree, file name) the user wrote it into and
        # has not yet delivered to that tree's branch
        self.pending: dict[str, tuple[str, str]] = {}
        # remote intent: (branch, comp, cid, slot) -> value written remotely
        # and not yet pulled into that branch's tree
        self.remote_pending: dict[tuple[int, str, str, str], Any] = {}
        self.log: list[str] = []

    # -- helpers ---------------------------------------------------------
    def tok(self) -> str:
        return f"tk{next(self._tok)}z"

    def seed_remote(self) -> None:
        for comp in (HTTP, HTTP, SQL, SQL, PYT, APP):
            self._remote_new_config(PROD, comp)
        # The dev branch was created after these configs existed: Storage
        # copies them under the same ids.
        self.api.remote[DEV] = copy.deepcopy(self.api.remote[PROD])

    def _remote_new_config(self, branch: int, comp: str) -> str:
        cid = self.api.new_id()
        t = self.tok
        params: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        if comp == HTTP:
            params = {"value": t()}
            rows = [self._remote_row(f"Row {t()}") for _ in range(2)]
        elif comp == SQL:
            params = {
                "blocks": [{"name": "B", "codes": [{"name": "C", "script": [f"SELECT '{t()}';"]}]}]
            }
        elif comp == PYT:
            params = {
                "blocks": [{"name": "B", "codes": [{"name": "C", "script": [f"print('{t()}')"]}]}],
                "packages": [f"pkg{t()}"],
            }
        elif comp == APP:
            params = {"code": f"print('{t()}')\n", "packages": [f"pkg{t()}"]}
        self.api.remote[branch][(comp, cid)] = {
            "id": cid,
            "name": f"Name {t()}",
            "description": f"Description {t()}" if comp != SQL else "",
            "configuration": {"parameters": params},
            "rows": rows,
            "isDisabled": False,
        }
        return cid

    def _remote_row(self, name: str) -> dict[str, Any]:
        return {
            "id": self.api.new_id(),
            "name": name,
            "description": "",
            "configuration": {"parameters": {"value": self.tok()}},
            "isDisabled": False,
        }

    def tree(self, branch: int | None = None) -> Path:
        return self.root / BRANCH_TREE[self.home if branch is None else branch]

    def local_tree(self, f: float) -> Path:
        """Users mostly work in main/; in dev/ once a dev pull made one."""
        dev = self.tree(DEV)
        if self.work_on_dev():
            return dev
        if "pull-dev-orphans-main" in self.masks:
            return self.tree(PROD)
        return dev if f > 0.75 and dev.is_dir() else self.tree(PROD)

    def work_on_dev(self) -> bool:
        """Masked mode: after `pull --branch DEV` the user follows the
        documented flow and works in dev/ with `--branch DEV` until a plain
        pull re-targets the manifest to production."""
        return "pull-dev-orphans-main" in self.masks and self.on_dev

    def note(self, msg: str) -> None:
        self.log.append(msg)

    # -- local actions ----------------------------------------------------
    def _local_edit_slot(self, d: Path, slot: str, track: bool = True) -> None:
        tree = d.relative_to(self.root).parts[0]
        before = set(tokens(local_projection(d)))
        new = self.tok()
        fname = SLOT_FILE.get(slot, "_config.yml")
        if slot.startswith("row:"):
            row_file = d / "rows" / slot[4:] / "_config.yml"
            data = read_yaml(row_file)
            data.setdefault("parameters", {})
            if not isinstance(data["parameters"], dict):
                data["parameters"] = {}
            data["parameters"]["value"] = new
            write_yaml(row_file, data)
            fname = "_config.yml"
        elif slot in ("value", "name"):
            data = read_yaml(d / "_config.yml")
            if slot == "value":
                params = data.get("parameters")
                if not isinstance(params, dict):
                    params = {}
                params["value"] = new
                data["parameters"] = params
            else:
                data["name"] = f"Name {new}"
            write_yaml(d / "_config.yml", data)
        elif slot == "desc":
            if (
                "new-description-file" in self.masks
                and not (d / "_description.md").exists()
                and not read_yaml(d / "_config.yml").get("description")
            ):
                return
            (d / "_description.md").write_text(f"Description {new}", encoding="utf-8")
        else:
            path = d / fname
            text = _read(path)
            if slot == "pkg":
                if '"pkg' not in text:
                    return
                path.write_text(re.sub(r'"pkgtk\d+z"', f'"pkg{new}"', text), encoding="utf-8")
            elif TOKEN_RE.search(text):
                path.write_text(TOKEN_RE.sub(new, text), encoding="utf-8")
            elif slot == "sql":
                path.write_text(text + f"SELECT '{new}';\n", encoding="utf-8")
            elif slot in ("py", "code"):
                path.write_text(text + f"print('{new}')\n", encoding="utf-8")
            else:
                return
        after = set(tokens(local_projection(d)))
        for gone in before - after:  # the user overwrote their own edit
            self.pending.pop(gone, None)
        if track:
            self.pending[new] = (tree, fname)
        self.note(f"  local edit {d.relative_to(self.root)} [{slot}] -> {new}")

    def _slots_of(self, d: Path) -> list[str]:
        comp, _ = dir_identity(d)
        slots = list(COMPONENT_SLOTS.get(comp, ("name",)))
        rows = d / "rows"
        if rows.is_dir():
            slots += [
                f"row:{p.name}" for p in sorted(rows.iterdir()) if (p / "_config.yml").exists()
            ]
        return slots

    def local_edit(self, a: float, b: float, c: float) -> None:
        d = _pick(config_dirs(self.local_tree(c)), a)
        if d is None:
            return
        slot = _pick(self._slots_of(d), b)
        # a local rename only changes the name field; keep it rare
        if slot == "name" and (b < 0.9 or "rename-across-trees" in self.masks):
            slot = self._slots_of(d)[0]
        self._local_edit_slot(d, slot)

    def local_new(self, a: float, _b: float, c: float) -> None:
        src = _pick(config_dirs(self.local_tree(c)), a)
        if src is None:
            return
        dst = src.parent / f"new-{self.tok()}"
        shutil.copytree(src, dst)
        if "new-config-rows" in self.masks:
            shutil.rmtree(dst / "rows", ignore_errors=True)
        for yml in dst.rglob("_config.yml"):
            data = read_yaml(yml)
            meta = data.get("_keboola") or {}
            meta.pop("config_id", None)
            meta.pop("row_id", None)
            meta.pop("base_config_hash", None)
            data["_keboola"] = meta
            write_yaml(yml, data)
        pyproject = dst / "pyproject.toml"
        if pyproject.exists():
            pyproject.write_text(
                re.sub(r'(?m)^config_id = ".*"\n', "", _read(pyproject)), encoding="utf-8"
            )
        self.note(f"  local new {dst.relative_to(self.root)} (copied from {src.name})")
        # Fresh content in every slot, a unique name included.
        for slot in self._slots_of(dst):
            self._local_edit_slot(dst, slot)
        if "rename-across-trees" in self.masks:
            # Name the directory the way pull would name the config.
            name = str(read_yaml(dst / "_config.yml").get("name", ""))
            named = dst.parent / name.lower().replace(" ", "-")
            shutil.move(str(dst), str(named))

    def _drop_pending_under(self, d: Path) -> None:
        for token, (_tree, fname) in list(self.pending.items()):
            if any(token in _read(p) for p in d.rglob(fname)):
                del self.pending[token]

    def local_delete(self, a: float, _b: float, c: float) -> None:
        d = _pick(config_dirs(self.local_tree(c)), a)
        if d is None:
            return
        self._drop_pending_under(d)
        shutil.rmtree(d)
        self.note(f"  local delete {d.relative_to(self.root)}")

    def local_move(self, a: float, _b: float, c: float) -> None:
        if "move-is-delete-create" in self.masks:
            return
        d = _pick(config_dirs(self.local_tree(c)), a)
        if d is None:
            return
        dst = d.parent / f"{d.name}-mv"
        if dst.exists():
            return
        shutil.move(str(d), str(dst))
        self.note(f"  local move {d.relative_to(self.root)} -> {dst.name}")

    def local_toggle(self, a: float, _b: float, c: float) -> None:
        d = _pick(config_dirs(self.local_tree(c)), a)
        if d is None:
            return
        data = read_yaml(d / "_config.yml")
        if data.get("is_disabled"):
            # Pull writes the flag sparsely, so deleting the line is the
            # natural way to re-enable -- push then leaves the remote alone.
            if "sparse-disabled-reenable" in self.masks:
                data["is_disabled"] = False
            else:
                data.pop("is_disabled")
        else:
            data["is_disabled"] = True
        write_yaml(d / "_config.yml", data)
        self.note(
            f"  local toggle disabled {d.relative_to(self.root)} -> {data.get('is_disabled', False)}"
        )

    def local_desc_clear(self, a: float, _b: float, c: float) -> None:
        if "retarget-reuses-other-tree-baseline" in self.masks:
            return
        d = _pick(config_dirs(self.local_tree(c)), a)
        if d is None:
            return
        self._drop_pending_under_file(d / "_description.md")
        (d / "_description.md").unlink(missing_ok=True)
        data = read_yaml(d / "_config.yml")
        if data.get("description"):
            data["description"] = ""
            write_yaml(d / "_config.yml", data)
        self.note(f"  local clear description {d.relative_to(self.root)}")

    def _drop_pending_under_file(self, path: Path) -> None:
        text = _read(path)
        for token in list(self.pending):
            if token in text:
                del self.pending[token]

    # -- remote actions -----------------------------------------------------
    def _remote_branch(self, f: float) -> int:
        return DEV if f > 0.8 else PROD

    def _remote_pick(self, branch: int, f: float) -> tuple[str, str] | None:
        return _pick(sorted(self.api.remote[branch]), f)

    def _remote_set(self, branch: int, key: tuple[str, str], slot: str) -> None:
        comp, cid = key
        cfg = self.api.remote[branch][key]
        if branch == DEV and not self.dev_synced:
            return
        self.remote_pending[(branch, comp, cid, slot)] = remote_projection(comp, cfg).get(slot)

    def remote_edit(self, a: float, b: float, c: float) -> None:
        branch = self._remote_branch(c)
        key = self._remote_pick(branch, a)
        if key is None:
            return
        comp, _cid = key
        cfg = self.api.remote[branch][key]
        slots = [s for s in COMPONENT_SLOTS[comp] if s != "name"]
        if cfg["rows"]:
            slots.append("row")
        slot = _pick(slots, b)
        new = self.tok()
        params = cfg["configuration"].setdefault("parameters", {})
        if slot == "value":
            params["value"] = new
        elif slot == "desc":
            clear = c < 0.05 and "stale-companion-file" not in self.masks
            cfg["description"] = "" if clear else f"Description {new}"
        elif slot == "sql":
            params["blocks"] = [
                {"name": "B", "codes": [{"name": "C", "script": [f"SELECT '{new}';"]}]}
            ]
        elif slot == "py":
            params["blocks"] = [
                {"name": "B", "codes": [{"name": "C", "script": [f"print('{new}')"]}]}
            ]
        elif slot == "code":
            params["code"] = f"print('{new}')\n"
        elif slot == "pkg":
            params["packages"] = [f"pkg{new}"]
        elif slot == "row":
            row = _pick(cfg["rows"], (b * 7) % 1)
            row["configuration"].setdefault("parameters", {})["value"] = new
            slot = f"row:{row['name']}"
        self._remote_set(branch, key, slot)
        self.note(f"  remote edit {branch} {key} [{slot}] -> {new}")

    def remote_create(self, a: float, _b: float, c: float) -> None:
        branch = self._remote_branch(c)
        comp = _pick([HTTP, SQL, PYT, APP], a)
        cid = self._remote_new_config(branch, comp)
        self.note(f"  remote create {branch} {(comp, cid)}")

    def remote_delete(self, a: float, _b: float, c: float) -> None:
        branch = self._remote_branch(c)
        key = self._remote_pick(branch, a)
        if key is None:
            return
        del self.api.remote[branch][key]
        for k in list(self.remote_pending):
            if k[0] == branch and k[1:3] == key:
                del self.remote_pending[k]
        self.note(f"  remote delete {branch} {key}")

    def remote_toggle(self, a: float, _b: float, c: float) -> None:
        branch = self._remote_branch(c)
        key = self._remote_pick(branch, a)
        if key is None:
            return
        cfg = self.api.remote[branch][key]
        cfg["isDisabled"] = not cfg["isDisabled"]
        self._remote_set(branch, key, "disabled")
        self.note(f"  remote toggle disabled {branch} {key} -> {cfg['isDisabled']}")

    def remote_rename(self, a: float, _b: float, c: float) -> None:
        if "rename-across-trees" in self.masks:
            return
        branch = self._remote_branch(c)
        key = self._remote_pick(branch, a)
        if key is None:
            return
        self.api.remote[branch][key]["name"] = f"Name {self.tok()}"
        self._remote_set(branch, key, "name")
        self.note(f"  remote rename {branch} {key} -> {self.api.remote[branch][key]['name']}")

    def remote_row_add(self, a: float, _b: float, c: float) -> None:
        if "remote-row-add-invisible" in self.masks:
            return
        branch = self._remote_branch(c)
        keys = [k for k in sorted(self.api.remote[branch]) if k[0] == HTTP]
        key = _pick(keys, a)
        if key is None:
            return
        row = self._remote_row(f"Row {self.tok()}")
        self.api.remote[branch][key]["rows"].append(row)
        self._remote_set(branch, key, f"row:{row['name']}")
        self.note(f"  remote row add {branch} {key} {row['name']}")

    def remote_row_delete(self, a: float, b: float, c: float) -> None:
        if "untracked-row-overwrite" in self.masks:
            return
        branch = self._remote_branch(c)
        keys = [
            k
            for k in sorted(self.api.remote[branch])
            if k[0] == HTTP and self.api.remote[branch][k]["rows"]
        ]
        key = _pick(keys, a)
        if key is None:
            return
        rows = self.api.remote[branch][key]["rows"]
        row = rows.pop(min(int(b * len(rows)), len(rows) - 1))
        self._remote_set(branch, key, f"row:{row['name']}")
        self.note(f"  remote row delete {branch} {key} {row['name']}")

    # -- commands -------------------------------------------------------------
    def _run(self, label: str, fn: Any) -> dict[str, Any] | None:
        try:
            result = fn()
        except REFUSALS as exc:
            self.note(f"  {label}: refused {type(exc).__name__}: {str(exc)[:160]}")
            return None
        except Exception as exc:
            raise Violation("crash", f"{label} raised {type(exc).__name__}: {exc}") from exc
        return result

    def _pull(self, label: str, branch: int | None, **kw: Any) -> dict[str, Any] | None:
        result = self._run(
            label,
            lambda: self.svc.pull(
                alias="prod", project_root=self.root, branch_override=branch, no_jobs=True, **kw
            ),
        )
        if result is None:
            return None
        pulled = int(result["branch_id"])
        self.on_dev = pulled == DEV
        if pulled == DEV:
            self.dev_synced = True
        acted = [
            (d.get("action"), d.get("path"))
            for d in result.get("details", [])
            if d.get("action") != "unchanged"
        ]
        self.note(f"  {label} -> branch {pulled}: {acted}")
        if kw.get("theirs"):
            tree = BRANCH_TREE[pulled]
            for token, (t, _f) in list(self.pending.items()):
                if t == tree:
                    del self.pending[token]
        self._acknowledge_remote(pulled)
        return result

    def _acknowledge_remote(self, branch: int) -> None:
        """A remote change is 'seen' once the branch's tree holds it."""
        local: dict[tuple[str, str], dict[str, Any]] = {}
        for d in config_dirs(self.tree(branch)):
            local[dir_identity(d)] = local_projection(d)
        for key, value in list(self.remote_pending.items()):
            b, comp, cid, slot = key
            if b != branch:
                continue
            proj = local.get((comp, cid))
            if proj is not None and proj.get(slot) == value:
                del self.remote_pending[key]

    def _leaves_edits_behind(self, target: int) -> bool:
        """Masked mode: switching the manifest to another branch tree while
        the current tree holds unpushed edits is what strands them (rows
        have no adopt guard). The masked user pushes before switching."""
        if "untracked-row-overwrite" not in self.masks:
            return False
        keep = BRANCH_TREE[target]
        if any(tree != keep for tree, _f in self.pending.values()):
            self.skipped_step = True
            return True
        return False

    def pull(self, *_: float) -> None:
        if not self._leaves_edits_behind(PROD):
            self._pull("pull", None)

    def pull_force(self, *_: float) -> None:
        if not self._leaves_edits_behind(PROD):
            self._pull("pull --force", None, force=True)

    def pull_theirs(self, *_: float) -> None:
        if not self._leaves_edits_behind(PROD):
            self._pull("pull --theirs", None, theirs=True)

    def pull_dev(self, *_: float) -> None:
        if not self._leaves_edits_behind(DEV):
            self._pull("pull --branch DEV", DEV)

    def plain_branch(self) -> int:
        return DEV if self.work_on_dev() else PROD

    def diff(self, *_: float) -> None:
        self.check_consistency(None, self.plain_branch())

    def _push(self, label: str, branch: int | None, force: bool = False) -> None:
        target = self.home if branch is None else branch
        before = copy.deepcopy(self.api.remote[target])
        result = self._run(
            label,
            lambda: self.svc.push(
                alias="prod", project_root=self.root, branch_override=branch, force=force
            ),
        )
        if result is None:
            return
        self.note(
            f"  {label} -> created={result.get('created')} updated={result.get('updated')} "
            f"deleted={result.get('deleted')} errors={len(result.get('errors') or [])} "
            f"skipped={result.get('skipped', 0)} "
            f"skipped_deletions={len(result.get('skipped_deletions') or [])}"
        )
        self.check_remote_intent(target, before, force)

    def push(self, *_: float) -> None:
        self._push("push", DEV if self.work_on_dev() else None)

    def push_force(self, *_: float) -> None:
        self._push("push --force", DEV if self.work_on_dev() else None, force=True)

    def push_dev(self, *_: float) -> None:
        if "promote-then-pull" in self.masks and not self.tree(DEV).is_dir():
            self.skipped_step = True
            return  # only the non-promote flow: push the pulled dev/ tree
        if "pull-dev-orphans-main" in self.masks and self.tree(DEV).is_dir() and not self.on_dev:
            self.skipped_step = True
            return  # dev/ is orphaned until `pull --branch DEV` re-targets it
        self._push("push --branch DEV", DEV)

    # -- invariants -------------------------------------------------------------
    def check_remote_intent(self, branch: int, before: dict, force: bool) -> None:
        """Inv 4: push never clobbers / deletes an unpulled remote change, and
        a plain push never deletes a remote config."""
        after = self.api.remote[branch]
        if not force:
            gone = sorted(set(before) - set(after))
            if gone:
                raise Violation("plain_push_deleted", f"plain push deleted {gone} on {branch}")
        for (b, comp, cid, slot), value in list(self.remote_pending.items()):
            if b != branch:
                continue
            if (comp, cid) not in after:
                if force and "force-delete-over-remote-edit" in self.masks:
                    del self.remote_pending[(b, comp, cid, slot)]
                    continue
                raise Violation(
                    "remote_edit_deleted",
                    f"push{' --force' if force else ''} deleted {comp}/{cid} on {branch} "
                    f"carrying an unpulled remote change [{slot}]",
                )
            now = remote_projection(comp, after[(comp, cid)]).get(slot)
            if now != value:
                raise Violation(
                    "remote_clobbered",
                    f"push overwrote unpulled remote change {comp}/{cid} [{slot}] on {branch}: "
                    f"{value!r} -> {now!r}",
                )

    def deliver(self) -> None:
        """Forget local intent that reached the branch of its tree."""
        for token, (tree, fname) in list(self.pending.items()):
            branch = TREE_BRANCH[tree]
            for f in (self.root / tree).rglob(fname):
                if token not in _read(f):
                    continue
                d = f.parent
                if d.parent.name == "rows":
                    d = d.parent.parent
                comp, cid = dir_identity(d)
                cfg = self.api.remote[branch].get((comp, cid))
                if cfg is not None and token in json.dumps(cfg):
                    del self.pending[token]
                    break

    def check_no_lost_edit(self, label: str) -> None:
        """Inv 1: a pending local edit is still on disk in its tree."""
        for token, (tree, fname) in self.pending.items():
            if not any(token in _read(f) for f in (self.root / tree).rglob(fname)):
                raise Violation(
                    "lost_edit",
                    f"after {label}: local edit {token} in {tree}/**/{fname} is gone from disk",
                )

    def check_consistency(self, label: str | None, branch: int = PROD) -> None:
        """Inv 2/3/5: diff lists every config whose local and remote content
        differ, and lists nothing as changed whose content is equal."""
        override = None if branch == PROD else branch
        result = self._run(
            "diff",
            lambda: self.svc.diff(alias="prod", project_root=self.root, branch_override=override),
        )
        if result is None:
            return
        if label is None:
            s = result["summary"]
            self.note(f"  diff: { {k: v for k, v in s.items() if v} }")
        listed_ids: dict[str, set[str]] = {}
        listed_paths: dict[str, set[str]] = {}
        for ch in result["changes"]:
            for cid in (ch.get("config_id"), ch.get("parent_config_id")):
                if cid:
                    listed_ids.setdefault(str(cid), set()).add(ch["change_type"])
            listed_paths.setdefault(ch.get("path", ""), set()).add(ch["change_type"])
        for key in ("orphaned", "never_fetched", "remote_only"):
            for item in result.get(key) or []:
                if isinstance(item, dict) and item.get("config_id"):
                    listed_ids.setdefault(str(item["config_id"]), set()).add(key)
        tree = self.tree(branch)
        if not tree.is_dir():  # push --branch DEV promotes main/
            tree = self.tree(PROD)
        remote = self.api.remote[branch]
        for d in config_dirs(tree):
            rel = d.relative_to(tree).as_posix()
            comp, cid = dir_identity(d)
            local = local_projection(d)
            if not cid:
                if rel not in listed_paths:
                    raise Violation("invisible_new_config", f"new local config {rel} not in diff")
                continue
            cfg = remote.get((comp, cid))
            listed = listed_ids.get(cid, set()) | listed_paths.get(rel, set())
            if cfg is None:
                if not listed:
                    raise Violation(
                        "invisible_divergence",
                        f"{rel} ({cid}) is gone from branch {branch} but diff is silent",
                    )
                continue
            rproj = remote_projection(comp, cfg)
            # Absent `is_disabled` means "leave the remote alone" (#467): a
            # disabled-only difference may or may not be listed.
            ambiguous = (
                "sparse-disabled-reenable" in self.masks
                and "is_disabled" not in read_yaml(d / "_config.yml")
                and local.pop("disabled") != rproj.pop("disabled")
            )
            differs = {k for k in set(local) | set(rproj) if local.get(k) != rproj.get(k)}
            if differs and not listed:
                pend = [t for t in self.pending if any(t in str(v) for v in local.values())]
                raise Violation(
                    "invisible_divergence" if not pend else "invisible_local_edit",
                    f"{rel} ({cid}) differs from branch {branch} in {sorted(differs)} "
                    f"(local={ {k: local.get(k) for k in differs} } "
                    f"remote={ {k: rproj.get(k) for k in differs} }) but diff is silent",
                )
            phantom = listed & {"modified", "remote_modified", "conflict", "added", "deleted"}
            if not differs and phantom and not ambiguous:
                raise Violation(
                    "phantom_drift",
                    f"{rel} ({cid}) equals branch {branch} but diff lists {sorted(phantom)}",
                )
        # A config that exists only on the remote (created in the UI, not
        # pulled yet) must show up in diff too, not only local directories.
        local_ids = {dir_identity(d)[1] for d in config_dirs(tree)}
        for comp, cid in remote:
            if cid not in local_ids and cid not in listed_ids:
                raise Violation(
                    "invisible_remote_config",
                    f"{comp}/{cid} exists only on branch {branch} but diff is silent",
                )

    def step(self, step: Step) -> None:
        name, a, b, c = step
        self.note(name)
        self.skipped_step = False
        getattr(self, name)(a, b, c)
        if self.skipped_step:
            return
        self.deliver()
        self.check_no_lost_edit(name)
        if name in ("pull", "pull_force", "pull_theirs", "push", "push_force"):
            self.check_consistency(name, self.plain_branch())
        elif name in ("pull_dev", "push_dev"):
            self.check_consistency(name, DEV)


def run_walk(
    steps: list[Step], masks: frozenset[str] = frozenset()
) -> tuple[Violation | None, list[str]]:
    tmp = Path(tempfile.mkdtemp(prefix="sync-walk-"))
    try:
        sim = Sim(tmp, masks)
        sim.seed_remote()
        sim.svc.init_sync(alias="prod", project_root=sim.root)
        sim.svc.pull(alias="prod", project_root=sim.root, no_jobs=True)
        for i, step in enumerate(steps):
            try:
                sim.step(step)
            except Violation as v:
                v.step_index = i
                return v, sim.log
        # The per-step checks run diff only after sync commands; one final
        # diff covers edits made after the last of them.
        try:
            sim.check_consistency("end", sim.plain_branch())
        except Violation as v:
            v.step_index = len(steps)
            return v, sim.log
        return None, sim.log
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def shrink(steps: list[Step], kind: str, masks: frozenset[str] = frozenset()) -> list[Step]:
    """Drop steps one at a time while the walk still fails with *kind*."""
    current = list(steps)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(current):
            candidate = current[:i] + current[i + 1 :]
            v, _ = run_walk(candidate, masks)
            if v is not None and v.kind == kind:
                current = candidate
                changed = True
            else:
                i += 1
    return current


# ===========================================================================
# The random walk itself
# ===========================================================================

# Known findings whose trigger the CI walk steers around. Each name has an
# explicit ``xfail(strict=True)`` test below (or, for ``move-is-delete-create``,
# in test_sync_formal_counterexamples.py). When a finding is fixed, its xfail
# test goes red: delete the marker AND the mask name here, so the walk starts
# exercising that path again.
MASKS = frozenset(
    {
        "stale-companion-file",
        "new-description-file",
        "new-config-rows",
        "remote-row-add-invisible",
        "promote-then-pull",
        "rename-across-trees",
        "untracked-row-overwrite",
        "retarget-reuses-other-tree-baseline",
        # Not a bug by itself: after `pull --branch DEV` main/ is orphaned by
        # design (#649), so the masked user keeps working in dev/ with
        # --branch DEV. Unmasked, it surfaces the row variant of
        # new-config-rows (test_finding_row_edit_in_untracked_config_is_visible).
        "pull-dev-orphans-main",
        "sparse-disabled-reenable",
        "force-delete-over-remote-edit",
        "move-is-delete-create",  # #792 I
    }
)

CI_SEEDS = range(50)
CI_STEPS = 25


def _walk_seeds() -> list[int]:
    """``SYNC_WALK_SEEDS=N`` runs seeds 0..N-1 instead of the CI set."""
    n = os.environ.get("SYNC_WALK_SEEDS")
    return list(range(int(n))) if n else list(CI_SEEDS)


def _walk_masks() -> frozenset[str]:
    """``SYNC_WALK_UNMASK=a,b`` (or ``all``) re-enables known-finding triggers."""
    unmask = os.environ.get("SYNC_WALK_UNMASK", "")
    if unmask == "all":
        return frozenset()
    return MASKS - set(unmask.split(","))


@pytest.mark.parametrize("seed", _walk_seeds())
def test_sync_random_walk(seed: int) -> None:
    steps_n = int(os.environ.get("SYNC_WALK_STEPS", CI_STEPS))
    masks = _walk_masks()
    steps = generate_steps(seed, steps_n)
    violation, _ = run_walk(steps, masks)
    if violation is None:
        return
    small = shrink(steps, violation.kind, masks)
    violation, log = run_walk(small, masks)
    pytest.fail(
        f"seed {seed}: {violation}\n"
        f"minimal reproduction ({len(small)} steps): {small!r}\n" + "\n".join(log),
        pytrace=False,
    )


# ===========================================================================
# Findings: shrunk, deterministic reproductions. Each asserts the SAFE
# behavior and is xfail(strict=True) while the bug exists.
#
# Seeded world (ids are deterministic): keboola.ex-http 1000 "Name tk6z"
# (rows "Row tk2z"/1001, "Row tk4z"/1002), keboola.ex-http 1003 "Name tk13z"
# (rows "Row tk9z"/1004, "Row tk11z"/1005), snowflake 1006 "Name tk16z"
# (no description), snowflake 1007, python-transformation-v2 1008
# "Name tk21z", custom-python app 1009 "Name tk25z". The dev branch starts
# as a copy of production.
# ===========================================================================

HTTP_1 = (HTTP, "1000")
HTTP_2 = (HTTP, "1003")
SQL_1 = (SQL, "1006")
PYT_1 = (PYT, "1008")
APP_1 = (APP, "1009")


def _world(tmp_path: Path) -> Sim:
    sim = Sim(tmp_path)
    sim.seed_remote()
    sim.svc.init_sync(alias="prod", project_root=sim.root)
    sim.svc.pull(alias="prod", project_root=sim.root, no_jobs=True)
    return sim


def _dir(sim: Sim, rel: str, tree: str = "main") -> Path:
    return sim.root / tree / rel


def _pull(sim: Sim, branch: int | None = None, **kw: Any) -> dict[str, Any]:
    return sim.svc.pull(
        alias="prod", project_root=sim.root, branch_override=branch, no_jobs=True, **kw
    )


def _push(sim: Sim, branch: int | None = None, **kw: Any) -> dict[str, Any]:
    return sim.svc.push(alias="prod", project_root=sim.root, branch_override=branch, **kw)


def _changes(sim: Sim, branch: int | None = None) -> list[tuple[str, str]]:
    d = sim.svc.diff(alias="prod", project_root=sim.root, branch_override=branch)
    return [
        (c["change_type"], c.get("parent_config_id") or c["config_id"] or c["path"])
        for c in d["changes"]
    ]


def _proj(sim: Sim, branch: int, key: tuple[str, str]) -> dict[str, Any]:
    return remote_projection(key[0], sim.api.remote[branch][key])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING stale-companion-file: when the remote CLEARS a field that "
        "pull extracts into a companion file (description -> _description.md, "
        "packages -> pyproject.toml), pull reports 'updated' but leaves the "
        "old file on disk: extract_code_files only writes non-empty values "
        "(code_extraction.py:314-319 _extract_description, and the "
        "`if packages:` branches) and sync_service.py:835 never removes a "
        "stale companion. diff then says clean, and the next push of ANY "
        "edit to that config merges the stale file back and silently "
        "restores the cleared value on the remote."
    ),
)
@pytest.mark.parametrize("field", ["description", "packages"])
def test_finding_cleared_remote_field_is_not_resurrected(tmp_path: Path, field: str) -> None:
    sim = _world(tmp_path)
    if field == "description":
        key, rel, edit_slot = HTTP_1, "extractor/keboola.ex-http/name-tk6z", "value"
        sim.api.remote[PROD][key]["description"] = ""
        slot = "desc"
    else:
        key, rel, edit_slot = APP_1, "application/kds-team.app-custom-python/name-tk25z", "code"
        sim.api.remote[PROD][key]["configuration"]["parameters"]["packages"] = []
        slot = "pkg"
    _pull(sim)
    sim._local_edit_slot(_dir(sim, rel), edit_slot)  # an unrelated edit
    _push(sim)
    assert _proj(sim, PROD, key)[slot] == (), f"push restored the cleared {field}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING new-description-file: a config pulled with an empty "
        "description has no _description.md, so pull_extra_hashes has no "
        "entry for it. Creating the file is invisible: diff only checks the "
        "companions recorded at pull time (config_locally_modified / the "
        "diff unchanged-shortcut around sync_service.py:1290), reports the "
        "config unchanged, and push never sends the description."
    ),
)
def test_finding_new_description_file_is_pushed(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    d = _dir(sim, "transformation/keboola.snowflake-transformation/name-tk16z")
    assert not (d / "_description.md").exists()
    (d / "_description.md").write_text("Description tk900z", encoding="utf-8")
    assert ("modified", "1006") in _changes(sim)
    _push(sim)
    assert _proj(sim, PROD, SQL_1)["desc"] == ("tk900z",)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING new-config-rows: rows are discovered only under configs the "
        "manifest already tracks (branch_scope.py:475-478 "
        "find_untracked_rows, called at sync_service.py:1454). A new config "
        "authored with rows/ is created WITHOUT its rows; push reports "
        "success. On production a second push creates them."
    ),
)
def test_finding_new_config_is_created_with_its_rows(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim.local_new(0.0, 0.0, 0.0)  # copy of keboola.ex-http 1000 (2 rows)
    assert _push(sim)["created"] == 1
    (new_key,) = [k for k in sim.api.remote[PROD] if k[1] == "1010"]
    assert len(sim.api.remote[PROD][new_key]["rows"]) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING new-config-rows (dev-branch variant, worse): promoting a "
        "new config with rows (`push --branch DEV`, no dev/ tree) creates "
        "the config on DEV; its manifest entry then belongs to the dev tree "
        "while the files live in main/, so find_untracked_rows "
        "(branch_scope.py:477 only_branch_path filter) never sees the rows "
        "again: diff --branch DEV is clean and the rows are never created."
    ),
)
def test_finding_promoted_new_config_rows_reach_dev(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim.local_new(0.0, 0.0, 0.0)
    _push(sim, DEV)
    _push(sim, DEV)  # even a second push
    (new_key,) = [k for k in sim.api.remote[DEV] if k[1] == "1010"]
    assert len(sim.api.remote[DEV][new_key]["rows"]) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING new-config-rows (adopted-config variant): after `pull "
        "--branch DEV` drops a production config that is gone on DEV, its "
        "main/ directory is untracked; a config-level edit there diffs as a "
        "conflict (#792 E), but a ROW edit is invisible -- rows are walked "
        "only under manifest-tracked configs (branch_scope.py:475)."
    ),
)
def test_finding_row_edit_in_untracked_config_is_visible(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    del sim.api.remote[DEV][HTTP_2]
    row = _dir(sim, "extractor/keboola.ex-http/name-tk13z/rows/row-tk11z/_config.yml")
    row.write_text(row.read_text().replace("tk12z", "tk900z"), encoding="utf-8")
    _pull(sim, DEV)
    assert any(cid == "1003" for _, cid in _changes(sim))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING remote-row-add-invisible: a row added on the remote (UI) "
        "has no manifest entry, so compute_row_changeset never reports it: "
        "diff is clean and pull writes it without any detail entry. A user "
        "reviewing `sync diff` before `sync pull` does not see it."
    ),
)
def test_finding_remote_added_row_is_visible_in_diff(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim.api.remote[PROD][HTTP_1]["rows"].append(sim._remote_row("Row extra"))
    assert any(cid == "1000" for _, cid in _changes(sim))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING promote-then-pull (edit): `push --branch DEV` from main/ "
        "re-stamps the shared manifest baseline with the DEV result "
        "(stamp_updated_config). `sync diff` on production then calls the "
        "local edit `remote_modified` (production never changed), and the "
        "next plain `sync pull` silently replaces the edited transform.sql "
        "with production's code. The edit survives only on DEV; the next "
        "promote from the reverted main/ has nothing of it."
    ),
)
def test_finding_promote_then_pull_keeps_local_edit(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sql = _dir(sim, "transformation/keboola.snowflake-transformation/name-tk16z/transform.sql")
    sql.write_text(sql.read_text().replace("tk15z", "tk900z"), encoding="utf-8")
    _push(sim, DEV)
    assert ("modified", "1006") in _changes(sim)
    with contextlib.suppress(SyncConflictError):
        _pull(sim)
    assert "tk900z" in sql.read_text()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING promote-then-pull (new config): a config authored in main/ "
        "and promoted with `push --branch DEV` exists only on DEV. The next "
        "plain `sync pull` (production) reports it 'removed' -- the label "
        "for 'deleted on the remote' -- and deletes the authored directory "
        "from main/ (stale-entry sweep, _sync_stale.py)."
    ),
)
def test_finding_promoted_new_config_dir_survives_pull(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim.local_new(0.6, 0.0, 0.0)  # a python-transformation-v2 copy
    (new_dir,) = (sim.root / "main/transformation/keboola.python-transformation-v2").glob("new-*")
    assert _push(sim, DEV)["created"] == 1
    _pull(sim)
    assert new_dir.is_dir()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING rename-across-trees: one manifest entry (one path) serves "
        "both branch trees. A config renamed on DEV gets a new path on `pull "
        "--branch DEV`; the next plain `sync pull` writes production's copy "
        "under that NEW path in main/ and leaves the old main/ directory, "
        "which still carries the config id. diff calls it 'added' and a plain "
        "`sync push` CREATES A DUPLICATE config in production."
    ),
)
def test_finding_rename_on_dev_does_not_duplicate_prod_config(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    _pull(sim, DEV)
    sim.api.remote[DEV][PYT_1]["name"] = "Renamed on dev"
    _pull(sim, DEV)
    _pull(sim)
    before = set(sim.api.remote[PROD])
    _push(sim)
    assert set(sim.api.remote[PROD]) == before, "push created a duplicate config"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING untracked-row-overwrite: rows have no #792 E adopt guard. "
        "A row deleted on production is dropped from the SHARED manifest by "
        "a plain `sync pull`; the dev/ copy of that row file, edited locally, "
        "is now untracked and the next `pull --branch DEV` overwrites it "
        "without a conflict or skip (sync_service.py:931 write branch)."
    ),
)
def test_finding_untracked_row_edit_survives_dev_pull(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    sim.api.remote[PROD][HTTP_2]["rows"] = [r for r in rows if r["name"] != "Row tk11z"]
    _pull(sim, DEV)
    row = _dir(sim, "extractor/keboola.ex-http/name-tk13z/rows/row-tk11z/_config.yml", "dev")
    row.write_text(row.read_text().replace("tk12z", "tk900z"), encoding="utf-8")
    _pull(sim)  # plain pull of production
    with contextlib.suppress(SyncConflictError):
        _pull(sim, DEV)
    assert "tk900z" in row.read_text()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING retarget-reuses-other-tree-baseline: `pull --branch DEV` "
        "re-stamps the shared manifest entry (incl. pull_extra_hashes) from "
        "dev/. A later plain `sync pull` judges main/ against dev/'s "
        "baseline: a main/ config whose _description.md was removed (and "
        "pushed) is reported 'skipped: locally modified' and diffs as a "
        "conflict although main/ equals production."
    ),
)
def test_finding_retarget_pull_has_no_false_conflict(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    d = _dir(sim, "transformation/keboola.python-transformation-v2/name-tk21z")
    (d / "_description.md").unlink()
    _push(sim)
    assert _proj(sim, PROD, PYT_1)["desc"] == ()
    _pull(sim, DEV)
    result = _pull(sim)
    assert [x for x in result["details"] if x["action"] == "skipped"] == []
    assert _changes(sim) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING sparse-disabled-reenable: pull writes `is_disabled: true` "
        "only when disabled and omits the key otherwise, and push treats an "
        "absent key as 'leave the remote alone' (#467, _sync_push_ops.py "
        "~801). Re-enabling by deleting the line -- the natural edit of the "
        "format pull wrote -- reports 'updated', changes nothing, and diff "
        "stays clean, so the config silently stays disabled."
    ),
)
def test_finding_reenable_by_removing_flag_is_pushed_or_reported(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim.api.remote[PROD][SQL_1]["isDisabled"] = True
    _pull(sim)
    cfg = _dir(sim, "transformation/keboola.snowflake-transformation/name-tk16z/_config.yml")
    data = read_yaml(cfg)
    assert data.get("is_disabled") is True
    data.pop("is_disabled")
    write_yaml(cfg, data)
    _push(sim)
    assert sim.api.remote[PROD][SQL_1]["isDisabled"] is False or _changes(sim)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING force-delete-over-remote-edit: a config deleted locally and "
        "edited on the remote since the last pull diffs as plain 'deleted' "
        "(not a conflict), so `push --force` deletes it and the remote edit "
        "goes with it. Everywhere else --force only enables deletes and "
        "never overrides a conflict (plan_push, _sync_push_ops.py:155)."
    ),
)
def test_finding_force_push_does_not_delete_remotely_edited_config(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, "transformation/keboola.snowflake-transformation/name-tk16z"))
    params = sim.api.remote[PROD][SQL_1]["configuration"]["parameters"]
    params["blocks"][0]["codes"][0]["script"] = ["SELECT 'tk900z';"]
    _push(sim, force=True)
    assert SQL_1 in sim.api.remote[PROD], "the unpulled remote edit was deleted"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING force-delete-row-errors: `push --force` of a deleted config "
        "dir with rows deletes the config first and then each row; the row "
        "DELETEs answer 404 (the config is already gone) and land in "
        "errors[], so a fully successful delete reports failures and exits "
        "1 (#745 partial-failure rule)."
    ),
)
def test_finding_force_delete_of_config_with_rows_reports_no_errors(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, "extractor/keboola.ex-http/name-tk13z"))
    result = _push(sim, force=True)
    assert HTTP_2 not in sim.api.remote[PROD]
    assert result["errors"] == []
