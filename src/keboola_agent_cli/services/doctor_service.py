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
from typing import Any

from .. import __version__
from ..config_store import ConfigStore
from ..errors import KeboolaApiError
from ..models import AppConfig
from .base import ClientFactory, default_client_factory
from .mcp_service import McpService


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
        self._client_factory = client_factory or default_client_factory
        self._mcp_service = mcp_service or McpService(config_store)

    def run_checks(self) -> dict[str, Any]:
        """Run all health checks and return structured results.

        Returns:
            Dict with 'checks' list and 'summary' dict.
        """
        all_checks: list[dict[str, Any]] = []

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
