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

Write path (issue #690): ``create_project_subscription`` / ``delete_project_subscription``
mirror the list/get shape above -- no shaping, no validation beyond what the
service itself rejects.
"""

from typing import Any
from urllib.parse import quote

from ._core import _CoreClient


class _NotificationsMixin(_CoreClient):
    """Notification Service: read/write access to project notification subscriptions."""

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

    def create_project_subscription(
        self,
        event: str,
        recipient: dict[str, Any],
        filters: list[dict[str, Any]] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a notification subscription for the token's project.

        Args:
            event: Kebab-case event name (e.g. ``job-failed``); the service
                treats ``EventName`` as an open string, so nothing here
                validates it against a fixed set.
            recipient: Discriminated on ``channel`` -- an email recipient
                carries ``address``, a webhook recipient carries ``url``.
            filters: Optional list of ``{"field": ..., "value": ...}`` (or
                ``operator``-bearing) dicts; omitted from the body entirely
                when falsy rather than sent as an empty list.
            expires_at: Optional ISO-8601 expiry. Sent on the wire as
                ``expiresAt`` (camelCase) -- the swagger's field name, not
                this method's snake_case parameter.

        Returns:
            The created subscription dict verbatim from the API.
        """
        body: dict[str, Any] = {"event": event, "recipient": recipient}
        if filters:
            body["filters"] = filters
        if expires_at:
            body["expiresAt"] = expires_at
        return self._notification_request("POST", "/project-subscriptions", json=body).json()

    def delete_project_subscription(self, subscription_id: str) -> None:
        """Delete one subscription by ID.

        Args:
            subscription_id: Numeric-string subscription ID.
        """
        path = f"/project-subscriptions/{quote(str(subscription_id), safe='')}"
        self._notification_request("DELETE", path)
