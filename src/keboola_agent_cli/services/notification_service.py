"""Fleet-wide audit of Flow Notification subscriptions (issue #600).

Wraps ``KeboolaClient.list_project_subscriptions`` (``GET
/project-subscriptions`` on the derived ``notification.{stack}`` host) with
the fan-out / per-project-error shape every other multi-project service uses
(``ScheduleService`` is the closest sibling -- same question shape: "audit a
per-project sibling-service concept across every registered project, joined
against the config it points at").

These subscriptions are the Flow Builder **Notifications tab** (the bell
icon: Success / Error / Processing-delay cards), which lives in the
notification service and NOT in the flow's ``configuration`` JSON. The
in-flow ``type: "notification"`` task is a different mechanism entirely and
is already visible through ``flow detail``.

READ-ONLY BY DESIGN: the service also exposes ``POST`` / ``DELETE
/project-subscriptions``, which change who gets paged when production
breaks. Nothing here writes; the GET-only dispatcher in ``client/_core.py``
enforces the same restriction one layer down.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService, project_error_entry

logger = logging.getLogger(__name__)

# Filter fields the notification service matches subscriptions on. Verified
# against the service's own OpenAPI examples -- NOT the camelCase
# `configurationId` / `component` an outside reader would guess.
FILTER_FIELD_COMPONENT_ID = "job.component.id"
FILTER_FIELD_CONFIG_ID = "job.configuration.id"
FILTER_FIELD_BRANCH_ID = "branch.id"

# A subscription carrying no config filter fires for EVERY job in the
# project. That is a legitimate, and operationally important, state -- it is
# the "page me on any failure" catch-all -- so it must render as its own
# scope rather than as a flow with a missing name.
SCOPE_CONFIG = "config"
SCOPE_PROJECT_WIDE = "project-wide"


def _index_filters(filters: Any) -> dict[str, Any]:
    """Index a subscription's ``filters`` list by field name.

    The service models filters as ``[{field, value, operator?}]``. Only the
    identity fields are indexed by name here; a threshold filter such as
    ``durationOvertimePercentage >= 0.75`` is preserved verbatim in the row's
    ``filters`` key instead, since collapsing it to a scalar would drop the
    operator that gives it meaning.
    """
    indexed: dict[str, Any] = {}
    if not isinstance(filters, list):
        return indexed
    for entry in filters:
        if not isinstance(entry, dict):
            continue
        field = entry.get("field")
        if isinstance(field, str) and field:
            indexed[field] = entry.get("value")
    return indexed


def _recipient_address(recipient: Any) -> tuple[str, str]:
    """Return ``(channel, address)`` for either recipient shape.

    The service discriminates on ``channel``: an ``email`` recipient carries
    ``address``, a ``webhook`` recipient carries ``url``. Both are "where the
    notification goes", so they share one column -- keeping ``channel``
    alongside means the caller can still tell them apart.
    """
    if not isinstance(recipient, dict):
        return "", ""
    channel = str(recipient.get("channel", "") or "")
    address = recipient.get("address") or recipient.get("url") or ""
    return channel, str(address)


def _shape_subscription(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one raw subscription into the CLI-facing row (name unresolved)."""
    filters = raw.get("filters")
    indexed = _index_filters(filters)
    channel, address = _recipient_address(raw.get("recipient"))

    component_id = str(indexed.get(FILTER_FIELD_COMPONENT_ID) or "")
    config_id = str(indexed.get(FILTER_FIELD_CONFIG_ID) or "")
    branch_filter = indexed.get(FILTER_FIELD_BRANCH_ID)

    return {
        "subscription_id": str(raw.get("id", "")),
        "event": str(raw.get("event", "")),
        "scope": SCOPE_CONFIG if config_id else SCOPE_PROJECT_WIDE,
        "component_id": component_id,
        "config_id": config_id,
        "config_name": "",
        "branch_id": str(branch_filter) if branch_filter is not None else "",
        "channel": channel,
        "address": address,
        "expires_at": str(raw.get("expiresAt") or ""),
        # Kept verbatim so a threshold filter (durationOvertimePercentage with
        # its `>=` operator) or a field this version does not know about is
        # still auditable from `--json` output.
        "filters": filters if isinstance(filters, list) else [],
    }


