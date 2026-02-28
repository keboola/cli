"""Tests for Pydantic models serialization and deserialization."""

import json

import pytest
from pydantic import ValidationError

from keboola_agent_cli.models import (
    AppConfig,
    ErrorResponse,
    ProjectConfig,
    SuccessResponse,
    TokenVerifyResponse,
)


class TestProjectConfig:
    """Tests for ProjectConfig model."""

    def test_create_with_all_fields(self) -> None:
        """ProjectConfig can be created with all fields specified."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-secret-token",
            project_name="My Project",
            project_id=1234,
        )
        assert config.stack_url == "https://connection.keboola.com"
        assert config.token == "901-secret-token"
        assert config.project_name == "My Project"
        assert config.project_id == 1234

    def test_default_values(self) -> None:
        """ProjectConfig has sensible defaults for optional fields."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-token",
        )
        assert config.project_name == ""
        assert config.project_id is None

    def test_json_round_trip(self) -> None:
        """ProjectConfig can be serialized to JSON and deserialized back."""
        original = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-secret-token",
            project_name="My Project",
            project_id=1234,
        )
        json_str = original.model_dump_json()
        restored = ProjectConfig.model_validate_json(json_str)
        assert restored == original

    def test_json_output_is_valid(self) -> None:
        """ProjectConfig JSON output is valid JSON."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="token",
        )
        json_str = config.model_dump_json()
        parsed = json.loads(json_str)
        assert "stack_url" in parsed
        assert "token" in parsed


class TestAppConfig:
    """Tests for AppConfig model."""

    def test_empty_config(self) -> None:
        """AppConfig can be created with defaults (no projects)."""
        config = AppConfig()
        assert config.version == 1
        assert config.default_project == ""
        assert config.projects == {}

    def test_config_with_projects(self) -> None:
        """AppConfig can hold multiple project connections."""
        config = AppConfig(
            version=1,
            default_project="prod-aws",
            projects={
                "prod-aws": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-token",
                    project_name="Production",
                    project_id=1001,
                ),
                "dev-azure": ProjectConfig(
                    stack_url="https://connection.north-europe.azure.keboola.com",
                    token="532-token",
                    project_name="Development",
                    project_id=2002,
                ),
            },
        )
        assert len(config.projects) == 2
        assert "prod-aws" in config.projects
        assert "dev-azure" in config.projects
        assert config.projects["prod-aws"].project_id == 1001

    def test_json_round_trip(self) -> None:
        """AppConfig can be serialized to JSON and deserialized back."""
        original = AppConfig(
            version=1,
            default_project="test",
            projects={
                "test": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-token",
                    project_name="Test",
                    project_id=999,
                ),
            },
        )
        json_str = original.model_dump_json()
        restored = AppConfig.model_validate_json(json_str)
        assert restored == original
        assert restored.projects["test"].project_name == "Test"

    def test_json_output_structure(self) -> None:
        """AppConfig JSON output has the expected top-level keys."""
        config = AppConfig(
            version=1,
            default_project="prod",
            projects={
                "prod": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="t",
                ),
            },
        )
        parsed = json.loads(config.model_dump_json())
        assert parsed["version"] == 1
        assert parsed["default_project"] == "prod"
        assert "prod" in parsed["projects"]
        assert parsed["projects"]["prod"]["stack_url"] == "https://connection.keboola.com"


class TestErrorResponse:
    """Tests for ErrorResponse model."""

    def test_create(self) -> None:
        """ErrorResponse can be created with all fields."""
        err = ErrorResponse(
            code="INVALID_TOKEN",
            message="Token is invalid or expired",
            project="prod-aws",
            retryable=False,
        )
        assert err.code == "INVALID_TOKEN"
        assert err.message == "Token is invalid or expired"
        assert err.project == "prod-aws"
        assert err.retryable is False

    def test_defaults(self) -> None:
        """ErrorResponse has empty project and retryable=False by default."""
        err = ErrorResponse(code="ERR", message="Something failed")
        assert err.project == ""
        assert err.retryable is False

    def test_json_serialization(self) -> None:
        """ErrorResponse serializes to valid JSON with expected keys."""
        err = ErrorResponse(
            code="NETWORK_ERROR",
            message="Connection timed out",
            project="dev",
            retryable=True,
        )
        parsed = json.loads(err.model_dump_json())
        assert parsed["code"] == "NETWORK_ERROR"
        assert parsed["retryable"] is True


class TestSuccessResponse:
    """Tests for SuccessResponse model."""

    def test_with_list_data(self) -> None:
        """SuccessResponse can hold a list as data payload."""
        resp = SuccessResponse(status="ok", data=[{"name": "item1"}, {"name": "item2"}])
        assert resp.status == "ok"
        assert len(resp.data) == 2

    def test_with_empty_data(self) -> None:
        """SuccessResponse can hold None or empty data."""
        resp = SuccessResponse()
        assert resp.status == "ok"
        assert resp.data is None

    def test_json_serialization(self) -> None:
        """SuccessResponse serializes with status and data keys."""
        resp = SuccessResponse(status="ok", data={"message": "done"})
        parsed = json.loads(resp.model_dump_json())
        assert parsed["status"] == "ok"
        assert parsed["data"]["message"] == "done"

    def test_json_round_trip(self) -> None:
        """SuccessResponse can be round-tripped through JSON."""
        original = SuccessResponse(status="ok", data=["a", "b", "c"])
        json_str = original.model_dump_json()
        restored = SuccessResponse.model_validate_json(json_str)
        assert restored == original


class TestStackUrlValidation:
    """Tests for S2: URL validation on ProjectConfig.stack_url."""

    def test_project_add_rejects_http_url(self) -> None:
        """http:// URL is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="http://connection.keboola.com",
                token="901-token",
            )

    def test_project_add_rejects_file_url(self) -> None:
        """file:// URL is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="file:///etc/passwd",
                token="901-token",
            )

    def test_project_add_rejects_ftp_url(self) -> None:
        """ftp:// URL is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="ftp://connection.keboola.com",
                token="901-token",
            )

    def test_project_add_rejects_no_scheme(self) -> None:
        """URL without scheme is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="connection.keboola.com",
                token="901-token",
            )

    def test_project_add_accepts_https_url(self) -> None:
        """https:// URL is accepted without error."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.keboola.com"

    def test_project_add_accepts_https_azure(self) -> None:
        """https:// Azure stack URL is accepted."""
        config = ProjectConfig(
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.north-europe.azure.keboola.com"

    def test_project_add_accepts_https_gcp(self) -> None:
        """https:// GCP stack URL is accepted."""
        config = ProjectConfig(
            stack_url="https://connection.europe-west3.gcp.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.europe-west3.gcp.keboola.com"


class TestTokenVerifyResponseValidation:
    """Tests for Phase 6: TokenVerifyResponse required fields and project_id default."""

    def test_token_verify_response_rejects_missing_fields(self) -> None:
        """TokenVerifyResponse with missing required fields raises ValidationError."""
        with pytest.raises(ValidationError):
            TokenVerifyResponse(
                token_id="123",
                token_description="My Token",
                # project_name missing
                # owner_name missing
            )

    def test_token_verify_response_rejects_missing_owner_name(self) -> None:
        """TokenVerifyResponse with missing owner_name raises ValidationError."""
        with pytest.raises(ValidationError, match="owner_name"):
            TokenVerifyResponse(
                token_id="123",
                token_description="My Token",
                project_name="Test Project",
                # owner_name missing
            )

    def test_token_verify_response_rejects_missing_token_id(self) -> None:
        """TokenVerifyResponse with missing token_id raises ValidationError."""
        with pytest.raises(ValidationError, match="token_id"):
            TokenVerifyResponse(
                token_description="My Token",
                project_name="Test Project",
                owner_name="Test Owner",
            )

    def test_token_verify_response_rejects_missing_token_description(self) -> None:
        """TokenVerifyResponse with missing token_description raises ValidationError."""
        with pytest.raises(ValidationError, match="token_description"):
            TokenVerifyResponse(
                token_id="123",
                project_name="Test Project",
                owner_name="Test Owner",
            )

    def test_token_verify_response_rejects_missing_project_name(self) -> None:
        """TokenVerifyResponse with missing project_name raises ValidationError."""
        with pytest.raises(ValidationError, match="project_name"):
            TokenVerifyResponse(
                token_id="123",
                token_description="My Token",
                owner_name="Test Owner",
            )

    def test_project_id_default_none(self) -> None:
        """TokenVerifyResponse project_id defaults to None, not 0."""
        response = TokenVerifyResponse(
            token_id="123",
            token_description="My Token",
            project_name="Test Project",
            owner_name="Test Owner",
        )
        assert response.project_id is None

    def test_token_verify_response_with_all_fields(self) -> None:
        """TokenVerifyResponse with all fields specified works correctly."""
        response = TokenVerifyResponse(
            token_id="123",
            token_description="My Token",
            project_id=4567,
            project_name="Test Project",
            owner_name="Test Owner",
        )
        assert response.token_id == "123"
        assert response.token_description == "My Token"
        assert response.project_id == 4567
        assert response.project_name == "Test Project"
        assert response.owner_name == "Test Owner"


class TestMaxParallelWorkersValidation:
    """Tests for max_parallel_workers upper bound validation."""

    def test_max_workers_upper_bound(self) -> None:
        """max_parallel_workers > 100 raises ValidationError."""
        with pytest.raises(ValidationError, match="less than or equal to 100"):
            AppConfig(max_parallel_workers=200)

    def test_max_workers_at_100_is_valid(self) -> None:
        """max_parallel_workers = 100 is accepted."""
        config = AppConfig(max_parallel_workers=100)
        assert config.max_parallel_workers == 100

    def test_max_workers_default_is_valid(self) -> None:
        """Default max_parallel_workers (10) is accepted."""
        config = AppConfig()
        assert config.max_parallel_workers == 10

    def test_max_workers_at_1_is_valid(self) -> None:
        """max_parallel_workers = 1 is accepted."""
        config = AppConfig(max_parallel_workers=1)
        assert config.max_parallel_workers == 1
