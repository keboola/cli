"""Tests for SharingService - bucket sharing and linking business logic."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.sharing_service import SharingService


class TestResolveToken:
    """Tests for master token resolution logic."""

    def test_project_specific_env_var(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KBC_MASTER_TOKEN_{ALIAS} takes priority over global."""
        store = setup_single_project(tmp_config_dir)
        service = SharingService(config_store=store)

        monkeypatch.setenv("KBC_MASTER_TOKEN_PROD", "master-prod-token")
        monkeypatch.setenv("KBC_MASTER_TOKEN", "master-global-token")

        projects = service.resolve_projects(["prod"])
        token = service.resolve_master_token("prod", projects["prod"])
        assert token == "master-prod-token"

    def test_global_env_var_fallback(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KBC_MASTER_TOKEN used when project-specific var not set."""
        store = setup_single_project(tmp_config_dir)
        service = SharingService(config_store=store)

        monkeypatch.delenv("KBC_MASTER_TOKEN_PROD", raising=False)
        monkeypatch.setenv("KBC_MASTER_TOKEN", "master-global-token")

        projects = service.resolve_projects(["prod"])
        token = service.resolve_master_token("prod", projects["prod"])
        assert token == "master-global-token"

    def test_project_token_fallback(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to project's configured token when no master token set."""
        store = setup_single_project(tmp_config_dir, token="901-regular-token")
        service = SharingService(config_store=store)

        monkeypatch.delenv("KBC_MASTER_TOKEN_PROD", raising=False)
        monkeypatch.delenv("KBC_MASTER_TOKEN", raising=False)

        projects = service.resolve_projects(["prod"])
        token = service.resolve_master_token("prod", projects["prod"])
        assert token == "901-regular-token"

    def test_alias_with_hyphens(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hyphens in alias are converted to underscores for env var lookup."""
        store = setup_single_project(tmp_config_dir, alias="padak-2-0")
        service = SharingService(config_store=store)

        monkeypatch.setenv("KBC_MASTER_TOKEN_PADAK_2_0", "master-padak")

        projects = service.resolve_projects(["padak-2-0"])
        token = service.resolve_master_token("padak-2-0", projects["padak-2-0"])
        assert token == "master-padak"


class TestListShared:
    """Tests for listing shared buckets."""

    def test_list_shared_success(self, tmp_config_dir: Path) -> None:
        """list_shared returns shared buckets from API."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.list_shared_buckets.return_value = [
            {
                "id": "out.c-data",
                "displayName": "data",
                "description": "Shared data",
                "sharing": "organization-project",
                "backend": "snowflake",
                "rowsCount": 1000,
                "dataSizeBytes": 50000,
                "tables": [{"id": "out.c-data.users", "name": "users", "displayName": "users"}],
                "project": {"id": 123, "name": "Source Project"},
                "sharedBy": {"id": 1, "name": "admin", "date": "2026-01-01"},
            }
        ]

        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_shared(aliases=["prod"])
        assert len(result["shared_buckets"]) == 1
        assert result["shared_buckets"][0]["source_bucket_id"] == "out.c-data"
        assert result["shared_buckets"][0]["source_project_id"] == 123
        assert result["shared_buckets"][0]["sharing"] == "organization-project"
        assert len(result["shared_buckets"][0]["tables"]) == 1

    def test_list_shared_deduplicates(self, tmp_config_dir: Path) -> None:
        """Same shared bucket seen from multiple projects is only listed once."""
        store = setup_two_projects(tmp_config_dir)

        shared_bucket = {
            "id": "out.c-data",
            "displayName": "data",
            "description": "",
            "sharing": "organization",
            "backend": "snowflake",
            "rowsCount": 0,
            "dataSizeBytes": 0,
            "tables": [],
            "project": {"id": 123, "name": "Source"},
        }

        mock_client = MagicMock()
        mock_client.list_shared_buckets.return_value = [shared_bucket]

        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_shared()
        assert len(result["shared_buckets"]) == 1


class TestShare:
    """Tests for sharing a bucket."""

    def test_share_success(self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """share calls client.share_bucket with master token."""
        store = setup_single_project(tmp_config_dir)
        monkeypatch.setenv("KBC_MASTER_TOKEN_PROD", "master-token")

        mock_client = MagicMock()
        mock_client.share_bucket.return_value = {"status": "success", "id": 123}

        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.share(
            alias="prod",
            bucket_id="out.c-data",
            sharing_type="organization-project",
        )

        assert result["job_status"] == "success"
        assert result["sharing_type"] == "organization-project"
        mock_client.share_bucket.assert_called_once_with(
            bucket_id="out.c-data",
            sharing_type="organization-project",
            target_project_ids=None,
            target_users=None,
        )

    def test_share_uses_master_token(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """share creates client with master token, not project token."""
        store = setup_single_project(tmp_config_dir, token="regular-token")
        monkeypatch.setenv("KBC_MASTER_TOKEN_PROD", "master-token")

        created_tokens: list[str] = []

        def factory(url: str, token: str) -> MagicMock:
            created_tokens.append(token)
            client = MagicMock()
            client.share_bucket.return_value = {"status": "success"}
            return client

        service = SharingService(config_store=store, client_factory=factory)
        service.share(alias="prod", bucket_id="out.c-data", sharing_type="organization")

        assert created_tokens == ["master-token"]


class TestUnshare:
    """Tests for disabling sharing."""

    def test_unshare_success(self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = setup_single_project(tmp_config_dir)
        monkeypatch.setenv("KBC_MASTER_TOKEN_PROD", "master-token")

        mock_client = MagicMock()
        mock_client.unshare_bucket.return_value = {"status": "success"}

        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.unshare(alias="prod", bucket_id="out.c-data")
        assert result["job_status"] == "success"
        mock_client.unshare_bucket.assert_called_once_with(bucket_id="out.c-data")


class TestLink:
    """Tests for linking a shared bucket."""

    def test_link_success(self, tmp_config_dir: Path) -> None:
        """link creates linked bucket using regular project token."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.link_bucket.return_value = {
            "status": "success",
            "results": {"id": "in.c-shared-data"},
        }

        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.link(
            alias="prod",
            source_project_id=999,
            source_bucket_id="out.c-data",
            name="shared-data",
        )

        assert result["linked_bucket_id"] == "in.c-shared-data"
        assert result["source_project_id"] == 999
        mock_client.link_bucket.assert_called_once_with(
            source_project_id=999,
            source_bucket_id="out.c-data",
            name="shared-data",
        )

    def test_link_auto_name(self, tmp_config_dir: Path) -> None:
        """link generates name from source bucket ID when not specified."""
        store = setup_single_project(tmp_config_dir)

        mock_client = MagicMock()
        mock_client.link_bucket.return_value = {
            "status": "success",
            "results": {"id": "in.c-shared-data"},
        }

        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.link(alias="prod", source_project_id=999, source_bucket_id="out.c-data")

        call_args = mock_client.link_bucket.call_args
        assert call_args.kwargs["name"] == "shared-data"

    def test_link_uses_regular_token(self, tmp_config_dir: Path) -> None:
        """link uses the project's regular token, not master token."""
        store = setup_single_project(tmp_config_dir, token="regular-token")

        created_tokens: list[str] = []

        def factory(url: str, token: str) -> MagicMock:
            created_tokens.append(token)
            client = MagicMock()
            client.link_bucket.return_value = {"status": "success", "results": {"id": "in.c-x"}}
            return client

        service = SharingService(config_store=store, client_factory=factory)
        service.link(alias="prod", source_project_id=999, source_bucket_id="out.c-x")

        assert created_tokens == ["regular-token"]


class TestUnlink:
    """Tests for removing a linked bucket."""

    def test_unlink_success(self, tmp_config_dir: Path) -> None:
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-shared-data",
            "sourceBucket": {"id": "out.c-data", "project": {"id": 999}},
        }
        mock_client.delete_bucket.return_value = {"status": "success"}

        store = setup_single_project(tmp_config_dir)
        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.unlink(alias="prod", bucket_id="in.c-shared-data")
        assert "deleted" in result["message"]
        mock_client.delete_bucket.assert_called_once_with(bucket_id="in.c-shared-data", force=True)

    def test_unlink_rejects_non_linked_bucket(self, tmp_config_dir: Path) -> None:
        """unlink raises error if bucket is not a linked bucket."""
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {
            "id": "out.c-data",
            "sourceBucket": None,
        }

        store = setup_single_project(tmp_config_dir)
        service = SharingService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="not a linked bucket"):
            service.unlink(alias="prod", bucket_id="out.c-data")
