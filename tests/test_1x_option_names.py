"""The kbagent 1.x option names accepted on 0.x (#791).

kbagent 1.0.0 renames options and removes the old names. 0.x accepts the 1.x
names too, so one command form works on both versions during the migration
window. Each case runs the old 0.x form and the 1.x form and checks that the
service gets the same arguments. ``-`` as the path of a ``--X-file`` option
reads stdin.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer.main
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.permissions import OPERATION_REGISTRY
from keboola_agent_cli.services.sharing_service import SharingService

runner = CliRunner()
ALIAS = "prod"

STATE = '{"lastId": 7}'
CONFIG = '{"parameters": {"table": "orders"}}'
ACTION = '{"type": "cli_command", "params": {"argv": ["kbagent", "version"]}}'
FLOW = "phases: []\ntasks: []\n"
SECRETS = '{"#token": "abc"}'
MEMBERS = '[{"account_code": "100"}]'
STORAGE = '{"input": {"tables": []}}'


def a(command: str) -> list[str]:
    """The argv of a command line (shell quoting)."""
    return shlex.split(command)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mocked service must not fall through to a real HTTP call or the real config dir."""
    monkeypatch.setenv("KBAGENT_CONFIG_DIR", str(tmp_path / "isolated-config"))

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("network access in a unit test")

    with (
        patch("httpx.Client.send", side_effect=refuse),
        patch("httpx.AsyncClient.send", side_effect=refuse),
    ):
        yield


def _store(config_dir: Path) -> ConfigStore:
    config_dir.mkdir(parents=True, exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        ALIAS,
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
            project_name="Prod",
            project_id=1234,
        ),
    )
    return store


def _invoke(
    config_dir: Path,
    service_class: str,
    args: list[str],
    *,
    stdin: str | None = None,
    service: MagicMock | None = None,
) -> tuple[Any, MagicMock]:
    """Run ``kbagent --json ARGS`` with ``service_class`` in cli.py replaced by a mock."""
    svc = service if service is not None else MagicMock()
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=_store(config_dir)),
        patch(f"keboola_agent_cli.cli.{service_class}", return_value=svc),
        patch.dict("os.environ", {"KBC_MANAGE_API_TOKEN": "manage-token"}),
    ):
        result = runner.invoke(app, ["--allow-env-manage-token", "--json", *args], input=stdin)
    return result, svc


def _call(tmp_path: Path, name: str, service_class: str, method: str, args: list[str], **kw: Any):
    """The arguments ``method`` of the mocked ``service_class`` got from ``args``."""
    result, svc = _invoke(tmp_path / name, service_class, args, **kw)
    mock = getattr(svc, method)
    assert mock.called, f"{method} not called for {args}: {result.output}"
    return mock.call_args


