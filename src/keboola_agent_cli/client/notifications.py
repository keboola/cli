"""Flow Notification subscriptions -- GET /project-subscriptions.

New for issue #600. These are the per-flow **Notifications tab** recipients
(the bell icon in Flow Builder: Success / Error / Processing-delay cards), a
different mechanism from the in-flow ``type: "notification"`` task -- the
latter lives inside the flow's own ``configuration`` JSON and is already
visible through ``flow detail``, while these live in a separate service and
were previously unreachable from any CLI.

The service also exposes ``POST`` and ``DELETE /project-subscriptions``. They
are deliberately NOT wrapped: kbagent's notification surface is read-only,
and the GET-only dispatcher in ``_core.py`` makes that structural.
"""

from typing import Any

from ._core import _CoreClient


class _NotificationMixin(_CoreClient):
    """Read-only access to the project's notification subscriptions."""

    def list_project_subscriptions(self, event: str | None = None) -> list[dict[str, Any]]:
        """List every notification subscription for the token's project.

        ``GET /project-subscriptions`` on the ``notification.{stack-suffix}``
        host. The project is resolved server-side from the token, so there is
        no project parameter.

        ``event`` is passed through as the ``?event=`` filter. The service
        types it as a free-form string (not an enum), so no client-side
        allow-list is imposed here -- an unknown value is the server's 400 to
        raise, and a new event type works the day Keboola ships it. Known
        values are kebab-case: ``job-failed``, ``job-succeeded``,
        ``job-succeeded-with-warning``, ``job-processing-long`` and their
        ``phase-job-*`` counterparts.

        Returns the raw list verbatim; each item carries ``id``, ``event``,
        optional ``expiresAt``, optional ``filters`` (a list of
        ``{field, value, operator?}``) and a ``recipient`` whose shape depends
        on its ``channel``: ``email`` carries ``address``, ``webhook`` carries
        ``url``. Shaping happens in the service layer.
        """
        params = {"event": event} if event else None
        response = self._notification_get("/project-subscriptions", params=params)
        payload = response.json()
        # The endpoint is documented to answer with a bare array; tolerate a
        # wrapped shape rather than raising a TypeError deep in the service.
        if isinstance(payload, dict):
            wrapped = payload.get("subscriptions")
            return wrapped if isinstance(wrapped, list) else []
        return payload if isinstance(payload, list) else []
