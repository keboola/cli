"""Tests for config state read/write (issue #593).

This file is shared across layers per the issue #593 implementation split:
- Client layer tests live in classes prefixed ``TestConfigStateClient*``
  (this agent's scope: ``client/configs.py::update_config_state`` and
  ``update_config_row_state``).
- Service and CLI layer tests are added by a follow-up agent in separate
  classes in this same file.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.constants import CONFIG_STATE_MAX_BYTES
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService

FAKE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


class TestConfigStateClientUpdateConfigState:
    """Tests for KeboolaClient.update_config_state()."""

    def test_update_config_state_production_url(self, httpx_mock) -> None:
        """No branch_id -> non-branch-scoped production URL."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42/state",
            json={
                "id": "42",
                "name": "cfg",
                "configuration": {},
                "state": {"lastId": 123},
                "version": 5,
            },
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_state("keboola.ex-db-snowflake", "42", {"lastId": 123})

        assert result["state"] == {"lastId": 123}
        assert result["version"] == 5

    def test_update_config_state_uses_put_method(self, httpx_mock) -> None:
        """The request is a PUT, not POST/PATCH."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42/state",
            json={"id": "42", "state": {}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            client.update_config_state("keboola.ex-db-snowflake", "42", {})

        request = httpx_mock.get_requests()[0]
        assert request.method == "PUT"

    def test_update_config_state_branch_scoped_url(self, httpx_mock) -> None:
        """branch_id set -> branch-scoped URL prefix."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/components/keboola.ex-db-snowflake/configs/42/state",
            json={"id": "42", "state": {"cursor": "abc"}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_state(
                "keboola.ex-db-snowflake", "42", {"cursor": "abc"}, branch_id=123
            )

        assert result["state"] == {"cursor": "abc"}

    def test_update_config_state_body_is_json_not_form_encoded(self, httpx_mock) -> None:
        """CRITICAL regression guard: body must be genuine JSON {"state": ...},
        NEVER the form-encoded data={"state": json.dumps(state)} shape that
        update_config() uses for the `configuration` field. Sending the wrong
        shape here silently breaks the write against the real API.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42/state",
            json={"id": "42", "state": {"lastId": 123, "nested": {"a": 1}}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            client.update_config_state(
                "keboola.ex-db-snowflake", "42", {"lastId": 123, "nested": {"a": 1}}
            )

        request = httpx_mock.get_requests()[0]
        # A form-encoded body would not be valid JSON at all (or would parse
        # to a flat string-keyed dict without nested structures preserved).
        parsed = json.loads(request.content)
        assert parsed == {"state": {"lastId": 123, "nested": {"a": 1}}}
        # The Content-Type must be application/json, not
        # application/x-www-form-urlencoded.
        assert "application/json" in request.headers.get("content-type", "")

    def test_update_config_state_component_and_config_id_escaped(self, httpx_mock) -> None:
        """Special characters in component_id/config_id are URL-escaped."""
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/components/"
                "keboola.ex-http%2Fspecial/configs/cfg%20id/state"
            ),
            json={"id": "cfg id", "state": {}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_state("keboola.ex-http/special", "cfg id", {})

        assert result == {"id": "cfg id", "state": {}}

    def test_update_config_state_returns_full_detail_not_bare_state(self, httpx_mock) -> None:
        """Response is the full config detail object, not just the state dict."""
        full_detail = {
            "id": "42",
            "name": "cfg",
            "version": 7,
            "changeDescription": "state updated",
            "configuration": {"parameters": {"x": 1}},
            "rows": [],
            "state": {"lastId": 999},
            "currentVersion": {"created": "2026-08-17T00:00:00+0000"},
        }
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42/state",
            json=full_detail,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_state("keboola.ex-db-snowflake", "42", {"lastId": 999})

        assert result == full_detail


class TestConfigStateClientUpdateConfigRowState:
    """Tests for KeboolaClient.update_config_row_state()."""

    def test_update_config_row_state_production_url(self, httpx_mock) -> None:
        """No branch_id -> non-branch-scoped production URL for the row endpoint."""
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/components/"
                "keboola.ex-db-snowflake/configs/42/rows/row-1/state"
            ),
            json={"id": "row-1", "state": {"lastId": 5}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_row_state(
                "keboola.ex-db-snowflake", "42", "row-1", {"lastId": 5}
            )

        assert result["state"] == {"lastId": 5}

    def test_update_config_row_state_uses_put_method(self, httpx_mock) -> None:
        """The request is a PUT."""
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/components/"
                "keboola.ex-db-snowflake/configs/42/rows/row-1/state"
            ),
            json={"id": "row-1", "state": {}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            client.update_config_row_state("keboola.ex-db-snowflake", "42", "row-1", {})

        request = httpx_mock.get_requests()[0]
        assert request.method == "PUT"

    def test_update_config_row_state_branch_scoped_url(self, httpx_mock) -> None:
        """branch_id set -> branch-scoped URL prefix for the row endpoint."""
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/branch/123/components/"
                "keboola.ex-db-snowflake/configs/42/rows/row-1/state"
            ),
            json={"id": "row-1", "state": {"cursor": "xyz"}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_row_state(
                "keboola.ex-db-snowflake", "42", "row-1", {"cursor": "xyz"}, branch_id=123
            )

        assert result["state"] == {"cursor": "xyz"}

    def test_update_config_row_state_body_is_json_not_form_encoded(self, httpx_mock) -> None:
        """Same CRITICAL regression guard as the config-level endpoint: body
        must be {"state": ...} as real JSON, not form-encoded json.dumps.
        """
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/components/"
                "keboola.ex-db-snowflake/configs/42/rows/row-1/state"
            ),
            json={"id": "row-1", "state": {"nested": {"list": [1, 2, 3]}}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            client.update_config_row_state(
                "keboola.ex-db-snowflake",
                "42",
                "row-1",
                {"nested": {"list": [1, 2, 3]}},
            )

        request = httpx_mock.get_requests()[0]
        parsed = json.loads(request.content)
        assert parsed == {"state": {"nested": {"list": [1, 2, 3]}}}
        assert "application/json" in request.headers.get("content-type", "")

    def test_update_config_row_state_ids_escaped(self, httpx_mock) -> None:
        """Special characters in component_id/config_id/row_id are URL-escaped."""
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/components/"
                "keboola.ex-http%2Fspecial/configs/cfg%20id/rows/row%2Fid/state"
            ),
            json={"id": "row/id", "state": {}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_row_state(
                "keboola.ex-http/special", "cfg id", "row/id", {}
            )

        assert result == {"id": "row/id", "state": {}}

    def test_update_config_row_state_returns_full_detail(self, httpx_mock) -> None:
        """Response is the full config detail object, matching the config-level variant."""
        full_detail = {
            "id": "42",
            "name": "cfg",
            "version": 3,
            "configuration": {},
            "rows": [{"id": "row-1", "state": {"lastId": 1}}],
            "state": {},
            "currentVersion": {"created": "2026-08-17T00:00:00+0000"},
        }
        httpx_mock.add_response(
            url=(
                "https://connection.keboola.com/v2/storage/components/"
                "keboola.ex-db-snowflake/configs/42/rows/row-1/state"
            ),
            json=full_detail,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
        ) as client:
            result = client.update_config_row_state(
                "keboola.ex-db-snowflake", "42", "row-1", {"lastId": 1}
            )

        assert result == full_detail


# ---------------------------------------------------------------------------
# Service layer tests (issue #593 Part B)
#
# Mocking follows the rest of the config_service test suite: a MagicMock
# client injected via client_factory, no pytest-httpx. See
# tests/test_config_set_default_bucket.py / tests/test_variables_cli.py for
# the reference shape this mirrors.
# ---------------------------------------------------------------------------


runner = CliRunner()


def _state_detail(state: dict | None = None, rows: list[dict] | None = None) -> dict:
    """Build a sample config detail response carrying root + row state."""
    return {
        "id": "cfg-001",
        "name": "My Config",
        "version": 3,
        "configuration": {"parameters": {"a": 1}},
        "state": state if state is not None else {},
        "rows": rows if rows is not None else [],
    }


def _make_state_service(
    tmp_config_dir: Path, detail: dict | None = None
) -> tuple[ConfigService, MagicMock]:
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_detail.return_value = detail if detail is not None else _state_detail()
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


class TestConfigServiceGetConfigState:
    """Tests for ConfigService.get_config_state."""

    def test_get_root_state(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir, _state_detail(state={"lastId": 42}))

        result = service.get_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
        )

        assert result == {
            "project_alias": "prod",
            "component_id": "keboola.ex-db-snowflake",
            "config_id": "cfg-001",
            "row_id": None,
            "branch_id": None,
            "state": {"lastId": 42},
        }
        client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", branch_id=None
        )
        client.close.assert_called_once()

    def test_get_row_state(self, tmp_config_dir: Path) -> None:
        rows = [
            {"id": "row-1", "state": {"cursor": "abc"}},
            {"id": "row-2", "state": {}},
        ]
        service, _client = _make_state_service(tmp_config_dir, _state_detail(rows=rows))

        result = service.get_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-1",
        )

        assert result["state"] == {"cursor": "abc"}
        assert result["row_id"] == "row-1"

    def test_get_missing_row_raises_named_not_found(self, tmp_config_dir: Path) -> None:
        """A typo'd row id must fail loudly and name the row -- never a
        silent empty dict (this is the exact class of bug issue #593 fixes
        for --set)."""
        rows = [{"id": "row-1", "state": {}}]
        service, _client = _make_state_service(tmp_config_dir, _state_detail(rows=rows))

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_config_state(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                row_id="row-typo",
            )

        assert exc_info.value.error_code == ErrorCode.NOT_FOUND
        assert "row-typo" in exc_info.value.message

    def test_root_state_missing_key_defaults_to_empty_dict(self, tmp_config_dir: Path) -> None:
        detail = _state_detail()
        del detail["state"]
        service, _client = _make_state_service(tmp_config_dir, detail)

        result = service.get_config_state(
            alias="prod", component_id="keboola.ex-db-snowflake", config_id="cfg-001"
        )

        assert result["state"] == {}

    def test_branch_id_propagated_to_client(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir)

        service.get_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            branch_id=999,
        )

        client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", branch_id=999
        )


class TestConfigServiceSetConfigState:
    """Tests for ConfigService.set_config_state."""

    def test_reject_non_object_list(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.set_config_state(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                state=[1, 2, 3],  # ty: ignore[invalid-argument-type]  # deliberately wrong type: exercises the validation
            )

        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        client.get_config_detail.assert_not_called()

    def test_reject_non_object_scalar(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.set_config_state(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                state="not-an-object",  # ty: ignore[invalid-argument-type]  # deliberately wrong type: exercises the validation
            )

        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        client.get_config_detail.assert_not_called()

    def test_reject_oversized_state(self, tmp_config_dir: Path) -> None:
        """Serialized body >= CONFIG_STATE_MAX_BYTES is rejected before any
        network call -- the 4 MB cap is enforced locally, not just by the API."""
        service, client = _make_state_service(tmp_config_dir)
        huge_state = {"blob": "x" * (CONFIG_STATE_MAX_BYTES + 10)}

        with pytest.raises(KeboolaApiError) as exc_info:
            service.set_config_state(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                state=huge_state,
            )

        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
        client.get_config_detail.assert_not_called()

    def test_dry_run_returns_diff_without_writing(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir, _state_detail(state={"lastId": 1}))

        result = service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"lastId": 2},
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["old_state"] == {"lastId": 1}
        assert result["new_state"] == {"lastId": 2}
        assert any("lastId" in c for c in result["changes"])
        assert result["project_alias"] == "prod"
        assert result["component_id"] == "keboola.ex-db-snowflake"
        assert result["config_id"] == "cfg-001"
        assert result["row_id"] is None
        client.update_config_state.assert_not_called()
        client.update_config_row_state.assert_not_called()

    def test_no_op_short_circuit_skips_write(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir, _state_detail(state={"lastId": 1}))

        result = service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"lastId": 1},
        )

        assert result["changed"] is False
        assert result["state"] == {"lastId": 1}
        client.update_config_state.assert_not_called()
        client.update_config_row_state.assert_not_called()

    def test_real_write_root_state(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir, _state_detail(state={"lastId": 1}))
        client.update_config_state.return_value = _state_detail(state={"lastId": 2})

        result = service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"lastId": 2},
        )

        assert result["changed"] is True
        assert result["state"] == {"lastId": 2}
        assert result["row_id"] is None
        client.update_config_state.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", {"lastId": 2}, branch_id=None
        )
        client.close.assert_called_once()

    def test_real_write_row_state(self, tmp_config_dir: Path) -> None:
        rows = [{"id": "row-1", "state": {"cursor": "old"}}]
        service, client = _make_state_service(tmp_config_dir, _state_detail(rows=rows))
        # Real shape of PUT .../rows/{row}/state: the bare row, not a detail.
        client.update_config_row_state.return_value = {
            "id": "row-1",
            "state": {"cursor": "new"},
            "version": 2,
        }

        result = service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"cursor": "new"},
            row_id="row-1",
        )

        assert result["changed"] is True
        assert result["state"] == {"cursor": "new"}
        assert result["row_id"] == "row-1"
        client.update_config_row_state.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", "row-1", {"cursor": "new"}, branch_id=None
        )
        client.update_config_state.assert_not_called()

    def test_set_missing_row_raises_named_not_found(self, tmp_config_dir: Path) -> None:
        rows = [{"id": "row-1", "state": {}}]
        service, client = _make_state_service(tmp_config_dir, _state_detail(rows=rows))

        with pytest.raises(KeboolaApiError) as exc_info:
            service.set_config_state(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                state={"x": 1},
                row_id="row-typo",
            )

        assert exc_info.value.error_code == ErrorCode.NOT_FOUND
        assert "row-typo" in exc_info.value.message
        client.update_config_state.assert_not_called()
        client.update_config_row_state.assert_not_called()

    def test_branch_id_propagated_to_client_write(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir, _state_detail(state={"lastId": 1}))
        client.update_config_state.return_value = _state_detail(state={"lastId": 2})

        service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"lastId": 2},
            branch_id=456,
        )

        client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", branch_id=456
        )
        client.update_config_state.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", {"lastId": 2}, branch_id=456
        )


# ---------------------------------------------------------------------------
# CLI layer tests (issue #593 Part B)
# ---------------------------------------------------------------------------


class TestConfigStateCli:
    """CLI-level tests for `config state-get` / `config state-set`."""

    @staticmethod
    def _invoke(
        tmp_config_dir: Path,
        command: str,
        args: list[str],
        json_mode: bool = True,
        input_text: str | None = None,
    ) -> Result:
        base = ["--config-dir", str(tmp_config_dir)]
        if json_mode:
            base = ["--json", *base]
        return runner.invoke(app, [*base, "config", command, *args], input=input_text)

    @staticmethod
    def _patch_service(mp: pytest.MonkeyPatch, store, mock_client: MagicMock) -> None:
        # `state-get` / `state-set` live in commands/config_state.py (split out
        # of config.py for the file-size-budget ratchet, see that module's
        # docstring) and import `get_service` into their own module
        # namespace, so the patch target differs from the sibling config
        # commands defined directly in commands/config.py.
        mp.setattr(
            "keboola_agent_cli.commands.config_state.get_service",
            lambda ctx, name: ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            ),
        )

    # -- state-get ----------------------------------------------------------

    def test_state_get_json_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 7})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-get",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["state"] == {"lastId": 7}
        assert payload["data"]["row_id"] is None

    def test_state_get_human_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 7})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-get",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                ],
                json_mode=False,
            )

        assert result.exit_code == 0, result.output
        assert "lastId" in result.output

    def test_state_get_row(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(
            rows=[{"id": "row-1", "state": {"cursor": "abc"}}]
        )

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-get",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-1",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["state"] == {"cursor": "abc"}
        assert payload["data"]["row_id"] == "row-1"

    def test_state_get_missing_row_exit_code(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(
            rows=[{"id": "row-1", "state": {}}]
        )

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-get",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-typo",
                ],
            )

        assert result.exit_code == 1, result.output
        assert "row-typo" in result.output

    def test_state_get_branch_propagation(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-get",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--branch",
                    "321",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", branch_id=321
        )

    # -- state-set ------------------------------------------------------

    def test_state_set_rejects_non_object(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail()

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    "[1, 2, 3]",
                    "--yes",
                ],
            )

        assert result.exit_code != 0, result.output
        assert "VALIDATION_ERROR" in result.output
        mock_client.update_config_state.assert_not_called()

    def test_state_set_json_mode_skips_prompt_no_yes_needed(self, tmp_config_dir: Path) -> None:
        """`--json` skips the confirmation prompt WITHOUT requiring --yes
        (repo convention, see `config row-delete`)."""
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})
        mock_client.update_config_state.return_value = _state_detail(state={"lastId": 2})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["changed"] is True
        mock_client.update_config_state.assert_called_once()

    def test_state_set_human_mode_without_yes_prompts_and_aborts_on_no(
        self, tmp_config_dir: Path
    ) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                ],
                json_mode=False,
                input_text="n\n",
            )

        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        mock_client.update_config_state.assert_not_called()

    def test_state_set_human_mode_without_yes_prompts_and_writes_on_yes(
        self, tmp_config_dir: Path
    ) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})
        mock_client.update_config_state.return_value = _state_detail(state={"lastId": 2})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                ],
                json_mode=False,
                input_text="y\n",
            )

        assert result.exit_code == 0, result.output
        mock_client.update_config_state.assert_called_once()

    def test_state_set_yes_flag_skips_prompt(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})
        mock_client.update_config_state.return_value = _state_detail(state={"lastId": 2})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                    "--yes",
                ],
                json_mode=False,
                input_text="",
            )

        assert result.exit_code == 0, result.output
        mock_client.update_config_state.assert_called_once()

    def test_state_set_dry_run_never_prompts_even_without_yes(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                    "--dry-run",
                ],
                json_mode=False,
                input_text="",
            )

        assert result.exit_code == 0, result.output
        assert "Dry-run" in result.output
        mock_client.update_config_state.assert_not_called()

    def test_state_set_dry_run_json_shape(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["dry_run"] is True
        assert payload["data"]["old_state"] == {"lastId": 1}
        assert payload["data"]["new_state"] == {"lastId": 2}
        mock_client.update_config_state.assert_not_called()

    def test_state_set_row(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(
            rows=[{"id": "row-1", "state": {"cursor": "old"}}]
        )
        # Real shape of PUT .../rows/{row}/state: the bare row, not a detail.
        mock_client.update_config_row_state.return_value = {
            "id": "row-1",
            "state": {"cursor": "new"},
            "version": 2,
        }

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-1",
                    "--state",
                    '{"cursor": "new"}',
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["row_id"] == "row-1"
        assert payload["data"]["state"] == {"cursor": "new"}
        mock_client.update_config_row_state.assert_called_once()
        mock_client.update_config_state.assert_not_called()

    def test_state_set_branch_propagation(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})
        mock_client.update_config_state.return_value = _state_detail(state={"lastId": 2})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 2}',
                    "--branch",
                    "654",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", branch_id=654
        )
        mock_client.update_config_state.assert_called_once_with(
            "keboola.ex-db-snowflake", "cfg-001", {"lastId": 2}, branch_id=654
        )

    def test_state_set_no_op_json_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _state_detail(state={"lastId": 1})

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                "state-set",
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--state",
                    '{"lastId": 1}',
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["changed"] is False
        mock_client.update_config_state.assert_not_called()


class TestConfigStateRowWriteResponseShape:
    """Regression: the row PUT answers with a different shape than the root PUT.

    Caught by the live E2E run against project 5946 (issue #593). The root
    endpoint returns the full configuration detail -- which carries a ``rows``
    array -- while the row endpoint returns the *bare row object* with no
    ``rows`` key at all. The service used to look the row back up inside
    ``result["rows"]`` regardless, so every row write raised NOT_FOUND even
    though the PUT had returned 200 and the state had landed on the server.

    These tests deliberately mock the SHAPES THE REAL API RETURNS. A mock that
    echoes a full config detail for the row endpoint would pass while the real
    call fails -- which is exactly how the bug survived the first test pass.
    """

    def test_row_write_returns_state_from_bare_row_response(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(
            tmp_config_dir, _state_detail(rows=[{"id": "row-1", "state": {}}])
        )
        # Real shape of PUT .../rows/{row}/state: the bare row, no "rows" key.
        client.update_config_row_state.return_value = {
            "id": "row-1",
            "configuration": {"foo": "bar"},
            "state": {"rowCursor": "abc"},
            "version": 3,
        }

        result = service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"rowCursor": "abc"},
            row_id="row-1",
        )

        assert result["changed"] is True
        assert result["state"] == {"rowCursor": "abc"}
        assert result["row_id"] == "row-1"
        client.update_config_row_state.assert_called_once()

    def test_root_write_still_reads_state_from_full_detail(self, tmp_config_dir: Path) -> None:
        service, client = _make_state_service(tmp_config_dir)
        # Real shape of PUT .../state: the full configuration detail.
        client.update_config_state.return_value = _state_detail(state={"lastImportId": "999"})

        result = service.set_config_state(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            state={"lastImportId": "999"},
        )

        assert result["changed"] is True
        assert result["state"] == {"lastImportId": "999"}
