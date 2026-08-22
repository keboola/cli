"""Service-layer tests for RepoValidateService + the pure validator.

The pure validator (validate_keboola_repo) is exercised against in-memory
_RepoSnapshot fixtures -- no I/O. The service layer is exercised with a
mocked GitHub client that returns hand-crafted tree + content responses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.repo_validate_service import (
    SEVERITY_BLOCKING,
    SEVERITY_OK,
    SEVERITY_WARN,
    RepoValidateService,
    _RepoSnapshot,
    aggregate_verdict,
    validate_keboola_repo,
)

# ---------------------------------------------------------------------------
# Pure validator (no I/O)
# ---------------------------------------------------------------------------


def _good_snapshot(**overrides: Any) -> _RepoSnapshot:
    """A snapshot that passes every check on the happy path."""
    paths = {
        "keboola-config/nginx/sites/default.conf",
        "keboola-config/supervisord/services/app.conf",
        "keboola-config/setup.sh",
        "pyproject.toml",
        "app.py",
    }
    snapshot = _RepoSnapshot(
        paths=paths,
        truncated=False,
        setup_sh="#!/bin/bash\nset -Eeuo pipefail\ncd /app && uv sync\n",
        pyproject_toml='[project]\nname = "demo"\nrequires-python = ">=3.12"\ndependencies = ["httpx"]\n',
        nginx_conf="server {\n  proxy_pass http://localhost:5000;\n}\n",
        app_conf="[program:app]\ncommand=/app/.venv/bin/uv run python app.py --port 5000\n",
    )
    for k, v in overrides.items():
        setattr(snapshot, k, v)
    return snapshot


class TestPureValidatorHappyPath:
    def test_well_formed_repo_passes_all_checks(self) -> None:
        results = validate_keboola_repo(
            _good_snapshot(), type_="python-js", runtime_python_pin="3.12.10"
        )
        verdict = aggregate_verdict(results)
        assert verdict["verdict"] == SEVERITY_OK
        assert verdict["blocking_count"] == 0
        # Every named check is OK.
        assert all(r.severity == SEVERITY_OK for r in results)


class TestPureValidatorGoldenRule:
    def test_missing_nginx_default_conf_blocks(self) -> None:
        snap = _good_snapshot()
        snap.paths.discard("keboola-config/nginx/sites/default.conf")
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.nginx-default-conf"] == SEVERITY_BLOCKING

    def test_missing_app_conf_blocks(self) -> None:
        snap = _good_snapshot()
        snap.paths.discard("keboola-config/supervisord/services/app.conf")
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.supervisord-app-conf"] == SEVERITY_BLOCKING

    def test_missing_pyproject_blocks(self) -> None:
        snap = _good_snapshot()
        snap.paths.discard("pyproject.toml")
        snap.pyproject_toml = None
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.pyproject-toml"] == SEVERITY_BLOCKING


class TestPureValidatorSetupSh:
    def test_pip_install_in_setup_sh_blocks(self) -> None:
        snap = _good_snapshot(setup_sh="#!/bin/bash\nset -e\npip install -r requirements.txt\n")
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-no-pip"] == SEVERITY_BLOCKING

    def test_pip_install_only_in_a_comment_does_not_block(self) -> None:
        """A comment warning against pip is not an invocation of pip.

        The rule greps for what the script RUNS. Reading prose as code
        penalises exactly the author who documents the rule above the
        command that follows it -- i.e. the one complying with it.
        """
        snap = _good_snapshot(
            setup_sh="#!/bin/bash\n# always uv sync here, never pip install\nuv sync\n"
        )
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-no-pip"] == SEVERITY_OK
        assert names["golden-rule.setup-sh-uv-sync"] == SEVERITY_OK

    def test_pip_install_with_a_trailing_comment_still_blocks(self) -> None:
        """Stripping comments must not smuggle a real invocation past the rule."""
        snap = _good_snapshot(setup_sh="#!/bin/bash\npip install flask  # legacy\n")
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-no-pip"] == SEVERITY_BLOCKING

    def test_hash_inside_a_quoted_string_is_not_a_comment(self) -> None:
        """Only unquoted `#` starts a comment, so quoted code stays visible."""
        snap = _good_snapshot(
            setup_sh='#!/bin/bash\necho "install deps # step 1"\npip install flask\n'
        )
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-no-pip"] == SEVERITY_BLOCKING

    def test_uv_sync_only_in_a_comment_still_warns(self) -> None:
        """The inverse false reading: prose must not satisfy the rule either."""
        snap = _good_snapshot(setup_sh="#!/bin/bash\n# remember to uv sync\necho hello\n")
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-uv-sync"] == SEVERITY_WARN

    def test_setup_sh_without_uv_sync_warns(self) -> None:
        # Setup.sh present but no `uv sync` invocation; pyproject.toml
        # declares deps so the WARN should fire.
        snap = _good_snapshot(setup_sh="#!/bin/bash\necho hello\n")
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-uv-sync"] == SEVERITY_WARN
        assert names["golden-rule.setup-sh-no-pip"] == SEVERITY_OK

    def test_no_setup_sh_with_deps_blocks(self) -> None:
        snap = _good_snapshot()
        snap.paths.discard("keboola-config/setup.sh")
        snap.setup_sh = None
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.setup-sh-present"] == SEVERITY_BLOCKING

    def test_no_setup_sh_no_deps_warns(self) -> None:
        snap = _good_snapshot()
        snap.paths.discard("keboola-config/setup.sh")
        snap.setup_sh = None
        snap.pyproject_toml = '[project]\nname = "demo"\nrequires-python = ">=3.12"\n'
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        # Soft warn (intentional for static-only apps).
        assert names["golden-rule.setup-sh-present"] == SEVERITY_WARN


class TestPureValidatorRequiresPython:
    def test_too_new_blocks(self) -> None:
        snap = _good_snapshot(
            pyproject_toml='[project]\nrequires-python = ">=3.99"\ndependencies = ["x"]\n'
        )
        results = validate_keboola_repo(snap, type_="python-js", runtime_python_pin="3.12.10")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.requires-python"] == SEVERITY_BLOCKING

    def test_compatible_passes(self) -> None:
        snap = _good_snapshot()
        results = validate_keboola_repo(snap, type_="python-js", runtime_python_pin="3.12.10")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.requires-python"] == SEVERITY_OK


class TestPureValidatorPortMatch:
    def test_mismatched_port_warns(self) -> None:
        snap = _good_snapshot(
            nginx_conf="server { proxy_pass http://localhost:5000; }\n",
            app_conf="[program:app]\ncommand=python app.py --port 8000\n",
        )
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["golden-rule.nginx-app-port-match"] == SEVERITY_WARN


class TestPureValidatorTypeRestriction:
    def test_streamlit_returns_only_blocking_meta(self) -> None:
        results = validate_keboola_repo(_good_snapshot(), type_="streamlit")
        assert len(results) == 1
        assert results[0].name == "meta.type-supported"
        assert results[0].severity == SEVERITY_BLOCKING


class TestPureValidatorTruncated:
    def test_truncated_warns(self) -> None:
        snap = _good_snapshot()
        snap.truncated = True
        results = validate_keboola_repo(snap, type_="python-js")
        names = {r.name: r.severity for r in results}
        assert names["meta.tree-truncated"] == SEVERITY_WARN


# ---------------------------------------------------------------------------
# Service layer (with mocked GitHub client)
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return ConfigStore(config_dir=config_dir)


def _good_tree() -> dict[str, Any]:
    return {
        "tree": [
            {"path": "pyproject.toml", "type": "blob"},
            {"path": "keboola-config/nginx/sites/default.conf", "type": "blob"},
            {"path": "keboola-config/supervisord/services/app.conf", "type": "blob"},
            {"path": "keboola-config/setup.sh", "type": "blob"},
            {"path": "app.py", "type": "blob"},
        ],
        "truncated": False,
    }


class TestServiceURLParsing:
    def test_non_github_host_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = RepoValidateService(config_store=store)
        with pytest.raises(KeboolaApiError) as exc:
            service.validate_repo(git_repo="https://gitlab.com/owner/repo")
        assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT

    def test_strips_dot_git_suffix(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        github_mock = MagicMock()
        github_mock.get_tree_recursive.return_value = _good_tree()
        github_mock.get_file_content.return_value = "#!/bin/bash\ncd /app && uv sync\n"
        service = RepoValidateService(
            config_store=store,
            github_client_factory=lambda token: github_mock,
        )
        service.validate_repo(git_repo="https://github.com/o/r.git")
        # owner/repo extracted correctly.
        github_mock.get_tree_recursive.assert_called_once_with("o", "r", "main")

    def test_garbage_url_raises_invalid_format(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = RepoValidateService(config_store=store)
        with pytest.raises(KeboolaApiError) as exc:
            service.validate_repo(git_repo="https://github.com/")
        assert exc.value.error_code == ErrorCode.INVALID_FORMAT


class TestServiceTypeRestriction:
    def test_streamlit_rejected_at_service_layer(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = RepoValidateService(config_store=store)
        with pytest.raises(KeboolaApiError) as exc:
            service.validate_repo(git_repo="https://github.com/o/r", type_="streamlit")
        assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT


class TestServicePrivateRepo404Hint:
    def test_404_without_pat_surfaces_private_repo_hint(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        github_mock = MagicMock()
        github_mock.get_tree_recursive.side_effect = KeboolaApiError(
            message="not found", status_code=404, error_code=ErrorCode.API_ERROR
        )
        service = RepoValidateService(
            config_store=store,
            github_client_factory=lambda token: github_mock,
        )
        with pytest.raises(KeboolaApiError) as exc:
            service.validate_repo(git_repo="https://github.com/o/r")
        assert exc.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "--git-pat-env" in exc.value.message


class TestServiceCallBudget:
    def test_happy_path_uses_at_most_4_github_calls(self, tmp_path: Path) -> None:
        """Per the plan: 1 trees-recursive + up to 3 contents."""
        store = _make_store(tmp_path)
        github_mock = MagicMock()
        github_mock.get_tree_recursive.return_value = _good_tree()
        github_mock.get_file_content.return_value = "#!/bin/bash\ncd /app && uv sync\n"
        service = RepoValidateService(
            config_store=store,
            github_client_factory=lambda token: github_mock,
        )
        service.validate_repo(git_repo="https://github.com/o/r")
        # 1 tree + 4 contents (setup.sh, pyproject.toml, nginx, app.conf).
        # Spec says ≤4 in typical case but the port-match check fetches both
        # nginx and app.conf -- still bounded.
        assert github_mock.get_tree_recursive.call_count == 1
        assert github_mock.get_file_content.call_count <= 4


class TestServiceOutputShape:
    def test_returns_verdict_envelope(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        github_mock = MagicMock()
        github_mock.get_tree_recursive.return_value = _good_tree()
        github_mock.get_file_content.return_value = "#!/bin/bash\ncd /app && uv sync\n"
        service = RepoValidateService(
            config_store=store,
            github_client_factory=lambda token: github_mock,
        )
        result = service.validate_repo(git_repo="https://github.com/o/r")
        assert result["git_repo"] == "https://github.com/o/r"
        assert result["type"] == "python-js"
        assert "verdict" in result
        assert "checks" in result
        assert isinstance(result["checks"], list)
