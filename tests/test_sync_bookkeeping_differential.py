"""Differential test of the sync engine's manifest bookkeeping.

Oracle: after any operation that leaves the local files and the remote in
agreement (push, pull, clone, scaffold + push, adopt-by-id, row writes, ...),
the manifest bookkeeping of the touched configs must equal what a FRESH
``sync pull`` into an empty directory writes for the same remote state:
``pull_hash``, ``pull_config_hash``, ``pull_extra_hashes``,
``config_hash_version``, row hashes, ``branch_id`` / ``component_id`` / ``id``.
``path`` may legitimately differ (a rename keeps the old directory, a promote
reads ``main/``), so it is reported but not compared.

Behavioral consequence, checked too: right after the operation ``sync diff``
is clean, and a semantic edit to EACH file of the config dir (``_config.yml``,
every companion file, every row file) makes ``sync diff`` report a change.

The bug class this hunts is PR #917's: one of the ~18 bookkeeping write sites
(``_sync_writeback.py``, ``sync_service.py``, ``_sync_bindings.py``,
``_sync_baseline.py``, ``_sync_push_ops.py``, ``_sync_stale.py``) omitting or
mis-computing a field. Cases that fail on current code are kept as
``xfail(strict=True, reason="FINDING: ...")``: green in CI, red the moment the
bug is fixed (then delete the marker).

Locally authored configs are written in the exact canonical form ``sync pull``
produces (same converter, same code extraction, same YAML dump), minus the
``config_id`` the API has not assigned yet. Any remaining difference against a
fresh pull is therefore produced by the write path, not by the test's input.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.component_service import materialize_pushed_config
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.code_extraction import extract_code_files
from keboola_agent_cli.sync.config_format import (
    api_config_to_local,
    api_row_to_local,
    dump_config_yaml,
)
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest

PROD = 12345
DEV = 99999
CONFIG_FILE = "_config.yml"
EXTRA_FILES = ("_description.md", "transform.sql", "transform.py", "code.py", "pyproject.toml")
BOOKKEEPING_KEYS = ("pull_hash", "pull_config_hash", "pull_extra_hashes", "config_hash_version")
TOKEN = "original"


# ===========================================================================
# Component variants (API-side bodies)
# ===========================================================================


@dataclass(frozen=True)
class Variant:
    """One component flavour: its id, Storage type, and a seed config body."""

    name: str
    component_id: str
    component_type: str
    body: Callable[[], dict[str, Any]]
    rows: Callable[[], list[dict[str, Any]]] = list


def _extractor_body() -> dict[str, Any]:
    return {
        "parameters": {"baseUrl": f"https://{TOKEN}.example.com"},
        "storage": {"output": {"tables": [{"source": "a.csv", "destination": "in.c-a.a"}]}},
    }


def _sql_body() -> dict[str, Any]:
    return {
        "parameters": {
            "blocks": [
                {
                    "name": "Block 1",
                    "codes": [{"name": "Code 1", "script": [f"SELECT '{TOKEN}' AS c;"]}],
                }
            ]
        },
        "storage": {"output": {"tables": [{"source": "t", "destination": "out.c-a.t"}]}},
    }


def _python_body() -> dict[str, Any]:
    return {
        "parameters": {
            "blocks": [
                {"name": "Block 1", "codes": [{"name": "Code 1", "script": [f"print('{TOKEN}')"]}]}
            ],
            "packages": [f"{TOKEN}-pkg"],
        }
    }


def _app_body() -> dict[str, Any]:
    return {"parameters": {"code": f"print('{TOKEN}')\n", "packages": [f"{TOKEN}-pkg"]}}


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "name": f"Row {i}",
            "description": "",
            "configuration": {"parameters": {"table": f"{TOKEN}_{i}"}},
            "isDisabled": False,
        }
        for i in (1, 2)
    ]


VARIANTS: dict[str, Variant] = {
    v.name: v
    for v in (
        Variant("extractor", "keboola.ex-http", "extractor", _extractor_body),
        Variant("sql", "keboola.snowflake-transformation", "transformation", _sql_body),
        Variant("python", "keboola.python-transformation-v2", "transformation", _python_body),
        Variant("app", "kds-team.app-custom-python", "application", _app_body),
        Variant("rows", "keboola.ex-http", "extractor", _extractor_body, _rows),
    )
}
CONFIG_VARIANTS = ("extractor", "sql", "python", "app", "rows")
CODE_VARIANTS = ("sql", "python", "app")


# ===========================================================================
# Faithful in-memory Storage API (multi-component, rows, description, disable)
# ===========================================================================


class Api:
    """Branch-aware in-memory Storage API.

    ``remote[branch][(component_id, config_id)] = config``. Unlike the
    ``FakeApi`` of ``test_sync_formal_counterexamples.py`` it is
    multi-component, stores rows, and honours ``description`` and
    ``is_disabled`` on update (``None`` = unchanged, like Storage).
    """

    def __init__(self, first_id: int = 1000) -> None:
        self.remote: dict[int, dict[tuple[str, str], dict[str, Any]]] = {PROD: {}, DEV: {}}
        self.types: dict[str, str] = {}
        # A second project gets a disjoint id range, so a stale source id can
        # never pass for the target's id by coincidence.
        self._ids = itertools.count(first_id)

    # -- seeding -----------------------------------------------------------
    def seed(self, variant: Variant, name: str = "Orders", branch: int = PROD) -> str:
        cid = f"cfg-{next(self._ids)}"
        self.types[variant.component_id] = variant.component_type
        rows = []
        for row in variant.rows():
            rows.append({"id": f"row-{next(self._ids)}", **copy.deepcopy(row)})
        self.remote[branch][(variant.component_id, cid)] = {
            "id": cid,
            "name": name,
            "description": f"{TOKEN} description",
            "configuration": variant.body(),
            "rows": rows,
            "isDisabled": False,
        }
        return cid

    def branch_from_prod(self) -> None:
        """Model Keboola's dev-branch creation: a copy of production, same ids."""
        self.remote[DEV] = copy.deepcopy(self.remote[PROD])

    def get(self, branch: int, component_id: str, config_id: str) -> dict[str, Any]:
        return self.remote[branch][(component_id, config_id)]

    def only(self, branch: int) -> tuple[str, str]:
        (key,) = self.remote[branch]
        return key

    # -- client ------------------------------------------------------------
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
        for (comp, _cid), cfg in self.remote[self._b(branch_id)].items():
            by_comp.setdefault(comp, []).append(copy.deepcopy(cfg))
        return [
            {"id": comp, "type": self.types.get(comp, "other"), "configurations": cfgs}
            for comp, cfgs in by_comp.items()
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
        cid = f"cfg-{next(self._ids)}"
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

    def _update(
        self,
        component_id: str,
        config_id: str,
        name: str | None = None,
        configuration: dict | None = None,
        description: str | None = None,
        branch_id: int | None = None,
        is_disabled: bool | None = None,
        **_: Any,
    ) -> dict:
        cfg = self.remote[self._b(branch_id)][(component_id, config_id)]
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
        self.remote[self._b(branch_id)].pop((component_id, config_id))

    def _detail(
        self, component_id: str, config_id: str, branch_id: int | None = None, **_: Any
    ) -> dict:
        return copy.deepcopy(self.remote[self._b(branch_id)][(component_id, config_id)])

    def _rows_of(self, component_id: str, config_id: str, branch_id: int | None) -> list[dict]:
        return self.remote[self._b(branch_id)][(component_id, config_id)]["rows"]

    def _create_row(
        self,
        component_id: str,
        config_id: str,
        name: str = "",
        configuration: dict | None = None,
        description: str = "",
        is_disabled: bool = False,
        branch_id: int | None = None,
        **_: Any,
    ) -> dict:
        row = {
            "id": f"row-{next(self._ids)}",
            "name": name,
            "description": description or "",
            "configuration": copy.deepcopy(configuration or {}),
            "isDisabled": bool(is_disabled),
        }
        self._rows_of(component_id, config_id, branch_id).append(row)
        return copy.deepcopy(row)

    def _find_row(
        self, component_id: str, config_id: str, row_id: str, branch_id: int | None
    ) -> dict:
        (row,) = [r for r in self._rows_of(component_id, config_id, branch_id) if r["id"] == row_id]
        return row

    def _update_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        name: str | None = None,
        configuration: dict | None = None,
        description: str | None = None,
        is_disabled: bool | None = None,
        branch_id: int | None = None,
        **_: Any,
    ) -> dict:
        row = self._find_row(component_id, config_id, row_id, branch_id)
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
        self, component_id: str, config_id: str, row_id: str, branch_id: int | None = None
    ) -> None:
        rows = self._rows_of(component_id, config_id, branch_id)
        rows[:] = [r for r in rows if r["id"] != row_id]

    def _get_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
        **_: Any,
    ) -> dict:
        return copy.deepcopy(self._find_row(component_id, config_id, row_id, branch_id))


