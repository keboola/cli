/**
 * App registry.
 *
 * Each app under `src/apps/<slug>/` exports a default `AppManifest` from its
 * `index.tsx`. This file picks them up at build time via Vite's
 * `import.meta.glob` (eager) so:
 *
 *   - no manual wiring in App.tsx / Sidebar.tsx when adding an app,
 *   - bundle stays static (no runtime fetch of app modules),
 *   - TS keeps full type safety on the manifest shape.
 *
 * Folders prefixed with `_` (e.g. `_templates/`) are skipped by Vite's glob
 * pattern, which matches the convention used in the rest of the repo for
 * "this is a template, not a runtime artefact" directories.
 */
import type { ComponentType } from "react";
import type { LucideIcon } from "lucide-react";

export interface AppManifest {
  /** URL-safe slug. Becomes `app:<slug>` in the UI state. Must match folder name. */
  slug: string;
  /** Sidebar label. Short, lowercase-ish to match NERD UI voice. */
  label: string;
  /** Sidebar section header. Defaults to "Apps" if omitted. */
  section?: string;
  /** Lucide icon component. */
  icon: LucideIcon;
  /** The page-level React component. Rendered inside <Shell>. */
  component: ComponentType;
  /** One-line description. Shown on the apps index, in tooltips, etc. */
  description?: string;
  /**
   * If true, the app is hidden from the sidebar (still reachable by
   * `setPage("app:<slug>")`). Useful for embedded/sub-apps.
   */
  hidden?: boolean;
}

type GlobModule = { default: AppManifest };

/**
 * Eagerly imported map of `./<slug>/index.tsx` → module. Vite resolves this
 * at build time, so there's no runtime cost beyond the modules themselves.
 *
 * The `!./_*` exclusion would be cleaner but Vite's glob does not support
 * negation patterns; we filter the result in `loadApps()` instead.
 */
const modules = import.meta.glob<GlobModule>("./*/index.tsx", { eager: true });

function loadApps(): AppManifest[] {
  const apps: AppManifest[] = [];
  for (const [path, mod] of Object.entries(modules)) {
    // path looks like "./morning-brief/index.tsx"
    const folder = path.split("/")[1] ?? "";
    if (folder.startsWith("_")) continue;
    const manifest = mod.default;
    if (!manifest) {
      console.warn(`apps/${folder}/index.tsx has no default export, skipping`);
      continue;
    }
    if (manifest.slug !== folder) {
      console.warn(
        `apps/${folder}/index.tsx declares slug="${manifest.slug}", expected "${folder}"`,
      );
    }
    apps.push(manifest);
  }
  // Stable order: alphabetical by slug. Sidebar can re-sort if a manifest
  // ever grows an `order` field.
  apps.sort((a, b) => a.slug.localeCompare(b.slug));
  return apps;
}

export const APPS: readonly AppManifest[] = loadApps();

export function findApp(slug: string): AppManifest | undefined {
  return APPS.find((a) => a.slug === slug);
}

/**
 * Page ID for the UI state machine. Apps live in the `app:<slug>` namespace
 * so they coexist with the existing built-in PageIds.
 */
export type AppPageId = `app:${string}`;

export function isAppPageId(id: string): id is AppPageId {
  return id.startsWith("app:");
}

export function appPageId(slug: string): AppPageId {
  return `app:${slug}`;
}

export function slugFromAppPageId(id: AppPageId): string {
  return id.slice("app:".length);
}
