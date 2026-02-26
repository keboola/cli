"""Tests for OutputFormatter with JSON and Rich dual mode."""

import json
import sys
from io import StringIO

from rich.console import Console

from keboola_agent_cli.output import OutputFormatter


class TestOutputFormatterJsonMode:
    """Tests for OutputFormatter in JSON mode."""

    def test_output_list_data(self) -> None:
        """JSON mode outputs a valid JSON envelope with list data."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.output([{"id": 1, "name": "test"}])
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"] == [{"id": 1, "name": "test"}]

    def test_output_dict_data(self) -> None:
        """JSON mode outputs valid JSON with dict data."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.output({"key": "value"})
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"]["key"] == "value"

    def test_output_empty_list(self) -> None:
        """JSON mode handles empty list data."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.output([])
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"] == []

    def test_error_json_output(self) -> None:
        """JSON mode error outputs structured error envelope."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.error(
                message="Token expired",
                error_code="INVALID_TOKEN",
                project="prod-aws",
                retryable=False,
            )
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_TOKEN"
        assert result["error"]["message"] == "Token expired"
        assert result["error"]["project"] == "prod-aws"
        assert result["error"]["retryable"] is False

    def test_success_json_output(self) -> None:
        """JSON mode success outputs structured response."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.success("Project added successfully")
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"]["message"] == "Project added successfully"


class TestOutputFormatterHumanMode:
    """Tests for OutputFormatter in human (Rich) mode."""

    def test_output_calls_human_formatter(self) -> None:
        """Human mode calls the provided human_formatter callable."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        called_with: list = []

        def mock_formatter(console: Console, data: object) -> None:
            called_with.append(data)

        formatter.output({"test": "data"}, mock_formatter)
        assert len(called_with) == 1
        assert called_with[0] == {"test": "data"}

    def test_output_without_formatter_does_not_crash(self) -> None:
        """Human mode without a formatter falls back to console.print and does not crash."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.output("simple string")

    def test_error_does_not_crash(self) -> None:
        """Human mode error output does not crash."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.error("Something went wrong")

    def test_success_does_not_crash(self) -> None:
        """Human mode success output does not crash."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.success("All good")


class TestOutputFormatterInit:
    """Tests for OutputFormatter initialization options."""

    def test_json_mode_flag(self) -> None:
        """json_mode flag is stored correctly."""
        formatter = OutputFormatter(json_mode=True)
        assert formatter.json_mode is True

    def test_verbose_flag(self) -> None:
        """verbose flag is stored correctly."""
        formatter = OutputFormatter(verbose=True)
        assert formatter.verbose is True

    def test_no_color_creates_console(self) -> None:
        """no_color creates a Console instance with color disabled."""
        formatter = OutputFormatter(no_color=True)
        assert formatter.console is not None
        assert formatter.err_console is not None
