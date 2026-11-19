"""Generic configuration writes of a keboola.flow get the `flow new` / `flow update` checks.

`config new --push`, `config update` and `config clone` wrote any body, so an
agent could store a flow the job queue never runs (no phases, two first
phases, a task in an undefined phase ...). They now refuse it with
INVALID_FLOW_DEFINITION before anything is written, dry run included. An empty
body (neither `phases` nor `tasks`) stays allowed: `flow new` without a
definition creates the same empty flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


def _task(tid: str, phase: str) -> dict[str, Any]:
    return {"id": tid, "name": tid, "phase": phase, "enabled": True, "task": {"type": "job"}}


VALID = {
    "phases": [
        {"id": "p1", "name": "P1", "next": [{"id": "a", "goto": "p2"}]},
        {"id": "p2", "name": "P2", "next": [{"id": "end", "goto": None}]},
    ],
    "tasks": [_task("t1", "p1"), _task("t2", "p2")],
}
BROKEN = [
    pytest.param({"phases": [], "tasks": []}, "no phases", id="no-phases"),
    pytest.param(
        {
            "phases": [*VALID["phases"], {"id": "island", "name": "I"}],
            "tasks": [*VALID["tasks"], _task("ti", "island")],
        },
        "exactly one first phase",
        id="two-first-phases",
    ),
    pytest.param(
        {"phases": VALID["phases"], "tasks": [*VALID["tasks"], _task("tx", "ghost")]},
        "ghost",
        id="task-in-undefined-phase",
    ),
]


def _client(configuration: dict[str, Any] | None = None) -> MagicMock:
    client = MagicMock()
    detail = {"id": "01flow", "name": "F", "version": 3, "configuration": configuration or VALID}
    client.get_config_detail.return_value = detail
    client.update_config.return_value = {"id": "01flow", "name": "F"}
    client.create_config.return_value = {"id": "02flow", "name": "F copy"}
    client.create_config_copy.return_value = {"id": "02flow", "name": "F copy"}
    client.list_config_rows.return_value = []
    return client


def _service(tmp_path: Path, client: MagicMock) -> ConfigService:
    ai = MagicMock()
    ai.get_component_detail.return_value = {"configurationSchema": {}}
    return ConfigService(
        config_store=setup_single_project(tmp_path / "cfg"),
        client_factory=lambda url, token: client,
        ai_client_factory=lambda url, token: ai,
    )


def _written(client: MagicMock) -> bool:
    return bool(
        client.update_config.called
        or client.create_config.called
        or client.create_config_copy.called
    )


# ---------------------------------------------------------------------------
# config update
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("body", "needle"), BROKEN)
@pytest.mark.parametrize("dry_run", [False, True])
def test_update_with_a_broken_flow_writes_nothing(
    tmp_path: Path, body: dict[str, Any], needle: str, dry_run: bool
) -> None:
    client = _client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(tmp_path, client).update_config(
            alias="prod",
            component_id="keboola.flow",
            config_id="01flow",
            configuration=body,
            dry_run=dry_run,
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    assert needle in exc.value.message
    assert not _written(client)


def test_update_with_set_that_breaks_the_flow_writes_nothing(tmp_path: Path) -> None:
    client = _client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(tmp_path, client).update_config(
            alias="prod",
            component_id="keboola.flow",
            config_id="01flow",
            set_paths=[("phases", [])],
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    assert not _written(client)


def test_update_with_a_valid_flow_writes(tmp_path: Path) -> None:
    client = _client()
    _service(tmp_path, client).update_config(
        alias="prod", component_id="keboola.flow", config_id="01flow", configuration=VALID
    )
    client.update_config.assert_called_once()


def test_rename_of_a_broken_flow_still_works(tmp_path: Path) -> None:
    """Only a write of the body is checked: a name-only update never was."""
    client = _client({"phases": [], "tasks": []})
    _service(tmp_path, client).update_config(
        alias="prod", component_id="keboola.flow", config_id="01flow", name="Renamed"
    )
    client.update_config.assert_called_once()


def test_other_components_are_not_checked(tmp_path: Path) -> None:
    client = _client()
    _service(tmp_path, client).update_config(
        alias="prod",
        component_id="keboola.ex-db",
        config_id="01flow",
        configuration={"phases": [], "parameters": {}},
    )
    client.update_config.assert_called_once()


def test_update_cli_exit_code(tmp_path: Path) -> None:
    client = _client()
    service = _service(tmp_path, client)
    args = ["--json", "config", "update", "--project", "prod", "--component-id", "keboola.flow"]
    args += ["--config-id", "01flow", "--configuration", json.dumps({"phases": [], "tasks": []})]
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ConfigService") as mock_config,
    ):
        mock_store.return_value = service._config_store
        mock_config.return_value = service
        result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == ErrorCode.INVALID_FLOW_DEFINITION
    assert not _written(client)


# ---------------------------------------------------------------------------
# config new --push (create_config)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("body", "needle"), BROKEN)
@pytest.mark.parametrize("dry_run", [False, True])
def test_create_with_a_broken_flow_writes_nothing(
    tmp_path: Path, body: dict[str, Any], needle: str, dry_run: bool
) -> None:
    client = _client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(tmp_path, client).create_config(
            alias="prod",
            component_id="keboola.flow",
            name="F",
            configuration=body,
            dry_run=dry_run,
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    assert needle in exc.value.message
    assert not _written(client)


def test_create_without_a_body_is_the_empty_flow(tmp_path: Path) -> None:
    """No --configuration: the empty flow `flow new` makes without a definition."""
    client = _client()
    _service(tmp_path, client).create_config(alias="prod", component_id="keboola.flow", name="F")
    client.create_config.assert_called_once()


@pytest.mark.parametrize(
    "body", [{}, {"configuration": {"phases": [], "tasks": []}}, {"variables_id": "7"}]
)
def test_create_with_a_body_without_phases_is_refused(tmp_path: Path, body: dict[str, Any]) -> None:
    """An explicit body is checked whole: {} or a wrapped envelope has no phases."""
    client = _client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(tmp_path, client).create_config(
            alias="prod", component_id="keboola.flow", name="F", configuration=body
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    assert not _written(client)


def test_create_with_no_validate_skips_the_check(tmp_path: Path) -> None:
    """--no-validate is the documented escape hatch for every check of `config new`."""
    client = _client()
    _service(tmp_path, client).create_config(
        alias="prod",
        component_id="keboola.flow",
        name="F",
        configuration={"phases": [], "tasks": []},
        validate=False,
    )
    client.create_config.assert_called_once()


# ---------------------------------------------------------------------------
# config clone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_clone_with_set_that_breaks_the_flow_writes_nothing(tmp_path: Path, dry_run: bool) -> None:
    client = _client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(tmp_path, client).clone_config(
            alias="prod",
            component_id="keboola.flow",
            config_id="01flow",
            name="F copy",
            set_overrides={"phases": "[]"},
            dry_run=dry_run,
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    assert not _written(client)


def test_clone_of_a_valid_flow_is_copied(tmp_path: Path) -> None:
    client = _client()
    _service(tmp_path, client).clone_config(
        alias="prod", component_id="keboola.flow", config_id="01flow", name="F copy"
    )
    client.create_config_copy.assert_called_once()


def test_update_merge_with_an_empty_body_keeps_the_flow(tmp_path: Path) -> None:
    """`--merge '{}'` merges nothing; before, it wrote an empty body over the flow."""
    client = _client()
    _service(tmp_path, client).update_config(
        alias="prod",
        component_id="keboola.flow",
        config_id="01flow",
        configuration={},
        merge=True,
    )
    assert client.update_config.call_args.kwargs["configuration"]["phases"] == VALID["phases"]


@pytest.mark.parametrize("body", [{}, {"configuration": {"phases": [], "tasks": []}}])
def test_update_with_a_body_without_phases_is_refused(tmp_path: Path, body: dict[str, Any]) -> None:
    client = _client()
    with pytest.raises(KeboolaApiError) as exc:
        _service(tmp_path, client).update_config(
            alias="prod", component_id="keboola.flow", config_id="01flow", configuration=body
        )
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    assert not _written(client)


def test_clone_without_set_copies_the_source_as_it_is(tmp_path: Path) -> None:
    """No --set: the copy is the source body, checked or not (it is not changed)."""
    client = _client({"phases": [], "tasks": []})
    _service(tmp_path, client).clone_config(
        alias="prod", component_id="keboola.flow", config_id="01flow", name="F copy"
    )
    client.create_config_copy.assert_called_once()