class World:
    """A real SyncService over :class:`Api` and a tmp working tree."""

    def __init__(self, tmp: Path, api: Api | None = None, name: str = "project") -> None:
        self.api = api or Api()
        self.root = tmp / name
        self.root.mkdir(parents=True)
        cfgdir = tmp / f"{name}-cfg"
        cfgdir.mkdir()
        self.store = setup_single_project(cfgdir)
        self.svc = SyncService(config_store=self.store, client_factory=self._client)

    def _client(self, url: str, token: str) -> Any:
        return self.api.client()

    def init(self) -> None:
        self.svc.init_sync(alias="prod", project_root=self.root)

    def pull(self, branch: int | None = None, **kw: Any) -> dict:
        return self.svc.pull(alias="prod", project_root=self.root, branch_override=branch, **kw)

    def diff(self, branch: int | None = None) -> dict:
        return self.svc.diff(alias="prod", project_root=self.root, branch_override=branch)

    def push(self, branch: int | None = None, **kw: Any) -> dict:
        result = self.svc.push(alias="prod", project_root=self.root, branch_override=branch, **kw)
        assert result.get("errors", []) == [], result
        return result

    def source_tree(self, branch: int | None) -> Path:
        manifest = load_manifest(self.root)
        return self.root / self.svc._resolve_source_branch_path(manifest, self.root, branch)

    def config_dir(self, config_id: str, branch: int | None = None) -> Path:
        manifest = load_manifest(self.root)
        (entry,) = [c for c in manifest.configurations if c.id == config_id]
        return self.source_tree(branch) / entry.path


# ===========================================================================
# Local authoring in the canonical (pull-identical) form
# ===========================================================================