def _file(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


P = f"--project {ALIAS}"
REF = f"semantic-layer reference-data {{}} {P}"
SCOPE = f"{P} --type metric --context-id c1"
CONSTRAINT = (
    f"semantic-layer add constraint {P} --name c --constraint-type inequality --rule 'a > b'"
)
ORG = "org setup --url https://connection.keboola.com --dry-run"
SHARE = f"sharing share {P} --bucket-id out.c-b --type"
PROMOTE = f"semantic-layer promote --from-project {ALIAS} --to-project {ALIAS} --dry-run"
QUERY = f"workspace query {P} --workspace-id 5"
ROW = f"{P} --component-id c --config-id 1"
STATE_SET = f"config state-set {ROW} --yes"
SYNC = f"component sync-action testConnection --component-id c {P}"
ENCRYPT = f"encrypt values {P} --component-id c"
SECRETS_SET = f"data-app secrets-set {P} --app-id 1"

# (id, service class in cli.py, service method, old form, 1.x form, stdin of both forms)
# fmt: off
CASES: list[tuple[str, str, str, str, str, str | None]] = [
    ("ref get", "SemanticLayerService", "get_reference_data",
     f"{REF.format('get')} --id u1", f"{REF.format('get')} --reference-data-id u1", None),
    ("ref delete", "SemanticLayerService", "delete_reference_data",
     f"{REF.format('delete')} --id u1 --yes",
     f"{REF.format('delete')} --reference-data-id u1 --yes", None),
    ("ref set --members", "SemanticLayerService", "set_reference_data",
     f"{REF.format('set')} --dimension coa --members-file -",
     f"{REF.format('set')} --dimension coa --members '{MEMBERS}'", MEMBERS),
    ("stream detail", "StreamService", "get_source_detail",
     f"stream detail s1 {P}", f"stream detail {P} --source-id s1", None),
    ("stream delete", "StreamService", "delete_source",
     f"stream delete s1 {P} --yes", f"stream delete {P} --yes --source-id s1", None),
    ("scope request-cancel", "SemanticLayerService", "scope_request_delete",
     f"semantic-layer scope request-delete {SCOPE}",
     f"semantic-layer scope request-cancel {SCOPE}", None),
    ("sl scope request-cancel", "SemanticLayerService", "scope_request_delete",
     f"sl scope request-delete {SCOPE}", f"sl scope request-cancel {SCOPE}", None),
    ("create-table", "StorageService", "create_table",
     f"storage create-table {P} --bucket-id in.c-b --name t --column pk:STRING --not-null pk",
     (f"storage create-table {P} --bucket-id in.c-b --name t --column pk:STRING"
     " --not-null-column pk"), None),
    ("org setup", "OrgService", "setup_organization",
     f"{ORG} --project-ids 11,22", f"{ORG} --project-id 11 --project-id 22", None),
    ("org setup merge", "OrgService", "setup_organization",
     f"{ORG} --project-ids 11,22", f"{ORG} --project-ids 11 --project-id 22", None),
    ("share projects", "SharingService", "share",
     f"{SHARE} selected-projects --target-project-ids 11,22",
     f"{SHARE} selected-projects --target-project 11 --target-project 22", None),
    ("share users merge", "SharingService", "share",
     f"{SHARE} selected-users --target-users a@x.cz,b@x.cz",
     f"{SHARE} selected-users --target-users a@x.cz --target-user b@x.cz", None),
    ("add constraint", "SemanticLayerService", "add_constraint",
     f"{CONSTRAINT} --metrics a,b", f"{CONSTRAINT} --metric a --metric b", None),
    ("edit constraint", "SemanticLayerService", "edit_constraint",
     f"semantic-layer edit constraint {P} --name c --new-metrics a,b",
     f"semantic-layer edit constraint {P} --name c --new-metric a --new-metric b", None),
    ("build", "SemanticLayerService", "build_model",
     f"semantic-layer build {P} --dry-run --tables in.c-b.t1,in.c-b.t2",
     f"semantic-layer build {P} --dry-run --table-id in.c-b.t1 --table-id in.c-b.t2", None),
    ("promote", "SemanticLayerService", "promote_model",
     f"{PROMOTE} --types metrics,datasets", f"{PROMOTE} --type metrics --type datasets", None),
    ("workspace load", "WorkspaceService", "load_tables",
     f"workspace load {P} --workspace-id 5 --force --tables in.c-b.t1 --tables in.c-b.t2",
     f"workspace load {P} --workspace-id 5 --force --table-id in.c-b.t1 --table-id in.c-b.t2",
     None),
    ("download-table", "StorageService", "download_table",
     f"storage download-table {P} --table-id in.c-b.t --columns a",
     f"storage download-table {P} --table-id in.c-b.t --column a", None),
    ("unload-table", "StorageService", "unload_table_to_file",
     f"storage unload-table {P} --table-id in.c-b.t --columns a",
     f"storage unload-table {P} --table-id in.c-b.t --column a", None),
    ("describe-bucket", "StorageService", "describe_bucket",
     f"storage describe-bucket {P} --bucket-id in.c-b --text Sales",
     f"storage describe-bucket {P} --bucket-id in.c-b --description Sales", None),
    ("describe-table stdin", "StorageService", "describe_table",
     f"storage describe-table {P} --table-id in.c-b.t --stdin",
     f"storage describe-table {P} --table-id in.c-b.t --description-file -", "Sales"),
    ("description-set stdin", "BranchService", "set_project_description",
     f"project description-set {P} --stdin",
     f"project description-set {P} --description-file -", "# Hi"),
    ("metadata-set --value", "BranchService", "set_branch_metadata",
     f"branch metadata-set {P} --key k --text v", f"branch metadata-set {P} --key k --value v",
     None),
    ("metadata-set stdin", "BranchService", "set_branch_metadata",
     f"branch metadata-set {P} --key k --stdin",
     f"branch metadata-set {P} --key k --value-file -", "v"),
    ("workspace query stdin", "WorkspaceService", "execute_query",
     f"{QUERY} --sql 'SELECT 1'", f"{QUERY} --sql-file -", "SELECT 1"),
    ("agent create --definition", "AgentService", "create_task",
     f"agent create --name n --manual --from-file '{ACTION}'",
     f"agent create --name n --manual --definition '{ACTION}'", None),
    ("agent create stdin", "AgentService", "create_task",
     "agent create --name n --manual --from-file -",
     "agent create --name n --manual --definition-file -", ACTION),
    ("flow new --definition", "FlowService", "create_flow",
     f"flow new {P} --name f --file '{FLOW}'", f"flow new {P} --name f --definition '{FLOW}'",
     None),
    ("flow update stdin", "FlowService", "update_flow",
     f"flow update {P} --flow-id 9 --file -", f"flow update {P} --flow-id 9 --definition-file -",
     FLOW),
    ("encrypt --values", "EncryptService", "encrypt",
     f"{ENCRYPT} --input '{SECRETS}'", f"{ENCRYPT} --values '{SECRETS}'", None),
    ("encrypt stdin", "EncryptService", "encrypt",
     f"{ENCRYPT} --input -", f"{ENCRYPT} --values-file -", SECRETS),
    ("secrets-set stdin", "DataAppService", "set_data_app_secrets",
     f"{SECRETS_SET} --secret '#token=abc'", f"{SECRETS_SET} --secret-file -", SECRETS),
    ("row-create stdin", "ConfigService", "create_config_row",
     f"config row-create {ROW} --name r --configuration -",
     f"config row-create {ROW} --name r --configuration-file -", CONFIG),
    ("row-update stdin", "ConfigService", "update_config_row",
     f"config row-update {ROW} --row-id 2 --configuration -",
     f"config row-update {ROW} --row-id 2 --configuration-file -", CONFIG),
    ("config update stdin", "ConfigService", "update_config",
     f"config update {ROW} --configuration -", f"config update {ROW} --configuration-file -",
     CONFIG),
    ("state-set stdin", "ConfigService", "set_config_state",
     f"{STATE_SET} --state -", f"{STATE_SET} --state-file -", STATE),
    ("sync-action stdin", "ComponentService", "run_sync_action",
     f"{SYNC} --config-data -", f"{SYNC} --config-data-file -", CONFIG),
]
# fmt: on


@pytest.mark.parametrize(
    ("service_class", "method", "old", "new", "stdin"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_new_form_reaches_the_service_like_the_old_form(
    tmp_path: Path, service_class: str, method: str, old: str, new: str, stdin: str | None
) -> None:
    expected = _call(tmp_path, "old", service_class, method, a(old), stdin=stdin)
    assert _call(tmp_path, "new", service_class, method, a(new), stdin=stdin) == expected


@pytest.mark.parametrize(
    ("service_class", "method", "command", "old_option", "new_option"),
    [
        (
            "StorageService",
            "describe_batch",
            f"storage describe-batch {P}",
            "--from-file",
            "--file",
        ),
        ("WorkspaceService", "execute_query", QUERY, "--file", "--sql-file"),
        ("FlowService", "create_flow", f"flow new {P} --name f", "--file", "--definition-file"),
        ("DataAppService", "set_data_app_secrets", SECRETS_SET, "--secrets-file", "--secret-file"),
        (
            "StorageService",
            "describe_bucket",
            f"storage describe-bucket {P} --bucket-id in.c-b",
            "--file",
            "--description-file",
        ),
    ],
    ids=["describe-batch", "workspace query", "flow new", "secrets-set", "describe-bucket"],
)
def test_file_options_read_the_same_file(
    tmp_path: Path, service_class: str, method: str, command: str, old_option: str, new_option: str
) -> None:
    """A 1.x file option reads the file like its 0.x name."""
    flow = command.startswith("flow")
    path = _file(tmp_path, "input.txt", FLOW if flow else SECRETS)
    old_value = f"@{path}" if flow else path  # 0.x `flow --file` takes inline text or @path
    expected = _call(tmp_path, "old", service_class, method, [*a(command), old_option, old_value])
    new = [*a(command), new_option, path]
    assert _call(tmp_path, "new", service_class, method, new) == expected


def test_import_type_next_to_types(tmp_path: Path) -> None:
    path = _file(tmp_path, "snap.json", "{}")
    command = f"semantic-layer import {P} --dry-run --file {path}"
    expected = _call(
        tmp_path, "old", "SemanticLayerService", "import_snapshot", a(f"{command} --types metrics")
    )
    new = a(f"{command} --type metrics")
    assert _call(tmp_path, "new", "SemanticLayerService", "import_snapshot", new) == expected


def test_project_invite_file_is_the_from_csv_option(tmp_path: Path) -> None:
    csv = _file(tmp_path, "people.csv", "email,project,role\na@x.cz,prod,guest\n")
    command = a("project invite --dry-run --default-role guest")
    expected = _call(tmp_path, "old", "MemberService", "invite_bulk", [*command, "--from-csv", csv])
    new = [*command, "--file", csv]
    assert _call(tmp_path, "new", "MemberService", "invite_bulk", new) == expected


def test_token_list_column_selects_the_same_table(tmp_path: Path) -> None:
    """--columns only shapes the human table; --column must render the same one."""
    outputs = []
    for option in ("--columns", "--column"):
        svc = MagicMock()
        svc.list_tokens.return_value = {"tokens": [{"id": "1", "description": "ci"}], "errors": []}
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=_store(tmp_path / option)),
            patch("keboola_agent_cli.cli.TokenService", return_value=svc),
        ):
            result = runner.invoke(app, a(f"token list {P} {option} id {option} description"))
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
    assert outputs[0] == outputs[1]


def test_lineage_info_file_reads_the_cache_like_load(tmp_path: Path) -> None:
    path = _file(tmp_path, "lineage.json", "{}")
    outputs = []
    for option in ("--load", "--file", "-l"):
        result = runner.invoke(app, ["--json", "lineage", "info", option, path])
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
    assert outputs[0] == outputs[1] == outputs[2]


def test_lineage_show_and_server_take_file() -> None:
    root: Any = typer.main.get_command(app)
    for command in ("show", "info", "server"):
        params = root.commands["lineage"].commands[command].params
        load = next(p for p in params if p.name == "load")
        assert load.opts == ["--load", "--file", "-l"]


def _dev_portal(args: list[str], stdin: str | None = None) -> MagicMock:
    svc = MagicMock()
    with (
        patch("keboola_agent_cli.commands.dev_portal.get_dev_portal_service", return_value=svc),
        patch("keboola_agent_cli.commands.dev_portal.resolve_identity_alias", return_value="me"),
    ):
        runner.invoke(app, ["--json", "dev-portal", *args, "--dry-run"], input=stdin)
    return svc


def test_dev_portal_data_file_is_the_data_option(tmp_path: Path) -> None:
    payload = _file(tmp_path, "app.json", '{"name": "x"}')
    old = _dev_portal(["create", "--vendor", "v", "--data", payload]).prepare_create
    new = _dev_portal(["create", "--vendor", "v", "--data-file", payload]).prepare_create
    assert old.called
    assert old.call_args == new.call_args


def test_dev_portal_patch_value_file_reads_stdin() -> None:
    command = ["patch", "--app", "v.a", "--property", "shortDescription"]
    old = _dev_portal([*command, "--value", "Short text"]).prepare_patch
    new = _dev_portal([*command, "--value-file", "-"], stdin="Short text").prepare_patch
    assert old.called
    assert old.call_args == new.call_args


@pytest.mark.parametrize("method", ["post", "patch"])
@pytest.mark.parametrize("from_stdin", [True, False])
def test_http_body_file_sends_the_body(tmp_path: Path, method: str, from_stdin: bool) -> None:
    body = '{"a": 1}'
    body_file = "-" if from_stdin else _file(tmp_path, "b.json", body)
    calls = []
    for args, stdin in ((["--body", body], None), (["--body-file", body_file], body)):
        with patch("keboola_agent_cli.commands.http_client.HttpForwarderService.request") as req:
            req.return_value = MagicMock(decoded={"ok": True})
            runner.invoke(app, ["--json", "http", method, "/x", *args], input=stdin)
        assert req.called
        calls.append((*req.call_args.args, json.loads(req.call_args.kwargs["body"])))
    assert calls[0] == calls[1]


def _agent_async(tmp_path: Path, method: str, args: list[str], stdin: str | None = None) -> Any:
    svc = MagicMock()
    setattr(svc, method, AsyncMock(return_value=MagicMock()))
    _invoke(tmp_path, "AgentService", args, stdin=stdin, service=svc)
    mock = getattr(svc, method)
    assert mock.called
    return mock.call_args


def test_agent_run_runtime_input_file(tmp_path: Path) -> None:
    old = _agent_async(tmp_path / "old", "run_task", a("agent run t1 --runtime-input '{\"k\": 1}'"))
    new = _agent_async(
        tmp_path / "new", "run_task", a("agent run t1 --runtime-input-file -"), '{"k": 1}'
    )
    assert old == new


def test_agent_test_definition_file(tmp_path: Path) -> None:
    path = _file(tmp_path, "action.json", ACTION)
    old = _agent_async(tmp_path / "old", "test_action", ["agent", "test", "--from-file", ACTION])
    new = _agent_async(
        tmp_path / "new", "test_action", ["agent", "test", "--definition-file", path]
    )
    assert old == new


def test_flow_validate_definition_file(tmp_path: Path) -> None:
    path = _file(tmp_path, "flow.yaml", FLOW)
    outputs = []
    for args in (["--file", f"@{path}"], ["--definition", FLOW], ["--definition-file", path]):
        result, _ = _invoke(tmp_path / args[0], "FlowService", ["flow", "validate", *args])
        assert result.exit_code == 0, result.output
        outputs.append(json.loads(result.output)["data"])
    assert outputs[0] == outputs[1] == outputs[2]


def test_merge_request_resolve_resolved_file(tmp_path: Path) -> None:
    body = '{"name": "n", "description": "", "isDisabled": false, "configuration": {}, "rows": []}'
    path = _file(tmp_path, "resolved.json", body)
    command = a(f"merge-request resolve {P} --merge-request-id 7 --component-id c --config-id 1")
    calls = []
    for args in (["--resolved", body], ["--resolved-file", path]):
        svc = MagicMock()
        svc.get_merge_request_row.return_value = {"id": 7, "branches": {"branchFromId": 3}}
        _invoke(tmp_path / args[0], "MergeRequestService", [*command, *args], service=svc)
        assert svc.resolve_conflict.called
        calls.append(svc.resolve_conflict.call_args)
    assert calls[0] == calls[1]


def _transformation(tmp_path: Path, method: str, args: list[str], stdin: str | None) -> Any:
    svc = MagicMock()
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=_store(tmp_path)),
        patch("keboola_agent_cli.commands.transformation.TransformationService", return_value=svc),
    ):
        runner.invoke(app, ["--json", "transformation", *args], input=stdin)
    mock = getattr(svc, method)
    assert mock.called
    return mock.call_args


