"""A command error handler keeps `retryable` and `details` of a KeboolaApiError (CLI-34).

The agent context tells agents to read `error.retryable` ("Check retryable --
if true, retry"). A handler that passes only the code and the message prints
`retryable: false` for a retryable API error and drops its `details`.
"""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import emit_api_error
from keboola_agent_cli.errors import ErrorCode, FeatureNotEnabledError, KeboolaApiError
from keboola_agent_cli.output import OutputFormatter
from keboola_agent_cli.services.kai_service import KaiService

COMMANDS = Path(__file__).parent.parent / "src" / "keboola_agent_cli" / "commands"
runner = CliRunner()

# Exceptions that carry a code but no `retryable` or `details`. A call inside
# `except <one of these>` may take `error_code=exc.error_code` without them.
NO_RETRY_FIELDS = {"SyncConflictError", "ForwarderError"}


def _type_names(node: ast.expr | None) -> set[str]:
    """Names in an exception type: `A`, `(A, B)` or `A | B`."""
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return set().union(*(_type_names(elt) for elt in node.elts))
    if isinstance(node, ast.BinOp):
        return _type_names(node.left) | _type_names(node.right)
    return {ast.unparse(node)}


def _isinstance_types(test: ast.expr) -> set[str] | None:
    """Types of `isinstance(x, T)` in an `if` test, also inside `and`; None if absent."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for value in test.values:
            found = _isinstance_types(value)
            if found is not None:
                return found
        return None
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "isinstance"
        and len(test.args) == 2
    ):
        return _type_names(test.args[1])
    return None


def _is_formatter_error(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "error"
        and "formatter" in ast.unparse(node.func.value)
    )


def _forwards_code_of(call: ast.Call) -> bool:
    """`error_code=exc.error_code`: the code comes from an exception object."""
    return any(
        kw.arg == "error_code"
        and isinstance(kw.value, ast.Attribute)
        and kw.value.attr == "error_code"
        and isinstance(kw.value.value, ast.Name)
        for kw in call.keywords
    )


def _violations_in(source: str) -> list[int]:
    """Lines of `formatter.error` calls that hold a KeboolaApiError and drop its fields.

    A call holds one inside `except KeboolaApiError`, inside a function with a
    parameter annotated with `KeboolaApiError` (also in a union), and inside
    `if isinstance(x, KeboolaApiError)`. The innermost scope wins: an
    `except` or `isinstance` branch for other types holds none. Outside
    `except SyncConflictError` / `except ForwarderError`, a call that takes
    `error_code=exc.error_code` holds one too.
    """
    found: set[int] = set()

    def visit(node: ast.AST, holding: bool, code_rule: bool) -> None:
        if _is_formatter_error(node):
            assert isinstance(node, ast.Call)
            keywords = {kw.arg for kw in node.keywords}
            if (holding or (code_rule and _forwards_code_of(node))) and not {
                "retryable",
                "details",
            } <= keywords:
                found.add(node.lineno)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            params = node.args.args + node.args.kwonlyargs
            if any(a.annotation and "KeboolaApiError" in _type_names(a.annotation) for a in params):
                holding = True
        elif isinstance(node, ast.ExceptHandler):
            names = _type_names(node.type)
            holding = "KeboolaApiError" in names
            code_rule = not names & NO_RETRY_FIELDS
        elif isinstance(node, ast.If):
            types = _isinstance_types(node.test)
            if types is not None:
                visit(node.test, holding, code_rule)
                for child in node.body:
                    visit(child, "KeboolaApiError" in types, code_rule)
                for child in node.orelse:
                    visit(child, holding, code_rule)
                return
        for child in ast.iter_child_nodes(node):
            visit(child, holding, code_rule)

    visit(ast.parse(source), holding=False, code_rule=True)
    return sorted(found)


def _violations() -> list[str]:
    return [
        f"{path.name}:{line}"
        for path in sorted(COMMANDS.rglob("*.py"))
        for line in _violations_in(path.read_text(encoding="utf-8"))
    ]


def test_api_error_handlers_forward_retryable_and_details() -> None:
    """Use `emit_api_error(formatter, exc)` or pass `retryable=` and `details=`."""
    assert _violations() == []


def test_rule_checks_api_handler_next_to_exempt_type() -> None:
    """Only `except SyncConflictError` is exempt, not the whole function."""
    source = """
def sync_pull(formatter):
    try:
        pull()
    except SyncConflictError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
"""
    assert _violations_in(source) == [8]


def test_rule_checks_union_annotated_parameter() -> None:
    source = """
def _handle(formatter, exc: ConfigError | KeboolaApiError):
    if isinstance(exc, ConfigError):
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise SystemExit(5)
    formatter.error(message=exc.message, error_code=ErrorCode.API_ERROR)
"""
    assert _violations_in(source) == [6]


def test_rule_checks_isinstance_branch_in_broad_handler() -> None:
    source = """
def run(formatter):
    try:
        work()
    except Exception as exc:
        if isinstance(exc, (ConfigError, KeboolaApiError)):
            formatter.error(message=str(exc), error_code=ErrorCode.API_ERROR)
        formatter.error(message=str(exc), error_code=ErrorCode.INIT_ERROR)
"""
    assert _violations_in(source) == [7]


def test_rule_reports_a_nested_call_once() -> None:
    source = """
def outer(formatter):
    def inner():
        try:
            work()
        except KeboolaApiError as exc:
            formatter.error(message=exc.message, error_code=exc.error_code)
    inner()