def author_config(tree: Path, variant: Variant, name: str = "Authored") -> Path:
    """Write a NEW untracked config dir exactly as pull would, minus ``config_id``."""
    api_cfg = {
        "name": name,
        "description": f"{TOKEN} description",
        "configuration": variant.body(),
        "isDisabled": False,
    }
    local = api_config_to_local(variant.component_id, api_cfg, "")
    local["_keboola"].pop("config_id")
    config_dir = tree / variant.component_type / variant.component_id / name.lower()
    config_dir.mkdir(parents=True)
    extract_code_files(variant.component_id, local, config_dir)
    (config_dir / CONFIG_FILE).write_text(dump_config_yaml(local), encoding="utf-8", newline="")
    for row in variant.rows():
        author_row(config_dir, variant, row)
    return config_dir


def author_row(config_dir: Path, variant: Variant, row: dict[str, Any]) -> Path:
    """Write a NEW untracked row dir exactly as pull would, minus ``row_id``."""
    local = api_row_to_local({**row, "id": ""}, variant.component_id)
    local["_keboola"].pop("row_id")
    row_dir = config_dir / "rows" / row["name"].lower().replace(" ", "-")
    row_dir.mkdir(parents=True)
    (row_dir / CONFIG_FILE).write_text(dump_config_yaml(local), encoding="utf-8", newline="")
    return row_dir


def semantic_edit(path: Path) -> None:
    """Change what a file MEANS, not just its bytes."""
    if path.name == CONFIG_FILE:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        params = data.setdefault("parameters", {}) or {}
        data["parameters"] = params
        params["edit_marker"] = params.get("edit_marker", 0) + 1
        path.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
        return
    text = path.read_text(encoding="utf-8")
    assert TOKEN in text, f"no {TOKEN!r} token to edit in {path}"
    # The token stays a prefix, so the file can be edited again later. Written
    # in text mode like pull writes companion files (CRLF on Windows), so the
    # edited file has the line endings a fresh pull would produce.
    path.write_text(text.replace(TOKEN, f"{TOKEN}e", 1), encoding="utf-8")


# ===========================================================================
# Oracle
# ===========================================================================


def _file_hash(world: World, path: Path) -> str:
    return world.svc._file_hash(path)


