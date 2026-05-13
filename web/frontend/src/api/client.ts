/**
 * Thin fetch wrapper. The browser never sees the bearer token.
 *
 * Two transport modes, both transparent to call sites:
 *
 * - **BFF mode** (Vite dev / Fastify prod): the BFF receives /api/* on the
 *   same origin and proxies upstream with ``Authorization: Bearer <token>``
 *   attached. The browser sends no auth.
 * - **Single-process mode** (`kbagent serve --ui`): FastAPI serves both
 *   the SPA and the API on one origin. ``GET /`` sets a HttpOnly
 *   ``kbagent_session`` cookie (SameSite=Strict, Path=/) that the browser
 *   attaches automatically to every same-origin request when we pass
 *   ``credentials: "include"`` (REST) / ``withCredentials: true`` (SSE).
 *
 * The ``credentials`` opt-in is a no-op in BFF mode (no cookie is set on
 * the BFF origin), so we can use the same call shape unconditionally.
 *
 * The token never lands in the JS heap, in URLs, or in uvicorn's access
 * log -- the cookie is HttpOnly + the auth path is header/cookie only.
 */
export interface KbagentError {
  status: "error";
  error: { code: string; message: string };
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

const API_BASE = "/api";

interface RequestOptions {
  manageToken?: string;
  signal?: AbortSignal;
  body?: unknown;
  query?: Record<string, string | number | boolean | string[] | undefined | null>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      if (Array.isArray(value)) {
        for (const v of value) url.searchParams.append(key, String(v));
      } else {
        url.searchParams.append(key, String(value));
      }
    }
  }
  return url.pathname + url.search;
}

async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) {
    headers["content-type"] = "application/json";
  }
  if (opts.manageToken) {
    headers["x-manage-token"] = opts.manageToken;
  }
  const res = await fetch(buildUrl(path, opts.query), {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal,
    // Send the kbagent_session cookie on same-origin requests in
    // single-process UI mode. In BFF mode no such cookie exists, so this
    // is a no-op (browser sends an empty cookie jar for the BFF origin).
    credentials: "include",
  });
  if (!res.ok) {
    let payload: KbagentError | null = null;
    try {
      payload = (await res.json()) as KbagentError;
    } catch {
      // not JSON
    }
    const message = payload?.error?.message ?? res.statusText;
    const code = payload?.error?.code ?? "HTTP_ERROR";
    throw new ApiError(code, message, res.status);
  }
  // 204 No Content
  if (res.status === 204) return undefined as T;
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("POST", path, { ...opts, body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("PUT", path, { ...opts, body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("PATCH", path, { ...opts, body }),
  delete: <T>(path: string, opts?: RequestOptions) =>
    request<T>("DELETE", path, opts),
};

/**
 * Subscribe to an SSE endpoint. Returns the EventSource so the caller can close it.
 *
 * ``withCredentials: true`` makes the browser attach the same-origin
 * ``kbagent_session`` cookie -- the only auth surface in single-process
 * UI mode. In BFF mode the cookie is absent and the BFF injects the
 * Authorization header upstream, so the call shape stays identical.
 */
export function sseSubscribe(
  path: string,
  query: RequestOptions["query"] | undefined,
  handlers: Record<string, (data: unknown) => void>,
): EventSource {
  const url = buildUrl(path, query);
  const es = new EventSource(url, { withCredentials: true });
  for (const [event, handler] of Object.entries(handlers)) {
    es.addEventListener(event, (msg) => {
      try {
        const evt = msg as MessageEvent;
        const data = JSON.parse(evt.data);
        handler(data);
      } catch (err) {
        console.warn("SSE event parse failed", err);
      }
    });
  }
  return es;
}

/**
 * POST to an SSE endpoint and stream events back. EventSource only supports
 * GET, so for POST we hand-roll a `fetch + ReadableStream + manual SSE parser`.
 *
 * Each "message" in the SSE protocol is delimited by a blank line; within a
 * message, `event:` and `data:` lines accumulate. Once a blank line arrives
 * we dispatch the buffered message to `handlers[event]`. Unknown event names
 * fall through to `handlers["message"]` if defined.
 */
export interface SsePostHandle {
  /** Reject the in-flight fetch (best-effort -- backend cancels via finally). */
  abort: () => void;
  /** Resolves when the server closes the stream (or aborted). */
  done: Promise<void>;
}

export function ssePost(
  path: string,
  body: unknown,
  handlers: Record<string, (data: unknown) => void>,
): SsePostHandle {
  const controller = new AbortController();
  const done = (async () => {
    const sseHeaders: Record<string, string> = {
      "content-type": "application/json",
      accept: "text/event-stream",
    };
    const res = await fetch(buildUrl(path), {
      method: "POST",
      headers: sseHeaders,
      body: JSON.stringify(body),
      signal: controller.signal,
      // Same rationale as ``request()`` above -- carries the session
      // cookie in --ui mode, no-op in BFF mode.
      credentials: "include",
    });
    if (!res.ok || !res.body) {
      throw new ApiError("HTTP_ERROR", res.statusText, res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let event = "message";
    let dataLines: string[] = [];
    const dispatch = () => {
      if (dataLines.length === 0) return;
      const payload = dataLines.join("\n");
      const handler = handlers[event] ?? handlers["message"];
      if (handler) {
        try {
          handler(JSON.parse(payload));
        } catch {
          handler(payload);
        }
      }
      event = "message";
      dataLines = [];
    };
    while (true) {
      const { value, done: streamDone } = await reader.read();
      if (streamDone) break;
      buf += decoder.decode(value, { stream: true });
      // SSE: messages are separated by a blank line. We parse line by line.
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl).replace(/\r$/, "");
        buf = buf.slice(nl + 1);
        if (line === "") {
          dispatch();
          continue;
        }
        if (line.startsWith(":")) continue; // comment / heartbeat
        const colon = line.indexOf(":");
        if (colon === -1) continue;
        const field = line.slice(0, colon);
        const value = line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") event = value;
        else if (field === "data") dataLines.push(value);
      }
    }
    // Flush any trailing event without terminating blank line.
    dispatch();
  })();
  return { abort: () => controller.abort(), done };
}
