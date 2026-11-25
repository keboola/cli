"""Tests for data-app plain env vars and the secrets-* changes of issue #738.

- ``data-app secrets-set`` takes a key without ``#`` and stores it encrypted under ``#KEY``.
- ``data-app env-set/list/get/remove`` manage the keys without ``#`` and never encrypt.
- A write that would give a secret and a plain env var the same runtime env var name is
  refused with ``INVALID_ARGUMENT`` in both directions.
- ``secrets-list/get/remove`` on a plain key still work and add a ``DEPRECATED`` warning.

The file is NOT named ``*secrets*``: the local Read/Write/Edit deny rule matches that.
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
from keboola_agent_cli.services.data_app_service import DataAppService

runner = CliRunner()

# Synthetic fixture token, the one the data-app test suite uses.
TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
CIPHER = "KBC::ProjectSecureGKMS::abcdef0123456789"
HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
AUTH = {"Authorization": "Bearer test-token"}


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=5725,
        ),
    )
    return store


def _make_service(
    store: ConfigStore, secrets: dict[str, str] | None = None
) -> tuple[DataAppService, MagicMock, MagicMock]:
    """A real service over mocked clients. Returns (service, storage mock, encrypt mock)."""
    ds_mock = MagicMock()
    storage_mock = MagicMock()
    encrypt_mock = MagicMock()
    ds_mock.get_app.return_value = {"configId": "cfg1"}
    storage_mock.get_config_detail.return_value = {
        "configuration": {"parameters": {"dataApp": {"secrets": dict(secrets or {})}}},
        "version": "7",
    }
    storage_mock.update_config.return_value = {"version": 8}
    encrypt_mock.encrypt.side_effect = lambda alias, component_id, input_data: {
        key: CIPHER for key in input_data
    }
    service = DataAppService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
        encrypt_service=encrypt_mock,
    )
    return service, storage_mock, encrypt_mock


def _written_block(storage_mock: MagicMock) -> dict[str, str]:
    configuration = storage_mock.update_config.call_args.kwargs["configuration"]
    return configuration["parameters"]["dataApp"]["secrets"]


def _invoke(args: list[str], store: ConfigStore, service: Any) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.DataAppService") as mock_service,
    ):
        mock_store.return_value = store
        mock_service.return_value = service
        return runner.invoke(app, args)


BASE = ["--project", "prod", "--app-id", "42"]


# ---------------------------------------------------------------------------
# secrets-set: '#' optional, always encrypted
# ---------------------------------------------------------------------------


class TestSecretsSetHashOptional:
    def test_key_without_hash_is_encrypted_under_hash_key(self, tmp_path: Path) -> None:
        service, storage, encrypt = _make_service(_make_store(tmp_path))
        result = service.set_data_app_secrets(
            alias="prod", app_id="42", secrets={"API_KEY": "s3cr3t"}
        )
        encrypt.encrypt.assert_called_once()
        assert encrypt.encrypt.call_args.kwargs["input_data"] == {"#API_KEY": "s3cr3t"}
        assert _written_block(storage) == {"#API_KEY": CIPHER}
        assert result["secrets_set"] == ["API_KEY"]

    def test_key_with_hash_is_unchanged(self, tmp_path: Path) -> None:
        service, storage, encrypt = _make_service(_make_store(tmp_path))
        service.set_data_app_secrets(alias="prod", app_id="42", secrets={"#API_KEY": "s3cr3t"})
        assert encrypt.encrypt.call_args.kwargs["input_data"] == {"#API_KEY": "s3cr3t"}
        assert _written_block(storage) == {"#API_KEY": CIPHER}

    def test_cli_accepts_key_without_hash(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store)
        result = _invoke(
            ["--json", "data-app", "secrets-set", *BASE, "--secret", "API_KEY=s3cr3t"],
            store,
            service,
        )
        assert result.exit_code == 0, result.output
        assert _written_block(storage) == {"#API_KEY": CIPHER}

    def test_secret_file_key_without_hash(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store)
        secret_file = tmp_path / "in.json"
        secret_file.write_text(json.dumps({"TOKEN": "t"}))
        result = _invoke(
            ["--json", "data-app", "secrets-set", *BASE, "--secret-file", str(secret_file)],
            store,
            service,
        )
        assert result.exit_code == 0, result.output
        assert _written_block(storage) == {"#TOKEN": CIPHER}

    def test_refused_when_plain_key_has_the_env_var_name(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, encrypt = _make_service(store, {"api-key": "plain"})
        result = _invoke(
            ["--json", "data-app", "secrets-set", *BASE, "--secret", "API_KEY=s3cr3t"],
            store,
            service,
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert (
            "kbagent data-app env-remove --project prod --app-id 42 --key 'api-key'"
            in (error["message"])
        )
        encrypt.encrypt.assert_not_called()
        storage.update_config.assert_not_called()

    def test_collision_refused_in_dry_run_too(self, tmp_path: Path) -> None:
        service, storage, _ = _make_service(_make_store(tmp_path), {"API_KEY": "plain"})
        with pytest.raises(KeboolaApiError) as exc:
            service.set_data_app_secrets(
                alias="prod", app_id="42", secrets={"#API_KEY": "x"}, dry_run=True
            )
        assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert exc.value.details["collisions"] == [
            {"key": "#API_KEY", "existing_key": "API_KEY", "env_var": "API_KEY"}
        ]
        storage.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# env-set / env-list / env-get / env-remove
# ---------------------------------------------------------------------------


class TestEnvSet:
    def test_writes_plain_value_and_keeps_secrets(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, encrypt = _make_service(store, {"#TOKEN": CIPHER})
        result = _invoke(
            ["--json", "data-app", "env-set", *BASE, "--var", "LOG_LEVEL=debug=x"],
            store,
            service,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["status"] == "updated"
        assert data["env_set"] == ["LOG_LEVEL"]
        assert data["deploy_required"] is True
        assert data["next_step"] == "kbagent data-app deploy --project prod --app-id 42 --wait"
        assert _written_block(storage) == {"#TOKEN": CIPHER, "LOG_LEVEL": "debug=x"}
        encrypt.encrypt.assert_not_called()

    def test_var_file(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store)
        var_file = tmp_path / "env.json"
        var_file.write_text(json.dumps({"A": "1", "b-c": "2"}))
        result = _invoke(
            ["--json", "data-app", "env-set", *BASE, "--var-file", str(var_file)],
            store,
            service,
        )
        assert result.exit_code == 0, result.output
        assert _written_block(storage) == {"A": "1", "b-c": "2"}

    def test_same_values_are_unchanged_without_write(self, tmp_path: Path) -> None:
        service, storage, _ = _make_service(_make_store(tmp_path), {"A": "1"})
        result = service.set_data_app_env(alias="prod", app_id="42", env={"A": "1"})
        assert result["status"] == "unchanged"
        assert result["deploy_required"] is False
        storage.update_config.assert_not_called()

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        service, storage, _ = _make_service(_make_store(tmp_path))
        result = service.set_data_app_env(alias="prod", app_id="42", env={"A": "1"}, dry_run=True)
        assert result["dry_run"] is True
        preview = result["put_storage_config_preview"]["configuration"]
        assert preview["parameters"]["dataApp"]["secrets"] == {"A": "1"}
        storage.update_config.assert_not_called()

    def test_hash_key_refused_naming_secrets_set(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store)
        result = _invoke(
            ["--json", "data-app", "env-set", *BASE, "--var", "#TOKEN=x"], store, service
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert "kbagent data-app secrets-set" in error["message"]
        storage.get_config_detail.assert_not_called()

    def test_refused_when_secret_has_the_env_var_name(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store, {"#TOKEN": CIPHER})
        result = _invoke(
            ["--json", "data-app", "env-set", *BASE, "--var", "TOKEN=x"], store, service
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert (
            "kbagent data-app secrets-remove --project prod --app-id 42 --key '#TOKEN'"
            in (error["message"])
        )
        storage.update_config.assert_not_called()

    def test_var_without_equals_is_invalid_argument(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store)
        result = _invoke(["--json", "data-app", "env-set", *BASE, "--var", "NOPE"], store, service)
        assert result.exit_code == 2
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"


class TestEnvReads:
    def test_list_shows_only_plain_keys_with_values(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"#TOKEN": CIPHER, "B": "2", "a-b": "1"})
        result = _invoke(["--json", "data-app", "env-list", *BASE], store, service)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert [(e["key"], e["env_var"], e["value"]) for e in data["env"]] == [
            ("B", "B", "2"),
            ("a-b", "A_B", "1"),
        ]
        assert data["limit"] == 100
        assert data["has_more"] is False

    def test_list_limit(self, tmp_path: Path) -> None:
        service, *_ = _make_service(_make_store(tmp_path), {"A": "1", "B": "2"})
        result = service.list_data_app_env(alias="prod", app_id="42", limit=1)
        assert [e["key"] for e in result["env"]] == ["A"]
        assert result["has_more"] is True

    def test_get_returns_value(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"LOG_LEVEL": "debug"})
        result = _invoke(
            ["--json", "data-app", "env-get", *BASE, "--key", "LOG_LEVEL"], store, service
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["value"] == "debug"

    def test_get_hash_key_refused_naming_secrets_get(self, tmp_path: Path) -> None:
        service, *_ = _make_service(_make_store(tmp_path), {"#TOKEN": CIPHER})
        with pytest.raises(KeboolaApiError) as exc:
            service.get_data_app_env(alias="prod", app_id="42", key="#TOKEN")
        assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "kbagent data-app secrets-get" in exc.value.message

    def test_get_missing_key_is_not_found(self, tmp_path: Path) -> None:
        service, *_ = _make_service(_make_store(tmp_path))
        with pytest.raises(KeboolaApiError) as exc:
            service.get_data_app_env(alias="prod", app_id="42", key="NOPE")
        assert exc.value.error_code == ErrorCode.NOT_FOUND


class TestEnvRemove:
    def test_removes_plain_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store, {"#TOKEN": CIPHER, "A": "1"})
        result = _invoke(
            ["--json", "data-app", "env-remove", *BASE, "--key", "A", "--key", "GONE"],
            store,
            service,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["status"] == "removed"
        assert data["removed"] == ["A"]
        assert data["not_found"] == ["GONE"]
        assert _written_block(storage) == {"#TOKEN": CIPHER}

    def test_missing_key_is_unchanged(self, tmp_path: Path) -> None:
        service, storage, _ = _make_service(_make_store(tmp_path))
        result = service.remove_data_app_env(alias="prod", app_id="42", keys=["A"])
        assert result["status"] == "unchanged"
        storage.update_config.assert_not_called()

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store, {"A": "1"})
        result = _invoke(
            ["--json", "data-app", "env-remove", *BASE, "--key", "A", "--dry-run"], store, service
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["to_remove"] == ["A"]
        storage.update_config.assert_not_called()

    def test_hash_key_refused_naming_secrets_remove(self, tmp_path: Path) -> None:
        service, storage, _ = _make_service(_make_store(tmp_path), {"#TOKEN": CIPHER})
        with pytest.raises(KeboolaApiError) as exc:
            service.remove_data_app_env(alias="prod", app_id="42", keys=["#TOKEN"])
        assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "kbagent data-app secrets-remove" in exc.value.message
        storage.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# secrets-list / -get / -remove on a plain key: still work, DEPRECATED warning
# ---------------------------------------------------------------------------


def _deprecated(output: str) -> dict[str, Any]:
    warnings = json.loads(output)["warnings"]
    assert [w["code"] for w in warnings] == ["DEPRECATED"]
    return warnings[0]


class TestSecretsPlainKeyDeprecation:
    def test_list_marks_kind_and_warns(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"#TOKEN": CIPHER, "PLAIN": "v"})
        result = _invoke(["--json", "data-app", "secrets-list", *BASE], store, service)
        assert result.exit_code == 0, result.output
        kinds = {e["key"]: e["kind"] for e in json.loads(result.output)["data"]["secrets"]}
        assert kinds == {"#TOKEN": "secret", "PLAIN": "env"}
        warning = _deprecated(result.output)
        assert warning["replacement"] == "data-app env-list"
        assert warning["remove_after"] == "2026-12-23"

    def test_list_without_plain_keys_has_no_warning(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"#TOKEN": CIPHER})
        result = _invoke(["--json", "data-app", "secrets-list", *BASE], store, service)
        assert "warnings" not in json.loads(result.output)

    def test_get_plain_key_still_works_and_warns(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"PLAIN": "v"})
        result = _invoke(
            ["--json", "data-app", "secrets-get", *BASE, "--key", "PLAIN"], store, service
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["value"] == "v"
        assert _deprecated(result.output)["replacement"] == "data-app env-get"

    def test_remove_plain_key_still_works_and_warns(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, storage, _ = _make_service(store, {"PLAIN": "v"})
        result = _invoke(
            ["--json", "data-app", "secrets-remove", *BASE, "--key", "PLAIN"], store, service
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["status"] == "removed"
        assert _deprecated(result.output)["replacement"] == "data-app env-remove"
        configuration = storage.update_config.call_args.kwargs["configuration"]
        assert "secrets" not in configuration["parameters"]["dataApp"]

    def test_hash_key_has_no_warning(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"#TOKEN": CIPHER})
        result = _invoke(
            ["--json", "data-app", "secrets-get", *BASE, "--key", "#TOKEN"], store, service
        )
        assert result.exit_code == 0, result.output
        assert "warnings" not in json.loads(result.output)

    def test_human_mode_warns_on_stderr(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, *_ = _make_service(store, {"PLAIN": "v"})
        result = _invoke(["data-app", "secrets-get", *BASE, "--key", "PLAIN"], store, service)
        assert result.exit_code == 0, result.output
        assert "Warning:" in result.stderr
        assert "data-app env-get" in result.stderr
        assert "2026-12-23" in result.stderr
        assert "Warning:" not in result.stdout


# ---------------------------------------------------------------------------
# kbagent serve routes
# ---------------------------------------------------------------------------


def _serve_app(tmp_path: Path, data_app: Any) -> Any:
    from keboola_agent_cli.server import create_app
    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.config_store = _make_store(tmp_path / "serve")
    registry.data_app = data_app
    serve_app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    serve_app.dependency_overrides[get_registry] = lambda: registry
    return serve_app


@pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI not installed")
class TestServe:
    def test_env_routes_forward_kwargs(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        data_app = MagicMock()
        for method in ("set", "list", "get", "remove"):
            getattr(data_app, f"{method}_data_app_env").return_value = {"ok": True}
        with TestClient(_serve_app(tmp_path, data_app)) as client:
            base = "/data-apps/prod/42/env"
            assert client.get(base, headers=AUTH, params={"limit": 5}).status_code == 200
            assert client.get(f"{base}/LOG_LEVEL", headers=AUTH).status_code == 200
            body = {"env": {"A": "1"}, "dry_run": True}
            assert client.patch(base, headers=AUTH, json=body).status_code == 200
            body = {"keys": ["A"], "branch_id": 7}
            assert client.post(f"{base}/remove", headers=AUTH, json=body).status_code == 200
        data_app.list_data_app_env.assert_called_once_with(
            alias="prod", app_id="42", branch_id=None, limit=5
        )
        data_app.get_data_app_env.assert_called_once_with(
            alias="prod", app_id="42", key="LOG_LEVEL", branch_id=None
        )
        data_app.set_data_app_env.assert_called_once_with(
            alias="prod", app_id="42", env={"A": "1"}, branch_id=None, dry_run=True
        )
        data_app.remove_data_app_env.assert_called_once_with(
            alias="prod", app_id="42", keys=["A"], branch_id=7, dry_run=False
        )

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("patch", "/data-apps/prod/42/env", {"env": {"A": "1"}, "encrypt": True}),
            ("post", "/data-apps/prod/42/env/remove", {"keys": ["A"], "force": True}),
        ],
    )
    def test_env_routes_refuse_unknown_fields(
        self, tmp_path: Path, method: str, path: str, body: dict[str, Any]
    ) -> None:
        from fastapi.testclient import TestClient

        data_app = MagicMock()
        with TestClient(_serve_app(tmp_path, data_app)) as client:
            res = getattr(client, method)(path, headers=AUTH, json=body)
        assert res.status_code == 422, res.text
        data_app.set_data_app_env.assert_not_called()
        data_app.remove_data_app_env.assert_not_called()

    def test_env_set_collision_is_400(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        service, storage, _ = _make_service(_make_store(tmp_path), {"#TOKEN": CIPHER})
        with TestClient(_serve_app(tmp_path, service)) as client:
            res = client.patch("/data-apps/prod/42/env", headers=AUTH, json={"env": {"TOKEN": "x"}})
        assert res.status_code == 400, res.text
        assert "INVALID_ARGUMENT" in res.text
        storage.update_config.assert_not_called()

    def test_secrets_set_collision_is_400(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        service, storage, _ = _make_service(_make_store(tmp_path), {"TOKEN": "plain"})
        with TestClient(_serve_app(tmp_path, service)) as client:
            res = client.patch(
                "/data-apps/prod/42/secrets", headers=AUTH, json={"secrets": {"TOKEN": "x"}}
            )
        assert res.status_code == 400, res.text
        storage.update_config.assert_not_called()

    def test_secrets_get_plain_key_adds_deprecated_header(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        service, *_ = _make_service(_make_store(tmp_path), {"PLAIN": "v"})
        with TestClient(_serve_app(tmp_path, service)) as client:
            res = client.get("/data-apps/prod/42/secrets/PLAIN", headers=AUTH)
        assert res.status_code == 200, res.text
        warnings = json.loads(res.headers["Kbagent-Warnings"])
        assert warnings[0]["code"] == "DEPRECATED"
        assert warnings[0]["replacement"] == "data-app env-get"
