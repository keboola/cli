"""Service registry and FastAPI dependency providers.

A single :class:`ServiceRegistry` holds long-lived service instances bound
to one :class:`ConfigStore`. Endpoints declare what they need via FastAPI
``Depends(get_<service>)`` helpers — clean DI without rebuilding services
per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request

from ..config_store import ConfigStore
from ..dev_portal_client import DeveloperPortalClient
from ..services.branch_service import BranchService
from ..services.component_service import ComponentService
from ..services.config_service import ConfigService
from ..services.data_app_git_service import DataAppGitService
from ..services.data_app_service import DataAppService
from ..services.deep_lineage_service import DeepLineageService
from ..services.dev_portal_service import DeveloperPortalService
from ..services.doctor_service import DoctorService
from ..services.encrypt_service import EncryptService
from ..services.feature_service import FeatureService
from ..services.flow_service import FlowService
from ..services.job_service import JobService
from ..services.kai_service import KaiService
from ..services.lineage_service import LineageService
from ..services.mcp_service import McpService
from ..services.member_service import MemberService
from ..services.org_service import OrgService
from ..services.project_service import ProjectService
from ..services.repo_validate_service import RepoValidateService
from ..services.schedule_service import ScheduleService
from ..services.search_service import SearchService
from ..services.semantic_layer_service import SemanticLayerService
from ..services.sharing_service import SharingService
from ..services.storage_service import StorageService
from ..services.stream_service import StreamService
from ..services.sync_service import SyncService
from ..services.token_service import TokenService
from ..services.variables_service import VariablesService
from ..services.version_service import VersionService
from ..services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    pass


@dataclass
class ServiceRegistry:
    """Container of long-lived services for the FastAPI app."""

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
    storage: StorageService = field(init=False)
    stream: StreamService = field(init=False)
    job: JobService = field(init=False)
    branch: BranchService = field(init=False)
    workspace: WorkspaceService = field(init=False)
    flow: FlowService = field(init=False)
    schedule: ScheduleService = field(init=False)
    lineage: LineageService = field(init=False)
    deep_lineage: DeepLineageService = field(init=False)
    sharing: SharingService = field(init=False)
    data_app: DataAppService = field(init=False)
    data_app_git: DataAppGitService = field(init=False)
    dev_portal: DeveloperPortalService = field(init=False)
    semantic_layer: SemanticLayerService = field(init=False)
    repo_validate: RepoValidateService = field(init=False)
    mcp: McpService = field(init=False)
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

    def __post_init__(self) -> None:
        cs = self.config_store
        self.project = ProjectService(config_store=cs)
        self.config = ConfigService(config_store=cs)
        self.component = ComponentService(config_store=cs)
        self.storage = StorageService(config_store=cs)
        self.stream = StreamService(config_store=cs)
        self.job = JobService(config_store=cs)
        self.branch = BranchService(config_store=cs)
        self.workspace = WorkspaceService(config_store=cs)
        self.flow = FlowService(config_store=cs)
        self.schedule = ScheduleService(config_store=cs)
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
        self.mcp = McpService(config_store=cs)
        self.kai = KaiService(config_store=cs)
        self.encrypt = EncryptService(config_store=cs)
        self.search = SearchService(config_store=cs)
        self.org = OrgService(config_store=cs)
        self.member = MemberService(config_store=cs)
        self.feature = FeatureService(config_store=cs)
        self.sync = SyncService(config_store=cs)
        self.variables = VariablesService(config_store=cs)
        self.doctor = DoctorService(config_store=cs, mcp_service=self.mcp)
        self.version = VersionService()
        self.token = TokenService(config_store=cs)


def install_registry(app: FastAPI, registry: ServiceRegistry) -> None:
    """Attach the registry to the FastAPI app state."""
    app.state.registry = registry


def get_registry(request: Request) -> ServiceRegistry:
    """FastAPI dependency: return the registry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


def get_manage_token(request: Request) -> str | None:
    """Return the per-request manage token from the X-Manage-Token header.

    Returns None if not provided. Endpoints that require it should validate.
    Never logged, never persisted.
    """
    return request.headers.get("x-manage-token")