"""
    assert _violations_in(source) == [7]


def _assert_error_envelope(stdout: str, code: str) -> dict:
    error = json.loads(stdout)["error"]
    assert error["code"] == code
    assert error["retryable"] is True
    assert error["details"] == {"api_error_code": "kai.unavailable"}
    return error


def _retryable_error(code: str = ErrorCode.API_ERROR) -> KeboolaApiError:
    return KeboolaApiError(
        message="API error 503",
        status_code=503,
        error_code=code,
        retryable=True,
        details={"api_error_code": "kai.unavailable"},
    )


def test_kai_ping_json_error_carries_retryable(tmp_config_dir: Path) -> None:
    setup_single_project(tmp_config_dir)
    service = MagicMock(spec=KaiService)
    service.resolve_alias.return_value = "prod"
    service.ping.side_effect = _retryable_error()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("keboola_agent_cli.commands.kai.get_service", lambda ctx, name: service)
        result = runner.invoke(
            app,
            ["--json", "--config-dir", str(tmp_config_dir), "kai", "ping", "--project", "prod"],
        )

    assert result.exit_code == 1
    _assert_error_envelope(result.stdout, ErrorCode.API_ERROR)


def test_config_restore_json_error_carries_retryable(tmp_config_dir: Path) -> None:
    store = setup_single_project(tmp_config_dir)
    service = MagicMock()
    service.restore_config.side_effect = _retryable_error()

    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.ConfigService", return_value=service),
    ):
        result = runner.invoke(
            app,
            [
                "--json",
                "config",
                "restore",
                "--project",
                "prod",
                "--component-id",
                "keboola.comp",
                "--config-id",
                "cfg-1",
            ],
        )

    assert result.exit_code == 1
    _assert_error_envelope(result.stdout, ErrorCode.API_ERROR)


def test_search_json_error_keeps_typed_code(tmp_config_dir: Path) -> None:
    """`search` used to report every API error as API_ERROR."""
    store = setup_single_project(tmp_config_dir)
    service = MagicMock()
    service.search.side_effect = _retryable_error(ErrorCode.TIMEOUT)

    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.SearchService", return_value=service),
    ):
        result = runner.invoke(app, ["--json", "search", "orders"])

    # TIMEOUT exits 4 (spec E1). `search` exited 1 for every API error before 1.0.0.
    assert result.exit_code == 4
    _assert_error_envelope(result.stdout, ErrorCode.TIMEOUT)


def test_emit_api_error_forwards_every_field(capsys: pytest.CaptureFixture[str]) -> None:
    emit_api_error(OutputFormatter(json_mode=True), _retryable_error(), project="prod")

    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == ErrorCode.API_ERROR
    assert error["message"] == "API error 503"
    assert error["project"] == "prod"
    assert error["retryable"] is True
    assert error["details"] == {"api_error_code": "kai.unavailable"}


def test_emit_api_error_leaves_out_empty_details(capsys: pytest.CaptureFixture[str]) -> None:
    exc = KeboolaApiError(message="gone", error_code=ErrorCode.NOT_FOUND)
    emit_api_error(OutputFormatter(json_mode=True), exc)

    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == ErrorCode.NOT_FOUND
    assert error["retryable"] is False
    assert "details" not in error


def _identity_add(tmp_config_dir: Path, side_effect: Exception):
    service = MagicMock()
    service.add_identity.side_effect = side_effect
    with patch(
        "keboola_agent_cli.commands.dev_portal.get_dev_portal_service", return_value=service
    ):
        return runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_config_dir),
                "dev-portal",
                "identity",
                "add",
                "--alias",
                "vendor",
                "--username",
                "user",
                "--password",
                "secret",
            ],
        )


def test_dev_portal_identity_add_api_error_carries_retryable(tmp_config_dir: Path) -> None:
    result = _identity_add(tmp_config_dir, _retryable_error(ErrorCode.TIMEOUT))

    assert result.exit_code == 4  # map_error_to_exit_code: TIMEOUT
    _assert_error_envelope(result.stdout, ErrorCode.TIMEOUT)


def test_dev_portal_identity_add_config_error_keeps_code(tmp_config_dir: Path) -> None:
    result = _identity_add(tmp_config_dir, FeatureNotEnabledError("feature off"))

    assert result.exit_code == 5
    error = json.loads(result.stdout)["error"]
    assert error["code"] == ErrorCode.FEATURE_NOT_ENABLED
    assert error["message"] == "feature off"


@pytest.mark.parametrize(
    ("service_class", "method", "args"),
    [
        ("StreamService", "list_sources", ["stream", "list", "--project", "prod"]),
        ("TokenService", "list_tokens", ["token", "list", "--project", "prod"]),
    ],
)
def test_group_handler_json_error_carries_details(
    tmp_config_dir: Path, service_class: str, method: str, args: list[str]
) -> None:
    """The shared `_handle_errors` of a group forwards `details` too."""
    setup_single_project(tmp_config_dir)
    service = MagicMock()
    getattr(service, method).side_effect = _retryable_error()

    with patch(f"keboola_agent_cli.cli.{service_class}", return_value=service):
        result = runner.invoke(app, ["--json", "--config-dir", str(tmp_config_dir), *args])

    assert result.exit_code == 1
    _assert_error_envelope(result.stdout, ErrorCode.API_ERROR)
