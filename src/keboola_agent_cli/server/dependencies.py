"""Service registry and FastAPI dependency providers.

A single :class:`ServiceRegistry` holds long-lived service instances bound
to one :class:`ConfigStore`. Endpoints declare what they need via FastAPI
``Depends(get_<service>)`` helpers — clean DI without rebuilding services
per request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, Request

from ..config_store import ConfigStore
from ..dev_portal_client import DeveloperPortalClient
from ..errors import PermissionDeniedError
from ..permissions import PermissionEngine
from ..services.auth_service import AuthService
from ..services.billing_service import BillingService
from ..services.branch_service import BranchService
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
from ..services.notification_service import NotificationService
from ..services.org_service import OrgService
from ..services.project_service import ProjectService
from ..services.repo_validate_service import RepoValidateService
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
    """

    def _check_permission(
        engine: PermissionEngine = Depends(get_permission_engine),
    ) -> None:
        engine.check_or_raise(operation)

    return _check_permission


def get_manage_token(request: Request) -> str | None:
    """Return the per-request manage token from the X-Manage-Token header.

    Returns None if not provided. Endpoints that require it should validate.
    Never logged, never persisted.
    """
    return request.headers.get("x-manage-token")
