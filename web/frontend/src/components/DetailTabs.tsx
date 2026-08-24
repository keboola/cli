export interface DetailTab {
  id: string;
  label: string;
}

/**
 * Tab bar for detail views (drawer / side panel bodies).
 *
 * The button styling is lifted verbatim from the hand-rolled tab rows on the
 * Streams / Flows / Storage detail panels (`nerd-btn text-xs` + the
 * `border-keboola text-keboola` active marker) so a converted page is visually
 * indistinguishable from the ones still rolling their own.
 */
export function DetailTabs({
  tabs,
  active,
  onChange,
  className = "flex flex-wrap gap-2 mb-4",
}: {
  tabs: DetailTab[];
  active: string;
  onChange: (id: string) => void;
  className?: string;
}) {
  return (
    <div className={className} role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={active === t.id}
          className={`nerd-btn text-xs ${active === t.id ? "border-keboola text-keboola" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
