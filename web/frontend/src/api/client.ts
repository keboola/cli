/**
 * Thin fetch wrapper. The BFF accepts requests on the same origin (Vite proxies
 * /api/* in dev, Fastify serves /api/* in prod), so no auth headers needed
 * client-side -- the BFF injects the kbagent serve token on the way upstream.
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
 */
export function sseSubscribe(
  path: string,
  query: RequestOptions["query"] | undefined,
  handlers: Record<string, (data: unknown) => void>,
): EventSource {
  const url = buildUrl(path, query);
  const es = new EventSource(url, { withCredentials: false });
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
