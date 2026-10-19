/**
 * The branch a page asks the serve API for.
 */

/** The `branch_id` that means production (`--branch 0` in the CLI). */
export const PRODUCTION_BRANCH_ID = 0;

/**
 * The `branch_id` to send for the branch selected in the top bar.
 *
 * `null` (the production view) becomes `0`, not "no `branch_id`": a route that
 * gets no `branch_id` uses the active branch saved by `kbagent branch use`
 * (some routes now, the routes listed in spec rule V2 from 1.0.0), so the production
 * view would show or change that branch.
 */
export function branchParam(branchId: number | null): number {
  return branchId ?? PRODUCTION_BRANCH_ID;
}
