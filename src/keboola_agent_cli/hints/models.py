"""Data models for the --hint code generation system.

Pure dataclasses describing what code to generate for each CLI command.
No external imports — these are consumed by the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class HintMode(StrEnum):
    """Which code layer to generate."""

    CLIENT = "client"
    SERVICE = "service"


@dataclass
class ClientCall:
    """Describes a KeboolaClient (or ManageClient) method call.

    Used by the renderer to generate direct API usage code.
    """

    method: str
    """Client method name, e.g. 'list_components'."""

    args: dict[str, str] = field(default_factory=dict)
    """Mapping of param name -> code expression (inserted literally).
    Example: {"branch_id": "123", "component_type": '"extractor"'}
    """

    client_type: str = "storage"
    """Which client class: 'storage' (KeboolaClient) or 'manage' (ManageClient)."""

    result_var: str = "result"
    """Variable name to assign the result to."""

    result_hint: str = ""
    """Type hint for the result, e.g. 'list[dict]'."""


@dataclass
class ServiceCall:
    """Describes a service-layer method call.

    Used by the renderer to generate code that leverages CLI config.
    """

    service_class: str
    """Class name, e.g. 'ConfigService'."""

    service_module: str
    """Module name under services/, e.g. 'config_service'."""

    method: str
    """Method name, e.g. 'list_configs'."""

    args: dict[str, str] = field(default_factory=dict)
    """Mapping of param name -> code expression."""


@dataclass
class HintStep:
    """One logical step in a command's hint.

    Simple commands have one step. Multi-step commands (e.g. job run --wait)
    have multiple steps to show the full flow.
    """

    comment: str
    """Human-readable description of what this step does."""

    client: ClientCall
    """How to do this with KeboolaClient."""

    service: ServiceCall | None = None
    """How to do this with the service layer. None if not applicable."""

    kind: str = "single"
    """Step kind: 'single' for one-shot, 'poll_loop' for polling patterns."""

    poll_interval: float = 5.0
    """Seconds between polls (only for poll_loop kind)."""

    poll_condition: str = ""
    """Python expression for poll loop condition, e.g. 'not job.get(\"isFinished\")'."""


@dataclass
class CommandHint:
    """Complete hint definition for one CLI command.

    Registered in HintRegistry and looked up by cli_command key.
    """

    cli_command: str
    """Dot-separated command path, e.g. 'config.list'."""

    description: str
    """What the command does (used in generated docstring)."""

    steps: list[HintStep]
    """Ordered list of steps to render."""

    notes: list[str] = field(default_factory=list)
    """Extra tips or caveats added as comments in generated code."""
