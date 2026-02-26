"""Output formatting with JSON and Rich dual mode support."""

import json
import sys
from typing import Any, Callable

from rich.console import Console

from .models import ErrorResponse, SuccessResponse


class OutputFormatter:
    """Formats CLI output as either JSON (for machines/agents) or Rich (for humans).

    In JSON mode, all output goes to stdout as valid JSON.
    In human mode, Rich console is used for formatted tables and panels.
    """

    def __init__(
        self,
        json_mode: bool = False,
        no_color: bool = False,
        verbose: bool = False,
    ) -> None:
        self.json_mode = json_mode
        self.verbose = verbose
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        force_terminal = None if is_tty and not no_color else False
        self.console = Console(
            no_color=no_color,
            force_terminal=force_terminal,
        )
        self.err_console = Console(
            stderr=True,
            no_color=no_color,
            force_terminal=force_terminal,
        )

    def output(self, data: Any, human_formatter: Callable[[Console, Any], None] | None = None) -> None:
        """Output data in the appropriate format.

        Args:
            data: The data to output. In JSON mode, this is serialized directly.
                  In human mode, it's passed to human_formatter.
            human_formatter: A callable that takes (Console, data) and prints
                           human-friendly output. If None in human mode, prints repr.
        """
        if self.json_mode:
            response = SuccessResponse(status="ok", data=data)
            sys.stdout.write(response.model_dump_json(indent=2) + "\n")
        else:
            if human_formatter is not None:
                human_formatter(self.console, data)
            else:
                self.console.print(data)

    def error(self, message: str, error_code: str = "ERROR", project: str = "", retryable: bool = False) -> None:
        """Output an error message.

        Args:
            message: Human-readable error description.
            error_code: Machine-readable error code.
            project: Project alias related to the error.
            retryable: Whether the operation can be retried.
        """
        if self.json_mode:
            err = ErrorResponse(
                code=error_code,
                message=message,
                project=project,
                retryable=retryable,
            )
            error_envelope = {"status": "error", "error": err.model_dump()}
            sys.stdout.write(json.dumps(error_envelope, indent=2) + "\n")
        else:
            self.err_console.print(f"[bold red]Error:[/bold red] {message}")

    def success(self, message: str) -> None:
        """Output a success message.

        Args:
            message: The success message to display.
        """
        if self.json_mode:
            response = SuccessResponse(status="ok", data={"message": message})
            sys.stdout.write(response.model_dump_json(indent=2) + "\n")
        else:
            self.console.print(f"[bold green]Success:[/bold green] {message}")
