"""Route-to-operation map that puts the REST surface behind the session firewall.

Issue #655: ``PermissionEngine`` used to be built only in the Typer callback,
so a persisted ``permissions set --mode deny`` policy -- and both
``--deny-writes`` / ``--deny-destructive`` -- protected the CLI process and
nothing else. ``kbagent serve`` exposed every route, ``DELETE
/storage/buckets`` included, behind one all-or-nothing bearer token.

#677 built the enforcement machinery (an engine on ``app.state``, a
``PermissionDeniedError`` -> HTTP 403 handler, and the
:func:`~keboola_agent_cli.server.dependencies.require_permission` dependency)
but wired it to three ``/auth/*`` routes only. This module supplies the
missing half: coverage.

Why one central table instead of 231 per-route declarations
-----------------------------------------------------------
FastAPI puts the matched route object into ``request.scope["route"]`` *before*
dependencies run, so a single app-level dependency can look the request up by
``(method, route.path)``. That buys two things a scattered declaration cannot:

1. **One auditable screen.** A reviewer answering "what can a caller still do
   under ``--deny-destructive``?" reads one file, not thirty routers.
2. **Fail-closed by construction.** A route with no entry is *denied*, not
   silently allowed -- the failure mode of a forgotten annotation is a loud
   403, never an open door. ``tests/test_server_route_permissions.py`` keeps
   that path unreachable in practice by asserting the table covers the live
   app exactly, in both directions.

The per-route form still works and still wins: a route declaring
``Depends(require_permission(...))`` inline is enforced by that dependency and
skipped here (see :func:`resolve_route_operation`). That is what keeps the
``/auth/*`` routes from #677 -- and test-only probe routes registered after
``create_app`` -- working unchanged.

Granularity caveat
------------------
A few CLI operations are finer-grained than the route that mirrors them.
``POST /semantic-layer/items/{kind}`` covers ``metric``/``dataset``/... in one
route, so it maps to the collapsed parent key ``semantic-layer.add`` rather
than ``semantic-layer.add.metric``. A policy naming only the leaf key is
therefore enforced on the CLI but not over REST; name the parent (or
``cli:write``) to cover both. Documented in ``docs/web-server.md``.
"""

from __future__ import annotations

from fastapi import Depends, Request

from ..permissions import OPERATION_REGISTRY, PermissionEngine
from .dependencies import get_permission_engine

# Paths served without a permission check.
#
# All of them are bootstrap surface: they carry no project data and no side
# effects, and denying them would leave a caller unable to discover *why* it
# is being denied. ``/health/ping``, ``/health/auth-info``, ``/docs``,
# ``/redoc`` and ``/openapi.json`` are already unauthenticated
# (``server/auth.py``'s ``PUBLIC_PATHS``); ``/ui-config`` and the SPA shell
# routes still require the bearer token, they just carry no policy decision.
#
# FastAPI's own ``/docs``, ``/redoc`` and ``/openapi.json`` are plain Starlette
# routes registered inside ``FastAPI.__init__``, so the app-level dependency
# never attaches to them at all. They are listed anyway: the exemption should
# read as a decision, not as an accident of registration order.
UNGUARDED_PATHS: frozenset[str] = frozenset(
    {
        "/health/ping",
        "/health/auth-info",
        "/ui-config",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        # `kbagent serve --ui` SPA shell (see server/__init__.py::_install_ui).
        "/",
        "/index.html",
    }
)

