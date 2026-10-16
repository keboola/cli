"""Service registry and FastAPI dependency providers.

A single :class:`ServiceRegistry` holds long-lived service instances bound
to one :class:`ConfigStore`. Endpoints declare what they need via FastAPI
``Depends(get_<service>)`` helpers — clean DI without rebuilding services
per request.
"""

from __future__ import annotations

import email.message
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, params
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from ..config_store import ConfigStore
from ..dev_portal_client import DeveloperPortalClient
from ..errors import PermissionDeniedError
from ..permissions import PermissionEngine
from ..project_ref import is_project_id, resolve_project_ref
from ..services.auth_service import AuthService
from ..services.billing_service import BillingService
from ..services.branch_service import BranchService
from ..services.cls_service import ClsService
from ..services.component_service import ComponentService
from ..services.config_service import ConfigService
from ..services.data_app_git_service import DataAppGitService
from ..services.data_app_service import DataAppService
from ..services.deep_lineage_service import DeepLineageService
from ..services.dev_portal_service import DeveloperPortalService
from ..services.docs_service import DocsService
from ..services.doctor_service import DoctorService
from ..services.encrypt_service import EncryptService
from ..services.feature_service import FeatureService
from ..services.flow_service import FlowService
from ..services.job_service import JobService
from ..services.kai_service import KaiService
from ..services.lineage_service import LineageService
from ..services.member_service import MemberService
from ..services.merge_request_service import MergeRequestService
from ..services.notification_service import NotificationService
from ..services.org_service import OrgService
from ..services.project_service import ProjectService
from ..services.repo_validate_service import RepoValidateService
from ..services.rls_service import RlsService
from ..services.schedule_service import ScheduleService
from ..services.search_service import SearchService
from ..services.semantic_layer_service import SemanticLayerService
from ..services.sharing_service import SharingService
from ..services.snapshot_service import SnapshotService
from ..services.storage_service import StorageService
from ..services.stream_service import StreamService
from ..services.sync_service import SyncService
from ..services.token_service import TokenService
from ..services.transformation_service import TransformationService
from ..services.variables_service import VariablesService
from ..services.version_service import VersionService
from ..services.workspace_service import WorkspaceService


