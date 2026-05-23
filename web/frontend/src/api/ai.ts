/**
 * One-shot local-AI invocation for apps.
 *
 * `kbagent serve` exposes two ways to call AI:
 *
 * 1. `POST /kai/ask` -- hosted Kai. Requires a MASTER storage token on
 *    the project. Convenient if you have it; not the default for apps.
 *
 * 2. `POST /ai/chat/stream` -- local CLI (claude / codex / gemini). Uses
 *    the user's own AI install on this machine. No master token, no
 *    network round-trip to Keboola, no provider lock-in.
 *
 * Apps should default to (2). The browser hits the SSE endpoint, the
 * server spawns the local CLI in a child process, streams its output
 * back, and emits a final `done` event with the assembled response.
 *
 * This helper hides the SSE plumbing: pass a single-shot prompt, get a
 * Promise<string> back. For interactive chat (multi-turn, streamed
 * partial output) use `ssePost` directly -- see `pages/LocalAi.tsx`.
 */
import { ApiError, ssePost } from "./client";

export type LocalAiCli = "claude" | "codex" | "gemini";

export interface AskLocalAiOpts {
  /** The prompt. Keep it tight; the model needs the full ask in one go. */
  message: string;
  /** Project alias to ground the prompt in (active project from useUIState). */
  project?: string | null;
  /** Active branch ID, if any. Lets the CLI scope its reasoning. */
  branchId?: number | null;
  /** Which local CLI to invoke. Defaults to `claude`. */
  cli?: LocalAiCli;
  /** Optional AbortSignal -- aborting calls handle.abort() internally. */
  signal?: AbortSignal;
}

/**
 * Fire a one-shot prompt at the local AI and resolve with the final
 * response text.
 *
 * - Streamed `stdout` chunks are accumulated.
 * - The `done` event carries the canonical final response in
 *   `data.response`; we prefer that over the streamed text because the
 *   CLI may run tools without emitting a textual summary.
 * - On `done.status === "error"` we reject with the embedded message.
 * - On any SSE-level failure (HTTP error, network drop) we reject
 *   with the original error.
 */
export function askLocalAi(opts: AskLocalAiOpts): Promise<string> {
  const cli = opts.cli ?? "claude";
  return new Promise<string>((resolve, reject) => {
    let streamed = "";
    let resolved = false;
    const finalize = (value: string) => {
      if (resolved) return;
      resolved = true;
      resolve(value);
    };
    const fail = (err: unknown) => {
      if (resolved) return;
      resolved = true;
      reject(err);
    };

    const handle = ssePost(
      "/ai/chat/stream",
      {
        cli,
        message: opts.message,
        project: opts.project ?? null,
        branch_id: opts.branchId ?? null,
      },
      {
        stdout: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          // Best-effort streaming text accumulation. Different CLIs emit
          // different shapes; we cover the two most common:
          //   - { type: "assistant", message: { content: [{ text }] } }   (claude)
          //   - { text: "..." }                                            (codex/gemini)
          if (data.type === "assistant" && typeof data.message === "object") {
            const msg = data.message as Record<string, unknown>;
            const content = msg.content;
            if (Array.isArray(content)) {
              for (const part of content) {
                if (
                  part &&
                  typeof part === "object" &&
                  (part as { type?: unknown }).type === "text" &&
                  typeof (part as { text?: unknown }).text === "string"
                ) {
                  streamed += (part as { text: string }).text;
                }
              }
            }
          } else if (typeof data.text === "string") {
            streamed += data.text;
          }
        },
        done: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          if (data.status === "error") {
            fail(
              new ApiError(
                "AI_ERROR",
                String(data.error ?? "Local AI invocation failed"),
                500,
              ),
            );
            return;
          }
          // Prefer the canonical `response` if present (some CLIs emit no
          // textual stdout when they only ran tools).
          const finalText =
            typeof data.response === "string" && data.response
              ? data.response
              : streamed;
          finalize(finalText);
        },
      },
    );

    if (opts.signal) {
      if (opts.signal.aborted) {
        handle.abort();
        fail(new DOMException("Aborted", "AbortError"));
        return;
      }
      opts.signal.addEventListener(
        "abort",
        () => {
          handle.abort();
          fail(new DOMException("Aborted", "AbortError"));
        },
        { once: true },
      );
    }

    handle.done.catch(fail);
  });
}
