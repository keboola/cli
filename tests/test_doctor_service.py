"""Tests for DoctorService - health check logic extracted from doctor command."""

from pathlib import Path
from unittest.mock import MagicMock

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.doctor_service import DoctorService
from keboola_agent_cli.services.mcp_service import McpService


def _make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
) -> MagicMock:
    """Create a mock KeboolaClient with verify_token returning valid data."""
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description="My Token",
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
    )
    return mock_client


def _make_failing_client(error: KeboolaApiError) -> MagicMock:
    """Create a mock KeboolaClient whose verify_token raises the given error."""
    mock_client = MagicMock()
    mock_client.verify_token.side_effect = error
    return mock_client


def _make_mcp_service_mock(status: str = "pass") -> MagicMock:
    """Create a mock McpService returning a check_server_available result."""
    mock_mcp = MagicMock(spec=McpService)
    mock_mcp.check_server_available.return_value = {
        "check": "mcp_server",
        "name": "MCP server",
        "status": status,
        "message": "MCP server available" if status == "pass" else "MCP server not found",
    }
    return mock_mcp


class TestDoctorServiceCheckConfigFile:
    """Tests for DoctorService._check_config_file() - config file existence and permissions."""

    def test_config_file_not_found(self, tmp_config_dir: Path) -> None:
        """When config file does not exist, returns 'warn' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_config_file()

        assert result["check"] == "config_file"
        assert result["status"] == "warn"
        assert "not found" in result["message"]

    def test_config_file_exists_correct_permissions(self, tmp_config_dir: Path) -> None:
        """When config file exists with 0600 permissions, returns 'pass' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Test",
                project_id=1234,
            ),
        )
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_config_file()

        assert result["check"] == "config_file"
        assert result["status"] == "pass"
        assert "correct permissions" in result["message"]

    def test_config_file_wrong_permissions(self, tmp_config_dir: Path) -> None:
        """When config file exists with wrong permissions, returns 'warn' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Test",
                project_id=1234,
            ),
        )
        # Change permissions to 0644
        store.config_path.chmod(0o644)

        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_config_file()

        assert result["check"] == "config_file"
        assert result["status"] == "warn"
        assert "permissions" in result["message"]
        assert "0o644" in result["message"]


class TestDoctorServiceCheckConfigSource:
    """Tests for DoctorService._check_config_source() - config source reporting."""

    def test_reports_global_source(self, tmp_config_dir: Path) -> None:
        """Reports global config source."""
        store = ConfigStore(config_dir=tmp_config_dir, source="global")
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_config_source()

        assert result["check"] == "config_source"
        assert result["status"] == "pass"
        assert "global" in result["message"]

    def test_reports_local_source(self, tmp_config_dir: Path) -> None:
        """Reports local config source."""
        store = ConfigStore(config_dir=tmp_config_dir, source="local")
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_config_source()

        assert result["check"] == "config_source"
        assert result["status"] == "pass"
        assert "local" in result["message"]

    def test_config_source_in_run_checks(self, tmp_config_dir: Path) -> None:
        """run_checks includes config_source as first check."""
        store = ConfigStore(config_dir=tmp_config_dir, source="local")
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service.run_checks()

        assert result["checks"][0]["check"] == "config_source"


class TestDoctorServiceCheckConfigValid:
    """Tests for DoctorService._check_config_valid() - config file validation."""

    def test_no_config_file_returns_skip(self, tmp_config_dir: Path) -> None:
        """When config file does not exist, returns 'skip' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "skip"
        assert config is None

    def test_valid_config_file(self, tmp_config_dir: Path) -> None:
        """When config file is valid JSON with projects, returns 'pass'."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Test",
                project_id=1234,
            ),
        )

        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "pass"
        assert "1 project" in result["message"]
        assert config is not None
        assert len(config.projects) == 1

    def test_invalid_json_config(self, tmp_config_dir: Path) -> None:
        """When config file contains invalid JSON, returns 'fail'."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_path = tmp_config_dir / "config.json"
        config_path.write_text("not valid json {{{", encoding="utf-8")
        config_path.chmod(0o600)

        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "fail"
        assert "not valid JSON" in result["message"]
        assert config is None

    def test_valid_json_invalid_structure(self, tmp_config_dir: Path) -> None:
        """When config file is valid JSON but has invalid structure, returns 'fail'."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_path = tmp_config_dir / "config.json"
        # Valid JSON, but invalid structure for AppConfig
        config_path.write_text('{"projects": "not-a-dict"}', encoding="utf-8")
        config_path.chmod(0o600)

        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "fail"
        assert "invalid structure" in result["message"]
        assert config is None


class TestDoctorServiceCheckConnectivity:
    """Tests for DoctorService._check_connectivity() - API connectivity checks."""

    def test_no_config_returns_skip(self, tmp_config_dir: Path) -> None:
        """When config is None, returns skip for connectivity."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        results = service._check_connectivity(None)

        assert len(results) == 1
        assert results[0]["check"] == "connectivity"
        assert results[0]["status"] == "skip"

    def test_successful_connectivity(self, tmp_config_dir: Path) -> None:
        """When API responds successfully, returns 'pass' with response time."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Production",
                project_id=1234,
            ),
        )
        config = store.load()

        mock_client = _make_mock_client(project_name="Production", project_id=1234)
        service = DoctorService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
            mcp_service=_make_mcp_service_mock(),
        )

        results = service._check_connectivity(config)

        assert len(results) == 1
        assert results[0]["check"] == "connectivity"
        assert results[0]["status"] == "pass"
        assert "Production" in results[0]["message"]
        assert "response_time_ms" in results[0]
        mock_client.close.assert_called_once()

    def test_connectivity_failure(self, tmp_config_dir: Path) -> None:
        """When API call fails, returns 'fail' with error details."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdef1234",
                project_name="Bad",
                project_id=9999,
            ),
        )
        config = store.load()

        error = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )
        fail_client = _make_failing_client(error)
        service = DoctorService(
            config_store=store,
            client_factory=lambda url, token: fail_client,
            mcp_service=_make_mcp_service_mock(),
        )

        results = service._check_connectivity(config)

        assert len(results) == 1
        assert results[0]["check"] == "connectivity"
        assert results[0]["status"] == "fail"
        assert "Invalid token" in results[0]["message"]
        assert results[0]["error_code"] == "INVALID_TOKEN"
        fail_client.close.assert_called_once()

    def test_multiple_projects_mixed(self, tmp_config_dir: Path) -> None:
        """With multiple projects, each gets its own connectivity check."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Production",
                project_id=1234,
            ),
        )
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdef1234",
                project_name="Bad",
                project_id=9999,
            ),
        )
        config = store.load()

        error = KeboolaApiError(
            message="Forbidden",
            status_code=403,
            error_code="ACCESS_DENIED",
            retryable=False,
        )

        def factory(url: str, token: str) -> MagicMock:
            if "badtoken" in token:
                return _make_failing_client(error)
            return _make_mock_client(project_name="Production", project_id=1234)

        service = DoctorService(
            config_store=store,
            client_factory=factory,
            mcp_service=_make_mcp_service_mock(),
        )

        results = service._check_connectivity(config)

        assert len(results) == 2
        statuses = {r["alias"]: r["status"] for r in results}
        assert statuses["prod"] == "pass"
        assert statuses["bad"] == "fail"


class TestDoctorServiceCheckVersion:
    """Tests for DoctorService._check_version() - CLI version check."""

    def test_version_check_passes(self, tmp_config_dir: Path) -> None:
        """Version check always returns 'pass' with version string."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_version()

        assert result["check"] == "version"
        assert result["status"] == "pass"
        assert "kbagent v" in result["message"]


