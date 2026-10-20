"""The environment variables that kbagent reads (spec rule EV1).

One entry per variable: what it does, whether its value is a secret, the first
release that read it, and the option it sets, if any. ``make cli-spec-check``
(rule ``env.registry-complete``) fails when ``src/`` reads a variable that is
not listed here, or when an entry names a variable that nothing reads. The
generated command reference (``scripts/gen_command_reference.py``) prints the
table.

A variable that sets an option is declared on that option with ``envvar=``, so
``--help`` names it. Precedence is flag > environment variable > saved choice >
default (EV3). Help output never shows the value of a variable.

``added_in`` is the first release tag that contains the commit that added the
read. ``library`` names a dependency that reads the variable in the kbagent
process (kbagent itself does not). A name with ``<ALIAS>`` is a family: the
placeholder is a project alias in upper case, with ``-`` replaced by ``_``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVar:
    """One environment variable that kbagent reads."""

    name: str
    purpose: str
    secret: bool
    added_in: str
    option: str = ""
    library: str = ""


ENV_VARS: tuple[EnvVar, ...] = (
    # Configuration and target selection
    EnvVar(
        "KBAGENT_CONFIG_DIR",
        "Configuration directory when --config-dir is not given.",
        secret=False,
        added_in="0.7.0",
        option="--config-dir",
    ),
    EnvVar(
        "KBAGENT_PROJECT",
        "Project alias for a command without --project; wins over the active project.",
        secret=False,
        added_in="0.22.0",
    ),
    EnvVar(
        "KBAGENT_PROJECT_FROM_ENV",
        "1 adds the project '__env__' from KBC_TOKEN and KBC_STORAGE_API_URL, "
        "without writing it to config.json.",
        secret=False,
        added_in="0.50.0",
    ),
    EnvVar(
        "KBAGENT_MAX_PARALLEL_WORKERS",
        "Number of parallel workers for commands that read several projects.",
        secret=False,
        added_in="0.4.0",
    ),
    EnvVar(
        "KBC_TOKEN",
        "Storage API token for 'project add' and for KBAGENT_PROJECT_FROM_ENV.",
        secret=True,
        added_in="0.4.0",
        option="project add --token",
    ),
    EnvVar(
        "KBC_STORAGE_API_URL",
        "Stack URL for 'project add', 'org setup' and KBAGENT_PROJECT_FROM_ENV.",
        secret=False,
        added_in="0.4.0",
        option="project add --url, org setup --url",
    ),
    EnvVar(
        "KBC_MANAGE_API_TOKEN",
        "Manage API token; read only with --allow-env-manage-token.",
        secret=True,
        added_in="0.4.0",
    ),
    EnvVar(
        "KBC_MASTER_TOKEN",
        "Token for the sharing commands when the project token lacks the permission.",
        secret=True,
        added_in="0.16.0",
    ),
    EnvVar(
        "KBC_MASTER_TOKEN_<ALIAS>",
        "Token for the sharing commands of one project; wins over KBC_MASTER_TOKEN.",
        secret=True,
        added_in="0.16.0",
    ),
    # Sign-in
    EnvVar(
        "KBC_LOGIN_EMAIL",
        "Account email for 'auth login-password'.",
        secret=False,
        added_in="0.84.0",
        option="auth login-password --email",
    ),
    EnvVar(
        "KBC_LOGIN_PASSWORD",
        "Account password for 'auth login-password'.",
        secret=True,
        added_in="0.84.0",
        option="auth login-password --password",
    ),
    EnvVar(
        "KBC_LOGIN_TOTP_SECRET",
        "Base32 TOTP seed for 'auth login-password'.",
        secret=True,
        added_in="0.84.0",
        option="auth login-password --totp-secret",
    ),
    # Observability and telemetry
    EnvVar(
        "KBAGENT_CONVERSATION_ID",
        "Value of the X-Conversation-ID header on every API request.",
        secret=False,
        added_in="0.16.3",
        option="--conversation-id",
    ),
    EnvVar(
        "KBAGENT_DISABLE_TELEMETRY",
        "1 turns off the usage telemetry.",
        secret=False,
        added_in="0.93.0",
    ),
    EnvVar(
        "DO_NOT_TRACK",
        "1 turns off the usage telemetry (the cross-tool convention).",
        secret=False,
        added_in="0.93.0",
    ),
    # kbagent serve and scheduled agents
    EnvVar(
        "KBAGENT_SERVE_URL",
        "URL of a running 'kbagent serve' for the 'http' commands.",
        secret=False,
        added_in="0.40.0",
    ),
    EnvVar(
        "KBAGENT_SERVE_TOKEN",
        "Bearer token of 'kbagent serve': the server reuses it, the 'http' commands send it.",
        secret=True,
        added_in="0.40.0",
    ),
    EnvVar(
        "KBAGENT_UI_DIST",
        "Directory of the built web UI for 'serve --ui'.",
        secret=False,
        added_in="0.40.0",
    ),
    EnvVar(
        "KBAGENT_ALLOW_AI_EXTRA_ARGS",
        "1 lets a scheduled ai_agent task pass its extra_args to the agent CLI.",
        secret=False,
        added_in="0.61.0",
    ),
    # Updates
    EnvVar(
        "KBAGENT_AUTO_UPDATE",
        "false turns off the update check and the self-update at startup.",
        secret=False,
        added_in="0.18.0",
    ),
    EnvVar(
        "KBAGENT_SKIP_UPDATE",
        "1 skips the self-update; set by kbagent for the process it starts after an update.",
        secret=False,
        added_in="0.18.0",
    ),
    EnvVar(
        "KBAGENT_UPDATED_FROM",
        "Version before a self-update; set by kbagent so the next run shows what changed.",
        secret=False,
        added_in="0.18.1",
    ),
    EnvVar(
        "KBAGENT_INCLUDE_PRERELEASE",
        "1 makes 'version' and 'update' include pre-releases, as --beta does.",
        secret=False,
        added_in="0.43.3",
    ),
    EnvVar(
        "KBAGENT_UPDATE_TIMEOUT",
        "Seconds the self-update may take.",
        secret=False,
        added_in="0.60.0",
    ),
    EnvVar(
        "KBAGENT_DEFER_UPDATE",
        "1 forces the deferred self-update (after the process exits), 0 the in-place one.",
        secret=False,
        added_in="0.79.0",
    ),
    EnvVar(
        "UV_LINK_MODE",
        "When set on Windows, the self-update does not add --link-mode copy (uv reads it).",
        secret=False,
        added_in="0.96.0",
    ),
    EnvVar(
        "SYSTEMROOT",
        "Windows directory, to find PowerShell for the deferred self-update.",
        secret=False,
        added_in="0.79.0",
    ),
    # Browser and terminal detection
    EnvVar(
        "SSH_CONNECTION",
        "Set in an SSH session: 'auth login' and 'config oauth-url' open no browser.",
        secret=False,
        added_in="0.80.0",
    ),
    EnvVar(
        "SSH_TTY",
        "Set in an SSH session: 'auth login' and 'config oauth-url' open no browser.",
        secret=False,
        added_in="0.80.0",
    ),
    EnvVar(
        "container",
        "Set in a container: 'auth login' and 'config oauth-url' open no browser.",
        secret=False,
        added_in="0.80.0",
    ),
    EnvVar(
        "WSL_INTEROP",
        "Set in WSL: the browser opens through wslview, and copying uses clip.exe.",
        secret=False,
        added_in="0.80.0",
    ),
    EnvVar(
        "NO_COLOR",
        "Set: no ANSI colors in human output (HO3).",
        secret=False,
        added_in="0.4.0",
        library="rich",
    ),
)

ENV_VAR_NAMES: frozenset[str] = frozenset(var.name for var in ENV_VARS)