def test_transformation_edit_storage_file_and_op_file_stdin(tmp_path: Path) -> None:
    op = '[{"op": "str_replace", "search_for": "a", "replace_with": "b"}]'
    command = a(f"edit {P} --config-id 1 --change-description x")
    ops = _file(tmp_path, "ops.json", op)
    storage = _file(tmp_path, "storage.json", STORAGE)
    old = [*command, "--op-file", ops, "--storage", STORAGE]
    new = [*command, "--op-file", "-", "--storage-file", storage]
    assert _transformation(tmp_path / "old", "edit", old, None) == _transformation(
        tmp_path / "new", "edit", new, op
    )


def test_transformation_create_sql_file_stdin(tmp_path: Path) -> None:
    command = a(f"create {P} --name t --dry-run")
    old = _transformation(tmp_path / "old", "create", [*command, "--sql", "SELECT 1;"], None)
    new = _transformation(tmp_path / "new", "create", [*command, "--sql-file", "-"], "SELECT 1;")
    assert old == new


def test_data_app_create_description_file_stdin(tmp_path: Path) -> None:
    command = f"data-app create {P} --name App --slug app --use-managed-git-repo"
    old = _call(
        tmp_path, "old", "DataAppService", "create_data_app", a(f"{command} --description About")
    )
    new = a(f"{command} --description-file -")
    assert _call(tmp_path, "new", "DataAppService", "create_data_app", new, stdin="About") == old


