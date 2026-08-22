"""CLI tests for ``kbagent config new --push`` (one-shot remote create).

Scope:
- Flag-combination validation (exit 2 on misuse).
- Push-mode happy paths: minimal, --no-files, --output-dir + push, --dry-run.
- Body parsing: --configuration (inline / @file / -), --configuration-file.
- Validation propagation: service-layer ConfigError surfaces as exit 5.

Regression coverage for scaffold-only mode lives in ``test_component_cli.py``
(TestConfigNew) -- preserved byte-for-byte.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from typer.testing import CliRunner, Result

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.sync_service import SyncService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="Production",
            project_id=1234,
        ),
    )
    return store


def _push_result() -> dict:
    """Canonical success envelope from ConfigService.create_config."""
    return {
        "id": "12345",
        "name": "test-config",
        "description": "",
        "configuration": {},
        "version": 1,
        "created": "2026-05-11T10:00:00+00:00",
        "project_alias": "prod",
        "branch_id": None,
        "validation_status": "skipped",
    }


def _invoke_push(
    args: list[str],
    *,
    config_service_mock: MagicMock | None = None,
    component_service_mock: MagicMock | None = None,
    sync_service_mock: MagicMock | None = None,
    config_dir: Path | None = None,
    input_text: str | None = None,
) -> Result:
    """Invoke the CLI with both service mocks installed."""
    assert config_dir is not None, "tests must pass a config_dir"
    store = _setup_config(config_dir)

    svc_config = config_service_mock or MagicMock()
    svc_component = component_service_mock or MagicMock()
    if not config_service_mock:
        svc_config.create_config.return_value = _push_result()
    if not component_service_mock:
        # Default scaffold response for paths that also exercise scaffold step.
        svc_component.generate_scaffold.return_value = {
            "component_id": "keboola.ex-http",
            "component_name": "HTTP",
            "component_type": "extractor",
            "directory": "extractor/keboola.ex-http/test-config",
            "files": [
                {"path": "_config.yml", "content": "name: test\n"},
            ],
        }

    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        patch("keboola_agent_cli.cli.ConfigService") as MockConfigService,
        patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockCompService.return_value = svc_component
        MockConfigService.return_value = svc_config
        MockSyncService.return_value = sync_service_mock or SyncService(config_store=store)
        return runner.invoke(app, args, input=input_text)


# ---------------------------------------------------------------------------
# Flag-combination validation
# ---------------------------------------------------------------------------


class TestConfigNewFlagValidation:
    """Push-gated flags must reject misuse with exit 2."""

    def test_push_requires_project(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--name",
                "T1",
                "--push",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert "requires --project" in envelope["error"]["message"]

    def test_push_requires_non_empty_name(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--push",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output
        assert "requires a non-empty --name" in json.loads(result.output)["error"]["message"]

    def test_no_files_without_push(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--no-files",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output
        assert "--no-files requires --push" in json.loads(result.output)["error"]["message"]

    def test_description_without_push(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--description",
                "desc",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output

    def test_branch_without_push(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--branch",
                "42",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output

    def test_dry_run_without_push(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--dry-run",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output

    def test_no_validate_without_push(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--no-validate",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output

    def test_configuration_and_configuration_file_mutually_exclusive(self, tmp_path: Path) -> None:
        body_file = tmp_path / "body.json"
        body_file.write_text("{}")
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "T1",
                "--push",
                "--configuration",
                "{}",
                "--configuration-file",
                str(body_file),
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output
        assert "mutually exclusive" in json.loads(result.output)["error"]["message"]

    def test_no_files_and_output_dir_mutually_exclusive(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "T1",
                "--push",
                "--no-files",
                "--output-dir",
                str(tmp_path / "scaffold"),
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Push-mode happy paths
# ---------------------------------------------------------------------------


class TestConfigNewPushHappyPath:
    def test_push_no_files_json(self, tmp_path: Path) -> None:
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["status"] == "ok"
        data = envelope["data"]
        assert data["id"] == "12345"
        assert data["project_alias"] == "prod"
        assert data["validation_status"] == "skipped"

        # Verify the service was called with validate=True and an empty body.
        svc_config.create_config.assert_called_once()
        call_kwargs = svc_config.create_config.call_args.kwargs
        assert call_kwargs["alias"] == "prod"
        assert call_kwargs["component_id"] == "keboola.ex-http"
        assert call_kwargs["name"] == "test-config"
        assert call_kwargs["configuration"] is None
        assert call_kwargs["validate"] is True
        assert call_kwargs["dry_run"] is False

    def test_push_with_output_dir_scaffolds_and_posts(self, tmp_path: Path) -> None:
        """With --push --output-dir: scaffold to disk AND POST."""
        scaffold_dir = tmp_path / "scaffold"
        scaffold_dir.mkdir()
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()
        svc_component = MagicMock()
        svc_component.generate_scaffold.return_value = {
            "component_id": "keboola.ex-http",
            "component_name": "HTTP",
            "component_type": "extractor",
            "directory": "extractor/keboola.ex-http/test-config",
            "files": [{"path": "_config.yml", "content": "name: test\n"}],
        }

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--output-dir",
                str(scaffold_dir),
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
            component_service_mock=svc_component,
        )

        assert result.exit_code == 0, result.output
        svc_config.create_config.assert_called_once()
        # The scaffold file should have been written.
        written = scaffold_dir / "extractor/keboola.ex-http/test-config/_config.yml"
        assert written.exists(), f"Scaffold file not written at {written}"
        # JSON mode must emit a SINGLE valid JSON document on stdout; the
        # scaffold "Written ..." dim line must not leak above it (B-2 regression
        # fix: _write_scaffold_to_disk now honors formatter.json_mode).
        json.loads(result.output)

    def test_push_output_dir_dry_run_does_not_write_files(self, tmp_path: Path) -> None:
        """--push --output-dir --dry-run must NOT write scaffold to disk.

        Dry-run is a preview; it must have zero filesystem side effects
        regardless of which flags accompany it. Regression guard for NB-1
        from the PR #282 review: the scaffold-write branch at config.py:1338
        previously had no `and not dry_run` clause, so dry-run silently
        created files alongside the preview envelope.
        """
        scaffold_dir = tmp_path / "scaffold"
        scaffold_dir.mkdir()
        svc_config = MagicMock()
        svc_config.create_config.return_value = {
            "dry_run": True,
            "would_post": {"name": "test-config", "configuration": {}},
            "validation_status": "skipped",
            "validation_errors": [],
            "project_alias": "prod",
            "branch_id": None,
        }
        svc_component = MagicMock()
        svc_component.generate_scaffold.return_value = {
            "component_id": "keboola.ex-http",
            "component_name": "HTTP",
            "component_type": "extractor",
            "directory": "extractor/keboola.ex-http/test-config",
            "files": [{"path": "_config.yml", "content": "name: test\n"}],
        }

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--output-dir",
                str(scaffold_dir),
                "--dry-run",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
            component_service_mock=svc_component,
        )

        assert result.exit_code == 0, result.output
        # Envelope must reflect dry-run (the formatter wraps the payload under
        # ``data``).
        envelope = json.loads(result.output)
        assert envelope["data"]["dry_run"] is True, envelope
        # No scaffold files anywhere under output_dir.
        written_files = list(scaffold_dir.rglob("*.yml")) + list(scaffold_dir.rglob("*.json"))
        assert written_files == [], f"Dry-run must not write files; found {written_files}"

    def test_push_with_configuration_inline(self, tmp_path: Path) -> None:
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--configuration",
                '{"parameters":{"url":"https://api.example.com"}}',
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 0, result.output
        call_kwargs = svc_config.create_config.call_args.kwargs
        assert call_kwargs["configuration"] == {"parameters": {"url": "https://api.example.com"}}

    def test_push_with_configuration_at_file(self, tmp_path: Path) -> None:
        body_file = tmp_path / "body.json"
        body_file.write_text('{"parameters": {"k": 1}}', encoding="utf-8")
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--configuration",
                f"@{body_file}",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 0, result.output
        assert svc_config.create_config.call_args.kwargs["configuration"] == {
            "parameters": {"k": 1}
        }

    def test_push_with_configuration_stdin(self, tmp_path: Path) -> None:
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--configuration",
                "-",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
            input_text='{"parameters":{"k":42}}',
        )

        assert result.exit_code == 0, result.output
        assert svc_config.create_config.call_args.kwargs["configuration"] == {
            "parameters": {"k": 42}
        }

    def test_push_with_configuration_file_typed_path(self, tmp_path: Path) -> None:
        body_file = tmp_path / "body.json"
        body_file.write_text('{"parameters": {"k": "v"}}', encoding="utf-8")
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--configuration-file",
                str(body_file),
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 0, result.output
        assert svc_config.create_config.call_args.kwargs["configuration"] == {
            "parameters": {"k": "v"}
        }

    def test_push_no_validate_passed_through(self, tmp_path: Path) -> None:
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--no-validate",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 0, result.output
        assert svc_config.create_config.call_args.kwargs["validate"] is False

    def test_push_dry_run_returns_envelope(self, tmp_path: Path) -> None:
        svc_config = MagicMock()
        svc_config.create_config.return_value = {
            "dry_run": True,
            "project_alias": "prod",
            "component_id": "keboola.ex-http",
            "name": "test-config",
            "description": "",
            "configuration": {},
            "branch_id": None,
            "validation_status": "skipped",
            "validation_errors": [],
        }

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--dry-run",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["data"]["dry_run"] is True
        assert envelope["data"]["validation_status"] == "skipped"
        assert svc_config.create_config.call_args.kwargs["dry_run"] is True


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestConfigNewPushErrors:
    def test_invalid_inline_json_exits_2(self, tmp_path: Path) -> None:
        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--configuration",
                "not-json",
            ],
            config_dir=tmp_path / "config",
        )
        assert result.exit_code == 2, result.output

    def test_validation_failure_surfaces_as_exit_5(self, tmp_path: Path) -> None:
        """ConfigError from the service => exit 5 + CONFIG_ERROR envelope."""
        svc_config = MagicMock()
        svc_config.create_config.side_effect = ConfigError(
            "Configuration body failed schema validation for 'X':\n  - foo: bar"
        )

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
                "--configuration",
                '{"bad":"body"}',
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        assert result.exit_code == 5, result.output
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert "schema validation" in envelope["error"]["message"]

    def test_api_error_surfaces_with_mapped_exit_code(self, tmp_path: Path) -> None:
        svc_config = MagicMock()
        svc_config.create_config.side_effect = KeboolaApiError(
            message="500 boom", error_code="STORAGE_ERROR", status_code=500
        )

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--no-files",
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
        )

        # STORAGE_ERROR is not in the explicit "network/auth/timeout" mapping
        # in map_error_to_exit_code(), so it falls through to the general
        # error bucket (exit 1). Tighten the assertion so any future change
        # to the mapping is caught.
        assert result.exit_code == 1, result.output
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"


# ---------------------------------------------------------------------------
# Scaffold stamping + branch-aware placement (issue #644)
# ---------------------------------------------------------------------------

_REALISTIC_SCAFFOLD = {
    "component_id": "keboola.ex-http",
    "component_name": "HTTP",
    "component_type": "extractor",
    "directory": "extractor/keboola.ex-http/test-config",
    "files": [
        {
            "path": "_config.yml",
            "content": (
                "# NOTE: config_id will be assigned by Keboola on first push\n"
                "version: 2\n"
                'name: "test-config"\n'
                "parameters: {}\n"
                "\n"
                "_keboola:\n"
                "  component_id: keboola.ex-http\n"
            ),
        },
    ],
}


def _write_manifest(output_dir: Path, branches: list[dict]) -> None:
    """Materialize a minimal .keboola/manifest.json in *output_dir*."""
    keboola_dir = output_dir / ".keboola"
    keboola_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 2,
        "project": {"id": 1234, "apiHost": "connection.keboola.com"},
        "naming": {},
        "branches": branches,
        "configurations": [],
    }
    (keboola_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestConfigNewPushScaffoldStamping:
    """--push --output-dir must write a scaffold that carries the created
    config's identity; otherwise the next ``sync push`` duplicates it
    (issue #644)."""

    def _component_mock(self) -> MagicMock:
        svc = MagicMock()
        svc.generate_scaffold.return_value = json.loads(json.dumps(_REALISTIC_SCAFFOLD))
        return svc

    def _run(self, tmp_path: Path, out_dir: Path, extra: list[str] | None = None, **kwargs):
        args = [
            "--json",
            "config",
            "new",
            "--component-id",
            "keboola.ex-http",
            "--project",
            "prod",
            "--name",
            "test-config",
            "--push",
            "--output-dir",
            str(out_dir),
        ] + (extra or [])
        return _invoke_push(args, config_dir=tmp_path / "config", **kwargs)

    def test_written_scaffold_carries_created_config_id(self, tmp_path: Path) -> None:
        out = tmp_path / "ws"
        out.mkdir()
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = self._run(
            tmp_path,
            out,
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
        )

        assert result.exit_code == 0, result.output
        written = out / "extractor/keboola.ex-http/test-config/_config.yml"
        parsed = yaml.safe_load(written.read_text(encoding="utf-8"))
        assert parsed["_keboola"]["config_id"] == "12345"
        assert isinstance(parsed["_keboola"]["config_id"], str)
        assert "assigned by Keboola on first push" not in written.read_text(encoding="utf-8")

    def test_envelope_reports_local_scaffold(self, tmp_path: Path) -> None:
        out = tmp_path / "ws"
        out.mkdir()
        svc_config = MagicMock()
        svc_config.create_config.return_value = _push_result()

        result = self._run(
            tmp_path,
            out,
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
        )

        envelope = json.loads(result.output)
        scaffold_info = envelope["data"]["local_scaffold"]
        assert scaffold_info["files"] == ["_config.yml"]
        # Path comparison, not str.endswith -- Windows renders backslashes.
        assert Path(scaffold_info["directory"]) == out / "extractor/keboola.ex-http/test-config"

    def test_branch_create_writes_into_registered_branch_dir(self, tmp_path: Path) -> None:
        """A config created in a dev branch must scaffold into that branch's
        subtree, never into the default branch's (main/) tree."""
        out = tmp_path / "ws"
        _write_manifest(out, [{"id": 10, "path": "main"}, {"id": 20, "path": "dev-x"}])
        svc_config = MagicMock()
        svc_config.create_config.return_value = {**_push_result(), "branch_id": 20}

        result = self._run(
            tmp_path,
            out,
            extra=["--branch", "20"],
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
        )

        assert result.exit_code == 0, result.output
        expected = out / "dev-x/extractor/keboola.ex-http/test-config/_config.yml"
        assert expected.exists(), (
            f"expected scaffold under dev-x/, tree: {list(out.rglob('_config.yml'))}"
        )
        assert not (out / "main/extractor").exists(), "must not write into the default branch tree"

    def test_branch_unknown_to_manifest_gets_registered(self, tmp_path: Path) -> None:
        from keboola_agent_cli.sync.branch_registry import ScaffoldPlacement

        out = tmp_path / "ws"
        _write_manifest(out, [{"id": 10, "path": "main"}])
        svc_config = MagicMock()
        svc_config.create_config.return_value = {**_push_result(), "branch_id": 20}
        sync_mock = MagicMock()
        sync_mock.resolve_scaffold_placement.return_value = ScaffoldPlacement("issue-branch")

        result = self._run(
            tmp_path,
            out,
            extra=["--branch", "20"],
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
            sync_service_mock=sync_mock,
        )

        assert result.exit_code == 0, result.output
        sync_mock.resolve_scaffold_placement.assert_called_once()
        kwargs = sync_mock.resolve_scaffold_placement.call_args.kwargs
        assert kwargs.get("branch_id") == 20
        expected = out / "issue-branch/extractor/keboola.ex-http/test-config/_config.yml"
        assert expected.exists(), f"tree: {list(out.rglob('_config.yml'))}"

    def test_placement_warning_surfaces_in_envelope(self, tmp_path: Path) -> None:
        """A degraded placement (registration failed -> branch-{id}/) must be
        honored AND surfaced -- never silently retargeted to the default
        tree, which is the exact duplicate factory this fix removes."""
        from keboola_agent_cli.sync.branch_registry import ScaffoldPlacement

        out = tmp_path / "ws"
        _write_manifest(out, [{"id": 10, "path": "main"}])
        svc_config = MagicMock()
        svc_config.create_config.return_value = {**_push_result(), "branch_id": 20}
        sync_mock = MagicMock()
        sync_mock.resolve_scaffold_placement.return_value = ScaffoldPlacement(
            "branch-20", "registration failed; reconcile with sync pull"
        )

        result = self._run(
            tmp_path,
            out,
            extra=["--branch", "20"],
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
            sync_service_mock=sync_mock,
        )

        assert result.exit_code == 0, result.output
        expected = out / "branch-20/extractor/keboola.ex-http/test-config/_config.yml"
        assert expected.exists(), f"tree: {list(out.rglob('_config.yml'))}"
        assert not (out / "main/extractor").exists()
        envelope = json.loads(result.output)
        assert envelope["data"]["warnings"] == ["registration failed; reconcile with sync pull"]

    def test_branch_without_manifest_stays_flat(self, tmp_path: Path) -> None:
        """No sync workspace in output_dir -> flat layout (placement None)."""
        from keboola_agent_cli.sync.branch_registry import ScaffoldPlacement

        out = tmp_path / "plain"
        out.mkdir()
        svc_config = MagicMock()
        svc_config.create_config.return_value = {**_push_result(), "branch_id": 20}
        sync_mock = MagicMock()
        sync_mock.resolve_scaffold_placement.return_value = ScaffoldPlacement(None)

        result = self._run(
            tmp_path,
            out,
            extra=["--branch", "20"],
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
            sync_service_mock=sync_mock,
        )

        assert result.exit_code == 0, result.output
        expected = out / "extractor/keboola.ex-http/test-config/_config.yml"
        assert expected.exists(), f"tree: {list(out.rglob('_config.yml'))}"

    def test_pushed_body_is_mirrored_not_placeholder(self, tmp_path: Path) -> None:
        """--configuration: the local file mirrors the pushed (already
        encrypted) body; placeholder scaffolding would make the next push
        overwrite the real remote config with TODO templates."""
        out = tmp_path / "ws"
        out.mkdir()
        svc_config = MagicMock()
        svc_config.create_config.return_value = {
            **_push_result(),
            "configuration": {"parameters": {"baseUrl": "https://real.example.com"}},
        }

        result = self._run(
            tmp_path,
            out,
            extra=["--configuration", '{"parameters": {"baseUrl": "https://real.example.com"}}'],
            config_service_mock=svc_config,
            component_service_mock=self._component_mock(),
        )

        assert result.exit_code == 0, result.output
        written = out / "extractor/keboola.ex-http/test-config/_config.yml"
        parsed = yaml.safe_load(written.read_text(encoding="utf-8"))
        assert parsed["parameters"] == {"baseUrl": "https://real.example.com"}
        assert parsed["_keboola"]["config_id"] == "12345"
        assert "TODO" not in written.read_text(encoding="utf-8")

    def test_pushed_transformation_body_extracts_code_through_cli(self, tmp_path: Path) -> None:
        """End-to-end through the Typer command: a pushed SQL body yields a
        real transform.sql next to _config.yml (PR #653 review sweep -- the
        mirror branch was previously only unit-tested)."""
        out = tmp_path / "ws"
        out.mkdir()
        body = {
            "parameters": {
                "blocks": [{"name": "Blocks", "codes": [{"name": "Code", "script": ["SELECT 1;"]}]}]
            }
        }
        svc_config = MagicMock()
        svc_config.create_config.return_value = {**_push_result(), "configuration": body}
        svc_component = MagicMock()
        svc_component.generate_scaffold.return_value = {
            **json.loads(json.dumps(_REALISTIC_SCAFFOLD)),
            "component_id": "keboola.snowflake-transformation",
            "directory": "transformation/keboola.snowflake-transformation/test-config",
        }

        result = _invoke_push(
            [
                "--json",
                "config",
                "new",
                "--component-id",
                "keboola.snowflake-transformation",
                "--project",
                "prod",
                "--name",
                "test-config",
                "--push",
                "--output-dir",
                str(out),
                "--configuration",
                json.dumps(body),
            ],
            config_dir=tmp_path / "config",
            config_service_mock=svc_config,
            component_service_mock=svc_component,
        )

        assert result.exit_code == 0, result.output
        base = out / "transformation/keboola.snowflake-transformation/test-config"
        assert (base / "transform.sql").is_file(), list(out.rglob("*"))
        assert "SELECT 1;" in (base / "transform.sql").read_text(encoding="utf-8")
        envelope = json.loads(result.output)
        assert sorted(envelope["data"]["local_scaffold"]["files"]) == [
            "_config.yml",
            "transform.sql",
        ]
