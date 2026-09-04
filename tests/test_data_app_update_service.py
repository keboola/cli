"""Service-layer tests for ``DataAppService.update_data_app`` (issue #737).

Covers the read-modify-write contract: only the requested fields change,
everything else in the config body survives bit-identical, and a request
that already matches the stored config writes nothing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.data_app_service import DataAppService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
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


def _base_configuration() -> dict[str, Any]:
    """A realistic data-app config body: git + secrets + storage mapping."""
    return {
        "parameters": {
            "id": "74021026",
            "autoSuspendAfterSeconds": 900,
            "dataApp": {
                "slug": "people-review",
                "git": {
                    "repository": "https://github.com/org/repo",
                    "branch": "main",
                    "private": True,
                    "username": "u",
                    "#password": "KBC::ProjectSecureGKMS::ciphertext",
                },
                "secrets": {"#API_KEY": "KBC::ProjectSecureGKMS::secret"},
            },
        },
        "runtime": {"backend": {"size": "tiny"}},
        "storage": {"input": {"tables": [{"source": "in.c-main.data"}]}},
        "authorization": {
            "app_proxy": {
                "auth_providers": [{"id": "simpleAuth", "type": "password"}],
                "auth_rules": [
                    {
                        "type": "pathPrefix",
                        "value": "/",
                        "auth_required": True,
                        "auth": ["simpleAuth"],
                    }
                ],
            }
        },
    }


def _make_service(
    store: ConfigStore, configuration: dict[str, Any] | None = None
) -> tuple[DataAppService, MagicMock, MagicMock]:
    ds_mock = MagicMock()
    ds_mock.get_app.return_value = {"id": "74021026", "configId": "9001"}
    storage_mock = MagicMock()
    storage_mock.get_config_detail.return_value = {
        "id": "9001",
        "version": 7,
        "configuration": _base_configuration() if configuration is None else configuration,
    }
    storage_mock.update_config.return_value = {"version": 8}
    service = DataAppService(
        config_store=store,
        client_factory=lambda url, token: storage_mock,
        ds_client_factory=lambda url, token: ds_mock,
        encrypt_service=MagicMock(),
    )
    return service, ds_mock, storage_mock


def _put_body(storage_mock: MagicMock) -> dict[str, Any]:
    return storage_mock.update_config.call_args.kwargs["configuration"]


# ---------------------------------------------------------------------------
# Enabling Storage access -- the issue's primary ask
# ---------------------------------------------------------------------------


def test_update_enables_workspace(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    result = service.update_data_app(alias="prod", app_id="74021026", workspace=True)

    assert result["changed"] == ["workspace"]
    assert result["deploy_required"] is True
    assert result["config_version_before"] == "7"
    assert result["config_version_after"] == "8"
    assert _put_body(storage)["runtime"]["workspace"] == {"enabled": True}


def test_update_disabling_workspace_drops_the_key(tmp_path: Path) -> None:
    """`--no-workspace` must omit the key, not write ``enabled: false``.

    ``create --no-workspace`` produces a body with no ``runtime.workspace``
    at all; writing ``false`` here would make the two paths diverge.
    """
    configuration = _base_configuration()
    configuration["runtime"]["workspace"] = {"enabled": True}
    service, _ds, storage = _make_service(_make_store(tmp_path), configuration)

    result = service.update_data_app(alias="prod", app_id="74021026", workspace=False)

    assert result["changed"] == ["workspace"]
    assert "workspace" not in _put_body(storage)["runtime"]


def test_update_preserves_every_untouched_key(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    service.update_data_app(alias="prod", app_id="74021026", workspace=True)

    body = _put_body(storage)
    original = _base_configuration()
    assert body["storage"] == original["storage"]
    assert body["authorization"] == original["authorization"]
    assert body["parameters"]["dataApp"] == original["parameters"]["dataApp"]
    assert body["parameters"]["id"] == "74021026"
    assert body["runtime"]["backend"] == {"size": "tiny"}


# ---------------------------------------------------------------------------
# The other updatable fields
# ---------------------------------------------------------------------------


def test_update_auto_suspend_and_size(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    result = service.update_data_app(
        alias="prod", app_id="74021026", auto_suspend_after_seconds=300, size="small"
    )

    assert result["changed"] == ["auto_suspend_after_seconds", "size"]
    body = _put_body(storage)
    assert body["parameters"]["autoSuspendAfterSeconds"] == 300
    assert body["runtime"]["backend"]["size"] == "small"


def test_update_auth_switches_the_proxy_block(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    result = service.update_data_app(alias="prod", app_id="74021026", auth="public")

    assert result["changed"] == ["auth"]
    assert result["changes"][0]["before"] == "password"
    rules = _put_body(storage)["authorization"]["app_proxy"]["auth_rules"]
    assert rules[0]["auth_required"] is False
    assert "auth" not in rules[0]


def test_update_git_branch(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    service.update_data_app(alias="prod", app_id="74021026", git_branch="release")

    git = _put_body(storage)["parameters"]["dataApp"]["git"]
    assert git["branch"] == "release"
    # The encrypted PAT must survive the rewrite untouched.
    assert git["#password"] == "KBC::ProjectSecureGKMS::ciphertext"


def test_update_git_branch_rejected_without_a_git_block(tmp_path: Path) -> None:
    configuration = _base_configuration()
    del configuration["parameters"]["dataApp"]["git"]
    service, _ds, storage = _make_service(_make_store(tmp_path), configuration)

    with pytest.raises(KeboolaApiError) as exc:
        service.update_data_app(alias="prod", app_id="74021026", git_branch="release")
    assert exc.value.error_code == ErrorCode.DATA_APP_INVALID_GIT
    storage.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# No-ops, dry runs, validation
# ---------------------------------------------------------------------------


def test_update_with_matching_values_writes_nothing(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    result = service.update_data_app(
        alias="prod", app_id="74021026", auto_suspend_after_seconds=900, size="tiny"
    )

    assert result["changed"] == []
    assert result["deploy_required"] is False
    assert result["config_version_after"] == "7"
    storage.update_config.assert_not_called()


def test_update_dry_run_makes_no_put(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    result = service.update_data_app(alias="prod", app_id="74021026", workspace=True, dry_run=True)

    assert result["dry_run"] is True
    preview = result["put_storage_config_preview"]["configuration"]
    assert preview["runtime"]["workspace"] == {"enabled": True}
    storage.update_config.assert_not_called()


def test_update_dry_run_redacts_the_encrypted_pat(tmp_path: Path) -> None:
    service, _ds, _storage = _make_service(_make_store(tmp_path))
    result = service.update_data_app(alias="prod", app_id="74021026", workspace=True, dry_run=True)

    preview = result["put_storage_config_preview"]["configuration"]
    assert preview["parameters"]["dataApp"]["git"]["#password"] == "<encrypted>"


def test_update_without_any_field_is_a_usage_error(tmp_path: Path) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    with pytest.raises(KeboolaApiError) as exc:
        service.update_data_app(alias="prod", app_id="74021026")
    assert exc.value.error_code == ErrorCode.MISSING_PARAMETER
    storage.get_config_detail.assert_not_called()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"size": "enormous"}, ErrorCode.VALIDATION_ERROR),
        ({"auth": "oauth"}, ErrorCode.VALIDATION_ERROR),
        ({"auto_suspend_after_seconds": -1}, ErrorCode.VALIDATION_ERROR),
        ({"git_branch": "bad\nbranch"}, ErrorCode.VALIDATION_ERROR),
    ],
)
def test_update_rejects_invalid_values(
    tmp_path: Path, kwargs: dict[str, Any], code: ErrorCode
) -> None:
    service, _ds, storage = _make_service(_make_store(tmp_path))
    with pytest.raises(KeboolaApiError) as exc:
        service.update_data_app(alias="prod", app_id="74021026", **kwargs)
    assert exc.value.error_code == code
    storage.get_config_detail.assert_not_called()


# ---------------------------------------------------------------------------
# detail surfaces the Storage-access state (the diagnosis half of #737)
# ---------------------------------------------------------------------------


def test_detail_reports_workspace_disabled_when_the_key_is_absent(tmp_path: Path) -> None:
    service, _ds, _storage = _make_service(_make_store(tmp_path))
    assert service.get_data_app(alias="prod", app_id="74021026")["workspace_enabled"] is False


def test_detail_reports_workspace_enabled(tmp_path: Path) -> None:
    configuration = _base_configuration()
    configuration["runtime"]["workspace"] = {"enabled": True}
    service, _ds, _storage = _make_service(_make_store(tmp_path), configuration)
    assert service.get_data_app(alias="prod", app_id="74021026")["workspace_enabled"] is True