@dataclass
class ServiceRegistry:
    """Container of long-lived services for the FastAPI app.

    Sentinel-token guard note (programmatic-auth, contract section 12): this
    module never turns a `ProjectConfig` into credentials itself -- every
    service below is constructed with only `config_store` and resolves its
    own client factory (`make_client_factory` / a guarded `default_*_client_
    factory`, see `services/base.py` and each service's own factory). There
    is no separate chokepoint to guard here; `kbagent serve` inherits both
    the bearer-session support (Storage/Manage) and the `AUTH_NOT_SUPPORTED_
    ON_STACK` fail-fast guards (AI/data-science/metastore/dev-portal/
    stream) purely by delegating to those already-guarded services.

    Serving session-registered projects is a deliberate trade for web-UI
    usability, and it carries two properties worth stating where the wiring
    lives (see `docs/web-server.md` > "Session-registered projects"):

    1. A browser-login session is USER-scoped. Whoever holds
       `KBAGENT_SERVE_TOKEN` acts as the signed-in Keboola user for as long
       as the session lives, and the serve token is not that user's Keboola
       identity -- the REST surface has no second identity layer to tell the
       two apart.
    2. Refresh-token rotation was designed for short CLI invocations. In a
       daemon running for weeks, the crash window between `put_session`
       (`services/auth_service.py`) and the following revoke stays open far
       longer, and a crash inside it leaves a server-side session that no
       later `auth logout` can revoke.

    A session that expires while the daemon runs surfaces as HTTP 401 with
    `error_code: SESSION_EXPIRED` (mapped centrally in `app.py`), because a
    browser login only completes on the host, never for a REST caller.
    """

    config_store: ConfigStore
    # Self-contact info -- the URL + bearer token of the running serve.
    # Injected into AI-agent / CLI subprocess env so `kbagent http` (and
    # any HTTP-aware tool) can call this very server instead of forking
    # a fresh process tree against potentially stale local config.
    # Populated by ``create_app`` once uvicorn binding is known.
    serve_url: str | None = None
    serve_token: str | None = None
    project: ProjectService = field(init=False)
    config: ConfigService = field(init=False)
    component: ComponentService = field(init=False)
    snapshot: SnapshotService = field(init=False)
    storage: StorageService = field(init=False)
    stream: StreamService = field(init=False)
    job: JobService = field(init=False)
    branch: BranchService = field(init=False)
    workspace: WorkspaceService = field(init=False)
    flow: FlowService = field(init=False)
    schedule: ScheduleService = field(init=False)
    notification: NotificationService = field(init=False)
    lineage: LineageService = field(init=False)
    deep_lineage: DeepLineageService = field(init=False)
    sharing: SharingService = field(init=False)
    data_app: DataAppService = field(init=False)
    data_app_git: DataAppGitService = field(init=False)
    dev_portal: DeveloperPortalService = field(init=False)
    semantic_layer: SemanticLayerService = field(init=False)
    rls: RlsService = field(init=False)
    cls: ClsService = field(init=False)
    repo_validate: RepoValidateService = field(init=False)
    kai: KaiService = field(init=False)
    encrypt: EncryptService = field(init=False)
    search: SearchService = field(init=False)
    org: OrgService = field(init=False)
    member: MemberService = field(init=False)
    feature: FeatureService = field(init=False)
    sync: SyncService = field(init=False)
    variables: VariablesService = field(init=False)
    doctor: DoctorService = field(init=False)
    version: VersionService = field(init=False)
    token: TokenService = field(init=False)
    docs: DocsService = field(init=False)
    transformation: TransformationService = field(init=False)
    billing: BillingService = field(init=False)
    auth: AuthService = field(init=False)
    merge_request: MergeRequestService = field(init=False)

    def __post_init__(self) -> None:
        cs = self.config_store
        self.project = ProjectService(config_store=cs)
        self.config = ConfigService(config_store=cs)
        self.component = ComponentService(config_store=cs)
        self.snapshot = SnapshotService(config_store=cs)
        self.storage = StorageService(config_store=cs)
        self.stream = StreamService(config_store=cs)
        self.job = JobService(config_store=cs)
        self.branch = BranchService(config_store=cs)
        self.merge_request = MergeRequestService(config_store=cs)
        self.workspace = WorkspaceService(config_store=cs)
        self.flow = FlowService(config_store=cs)
        self.schedule = ScheduleService(config_store=cs)
        self.notification = NotificationService(config_store=cs)
        self.lineage = LineageService(config_store=cs)
        self.deep_lineage = DeepLineageService(config_store=cs)
        self.sharing = SharingService(config_store=cs)
        self.data_app = DataAppService(config_store=cs)
        self.data_app_git = DataAppGitService(config_store=cs)
        self.dev_portal = DeveloperPortalService(
            config_store=cs,
            client_factory=lambda identity: DeveloperPortalClient(identity),
        )
        # SemanticLayerService takes both a storage client_factory (for
        # validate --deep + add dataset --deep-fields + build) and an
        # optional metastore_client_factory; the defaults work for both.
        self.semantic_layer = SemanticLayerService(config_store=cs)
        self.rls = RlsService(config_store=cs)
        self.cls = ClsService(config_store=cs)
        self.repo_validate = RepoValidateService(config_store=cs)
        self.kai = KaiService(config_store=cs)
        self.encrypt = EncryptService(config_store=cs)
        self.search = SearchService(config_store=cs)
        self.org = OrgService(config_store=cs)
        self.member = MemberService(config_store=cs)
        self.feature = FeatureService(config_store=cs)
        self.sync = SyncService(config_store=cs)
        self.variables = VariablesService(config_store=cs)
        self.doctor = DoctorService(config_store=cs)
        self.version = VersionService()
        self.token = TokenService(config_store=cs)
        self.docs = DocsService(config_store=cs)
        self.transformation = TransformationService(config_store=cs)
        self.billing = BillingService(config_store=cs)
        # AuthService's browser-facing seams (client factory, browser opener,
        # sleep) all have safe defaults and are never reached by the read-only
        # session/project methods the REST surface exposes -- a browser login
        # only ever completes on the host, never for a REST caller.
        self.auth = AuthService(config_store=cs)


def install_registry(app: FastAPI, registry: ServiceRegistry) -> None:
    """Attach the registry to the FastAPI app state."""
    app.state.registry = registry


