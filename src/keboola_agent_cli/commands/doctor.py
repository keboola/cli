"""Doctor command - thin CLI wrapper over DoctorService.

Delegates all health check logic to DoctorService (3-layer architecture).
"""

import typer

from ..output import format_doctor_panel
from ._helpers import get_formatter, get_service


def doctor_command(ctx: typer.Context) -> None:
    """Run health checks on CLI configuration and project connectivity."""
    formatter = get_formatter(ctx)
    doctor_service = get_service(ctx, "doctor_service")
    result = doctor_service.run_checks()
    formatter.output(result, format_doctor_panel)