def bookkeeping(world: World, branch: int | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Bookkeeping per tracked config of *branch*: compared fields + path."""
    target = branch or PROD
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for c in load_manifest(world.root).configurations:
        if (c.branch_id or PROD) != target:
            continue
        meta = {k: c.metadata.get(k) for k in BOOKKEEPING_KEYS}
        meta["pull_extra_hashes"] = meta["pull_extra_hashes"] or {}
        rows = {
            r.id: {
                k: (r.metadata or {}).get(k) for k in BOOKKEEPING_KEYS if k != "pull_extra_hashes"
            }
            for r in c.rows
        }
        out[(c.component_id, c.id)] = {
            "branch_id": c.branch_id,
            **meta,
            "rows": rows,
            "_path": c.path,
            "_row_paths": {r.id: r.path for r in c.rows},
        }
    return out


def fresh_pull(tmp: Path, api: Api, branch: int | None) -> World:
    fresh = World(tmp, api, name=f"fresh-{next(_FRESH)}")
    fresh.init()
    fresh.pull(branch=branch)
    return fresh


_FRESH = itertools.count()


def self_consistency(world: World, branch: int | None) -> list[str]:
    """Hashes recorded for the files must describe the files on disk."""
    problems: list[str] = []
    tree = world.source_tree(branch)
    target = branch or PROD
    for c in load_manifest(world.root).configurations:
        if (c.branch_id or PROD) != target:
            continue
        config_dir = tree / c.path
        cf = config_dir / CONFIG_FILE
        if c.metadata.get("pull_hash") != (_file_hash(world, cf) if cf.exists() else None):
            problems.append(f"{c.id}: pull_hash does not match {c.path}/{CONFIG_FILE}")
        on_disk = {
            f: _file_hash(world, config_dir / f) for f in EXTRA_FILES if (config_dir / f).exists()
        }
        recorded = c.metadata.get("pull_extra_hashes") or {}
        if recorded != on_disk:
            problems.append(
                f"{c.id}: pull_extra_hashes {sorted(recorded)} != companion files on disk "
                f"{sorted(on_disk)} (or a hash differs)"
            )
        for r in c.rows:
            rf = config_dir / r.path / CONFIG_FILE
            if (r.metadata or {}).get("pull_hash") != (
                _file_hash(world, rf) if rf.exists() else None
            ):
                problems.append(f"{c.id}/{r.id}: row pull_hash does not match its file")
    return problems


def compare_to_fresh(world: World, fresh: World, branch: int | None) -> list[str]:
    """Field-by-field bookkeeping difference against a fresh pull."""
    mine = bookkeeping(world, branch)
    theirs = bookkeeping(fresh, branch)
    problems: list[str] = []
    for key in sorted(set(mine) | set(theirs)):
        if key not in mine:
            problems.append(f"{key}: missing from the manifest (fresh pull tracks it)")
            continue
        if key not in theirs:
            problems.append(f"{key}: tracked, but a fresh pull does not track it")
            continue
        a, b = mine[key], theirs[key]
        for field in ("branch_id", *BOOKKEEPING_KEYS):
            if field == "pull_extra_hashes":
                if a[field] != b[field]:
                    ka, kb = sorted(a[field]), sorted(b[field])
                    detail = (
                        f"files {ka} vs fresh {kb}"
                        if ka != kb
                        else "hash of "
                        + ", ".join(f for f in ka if a[field][f] != b[field][f])
                        + " differs"
                    )
                    problems.append(f"{key}: pull_extra_hashes differ ({detail})")
            elif a[field] != b[field]:
                problems.append(f"{key}: {field} {a[field]!r} != fresh {b[field]!r}")
        if set(a["rows"]) != set(b["rows"]):
            problems.append(f"{key}: row ids {sorted(a['rows'])} != fresh {sorted(b['rows'])}")
        for rid in set(a["rows"]) & set(b["rows"]):
            for field, va in a["rows"][rid].items():
                vb = b["rows"][rid][field]
                if va != vb:
                    problems.append(f"{key} row {rid}: {field} {va!r} != fresh {vb!r}")
    return problems


DIRTY_KEYS = ("added", "modified", "remote_modified", "conflict", "deleted", "remote_deleted")


def dirty(summary: dict[str, int]) -> dict[str, int]:
    return {k: summary[k] for k in DIRTY_KEYS if summary.get(k)}


def edit_sensitivity(world: World, branch: int | None) -> list[str]:
    """Each file of each tracked config, edited alone, must show in ``sync diff``."""
    problems: list[str] = []
    tree = world.source_tree(branch)
    target = branch or PROD
    entries = [
        c for c in load_manifest(world.root).configurations if (c.branch_id or PROD) == target
    ]
    for c in entries:
        config_dir = tree / c.path
        files = [config_dir / f for f in (CONFIG_FILE, *EXTRA_FILES) if (config_dir / f).exists()]
        files += [config_dir / r.path / CONFIG_FILE for r in c.rows]
        for path in files:
            original = path.read_bytes()
            semantic_edit(path)
            try:
                result = world.diff(branch=branch)
                hit = [
                    ch
                    for ch in result["changes"]
                    if ch["change_type"] in ("modified", "conflict")
                    and c.id in (ch.get("config_id"), ch.get("parent_config_id"))
                ]
                if not hit:
                    problems.append(
                        f"edit of {path.relative_to(tree)} is invisible to sync diff "
                        f"(summary {dirty(result['summary'])})"
                    )
                elif any(ch["change_type"] == "conflict" for ch in hit):
                    problems.append(f"edit of {path.relative_to(tree)} diffs as a CONFLICT")
            finally:
                path.write_bytes(original)
    return problems


def check(world: World, tmp: Path, branch: int | None) -> None:
    """Run the whole oracle; one assertion listing every problem found."""
    problems: list[str] = []
    summary = world.diff(branch=branch)["summary"]
    if dirty(summary):
        problems.append(f"sync diff not clean right after the operation: {dirty(summary)}")
    problems += self_consistency(world, branch)
    problems += compare_to_fresh(world, fresh_pull(tmp, world.api, branch), branch)
    problems += edit_sensitivity(world, branch)
    assert problems == [], "\n".join(problems)


# ===========================================================================
# Scenarios -- each drives one write path and returns the branch to check
# ===========================================================================


@dataclass(frozen=True)
class Seeded:
    """A pulled World and the id of the config the variant seeded."""

    world: World
    config_id: str


def _seeded(tmp: Path, variant: Variant) -> Seeded:
    w = World(tmp)
    cid = w.api.seed(variant)
    w.init()
    w.pull()
    _W[tmp] = w
    return Seeded(world=w, config_id=cid)


def _replace_token(obj: Any, new: str) -> Any:
    if isinstance(obj, str):
        return obj.replace(TOKEN, new)
    if isinstance(obj, list):
        return [_replace_token(v, new) for v in obj]
    if isinstance(obj, dict):
        return {k: _replace_token(v, new) for k, v in obj.items()}
    return obj


def _remote_edit(api: Api, branch: int, key: tuple[str, str]) -> None:
    """Change everything about a config remotely except its name (path)."""
    cfg = api.remote[branch][key]
    # The token stays a prefix, so the edit oracle can still find it.
    for field in ("description", "configuration", "rows"):
        cfg[field] = _replace_token(cfg[field], f"{TOKEN}r")


def sc_pull(tmp: Path, v: Variant) -> int | None:
    """sync pull (new config) -- sync_service.pull, new entry."""
    _seeded(tmp, v)
    return None


def sc_pull_remote_update(tmp: Path, v: Variant) -> int | None:
    """sync pull after a remote change -- sync_service.pull, rewrite branch."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    _remote_edit(w.api, PROD, (v.component_id, cid))
    w.pull()
    _W[tmp] = w
    return None


def sc_pull_force_remote_update(tmp: Path, v: Variant) -> int | None:
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    _remote_edit(w.api, PROD, (v.component_id, cid))
    w.pull(force=True)
    _W[tmp] = w
    return None


def sc_pull_theirs_over_local_edits(tmp: Path, v: Variant) -> int | None:
    """sync pull --theirs after editing every local file."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    d = w.config_dir(cid)
    for f in (CONFIG_FILE, *EXTRA_FILES):
        if (d / f).exists():
            semantic_edit(d / f)
    for rf in d.glob("rows/*/_config.yml"):
        semantic_edit(rf)
    w.pull(theirs=True)
    _W[tmp] = w
    return None


def sc_pull_idempotent(tmp: Path, v: Variant) -> int | None:
    """Second pull with nothing changed -- the remote_unchanged reuse branch."""
    w = _seeded(tmp, v).world
    w.pull()
    _W[tmp] = w
    return None


def sc_push_create(tmp: Path, v: Variant) -> int | None:
    """push of a new untracked config (+ rows) -- stamp_created_config."""
    w = World(tmp)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v)
    assert w.push()["created"] >= 1
    _W[tmp] = w
    return None


def sc_push_create_promote_dev(tmp: Path, v: Variant) -> int | None:
    """Author in main/, ``push --branch DEV`` (the PR #917 customer loop)."""
    w = World(tmp)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v)
    assert w.push(branch=DEV)["created"] >= 1
    _W[tmp] = w
    return DEV


def sc_promote_pulled_to_dev(tmp: Path, v: Variant) -> int | None:
    """A pulled production config pushed to an (empty) dev branch -- create on DEV."""
    w = _seeded(tmp, v).world
    w.push(branch=DEV)
    _W[tmp] = w
    return DEV


def sc_promote_edit_to_branched_dev(tmp: Path, v: Variant) -> int | None:
    """Realistic dev branch (copy of prod, same ids): edit in main/, push --branch DEV."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)
    _W[tmp] = w
    return DEV


def sc_push_update_config_yml(tmp: Path, v: Variant) -> int | None:
    """push after editing _config.yml -- stamp_updated_config."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    assert w.push()["updated"] >= 1
    _W[tmp] = w
    return None


def sc_push_update_companion(tmp: Path, v: Variant) -> int | None:
    """push after editing every companion file (code, packages, description)."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    d = w.config_dir(cid)
    for f in EXTRA_FILES:
        if (d / f).exists():
            semantic_edit(d / f)
    assert w.push()["updated"] >= 1
    _W[tmp] = w
    return None


def _with_disabled(data: dict[str, Any]) -> dict[str, Any]:
    """Add ``is_disabled: true`` where pull writes it (right after ``description``)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        out[key] = value
        if key == "description":
            out["is_disabled"] = True
    if "is_disabled" not in out:  # description extracted to _description.md
        out = {}
        for key, value in data.items():
            out[key] = value
            if key == "name":
                out["is_disabled"] = True
    return out


def sc_push_disable(tmp: Path, v: Variant) -> int | None:
    """push of ``is_disabled: true`` added to _config.yml."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    f = w.config_dir(cid) / CONFIG_FILE
    data = _with_disabled(yaml.safe_load(f.read_text()))
    f.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
    assert w.push()["updated"] >= 1
    assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is True
    _W[tmp] = w
    return None


def sc_push_rename_in_yaml(tmp: Path, v: Variant) -> int | None:
    """push of a ``name:`` change (directory keeps its old name)."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    f = w.config_dir(cid) / CONFIG_FILE
    data = yaml.safe_load(f.read_text())
    data["name"] = "Renamed"
    f.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
    assert w.push()["updated"] >= 1
    _W[tmp] = w
    return None


def sc_push_remove_description(tmp: Path, v: Variant) -> int | None:
    """push after deleting _description.md (clear the description)."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    (w.config_dir(cid) / "_description.md").unlink()
    assert w.push()["updated"] >= 1
    assert w.api.get(PROD, v.component_id, cid)["description"] == ""
    _W[tmp] = w
    return None


def sc_adopt_by_id_then_pull(tmp: Path, v: Variant) -> int | None:
    """Manifest lost the entry (file still carries its config_id, unedited), then
    sync pull re-adopts it. (Adopt + EDIT without a base hash is a conflict by
    design, #792 E; adopt + edit WITH a base hash is ``scaffold_then_push``,
    which drives stamp_updated_config's register-the-adopted-entry branch.)"""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    manifest = load_manifest(w.root)
    manifest.configurations = [c for c in manifest.configurations if c.id != cid]
    save_manifest(w.root, manifest)
    w.pull()
    return None


def sc_promote_row_create_to_branched_dev(tmp: Path, v: Variant) -> int | None:
    """Realistic dev branch (copy of prod): add a row dir in main/, push --branch DEV."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    author_row(w.config_dir(cid), v, {**_rows()[0], "name": "Row 3"})
    assert w.push(branch=DEV)["created"] == 1
    return DEV


def sc_scaffold_then_push(tmp: Path, v: Variant) -> int | None:
    """``config new --push --output-dir`` (mirrored body) then edit + sync push."""
    w = World(tmp)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    created = w.api._create(v.component_id, "Scaffolded", v.body(), f"{TOKEN} description")
    d = w.root / "main" / v.component_type / v.component_id / "scaffolded"
    materialize_pushed_config(
        v.component_id,
        created["id"],
        created["name"],
        created["description"],
        created["configuration"],
        d,
    )
    semantic_edit(d / CONFIG_FILE)
    w.push()
    _W[tmp] = w
    return None


def sc_scaffold_then_pull(tmp: Path, v: Variant) -> int | None:
    """``config new --push --output-dir`` (mirrored body) then sync pull."""
    w = World(tmp)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    created = w.api._create(v.component_id, "Scaffolded", v.body(), f"{TOKEN} description")
    d = w.root / "main" / v.component_type / v.component_id / "scaffolded"
    materialize_pushed_config(
        v.component_id,
        created["id"],
        created["name"],
        created["description"],
        created["configuration"],
        d,
    )
    w.pull()
    _W[tmp] = w
    return None


def sc_config_rename_command_then_pull(tmp: Path, v: Variant) -> int | None:
    """``kbagent config rename --directory`` (pops pull_hash/pull_config_hash)
    followed by sync pull."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    ConfigService(
        config_store=w.store,
        client_factory=lambda url, token: w.api.client(),
        ai_client_factory=MagicMock(),
    ).rename_config("prod", v.component_id, cid, "Renamed", directory=w.root)
    w.pull()
    _W[tmp] = w
    return None


def sc_clone(tmp: Path, v: Variant) -> int | None:
    """``sync clone`` of a pulled reference tree into an empty target project."""
    src = World(tmp, name="source")
    src.api.seed(v)
    src.init()
    src.pull()
    target_api = Api(first_id=5000)
    target_api.types = dict(src.api.types)
    w = World(tmp, target_api, name="target")
    w.root.rmdir()
    result = w.svc.clone_project(src.root, "prod", w.root)
    assert result.get("errors", []) == [], result
    _W[tmp] = w
    return None


# Row-only scenarios -------------------------------------------------------


def sc_row_create(tmp: Path, v: Variant) -> int | None:
    """New row dir under a tracked config -- _push_create_row."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    author_row(w.config_dir(cid), v, {**_rows()[0], "name": "Row 3"})
    assert w.push()["created"] == 1
    _W[tmp] = w
    return None


def sc_row_update(tmp: Path, v: Variant) -> int | None:
    """Edited row file -- push_update_row."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    (rf, *_) = sorted(w.config_dir(cid).glob("rows/*/_config.yml"))
    semantic_edit(rf)
    assert w.push()["updated"] == 1
    _W[tmp] = w
    return None


def sc_row_delete(tmp: Path, v: Variant) -> int | None:
    """Deleted row dir, push --force -- _push_delete_row."""
    import shutil

    seeded = _seeded(tmp, v)

    w, cid = seeded.world, seeded.config_id
    (rf, *_) = sorted(w.config_dir(cid).glob("rows/*/_config.yml"))
    shutil.rmtree(rf.parent)
    assert w.push(force=True)["deleted"] == 1
    _W[tmp] = w
    return None


def sc_row_remote_delete_pull(tmp: Path, v: Variant) -> int | None:
    """Row deleted remotely, then pull -- sweep_stale_rows."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    w.api.get(PROD, v.component_id, cid)["rows"].pop(0)
    w.pull()
    _W[tmp] = w
    return None


def sc_row_disable(tmp: Path, v: Variant) -> int | None:
    """``is_disabled: true`` added to a row file, pushed."""
    seeded = _seeded(tmp, v)
    w, cid = seeded.world, seeded.config_id
    (rf, *_) = sorted(w.config_dir(cid).glob("rows/*/_config.yml"))
    data = _with_disabled(yaml.safe_load(rf.read_text()))
    rf.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
    assert w.push()["updated"] == 1
    _W[tmp] = w
    return None


# The world each scenario leaves behind, keyed by its tmp dir.
_W: dict[Path, World] = {}


# ===========================================================================
# Parametrization + known findings
# ===========================================================================

SCENARIOS: dict[str, tuple[Callable[[Path, Variant], int | None], tuple[str, ...]]] = {
    "pull": (sc_pull, CONFIG_VARIANTS),
    "pull_idempotent": (sc_pull_idempotent, CONFIG_VARIANTS),
    "pull_remote_update": (sc_pull_remote_update, CONFIG_VARIANTS),
    "pull_force_remote_update": (sc_pull_force_remote_update, CONFIG_VARIANTS),
    "pull_theirs_over_local_edits": (sc_pull_theirs_over_local_edits, CONFIG_VARIANTS),
    "push_create": (sc_push_create, CONFIG_VARIANTS),
    "push_create_promote_dev": (sc_push_create_promote_dev, CONFIG_VARIANTS),
    "promote_pulled_to_dev": (sc_promote_pulled_to_dev, CONFIG_VARIANTS),
    "promote_edit_to_branched_dev": (sc_promote_edit_to_branched_dev, CONFIG_VARIANTS),
    "push_update_config_yml": (sc_push_update_config_yml, CONFIG_VARIANTS),
    "push_update_companion": (sc_push_update_companion, CONFIG_VARIANTS),
    "push_disable": (sc_push_disable, CONFIG_VARIANTS),
    "push_rename_in_yaml": (sc_push_rename_in_yaml, CONFIG_VARIANTS),
    "push_remove_description": (sc_push_remove_description, CONFIG_VARIANTS),
    "adopt_by_id_then_pull": (sc_adopt_by_id_then_pull, CONFIG_VARIANTS),
    "scaffold_then_push": (sc_scaffold_then_push, ("extractor", "sql", "python", "app")),
    "scaffold_then_pull": (sc_scaffold_then_pull, ("extractor", "sql", "python", "app")),
    "config_rename_command_then_pull": (sc_config_rename_command_then_pull, CONFIG_VARIANTS),
    "clone": (sc_clone, CONFIG_VARIANTS),
    "row_create": (sc_row_create, ("rows",)),
    "row_update": (sc_row_update, ("rows",)),
    "row_delete": (sc_row_delete, ("rows",)),
    "row_remote_delete_pull": (sc_row_remote_delete_pull, ("rows",)),
    "row_disable": (sc_row_disable, ("rows",)),
    "promote_row_create_to_branched_dev": (sc_promote_row_create_to_branched_dev, ("rows",)),
}

# Finding texts (severity first). Full stories: the explicit tests at the end.
F1 = (
    "F1 (MED): push --branch DEV of a config authored in main/ records a DEV entry "
    "whose branch tree is dev/, so diff also scans its main/ dir as an untracked "
    "adopted config: any edit diffs as modified AND conflict, push says 'Remote "
    "changes detected' (branch_scope.find_untracked_configs)"
)
F2 = (
    "F2 (HIGH): push --branch DEV to a dev branch holding the same config id "
    "re-stamps the PRODUCTION entry with the dev state and records no DEV entry "
    "(_sync_writeback.stamp_updated_config matches component+id, not branch)"
)
F3 = (
    "F3 (HIGH): a row created by push --branch DEV is recorded under the "
    "PRODUCTION entry (_sync_push_ops.push_row_change parent lookup ignores branch)"
)
F5 = (
    "F5 (HIGH): a new config authored with rows is created without its rows -- "
    "diff lists no rows under an untracked parent; a second push creates them"
)
F6 = (
    "F6 (MED): promoting a pulled config with rows to a dev branch without it "
    "POSTs the rows under the production config id (sync_service.push fills "
    "created_id_map only from a placeholder entry's previous_id)"
)
L1 = (
    "L1 (LOW): pyproject.toml carries config-derived fields (name, "
    "[tool.keboola] config_id) that push/clone never refresh; pull writes them"
)
L2 = (
    "L2 (LOW, layout only): after clearing a description by deleting "
    "_description.md, _config.yml lacks the `description: ''` a fresh pull writes"
)
L3 = (
    "L3 (LOW): push stamps config_hash_version on ROW entries, pull never does "
    "(apply_stamp in _sync_push_ops vs the row metadata in sync_service.pull); "
    "nothing reads it on rows"
)
L4 = (
    "L4 (LOW): a row created by push keeps no/stale _keboola.row_id in its file "
    "(writeback_after_push is called with config_id='' for rows), so pull_hash "
    "differs from a fresh pull"
)

# (scenario, variant) -> FINDING reason, from the actual run.
FINDINGS: dict[tuple[str, str], str] = {
    **{("push_create_promote_dev", v): F1 for v in ("extractor", "sql", "python")},
    # The rows are never pushed at all (F4) -- invisible to this oracle because a
    # fresh pull has no rows either; see test_new_config_with_rows_promoted_...
    ("push_create_promote_dev", "rows"): f"{F1}; F4 (HIGH) see explicit test",
    ("push_create_promote_dev", "app"): f"{F1}; {L1}",
    **{("promote_edit_to_branched_dev", v): F2 for v in CONFIG_VARIANTS},
    ("promote_row_create_to_branched_dev", "rows"): F3,
    ("push_create", "rows"): F5,
    ("promote_pulled_to_dev", "rows"): F6,
    ("push_create", "app"): L1,
    ("promote_pulled_to_dev", "app"): L1,
    ("push_rename_in_yaml", "python"): L1,
    ("push_rename_in_yaml", "app"): L1,
    ("clone", "app"): L1,
    **{("push_remove_description", v): L2 for v in CONFIG_VARIANTS},
    ("row_create", "rows"): f"{L3}; {L4}",
    ("row_update", "rows"): L3,
    ("row_disable", "rows"): L3,
    ("clone", "rows"): f"{L3}; {L4}",
}


def _cases() -> list[Any]:
    cases = []
    for sname, (_fn, variants) in SCENARIOS.items():
        for vname in variants:
            marks = []
            reason = FINDINGS.get((sname, vname))
            if reason:
                marks.append(pytest.mark.xfail(strict=True, reason=f"FINDING: {reason}"))
            cases.append(pytest.param(sname, vname, marks=marks, id=f"{sname}-{vname}"))
    return cases


@pytest.mark.parametrize(("scenario", "variant"), _cases())
def test_bookkeeping_matches_fresh_pull(tmp_path: Path, scenario: str, variant: str) -> None:
    fn, _ = SCENARIOS[scenario]
    v = VARIANTS[variant]
    branch = fn(tmp_path, v)
    world = _W.pop(tmp_path)
    check(world, tmp_path, branch)


# ===========================================================================
# Phase C / D link backfill (_sync_bindings._refresh_binding_hashes) via clone
# ===========================================================================


@pytest.mark.xfail(strict=True, reason=f"FINDING: {L3}; {L4}")
def test_clone_with_variable_and_flow_links_matches_fresh_pull(tmp_path: Path) -> None:
    """``sync clone`` of a tree whose transformation links a variables config and
    whose flow runs the transformation: Phase C (variables) and Phase D (flow
    task) rewrite those configs after the create, re-stamping their entries."""
    src = World(tmp_path, name="source")
    api = src.api
    api.types.update(
        {
            "keboola.variables": "other",
            "keboola.snowflake-transformation": "transformation",
            "keboola.flow": "other",
        }
    )
    var_id, values_row = "cfg-vars", "row-vals"
    api.remote[PROD][("keboola.variables", var_id)] = {
        "id": var_id,
        "name": "Vars",
        "description": "",
        "configuration": {"variables": [{"name": "x", "type": "string"}]},
        "rows": [
            {
                "id": values_row,
                "name": "Default",
                "description": "",
                "configuration": {"values": [{"name": "x", "value": TOKEN}]},
                "isDisabled": False,
            }
        ],
        "isDisabled": False,
    }
    trans_id = "cfg-trans"
    api.remote[PROD][("keboola.snowflake-transformation", trans_id)] = {
        "id": trans_id,
        "name": "Orders",
        "description": f"{TOKEN} description",
        "configuration": {
            **_sql_body(),
            "variables_id": var_id,
            "variables_values_id": values_row,
        },
        "rows": [],
        "isDisabled": False,
    }
    api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": f"{TOKEN} description",
        "configuration": {
            "phases": [{"id": "1", "name": "P", "dependsOn": []}],
            "tasks": [
                {
                    "id": "t1",
                    "name": "T",
                    "phase": "1",
                    "enabled": True,
                    "task": {
                        "type": "job",
                        "componentId": "keboola.snowflake-transformation",
                        "configId": trans_id,
                        "mode": "run",
                    },
                }
            ],
        },
        "rows": [],
        "isDisabled": False,
    }
    src.init()
    src.pull()

    target_api = Api(first_id=5000)
    target_api.types = dict(api.types)
    w = World(tmp_path, target_api, name="target")
    w.root.rmdir()
    result = w.svc.clone_project(src.root, "prod", w.root)
    assert result.get("errors", []) == [], result
    linked = target_api.remote[PROD]
    (new_trans,) = [cfg for (comp, _), cfg in linked.items() if comp.endswith("transformation")]
    (new_vars,) = [cfg for (comp, _), cfg in linked.items() if comp == "keboola.variables"]
    (new_flow,) = [cfg for (comp, _), cfg in linked.items() if comp == "keboola.flow"]
    assert new_trans["configuration"]["variables_id"] == new_vars["id"]
    assert new_flow["configuration"]["tasks"][0]["task"]["configId"] == new_trans["id"]
    check(w, tmp_path, None)


# ===========================================================================
# User-visible consequences of the findings (explicit stories)
# ===========================================================================


def _rows_variant() -> Variant:
    return VARIANTS["rows"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F2 (HIGH): push --branch DEV re-stamps the PRODUCTION manifest "
        "entry with the dev branch's state (stamp_updated_config matches on "
        "component+id, not branch); the prod diff then calls the un-promoted local "
        "edit a REMOTE change and the next `sync pull` overwrites it"
    ),
)
def test_promote_edit_to_dev_then_prod_pull_keeps_local_edit(tmp_path: Path) -> None:
    v = VARIANTS["sql"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    cfg = w.config_dir(cid) / CONFIG_FILE
    semantic_edit(cfg)
    w.push(branch=DEV)  # test the change on the dev branch first

    # Production never got the edit; it must still be a LOCAL modification.
    assert w.diff()["summary"]["modified"] == 1
    w.pull()
    assert "edit_marker" in cfg.read_text(), "sync pull overwrote the local edit"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F3 (HIGH): a row created by push --branch DEV is recorded under "
        "the PRODUCTION entry (push_row_change parent lookup ignores branch); prod "
        "diff calls it remote_deleted and the next `sync pull` deletes the new row dir"
    ),
)
def test_promote_new_row_to_dev_then_prod_pull_keeps_row(tmp_path: Path) -> None:
    v = _rows_variant()
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    row_dir = author_row(w.config_dir(cid), v, {**_rows()[0], "name": "Row 3"})
    w.push(branch=DEV)

    assert w.diff()["summary"]["remote_deleted"] == 0
    w.pull()
    assert row_dir.exists(), "sync pull deleted the locally authored row"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F4 (HIGH): rows of a config authored in main/ and promoted with "
        "push --branch DEV are never pushed -- find_untracked_rows looks for them "
        "under dev/ (branch_tree_path of the DEV entry), every later push says "
        "no_changes"
    ),
)
def test_new_config_with_rows_promoted_to_dev_gets_its_rows(tmp_path: Path) -> None:
    v = _rows_variant()
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v)
    w.push(branch=DEV)
    w.push(branch=DEV)  # even a second push
    ((_, cfg),) = w.api.remote[DEV].items()
    assert len(cfg["rows"]) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F5 (MED): a new config authored with rows is created WITHOUT its "
        "rows on the first push (status pushed, no error); rows only go up on a "
        "second push"
    ),
)
def test_new_config_with_rows_is_complete_after_one_push(tmp_path: Path) -> None:
    v = _rows_variant()
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v)
    w.push()
    ((_, cfg),) = w.api.remote[PROD].items()
    assert len(cfg["rows"]) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F1 (MED): after push --branch DEV creates a config authored in "
        "main/, the DEV entry does not claim its main/ dir; diff sees the dir a "
        "second time as an adopted untracked config, so an edit diffs as modified "
        "AND conflict and push reports 'Remote changes detected. Run sync pull first.'"
    ),
)
def test_edit_after_promote_create_is_not_a_conflict(tmp_path: Path) -> None:
    v = VARIANTS["sql"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    config_dir = author_config(w.root / "main", v)
    w.push(branch=DEV)
    semantic_edit(config_dir / "transform.sql")
    summary = w.diff(branch=DEV)["summary"]
    assert (summary["modified"], summary["conflict"]) == (1, 0)
    assert "skipped" not in w.push(branch=DEV)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F6 (MED): promoting a pulled config with rows to a dev branch that "
        "lacks it creates the config but every row fails -- the rows are POSTed "
        "under the PRODUCTION config id (created_id_map is only filled from a "
        "placeholder entry's previous_id)"
    ),
)
def test_promote_pulled_config_with_rows_to_dev_without_it(tmp_path: Path) -> None:
    v = _rows_variant()
    w = _seeded(tmp_path, v).world
    result = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    assert result["errors"] == []
    ((_, cfg),) = w.api.remote[DEV].items()
    assert len(cfg["rows"]) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING F7 (MED): after promoting a pulled config to a dev branch that "
        "lacks it, the main/ _config.yml carries the DEV config id and a DEV entry "
        "shares the PROD entry's path; the next production `sync pull` deletes "
        "that directory and re-writes the config under '<name>-<id>'"
    ),
)
def test_prod_pull_after_promote_to_dev_keeps_the_config_dir(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    before = w.config_dir(cid)
    w.push(branch=DEV)
    _remote_edit(w.api, PROD, (v.component_id, cid))
    w.pull()
    assert w.config_dir(cid) == before
