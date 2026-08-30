"""Storage API table triggers (issue #714).

A Keboola flow can be started automatically in more than one way, and only one
of them is a cron schedule. A **table trigger** fires a configuration whenever
one of a set of watched tables is imported into, subject to a cooldown. It is
NOT a component configuration -- it lives in its own Storage API resource --
which is why ``schedule list`` / ``search`` never saw it, and why a flow that
was demonstrably running could be reported as having "no trigger".

Wire-format notes, taken from the Storage API's own controller
(``Controller/Storage/Triggers/TriggerListAction``) rather than guessed:

- ``GET /v2/storage/triggers`` accepts ``?component=`` and
  ``?configurationId=``; ``configurationId`` is cast to string server-side, so
  a numeric id is accepted either way.
- A trigger carries ``id``, ``runWithTokenId``, ``component``,
  ``configurationId``, ``coolDownPeriodMinutes``, ``tables[].tableId``,
  ``creatorToken{id,description}`` and a nullable ``lastRun``
  (``null`` = never fired, which is NOT the same as disabled).
- ``component`` holds a component id, and the API's own example is the legacy
  ``orchestration`` -- do not assume every trigger names ``keboola.flow``.
- **The route is production-only**: it is declared
  ``isAvailableInBranch: false, isAvailableWithoutBranch: true``, so there is
  no branch-scoped variant to call and a dev branch cannot be queried. Callers
  must not advertise a branch-scoped answer here.
- **Listing needs no elevated privilege**: the list/detail routes are
  ``AsReadOnlyAction`` and scope only by the token's project -- even a
  read-only Storage token sees every trigger in the project (verified by the
  server's own ``testTriggersRestrictionsForReadOnlyUser`` E2E test). The
  project token kbagent already holds is always sufficient here.

The server DOES apply both filters (``TriggerRepository::findAllByFilter`` --
exact match, AND-ed), so the service layer's re-narrowing is defense in depth,
not a workaround: this codebase has been burned by an accepted-then-ignored
query filter before (the Notification Service and ``?event=``, issue #600),
and the second pass is one cheap iteration over a single-digit list.
"""

from typing import Any

from ._core import _CoreClient


class _TriggersMixin(_CoreClient):
    """Storage API: read access to table triggers."""

    def list_triggers(
        self,
        component: str | None = None,
        configuration_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the project's table triggers, optionally narrowed.

        Always queries **production** -- the Storage route has no branch-scoped
        variant (see the module docstring), so there is no ``branch_id``
        parameter to pass and a dev branch's triggers cannot be listed.

        Args:
            component: Optional component-id filter, sent as ``?component=``.
            configuration_id: Optional configuration-id filter, sent as
                ``?configurationId=``. Coerced to ``str`` because flow ids are
                strings in kbagent but numeric elsewhere.

        Returns:
            List of trigger dicts verbatim from the API. Do NOT treat the
            filters as authoritative -- narrow the result again in the caller.
        """
        params: dict[str, str] = {}
        if component:
            params["component"] = component
        if configuration_id:
            params["configurationId"] = str(configuration_id)
        response = self._request("GET", "/v2/storage/triggers", params=params)
        return response.json()
