import type { ReactNode } from "react";

export interface EmptyPath {
  title: string;
  description: string;
  action: ReactNode;
  badge?: string;
  icon?: ReactNode;
}

/**
 * Two-tile empty state -- inspired by Keboola UI's flow page that offers
 * "Build it yourself" alongside "Use Keboola MCP Server". Steers the user
 * toward the right entry point instead of dumping a single CTA.
 */
export function TwoPathEmpty({
  headline,
  subline,
  paths,
}: {
  headline: string;
  subline?: string;
  paths: [EmptyPath, EmptyPath];
}) {
  return (
    <div className="py-12 max-w-4xl mx-auto">
      <div className="text-center mb-10">
        <h2 className="text-2xl font-bold text-zinc-100">{headline}</h2>
        {subline ? <p className="text-zinc-500 mt-2 text-sm">{subline}</p> : null}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {paths.map((p, i) => (
          <div
            key={i}
            className="nerd-card hover:border-keboola/40 transition-colors flex flex-col text-center py-8"
          >
            {p.badge ? (
              <span className="nerd-pill-amber w-fit mx-auto mb-3">{p.badge}</span>
            ) : null}
            {p.icon ? <div className="flex justify-center mb-3">{p.icon}</div> : null}
            <h3 className="font-bold text-zinc-100 text-base">{p.title}</h3>
            <p className="text-zinc-500 text-xs mt-2 mb-6 px-4">{p.description}</p>
            <div className="flex justify-center">{p.action}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

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
