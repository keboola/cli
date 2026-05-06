"""Hint definitions for project member & invitation commands (since v0.26.1)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── project invite ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.invite",
        description="Invite a user (by email) to a project with a given role",
        steps=[
            HintStep(
                comment="POST /manage/projects/{id}/invitations",
                client=ClientCall(
                    method="create_project_invitation",
                    args={
                        "project_id": "{project_id}",
                        "email": "{email}",
                        "role": "{role}",
                        "reason": "{reason}",
                    },
                    client_type="manage",
                    result_var="invitation",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="MemberService",
                    service_module="member_service",
                    method="invite",
                    args={
                        "alias": "{project}",
                        "email": "{email}",
                        "role": "{role}",
                        "reason": "{reason}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses Manage API + KBC_MANAGE_API_TOKEN (not the Storage token).",
            "Allowed roles: admin, guest, readOnly, share.",
            "Re-inviting an existing invitee or member returns HTTP 400; the service "
            "treats it as a no-op with a 'note' field.",
        ],
    )
)

# ── project member-list ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.member-list",
        description="List active members (and optionally pending invitations)",
        steps=[
            HintStep(
                comment="GET /manage/projects/{id}/users",
                client=ClientCall(
                    method="list_project_members",
                    args={"project_id": "{project_id}"},
                    client_type="manage",
                    result_var="members",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="MemberService",
                    service_module="member_service",
                    method="list_members",
                    args={
                        "alias": "{project}",
                        "include_pending": "{include_pending}",
                    },
                ),
            ),
        ],
    )
)

# ── project invitation-list ──────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.invitation-list",
        description="List pending project invitations",
        steps=[
            HintStep(
                comment="GET /manage/projects/{id}/invitations",
                client=ClientCall(
                    method="list_project_invitations",
                    args={"project_id": "{project_id}"},
                    client_type="manage",
                    result_var="invitations",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="MemberService",
                    service_module="member_service",
                    method="list_invitations",
                    args={"alias": "{project}"},
                ),
            ),
        ],
    )
)

# ── project invitation-cancel ────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.invitation-cancel",
        description="Cancel a pending invitation",
        steps=[
            HintStep(
                comment="DELETE /manage/projects/{id}/invitations/{invitationId}",
                client=ClientCall(
                    method="cancel_project_invitation",
                    args={
                        "project_id": "{project_id}",
                        "invitation_id": "{invitation_id}",
                    },
                    client_type="manage",
                    result_var="_",
                    result_hint="None",
                ),
                service=ServiceCall(
                    service_class="MemberService",
                    service_module="member_service",
                    method="cancel_invitation",
                    args={
                        "alias": "{project}",
                        "email": "{email}",
                        "invitation_id": "{invitation_id}",
                    },
                ),
            ),
        ],
        notes=[
            "If --invitation-id is omitted, the service resolves it by listing "
            "pending invitations and matching --email (case-insensitive).",
        ],
    )
)

# ── project member-remove ────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.member-remove",
        description="Remove an active member from a project",
        steps=[
            HintStep(
                comment="DELETE /manage/projects/{id}/users/{userId}",
                client=ClientCall(
                    method="remove_project_member",
                    args={
                        "project_id": "{project_id}",
                        "user_id": "{user_id}",
                    },
                    client_type="manage",
                    result_var="_",
                    result_hint="None",
                ),
                service=ServiceCall(
                    service_class="MemberService",
                    service_module="member_service",
                    method="remove_member",
                    args={
                        "alias": "{project}",
                        "email": "{email}",
                    },
                ),
            ),
        ],
        notes=[
            "Destructive: revokes project access. Re-add via `kbagent project invite`.",
            "The service resolves --email to the numeric user_id automatically.",
        ],
    )
)

# ── project member-set-role ──────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="project.member-set-role",
        description="Change an existing member's role",
        steps=[
            HintStep(
                comment="PATCH /manage/projects/{id}/users/{userId}",
                client=ClientCall(
                    method="update_project_member_role",
                    args={
                        "project_id": "{project_id}",
                        "user_id": "{user_id}",
                        "role": "{role}",
                    },
                    client_type="manage",
                    result_var="updated",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="MemberService",
                    service_module="member_service",
                    method="set_member_role",
                    args={
                        "alias": "{project}",
                        "email": "{email}",
                        "role": "{role}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses HTTP PATCH (not PUT — PUT returns 404 even on real members).",
            "Allowed roles: admin, guest, readOnly, share.",
        ],
    )
)
