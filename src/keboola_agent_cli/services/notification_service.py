"""Notification subscription discovery, audit, and write service (issue #600, #690).

Fleet-wide queries over the Notification Service's ``/project-subscriptions``,
joined in memory against the project's component configurations so callers can
answer "which flow does this alert belong to, and who does it page?" across
every registered project at once. Since issue #690 it also owns the write
path -- create, delete, and replace-recipient -- so scripts can fix a stale
recipient without a trip through the web UI.

These subscriptions back the Flow Builder's *Notifications* tab (bell icon).
They are NOT stored in a flow's ``configuration`` JSON, so ``flow detail`` /
``config detail`` never showed them -- that was the last unauditable
notification surface in a fleet-wide "are our alert recipients still valid"
sweep.

Two wire-format traps this module exists to absorb, both taken from the
service's public swagger:

- ``filters[].field`` values are dotted paths into the *event payload*
  (``job.component.id``, ``job.configuration.id``, ``branch.id``, ``phase.id``,
  ``durationOvertimePercentage``), not flat keys like ``configurationId``.
- ``recipient`` is discriminated on ``channel``: email carries ``address``,
  webhook carries ``url``. A single ``address`` column has to read both.

``filters`` is optional (only ``event`` and ``recipient`` are required), so a
project-wide catch-all subscription with no filters at all is legal and common
-- every join here degrades to "project-wide, no specific config" rather than
erroring or dropping the row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..errors import ConfigError, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)

# Dotted paths into the event payload, used as ``filters[].field`` values.
FILTER_FIELD_COMPONENT_ID = "job.component.id"
FILTER_FIELD_CONFIG_ID = "job.configuration.id"
FILTER_FIELD_BRANCH_ID = "branch.id"
FILTER_FIELD_PHASE_ID = "phase.id"

# ``recipient.channel`` values the write path accepts. The CLI layer
# validates against the same tuple (defense in depth, not the primary guard --
# see ``_build_recipient``).
CHANNEL_EMAIL = "email"
CHANNEL_WEBHOOK = "webhook"
VALID_CHANNELS = (CHANNEL_EMAIL, CHANNEL_WEBHOOK)

# ``EventName`` is an open string in the schema, not an enum -- this tuple is
# for help text and hints only. Never reject an event name against it: the
# service is free to add events without a kbagent release.
KNOWN_EVENTS = (
    "job-failed",
    "job-succeeded",
    "job-succeeded-with-warning",
    "job-processing-long",
    "phase-job-failed",
    "phase-job-succeeded",
    "phase-job-succeeded-with-warning",
    "phase-job-processing-long",
)

SCOPE_CONFIG = "config"
SCOPE_PROJECT_WIDE = "project-wide"


def _filter_value(filters: list[Any], field: str) -> str:
    """Return the stringified value of the first filter matching ``field``.

    Filter values are declared as ``string | integer | boolean``, so every
    value is stringified here -- a numeric config ID must not reach the
    output columns (or an ``==`` comparison against a CLI option) as an int.
    """
    for item in filters:
        if isinstance(item, dict) and item.get("field") == field:
            value = item.get("value")
            return "" if value is None else str(value)
    return ""


def _recipient_address(recipient: dict[str, Any]) -> str:
    """Return a recipient's destination regardless of channel.

    Email recipients carry ``address``, webhook recipients carry ``url``. An
    unknown future channel yields an empty string rather than raising -- the
    row is still worth showing for its event and filters.
    """
    return str(recipient.get("address") or recipient.get("url") or "")


def _build_recipient(channel: str, address: str) -> dict[str, str]:
    """Build a ``recipient`` body for the given channel.

    Args:
        channel: One of :data:`VALID_CHANNELS`.
        address: Email address (``email``) or callback URL (``webhook``).

    Returns:
        ``{"channel": ..., "address": ...}`` for email, ``{"channel": ...,
        "url": ...}`` for webhook -- mirroring the discriminated shape
        :func:`_recipient_address` reads back.

    Raises:
        ConfigError: ``channel`` is outside :data:`VALID_CHANNELS`. The CLI
            layer validates this too (an explicit membership check against
            ``VALID_CHANNELS`` in the command, which exits 2 with
            ``INVALID_ARGUMENT``); this check is defense in depth for any
            other caller (SDK, tests) that skips it.
    """
    if channel == CHANNEL_EMAIL:
        return {"channel": CHANNEL_EMAIL, "address": address}
    if channel == CHANNEL_WEBHOOK:
        return {"channel": CHANNEL_WEBHOOK, "url": address}
    raise ConfigError(
        f"Unknown notification channel '{channel}'. Valid channels: {', '.join(VALID_CHANNELS)}."
    )


def _build_filters(
    component_id: str | None,
    config_id: str | None,
    branch_id: int | None,
) -> list[dict[str, Any]]:
    """Build ``filters[]`` in a stable component/config/branch order.

    Each scope argument is included only when provided, and every value is
    stringified -- filter values are declared as ``string | integer |
    boolean`` on the wire, and a numeric config/branch ID must not reach the
    API as a raw int (mirrors the read-side stringification in
    :func:`_filter_value`).
    """
    filters: list[dict[str, Any]] = []
    if component_id:
        filters.append({"field": FILTER_FIELD_COMPONENT_ID, "value": str(component_id)})
    if config_id:
        filters.append({"field": FILTER_FIELD_CONFIG_ID, "value": str(config_id)})
    if branch_id is not None:
        filters.append({"field": FILTER_FIELD_BRANCH_ID, "value": str(branch_id)})
    return filters


def _extract_subscription_fields(sub: dict[str, Any]) -> dict[str, Any]:
    """Project a raw subscription dict into the canonical audit row.

    ``config_name`` is deliberately left unset -- the service joins it
    separately once the project's configurations have been fetched.
    """
    raw_filters = sub.get("filters") or []
    filters = raw_filters if isinstance(raw_filters, list) else []
    recipient = sub.get("recipient") or {}
    if not isinstance(recipient, dict):
        recipient = {}

    config_id = _filter_value(filters, FILTER_FIELD_CONFIG_ID)

    return {
        "subscription_id": str(sub.get("id", "")),
        "event": str(sub.get("event", "")),
        "component_id": _filter_value(filters, FILTER_FIELD_COMPONENT_ID),
        "config_id": config_id,
        "branch_id": _filter_value(filters, FILTER_FIELD_BRANCH_ID),
        "phase_id": _filter_value(filters, FILTER_FIELD_PHASE_ID),
        "channel": str(recipient.get("channel", "")),
        "address": _recipient_address(recipient),
        "expires_at": str(sub.get("expiresAt", "") or ""),
        # A subscription can filter on fields with no dedicated column
        # (durationOvertimePercentage, and anything the service adds later),
        # so the raw list rides along for --json consumers.
        "filters": filters,
        "scope": SCOPE_CONFIG if config_id else SCOPE_PROJECT_WIDE,
    }


@dataclass
class ConfigNameIndex:
    """Lookup tables for resolving a subscription's parent configuration name.

    ``by_pair`` is the authoritative index. ``names_by_config_id`` exists only
    for the fallback path -- a subscription filtering on
    ``job.configuration.id`` with no component filter -- and holds every name
    seen for a config ID so an ambiguous match can be detected rather than
    guessed at.
    """

    by_pair: dict[tuple[str, str], str] = field(default_factory=dict)
    names_by_config_id: dict[str, list[str]] = field(default_factory=dict)


def _build_config_indexes(components: list[dict[str, Any]]) -> ConfigNameIndex:
    """Index a ``list_components_with_configs`` payload for name resolution."""
    index = ConfigNameIndex()

    for comp in components:
        comp_id = str(comp.get("id", ""))
        for cfg in comp.get("configurations", []) or []:
            cfg_id = str(cfg.get("id", ""))
            name = str(cfg.get("name", "") or "")
            index.by_pair[(comp_id, cfg_id)] = name
            index.names_by_config_id.setdefault(cfg_id, []).append(name)

    return index


def _resolve_config_name(row: dict[str, Any], index: ConfigNameIndex) -> str:
    """Resolve a row's parent configuration name, or "" when not determinable.

    Exact ``(component, config)`` match wins. When the subscription filters
    only on a config ID, fall back to a config-ID lookup -- but only when it
    is unambiguous. Two components sharing a config ID must not be guessed
    between: a wrong flow name in an alert audit is worse than a blank one.
    """
    config_id = row["config_id"]
    if not config_id:
        return ""

    component_id = row["component_id"]
    if component_id:
        return index.by_pair.get((component_id, config_id), "")

    candidates = index.names_by_config_id.get(config_id, [])
    return candidates[0] if len(candidates) == 1 else ""


def _matches_scope(
    row: dict[str, Any],
    component_id: str | None,
    config_id: str | None,
) -> bool:
    """Strict AND match of a row against the component / config filters."""
    if component_id is not None and row["component_id"] != component_id:
        return False
    return config_id is None or row["config_id"] == config_id


def _could_also_fire(
    row: dict[str, Any],
    component_id: str | None,
    config_id: str | None,
) -> bool:
    """Would a DROPPED row still fire for the scope being audited?

    True only when the mismatch comes from an ABSENT constraint on the row --
    that is the genuinely ambiguous case the exclusion counter warns about. A
    subscription carrying its own explicit filter for a *different* component
    can never fire for the audited one, so counting it would be noise in
    exactly the incident-response workflow this command exists for.
    """
    if component_id is not None and row["component_id"] and row["component_id"] != component_id:
        return False
    return not (config_id is not None and row["config_id"] and row["config_id"] != config_id)


class NotificationService(BaseService):
    """Audit and write queries over Notification Service subscriptions.

    ``list_subscriptions`` / ``get_subscription_detail`` are read-only. Since
    issue #690, ``create_subscription`` / ``delete_subscription`` /
    ``replace_subscription_recipient`` add a write path.
    """

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

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
            event: Event-name filter. Sent as ``?event=`` AND applied
                client-side, because the service ignores the parameter -- see
                the comment in :meth:`_fetch_project_subscriptions`.
            component_id: Client-side filter on the subscription's
                ``job.component.id`` filter value (the API has no such filter).
            config_id: Client-side filter on ``job.configuration.id``.
            branch_id: Branch used to resolve configuration names. Subscriptions
                themselves are project-level; a branch-scoped one carries a
                ``branch.id`` filter, surfaced as the ``branch_id`` column.

        Returns:
            ``{"subscriptions": [...], "errors": [...],
            "project_wide_excluded": int}``. The counter reports how many
            filter-less catch-all subscriptions a ``component_id`` /
            ``config_id`` filter dropped -- those also fire for the config
            being audited, so silently omitting them would answer the
            "who gets paged" question wrongly.
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
        project_wide_excluded = 0
        for result in successes:
            subscriptions.extend(result[1])
            project_wide_excluded += result[2]

        subscriptions.sort(
            key=lambda s: (
                s.get("project_alias", ""),
                s.get("event", ""),
                (s.get("config_name") or "").lower(),
                s.get("subscription_id", ""),
            )
        )
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "subscriptions": subscriptions,
            "errors": errors,
            "project_wide_excluded": project_wide_excluded,
        }

    # ------------------------------------------------------------------
    # detail
    # ------------------------------------------------------------------

    def get_subscription_detail(
        self,
        alias: str,
        subscription_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the full audit row for a single subscription.

        Raises:
            ConfigError: alias not registered.
            KeboolaApiError: subscription not found or permission denied.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            row = _extract_subscription_fields(client.get_project_subscription(subscription_id))
            row["project_alias"] = alias
            self._stamp_config_names(client, [row], effective_branch)
            return row
        finally:
            client.close()

    # ------------------------------------------------------------------
    # write path (issue #690)
    # ------------------------------------------------------------------

    def create_subscription(
        self,
        alias: str,
        event: str,
        channel: str,
        address: str,
        component_id: str | None = None,
        config_id: str | None = None,
        branch_id: int | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a notification subscription and return its audit row.

        Args:
            alias: Registered project alias.
            event: Kebab-case event name (see ``KNOWN_EVENTS`` for hints --
                the service treats ``EventName`` as an open string, so this
                is never validated against a fixed set).
            channel: ``"email"`` or ``"webhook"`` -- see :data:`VALID_CHANNELS`.
            address: Email address (``email``) or callback URL (``webhook``).
            component_id: Optional ``job.component.id`` filter.
            config_id: Optional ``job.configuration.id`` filter.
            branch_id: Optional ``branch.id`` filter.
            expires_at: Optional ISO-8601 expiry, sent on the wire as
                ``expiresAt``.

        Returns:
            The same shape as :meth:`get_subscription_detail`: the created
            subscription projected through ``_extract_subscription_fields``,
            with ``project_alias`` and a best-effort ``config_name`` stamped.

        Raises:
            ConfigError: ``alias`` not registered, or ``channel`` invalid.
            KeboolaApiError: the API rejected the subscription.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = branch_id or project.active_branch_id

        recipient = _build_recipient(channel, address)
        filters = _build_filters(component_id, config_id, branch_id)

        client = self._client_factory(project.stack_url, project.token)
        try:
            created = client.create_project_subscription(
                event, recipient, filters or None, expires_at
            )
            row = _extract_subscription_fields(created)
            row["project_alias"] = alias
            self._stamp_config_names(client, [row], effective_branch)
            return row
        finally:
            client.close()

    def delete_subscription(self, alias: str, subscription_id: str) -> dict[str, Any]:
        """Delete a notification subscription.

        Args:
            alias: Registered project alias.
            subscription_id: Numeric-string subscription ID.

        Returns:
            ``{"project_alias": alias, "subscription_id": str(subscription_id),
            "deleted": True}``.

        Raises:
            ConfigError: ``alias`` not registered.
            KeboolaApiError: the subscription was not found (404), or the API
                rejected the delete -- propagated verbatim, never swallowed.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_project_subscription(subscription_id)
            return {
                "project_alias": alias,
                "subscription_id": str(subscription_id),
                "deleted": True,
            }
        finally:
            client.close()

    def replace_subscription_recipient(
        self,
        alias: str,
        subscription_id: str,
        new_address: str,
        new_channel: str | None = None,
    ) -> dict[str, Any]:
        """Swap a subscription's recipient by creating a new one, then deleting the old.

        CREATE-then-DELETE, deliberately the reverse of the delete-then-recreate
        sketch in the originating issue. The Notification Service has no
        in-place recipient PATCH -- creating always mints a fresh ID -- so the
        two steps can never be one atomic operation, and the ordering decides
        which failure mode you get. Creating first means a failed second step
        (the delete) leaves the OLD subscription lingering alongside the new
        one: a duplicate alert, recoverable with a follow-up
        :meth:`delete_subscription` call. Deleting first would instead risk
        losing the alert entirely if the create then failed -- unrecoverable,
        and silently so. A failed delete therefore does NOT raise -- ANY
        exception, not just :class:`KeboolaApiError` (a raw transport failure
        such as ``httpx.RemoteProtocolError`` is just as possible, and letting
        it escape here would lose ``new_subscription_id`` from the response
        entirely, inviting a retry that mints a THIRD subscription): it is
        reported via ``old_deleted: False`` plus a warning naming the old
        subscription id, leaving the caller to decide whether to retry.

        The old subscription's ``event``, raw ``filters``, and ``expiresAt``
        are carried over VERBATIM (no rebuild) -- only the recipient changes.
        ``new_channel=None`` keeps the old subscription's channel; if that
        channel is empty or unknown and no explicit ``new_channel`` is given,
        this raises rather than guess at a destination.

        Args:
            alias: Registered project alias.
            subscription_id: The subscription whose recipient is being replaced.
            new_address: New email address or webhook URL.
            new_channel: Optional channel override. ``None`` keeps the old
                subscription's channel.

        Returns:
            ``{"old_subscription_id", "new_subscription_id", "old_address",
            "old_deleted": bool, "warnings": [str, ...], **new_audit_row}`` --
            the new subscription's audit row (same shape as
            :meth:`get_subscription_detail`) merged with the replace-specific
            keys above. ``new_subscription_id`` is ALWAYS the fresh id the API
            minted -- a caller must not keep using the old one.

        Raises:
            ConfigError: ``alias`` not registered, ``new_channel`` invalid, or
                the old recipient's channel is empty/unknown with no
                ``new_channel`` override.
            KeboolaApiError: fetching the old subscription or creating the new
                one failed. A failure to delete the old one is caught (any
                exception, not just this one) and reported instead -- see
                above.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            old = client.get_project_subscription(subscription_id)
            old_recipient = old.get("recipient") or {}
            if not isinstance(old_recipient, dict):
                old_recipient = {}
            old_address = _recipient_address(old_recipient)
            channel = new_channel or str(old_recipient.get("channel", ""))
            if not channel:
                raise ConfigError(
                    "The old subscription's recipient channel is empty or "
                    "unknown; pass an explicit channel to replace it."
                )

            recipient = _build_recipient(channel, new_address)
            created = client.create_project_subscription(
                str(old.get("event", "")),
                recipient,
                old.get("filters") or None,
                old.get("expiresAt"),
            )

            warnings: list[str] = []
            try:
                client.delete_project_subscription(subscription_id)
                old_deleted = True
            except Exception as exc:
                # Deliberately broad: the new subscription already exists at
                # this point, so ANY failure here -- a KeboolaApiError, or a
                # raw transport failure (httpx.ReadError / WriteError /
                # RemoteProtocolError / ProxyError, none of which
                # ``_do_request`` wraps) -- must be swallowed the same way.
                # Letting one of those escape would lose ``new_subscription_id``
                # from the response entirely, and a caller's retry would then
                # mint a THIRD subscription -- exactly what create-first
                # ordering exists to avoid.
                old_deleted = False
                logger.warning(
                    "Failed to delete old notification subscription %s after "
                    "replacing its recipient: %s",
                    subscription_id,
                    exc,
                )
                warnings.append(
                    f"Old subscription {subscription_id} was not deleted "
                    f"(a duplicate now exists alongside the new one): {exc}"
                )

            row = _extract_subscription_fields(created)
            row["project_alias"] = alias
            self._stamp_config_names(client, [row], effective_branch)

            return {
                "old_subscription_id": str(subscription_id),
                "new_subscription_id": row["subscription_id"],
                "old_address": old_address,
                "old_deleted": old_deleted,
                "warnings": warnings,
                **row,
            }
        finally:
            client.close()

    # ------------------------------------------------------------------
    # internals
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
        """Fetch, filter and name-join subscriptions for a single project.

        Returns one of TWO shapes, per the ``BaseService._run_parallel``
        convention: ``(alias, rows, excluded, True)`` on success, or the
        2-tuple ``(alias, error_dict)`` on failure. The arity difference IS
        the discriminator -- ``_run_parallel`` sorts on ``len(result) == 2``
        -- so neither shape may grow or shrink independently of the other.
        """
        effective_branch = branch_id or project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw = client.list_project_subscriptions(event=event)
            rows = [_extract_subscription_fields(sub) for sub in raw]

            # The live service IGNORES ``?event=`` -- verified against a real
            # stack, where a filtered request answers 200 with every
            # subscription in the project. The parameter is still sent (the
            # swagger documents it, and a server-side fix would then cost
            # nothing), but the narrowing has to happen here or --event
            # answers "who gets paged on failure" with a superset that
            # includes success recipients.
            if event:
                rows = [row for row in rows if row["event"] == event]

            # Count only rows the filter actually DROPPED. A subscription
            # filtering on job.component.id alone is labelled project-wide
            # (scope keys off the config filter), but --component-id KEEPS
            # it -- warning that it was hidden would contradict the table
            # the user is looking at.
            excluded = 0
            if component_id is not None or config_id is not None:
                kept: list[dict[str, Any]] = []
                for row in rows:
                    if _matches_scope(row, component_id, config_id):
                        kept.append(row)
                    elif row["scope"] == SCOPE_PROJECT_WIDE and _could_also_fire(
                        row, component_id, config_id
                    ):
                        excluded += 1
                rows = kept

            for row in rows:
                row["project_alias"] = alias
            self._stamp_config_names(client, rows, effective_branch)

            # Success shape: 4-tuple. See the arity contract in the docstring.
            return (alias, rows, excluded, True)
        except KeboolaApiError as exc:
            # Failure shape: 2-tuple, which is how _run_parallel detects it.
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        except Exception as exc:
            # Failure shape: 2-tuple, which is how _run_parallel detects it.
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
            )
        finally:
            client.close()

    @staticmethod
    def _stamp_config_names(
        client: Any,
        rows: list[dict[str, Any]],
        branch_id: int | None,
    ) -> None:
        """Stamp ``config_name`` on every row, in place.

        The single ``list_components_with_configs`` round-trip is skipped
        entirely when no row is config-scoped: its payload is proportional to
        the whole project, and a project full of catch-all subscriptions would
        otherwise pay for a join that can only ever produce blanks.

        Which listing call is used depends on what the rows actually need.
        ``list_components_with_configs`` sends ``include=configuration,rows``
        -- every config body and row in the project -- which is a steep price
        for a display name, and it is paid once per project in the fan-out.
        When every config-scoped row names its component (the common case:
        a Notifications-tab subscription filters on both), one cheap
        per-component listing answers the same question. Only a subscription
        filtering on a bare config ID needs the whole-project view, because
        resolving that ID means searching every component.

        A failing lookup is swallowed: ``config_name`` is a display nicety,
        and losing it must never hide the recipient the audit came for.
        """
        for row in rows:
            row["config_name"] = ""

        scoped = [row for row in rows if row["config_id"]]
        if not scoped:
            return

        index = ConfigNameIndex()
        try:
            if any(not row["component_id"] for row in scoped):
                index = _build_config_indexes(
                    client.list_components_with_configs(branch_id=branch_id)
                )
            else:
                for component_id in sorted({row["component_id"] for row in scoped}):
                    for cfg in client.list_component_configs(component_id, branch_id=branch_id):
                        index.by_pair[(component_id, str(cfg.get("id", "")))] = str(
                            cfg.get("name", "") or ""
                        )
        except Exception as exc:
            logger.debug("Config-name join failed for notification subscriptions: %s", exc)
            return

        for row in rows:
            row["config_name"] = _resolve_config_name(row, index)


__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_WEBHOOK",
    "FILTER_FIELD_BRANCH_ID",
    "FILTER_FIELD_COMPONENT_ID",
    "FILTER_FIELD_CONFIG_ID",
    "FILTER_FIELD_PHASE_ID",
    "KNOWN_EVENTS",
    "SCOPE_CONFIG",
    "SCOPE_PROJECT_WIDE",
    "VALID_CHANNELS",
    "NotificationService",
]
