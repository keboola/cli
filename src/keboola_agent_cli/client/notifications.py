"""Notification Service: project-level notification subscriptions (issue #600).

Backs the Flow Builder's *Notifications* tab (the bell icon: Success / Error /
Processing-delay / Warning cards). Those recipients are NOT part of a flow's
``configuration`` JSON -- they live in a separate platform service advertised
as ``{"id": "notification", ...}`` in ``GET /v2/storage`` -- which is why they
were invisible to ``flow detail`` / ``config detail`` before this mixin.

Not to be confused with the in-flow ``type: "notification"`` task, which IS
stored in the flow configuration and has always been visible.

Wire-format notes taken from the service's public swagger, because the shapes
are easy to guess wrong:

- Event names are **kebab-case** (``job-failed``, ``job-succeeded``,
  ``job-succeeded-with-warning``, ``job-processing-long`` and their
  ``phase-job-*`` variants) and ``EventName`` is an open string, not an enum --
  so nothing here validates the value against a fixed set.
- ``filters[].field`` values are **dotted paths into the event payload**
  (``job.component.id``, ``job.configuration.id``, ``branch.id``, ``phase.id``,
  ``durationOvertimePercentage``), not flat keys.
- ``recipient`` is discriminated on ``channel``: an email recipient carries
  ``address``, a webhook recipient carries ``url``.

Shaping those into audit rows is the service layer's job -- this mixin returns
the parsed JSON verbatim.
"""

from typing import Any
from urllib.parse import quote

from ._core import _CoreClient


class _NotificationsMixin(_CoreClient):
    """Notification Service: read access to project notification subscriptions."""

    def list_project_subscriptions(self, event: str | None = None) -> list[dict[str, Any]]:
        """List every notification subscription for the token's project.

        .. warning::
           **The service IGNORES ``event``** -- verified against a live stack,
           where a filtered request answers 200 with every subscription in the
           project. The parameter is still sent because the swagger documents
           it and a server-side fix would then cost nothing, but THIS METHOD
           DOES NOT NARROW. Callers that need narrowing must filter the
           returned list themselves; ``NotificationService`` does exactly that.

        Args:
            event: Optional event-name filter (e.g. ``job-failed``). Sent as
                ``?event=``; a falsy value is omitted entirely rather than sent
                as an empty parameter. See the warning above -- passing it does
                not reduce the result.

        Returns:
            List of subscription dicts verbatim from the API.
        """
        params = {"event": event} if event else {}
        response = self._notification_request("GET", "/project-subscriptions", params=params)
        return response.json()

    def get_project_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Return one subscription by ID.

        Args:
            subscription_id: Numeric-string subscription ID.

        Returns:
            The subscription dict verbatim from the API.
        """
        path = f"/project-subscriptions/{quote(str(subscription_id), safe='')}"
        return self._notification_request("GET", path).json()
