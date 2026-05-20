/**
 * Type Inspector - reference implementation of the Inspector archetype.
 *
 * Pick a project + table, see per-column profile (null %, distinct,
 * inferred type, sample values), and ask Kai (POST /kai/ask) to propose a
 * concrete Snowflake/BigQuery-ish type per column. Approve, edit, or
 * reject each proposal.
 *
 * Live profiling uses /storage/table-preview (sample of up to 500 rows);
 * the full "branch + workspace verification + table swap" workflow is
 * NOT implemented here — that belongs in a Playbook. The Apply button is
 * a deliberate stub that explains the next steps.
 *
 * This file is referenced by
 * plugins/kbagent/skills/kbagent/references/build-app-over-kbagent-serve.md
 * as the canonical example of the Inspector archetype + the
 * "AI button inside an app" pattern.
 */
import { TableProperties } from "lucide-react";
import type { AppManifest } from "../_registry";
import { TypeInspectorPage } from "./TypeInspectorPage";

const manifest: AppManifest = {
  slug: "type-inspector",
  label: "Type Inspector",
  section: "Apps",
  icon: TableProperties,
  component: TypeInspectorPage,
  description:
    "Profile each column of a Storage table, ask Kai to propose native types, approve per column.",
};

export default manifest;