# (HTTP method, route path template) -> OPERATION_REGISTRY key.
#
# The path is ``APIRoute.path`` verbatim -- including ``:path`` converters --
# so an entry can be copy-pasted from, and grepped against, the router
# decorator it mirrors.
ROUTE_OPERATIONS: dict[tuple[str, str], str] = {
    # ── health / meta ────────────────────────────────────────────────
    ("GET", "/version"): "version",
    ("GET", "/changelog"): "changelog",
    ("GET", "/doctor"): "doctor",
    ("GET", "/permissions/show"): "permissions.show",
    # ── projects ─────────────────────────────────────────────────────
    ("GET", "/projects"): "project.list",
    ("POST", "/projects"): "project.add",
    # Serve-only bulk form of `project remove`; same blast radius, so the
    # same admin-class key rather than a weaker one of its own.
    ("POST", "/projects/bulk-delete"): "project.remove",
    ("DELETE", "/projects/{alias}"): "project.remove",
    ("PATCH", "/projects/{alias}"): "project.edit",
    ("GET", "/projects/status"): "project.status",
    ("GET", "/projects/current"): "project.current",
    ("POST", "/projects/use/{alias}"): "project.use",
    ("GET", "/projects/{alias}/info"): "project.info",
    ("GET", "/projects/{alias}/description"): "project.description-get",
    ("PUT", "/projects/{alias}/description"): "project.description-set",
    # ── members ──────────────────────────────────────────────────────
    ("GET", "/members/{project}"): "project.member-list",
    ("GET", "/members/{project}/invitations"): "project.invitation-list",
    ("POST", "/members/{project}/invite"): "project.invite",
    ("POST", "/members/{project}/invitations/cancel"): "project.invitation-cancel",
    ("POST", "/members/{project}/remove"): "project.member-remove",
    ("POST", "/members/{project}/set-role"): "project.member-set-role",
    # ── feature flags (super-admin manage token) ─────────────────────
    ("GET", "/feature/{project}/list"): "feature.list",
    ("GET", "/feature/{project}/project-show"): "feature.project-show",
    ("POST", "/feature/{project}/project-add"): "feature.project-add",
    ("POST", "/feature/{project}/project-remove"): "feature.project-remove",
    ("GET", "/feature/{project}/user-show"): "feature.user-show",
    ("POST", "/feature/{project}/user-add"): "feature.user-add",
    ("POST", "/feature/{project}/user-remove"): "feature.user-remove",
    # ── billing ──────────────────────────────────────────────────────
    ("GET", "/billing/credits"): "billing.credits",
    # ── configurations ───────────────────────────────────────────────
    ("GET", "/configs"): "config.list",
    ("GET", "/configs/search"): "config.search",
    ("GET", "/configs/examples/{component_id}"): "config.examples",
    ("GET", "/configs/trash/{project}"): "config.trash-list",
    ("GET", "/configs/{project}/{component_id}/{config_id}"): "config.detail",
    ("PATCH", "/configs/{project}/{component_id}/{config_id}"): "config.update",
    ("DELETE", "/configs/{project}/{component_id}/{config_id}"): "config.delete",
    ("POST", "/configs/{project}/{component_id}/{config_id}/restore"): "config.restore",
    ("POST", "/configs/{project}/{component_id}"): "config.new",
    ("POST", "/configs/{project}/{component_id}/{config_id}/clone"): "config.clone",
    (
        "POST",
        "/configs/{project}/{component_id}/{config_id}/set-default-bucket",
    ): "config.set-default-bucket",
    ("POST", "/configs/{project}/{component_id}/{config_id}/rename"): "config.rename",
    ("GET", "/configs/{project}/{component_id}/{config_id}/metadata"): "config.metadata-list",
    ("GET", "/configs/{project}/{component_id}/{config_id}/metadata/{key}"): "config.get-metadata",
    ("PUT", "/configs/{project}/{component_id}/{config_id}/metadata/{key}"): "config.set-metadata",
    (
        "DELETE",
        "/configs/{project}/{component_id}/{config_id}/metadata/{metadata_id}",
    ): "config.delete-metadata",
    ("POST", "/configs/{project}/{component_id}/{config_id}/folder"): "config.set-folder",
    ("POST", "/configs/{project}/{component_id}/{config_id}/rows"): "config.row-create",
    ("PATCH", "/configs/{project}/{component_id}/{config_id}/rows/{row_id}"): "config.row-update",
    ("DELETE", "/configs/{project}/{component_id}/{config_id}/rows/{row_id}"): "config.row-delete",
    ("GET", "/configs/{project}/{component_id}/{config_id}/oauth-url"): "config.oauth-url",
    ("GET", "/configs/{project}/{component_id}/{config_id}/state"): "config.state-get",
    ("PUT", "/configs/{project}/{component_id}/{config_id}/state"): "config.state-set",
    ("GET", "/configs/{project}/{component_id}/{config_id}/variables"): "config.variables-get",
    ("PUT", "/configs/{project}/{component_id}/{config_id}/variables"): "config.variables-set",
    (
        "DELETE",
        "/configs/{project}/{component_id}/{config_id}/variables",
    ): "config.variables-clear",
    # ── components ───────────────────────────────────────────────────
    ("GET", "/components"): "component.list",
    ("GET", "/components/{component_id}"): "component.detail",
    # Scaffolding writes a new configuration (`config new --push`).
    ("POST", "/components/{component_id}/scaffold"): "config.new",
    ("POST", "/components/{component_id}/actions/{action}"): "component.sync-action",
    # ── storage: buckets ─────────────────────────────────────────────
    ("GET", "/storage/buckets"): "storage.buckets",
    ("GET", "/storage/buckets/{project}/{bucket_id:path}"): "storage.bucket-detail",
    ("POST", "/storage/buckets/{project}"): "storage.create-bucket",
    ("DELETE", "/storage/buckets/{project}"): "storage.delete-bucket",
    ("POST", "/storage/buckets/{project}/{bucket_id:path}/describe"): "storage.describe-bucket",
    # ── storage: tables ──────────────────────────────────────────────
    ("GET", "/storage/tables"): "storage.tables",
    ("GET", "/storage/table-detail/{project}/{table_id:path}"): "storage.table-detail",
    # Both read table DATA; `download-table` is the CLI operation that does
    # the same thing, and the preview is just a row-capped variant of it.
    ("GET", "/storage/table-preview/{project}/{table_id:path}"): "storage.download-table",
    ("GET", "/storage/table-download/{project}/{table_id:path}"): "storage.download-table",
    ("POST", "/storage/tables/{project}"): "storage.create-table",
    ("POST", "/storage/tables/{project}/upload"): "storage.upload-table",
    ("DELETE", "/storage/tables/{project}"): "storage.delete-table",
    ("POST", "/storage/tables/{project}/truncate"): "storage.truncate-table",
    ("POST", "/storage/tables/{project}/{table_id:path}/swap"): "storage.swap-tables",
    ("POST", "/storage/tables/{project}/{table_id:path}/pull"): "storage.clone-table",
    ("POST", "/storage/tables/{project}/{table_id:path}/describe"): "storage.describe-table",
    # ── storage: columns ─────────────────────────────────────────────
    ("POST", "/storage/columns/{project}/{table_id:path}"): "storage.add-column",
    ("DELETE", "/storage/columns/{project}/{table_id:path}"): "storage.delete-column",
    ("POST", "/storage/columns/{project}/{table_id:path}/describe"): "storage.describe-column",
    ("POST", "/storage/columns/{project}/describe-migrate"): "storage.describe-migrate",
    # ── storage: snapshots ───────────────────────────────────────────
    ("POST", "/storage/tables/{project}/{table_id:path}/snapshots"): "storage.snapshot-create",
    ("GET", "/storage/snapshots/{project}/{table_id:path}"): "storage.snapshots",
    ("GET", "/storage/snapshot-detail/{project}/{snapshot_id}"): "storage.snapshot-detail",
    ("DELETE", "/storage/snapshots/{project}"): "storage.snapshot-delete",
    ("POST", "/storage/table-from-snapshot/{project}"): "storage.table-from-snapshot",
    # ── storage: files ───────────────────────────────────────────────
    ("GET", "/storage/files"): "storage.files",
    ("POST", "/storage/files/upload"): "storage.file-upload",
    ("GET", "/storage/files/{project}/{file_id}"): "storage.file-detail",
    ("GET", "/storage/files/{project}/{file_id}/download"): "storage.file-download",
    ("DELETE", "/storage/files/{project}"): "storage.file-delete",
    ("POST", "/storage/files/{project}/{file_id}/tag"): "storage.file-tag",
    ("POST", "/storage/files/{project}/load-to-table"): "storage.load-file",
    # ── data streams ─────────────────────────────────────────────────
    ("GET", "/stream/{project}/list"): "stream.list",
    ("GET", "/stream/{project}/detail"): "stream.detail",
    ("POST", "/stream/{project}/create-source"): "stream.create-source",
    ("POST", "/stream/{project}/delete"): "stream.delete",
    # ── scoped storage tokens ────────────────────────────────────────
    ("GET", "/token/list"): "token.list",
    ("GET", "/token/{project}/list"): "token.list",
    ("POST", "/token/{project}/create"): "token.create",
    ("POST", "/token/{project}/delete"): "token.delete",
    ("POST", "/token/{project}/refresh"): "token.refresh",
    # ── jobs ─────────────────────────────────────────────────────────
    ("GET", "/jobs"): "job.list",
    ("GET", "/jobs/{project}/{job_id}"): "job.detail",
    # SSE tail of one job -- the streaming form of `job detail`.
    ("GET", "/jobs/{project}/{job_id}/stream"): "job.detail",
    ("POST", "/jobs/{project}/run"): "job.run",
    ("POST", "/jobs/{project}/terminate"): "job.terminate",
    # ── branches ─────────────────────────────────────────────────────
    ("GET", "/branches"): "branch.list",
    ("POST", "/branches/{project}"): "branch.create",
    ("POST", "/branches/{project}/use"): "branch.use",
    ("POST", "/branches/{project}/reset"): "branch.reset",
    ("DELETE", "/branches/{project}/{branch_id}"): "branch.delete",
    # `branch merge` is itself only a URL producer (the merge happens in the
    # web UI), so the GET mirrors it exactly -- including its `write` class.
    ("GET", "/branches/{project}/merge-url"): "branch.merge",
    ("GET", "/branches/{project}/metadata"): "branch.metadata-list",
    ("GET", "/branches/{project}/metadata/{key}"): "branch.metadata-get",
    ("PUT", "/branches/{project}/metadata/{key}"): "branch.metadata-set",
    ("DELETE", "/branches/{project}/metadata/{metadata_id}"): "branch.metadata-delete",
    # ── workspaces ───────────────────────────────────────────────────
    ("GET", "/workspaces"): "workspace.list",
    ("POST", "/workspaces/{project}"): "workspace.create",
    ("GET", "/workspaces/{project}/{workspace_id}"): "workspace.detail",
    ("DELETE", "/workspaces/{project}/{workspace_id}"): "workspace.delete",
    ("POST", "/workspaces/{project}/{workspace_id}/password"): "workspace.password",
    ("POST", "/workspaces/{project}/{workspace_id}/load"): "workspace.load",
    ("POST", "/workspaces/{project}/{workspace_id}/query"): "workspace.query",
    ("POST", "/workspaces/{project}/from-transformation"): "workspace.from-transformation",
    ("POST", "/workspaces/gc"): "workspace.gc",
    ("POST", "/workspaces/sql/improve/stream"): "workspace.sql-improve",
    # ── flows ────────────────────────────────────────────────────────
    ("GET", "/flows"): "flow.list",
    ("GET", "/flows/examples"): "flow.examples",
    ("POST", "/flows/validate"): "flow.validate",
    ("GET", "/flows/{project}/schema"): "flow.schema",
    ("GET", "/flows/{project}/{config_id}"): "flow.detail",
    ("POST", "/flows/{project}"): "flow.new",
    ("PATCH", "/flows/{project}/{config_id}"): "flow.update",
    ("DELETE", "/flows/{project}/{config_id}"): "flow.delete",
    ("GET", "/flows/{project}/{config_id}/schedules"): "schedule.list",
    ("POST", "/flows/{project}/{config_id}/schedule"): "flow.schedule",
    ("DELETE", "/flows/{project}/{config_id}/schedule"): "flow.schedule-remove",
    # ── schedules / notifications ────────────────────────────────────
    ("GET", "/schedules"): "schedule.list",
    ("GET", "/schedules/{project}/{schedule_id}"): "schedule.detail",
    ("GET", "/schedules/find/query"): "schedule.find",
    ("GET", "/notifications"): "notification.list",
    ("GET", "/notifications/{project}/{subscription_id}"): "notification.detail",
    # ── lineage (all read-only; `build` is the only cache writer) ─────
    ("POST", "/lineage/build"): "lineage.build",
    ("GET", "/lineage/info"): "lineage.info",
    ("POST", "/lineage/show"): "lineage.show",
    ("GET", "/lineage/edges"): "lineage.show",
    ("GET", "/lineage/browser"): "lineage.show",
    ("GET", "/lineage/data"): "lineage.show",
    ("GET", "/lineage/walk"): "lineage.show",
    ("GET", "/lineage/mermaid"): "lineage.show",
    # ── sharing ──────────────────────────────────────────────────────
    ("GET", "/sharing"): "sharing.list",
    ("GET", "/sharing/edges"): "sharing.edges",
    ("POST", "/sharing/{project}/share"): "sharing.share",
    ("POST", "/sharing/{project}/unshare/{bucket_id:path}"): "sharing.unshare",
    ("POST", "/sharing/{project}/link"): "sharing.link",
    ("POST", "/sharing/{project}/unlink/{bucket_id:path}"): "sharing.unlink",
    # ── data apps ────────────────────────────────────────────────────
    ("GET", "/data-apps"): "data-app.list",
    ("GET", "/data-apps/{project}/{app_id}"): "data-app.detail",
    ("POST", "/data-apps/{project}"): "data-app.create",
    ("POST", "/data-apps/{project}/{app_id}/deploy"): "data-app.deploy",
    ("POST", "/data-apps/{project}/{app_id}/start"): "data-app.start",
    ("POST", "/data-apps/{project}/{app_id}/stop"): "data-app.stop",
    ("DELETE", "/data-apps/{project}/{app_id}"): "data-app.delete",
    ("GET", "/data-apps/{project}/{app_id}/password"): "data-app.password",
    ("GET", "/data-apps/{project}/{app_id}/logs"): "data-app.logs",
    ("GET", "/data-apps/{project}/{app_id}/runs"): "data-app.runs",
    ("GET", "/data-apps/{project}/{app_id}/secrets"): "data-app.secrets-list",
    ("GET", "/data-apps/{project}/{app_id}/secrets/{key:path}"): "data-app.secrets-get",
    ("PUT", "/data-apps/{project}/{app_id}/secrets"): "data-app.secrets-set",
    ("POST", "/data-apps/{project}/{app_id}/secrets/remove"): "data-app.secrets-remove",
    ("POST", "/data-apps/validate-repo"): "data-app.validate-repo",
    ("GET", "/data-apps/{project}/{app_id}/git-repo"): "data-app.git-repo",
    ("GET", "/data-apps/{project}/{app_id}/git-repo/credentials"): "data-app.git-credentials",
    (
        "POST",
        "/data-apps/{project}/{app_id}/git-repo/credentials",
    ): "data-app.git-credentials-create",
    # ── developer portal ─────────────────────────────────────────────
    ("GET", "/dev-portal/apps"): "dev-portal.list",
    ("GET", "/dev-portal/apps/{app}"): "dev-portal.get",
    # ── Kai / local AI ───────────────────────────────────────────────
    ("GET", "/kai/ping"): "kai.ping",
    ("GET", "/kai/preflight"): "kai.preflight",
    ("POST", "/kai/ask"): "kai.ask",
    ("POST", "/kai/chat"): "kai.chat",
    ("GET", "/kai/history"): "kai.history",
    ("GET", "/kai/chat/{chat_id}"): "kai.chat-detail",
    ("POST", "/ai/chat/stream"): "ai.chat",
    # ── encrypt / search / docs ──────────────────────────────────────
    ("POST", "/encrypt/values"): "encrypt.values",
    ("GET", "/search"): "search",
    ("POST", "/documentation/query"): "docs.query",
    # ── semantic layer ───────────────────────────────────────────────
    ("GET", "/semantic-layer/models"): "semantic-layer.model.list",
    ("POST", "/semantic-layer/models"): "semantic-layer.model.create",
    ("DELETE", "/semantic-layer/models/{model}"): "semantic-layer.model.delete",
    ("GET", "/semantic-layer/show"): "semantic-layer.show",
    ("GET", "/semantic-layer/validate"): "semantic-layer.validate",
    ("GET", "/semantic-layer/search-context"): "semantic-layer.search-context",
    ("GET", "/semantic-layer/get-context"): "semantic-layer.get-context",
    ("GET", "/semantic-layer/schema"): "semantic-layer.schema",
    ("GET", "/semantic-layer/export"): "semantic-layer.export",
    ("POST", "/semantic-layer/diff"): "semantic-layer.diff",
    # Collapsed parent keys -- `kind` is a path param, see "Granularity
    # caveat" in the module docstring.
    ("POST", "/semantic-layer/items/{kind}"): "semantic-layer.add",
    ("PUT", "/semantic-layer/items/{kind}/{name}"): "semantic-layer.edit",
    ("DELETE", "/semantic-layer/items/{kind}/{name}"): "semantic-layer.remove",
    ("POST", "/semantic-layer/import"): "semantic-layer.import",
    ("POST", "/semantic-layer/promote"): "semantic-layer.promote",
    ("POST", "/semantic-layer/build"): "semantic-layer.build",
    ("POST", "/semantic-layer/token/encrypt"): "semantic-layer.token",
    ("GET", "/semantic-layer/reference-data"): "semantic-layer.reference-data.list",
    ("GET", "/semantic-layer/reference-data/{record_id}"): "semantic-layer.reference-data.get",
    ("PUT", "/semantic-layer/reference-data"): "semantic-layer.reference-data.set",
    (
        "DELETE",
        "/semantic-layer/reference-data/{record_id}",
    ): "semantic-layer.reference-data.delete",
    # ── transformations ──────────────────────────────────────────────
    ("POST", "/transformations/{project}"): "transformation.create",
    ("GET", "/transformations/{project}/{config_id}"): "transformation.show",
    ("PATCH", "/transformations/{project}/{config_id}"): "transformation.edit",
    # ── organization ─────────────────────────────────────────────────
    ("POST", "/org/setup"): "org.setup",
    # `--refresh` is a flag on the same command, not a command of its own.
    ("POST", "/org/refresh"): "org.setup",
    # ── scheduled agent tasks ────────────────────────────────────────
    ("GET", "/agents"): "agent.list",
    ("POST", "/agents"): "agent.create",
    ("GET", "/agents/cron/preview"): "agent.cron-preview",
    ("POST", "/agents/test"): "agent.test",
    ("POST", "/agents/test/stream"): "agent.test",
    ("POST", "/agents/prompt/improve"): "agent.prompt-improve",
    ("POST", "/agents/prompt/improve/stream"): "agent.prompt-improve",
    ("GET", "/agents/{task_id}"): "agent.show",
    ("PATCH", "/agents/{task_id}"): "agent.update",
    ("DELETE", "/agents/{task_id}"): "agent.delete",
    ("POST", "/agents/{task_id}/run"): "agent.run",
    ("POST", "/agents/{task_id}/run/stream"): "agent.run",
    ("GET", "/agents/{task_id}/runs"): "agent.runs",
    ("GET", "/agents/{task_id}/runs/{run_id}"): "agent.run-detail",
    ("GET", "/agents/{task_id}/runs/{run_id}/events"): "agent.run-events",
}