def get_registry(request: Request) -> ServiceRegistry:
    """FastAPI dependency: return the registry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


def install_permission_engine(app: FastAPI, engine: PermissionEngine) -> None:
    """Attach the REST surface's session firewall to the FastAPI app state.

    Called exactly once by ``create_app``. The engine deliberately does NOT
    live on :class:`ServiceRegistry`: server tests routinely replace the
    registry via ``dependency_overrides[get_registry]``, and an engine reachable
    only through the registry would vanish with it -- enforcement that a test
    override can silently switch off is not enforcement. ``app.state`` also
    survives a second registry-construction site being added later.
    """
    app.state.permission_engine = engine


def get_permission_engine(request: Request) -> PermissionEngine:
    """Return the app's permission engine, failing CLOSED when it is missing.

    ``create_app`` always calls :func:`install_permission_engine`, so an absent
    attribute means the app was assembled some other way. Treating that as "no
    policy" would turn an assembly bug into a silently open firewall, so it
    raises instead -- an app that cannot say whether an operation is permitted
    must not perform it. "No policy configured" is expressed by an engine
    wrapping a ``None`` policy, exactly as on the CLI side.
    """
    engine = getattr(request.app.state, "permission_engine", None)
    if engine is None:
        raise PermissionDeniedError(
            "Permission engine unavailable: this app was not built by create_app(), "
            "so no policy can be evaluated. Refusing the operation."
        )
    return engine  # type: ignore[no-any-return]


# Attribute stamped on the callable :func:`require_permission` returns, so the
# app-level dependency in ``route_permissions`` can recognise a route that
# already guards itself and skip its own (table-driven) lookup. A plain
# ``hasattr`` check on some ad-hoc name would be guesswork; a named constant
# makes the handshake between the two modules greppable.
PERMISSION_DEPENDENCY_MARKER = "kbagent_permission_operation"


def require_permission(operation: str) -> Callable[[PermissionEngine], None]:
    """Build a FastAPI dependency enforcing the permission policy for ``operation``.

    ``operation`` is an :data:`~keboola_agent_cli.permissions.OPERATION_REGISTRY`
    key (``"auth.register-projects"``, ``"config.delete"``, ...). Use it as a
    route dependency::

        @router.post("/auth/register-projects",
                     dependencies=[Depends(require_permission("auth.register-projects"))])

    A denial raises :class:`~keboola_agent_cli.errors.PermissionDeniedError`,
    which ``server/app.py`` maps centrally to HTTP 403 with
    ``error_code: PERMISSION_DENIED`` -- the same code and message the CLI
    prints, so a caller can branch on one value across both surfaces.

    The engine comes from :func:`get_permission_engine` (app state), never from
    the registry, so overriding ``get_registry`` in a test cannot disable the
    check.

    Since 0.99.0 (issue #655) this is the per-route OVERRIDE form, not the
    default one: every route is classified centrally in
    ``server/route_permissions.py`` and checked by one app-level dependency.
    Use this when a route needs an operation the table cannot express -- and
    when you do, the app-level dependency stands down for that route
    (:data:`PERMISSION_DEPENDENCY_MARKER` is how it recognises one).
    """

    def _check_permission(
        engine: PermissionEngine = Depends(get_permission_engine),
    ) -> None:
        engine.check_or_raise(operation)

    setattr(_check_permission, PERMISSION_DEPENDENCY_MARKER, operation)
    return _check_permission


def translate_project_refs(*names: str) -> Callable[..., None]:
    """Build a dependency that replaces a project ID in the named parameters with its alias.

    The REST form of the CLI's ``--project`` translation (CLI-22): a ``names``
    path parameter or query parameter that holds a registered project ID is
    rewritten in the request scope. FastAPI solves this dependency before it
    reads the route's own path and query parameters, so the handler and its
    services only receive the alias. An ID shared by several projects raises
    :class:`ConfigError` (HTTP 400 ``CONFIG_ERROR``). Request bodies are not
    translated.
    """

    def _translate(request: Request, registry: ServiceRegistry = Depends(get_registry)) -> None:
        path_params = request.scope.get("path_params") or {}
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        query = parse_qsl(raw_query, keep_blank_values=True)
        refs = [path_params[name] for name in names if name in path_params]
        refs += [value for key, value in query if key in names]
        if not any(is_project_id(ref) for ref in refs):
            return
        projects = registry.config_store.load().projects
        for name in names:
            if name in path_params:
                path_params[name] = resolve_project_ref(projects, path_params[name])
        translated = [
            (key, resolve_project_ref(projects, value) if key in names else value)
            for key, value in query
        ]
        if translated != query:
            request.scope["query_string"] = urlencode(translated).encode("latin-1")
            # FastAPI already read the query while it solved this dependency's
            # own parameters, and Starlette keeps that copy on the request.
            # Drop it, so the route reads the rewritten query string. Private
            # Starlette attribute: tests/test_server_project_ref.py::
            # test_query_param_project_ids_reach_service_as_aliases fails if
            # an upgrade renames it.
            request.__dict__.pop("_query_params", None)

    return _translate


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The strings pydantic reads as a false ``bool`` (compared lower-cased).
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", "f", "n"})
# Largest body the check reads on a route that has no body parameter itself.
_MAX_UNREAD_BODY_BYTES = 1024 * 1024


async def reject_undeclared_dry_run(request: Request) -> None:
    """App-level dependency: refuse a ``dry_run`` that the matched mutating route does not declare.

    FastAPI ignores undeclared query parameters and body fields. Without this
    check, ``DELETE /flows/p/123?dry_run=true`` runs the real delete while the
    caller expects a preview (CLI-33). A route declares ``dry_run`` as a query
    parameter or as a field of its JSON body model. A ``dry_run`` sent in any
    other place answers HTTP 422 before the route runs.

    A clearly false value (``false``, ``0``, ``no``, ``off``, ``f``, ``n`` in
    any case, or JSON ``false`` / ``0``) is ignored: it asks for the real
    operation, which is what the route does anyway. Any other value -- ``true``,
    ``null``, an unknown string, an object -- is refused.

    Body cost: for a route with a JSON body, FastAPI parses the body before it
    solves dependencies and Starlette caches the result on the request, so
    ``request.json()`` here reuses that copy. A route without a body parameter
    has nothing that reads its body, so reading a JSON body here takes nothing
    away from it. It reads such a body only when ``Content-Length`` is present
    and at most 1 MiB, so a missing or larger length skips the body check there.
    A form route (the multipart uploads, which spool the file to disk) is
    checked on the query string only: its body is never read here.
    """
    if request.method.upper() not in _MUTATING_METHODS:
        return
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return
    flat = get_flat_dependant(route.dependant)
    in_query = any(p.alias == "dry_run" for p in flat.query_params)
    in_body = _body_declares_dry_run(flat.body_params)
    where = f"{request.method.upper()} {route.path}"
    query_values = request.query_params.getlist("dry_run")
    if not in_query and not all(_is_false(value) for value in query_values):
        _refuse(where, "the query string", "the JSON body" if in_body else None)
    if in_body or any(isinstance(p.field_info, params.Form) for p in flat.body_params):
        return
    if not _is_json(request.headers.get("content-type")):
        return
    if not flat.body_params:
        length = request.headers.get("content-length", "")
        if not length.isdigit() or int(length) > _MAX_UNREAD_BODY_BYTES:
            return
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        # Malformed or too deeply nested JSON on a route without a body
        # parameter: the route never read it, so this check leaves it alone.
        return
    if isinstance(body, dict) and "dry_run" in body and not _is_false(body["dry_run"]):
        _refuse(where, "the JSON body", "the query string" if in_query else None)


def _body_declares_dry_run(body_params: list[typing.Any]) -> bool:
    """Whether the route's JSON body has a top-level ``dry_run`` field."""
    if any(p.alias == "dry_run" for p in body_params):
        return True
    if len(body_params) != 1 or getattr(body_params[0].field_info, "embed", False):
        # Several body parameters (or ``Body(embed=True)``): FastAPI nests each
        # one under its own name, so only a parameter named ``dry_run`` counts.
        return False
    for model in _models_in(body_params[0].field_info.annotation):
        fields = model.model_fields
        if "dry_run" in fields or any(f.alias == "dry_run" for f in fields.values()):
            return True
    return False


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """The pydantic models in ``annotation``, also inside ``X | None`` and ``Annotated``."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _models_in(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return [model for arg in typing.get_args(annotation) for model in _models_in(arg)]
    if origin is None and isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def _is_false(value: object) -> bool:
    """Whether ``value`` asks for the real operation (a false ``dry_run``)."""
    if isinstance(value, str):
        return value.lower() in _FALSE_STRINGS
    if isinstance(value, bool):
        return value is False
    return isinstance(value, int | float) and value == 0


def _is_json(content_type: str | None) -> bool:
    """Same content-type test FastAPI applies before it parses a JSON body."""
    if not content_type:
        return False
    message = email.message.Message()
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (
        subtype == "json" or subtype.endswith("+json")
    )


def _refuse(where: str, sent_in: str, declared_in: str | None) -> typing.NoReturn:
    if declared_in is None:
        reason = f"{where} has no dry run: `dry_run` in {sent_in} is not a parameter of this route."
        remedy = "Remove `dry_run` to run the real operation."
    else:
        reason = f"{where} takes `dry_run` in {declared_in}, not in {sent_in}."
        remedy = f"Send `dry_run` in {declared_in}."
    raise HTTPException(
        status_code=422,
        detail=f"{reason} The request was refused and nothing was changed. {remedy}",
    )


def get_manage_token(request: Request) -> str | None:
    """Return the per-request manage token from the X-Manage-Token header.

    Returns None if not provided. Endpoints that require it should validate.
    Never logged, never persisted.
    """
    return request.headers.get("x-manage-token")
