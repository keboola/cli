/**
 * BFF entrypoint.
 *
 * - Mounts /api/* as a transparent proxy to kbagent serve (auth + SSE).
 * - Mounts /__bff/info for client bootstrap (no secrets).
 * - Optionally serves the React build from STATIC_DIR (production).
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import fastifyCors from "@fastify/cors";
import fastifyStatic from "@fastify/static";
import Fastify from "fastify";
import { loadConfig } from "./config.js";
import { registerProxy } from "./proxy.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

async function main(): Promise<void> {
  const config = loadConfig();
  const app = Fastify({
    logger: {
      level: process.env.LOG_LEVEL ?? "info",
      transport: { target: "pino-pretty", options: { colorize: true } },
    },
    // Turn off body parsing for /api/* routes -- proxy passes raw bodies.
    bodyLimit: 50 * 1024 * 1024, // 50MB for file uploads
  });

  await app.register(fastifyCors, {
    origin: ["http://localhost:5173", "http://127.0.0.1:5173"],
    credentials: true,
  });

  // Public-ish bootstrap endpoint -- the React app needs to know the upstream
  // URL (for direct SSE connections if BFF proxy ever becomes a bottleneck).
  app.get("/__bff/info", async () => ({
    bff: { host: config.bffHost, port: config.bffPort },
    kbagent: { url: config.kbagentUrl, ready: true },
    version: "0.1.0",
  }));

  app.get("/__bff/health", async () => ({ status: "ok" }));

  await registerProxy(app, config);

  if (config.staticDir) {
    await app.register(fastifyStatic, {
      root: path.resolve(config.staticDir),
      prefix: "/",
      wildcard: false,
    });
    app.setNotFoundHandler(async (req, reply) => {
      // SPA fallback for client-side routing
      if (req.method === "GET" && !req.url.startsWith("/api")) {
        return reply.sendFile("index.html");
      }
      return reply.code(404).send({
        status: "error",
        error: { code: "NOT_FOUND", message: `No route for ${req.url}` },
      });
    });
  }

  try {
    await app.listen({ port: config.bffPort, host: config.bffHost });
    app.log.info(
      `kbagent web/backend listening on http://${config.bffHost}:${config.bffPort}`,
    );
    app.log.info(`upstream kbagent serve: ${config.kbagentUrl}`);
  } catch (err) {
    app.log.error(err);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
