"""An empty flow definition is refused before any write.

Before this fix ``kbagent flow new --definition-file empty.yaml`` created a
keboola.flow configuration with no phases and exit 0 (seen live), and
``--definition ""`` was read as "no definition". A definition that is given
must be usable; only an omitted one still creates an empty flow (the scaffold
the Keboola UI's "Create flow" also makes).

Covered: the CLI loader (new / update / validate), the service rule (create /
update, including the merged update body) and the REST routes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.flow_service import FlowService
from keboola_agent_cli.services.flow_validation import validate_conditional_flow

runner = CliRunner()
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

PHASES = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
TASKS = [
    {
        "id": "t1",
        "name": "T1",
        "phase": "p1",
        "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
    }
]

# Documents that carry no definition at all.
EMPTY_DOCUMENTS = ["", "   \n   \n", "# only a comment\n", "null\n", "~\n"]


def _store(tmp_path: Path) -> ConfigStore:
    """The config store of ``tmp_path``; the project is added on the first call."""
    store = ConfigStore(config_dir=tmp_path / "cfg")
    if store.get_project("prod") is not None:
        return store
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com", token=TOKEN, project_id=1, project_name="p"
        ),
    )
    return store


def _keboola_client(configuration: dict[str, Any] | None = None) -> MagicMock:
    client = MagicMock()
    client.create_config.return_value = {"id": "900", "name": "F"}
    client.update_config.return_value = {"id": "900", "name": "F"}
    client.get_config_detail.return_value = {
        "id": "900",
        "name": "F",
        "configuration": configuration if configuration is not None else {},
    }
    return client


def _service(store: ConfigStore, client: MagicMock) -> FlowService:
    ai = MagicMock()
    ai.get_component_detail.return_value = {"configurationSchema": {}}
    return FlowService(
        config_store=store,
        client_factory=lambda _url, _token: client,
        ai_client_factory=lambda _url, _token: ai,
    )


def _invoke(store: ConfigStore, service: FlowService, args: list[str], stdin: str | None = None):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.FlowService") as mock_service,
    ):
        mock_store.return_value = store
        mock_service.return_value = service
        return runner.invoke(app, args, input=stdin)


def _error(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)["error"]


NEW = ["--json", "flow", "new", "--project", "prod", "--name", "F"]
UPDATE = ["--json", "flow", "update", "--project", "prod", "--flow-id", "900"]
VALIDATE = ["--json", "flow", "validate"]


# ---------------------------------------------------------------------------
# An empty document is a load error (VALIDATION_ERROR, exit 2), nothing written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [NEW, UPDATE, [*UPDATE, "--name", "Renamed"], VALIDATE])
@pytest.mark.parametrize("document", EMPTY_DOCUMENTS)
def test_empty_definition_file_is_refused(
    tmp_path: Path, command: list[str], document: str
) -> None:
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})
    path = tmp_path / "flow.yaml"
    path.write_text(document, encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), client),
        [*command, "--definition-file", str(path)],
    )

    assert result.exit_code == 2, result.output
    error = _error(result)
    assert error["code"] == "VALIDATION_ERROR"
    assert "empty" in error["message"]
    client.create_config.assert_not_called()
    client.update_config.assert_not_called()


@pytest.mark.parametrize("command", [NEW, UPDATE, VALIDATE])
@pytest.mark.parametrize("inline", ["", "  "])
def test_empty_inline_definition_is_refused(
    tmp_path: Path, command: list[str], inline: str
) -> None:
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})

    result = _invoke(
        _store(tmp_path), _service(_store(tmp_path), client), [*command, "--definition", inline]
    )

    assert result.exit_code == 2, result.output
    assert _error(result)["code"] == "VALIDATION_ERROR"
    client.create_config.assert_not_called()
    client.update_config.assert_not_called()


def test_tab_only_file_is_refused(tmp_path: Path) -> None:
    """YAML refuses a tab; the result is the same load error, nothing written."""
    client = _keboola_client()
    path = tmp_path / "flow.yaml"
    path.write_text("\t\n", encoding="utf-8")

    result = _invoke(
        _store(tmp_path), _service(_store(tmp_path), client), [*NEW, "--definition-file", str(path)]
    )

    assert result.exit_code == 2, result.output
    assert _error(result)["code"] == "VALIDATION_ERROR"
    client.create_config.assert_not_called()


def test_empty_stdin_definition_is_refused(tmp_path: Path) -> None:
    client = _keboola_client()

    result = _invoke(
        _store(tmp_path), _service(_store(tmp_path), client), [*NEW, "--definition-file", "-"], ""
    )

    assert result.exit_code == 2, result.output
    assert _error(result)["code"] == "VALIDATION_ERROR"
    client.create_config.assert_not_called()


@pytest.mark.parametrize("document", ["{}\n", "phase:\n  - id: p1\n", "name: Daily ETL\n"])
@pytest.mark.parametrize("command", [NEW, [*UPDATE, "--name", "Renamed"], VALIDATE])
def test_mapping_without_phases_or_tasks_is_refused(
    tmp_path: Path, command: list[str], document: str
) -> None:
    """A typo like ``phase:`` must not be dropped silently (update would only rename)."""
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})
    path = tmp_path / "flow.yaml"
    path.write_text(document, encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), client),
        [*command, "--definition-file", str(path)],
    )

    assert result.exit_code == 2, result.output
    error = _error(result)
    assert error["code"] == "VALIDATION_ERROR"
    assert "phases" in error["message"]
    client.create_config.assert_not_called()
    client.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# A definition with no phases is INVALID_FLOW_DEFINITION, nothing written
# ---------------------------------------------------------------------------


def test_validation_rule_no_phases() -> None:
    assert any("no phases" in e for e in validate_conditional_flow([], []))
    assert any("no phases" in e for e in validate_conditional_flow([], TASKS))
    assert not validate_conditional_flow(PHASES, TASKS)


@pytest.mark.parametrize("document", ["phases: []\ntasks: []\n", "tasks: []\n", "phases: []\n"])
def test_new_without_phases_is_refused(tmp_path: Path, document: str) -> None:
    client = _keboola_client()
    path = tmp_path / "flow.yaml"
    path.write_text(document, encoding="utf-8")

    result = _invoke(
        _store(tmp_path), _service(_store(tmp_path), client), [*NEW, "--definition-file", str(path)]
    )

    assert result.exit_code == 1, result.output
    assert _error(result)["code"] == "INVALID_FLOW_DEFINITION"
    client.create_config.assert_not_called()


def test_update_that_empties_the_flow_is_refused(tmp_path: Path) -> None:
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})
    path = tmp_path / "flow.yaml"
    path.write_text("phases: []\n", encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), client),
        [*UPDATE, "--definition-file", str(path)],
    )

    assert result.exit_code == 1, result.output
    assert _error(result)["code"] == "INVALID_FLOW_DEFINITION"
    client.update_config.assert_not_called()


def test_validate_without_phases_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "flow.yaml"
    path.write_text("phases: []\ntasks: []\n", encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), _keboola_client()),
        [*VALIDATE, "--definition-file", str(path)],
    )

    assert result.exit_code == 2, result.output
    data = json.loads(result.stdout)["data"]
    assert data["valid"] is False
    assert any("no phases" in e for e in data["errors"])


# ---------------------------------------------------------------------------
# Omitted definition: the empty scaffold flow still works, renames still work
# ---------------------------------------------------------------------------


def test_new_without_definition_still_creates_an_empty_flow(tmp_path: Path) -> None:
    client = _keboola_client()

    result = _invoke(_store(tmp_path), _service(_store(tmp_path), client), NEW)

    assert result.exit_code == 0, result.output
    configuration = client.create_config.call_args.kwargs["configuration"]
    assert configuration == {"phases": [], "tasks": []}


def test_rename_of_an_empty_flow_still_works(tmp_path: Path) -> None:
    client = _keboola_client({"phases": [], "tasks": []})

    result = _invoke(
        _store(tmp_path), _service(_store(tmp_path), client), [*UPDATE, "--name", "Renamed"]
    )

    assert result.exit_code == 0, result.output
    client.update_config.assert_called_once()


def test_valid_definition_still_creates(tmp_path: Path) -> None:
    client = _keboola_client()
    path = tmp_path / "flow.json"
    path.write_text(json.dumps({"phases": PHASES, "tasks": TASKS}), encoding="utf-8")

    result = _invoke(
        _store(tmp_path), _service(_store(tmp_path), client), [*NEW, "--definition-file", str(path)]
    )

    assert result.exit_code == 0, result.output
    assert client.create_config.call_args.kwargs["configuration"]["phases"] == PHASES


# ---------------------------------------------------------------------------
# Service: explicit empty differs from omitted
# ---------------------------------------------------------------------------


def test_service_create_scaffold_when_both_omitted(tmp_path: Path) -> None:
    client = _keboola_client()
    _service(_store(tmp_path), client).create_flow(alias="prod", name="F")
    assert client.create_config.call_args.kwargs["configuration"] == {"phases": [], "tasks": []}


@pytest.mark.parametrize(("phases", "tasks"), [([], []), ([], None), (None, []), (None, TASKS)])
def test_service_create_refuses_explicit_empty(
    tmp_path: Path, phases: list | None, tasks: list | None
) -> None:
    client = _keboola_client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(_store(tmp_path), client).create_flow(
            alias="prod", name="F", phases=phases, tasks=tasks
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    client.create_config.assert_not_called()


def test_service_update_tasks_only_on_empty_flow_is_refused(tmp_path: Path) -> None:
    """The merged body (current phases + new tasks) is what gets validated."""
    client = _keboola_client({"phases": [], "tasks": []})
    with pytest.raises(KeboolaApiError) as exc:
        _service(_store(tmp_path), client).update_flow(alias="prod", config_id="900", tasks=TASKS)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    client.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

_HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
AUTH = {"Authorization": "Bearer test-token"}


def _rest_app(tmp_path: Path, client: MagicMock) -> Any:
    from keboola_agent_cli.models import AppConfig
    from keboola_agent_cli.server import create_app

    project = ProjectConfig(stack_url="https://connection.keboola.com", token=TOKEN, project_id=1)
    ConfigStore(config_dir=tmp_path).save(
        AppConfig(projects={"prod": project}, default_project="prod")
    )
    rest = create_app(config_dir=str(tmp_path), auth_token="test-token")
    client.get_component_detail.return_value = {"configurationSchema": {}}
    for service in vars(rest.state.registry).values():
        for name in list(getattr(service, "__dict__", {})):
            if name.endswith("client_factory"):
                setattr(service, name, lambda *_args, **_kwargs: client)
    return rest


def _request(rest: Any, method: str, url: str, body: dict[str, Any]) -> Any:
    from fastapi.testclient import TestClient

    with TestClient(rest, raise_server_exceptions=False) as http:
        return http.request(method, url, json=body, headers=AUTH)


@pytest.mark.skipif(not _HAS_FASTAPI, reason="FastAPI not installed")
@pytest.mark.parametrize("body", [{"phases": []}, {"phases": [], "tasks": []}, {"tasks": TASKS}])
def test_rest_create_refuses_explicit_empty(tmp_path: Path, body: dict[str, Any]) -> None:
    client = _keboola_client()
    res = _request(_rest_app(tmp_path, client), "POST", "/flows/prod", {"name": "F", **body})
    # The HTTP status of INVALID_FLOW_DEFINITION comes from the error table
    # (class "api": 502), like every other invalid flow definition over REST.
    assert res.status_code == 502, res.text
    assert res.json()["error"]["code"] == "INVALID_FLOW_DEFINITION", res.text
    client.create_config.assert_not_called()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="FastAPI not installed")
def test_rest_create_without_definition_still_creates(tmp_path: Path) -> None:
    client = _keboola_client()
    res = _request(_rest_app(tmp_path, client), "POST", "/flows/prod", {"name": "F"})
    assert res.status_code == 200, res.text
    client.create_config.assert_called_once()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="FastAPI not installed")
def test_rest_update_that_empties_the_flow_is_refused(tmp_path: Path) -> None:
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})
    res = _request(_rest_app(tmp_path, client), "PATCH", "/flows/prod/900", {"phases": []})
    assert res.status_code == 502, res.text
    assert res.json()["error"]["code"] == "INVALID_FLOW_DEFINITION", res.text
    client.update_config.assert_not_called()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="FastAPI not installed")
def test_rest_validate_without_phases_is_invalid(tmp_path: Path) -> None:
    res = _request(_rest_app(tmp_path, _keboola_client()), "POST", "/flows/validate", {})
    assert res.status_code == 200, res.text
    assert res.json()["valid"] is False


# ---------------------------------------------------------------------------
# Input that used to be dropped silently
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        "phases: null\n",
        "tasks: null\n",
        "phases: null\ntasks: null\n",
        # a valid phases list plus a misspelled tasks key: the tasks were dropped
        "phases:\n  - id: p1\n    name: P1\ntask:\n  - id: t1\n",
    ],
)
@pytest.mark.parametrize("command", [NEW, [*UPDATE, "--name", "Renamed"], VALIDATE])
def test_null_or_unknown_key_is_refused(tmp_path: Path, command: list[str], document: str) -> None:
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})
    path = tmp_path / "flow.yaml"
    path.write_text(document, encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), client),
        [*command, "--definition-file", str(path)],
    )

    assert result.exit_code == 2, result.output
    assert _error(result)["code"] == "VALIDATION_ERROR"
    client.create_config.assert_not_called()
    client.update_config.assert_not_called()


def test_empty_inline_with_a_file_is_the_mutual_exclusion_error(tmp_path: Path) -> None:
    """`--definition ""` used to be ignored, so the file was used without a word."""
    client = _keboola_client()
    path = tmp_path / "flow.json"
    path.write_text(json.dumps({"phases": PHASES, "tasks": TASKS}), encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), client),
        [*NEW, "--definition", "", "--definition-file", str(path)],
    )

    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in _error(result)["message"]
    client.create_config.assert_not_called()


@pytest.mark.parametrize(
    "document",
    [
        "phases: false\ntasks: []\n",
        "phases: []\ntasks: {}\n",
        "phases: [null]\ntasks: []\n",
        "phases:\n  - id: p1\n    next: oops\ntasks: []\n",
        # a nested condition whose operands are not a list (crashed before)
        (
            "phases:\n  - id: p\n    next:\n      - goto: null\n        condition:\n"
            "          type: operator\n          operator: EQUALS\n          operands: null\n"
            "      - goto: null\ntasks:\n  - id: t\n    phase: p\n"
        ),
    ],
)
def test_malformed_lists_are_invalid_not_a_crash(tmp_path: Path, document: str) -> None:
    """No project = no schema: the shape is still checked before the semantic rules."""
    path = tmp_path / "flow.yaml"
    path.write_text(document, encoding="utf-8")

    result = _invoke(
        _store(tmp_path),
        _service(_store(tmp_path), _keboola_client()),
        [*VALIDATE, "--definition-file", str(path)],
    )

    assert result.exit_code == 2, result.output
    data = json.loads(result.stdout)["data"]
    assert data["valid"] is False
    assert data["errors"]


@pytest.mark.parametrize(
    "condition",
    [
        {"type": "operator", "operator": [], "operands": []},
        {"type": "function", "function": {}, "operands": []},
        {"type": "operator", "operator": "AND", "operands": [{"type": "operator", "operator": []}]},
    ],
)
def test_condition_with_a_non_string_operator_is_invalid_not_a_crash(
    condition: dict[str, Any],
) -> None:
    phases = [{"id": "p", "next": [{"goto": None, "condition": condition}, {"goto": None}]}]
    errors = validate_conditional_flow(phases, [{"id": "t", "phase": "p"}])
    assert any("must be a string" in e for e in errors)


def test_service_create_refuses_malformed_lists(tmp_path: Path) -> None:
    client = _keboola_client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(_store(tmp_path), client).create_flow(
            alias="prod",
            name="F",
            phases=[None],
            tasks=[],  # type: ignore[list-item]
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    client.create_config.assert_not_called()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="FastAPI not installed")
@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/flows/prod", {"name": "F", "phases": None}),
        ("POST", "/flows/prod", {"name": "F", "phases": None, "tasks": None}),
        ("POST", "/flows/prod", {"name": "F", "phase": []}),
        ("PATCH", "/flows/prod/900", {"name": "Renamed", "tasks": None}),
        ("PATCH", "/flows/prod/900", {"name": "Renamed", "task": TASKS}),
        ("POST", "/flows/validate", {"phase": PHASES}),
    ],
)
def test_rest_null_or_unknown_field_is_refused(
    tmp_path: Path, method: str, url: str, body: dict[str, Any]
) -> None:
    client = _keboola_client({"phases": PHASES, "tasks": TASKS})
    res = _request(_rest_app(tmp_path, client), method, url, body)
    assert res.status_code == 422, res.text
    client.create_config.assert_not_called()
    client.update_config.assert_not_called()
