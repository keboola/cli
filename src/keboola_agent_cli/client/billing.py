"""Pay-As-You-Go credit balance -- GET /credits on the billing service.

New for issue #594. The billing service also exposes ``POST /credits``, which
triggers a REAL automatic top-up (real money charged to the project). That
endpoint is deliberately NOT wrapped anywhere in this mixin -- kbagent's
billing surface is read-only by design; see the module-level guardrail in
``services/billing_service.py`` for the feature-gate that keeps non-PAYG
projects off this host entirely.
"""

from typing import Any

from ._core import _CoreClient


class _BillingMixin(_CoreClient):
    """Pay-As-You-Go credit balance -- GET /credits on the billing service."""

    def get_credits(self) -> dict[str, Any]:
        """Fetch the project's PAYG credit balance.

        GETs ``/credits`` on the ``billing.{stack-suffix}`` host and returns
        the raw JSON dict verbatim (no shaping here -- see
        ``ProjectCredits`` in ``models.py`` for the tolerant parse used by
        the service layer). Read-only: this is the only method this mixin
        exposes, by design -- see the module docstring.
        """
        response = self._billing_get("/credits")
        return response.json()
