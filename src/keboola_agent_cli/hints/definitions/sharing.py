"""Hint definitions for sharing commands (list, share, unshare, link, unlink)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── sharing list ───────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="sharing.list",
        description="List shared and linked buckets",
        steps=[
            HintStep(
                comment="List shared buckets",
                client=ClientCall(
                    method="list_shared_buckets",
                    args={},
                    result_var="shared",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="SharingService",
                    service_module="sharing_service",
                    method="list_shared",
                    args={"aliases": "{project}"},
                ),
            ),
        ],
    )
)

# ── sharing share ──────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="sharing.share",
        description="Share a bucket with other projects",
        steps=[
            HintStep(
                comment="Share bucket",
                client=ClientCall(
                    method="share_bucket",
                    args={
                        "bucket_id": "{bucket_id}",
                        "sharing_type": "{type}",
                        "target_project_ids": "{target_project_ids}",
                        "target_users": "{target_users}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="SharingService",
                    service_module="sharing_service",
                    method="share",
                    args={
                        "alias": "{project}",
                        "bucket_id": "{bucket_id}",
                        "sharing_type": "{type}",
                        "target_project_ids": "{target_project_ids}",
                        "target_users": "{target_users}",
                    },
                ),
            ),
        ],
        notes=["May require a master token for organization-level sharing."],
    )
)

# ── sharing unshare ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="sharing.unshare",
        description="Stop sharing a bucket",
        steps=[
            HintStep(
                comment="Unshare bucket",
                client=ClientCall(
                    method="unshare_bucket",
                    args={"bucket_id": "{bucket_id}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="SharingService",
                    service_module="sharing_service",
                    method="unshare",
                    args={"alias": "{project}", "bucket_id": "{bucket_id}"},
                ),
            ),
        ],
        notes=["Requires a master token."],
    )
)

# ── sharing link ───────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="sharing.link",
        description="Link a shared bucket into this project",
        steps=[
            HintStep(
                comment="Link shared bucket",
                client=ClientCall(
                    method="link_bucket",
                    args={
                        "source_project_id": "{source_project_id}",
                        "source_bucket_id": "{bucket_id}",
                        "name": "{name}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="SharingService",
                    service_module="sharing_service",
                    method="link",
                    args={
                        "alias": "{project}",
                        "source_project_id": "{source_project_id}",
                        "source_bucket_id": "{bucket_id}",
                        "name": "{name}",
                    },
                ),
            ),
        ],
    )
)

# ── sharing unlink ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="sharing.unlink",
        description="Remove a linked bucket from this project",
        steps=[
            HintStep(
                comment="Unlink bucket",
                client=ClientCall(
                    method="delete_bucket",
                    args={"bucket_id": "{bucket_id}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="SharingService",
                    service_module="sharing_service",
                    method="unlink",
                    args={"alias": "{project}", "bucket_id": "{bucket_id}"},
                ),
            ),
        ],
        notes=["Service layer validates the bucket is linked before deleting."],
    )
)
