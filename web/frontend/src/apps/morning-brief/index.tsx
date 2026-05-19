/**
 * Morning Brief - reference implementation of the Dashboard archetype.
 *
 * Aggregates recent Queue jobs across all configured projects, flags
 * runs whose duration is meaningfully higher than their config's
 * recent median, and lets the user drill into details. No agents
 * involved on the read path; a per-row "Analyse" action stubs out
 * where an AI call would attach.
 *
 * This file is referenced by
 * plugins/kbagent/skills/kbagent/references/build-app-over-kbagent-serve.md
 * as the canonical example. Keep it small and idiomatic.
 */
import { Sunrise } from "lucide-react";
import type { AppManifest } from "../_registry";
import { MorningBriefPage } from "./MorningBriefPage";

const manifest: AppManifest = {
  slug: "morning-brief",
  label: "Morning Brief",
  section: "Apps",
  icon: Sunrise,
  component: MorningBriefPage,
  description:
    "Cross-project overview of recent jobs with cost / duration outliers highlighted.",
};

export default manifest;
