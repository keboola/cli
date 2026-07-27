"""Doctor service - health check logic for CLI configuration and connectivity.

Runs checks for:
1. Config file existence and permissions (0600)
2. Config file valid JSON and parseable
3. Token verification for each project (API call with response time)
4. CLI version
5. MCP server availability

Extracted from commands/doctor.py to respect the 3-layer architecture.
"""

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..config_store import ConfigStore
from ..constants import ENV_CONVERSATION_ID
from ..errors import KeboolaApiError
from ..models import AppConfig
from .base import ClientFactory, make_client_factory
from .mcp_service import McpService, ensure_mcp_installed


class DoctorService:
    """Business logic for health checks.

    Accepts ConfigStore, client_factory, and McpService via DI
    for easy testing with mocks.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        mcp_service: McpService | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or make_client_factory(config_store)
        self._mcp_service = mcp_service or McpService(config_store)

    def run_checks(self) -> dict[str, Any]:
        """Run all health checks and return structured results.

        Returns:
            Dict with 'checks' list and 'summary' dict.
        """
        all_checks: list[dict[str, Any]] = []

        # Check 0: Config source (local vs global)
        source_check = self._check_config_source()
        all_checks.append(source_check)

        # Check 1: Config file exists with correct permissions
        file_check = self._check_config_file()
        all_checks.append(file_check)

        # Check 2: Config file is valid JSON and parseable
        valid_check, config = self._check_config_valid()
        all_checks.append(valid_check)

        # Check 3: Project connectivity
        connectivity_checks = self._check_connectivity(config)
        all_checks.extend(connectivity_checks)

        # Check 4: CLI version
        version_check = self._check_version()
        all_checks.append(version_check)

        # Check 5: MCP server availability
        mcp_check = self._mcp_service.check_server_available()
        all_checks.append(mcp_check)

        # Check 6: Conversation ID
        conversation_check = self._check_conversation_id()
        all_checks.append(conversation_check)

        # Check 7: Claude Code plugin installation
        plugin_check = self._check_claude_plugin()
        all_checks.append(plugin_check)

        # Check 8: Plaintext #-secrets in synced configs (issue #378)
        sync_secret_check = self._check_sync_secrets()
        all_checks.append(sync_secret_check)

        # Build summary
        total = len(all_checks)
        passed = sum(1 for c in all_checks if c["status"] == "pass")
        failed = sum(1 for c in all_checks if c["status"] == "fail")
        warnings = sum(1 for c in all_checks if c["status"] == "warn")
        skipped = sum(1 for c in all_checks if c["status"] == "skip")

        return {
            "checks": all_checks,
            "summary": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "warnings": warnings,
                "skipped": skipped,
                "healthy": failed == 0,
            },
        }

    def _check_sync_secrets(self) -> dict[str, Any]:
        """Check 8: flag plaintext ``#``-secrets in synced configs (issue #378).

        Only meaningful inside a sync working tree (a directory containing
        ``.keboola/manifest.json``); skipped otherwise. Read-only -- filesystem
        and manifest only, no API call. An in-sync config whose ``#``-secret is
        still plaintext means the remote holds it unencrypted.
        """
        # Local import: sync_service is heavy and most doctor runs are not in a
        # sync tree, so keep it off the cold-start path.
        from .sync_service import scan_synced_plaintext_secrets

        cwd = Path.cwd()
        if not (cwd / ".keboola" / "manifest.json").exists():
            return {
                "check": "sync_secrets",
                "name": "Synced config secrets",
                "status": "skip",
                "message": "Not a sync working tree (no .keboola/manifest.json in current dir).",
            }

        try:
            warnings = scan_synced_plaintext_secrets(cwd)
        except Exception as exc:
            return {
                "check": "sync_secrets",
                "name": "Synced config secrets",
                "status": "warn",
                "message": f"Could not scan synced configs for plaintext secrets: {exc}",
            }

        if not warnings:
            return {
                "check": "sync_secrets",
                "name": "Synced config secrets",
                "status": "pass",
                "message": "No plaintext #-secrets in synced configs.",
            }

        shown = ", ".join(w["path"] for w in warnings[:5])
        more = f" (+{len(warnings) - 5} more)" if len(warnings) > 5 else ""
        return {
            "check": "sync_secrets",
            "name": "Synced config secrets",
            "status": "warn",
            "message": (
                f"{len(warnings)} synced config(s) hold #-secrets in PLAINTEXT (issue #378): "
                f"{shown}{more}. Re-push on kbagent >=0.54.0 to encrypt, then ROTATE the "
                f"credential -- config version history keeps the old plaintext."
            ),
        }

    def _check_config_source(self) -> dict[str, Any]:
        """Check 0: Report which config source is active."""
        return {
            "check": "config_source",
            "name": "Config source",
            "status": "pass",
            "message": f"Using {self._config_store.source} config at {self._config_store.config_path}",
        }

    def _check_config_file(self) -> dict[str, Any]:
        """Check 1: Config file exists and has correct permissions (0600).

        Returns:
            Dict with check name, status (pass/fail/warn), and message.
        """
        config_path = self._config_store.config_path

        if not config_path.exists():
            return {
                "check": "config_file",
                "name": "Config file",
                "status": "warn",
                "message": f"Config file not found at {config_path}. Run 'kbagent project add' to create it.",
            }

        # Check permissions (Unix only)
        try:
            file_stat = os.stat(config_path)
            mode = stat.S_IMODE(file_stat.st_mode)
            if mode != 0o600:
                return {
                    "check": "config_file",
                    "name": "Config file",
                    "status": "warn",
                    "message": f"Config file exists at {config_path} but has permissions {oct(mode)} (expected 0o600).",
                }
        except OSError:
            # On platforms where permission checking is not reliable
            pass

        return {
            "check": "config_file",
            "name": "Config file",
            "status": "pass",
            "message": f"Config file exists at {config_path} with correct permissions.",
        }

    def _check_config_valid(self) -> tuple[dict[str, Any], AppConfig | None]:
        """Check 2: Config file is valid JSON and parseable.

        Returns:
            Tuple of (check result dict, parsed AppConfig or None on failure).
        """
        config_path = self._config_store.config_path

        if not config_path.exists():
            return {
                "check": "config_valid",
                "name": "Config parseable",
                "status": "skip",
                "message": "No config file to validate.",
            }, None

        try:
            raw = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "check": "config_valid",
                "name": "Config parseable",
                "status": "fail",
                "message": f"Cannot read config file: {exc}",
            }, None

        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            return {
                "check": "config_valid",
                "name": "Config parseable",
                "status": "fail",
                "message": f"Config file is not valid JSON: {exc}",
            }, None

        try:
            config = self._config_store.load()
        except Exception as exc:
            return {
                "check": "config_valid",
                "name": "Config parseable",
                "status": "fail",
                "message": f"Config file has invalid structure: {exc}",
            }, None

        project_count = len(config.projects)
        return {
            "check": "config_valid",
            "name": "Config parseable",
            "status": "pass",
            "message": f"Config file is valid JSON with {project_count} project(s).",
        }, config

    def _check_connectivity(
        self,
        config: AppConfig | None,
    ) -> list[dict[str, Any]]:
        """Check 3: For each project, verify token via API call.

        Returns:
            List of check result dicts, one per project.
        """
        if config is None or not config.projects:
            return [
                {
                    "check": "connectivity",
                    "name": "Project connectivity",
                    "status": "skip",
                    "message": "No projects configured.",
                }
            ]

        results = []
        for alias, project in config.projects.items():
            client = self._client_factory(project.stack_url, project.token)
            start_time = time.monotonic()
            try:
                token_info = client.verify_token()
                elapsed = time.monotonic() - start_time
                results.append(
                    {
                        "check": "connectivity",
                        "name": f"Project '{alias}'",
                        "status": "pass",
                        "message": (
                            f"Connected to {project.stack_url} "
                            f"(project: {token_info.project_name}, id: {token_info.project_id}) "
                            f"in {round(elapsed * 1000)}ms"
                        ),
                        "alias": alias,
                        "response_time_ms": round(elapsed * 1000),
                    }
                )
            except KeboolaApiError as exc:
                elapsed = time.monotonic() - start_time
                results.append(
                    {
                        "check": "connectivity",
                        "name": f"Project '{alias}'",
                        "status": "fail",
                        "message": f"Failed: {exc.message}",
                        "alias": alias,
                        "error_code": exc.error_code,
                        "response_time_ms": round(elapsed * 1000),
                    }
                )
            finally:
                client.close()

        return results

    @staticmethod
    def _check_version() -> dict[str, Any]:
        """Check 4: CLI version information.

        Returns:
            Check result dict with the current CLI version.
        """
        return {
            "check": "version",
            "name": "CLI version",
            "status": "pass",
            "message": f"kbagent v{__version__}",
        }

    @staticmethod
    def _check_conversation_id() -> dict[str, Any]:
        """Check 6: Conversation ID env var is set.

        Returns:
            Check result dict with warn if not set, pass if set.
        """
        conversation_id = os.environ.get(ENV_CONVERSATION_ID, "")
        if not conversation_id:
            return {
                "check": "conversation_id",
                "name": "Conversation ID",
                "status": "warn",
                "message": (
                    f"{ENV_CONVERSATION_ID} not set. "
                    "API requests will not include X-Conversation-ID header."
                ),
            }
        return {
            "check": "conversation_id",
            "name": "Conversation ID",
            "status": "pass",
            "message": f"X-Conversation-ID: {conversation_id}",
        }

    @staticmethod
    def _check_claude_plugin() -> dict[str, Any]:
        """Check 7: Claude Code plugin installation.

        Detects whether the kbagent Claude Code plugin (this repo's plugin
        marketplace entry) is installed under ``~/.claude/plugins/cache/``.
        Emits a 'skip' if Claude Code is not detected at all on the host;
        'warn' with copy-pasteable install commands if Claude Code is
        present but the plugin is missing; 'pass' with the installed
        version otherwise.

        Intentionally does NOT auto-fix: Claude Code's plugin install flow
        goes through the user's in-session ``/plugin`` commands, which a
        background CLI cannot invoke. The most we can do is surface the
        gap and show the exact commands to run.
        """
        claude_home = Path.home() / ".claude"
        if not claude_home.is_dir():
            return {
                "check": "claude_plugin",
                "name": "Claude Code plugin",
                "status": "skip",
                "message": (
                    "Claude Code not detected (~/.claude/ absent). "
                    "Install instructions: https://github.com/keboola/cli#claude-code-plugin"
                ),
            }

        plugin_root = claude_home / "plugins" / "cache" / "keboola-agent-cli" / "kbagent"
        # Claude Code caches each plugin version under its own subdir
        # (~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/).
        version_dirs: list[Path] = []
        if plugin_root.is_dir():
            version_dirs = [p for p in plugin_root.iterdir() if p.is_dir()]

        if not version_dirs:
            return {
                "check": "claude_plugin",
                "name": "Claude Code plugin",
                "status": "warn",
                "message": (
                    "kbagent Claude Code plugin not installed. In Claude Code, run:\n"
                    "  /plugin marketplace add keboola/cli\n"
                    "  /plugin install kbagent@keboola-agent-cli\n"
                    "This enables the /keboola slash command and the "
                    "keboola-expert specialist subagent."
                ),
            }

        # Take the newest version dir name as the installed version.
        # Fallback to manifest lookup if the dir name is not parseable.
        latest = max(version_dirs, key=lambda p: p.name)
        plugin_version = latest.name
        manifest = latest / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                plugin_version = data.get("version", plugin_version)
            except (OSError, json.JSONDecodeError):
                pass

        cli_version = __version__
        drift = (
            ""
            if plugin_version == cli_version
            else f" (CLI is v{cli_version} -- run `/plugin update kbagent` in Claude Code to sync)"
        )
        return {
            "check": "claude_plugin",
            "name": "Claude Code plugin",
            "status": "pass",
            "message": f"kbagent plugin v{plugin_version} installed at {latest}{drift}",
            "plugin_path": str(latest),
            "plugin_version": plugin_version,
        }

    @staticmethod
    def warmup() -> dict[str, Any]:
        """Ensure MCP server is installed as a fast local binary.

        If only uvx fallback is available, installs via `uv tool install`
        to create a permanent binary with faster startup (~1s vs ~4.5s).

        Returns:
            Dict with installation result info.
        """
        return ensure_mcp_installed()