def resolve_route_operation(method: str, path: str) -> str | None:
    """Return the operation key guarding ``method path``, or None when exempt.

    ``path`` is the route TEMPLATE (``APIRoute.path``), never the concrete
    request URL -- looking policy up by a concrete URL would make the decision
    depend on user-supplied identifiers.
    """
    if path in UNGUARDED_PATHS:
        return None
    return ROUTE_OPERATIONS.get((method.upper(), path))


def enforce_route_permission(
    request: Request,
    engine: PermissionEngine = Depends(get_permission_engine),
) -> None:
    """App-level dependency: check the active policy for the matched route.

    Registered once on the ``FastAPI`` instance, so it runs for every route the
    app declares -- there is no per-router opt-in to forget. FastAPI resolves
    the route before dependencies run, so ``request.scope["route"]`` is the
    matched :class:`~fastapi.routing.APIRoute` and its ``.path`` is the
    template the table is keyed on.

    Three outcomes:

    * exempt path (:data:`UNGUARDED_PATHS`) -> allowed, no policy consulted;
    * mapped route -> ``engine.check_or_raise(operation)``, i.e. HTTP 403 with
      ``error_code: PERMISSION_DENIED`` when the policy says no;
    * unmapped route -> **denied**. See :func:`_deny_unmapped`.

    With no policy configured (the default) ``is_allowed`` returns True for
    everything, so a mapped route behaves exactly as it did before this
    dependency existed. Only an unmapped route changes behavior -- and the
    completeness test makes that state unreachable in a released build.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path is None:
        # No matched APIRoute (a Mount, or a 404 that never resolved). Nothing
        # to authorize; Starlette will answer for it.
        return

    if path in UNGUARDED_PATHS:
        return

    operation = ROUTE_OPERATIONS.get((request.method.upper(), path))
    if operation is None:
        if _declares_inline_permission(route):
            # The route carries its own `require_permission(...)`, which has
            # already run (route dependencies are resolved after app-level
            # ones). Checking again here would need an operation the table
            # does not have; the inline guard is the authority.
            return
        _deny_unmapped(request.method.upper(), path)
        return

    engine.check_or_raise(operation)


def _declares_inline_permission(route: object) -> bool:
    """Whether ``route`` declares a ``require_permission(...)`` dependency itself."""
    from .dependencies import PERMISSION_DEPENDENCY_MARKER

    for dependant in getattr(route, "dependencies", ()) or ():
        call = getattr(dependant, "dependency", None)
        if getattr(call, PERMISSION_DEPENDENCY_MARKER, None) is not None:
            return True
    return False


def _deny_unmapped(method: str, path: str) -> None:
    """Refuse a route no entry in :data:`ROUTE_OPERATIONS` classifies.

    Failing open here would mean a route added without a table entry is exempt
    from every policy -- silently, and exactly for the newest and least
    reviewed part of the surface. Failing closed makes the omission a loud
    403 the first time anyone calls the route, and
    ``tests/test_server_route_permissions.py`` turns it into a failing test
    long before that.
    """
    from ..errors import PermissionDeniedError

    raise PermissionDeniedError(
        f"{method} {path} has no permission classification, so its risk cannot be "
        "evaluated and it is refused. Add an entry to ROUTE_OPERATIONS in "
        "server/route_permissions.py (or list the path in UNGUARDED_PATHS)."
    )


def unknown_operations() -> list[str]:
    """Table values that are not :data:`OPERATION_REGISTRY` keys.

    A typo'd operation would be classified ``write`` by the engine's own
    fail-closed default and never match an exact-name policy pattern -- wrong
    in a way nothing else notices. The completeness test asserts this is empty.
    """
    return sorted({op for op in ROUTE_OPERATIONS.values() if op not in OPERATION_REGISTRY})
