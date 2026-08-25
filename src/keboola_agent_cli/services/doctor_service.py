"""Doctor service - health check logic for CLI configuration and connectivity.

Runs checks for:
1. Config file existence and permissions (0600)
2. Config file valid JSON and parseable
3. Token verification for each project (API call with response time)
4. CLI version

Extracted from commands/doctor.py to respect the 3-layer architecture.
"""

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from .. import __version__
from ..config_store import ConfigStore
from ..constants import ENV_CONVERSATION_ID
from ..errors import KeboolaApiError
from ..models import AppConfig
from ..permissions import (
    INERT_PATTERN_HINT,
    INERT_PATTERN_PREFIX,
    INERT_SINCE_VERSION,
    UNMATCHED_PATTERN_HINT,
    find_inert_patterns,
)
from .base import ClientFactory, make_client_factory

# Cap on how many offending items a single check names inline; the rest are
# summarised as "+N more" and the full list travels in `details` for --json.
_MAX_LISTED_TASKS = 5
AGENTS_FILENAME = "agents.json"


class DoctorService:
    """Business logic for health checks.

    Accepts ConfigStore and client_factory via DI for easy testing with mocks.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or make_client_factory(config_store)

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

        # Check 5: Conversation ID
        conversation_check = self._check_conversation_id()
        all_checks.append(conversation_check)

        # Check 6: Claude Code plugin installation
        plugin_check = self._check_claude_plugin()
        all_checks.append(plugin_check)

        # Check 7: Plaintext #-secrets in synced configs (issue #378)
        sync_secret_check = self._check_sync_secrets()
        all_checks.append(sync_secret_check)

        # Check 8: scheduled tasks still using the removed mcp_tool action
        mcp_tool_task_check = self._check_mcp_tool_tasks()
        all_checks.append(mcp_tool_task_check)

        # Check 9: persisted permission patterns that can no longer match
        inert_patterns_check = self._check_inert_permission_patterns(config)
        all_checks.append(inert_patterns_check)

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
        """Check 7: flag plaintext ``#``-secrets in synced configs (issue #378).

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

    def _check_mcp_tool_tasks(self) -> dict[str, Any]:
        """Check 8: flag scheduled tasks that still use the REMOVED MCP passthrough.

        ``agent --type mcp_tool`` was removed in the version named by
        :data:`REMOVED_IN_VERSION` (epic #390 phase 3). Such a task still
        round-trips through ``agents.json`` -- so it is not silently deleted --
        but it no longer runs: every firing is persisted as an errored run.
        Because it runs unattended, nobody is present to see that; this check is
        the standing reminder in the one command people run when something feels
        off. Read-only: filesystem only, no API call.
        """
        # Local import: keeps the server package off the doctor cold-start path.
        from ..server.agents_store import REMOVED_IN_VERSION, AgentStore

        agents_path = self._config_store.config_dir / AGENTS_FILENAME
        if not agents_path.exists():
            return {
                "check": "mcp_tool_tasks",
                "name": "Removed mcp_tool agent tasks",
                "status": "skip",
                "message": f"No {AGENTS_FILENAME} in the config dir -- no agent tasks registered.",
            }

        # AgentStore.load_tasks() is deliberately forgiving -- it swallows a
        # corrupt file and an invalid entry alike and returns whatever it could
        # parse. That is right for the scheduler (one bad entry must not stop
        # the rest) and wrong for a health check: "0 tasks" from an unreadable
        # file would render as a clean bill of health for someone whose
        # soon-to-break tasks we simply could not see. So read the raw file
        # ourselves and report what we could NOT account for.
        try:
            raw = json.loads(agents_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "check": "mcp_tool_tasks",
                "name": "Removed mcp_tool agent tasks",
                "status": "warn",
                "message": (
                    f"Could not read {AGENTS_FILENAME} ({exc}), so tasks using the "
                    f"removed 'mcp_tool' action could not be checked. Fix the file to "
                    f"find out whether any need migrating."
                ),
            }
        if not isinstance(raw, list):
            return {
                "check": "mcp_tool_tasks",
                "name": "Removed mcp_tool agent tasks",
                "status": "warn",
                "message": (
                    f"{AGENTS_FILENAME} is not a JSON list, so it holds no usable tasks and "
                    f"none could be checked for the removed 'mcp_tool' action."
                ),
            }

        tasks = AgentStore(config_dir=self._config_store.config_dir).load_tasks()
        unreadable = len(raw) - len(tasks)

        affected = [t for t in tasks if getattr(t.action, "type", None) == "mcp_tool"]
        if not affected:
            if unreadable > 0:
                return {
                    "check": "mcp_tool_tasks",
                    "name": "Removed mcp_tool agent tasks",
                    "status": "warn",
                    "message": (
                        f"{unreadable} of {len(raw)} entries in {AGENTS_FILENAME} could not be "
                        f"parsed and were skipped, so they could not be checked for the "
                        f"removed 'mcp_tool' action. The {len(tasks)} readable task(s) are "
                        f"clean."
                    ),
                }
            return {
                "check": "mcp_tool_tasks",
                "name": "Removed mcp_tool agent tasks",
                "status": "pass",
                "message": f"No tasks use the removed 'mcp_tool' action ({len(tasks)} checked).",
            }

        shown = ", ".join(
            f"{t.id} ({t.action.params.get('tool', '?')})" for t in affected[:_MAX_LISTED_TASKS]
        )
        more = (
            f" (+{len(affected) - _MAX_LISTED_TASKS} more)"
            if len(affected) > _MAX_LISTED_TASKS
            else ""
        )
        return {
            "check": "mcp_tool_tasks",
            "name": "Removed mcp_tool agent tasks",
            "status": "fail",
            "message": (
                f"{len(affected)} scheduled task(s) use the REMOVED 'mcp_tool' action "
                f"(removed in v{REMOVED_IN_VERSION}) and NO LONGER RUN: {shown}{more}. "
                f"Recreate each with --type cli_command -- see docs/mcp-migration.md."
            ),
            "details": {
                "removed_in": REMOVED_IN_VERSION,
                "tasks": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "tool": t.action.params.get("tool"),
                        "cron": None if t.manual else t.cron,
                        "enabled": t.enabled,
                    }
                    for t in affected
                ],
            },
        }

    @staticmethod
    def _check_inert_permission_patterns(config: AppConfig | None) -> dict[str, Any]:
        """Check 9: flag persisted permission patterns that can never match.

        Generalized (issue #688): any pattern matching zero known operations is
        flagged, not only the retired ``tool:`` namespace -- ``permissions set``
        now rejects such patterns at write time, but a policy persisted before
        that gate existed can still carry dead rules of either kind. WARN rather
        than FAIL -- the policy is still enforced, it is just narrower than its
        author intended. Read-only: config.json only, no API call.

        ``details["inert_since"]`` is included ONLY when at least one offending
        pattern starts with ``tool:`` -- that key names the version the MCP
        passthrough (and with it the ``tool:`` namespace) was removed, which is
        meaningless context for an unrelated typo like ``stroage.upload-table``.
        """
        policy = config.permissions if config is not None else None
        if policy is None:
            return {
                "check": "inert_permission_patterns",
                "name": "Inert permission patterns",
                "status": "skip",
                "message": "No persisted permission policy in config.json.",
            }

        inert = find_inert_patterns(policy)
        if not inert:
            return {
                "check": "inert_permission_patterns",
                "name": "Inert permission patterns",
                "status": "pass",
                "message": "No inert patterns in the persisted permission policy.",
            }

        has_tool_prefix = any(p.startswith(INERT_PATTERN_PREFIX) for p in inert)
        has_other = any(not p.startswith(INERT_PATTERN_PREFIX) for p in inert)

        since_clause = (
            f" have been inert since v{INERT_SINCE_VERSION} (the 'tool:' namespace was "
            "removed with the MCP passthrough) and"
            if has_tool_prefix
            else ""
        )
        hints = [
            hint
            for hint, present in (
                (INERT_PATTERN_HINT, has_tool_prefix),
                (UNMATCHED_PATTERN_HINT, has_other),
            )
            if present
        ]

        details: dict[str, Any] = {"mode": policy.mode, "patterns": inert}
        if has_tool_prefix:
            details["inert_since"] = INERT_SINCE_VERSION

        return {
            "check": "inert_permission_patterns",
            "name": "Inert permission patterns",
            "status": "warn",
            "message": (
                f"{len(inert)} pattern(s) in the persisted permission policy{since_clause} "
                f"match no known operation: {', '.join(inert)}. {' '.join(hints)}"
            ),
            "details": details,
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

        # POSIX only, and enforced as such -- the comment used to say "Unix only"
        # while the code ran everywhere. Windows reports 0o666 for any writable
        # file no matter how the ACL actually reads, so this check warned every
        # Windows user about permissions they had no way to "fix". Access there
        # is governed by the profile ACL, not by mode bits; same reasoning as
        # `auth/state_store.py::_fix_permissions_if_needed`.
        if os.name == "nt":
            return {
                "check": "config_file",
                "name": "Config file",
                "status": "pass",
                "message": f"Config file exists at {config_path} (POSIX mode bits not checked on Windows).",
            }

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
        """Check 5: Conversation ID env var is set.

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
        """Check 6: Claude Code plugin installation.

        Detects whether the kbagent Claude Code plugin is installed under
        ``~/.claude/plugins/cache/``, from either marketplace: the current
        one (``keboola-claude-kit``, published from keboola/ai-kit) or this
        repo's deprecated ``keboola-agent-cli`` shim. Emits a 'skip' if
        Claude Code is not detected at all on the host; 'warn' with
        copy-pasteable install commands if Claude Code is present but the
        plugin is missing; 'pass' with the installed version otherwise --
        plus a reinstall-from-ai-kit note when the copy came from the shim.

        With BOTH marketplaces installed, every cached version dir under both
        is considered and the NEWEST one wins by PEP 440 ordering, whichever
        marketplace it sits under. The reported version, path, marketplace
        name, drift hint and migration note therefore all describe the same
        copy -- a newest-copy-under-the-shim install gets the migration note,
        and the drift hint names the marketplace it should be updated from.

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

        # Claude Code caches each plugin version under its own subdir
        # (~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/), so the
        # marketplace the user installed FROM is visible in the path. Two
        # marketplaces can ship this plugin: the current one
        # (`keboola-claude-kit`, published from keboola/ai-kit) and the legacy
        # one (`keboola-agent-cli`, this repo's own deprecated shim). An install
        # from the shim still works and still passes, it just earns a migration
        # note.
        #
        # Collect version dirs from EVERY marketplace dir that exists rather than
        # stopping at the first one that holds any: a user who installed from both
        # keeps two independent copies, and the one worth reporting is the NEWEST,
        # whichever marketplace it came from. Short-circuiting on the first hit
        # meant that while ai-kit trails a cli release -- with a newer copy under
        # the legacy dir -- doctor reported the OLDER `keboola-claude-kit` copy,
        # suppressed the migration note, and aimed the drift hint at a path the
        # user was not actually running.
        cache_root = claude_home / "plugins" / "cache"
        marketplaces: tuple[tuple[str, bool], ...] = (
            ("keboola-claude-kit", False),
            ("keboola-agent-cli", True),
        )
        # (version dir, came-from-the-legacy-marketplace)
        candidates: list[tuple[Path, bool]] = []
        for marketplace, is_legacy in marketplaces:
            plugin_root = cache_root / marketplace / "kbagent"
            if not plugin_root.is_dir():
                continue
            candidates.extend((p, is_legacy) for p in plugin_root.iterdir() if p.is_dir())

        if not candidates:
            return {
                "check": "claude_plugin",
                "name": "Claude Code plugin",
                "status": "warn",
                "message": (
                    "kbagent Claude Code plugin not installed. In Claude Code, run:\n"
                    "  /plugin marketplace add keboola/ai-kit\n"
                    "  /plugin install kbagent@keboola-claude-kit\n"
                    "This enables the /keboola slash command and the "
                    "keboola-expert specialist subagent."
                ),
            }

        # Newest cached version across every marketplace dir, so the reported
        # version, the path, the drift hint and the migration note all describe
        # the SAME copy. Ordered by PEP 440 (`packaging.version`, as
        # version_service does) rather than by dir name: a plain string compare
        # sorts "0.100.0" below "0.90.0", which would report a stale copy as the
        # newest exactly once the minor version rolls past 99. Unparseable names
        # sort below every real version but stay eligible, so a hand-made dir
        # never hides a real install. Ties -- the same version cached under both
        # marketplaces -- resolve to the current marketplace: same code, no
        # reason to nag about the shim.
        latest, from_legacy_marketplace = max(
            candidates,
            key=lambda c: (DoctorService._plugin_version_sort_key(c[0].name), not c[1]),
        )
        plugin_version = latest.name
        manifest = latest / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                plugin_version = data.get("version", plugin_version)
            except (OSError, json.JSONDecodeError):
                pass

        # cache/<marketplace>/kbagent/<version> -- name the marketplace the
        # REPORTED copy came from, so a user with both installs can tell which of
        # the two the rest of this message is talking about.
        marketplace_name = latest.parent.parent.name

        cli_version = __version__
        # Qualify the update command with the marketplace of the reported copy:
        # with both marketplaces installed a bare `/plugin update kbagent` is
        # ambiguous, and the whole point of the hint is to name the copy that is
        # actually behind.
        drift = (
            ""
            if plugin_version == cli_version
            else (
                f" (CLI is v{cli_version} -- run "
                f"`/plugin update kbagent@{marketplace_name}` in Claude Code to sync)"
            )
        )
        # Installed from this repo's deprecated marketplace shim: still a pass (it
        # keeps working and keeps updating), but the shim's entry goes away in a few
        # releases, so say so now while there is time to move.
        migration = (
            ""
            if not from_legacy_marketplace
            else (
                " -- installed from the deprecated keboola-agent-cli marketplace; "
                "reinstall from keboola/ai-kit: /plugin marketplace add keboola/ai-kit "
                "then /plugin install kbagent@keboola-claude-kit"
            )
        )
        return {
            "check": "claude_plugin",
            "name": "Claude Code plugin",
            "status": "pass",
            "message": (
                f"kbagent plugin v{plugin_version} installed at {latest} "
                f"(from the {marketplace_name} marketplace){drift}{migration}"
            ),
            "plugin_path": str(latest),
            "plugin_version": plugin_version,
            "plugin_marketplace": marketplace_name,
        }

    @staticmethod
    def _plugin_version_sort_key(name: str) -> tuple[int, Version | None, str]:
        """PEP 440 sort key for a Claude Code plugin cache dir name.

        Claude Code names each cached version dir after the version it holds, so
        picking the newest install means ordering those names. Plain string
        ordering gets it wrong across a digit-count change ("0.100.0" < "0.90.0"),
        which matters as soon as two marketplaces are compared against each other.

        Returns:
            ``(1, Version, "")`` for a PEP 440-parseable name, ordering by
            version; ``(0, None, name)`` for anything else, which sorts below
            every real version but is still eligible to be picked (a lone
            unparseable dir is better reported than treated as no install).
        """
        try:
            return (1, Version(name.lstrip("v")), "")
        except InvalidVersion:
            return (0, None, name)
