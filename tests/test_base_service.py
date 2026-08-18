"""Tests for BaseService - shared infrastructure for multi-project parallel operations.

Tests resolve_projects(), _resolve_max_workers(), and _run_parallel() via a
concrete _TestService subclass, since BaseService is not meant to be
instantiated directly in production code.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import UNEXPECTED_ERROR_MAX_MESSAGE_LEN
from keboola_agent_cli.errors import (
    ConfigError,
    ErrorCode,
    KeboolaApiError,
    SessionAuthUnsupportedError,
)
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.base import (
    ENV_MAX_PARALLEL_WORKERS,
    UNEXPECTED_ERROR_CODE,
    BaseService,
    project_error_entry,
)


class _TestService(BaseService):
    """Concrete subclass of BaseService used exclusively for testing."""

    pass


def _setup_three_projects(tmp_config_dir: Path) -> ConfigStore:
    """Create a ConfigStore with three projects (prod, dev, staging) configured."""
    store = setup_two_projects(tmp_config_dir)
    store.add_project(
        "staging",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="5555-zzz",
            project_name="Staging",
            project_id=5555,
        ),
    )
    return store


class TestResolveProjects:
    """Tests for the inherited resolve_projects() method via a concrete subclass."""

    def test_resolve_all_projects_with_none_aliases(self, tmp_config_dir: Path) -> None:
        """Passing aliases=None returns all configured projects."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)

        resolved = service.resolve_projects(aliases=None)

        assert set(resolved.keys()) == {"prod", "dev"}
        assert resolved["prod"].project_id == 258
        assert resolved["dev"].project_id == 7012

    def test_resolve_specific_aliases(self, tmp_config_dir: Path) -> None:
        """Passing a list of aliases returns only those projects."""
        store = _setup_three_projects(tmp_config_dir)
        service = _TestService(config_store=store)

        resolved = service.resolve_projects(aliases=["prod", "staging"])

        assert set(resolved.keys()) == {"prod", "staging"}
        assert resolved["prod"].project_id == 258
        assert resolved["staging"].project_id == 5555
        assert "dev" not in resolved

    def test_unknown_alias_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Passing an alias not in the config raises ConfigError."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)

        with pytest.raises(ConfigError, match="Project 'nonexistent' not found"):
            service.resolve_projects(aliases=["nonexistent"])

    def test_mixed_valid_and_unknown_alias_raises(self, tmp_config_dir: Path) -> None:
        """If any alias in the list is unknown, ConfigError is raised."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)

        with pytest.raises(ConfigError, match="Project 'missing' not found"):
            service.resolve_projects(aliases=["prod", "missing"])

    def test_empty_aliases_list_returns_all(self, tmp_config_dir: Path) -> None:
        """Passing an empty list returns all configured projects (same as None)."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)

        resolved = service.resolve_projects(aliases=[])

        assert set(resolved.keys()) == {"prod", "dev"}

    def test_resolve_single_alias(self, tmp_config_dir: Path) -> None:
        """Resolving a single valid alias returns just that project."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)

        resolved = service.resolve_projects(aliases=["dev"])

        assert list(resolved.keys()) == ["dev"]
        assert resolved["dev"].project_name == "Development"

    def test_resolve_projects_preserves_config(self, tmp_config_dir: Path) -> None:
        """Resolved ProjectConfig instances have the correct stack_url and token."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)

        resolved = service.resolve_projects(aliases=["prod"])

        assert resolved["prod"].stack_url == "https://connection.keboola.com"
        assert resolved["prod"].token == "901-xxx"


