import { Check, Copy } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { DetailTabs } from "./DetailTabs";
import { JsonView } from "./JsonView";

/**
 * The Overview / Raw JSON pattern shared by every rendered detail view:
 * a human-readable body by default, with the untouched payload one click
 * away so nothing the API returned is ever hidden from the user.
 */
export function RawDetail({
  data,
  overview,
  defaultTab = "overview",
  // Caps the JSON pane just short of the viewport bottom instead of the
  // JsonView default (60vh), which left an expanded drawer half empty while
  // the payload scrolled inside a short box. A shorter payload still shrinks
  // to its own height -- this is a max, not a height.
  maxHeight = "calc(100vh - 14rem)",
}: {
  data: unknown;
  overview: ReactNode;
  defaultTab?: "overview" | "raw";
  maxHeight?: string;
}) {
  const [tab, setTab] = useState<string>(defaultTab);

  return (
    <>
      <DetailTabs
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "raw", label: "Raw JSON" },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === "overview" ? (
        overview
      ) : (
        <div className="space-y-2">
          <CopyJsonButton data={data} />
          <JsonView data={data} maxHeight={maxHeight} />
        </div>
      )}
    </>
  );
}

/**
 * `navigator.clipboard` is undefined on a non-secure origin (kbagent serve is
 * plain http by default), so the button degrades to a "select it manually"
 * hint instead of throwing — same contract as the Tokens secret panel.
 */
function CopyJsonButton({ data }: { data: unknown }) {
  const [state, setState] = useState<"idle" | "copied" | "manual">("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const onCopy = () => {
    const clip = navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") {
      setState("manual");
      return;
    }
    clip.writeText(JSON.stringify(data, null, 2)).then(
      () => {
        setState("copied");
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = setTimeout(() => setState("idle"), 2000);
      },
      () => setState("manual"),
    );
  };

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
        onClick={onCopy}
        title="Copy the raw JSON payload to the clipboard"
      >
        {state === "copied" ? (
          <>
            <Check className="w-3 h-3" /> copied
          </>
        ) : (
          <>
            <Copy className="w-3 h-3" /> copy JSON
          </>
        )}
      </button>
      {state === "manual" ? (
        <span className="text-[11px] text-amber-700 dark:text-neon-amber">
          Clipboard unavailable (non-secure origin) — select the JSON below and copy manually.
        </span>
      ) : null}
    </div>
  );
}