# --- conflicts and required values --------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"stream detail s1 {P} --source-id s1",
        f"stream delete s1 {P} --yes --source-id s1",
        f"agent create --name n --from-file '{ACTION}' --definition-file -",
        "agent run t1 --runtime-input '{}' --runtime-input-file -",
        f"flow new {P} --name f --definition '{FLOW}' --definition-file -",
        f"{ENCRYPT} --values '{SECRETS}' --values-file -",
        f"{STATE_SET} --state '{STATE}' --state-file -",
        f"config row-create {ROW} --name r --configuration '{CONFIG}' --configuration-file -",
        f"{SYNC} --config-data '{CONFIG}' --config-data-file -",
        "http post /x --body '{}' --body-file -",
        f"{REF.format('set')} --dimension coa --members '{MEMBERS}' --members-file -",
        (
            f"transformation edit {P} --config-id 1 --change-description x --storage '{STORAGE}'"
            " --storage-file -"
        ),
        (
            f"merge-request resolve {P} --merge-request-id 7 --component-id c --config-id 1"
            " --resolved '{}' --resolved-file -"
        ),
    ],
    ids=lambda command: " ".join(command.split()[:2]),
)
def test_both_forms_are_a_usage_error(tmp_path: Path, command: str) -> None:
    result, _ = _invoke(tmp_path, "StreamService", a(command), stdin="{}")
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize(
    "command",
    [
        f"stream delete {P} --yes",
        f"{REF.format('set')} --dimension coa",
        ENCRYPT,
        STATE_SET,
        "flow validate",
        CONSTRAINT,
    ],
    ids=lambda command: " ".join(command.split()[:3]),
)
def test_a_required_value_is_still_required(tmp_path: Path, command: str) -> None:
    result, _ = _invoke(tmp_path, "StreamService", a(command))
    assert result.exit_code == 2, result.output


