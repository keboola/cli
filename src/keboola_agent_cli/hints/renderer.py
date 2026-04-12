"""Code renderers for the --hint system.

Produces runnable Python code from CommandHint definitions.
Two renderers: ClientRenderer (direct API) and ServiceRenderer (CLI config).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import CommandHint

# Placeholder URL when project alias cannot be resolved
_DEFAULT_STACK_URL = "https://connection.keboola.com"


def _escape_for_python_string(value: str) -> str:
    """Escape a value for safe embedding inside a double-quoted Python string literal.

    Prevents code injection via crafted parameter values (CWE-94).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _sanitize_for_comment(value: str) -> str:
    """Remove characters that could break Python comments or docstrings."""
    return value.replace("\n", " ").replace("\r", " ").replace('"""', "...")


def _build_original_command(hint: CommandHint, params: dict[str, Any]) -> str:
    """Reconstruct the original CLI command string for the docstring.

    Values are sanitized to prevent docstring/comment injection.
    """
    parts = ["kbagent"]
    # Convert "config.list" -> "config list"
    parts.extend(hint.cli_command.split("."))

    for key, value in params.items():
        if value is None:
            continue
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                parts.append(flag)
        elif isinstance(value, list):
            for item in value:
                parts.extend([flag, _sanitize_for_comment(str(item))])
        else:
            parts.extend([flag, _sanitize_for_comment(str(value))])

    return " ".join(parts)


def _substitute_params(args: dict[str, str], params: dict[str, Any]) -> dict[str, str]:
    """Substitute {param} placeholders in hint args with actual CLI values.

    Placeholders like {component_type} are replaced with the actual value
    from params. If a param is None, the arg is omitted.
    """
    result: dict[str, str] = {}
    for key, template in args.items():
        if not isinstance(template, str) or "{" not in template:
            result[key] = template
            continue

        # Find all {placeholder} references
        resolved = template
        skip = False
        for param_name, param_value in params.items():
            placeholder = f"{{{param_name}}}"
            if placeholder in resolved:
                if param_value is None:
                    skip = True
                    break
                if isinstance(param_value, str):
                    safe = _escape_for_python_string(param_value)
                    resolved = resolved.replace(placeholder, f'"{safe}"')
                elif isinstance(param_value, list):
                    list_repr = (
                        "["
                        + ", ".join(f'"{_escape_for_python_string(str(v))}"' for v in param_value)
                        + "]"
                    )
                    resolved = resolved.replace(placeholder, list_repr)
                else:
                    resolved = resolved.replace(placeholder, str(param_value))

        if not skip:
            result[key] = resolved

    return result


def _format_call_args(args: dict[str, str]) -> str:
    """Format keyword arguments for a Python method call."""
    if not args:
        return ""
    parts = [f"{k}={v}" for k, v in args.items()]
    # Short calls on one line, long calls multi-line
    joined = ", ".join(parts)
    if len(joined) <= 60:
        return joined
    return "\n    " + ",\n    ".join(parts) + ",\n"


class ClientRenderer:
    """Renders Python code using KeboolaClient (direct API calls)."""

    @staticmethod
    def render(
        hint: CommandHint,
        params: dict[str, Any],
        stack_url: str | None,
        branch_id: int | None,
        config_dir: Path | None = None,
    ) -> str:
        """Generate Python code using the client layer."""
        url = stack_url or _DEFAULT_STACK_URL
        original_cmd = _build_original_command(hint, params)
        lines: list[str] = []

        # Shebang + docstring (escape to prevent triple-quote injection)
        safe_cmd = original_cmd.replace('"""', '"\\"" ')
        lines.append("#!/usr/bin/env python3")
        lines.append(f'"""Equivalent of: {safe_cmd}"""')
        lines.append("")

        # Imports
        client_types: set[str] = set()
        needs_time = any(step.kind == "poll_loop" for step in hint.steps)
        for step in hint.steps:
            client_types.add(step.client.client_type)

        needs_os = "storage" in client_types or "manage" in client_types
        if needs_os:
            lines.append("import os")
        if needs_time:
            lines.append("import time")
        if "mcp" in client_types:
            lines.append("from pathlib import Path")
        lines.append("")

        if "storage" in client_types:
            lines.append("from keboola_agent_cli.client import KeboolaClient")
        if "manage" in client_types:
            lines.append("from keboola_agent_cli.manage_client import ManageClient")
        if "mcp" in client_types:
            lines.append("from keboola_agent_cli.config_store import ConfigStore")
            lines.append("from keboola_agent_cli.services.mcp_service import McpService")

        lines.append("")

        # Client construction
        if "storage" in client_types:
            url_comment = ""
            if stack_url:
                # Find the project alias in params for the comment
                project = params.get("project")
                if project:
                    proj_label = project[0] if isinstance(project, list) else project
                    safe_label = _escape_for_python_string(str(proj_label))
                    url_comment = f"  # from project '{safe_label}'"
            lines.append("client = KeboolaClient(")
            lines.append(f'    base_url="{url}",{url_comment}')
            lines.append('    token=os.environ["KBC_STORAGE_TOKEN"],')
            lines.append(")")

        if "manage" in client_types:
            lines.append("manage_client = ManageClient(")
            lines.append(f'    base_url="{url}",')
            lines.append('    token=os.environ["KBC_MANAGE_API_TOKEN"],')
            lines.append(")")

        if "mcp" in client_types:
            config_dir_str = str(config_dir) if config_dir else "/path/to/.kbagent"
            lines.append("# MCP tools require ConfigStore (they go through keboola-mcp-server)")
            lines.append(
                f'mcp_service = McpService(config_store=ConfigStore(config_dir=Path("{config_dir_str}")))'
            )

        # Determine if we need try/finally for cleanup
        close_vars = []
        if "storage" in client_types:
            close_vars.append("client")
        if "manage" in client_types:
            close_vars.append("manage_client")

        indent = "    " if close_vars else ""
        lines.append("")
        if close_vars:
            lines.append("try:")

        # Steps
        for i, step in enumerate(hint.steps):
            resolved_args = _substitute_params(step.client.args, params)

            # Inject branch_id if present and method accepts it
            if branch_id is not None and "branch_id" not in resolved_args:
                if any("{branch" in v for v in step.client.args.values()):
                    pass  # Already handled by substitution
                elif "branch_id" in step.client.args:
                    pass  # Already in template
                else:
                    # Add branch_id for methods that typically accept it
                    resolved_args["branch_id"] = str(branch_id)

            client_var_map = {
                "storage": "client",
                "manage": "manage_client",
                "mcp": "mcp_service",
            }
            client_var = client_var_map.get(step.client.client_type, "client")
            call_args = _format_call_args(resolved_args)

            lines.append(f"{indent}# Step {i + 1}: {step.comment}")

            if step.kind == "poll_loop":
                # Generate polling loop
                lines.append(
                    f"{indent}{step.client.result_var} = {client_var}.{step.client.method}({call_args})"
                )
                lines.append(f"{indent}while {step.poll_condition}:")
                lines.append(f"{indent}    time.sleep({step.poll_interval})")
                lines.append(
                    f"{indent}    {step.client.result_var} = {client_var}.{step.client.method}({call_args})"
                )
            else:
                lines.append(
                    f"{indent}{step.client.result_var} = {client_var}.{step.client.method}({call_args})"
                )

            if i < len(hint.steps) - 1:
                lines.append("")

        # Print result
        last_var = hint.steps[-1].client.result_var
        lines.append("")
        lines.append(f"{indent}print({last_var})")

        # Finally (only for clients that need closing)
        if close_vars:
            lines.append("finally:")
            for var in close_vars:
                lines.append(f"    {var}.close()")

        # Notes
        if hint.notes:
            lines.append("")
            for note in hint.notes:
                lines.append(f"# NOTE: {note}")

        return "\n".join(lines)


