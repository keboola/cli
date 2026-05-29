"""Tests for FeatureService - stack/project/user feature-flag management."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.feature_service import FeatureService

STACK_URL = "https://connection.us-east4.gcp.keboola.com"
MANAGE_TOKEN = "manage-12345-abcdefghijklmnopqrstuvwxyz0123456789"
PROJECT_ID = 5725
ALIAS = "cuesta-master"
EMAIL = "max.ottomansky@keboola.com"


@pytest.fixture
def store_with_master(tmp_config_dir: Path) -> ConfigStore:
    """ConfigStore with the master cuesta project pre-registered."""
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        ALIAS,
        ProjectConfig(
            stack_url=STACK_URL,
            token="901-fake-storage-token-1234567890",
            project_name="[Cuesta training] - Master",
            project_id=PROJECT_ID,
        ),
    )
    return store


@pytest.fixture
def store_without_project_id(tmp_config_dir: Path) -> ConfigStore:
    """ConfigStore with an alias that has no numeric project_id."""
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        "no-id",
        ProjectConfig(
            stack_url=STACK_URL,
            token="901-fake-storage-token-1234567890",
            project_name="No ID project",
            project_id=None,
        ),
    )
    return store


@pytest.fixture
def manage_client_factory():
    """Factory returning a single shared MagicMock manage client."""
    mock = MagicMock()
    mock._stack_url = STACK_URL
    factory = MagicMock(return_value=mock)
    return factory, mock


# ──────────────────────────────────────────────────────────────────────
# Stack catalogue
# ──────────────────────────────────────────────────────────────────────


class TestListStackFeatures:
    def test_normalises_dict_features(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_features.return_value = [
            {"name": "queue-v2", "title": "Queue v2", "type": "project"},
            {"name": "snowflake-dwh", "title": "Snowflake"},
        ]
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.list_stack_features(manage_token=MANAGE_TOKEN, alias=ALIAS)

        assert result["alias"] == ALIAS
        assert result["stack_url"] == STACK_URL
        assert [f["name"] for f in result["features"]] == ["queue-v2", "snowflake-dwh"]
        assert result["features"][0]["title"] == "Queue v2"
        # Factory must be bound to the resolved stack URL + token.
        factory.assert_called_once_with(STACK_URL, MANAGE_TOKEN)
        mock_client.close.assert_called_once()

    def test_normalises_bare_string_features(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_features.return_value = ["queue-v2", "snowflake-dwh"]
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.list_stack_features(manage_token=MANAGE_TOKEN, alias=ALIAS)

        assert result["features"] == [
            {"name": "queue-v2", "title": "", "description": "", "type": ""},
            {"name": "snowflake-dwh", "title": "", "description": "", "type": ""},
        ]
        mock_client.close.assert_called_once()

    def test_unknown_alias_raises_config_error(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="not registered"):
            svc.list_stack_features(manage_token=MANAGE_TOKEN, alias="does-not-exist")
        factory.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Project features
# ──────────────────────────────────────────────────────────────────────


class TestListProjectFeatures:
    def test_reads_features_from_project_object(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.get_project.return_value = {
            "name": "[Cuesta training] - Master",
            "features": [{"name": "queue-v2"}, "input-mapping-default"],
        }
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.list_project_features(manage_token=MANAGE_TOKEN, alias=ALIAS)

        assert result["alias"] == ALIAS
        assert result["project_id"] == PROJECT_ID
        assert result["project_name"] == "[Cuesta training] - Master"
        # Mixed dict + bare string both normalise to the uniform shape.
        assert [f["name"] for f in result["features"]] == ["queue-v2", "input-mapping-default"]
        mock_client.get_project.assert_called_once_with(PROJECT_ID)
        mock_client.close.assert_called_once()

    def test_missing_features_key_yields_empty_list(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.get_project.return_value = {"name": "X"}
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.list_project_features(manage_token=MANAGE_TOKEN, alias=ALIAS)

        assert result["features"] == []

    def test_unknown_alias_raises_config_error(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="not registered"):
            svc.list_project_features(manage_token=MANAGE_TOKEN, alias="does-not-exist")

    def test_missing_project_id_raises_config_error(
        self, store_without_project_id, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = FeatureService(store_without_project_id, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="no numeric project_id"):
            svc.list_project_features(manage_token=MANAGE_TOKEN, alias="no-id")
        factory.assert_not_called()


class TestAddProjectFeature:
    def test_live_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.add_project_feature(manage_token=MANAGE_TOKEN, alias=ALIAS, feature="queue-v2")

        assert result["status"] == "added"
        assert result["project_id"] == PROJECT_ID
        assert result["feature"] == "queue-v2"
        mock_client.add_project_feature.assert_called_once_with(PROJECT_ID, "queue-v2")
        mock_client.close.assert_called_once()

    def test_dry_run_makes_no_client_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.add_project_feature(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature="queue-v2", dry_run=True
        )

        assert result["status"] == "dry_run"
        assert result["action"] == "add"
        assert result["feature"] == "queue-v2"
        factory.assert_not_called()
        mock_client.add_project_feature.assert_not_called()


class TestRemoveProjectFeature:
    def test_live_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.remove_project_feature(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature="queue-v2"
        )

        assert result["status"] == "removed"
        assert result["project_id"] == PROJECT_ID
        mock_client.remove_project_feature.assert_called_once_with(PROJECT_ID, "queue-v2")
        mock_client.close.assert_called_once()

    def test_dry_run_makes_no_client_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.remove_project_feature(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature="queue-v2", dry_run=True
        )

        assert result["status"] == "dry_run"
        assert result["action"] == "remove"
        factory.assert_not_called()
        mock_client.remove_project_feature.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# User features
# ──────────────────────────────────────────────────────────────────────


class TestListUserFeatures:
    def test_reads_features_from_user_object(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.get_user.return_value = {
            "email": EMAIL,
            "features": ["admin-ui-beta", {"name": "early-access"}],
        }
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.list_user_features(manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL)

        assert result["alias"] == ALIAS
        assert result["stack_url"] == STACK_URL
        assert result["email"] == EMAIL
        assert [f["name"] for f in result["features"]] == ["admin-ui-beta", "early-access"]
        mock_client.get_user.assert_called_once_with(EMAIL)
        # User ops resolve the stack URL only -- no numeric project_id required.
        factory.assert_called_once_with(STACK_URL, MANAGE_TOKEN)
        mock_client.close.assert_called_once()

    def test_works_without_project_id(
        self, store_without_project_id, manage_client_factory
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.get_user.return_value = {"email": EMAIL, "features": []}
        svc = FeatureService(store_without_project_id, manage_client_factory=factory)

        result = svc.list_user_features(manage_token=MANAGE_TOKEN, alias="no-id", email=EMAIL)

        assert result["features"] == []
        mock_client.close.assert_called_once()

    def test_unknown_alias_raises_config_error(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="not registered"):
            svc.list_user_features(manage_token=MANAGE_TOKEN, alias="does-not-exist", email=EMAIL)


class TestAddUserFeature:
    def test_live_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.add_user_feature(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL, feature="admin-ui-beta"
        )

        assert result["status"] == "added"
        assert result["email"] == EMAIL
        assert result["feature"] == "admin-ui-beta"
        mock_client.add_user_feature.assert_called_once_with(EMAIL, "admin-ui-beta")
        mock_client.close.assert_called_once()

    def test_dry_run_makes_no_client_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.add_user_feature(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email=EMAIL,
            feature="admin-ui-beta",
            dry_run=True,
        )

        assert result["status"] == "dry_run"
        assert result["action"] == "add"
        factory.assert_not_called()
        mock_client.add_user_feature.assert_not_called()


class TestRemoveUserFeature:
    def test_live_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.remove_user_feature(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL, feature="admin-ui-beta"
        )

        assert result["status"] == "removed"
        assert result["email"] == EMAIL
        mock_client.remove_user_feature.assert_called_once_with(EMAIL, "admin-ui-beta")
        mock_client.close.assert_called_once()

    def test_dry_run_makes_no_client_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        result = svc.remove_user_feature(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email=EMAIL,
            feature="admin-ui-beta",
            dry_run=True,
        )

        assert result["status"] == "dry_run"
        assert result["action"] == "remove"
        factory.assert_not_called()
        mock_client.remove_user_feature.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# close() on error
# ──────────────────────────────────────────────────────────────────────


class TestCloseOnError:
    def test_close_fires_even_when_client_raises(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_features.side_effect = RuntimeError("boom")
        svc = FeatureService(store_with_master, manage_client_factory=factory)

        with pytest.raises(RuntimeError, match="boom"):
            svc.list_stack_features(manage_token=MANAGE_TOKEN, alias=ALIAS)
        mock_client.close.assert_called_once()