# --- sharing unlink --dry-run / --yes ------------------------------------------------------


def _unlink(tmp_path: Path, bucket: dict[str, Any], extra: list[str]) -> tuple[Any, MagicMock]:
    store = _store(tmp_path)
    client = MagicMock()
    client.get_bucket_detail.return_value = bucket
    service = SharingService(config_store=store, client_factory=lambda _url, _token: client)
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.SharingService", return_value=service),
    ):
        result = runner.invoke(
            app, ["--json", "sharing", "unlink", *a(P), "--bucket-id", bucket["id"], *extra]
        )
    return result, client


LINKED = {"id": "in.c-shared", "sourceBucket": {"id": "out.c-data"}}


def test_sharing_unlink_dry_run_deletes_nothing(tmp_path: Path) -> None:
    result, client = _unlink(tmp_path, LINKED, ["--dry-run"])
    assert result.exit_code == 0, result.output
    client.delete_bucket.assert_not_called()
    assert json.loads(result.output)["data"] == {
        "project_alias": ALIAS,
        "bucket_id": "in.c-shared",
        "source_bucket_id": "out.c-data",
        "dry_run": True,
        "message": f"Would delete linked bucket 'in.c-shared' from project '{ALIAS}'.",
    }


def test_sharing_unlink_dry_run_refuses_a_bucket_that_is_not_linked(tmp_path: Path) -> None:
    result, client = _unlink(tmp_path, {"id": "in.c-own"}, ["--dry-run"])
    assert result.exit_code != 0
    assert json.loads(result.output)["error"]["code"] == "NOT_LINKED_BUCKET"
    client.delete_bucket.assert_not_called()


@pytest.mark.parametrize("yes", [[], ["--yes"], ["-y"]])
def test_sharing_unlink_yes_changes_nothing(tmp_path: Path, yes: list[str]) -> None:
    result, client = _unlink(tmp_path, LINKED, yes)
    assert result.exit_code == 0, result.output
    client.delete_bucket.assert_called_once_with(bucket_id="in.c-shared", force=True)
    assert "dry_run" not in json.loads(result.output)["data"]


# --- semantic-layer scope request-cancel ---------------------------------------------------


def test_request_cancel_has_the_permission_class_of_request_delete() -> None:
    assert (
        OPERATION_REGISTRY["semantic-layer.scope.request-cancel"]
        == OPERATION_REGISTRY["semantic-layer.scope.request-delete"]
    )


@pytest.mark.parametrize("command", ["request-delete", "request-cancel"])
def test_request_cancel_is_denied_like_request_delete(tmp_path: Path, command: str) -> None:
    with patch("keboola_agent_cli.cli.ConfigStore", return_value=_store(tmp_path)):
        result = runner.invoke(
            app, ["--deny-writes", "--json", "semantic-layer", "scope", command, *a(SCOPE)]
        )
    assert result.exit_code == 6, result.output
