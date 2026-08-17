"""Pay-As-You-Go credit balance discovery across one or many projects.

Wraps ``KeboolaClient.get_credits`` (``GET /credits`` on the derived
``billing.{stack}`` host) with the same fan-out / per-project-error shape
every other multi-project service uses (see ``ScheduleService`` in
``schedule_service.py`` for the idiom this mirrors).

MONEY GUARDRAIL: the billing service also exposes ``POST /credits``, which
triggers a REAL automatic top-up (real money charged to the project). This
service is **read-only by design** -- it only ever calls
``client.get_credits()`` (a GET). No method here issues, or ever should
issue, a POST to the billing host. See ``client/billing.py`` for the mixin
that enforces the same restriction one layer down.
"""

from __future__ import annotations

import logging
from typing import Any

from ..constants import MINUTES_PER_CREDIT, PAYG_FEATURE
from ..errors import ErrorCode, KeboolaApiError
from ..models import ProjectConfig, ProjectCredits
from .base import BaseService, project_error_entry

logger = logging.getLogger(__name__)

# KeboolaApiError codes that mean "we never got a real answer from the
# billing host" -- worth a friendlier message than the raw httpx error,
# because on a stack without PAYG the derived `billing.<stack>` host can be
# NXDOMAIN (see the has_feature gate below, which is what normally keeps a
# non-PAYG project from ever reaching this branch at all).
_UNREACHABLE_ERROR_CODES = frozenset(
    {ErrorCode.CONNECTION_ERROR, ErrorCode.TIMEOUT, ErrorCode.RETRY_EXHAUSTED}
)


def _build_credit_row(
    alias: str, project: ProjectConfig, credits_: ProjectCredits
) -> dict[str, Any]:
    """Project a parsed ``ProjectCredits`` payload into the CLI-facing row.

    Derives minutes from credits (never the reverse -- the API's native unit
    is credits) and ``purchased`` as ``consumed + remaining``, since the
    billing endpoint reports the current balance, not the lifetime total.
    """
    stats = credits_.stats
    component_jobs_consumed = (
        stats.component_jobs.consumed if stats and stats.component_jobs else 0.0
    )
    workspace_jobs = [
        {
            "workspace_type": job.workspace_type,
            "warehouse_size": job.warehouse_size,
            "consumed": job.consumed,
        }
        for job in (stats.workspace_jobs if stats else [])
    ]

    return {
        "project_alias": alias,
        "project_id": project.project_id,
        "consumed": credits_.consumed,
        "remaining": credits_.remaining,
        "purchased": credits_.consumed + credits_.remaining,
        "consumed_minutes": credits_.consumed * MINUTES_PER_CREDIT,
        "remaining_minutes": credits_.remaining * MINUTES_PER_CREDIT,
        "component_jobs_consumed": component_jobs_consumed,
        "workspace_jobs": workspace_jobs,
    }


class BillingService(BaseService):
    """Fleet-wide PAYG credit balance lookup.

    Read-only by construction -- see the module docstring's money guardrail.
    Like the other multi-project services, per-project failures degrade
    individually into the ``errors`` list rather than aborting the whole
    fan-out.
    """

    def get_credits(self, aliases: list[str] | None = None) -> dict[str, Any]:
        """Fetch the PAYG credit balance for one, many, or all projects.

        Args:
            aliases: Project aliases to query. ``None`` / empty means every
                registered project.

        Returns:
            ``{"credits": [row, ...], "errors": [entry, ...]}``, both sorted
            by ``project_alias`` for deterministic output. Each row has
            ``project_alias``, ``project_id``, ``consumed``, ``remaining``,
            ``purchased``, ``consumed_minutes``, ``remaining_minutes``,
            ``component_jobs_consumed``, ``workspace_jobs``.
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_credits(alias, project)

        successes, errors = self._run_parallel(projects, worker)

        credits_rows = [result[1] for result in successes]
        credits_rows.sort(key=lambda row: row.get("project_alias", ""))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"credits": credits_rows, "errors": errors}

    def _fetch_project_credits(self, alias: str, project: ProjectConfig) -> tuple[Any, ...]:
        """Fetch + shape the PAYG balance for a single project.

        Order matters here, in this exact sequence:

        1. Build the client.
        2. Feature-gate with ``client.has_feature(PAYG_FEATURE)`` BEFORE any
           billing call. This is NOT a fallback triggered by a billing 4xx --
           it runs first, unconditionally. On a stack without PAYG, the
           Storage API's own service index still advertises a
           ``billing.<stack>`` host entry that simply does not resolve
           (NXDOMAIN); calling it anyway would surface an opaque DNS/connect
           failure instead of the actual, actionable reason ("this project
           doesn't have PAYG"). Checking the feature flag first turns that
           into a clear ``PAYG_NOT_AVAILABLE`` error before the network call
           is ever attempted.
        3. ``client.get_credits()``, parsed through ``ProjectCredits``
           (tolerant model -- see ``models.py``) and projected into the row.

        A connection/DNS failure that still slips through step 3 (e.g. a
        transient host that resolves but refuses the connection) is
        re-worded to say the billing host is unreachable on this stack,
        rather than surfacing a bare httpx/stack-trace message.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            if not client.has_feature(PAYG_FEATURE):
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": str(ErrorCode.PAYG_NOT_AVAILABLE),
                        "message": (
                            f"Project does not have the '{PAYG_FEATURE}' feature enabled. "
                            "PAYG credit balances only exist for pay-as-you-go projects; "
                            "ask a Keboola admin to enable it if this project should have one."
                        ),
                    },
                )

            raw = client.get_credits()
            parsed = ProjectCredits.model_validate(raw)
            row = _build_credit_row(alias, project, parsed)
            return (alias, row, True)
        except KeboolaApiError as exc:
            message = exc.message
            if exc.error_code in _UNREACHABLE_ERROR_CODES:
                message = (
                    f"Could not reach the billing service on stack {project.stack_url!r}; "
                    f"the PAYG credit balance is unavailable right now. Original error: {exc.message}"
                )
            logger.debug("get_credits failed for project '%s': %s", alias, exc)
            return (alias, project_error_entry(alias, exc, message=message))
        except Exception as exc:
            logger.debug("Unexpected error fetching credits for project '%s': %s", alias, exc)
            return (alias, project_error_entry(alias, exc))
        finally:
            client.close()


__all__ = ["BillingService"]