class TestResolveMaxWorkers:
    """Tests for _resolve_max_workers()."""

    def test_default_value_from_app_config(self, tmp_config_dir: Path) -> None:
        """Default max_parallel_workers is 10 from AppConfig."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 10

    def test_custom_value_from_config_json(self, tmp_config_dir: Path) -> None:
        """max_parallel_workers can be set in config.json."""
        store = setup_single_project(tmp_config_dir)
        config = store.load()
        config.max_parallel_workers = 20
        store.save(config)

        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 20

    def test_env_var_overrides_config(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KBAGENT_MAX_PARALLEL_WORKERS env var overrides config.json value."""
        store = setup_single_project(tmp_config_dir)
        config = store.load()
        config.max_parallel_workers = 5
        store.save(config)

        monkeypatch.setenv(ENV_MAX_PARALLEL_WORKERS, "25")
        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 25

    def test_invalid_env_var_falls_back_to_config(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid (non-numeric) env var value falls back to config.json."""
        store = setup_single_project(tmp_config_dir)
        config = store.load()
        config.max_parallel_workers = 15
        store.save(config)

        monkeypatch.setenv(ENV_MAX_PARALLEL_WORKERS, "not-a-number")
        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 15

    def test_zero_env_var_falls_back_to_config(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero is not a valid positive integer, so it falls back to config.json."""
        store = setup_single_project(tmp_config_dir)
        config = store.load()
        config.max_parallel_workers = 8
        store.save(config)

        monkeypatch.setenv(ENV_MAX_PARALLEL_WORKERS, "0")
        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 8

    def test_negative_env_var_falls_back_to_config(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative env var value is not positive, so it falls back to config.json."""
        store = setup_single_project(tmp_config_dir)
        config = store.load()
        config.max_parallel_workers = 12
        store.save(config)

        monkeypatch.setenv(ENV_MAX_PARALLEL_WORKERS, "-3")
        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 12

    def test_env_var_with_value_one(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var set to 1 is valid and should be used (sequential execution)."""
        store = setup_single_project(tmp_config_dir)

        monkeypatch.setenv(ENV_MAX_PARALLEL_WORKERS, "1")
        service = _TestService(config_store=store)

        assert service._resolve_max_workers() == 1


class TestRunParallel:
    """Tests for _run_parallel()."""

    def test_empty_projects_returns_empty_tuple(self, tmp_config_dir: Path) -> None:
        """Empty projects dict returns ([], []) without spawning threads."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            raise AssertionError("Worker should not be called for empty projects")

        successes, errors = service._run_parallel({}, worker)

        assert successes == []
        assert errors == []

    def test_single_project_success_three_tuple(self, tmp_config_dir: Path) -> None:
        """Worker returning a 3-tuple is classified as success."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return (alias, "some_data", {"key": "value"})

        successes, errors = service._run_parallel(projects, worker)

        assert len(successes) == 1
        assert successes[0] == ("prod", "some_data", {"key": "value"})
        assert errors == []

    def test_multiple_projects_success(self, tmp_config_dir: Path) -> None:
        """All workers succeeding with 3-tuples are collected in successes."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return (alias, project.project_id, "ok")

        successes, errors = service._run_parallel(projects, worker)

        assert len(successes) == 2
        assert errors == []

        # Verify both projects are present (order may vary due to parallel execution)
        aliases = {s[0] for s in successes}
        assert aliases == {"prod", "dev"}

        project_ids = {s[1] for s in successes}
        assert project_ids == {258, 7012}

    def test_worker_returning_two_tuple_classified_as_error(self, tmp_config_dir: Path) -> None:
        """Worker returning a 2-tuple (alias, error_dict) is classified as error."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        error_dict = {
            "project_alias": "prod",
            "error_code": "ACCESS_DENIED",
            "message": "Forbidden",
        }

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return (alias, error_dict)

        successes, errors = service._run_parallel(projects, worker)

        assert successes == []
        assert len(errors) == 1
        assert errors[0] == error_dict

    def test_mixed_success_and_error(self, tmp_config_dir: Path) -> None:
        """One worker returns success (3-tuple), another returns error (2-tuple)."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        error_dict = {
            "project_alias": "prod",
            "error_code": "API_ERROR",
            "message": "Something went wrong",
        }

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            if alias == "prod":
                return (alias, error_dict)
            return (alias, project.project_id, "success")

        successes, errors = service._run_parallel(projects, worker)

        assert len(successes) == 1
        assert successes[0][0] == "dev"
        assert successes[0][1] == 7012

        assert len(errors) == 1
        assert errors[0] == error_dict

    def test_unexpected_exception_caught_as_unexpected_error(self, tmp_config_dir: Path) -> None:
        """Exception raised by worker is caught and recorded as UNEXPECTED_ERROR."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            raise RuntimeError("connection pool exhausted")

        successes, errors = service._run_parallel(projects, worker)

        assert successes == []
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "prod"
        assert errors[0]["error_code"] == "UNEXPECTED_ERROR"
        assert "connection pool exhausted" in errors[0]["message"]

    def test_exception_mixed_with_success(self, tmp_config_dir: Path) -> None:
        """One worker raises, the other succeeds. Both results are collected."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            if alias == "prod":
                raise ValueError("token expired")
            return (alias, project.project_id, "ok")

        successes, errors = service._run_parallel(projects, worker)

        assert len(successes) == 1
        assert successes[0][0] == "dev"

        assert len(errors) == 1
        assert errors[0]["project_alias"] == "prod"
        assert errors[0]["error_code"] == "UNEXPECTED_ERROR"
        assert "token expired" in errors[0]["message"]

    def test_max_workers_capped_at_number_of_projects(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_workers is min(len(projects), _resolve_max_workers())."""
        store = setup_two_projects(tmp_config_dir)

        # Set a very high max_workers via env var
        monkeypatch.setenv(ENV_MAX_PARALLEL_WORKERS, "100")
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        call_count = 0

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            nonlocal call_count
            call_count += 1
            return (alias, "ok", call_count)

        successes, errors = service._run_parallel(projects, worker)

        # Both projects should still be processed
        assert len(successes) == 2
        assert errors == []
        assert call_count == 2

    def test_four_plus_tuple_classified_as_success(self, tmp_config_dir: Path) -> None:
        """Worker returning a 4-tuple is also classified as success (len > 2)."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return (alias, "data1", "data2", "data3")

        successes, errors = service._run_parallel(projects, worker)

        assert len(successes) == 1
        assert successes[0] == ("prod", "data1", "data2", "data3")
        assert errors == []

    def test_all_projects_fail_with_exceptions(self, tmp_config_dir: Path) -> None:
        """When all workers raise exceptions, successes is empty and all are errors."""
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            raise ConnectionError(f"cannot reach {alias}")

        successes, errors = service._run_parallel(projects, worker)

        assert successes == []
        assert len(errors) == 2
        aliases = {e["project_alias"] for e in errors}
        assert aliases == {"prod", "dev"}
        for err in errors:
            assert err["error_code"] == "UNEXPECTED_ERROR"
            assert "cannot reach" in err["message"]


class TestProjectErrorEntry:
    """Tests for project_error_entry() -- the per-project error envelope filler."""

    def test_typed_error_keeps_its_code(self) -> None:
        exc = KeboolaApiError("Forbidden", status_code=403, error_code=ErrorCode.ACCESS_DENIED)
        entry = project_error_entry("prod", exc)

        assert entry == {
            "project_alias": "prod",
            "error_code": "ACCESS_DENIED",
            "message": "Forbidden",
        }

    def test_session_guard_code_survives(self) -> None:
        """A --json consumer branches on this code to offer a static-token remedy."""
        exc = SessionAuthUnsupportedError("The Data Science Service (data apps)")
        entry = project_error_entry("session", exc)

        assert entry["error_code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert "static Storage token" in entry["message"]

    def test_uncoded_exception_uses_fallback(self) -> None:
        entry = project_error_entry("prod", RuntimeError("pool exhausted"))

        assert entry["error_code"] == UNEXPECTED_ERROR_CODE
        assert entry["message"] == "pool exhausted"

    def test_explicit_fallback_code_is_honoured(self) -> None:
        entry = project_error_entry("prod", RuntimeError("stdio closed"), fallback_code="API_ERROR")

        assert entry["error_code"] == "API_ERROR"

    def test_explicit_message_overrides_the_exception_text(self) -> None:
        exc = SessionAuthUnsupportedError("The Keboola AI Service")
        entry = project_error_entry("session", exc, message="Failed to list schemas: nope")

        assert entry["error_code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert entry["message"] == "Failed to list schemas: nope"

    def test_uncoded_message_is_truncated(self) -> None:
        """An unknown exception may embed response buffers, so its text is capped."""
        entry = project_error_entry("prod", RuntimeError("x" * 5000))

        assert len(entry["message"]) == UNEXPECTED_ERROR_MAX_MESSAGE_LEN + len("...")
        assert entry["message"].endswith("...")

    def test_typed_message_is_not_truncated(self) -> None:
        """A typed error's message is curated remedy text -- it must arrive whole."""
        long_remedy = "y" * 5000
        exc = KeboolaApiError(long_remedy, error_code=ErrorCode.VALIDATION_ERROR)
        entry = project_error_entry("prod", exc)

        assert entry["message"] == long_remedy

    def test_error_code_serialises_as_a_plain_string(self) -> None:
        """The wire format must stay a JSON string, not an enum repr."""
        exc = SessionAuthUnsupportedError("The Keboola AI Service")
        payload = json.loads(json.dumps([project_error_entry("session", exc)]))

        assert payload[0]["error_code"] == "AUTH_NOT_SUPPORTED_ON_STACK"

    def test_fallback_code_is_an_enum_member_that_serialises_unchanged(self) -> None:
        """The two most-emitted per-project codes are `ErrorCode` members, not bare
        literals -- but `StrEnum` means the `--json` wire value is byte-identical,
        so no consumer branching on the string sees a change."""
        assert UNEXPECTED_ERROR_CODE is ErrorCode.UNEXPECTED_ERROR
        payload = json.loads(json.dumps([project_error_entry("prod", RuntimeError("boom"))]))

        assert payload[0]["error_code"] == "UNEXPECTED_ERROR"

    def test_enum_fallback_code_serialises_unchanged(self) -> None:
        """An explicit `ErrorCode` member fallback reaches the wire as a plain string."""
        entry = project_error_entry(
            "prod", RuntimeError("connection reset"), fallback_code=ErrorCode.API_ERROR
        )
        payload = json.loads(json.dumps([entry]))

        assert payload[0]["error_code"] == "API_ERROR"


class TestRunParallelPreservesErrorCodes:
    """_run_parallel()'s catch-all must not relabel a typed exception."""

    def test_session_guard_from_worker_keeps_its_code(self, tmp_config_dir: Path) -> None:
        store = setup_two_projects(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            if alias == "prod":
                raise SessionAuthUnsupportedError("The Data Science Service (data apps)")
            return (alias, project.project_id, "ok")

        successes, errors = service._run_parallel(projects, worker)

        assert [s[0] for s in successes] == ["dev"]
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "prod"
        assert errors[0]["error_code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK

    def test_api_error_from_worker_keeps_its_code(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store)
        projects = service.resolve_projects()

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            raise KeboolaApiError(
                "Bucket not found", status_code=404, error_code=ErrorCode.NOT_FOUND
            )

        successes, errors = service._run_parallel(projects, worker)

        assert successes == []
        assert errors[0]["error_code"] == ErrorCode.NOT_FOUND
        assert errors[0]["message"] == "Bucket not found"


class TestDefaultClientFactory:
    """Tests for default_client_factory and constructor defaults."""

    def test_default_client_factory_is_used_when_none(self, tmp_config_dir: Path) -> None:
        """When client_factory is None, default_client_factory is assigned."""
        store = setup_single_project(tmp_config_dir)
        service = _TestService(config_store=store, client_factory=None)

        # The internal _client_factory should be callable
        assert callable(service._client_factory)

    def test_custom_client_factory_is_used(self, tmp_config_dir: Path) -> None:
        """When a custom client_factory is provided, it is stored and used."""
        store = setup_single_project(tmp_config_dir)
        mock_factory = MagicMock()
        service = _TestService(config_store=store, client_factory=mock_factory)

        assert service._client_factory is mock_factory