class ServiceRenderer:
    """Renders Python code using the service layer (CLI config)."""

    @staticmethod
    def render(
        hint: CommandHint,
        params: dict[str, Any],
        stack_url: str | None,
        config_dir: Path | None,
        branch_id: int | None,
    ) -> str:
        """Generate Python code using the service layer."""
        original_cmd = _build_original_command(hint, params)
        lines: list[str] = []

        # Check if any step has a service call
        has_service = any(step.service is not None for step in hint.steps)
        if not has_service:
            # Fall back to client renderer with a note
            lines.append(f"# No service-layer equivalent for: {original_cmd}")
            lines.append("# Use --hint client instead (this command uses direct API calls).")
            lines.append("")
            return "\n".join(lines) + ClientRenderer.render(
                hint, params, stack_url, branch_id, config_dir=config_dir
            )

        # Shebang + docstring (escape to prevent triple-quote injection)
        safe_cmd = original_cmd.replace('"""', '"\\"" ')
        lines.append("#!/usr/bin/env python3")
        lines.append(f'"""Equivalent of: {safe_cmd}"""')
        lines.append("")

        # Collect unique service imports
        service_imports: dict[str, str] = {}  # module -> class
        for step in hint.steps:
            if step.service:
                service_imports[step.service.service_module] = step.service.service_class

        # Imports
        lines.append("from pathlib import Path")
        lines.append("")
        lines.append("from keboola_agent_cli.config_store import ConfigStore")
        for module, cls in sorted(service_imports.items()):
            lines.append(f"from keboola_agent_cli.services.{module} import {cls}")

        lines.append("")

        # Config store setup
        dir_str = str(config_dir) if config_dir else "/path/to/.kbagent"
        lines.append("# Path to the directory containing config.json")
        lines.append(f'# (same as: kbagent --config-dir "{dir_str}" ...)')
        lines.append(f'store = ConfigStore(config_dir=Path("{dir_str}"))')

        # Service construction
        created_services: set[str] = set()
        for step in hint.steps:
            if step.service and step.service.service_class not in created_services:
                var_name = _service_var_name(step.service.service_class)
                lines.append(f"{var_name} = {step.service.service_class}(config_store=store)")
                created_services.add(step.service.service_class)

        lines.append("")

        # Steps
        for i, step in enumerate(hint.steps):
            if step.service is None:
                lines.append(f"# Step {i + 1}: {step.comment}")
                lines.append("# (No service-layer equivalent — use client layer for this step)")
                continue

            resolved_args = _substitute_params(step.service.args, params)
            var_name = _service_var_name(step.service.service_class)
            call_args = _format_call_args(resolved_args)

            lines.append(f"# Step {i + 1}: {step.comment}")
            result_var = step.client.result_var
            lines.append(f"{result_var} = {var_name}.{step.service.method}({call_args})")

            if i < len(hint.steps) - 1:
                lines.append("")

        # Print result
        last_service_step = next((s for s in reversed(hint.steps) if s.service is not None), None)
        if last_service_step:
            lines.append("")
            lines.append(f"print({last_service_step.client.result_var})")

        # Notes
        if hint.notes:
            lines.append("")
            for note in hint.notes:
                lines.append(f"# NOTE: {note}")

        return "\n".join(lines)


def _service_var_name(class_name: str) -> str:
    """Convert 'ConfigService' -> 'config_service'."""
    # Insert underscore before each uppercase letter (except first), then lowercase
    result = []
    for i, char in enumerate(class_name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)
