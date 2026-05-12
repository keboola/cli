import type { ReactNode } from "react";

export function Empty({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="nerd-card text-center py-10">
      <div className="text-zinc-300 text-sm">{title}</div>
      {hint ? <div className="text-zinc-500 text-xs mt-2">{hint}</div> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="nerd-card border-red-700/40 text-red-400 text-sm">
      <div className="font-bold mb-1">Error</div>
      <div className="text-xs">{message}</div>
    </div>
  );
}

export function Loading({ label = "loading" }: { label?: string }) {
  return (
    <div className="text-zinc-500 text-xs flex items-center gap-2">
      <span className="w-2 h-2 rounded-full bg-keboola animate-pulse" />
      {label}
    </div>
  );
}

export function PageTitle({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-xl font-bold text-zinc-100">{title}</h1>
        {description ? <p className="text-zinc-500 text-xs mt-1">{description}</p> : null}
      </div>
      {actions ? <div className="flex gap-2">{actions}</div> : null}
    </div>
  );
}
