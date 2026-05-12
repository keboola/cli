import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { ManageTokenModal } from "../components/ManageTokenModal";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";

type PendingAction =
  | { type: "list"; include_pending: boolean }
  | { type: "invite"; email: string; role: string; reason?: string }
  | { type: "remove"; email: string }
  | { type: "set-role"; email: string; role: string };

export function MembersPage() {
  const { project } = useUIState();
  const qc = useQueryClient();
  const [members, setMembers] = useState<Array<Record<string, unknown>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [inviteForm, setInviteForm] = useState<{ email: string; role: string; reason: string } | null>(null);

  const callMu = useMutation({
    mutationFn: async ({ action, manageToken }: { action: PendingAction; manageToken: string }) => {
      if (!project) throw new Error("No project selected");
      if (action.type === "list") {
        const r = await api.get<{ members: Array<Record<string, unknown>> }>(
          `/members/${encodeURIComponent(project)}`,
          { query: { include_pending: action.include_pending }, manageToken },
        );
        setMembers(r.members);
        return r;
      }
      if (action.type === "invite") {
        return api.post(
          `/members/${encodeURIComponent(project)}/invite`,
          { email: action.email, role: action.role, reason: action.reason },
          { manageToken },
        );
      }
      if (action.type === "remove") {
        return api.post(
          `/members/${encodeURIComponent(project)}/remove`,
          { email: action.email },
          { manageToken },
        );
      }
      if (action.type === "set-role") {
        return api.post(
          `/members/${encodeURIComponent(project)}/set-role`,
          { email: action.email, role: action.role },
          { manageToken },
        );
      }
      throw new Error("unknown action");
    },
    onError: (err) => setError((err as Error).message),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["members"] });
      setError(null);
    },
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Project members"
        description={`Manage members of ${project ?? "(no project)"} -- requires a manage token.`}
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            disabled={!project}
            onClick={() => setInviteForm({ email: "", role: "guest", reason: "" })}
          >
            <UserPlus className="w-3 h-3" /> Invite
          </button>
        }
      />
      <button
        type="button"
        className="nerd-btn"
        disabled={!project}
        onClick={() => setPending({ type: "list", include_pending: true })}
      >
        Load members
      </button>
      {!project ? <Empty title="Select a project" /> : null}
      {error ? <ErrorBox message={error} /> : null}
      {callMu.isPending ? <Loading /> : null}
      {members ? (
        <DataTable
          rows={members}
          rowKey={(m) => String(m.id ?? m.email ?? Math.random())}
          columns={[
            { header: "Email", cell: (m) => <span>{String(m.email ?? "")}</span> },
            { header: "Name", cell: (m) => <span className="text-zinc-400">{String(m.name ?? "")}</span> },
            { header: "Role", cell: (m) => <span className="nerd-pill">{String(m.role ?? "")}</span> },
          ]}
        />
      ) : null}
      {inviteForm ? (
        <form
          className="nerd-card space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            setPending({ type: "invite", email: inviteForm.email, role: inviteForm.role, reason: inviteForm.reason });
            setInviteForm(null);
          }}
        >
          <h3 className="font-bold text-keboola">Invite member</h3>
          <input
            className="nerd-input w-full"
            placeholder="email"
            value={inviteForm.email}
            onChange={(e) => setInviteForm({ ...inviteForm, email: e.target.value })}
          />
          <select
            className="nerd-input w-full"
            value={inviteForm.role}
            onChange={(e) => setInviteForm({ ...inviteForm, role: e.target.value })}
          >
            <option>admin</option>
            <option>guest</option>
            <option>readOnly</option>
            <option>share</option>
          </select>
          <input
            className="nerd-input w-full"
            placeholder="reason (optional)"
            value={inviteForm.reason}
            onChange={(e) => setInviteForm({ ...inviteForm, reason: e.target.value })}
          />
          <div className="flex gap-2">
            <button type="submit" className="nerd-btn hover:text-keboola">
              Continue → manage token
            </button>
            <button type="button" className="nerd-btn" onClick={() => setInviteForm(null)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      {pending ? (
        <ManageTokenModal
          reason={`Action: ${pending.type}`}
          onCancel={() => setPending(null)}
          onSubmit={(token) => {
            const action = pending;
            setPending(null);
            callMu.mutate({ action, manageToken: token });
          }}
        />
      ) : null}
    </div>
  );
}
