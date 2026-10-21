/**
 * The projects a list page asks the serve API for.
 */

/** The query of a list route for the project selected in the top bar. */
export type ProjectScope = { project: string } | { all_projects: true };

/**
 * `null` (the "All projects" view) becomes `all_projects: true`, not "no
 * `project`": since 1.0.0 a list route without `project` reads only the active
 * project (`kbagent project use`), as the CLI does (spec T1, T2).
 */
export function projectScope(project: string | null | undefined): ProjectScope {
  return project ? { project } : { all_projects: true };
}
