"""Tests for scripts/check_pr_interface.py -- the `## Interface changes` gate (spec C10).

They pin the detection of each manifest, error-code, route and environment
variable change with its class, the extraction of the section, each failure
rule of the check with its passing case, and the skeleton.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _load() -> Any:
    name = "_check_pr_interface_under_test"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "check_pr_interface.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pri = _load()


def _option(long: str, **extra: Any) -> dict[str, Any]:
    param: dict[str, Any] = {
        "kind": "option",
        "name": long.lstrip("-").replace("-", "_"),
        "opts": [long],
        "type": "text",
        "default": None,
        "help": f"Help of {long}.",
    }
    param.update(extra)
    return param


def _manifest(*commands: dict[str, Any], aliases: dict[str, str] | None = None) -> dict[str, Any]:
    data = {
        "root": {"help": "Root.", "params": [_option("--json", type="boolean", is_flag=True)]},
        "aliases": aliases or {},
        "commands": list(commands),
    }
    return pri.parse_manifest(json.dumps(data))


def _command(path: str, category: str = "write", *params: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": path,
        "kind": "command",
        "hidden": False,
        "category": category,
        "help": f"Help of {path}.",
        "params": list(params),
    }


BASE_CMD = _command(
    "demo create", "write", _option("--name"), _option("--mode", type="choice", choices=["a", "b"])
)


def _diff(new_cmd: dict[str, Any]) -> list[Any]:
    return pri.diff_manifest(_manifest(BASE_CMD), _manifest(new_cmd))


def _changed(**option_fields: Any) -> dict[str, Any]:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["params"][0].update(option_fields)
    return cmd


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_same_manifest_has_no_items() -> None:
    assert pri.diff_manifest(_manifest(BASE_CMD), _manifest(BASE_CMD)) == []


def test_command_added_is_additive_and_removed_is_breaking() -> None:
    new = _command("demo list", "read")
    [added] = pri.diff_manifest(_manifest(BASE_CMD), _manifest(BASE_CMD, new))
    assert (added.kind, added.subject, added.cls, added.category) == (
        "command",
        "demo list",
        "additive",
        "read",
    )
    [removed] = pri.diff_manifest(_manifest(BASE_CMD, new), _manifest(BASE_CMD))
    assert (removed.subject, removed.cls) == ("demo list", "breaking")


def test_option_added_is_additive_and_required_option_added_is_breaking() -> None:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["params"].append(_option("--extra"))
    [item] = _diff(cmd)
    assert (item.kind, item.subject, item.cls, item.token) == (
        "option",
        "demo create --extra",
        "additive",
        "--extra",
    )
    cmd["params"][-1]["required"] = True
    assert _diff(cmd)[0].cls == "breaking"


def test_option_removed_is_breaking() -> None:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["params"].pop(0)
    [item] = _diff(cmd)
    assert (item.subject, item.changes, item.cls) == (
        "demo create --name",
        ("option removed",),
        "breaking",
    )


@pytest.mark.parametrize(
    ("fields", "cls"),
    [
        ({"opts": ["--name", "-n"]}, "additive"),
        ({"secondary_opts": ["--no-name"]}, "additive"),
        ({"type": "integer"}, "breaking"),
        ({"is_flag": True}, "breaking"),
        ({"default": "x"}, "breaking"),
        ({"required": True}, "breaking"),
        ({"multiple": True}, "additive"),
        ({"envvar": "DEMO_NAME"}, "additive"),
        ({"help": "Other help."}, "help"),
    ],
)
def test_option_change_classes(fields: dict[str, Any], cls: str) -> None:
    [item] = _diff(_changed(**fields))
    assert item.subject == "demo create --name"
    assert item.cls == cls


def test_option_name_removed_is_breaking() -> None:
    base = _command("demo create", "write", _option("--name", opts=["--name", "-n"]))
    [item] = pri.diff_manifest(
        _manifest(base), _manifest(_command("demo create", "write", _option("--name")))
    )
    assert item.cls == "breaking"
    assert item.changes == ("name removed: -n",)


def test_choice_value_removed_is_breaking_and_added_is_additive() -> None:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["params"][1]["choices"] = ["a"]
    assert _diff(cmd)[0].cls == "breaking"
    cmd["params"][1]["choices"] = ["a", "b", "c"]
    assert _diff(cmd)[0].cls == "additive"


def test_option_with_help_and_additive_change_takes_the_higher_class() -> None:
    [item] = _diff(_changed(help="New.", opts=["--name", "-n"]))
    assert item.cls == "additive"
    assert set(item.changes) == {"name added: -n", "help changed"}


@pytest.mark.parametrize(
    ("old", "new", "cls"),
    [
        ("read", "write", "breaking"),
        ("write", "admin", "breaking"),
        ("destructive", "write", "additive"),
    ],
)
def test_category_change(old: str, new: str, cls: str) -> None:
    before, after = _command("demo create", old), _command("demo create", new)
    [item] = pri.diff_manifest(_manifest(before), _manifest(after))
    assert (item.kind, item.cls) == ("command", cls)
    assert item.changes == (f"category {old} -> {new}",)


def test_hidden_change_is_a_note_and_help_change_is_help() -> None:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["hidden"] = True
    assert _diff(cmd)[0].cls == "note"
    cmd = copy.deepcopy(BASE_CMD)
    cmd["help"] = "Other."
    [item] = _diff(cmd)
    assert (item.kind, item.cls) == ("command", "help")


def test_root_option_uses_the_kbagent_path() -> None:
    base = _manifest(BASE_CMD)
    head = copy.deepcopy(base)
    head["commands"]["kbagent"]["params"].append(_option("--quiet"))
    [item] = pri.diff_manifest(base, head)
    assert (item.subject, item.command, item.cls) == ("kbagent --quiet", "kbagent", "additive")


def test_alias_changes() -> None:
    items = pri.diff_manifest(
        _manifest(aliases={"old": "x", "mv": "a"}), _manifest(aliases={"mv": "b", "nw": "y"})
    )
    assert [(i.subject, i.cls) for i in items] == [
        ("mv", "breaking"),
        ("nw", "additive"),
        ("old", "breaking"),
    ]


def test_missing_base_manifest_counts_as_all_added() -> None:
    items = pri.diff_manifest(pri.parse_manifest(None), _manifest(BASE_CMD))
    assert {(i.subject, i.cls) for i in items} == {
        ("demo create", "additive"),
        ("kbagent --json", "additive"),
    }


ERRORS = """| Code | Exit | Type | Description |
|---|---|---|---|
| `NOT_FOUND` | 1 | `api` | Not found |
| `GONE` | 1 | `api` | Gone |
| `SLOW` | 4 | `network` | Slow |
"""


def test_error_code_rows() -> None:
    head = (
        ERRORS.replace("| `GONE` | 1 | `api` | Gone |\n", "")
        .replace("| 4 | `network` | Slow", "| 1 | `network` | Slow")
        .replace("Not found", "Nothing found")
        + "| `NEW_CODE` | 2 | `usage` | New |\n"
    )
    items = pri.diff_error_codes(pri.parse_error_codes(ERRORS), pri.parse_error_codes(head))
    assert [(i.subject, i.cls) for i in items] == [
        ("GONE", "breaking"),
        ("NEW_CODE", "additive"),
        ("NOT_FOUND", "help"),
        ("SLOW", "breaking"),
    ]
    assert items[3].changes == ("exit code 4 -> 1",)


ROUTES = """| Method | Path | Summary |
|---|---|---|
| `GET` | `/projects` | List projects |
| `POST` | `/projects` | Add a project |
"""


def test_route_rows() -> None:
    head = (
        ROUTES.replace("| `POST` | `/projects` | Add a project |\n", "").replace(
            "List projects", "List the projects"
        )
        + "| `DELETE` | `/projects/{alias}` | Remove a project |\n"
    )
    items = pri.diff_routes(pri.parse_routes(ROUTES), pri.parse_routes(head))
    assert [(i.subject, i.cls) for i in items] == [
        ("DELETE /projects/{alias}", "additive"),
        ("GET /projects", "help"),
        ("POST /projects", "breaking"),
    ]


def test_env_var_registry() -> None:
    base = 'ENV_VARS: tuple[EnvVar, ...] = (EnvVar("A_VAR", "x", secret=False, added_in="1"),\n EnvVar(name="B_VAR", purpose="y", secret=False, added_in="1"))'
    head = 'ENV_VARS = (EnvVar("A_VAR", "x", secret=False, added_in="1"), EnvVar("C_VAR", "z", secret=True, added_in="2"))'
    assert pri.parse_env_vars(base) == {"A_VAR", "B_VAR"}
    items = pri.diff_env_vars(pri.parse_env_vars(base), pri.parse_env_vars(head))
    assert [(i.subject, i.cls) for i in items] == [("C_VAR", "additive"), ("B_VAR", "breaking")]


def test_real_env_var_registry_is_parsed() -> None:
    text = (REPO_ROOT / pri.ENV_VARS).read_text(encoding="utf-8")
    names = pri.parse_env_vars(text)
    assert {"KBAGENT_CONFIG_DIR", "KBAGENT_PROJECT", "KBC_TOKEN"} <= names


def test_real_sources_against_themselves_have_no_items() -> None:
    files = {
        path: (REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (pri.MANIFEST, pri.ERROR_CODES, pri.ENDPOINTS, pri.ENV_VARS)
    }
    assert pri.detect_items(files, files) == []
    manifest = pri.parse_manifest(files[pri.MANIFEST])
    assert "workspace create" in manifest["commands"]
    assert pri.parse_error_codes(files[pri.ERROR_CODES])
    assert pri.parse_routes(files[pri.ENDPOINTS])


@pytest.mark.parametrize(
    ("files", "needed"),
    [
        (["src/keboola_agent_cli/cli.py"], True),
        (["docs/cli-manifest.json"], True),
        (["docs/error-codes.md"], True),
        (["docs/web-server-endpoints.md"], True),
        (["scripts/cli_spec_baseline.json"], True),
        (["src/keboola_agent_cli/_ui_dist/index.html"], False),
        (["tests/test_cli.py", "README.md"], False),
        ([], False),
    ],
)
def test_needs_section(files: list[str], needed: bool) -> None:
    assert pri.needs_section(files) is needed


# --------------------------------------------------------------------------
# The section
# --------------------------------------------------------------------------


def test_extract_section_is_case_insensitive_and_stops_at_the_next_heading() -> None:
    body = "## Why\r\n\r\nx\r\n\r\n## interface CHANGES\r\n\r\n- a\r\n### Sub\r\n- b\r\n## Tests\r\n- c\r\n"
    assert pri.extract_section(body) == "\n- a\n### Sub\n- b"


def test_extract_section_ignores_headings_in_fences_and_comments() -> None:
    body = "<!--\n## Interface changes\n-->\n```\n## Interface changes\n```\n## Tests\n"
    assert pri.extract_section(body) is None
    assert pri.extract_section("## Interface changes\n- a\n```\n## Not a heading\n```\n- b") == (
        "- a\n```\n## Not a heading\n```\n- b"
    )


def test_extract_section_level_one_heading_ends_it_and_level_three_is_not_the_section() -> None:
    assert pri.extract_section("## Interface changes\nx\n# End\ny") == "x"
    assert pri.extract_section("### Interface changes\nx") is None


def test_logical_lines_join_wrapped_continuations() -> None:
    section = "- `a b`: one\n  wrapped BREAKING C4\n- second\n\npara\ngraph"
    assert pri.logical_lines(section) == [
        "- `a b`: one wrapped BREAKING C4",
        "- second",
        "para graph",
    ]


def _items(*cmds: dict[str, Any]) -> list[Any]:
    return pri.diff_manifest(_manifest(BASE_CMD), _manifest(*cmds))


def test_rule_1_missing_section_fails_only_when_needed() -> None:
    [problem] = pri.check_section(None, [], needed=True)
    assert "no `## Interface changes` section" in problem
    assert pri.check_section(None, [], needed=False) == []
    assert pri.check_section("None.", [], needed=True) == []


def test_rule_2_each_item_must_be_named() -> None:
    items = _items(_changed(help="New."))
    [problem] = pri.check_section("- The options do not change.", items, needed=True)
    assert "`demo create --name` (option: help changed) is not described" in problem
    assert "`demo create` and `--name`" in problem
    for line in (
        "- `demo create` `--name`: the help names the default.",
        "- `kbagent demo create --name NAME`: the help names the default.",
        "- `demo create`: `--name/--no-name` help.",
    ):
        assert pri.check_section(line, items, needed=True) == [], line


def test_rule_2_option_needs_its_command_on_the_same_line() -> None:
    items = _items(_changed(help="New."))
    section = "- `demo create` changes.\n- `--name`: new help."
    assert len(pri.check_section(section, items, needed=True)) == 1


def test_rule_2_a_subcommand_does_not_name_its_group() -> None:
    assert pri.names_command("- `demo create`", "demo") is False
    assert pri.names_command("- `demo`", "demo")
    assert pri.names_command("- `kbagent demo --x`", "demo")


def test_rule_2_error_code_route_and_variable_mentions() -> None:
    code = pri.diff_error_codes({}, {"NEW_CODE": ("2", "usage", "x")})
    route = pri.diff_routes({}, {("POST", "/demo"): "x"})
    env = pri.diff_env_vars(set(), {"KBAGENT_DEMO"})
    items = code + route + env
    assert len(pri.check_section("- nothing", items, needed=True)) == 3
    section = "- `NEW_CODE`: new.\n- `/demo`: new route.\n- `KBAGENT_DEMO`: new."
    assert pri.check_section(section, items, needed=True) == []
    assert pri.check_section("- `POST /demo` new", route, needed=True) == []


def test_rule_3_breaking_item_needs_breaking_and_c3_or_c4() -> None:
    items = _items(_changed(default="x"))
    base = "- `demo create --name`: default changed. R1: converges."
    problems = pri.check_section(base, items, needed=True)
    assert len(problems) == 1 and "is breaking (spec C1)" in problems[0]
    assert len(pri.check_section(base + " BREAKING.", items, needed=True)) == 1
    assert len(pri.check_section(base + " breaking, C4.", items, needed=True)) == 1
    assert pri.check_section(base + " BREAKING, ships under C4.", items, needed=True) == []
    assert pri.check_section(base + " BREAKING, C3 window.", items, needed=True) == []


def test_rule_4_mutating_command_needs_its_r1_class() -> None:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["params"].append(_option("--extra"))
    items = _items(cmd)
    line = "- `demo create --extra`: new option."
    [problem] = pri.check_section(line, items, needed=True)
    assert "spec R1" in problem and "`demo create`" in problem
    assert pri.check_section(line + " R1: duplicates.", items, needed=True) == []
    assert pri.check_section(line + "\n- `demo create` Converges.", items, needed=True) == []


def test_rule_4_skips_read_commands_and_help_changes() -> None:
    assert (
        pri.check_section("- `demo create --name`: help.", _items(_changed(help="x")), needed=True)
        == []
    )
    read_base = _command("demo list", "read")
    read_head = _command("demo list", "read", _option("--extra"))
    items = pri.diff_manifest(_manifest(read_base), _manifest(read_head))
    assert pri.check_section("- `demo list --extra`: new.", items, needed=True) == []


def test_rule_5_none_with_items_fails() -> None:
    items = _items(_changed(help="x"))
    for section in ("None.", "\n- None\n"):
        problems = pri.check_section(section, items, needed=True)
        assert any("says only `None`" in p for p in problems), section
    for section in ("None.", "None. Only tests change."):
        assert pri.check_section(section, [], needed=True) == []


def test_empty_section_fails_with_or_without_items() -> None:
    template = (REPO_ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    section = pri.extract_section(template)
    assert section is not None
    for items in ([], _items(_changed(help="x"))):
        [problem] = pri.check_section(section, items, needed=True)
        assert "is empty" in problem
    assert pri.check_section(section, [], needed=False) == []


def test_todo_left_from_the_skeleton_fails() -> None:
    items = _items(_changed(help="x"))
    problems = pri.check_section(pri.skeleton(items), items, needed=True)
    assert len(problems) == 1 and "TODO" in problems[0]


# --------------------------------------------------------------------------
# Skeleton
# --------------------------------------------------------------------------


def test_skeleton_lists_every_item_with_class_and_hints() -> None:
    cmd = copy.deepcopy(BASE_CMD)
    cmd["params"][0]["default"] = "x"
    cmd["params"].append(_option("--extra"))
    items = _items(cmd, _command("demo list", "read"))
    items += pri.diff_error_codes({}, {"NEW_CODE": ("2", "usage", "x")})
    text = pri.skeleton(items)
    lines = text.splitlines()
    assert lines[0] == "## Interface changes"
    assert "docs/cli-interface-spec.md#C10" in text
    by_subject = {
        i.subject: next(ln for ln in lines if ln.startswith(f"- `{i.subject}`")) for i in items
    }
    assert set(by_subject) == {"demo create --extra", "demo create --name", "demo list", "NEW_CODE"}
    assert (
        "BREAKING" in by_subject["demo create --name"] and "C3" in by_subject["demo create --name"]
    )
    assert "C4" in by_subject["demo create --name"]
    assert "converges, guarded or duplicates" in by_subject["demo create --extra"]
    assert "Class: additive." in by_subject["demo list"]
    assert "converges" not in by_subject["demo list"]
    assert "Class: additive." in by_subject["NEW_CODE"]
    # Filled in, the skeleton passes: only the TODO placeholders fail.
    assert pri.check_section(text.replace("TODO", "done"), items, needed=True) == []


def test_skeleton_without_items_says_none() -> None:
    text = pri.skeleton([])
    assert text.rstrip().endswith("None.")
    assert pri.check_section(pri.extract_section(text), [], needed=True) == []