class NotificationService(BaseService):
    """Fleet-wide discovery for notification subscriptions.

    Every method is read-only and accumulates per-project errors in the
    ``errors`` field of the returned dict rather than aborting the fan-out.
    """

    def list_subscriptions(
        self,
        aliases: list[str] | None = None,
        event: str | None = None,
        component_id: str | None = None,
        config_id: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List notification subscriptions across one, many, or all projects.

        Args:
            aliases: Project aliases to query. ``None`` / empty means every
                registered project.
            event: Passed to the service as the ``?event=`` filter. Free-form
                (the service types it as a string, not an enum); known values
                are kebab-case, e.g. ``job-failed``.
            component_id: Keep only subscriptions filtered to this component.
            config_id: Keep only subscriptions filtered to this configuration.
            branch_id: Keep only subscriptions carrying this ``branch.id``
                filter. NOTE this is a client-side filter over the returned
                rows, not a scoped request: the list endpoint is not
                branch-scoped and answers with every branch's subscriptions.

        Returns:
            ``{"subscriptions": [...], "errors": [...]}``. Each row carries
            ``project_alias``, ``subscription_id``, ``event``, ``scope``,
            ``component_id``, ``config_id``, ``config_name``, ``branch_id``,
            ``channel``, ``address``, ``expires_at`` and raw ``filters``.
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_subscriptions(
                alias,
                project,
                event=event,
                component_id=component_id,
                config_id=config_id,
                branch_id=branch_id,
            )

        successes, errors = self._run_parallel(projects, worker)

        subscriptions: list[dict[str, Any]] = []
        for result in successes:
            subscriptions.extend(result[1])
        subscriptions.sort(
            key=lambda s: (
                s.get("project_alias", ""),
                s.get("component_id", ""),
                (s.get("config_name") or "").lower(),
                s.get("event", ""),
                s.get("address", ""),
            )
        )
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {"subscriptions": subscriptions, "errors": errors}

    # ------------------------------------------------------------------

    def _fetch_project_subscriptions(
        self,
        alias: str,
        project: ProjectConfig,
        event: str | None,
        component_id: str | None,
        config_id: str | None,
        branch_id: int | None,
    ) -> tuple[Any, ...]:
        """Fetch, filter and name-resolve subscriptions for a single project.

        ``project.active_branch_id`` is deliberately NOT consulted. Every other
        branch-aware command treats a branch as a scope and inherits the
        project's active one; here it is one filter field on the subscription,
        so inheriting it would silently drop every production recipient from an
        audit run inside a project that happens to have a dev branch selected.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            raw_subscriptions = client.list_project_subscriptions(event=event)
            rows = [_shape_subscription(raw) for raw in raw_subscriptions if isinstance(raw, dict)]
            rows = _apply_row_filters(
                rows,
                component_id=component_id,
                config_id=config_id,
                branch_id=branch_id,
            )
            self._resolve_config_names(client, rows)
            for row in rows:
                row["project_alias"] = alias
            return (alias, rows, True)
        except KeboolaApiError as exc:
            return (alias, project_error_entry(alias, exc))
        except Exception as exc:
            # One project's failure must never abort the fan-out.
            return (alias, project_error_entry(alias, exc))
        finally:
            client.close()

    def _resolve_config_names(self, client: Any, rows: list[dict[str, Any]]) -> None:
        """Fill each row's ``config_name`` in place.

        One ``list_component_configs`` call per distinct (branch, component)
        pair actually referenced -- in practice one (production
        ``keboola.flow``), and none at all for a project whose subscriptions
        are all project-wide. The heavier ``list_components_with_configs``
        used by ``ScheduleService`` is deliberately avoided: it downloads
        every configuration BODY in the project (megabytes on the 276-flow
        fleet this issue came from) to recover a handful of names.

        Grouping by branch matters because a subscription may be filtered to a
        dev branch, whose configs are invisible from production -- looking
        that name up in the wrong branch would silently report it as deleted.

        Best-effort throughout: a lookup that fails leaves ``config_name``
        empty rather than failing the project. A subscription pointing at a
        deleted flow is a real state worth surfacing, not an error -- and it
        is exactly the kind of stale recipient an audit is looking for.
        """
        wanted = {
            (row["branch_id"], row["component_id"])
            for row in rows
            if row["component_id"] and row["config_id"]
        }
        names: dict[tuple[str, str, str], str] = {}
        for branch_key, component in sorted(wanted):
            try:
                configs = client.list_component_configs(
                    component, branch_id=int(branch_key) if branch_key else None
                )
            except (KeboolaApiError, ValueError) as exc:
                logger.debug(
                    "Config name lookup failed for %s (branch %s): %s",
                    component,
                    branch_key or "production",
                    exc,
                )
                continue
            for cfg in configs:
                if isinstance(cfg, dict):
                    names[(branch_key, component, str(cfg.get("id", "")))] = str(
                        cfg.get("name", "") or ""
                    )

        for row in rows:
            row["config_name"] = names.get(
                (row["branch_id"], row["component_id"], row["config_id"]), ""
            )


def _apply_row_filters(
    rows: list[dict[str, Any]],
    component_id: str | None,
    config_id: str | None,
    branch_id: int | None,
) -> list[dict[str, Any]]:
    """Apply the client-side row filters.

    The list endpoint takes only ``?event=``; component, config and branch
    are matched here against the subscription's own filter fields.

    A ``branch_id`` request keeps ONLY subscriptions carrying that
    ``branch.id`` filter. Production subscriptions carry no branch filter at
    all, so they are not silently folded into a dev-branch view.
    """
    result = rows
    if component_id:
        result = [r for r in result if r["component_id"] == component_id]
    if config_id:
        result = [r for r in result if r["config_id"] == config_id]
    if branch_id is not None:
        wanted = str(branch_id)
        result = [r for r in result if r["branch_id"] == wanted]
    return result
