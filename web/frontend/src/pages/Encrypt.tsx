import { useMutation } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { useUIState } from "../state";

export function EncryptPage() {
  const { project } = useUIState();
  const [componentId, setComponentId] = useState("keboola.ex-db-snowflake");
  const [json, setJson] = useState('{\n  "#password": "secret"\n}');
  const [result, setResult] = useState<Record<string, string> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const mu = useMutation({
    mutationFn: () => {
      const values = JSON.parse(json) as Record<string, string>;
      return api.post<Record<string, string>>("/encrypt/values", {
        project,
        component_id: componentId,
        values,
      });
    },
    onSuccess: (data) => {
      setResult(data);
      setError(null);
    },
    onError: (err) => {
      setError((err as Error).message);
      setResult(null);
    },
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Encrypt secrets"
        description="Convert plaintext #-prefixed values to KBC:: ciphertext for use in configs."
      />
      <div className="nerd-card space-y-3">
        <label className="text-xs text-zinc-400 block">
          Component ID
          <input
            className="nerd-input w-full mt-1"
            value={componentId}
            onChange={(e) => setComponentId(e.target.value)}
          />
        </label>
        <label className="text-xs text-zinc-400 block">
          Values (JSON, keys must start with #)
          <textarea
            className="nerd-input w-full mt-1 h-32 font-mono"
            value={json}
            onChange={(e) => setJson(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="nerd-btn flex items-center gap-1 hover:text-keboola"
          onClick={() => mu.mutate()}
          disabled={!project || mu.isPending}
        >
          <Lock className="w-3 h-3" /> {mu.isPending ? "encrypting..." : "Encrypt"}
        </button>
      </div>
      {error ? <ErrorBox message={error} /> : null}
      {result ? (
        <div className="nerd-card">
          <h3 className="font-bold text-keboola mb-2">Encrypted result</h3>
          <JsonView data={result} />
        </div>
      ) : null}
    </div>
  );
}
