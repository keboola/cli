/**
 * Runtime configuration for the BFF.
 *
 * Reads three required env vars:
 *   KBAGENT_SERVE_URL     -- e.g. http://127.0.0.1:8001
 *   KBAGENT_SERVE_TOKEN   -- bearer token printed by `kbagent serve`
 *   PORT (optional)       -- BFF listen port (default 8000)
 *
 * Fail-fast: missing env vars throw at startup so the operator notices.
 */
export interface AppConfig {
  bffPort: number;
  bffHost: string;
  kbagentUrl: string;
  kbagentToken: string;
  staticDir: string | null;
}

function requireEnv(key: string): string {
  const v = process.env[key];
  if (!v) {
    throw new Error(`Missing required env var: ${key}`);
  }
  return v;
}

export function loadConfig(): AppConfig {
  return {
    bffPort: Number(process.env.PORT ?? 8000),
    bffHost: process.env.HOST ?? "127.0.0.1",
    kbagentUrl: process.env.KBAGENT_SERVE_URL ?? "http://127.0.0.1:8001",
    kbagentToken: requireEnv("KBAGENT_SERVE_TOKEN"),
    staticDir: process.env.STATIC_DIR ?? null,
  };
}
