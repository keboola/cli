import { Lock, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

/**
 * Per-action manage-token prompt. Sent as ``X-Manage-Token`` for the next
 * request, never persisted (held in component state only).
 */
export function ManageTokenModal({
  reason,
  onSubmit,
  onCancel,
}: {
  reason: string;
  onSubmit: (token: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  return (
    <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
      <form
        className="nerd-card w-full max-w-md space-y-3 border-keboola/40"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onSubmit(value.trim());
        }}
      >
        <div className="flex items-center justify-between">
          <h3 className="font-bold text-keboola flex items-center gap-2">
            <Lock className="w-4 h-4" /> Manage token required
          </h3>
          <button type="button" className="text-zinc-500 hover:text-zinc-200" onClick={onCancel}>
            <X className="w-4 h-4" />
          </button>
        </div>
        <p className="text-xs text-zinc-400">{reason}</p>
        <p className="text-xs text-zinc-600">
          Sent only for THIS request. Not stored anywhere, not logged.
        </p>
        <input
          ref={ref}
          type="password"
          className="nerd-input w-full"
          placeholder="KBC manage API token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          required
        />
        <div className="flex gap-2 justify-end">
          <button type="button" className="nerd-btn text-xs" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="submit"
            className="nerd-btn hover:text-keboola"
            disabled={!value.trim()}
          >
            Submit
          </button>
        </div>
      </form>
    </div>
  );
}
