"""Encrypt command -- encrypt secret values via Keboola Encryption API.

Thin CLI layer: parses arguments, calls EncryptService, formats output.
No business logic belongs here.
"""

import json
import sys
from pathlib import Path

import typer

from ..errors import ConfigError, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

encrypt_app = typer.Typer(
    help="Encrypt secret values via Keboola Encryption API (one-way, no decrypt)"
)


@encrypt_app.callback(invoke_without_command=True)
def _encrypt_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "encrypt")


@encrypt_app.command("values")
def encrypt_values(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Keboola component ID (e.g. keboola.python-transformation-v2)",
    ),
    input_data: str = typer.Option(
        ...,
        "--input",
        help="JSON to encrypt: inline JSON, @file.json, or - for stdin",
    ),
    output_file: Path | None = typer.Option(
        None,
        "--output-file",
        help="Write result to file (0600 permissions) instead of stdout",
    ),
) -> None:
    """Encrypt #-prefixed secret values for a Keboola component.

    Input must be a JSON object with #-prefixed keys and string values.
    Already-encrypted values (KBC:: prefix) pass through unchanged.

    The Keboola Encryption API is one-way -- there is no decrypt endpoint.
    Encrypted values can only be used by Keboola components at runtime.

    Examples:
        kbagent encrypt values --project my-proj --component-id keboola.ex-db-snowflake --input '{"#password": "secret"}'
        echo '{"#token": "abc"}' | kbagent encrypt values --project my-proj --component-id keboola.ex-db-snowflake --input -
        kbagent encrypt values --project my-proj --component-id keboola.ex-db-snowflake --input @secrets.json --output-file encrypted.json
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "encrypt_service")

    # Parse input
    try:
        parsed = _parse_input(input_data)
    except (json.JSONDecodeError, FileNotFoundError, ValueError) as exc:
        formatter.error(message=str(exc), error_code="INPUT_ERROR")
        raise typer.Exit(code=2) from None

    if not isinstance(parsed, dict):
        formatter.error(
            message="Input must be a JSON object (dict), not " + type(parsed).__name__,
            error_code="INPUT_ERROR",
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.encrypt(alias=project, component_id=component_id, input_data=parsed)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    if output_file:
        output_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        output_file.chmod(0o600)
        if not formatter.json_mode:
            formatter.console.print(f"Encrypted values written to {output_file}")

    formatter.output(result, lambda c, d: c.print_json(json.dumps(d, indent=2)))


def _parse_input(raw: str) -> dict:
    """Parse input from inline JSON, @file, or stdin.

    Args:
        raw: One of:
            - "-" to read from stdin
            - "@path/to/file.json" to read from a file
            - Inline JSON string

    Returns:
        Parsed JSON as a dict.

    Raises:
        json.JSONDecodeError: If JSON parsing fails.
        FileNotFoundError: If @file does not exist.
    """
    if raw == "-":
        return json.loads(sys.stdin.read())
    if raw.startswith("@"):
        file_path = Path(raw[1:])
        if not file_path.is_file():
            raise FileNotFoundError(f"Input file not found: {file_path}")
        return json.loads(file_path.read_text(encoding="utf-8"))
    return json.loads(raw)