class TestDoctorServiceCheckConversationId:
    """Tests for DoctorService._check_conversation_id()."""

    def test_conversation_id_warn_when_not_set(self, tmp_config_dir: Path, monkeypatch) -> None:
        """Warns when KBAGENT_CONVERSATION_ID is not set."""
        monkeypatch.delenv("KBAGENT_CONVERSATION_ID", raising=False)
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_conversation_id()

        assert result["check"] == "conversation_id"
        assert result["status"] == "warn"
        assert "not set" in result["message"]

    def test_conversation_id_pass_when_set(self, tmp_config_dir: Path, monkeypatch) -> None:
        """Passes when KBAGENT_CONVERSATION_ID is set."""
        monkeypatch.setenv("KBAGENT_CONVERSATION_ID", "test-conv-123")
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service._check_conversation_id()

        assert result["check"] == "conversation_id"
        assert result["status"] == "pass"
        assert "test-conv-123" in result["message"]


class TestDoctorServiceRunChecks:
    """Tests for DoctorService.run_checks() - full health check orchestration."""

    def test_run_checks_returns_all_checks_and_summary(self, tmp_config_dir: Path) -> None:
        """run_checks returns a complete structure with checks and summary."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service.run_checks()

        assert "checks" in result
        assert "summary" in result
        assert len(result["checks"]) >= 5  # source, file, valid, connectivity, version, mcp
        assert "total" in result["summary"]
        assert "passed" in result["summary"]
        assert "failed" in result["summary"]
        assert "warnings" in result["summary"]
        assert "skipped" in result["summary"]
        assert "healthy" in result["summary"]

    def test_run_checks_no_config_is_healthy(self, tmp_config_dir: Path) -> None:
        """With no config file, run_checks is still healthy (no failures)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store, mcp_service=_make_mcp_service_mock())

        result = service.run_checks()

        assert result["summary"]["healthy"] is True
        assert result["summary"]["failed"] == 0

    def test_run_checks_with_connectivity_failure_is_unhealthy(self, tmp_config_dir: Path) -> None:
        """When a connectivity check fails, healthy is False."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdef1234",
                project_name="Bad",
                project_id=9999,
            ),
        )

        error = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )
        fail_client = _make_failing_client(error)
        service = DoctorService(
            config_store=store,
            client_factory=lambda url, token: fail_client,
            mcp_service=_make_mcp_service_mock(),
        )

        result = service.run_checks()

        assert result["summary"]["healthy"] is False
        assert result["summary"]["failed"] >= 1

    def test_run_checks_includes_mcp_check(self, tmp_config_dir: Path) -> None:
        """run_checks includes the MCP server availability check."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_mcp = _make_mcp_service_mock(status="warn")
        service = DoctorService(config_store=store, mcp_service=mock_mcp)

        result = service.run_checks()

        mcp_checks = [c for c in result["checks"] if c["check"] == "mcp_server"]
        assert len(mcp_checks) == 1
        assert mcp_checks[0]["status"] == "warn"
        mock_mcp.check_server_available.assert_called_once()
