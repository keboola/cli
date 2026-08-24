/**
 * Tests for the 401 self-heal path in the API client.
 *
 * In single-process UI mode (`kbagent serve --ui`) auth rides on the
 * HttpOnly `kbagent_session` cookie set by `GET /`. After a server restart
 * the cookie is stale, every API call answers 401, and -- before this fix --
 * the SPA silently rendered empty lists. The client now re-fetches the shell
 * once (`cache: "reload"` so no browser cache can swallow the request, which
 * is exactly how the bug happened in the first place), retries the request,
 * and only then surfaces a visible "session expired" signal.
 *
 * Runs in the default vitest node environment: `window` is stubbed with a
 * real EventTarget so `dispatchEvent`/`addEventListener` behave like the
 * browser's, and `fetch` is a vi.fn() -- no jsdom dependency needed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, SESSION_EXPIRED_EVENT } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function unauthorized(message = "Invalid Bearer token."): Response {
  return jsonResponse(401, {
    status: "error",
    error: { code: "UNAUTHORIZED", message },
  });
}

function shellResponse(): Response {
  return new Response("<!doctype html>", {
    status: 200,
    headers: { "content-type": "text/html" },
  });
}

/** URL of a fetch call, whether invoked with a string or a Request. */
function calledUrl(input: unknown): string {
  return typeof input === "string" ? input : (input as Request).url;
}

beforeEach(() => {
  const fakeWindow = new EventTarget() as unknown as Window & typeof globalThis;
  (fakeWindow as unknown as { location: { origin: string } }).location = {
    origin: "http://127.0.0.1:8001",
  };
  vi.stubGlobal("window", fakeWindow);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request 401 retry", () => {
  it("re-bootstraps the session cookie and retries once after a 401", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(unauthorized()) // GET /api/projects -> stale cookie
      .mockResolvedValueOnce(shellResponse()) // GET / -> fresh Set-Cookie
      .mockResolvedValueOnce(jsonResponse(200, { projects: [] })); // retry
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.get("/projects")).resolves.toEqual({ projects: [] });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [shellUrl, shellInit] = fetchMock.mock.calls[1];
    expect(calledUrl(shellUrl)).toBe("/");
    // cache: "reload" is the load-bearing part -- a plain fetch("/") could be
    // answered from the very browser cache that made the cookie go stale.
    expect(shellInit).toMatchObject({ cache: "reload", credentials: "include" });
  });

  it("dispatches a session-expired event when the retry still 401s", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(unauthorized())
      .mockResolvedValueOnce(shellResponse())
      .mockResolvedValueOnce(unauthorized("Invalid Bearer token."));
    vi.stubGlobal("fetch", fetchMock);
    const events: CustomEvent[] = [];
    window.addEventListener(SESSION_EXPIRED_EVENT, (evt) => {
      events.push(evt as CustomEvent);
    });

    await expect(api.get("/projects")).rejects.toMatchObject({ status: 401 });

    // Exactly one retry -- no loop of shell re-fetches on a genuinely
    // broken session.
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(events).toHaveLength(1);
    expect(events[0].detail).toMatchObject({
      code: "UNAUTHORIZED",
      message: "Invalid Bearer token.",
    });
  });

  it("does not retry or dispatch on non-401 errors", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse(502, {
          status: "error",
          error: { code: "API_ERROR", message: "upstream down" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const events: Event[] = [];
    window.addEventListener(SESSION_EXPIRED_EVENT, (evt) => events.push(evt));

    await expect(api.get("/projects")).rejects.toBeInstanceOf(ApiError);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(events).toHaveLength(0);
  });

  it("shares a single shell re-fetch across concurrent 401s", async () => {
    let releaseShell: (() => void) | undefined;
    const shellGate = new Promise<void>((resolve) => {
      releaseShell = resolve;
    });
    const seen401 = new Set<string>();
    let shellFetches = 0;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const url = calledUrl(input);
      if (url === "/") {
        shellFetches += 1;
        return shellGate.then(shellResponse);
      }
      if (!seen401.has(url)) {
        seen401.add(url);
        return Promise.resolve(unauthorized());
      }
      return Promise.resolve(jsonResponse(200, { ok: url }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const inFlight = Promise.all([api.get("/projects"), api.get("/jobs")]);
    // Let both requests hit their 401 and pile onto the shell fetch.
    await new Promise((resolve) => setTimeout(resolve, 0));
    releaseShell?.();

    await expect(inFlight).resolves.toEqual([
      { ok: "/api/projects" },
      { ok: "/api/jobs" },
    ]);
    expect(shellFetches).toBe(1);
  });
});
