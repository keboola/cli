/**
 * HTTP + SSE proxy from the BFF to `kbagent serve`.
 *
 * - Regular requests are forwarded with the bearer token attached.
 * - SSE responses (text/event-stream) stream chunk-by-chunk to the client.
 * - The X-Manage-Token header (if present) is forwarded verbatim, never logged.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { request as undiciRequest } from "undici";
import type { AppConfig } from "./config.js";

const HOP_BY_HOP = new Set([
  "host",
  "connection",
  "content-length",
  "transfer-encoding",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "upgrade",
]);

function buildHeaders(req: FastifyRequest, token: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (v === undefined) continue;
    const lower = k.toLowerCase();
    if (HOP_BY_HOP.has(lower)) continue;
    if (lower === "authorization") continue; // BFF auth is separate
    out[lower] = Array.isArray(v) ? v.join(",") : String(v);
  }
  out["authorization"] = `Bearer ${token}`;
  return out;
}

async function streamSSE(
  upstream: AsyncIterable<Buffer>,
  reply: FastifyReply,
): Promise<void> {
  reply.raw.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  for await (const chunk of upstream) {
    reply.raw.write(chunk);
  }
  reply.raw.end();
}

export async function registerProxy(
  app: FastifyInstance,
  config: AppConfig,
): Promise<void> {
  app.all("/api/*", async (req, reply) => {
    const path = req.url.replace(/^\/api/, "");
    const upstreamUrl = `${config.kbagentUrl}${path}`;
    const headers = buildHeaders(req, config.kbagentToken);
    const body =
      req.method === "GET" || req.method === "HEAD"
        ? undefined
        : req.body
          ? JSON.stringify(req.body)
          : undefined;
    if (body && !headers["content-type"]) {
      headers["content-type"] = "application/json";
    }
    try {
      const res = await undiciRequest(upstreamUrl, {
        method: req.method as "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
        headers,
        body,
      });
      const ct = res.headers["content-type"];
      const ctStr = Array.isArray(ct) ? ct.join(",") : (ct ?? "");
      if (ctStr.includes("text/event-stream")) {
        await streamSSE(res.body as AsyncIterable<Buffer>, reply);
        return;
      }
      const buf = await res.body.arrayBuffer();
      for (const [k, v] of Object.entries(res.headers)) {
        if (v === undefined) continue;
        if (HOP_BY_HOP.has(k.toLowerCase())) continue;
        reply.header(k, v as string);
      }
      reply.code(res.statusCode);
      reply.send(Buffer.from(buf));
    } catch (err) {
      reply.code(502).send({
        status: "error",
        error: {
          code: "BFF_UPSTREAM_ERROR",
          message: `Upstream kbagent serve unavailable: ${(err as Error).message}`,
        },
      });
    }
  });
}
