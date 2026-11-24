"""Doctor command - thin CLI wrapper over DoctorService.

Delegates all health check logic to DoctorService (3-layer architecture).
"""

import typer

from ..auto_update import show_deprecation_notice
from ..output import format_doctor_panel
from ..services.doctor_service import OptionalCheck
from ._helpers import get_formatter, get_service


def doctor_command(
    ctx: typer.Context,
    check: list[OptionalCheck] | None = typer.Option(
        None,
        "--check",
        help=(
            "Also run this optional check (repeat for more). Optional checks read more "
            "than the CLI setup; a plain doctor lists each one with the command to run it."
        ),
    ),
) -> None:
    """Run health checks on CLI configuration and project connectivity."""
    formatter = get_formatter(ctx)
    doctor_service = get_service(ctx, "doctor_service")

    result = doctor_service.run_checks(tuple(dict.fromkeys(check or ())))
    show_deprecation_notice(formatter)
    formatter.output(result, format_doctor_panel)
