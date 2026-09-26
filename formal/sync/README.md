# Formal models of the `kbagent sync` engine (issue #792)

Pilot: can a small model checker find real bugs in `sync pull | diff | push`?
`tla/` holds a TLA+ model checked with TLC. `lean/` is a separate Lean
effort, not covered here.

## What the TLA+ model covers

`tla/SyncEngine.tla` is a small finite model of the engine. Every operator
names the Python function and line range it mirrors.

- **Remote**: 2 branches (`prod`, plus `dev` as a copy of prod). There are
  2 initial config ids, and 2 more ids for configs created during the run.
  Content versions are 0..2. One component (`mcp`) can become ignored.
- **Local**: 2 trees (`main/`, `devt/`) and 5 directory paths. Each
  `_config.yml` is a record with fields for the component, the id, the name
  and the content. It also carries two abstract bits:
  - `cosm`: a byte-only edit. It changes the RAW sha256 (`pull_hash`) but
    not `config_hash`.
  - `drift`: the local form of the content hashes differently from the
    API's view of the same content (#686).
- **Manifest**: one entry per id: `branchId`, `path`, `componentId`,
  `pull_hash` (the RAW hash) and `pull_config_hash` (the normalized base).
- **Actors**:
  - **User**: edit content, cosmetic edit, `rm -rf` a directory,
    `config new` scaffold, `config new --push --output-dir`, mark a
    component ignored.
  - **Remote**: edit, delete (trash looks the same to sync), create a
    config, optionally reusing an existing config's name.
  - **Sync**: `sync pull` (plain / `--force` / `--theirs`), `sync push`, and
    `sync push` aborted by `ENCRYPTION_FAILED` after some of its changes
    were already applied. Any of these can use `--branch` for either
    branch, which also covers `branch use`.
  - `diff` is a pure function that every invariant can call.
- **Ghost fields**, used only by invariants:
  - lineage (which config a file or remote config descends from);
  - `ed`: the file holds work that is on no remote;
  - `sv`: the remote version the file was last synced with;
  - the user-deleted directories;
  - flags for the last operation.

**Abstracted away**:
- rows, so I10 is not checked;
- renames and the name-collision suffix (a pull where two configs land on
  one path is disabled);
- `config_hash_version` / legacy-shape migration;
- companion code files;
- dry-run;
- baseline read-back failures;
- variable and flow-task backfill.

The id pool is bounded: a push that needs more new ids than are free is
disabled. `PushAbort` applies an arbitrary strict prefix of the changes in
path/id order. That over-approximates the diff order the code uses.

## How to run

The model needs Java 17 and `tla2tools.jar`.

```sh
cd formal/sync/tla
java -XX:+UseParallelGC -cp ~/tools/tla/tla2tools.jar tlc2.TLC -workers auto \
     -config SyncEngine.cfg SyncEngine.tla   # clean run: invariants that hold
./run_all.sh                                 # every invariant, one TLC run each (~15 min)
./trace.py out/<NAME>.json                   # compact counterexample
```

The depth is bounded by `MaxSteps` (a state constraint). `MaxSteps = 4`
explores traces of up to 5 actions. BFS returns the shortest counterexample.

## Results (MaxSteps = 4; up to 153k distinct states per run)

| Invariant | Result |
|---|---|
| I3 ignored component never planned | **holds** (153,077 states; 196,569 at depth 5 on prod only) |
| I4 diff/push act only on the source tree | **holds** (153,077 states) |
| I7 never-fetched entry never deleted | **holds** (104,809 states, never-fetched initial entry) |
| I1 no double create | violated: a promote push re-creates configs on every run; a resurrect followed by an `ENCRYPTION_FAILED` abort also creates a copy |
| I2 push deletes only user-removed dirs | violated: **pull's stale-entry sweep deletes a directory the same pull just wrote, and the next push deletes the live remote config** |
| I2b delete requires `--force` | violated: `push()` never reads `force` (S1) |
| I5 pull keeps local work | violated: a remote delete plus a local edit ends with plain or `--force` pull deleting the edited directory silently |
| I6 push then diff is clean | violated on `--branch` promote (finding D, since fixed in the engine; the TLA model is unchanged). Holds on production only (30,334 states) |
| I8 manifest matches disk | violated: the stale sweep, and a promote write-back that records a `devt/` entry for a file in `main/` |
| I9 an aborted push is atomic | violated (strong reading): the changes before the failing one reached the API and the manifest was never saved |
| I11 no lost remote update | violated: an adopted file with a config id is diffed 2-way, so push reverts a UI edit |
| I11b no silent resurrect | violated: a remote delete followed by any push re-creates the config, even with no local edit (S2) |
| I12 a pull resolves REMOTE MODIFIED | violated: after a cosmetic edit, plain pull skips the file forever and `--force` raises a conflict (S3) |

Each violation was checked against the code. The main ones were replayed
against the real `SyncService` with a stateful fake API (the scratch harness
from the #792 pilot). Details are in the pilot report.

## Consolidated findings (A..K)

The spec's suspicious spots (S1..S5), the Lean refutations (F1..F8) and the
TLA+ counterexamples (I1..I12) overlap heavily -- several independently
rediscover the same code path. This table is the deduplicated result, with
the regression test for each in `tests/test_sync_formal_counterexamples.py`.

| ID | Finding | Sources | Severity | Test |
|----|---------|---------|----------|------|
| A | Remote delete + recreate under the same name: pull writes the new config into the old directory, the stale sweep then deletes that directory; the next push DELETEs the live new config | Lean F8, TLA I2 | HIGH | `test_a_recreate_under_same_name_does_not_delete_new_config` |
| B | Pull compares only `_config.yml`: local edits in `transform.sql`/`code.py`/`_description.md` are silently overwritten when the remote changed, no SYNC_CONFLICT | Lean F6 | HIGH | `test_b_pull_never_overwrites_local_sql_edit` |
| C | Remote delete + local edit: pull (plain/`--force`) deletes the locally-edited directory with no conflict | Lean F7, TLA I5 | HIGH | `test_c_pull_never_deletes_locally_edited_dir_on_remote_delete` |
| D | `sync push --branch dev` (promote) creates another dev copy of a prod-only config on every push. **Fixed** (fix/792-dev-promote-duplicates): on the promote path the target-branch entry shadows the production entry for the same `main/` dir (`sync/branch_scope.py::_promoted_paths`) | TLA I6/I1 | HIGH | `test_d_promote_push_is_idempotent`, `test_d_promote_push_diff_is_clean_and_edits_update_the_dev_copy` (regression guards) |
| E | An untracked file carrying a config id (`config new --push --output-dir` scaffold / adopted orphan) is diffed 2-way: push overwrites a UI edit made after the scaffold was written | TLA I11 | MED | `test_e_adopted_scaffold_push_does_not_overwrite_remote_edit` |
| F | A push aborted by `ENCRYPTION_FAILED` leaves the manifest unsaved; the retry duplicates the change(s) the aborted push already applied | TLA I1b (model trace only; replayed live for this pilot) | MED | `test_f_aborted_push_does_not_duplicate_already_created_config` |
| G | `sync push` deletes remote configs with no `--force`; the CLI help text says `--force` gates deletion (soft delete to trash since 0.89.0, restorable) | Spec S1, Lean F1, TLA I2b | MED (product decision) | `test_g_push_without_force_does_not_delete_remote_config` |
| H | A config deleted remotely by another actor is silently re-created by the next push, no warning | Spec S2, Lean F2, TLA I11b | MED | `test_h_push_does_not_silently_resurrect_deleted_config` |
| I | Moving a config's directory by hand (`mv`/`git mv`) is seen as remote DELETE + CREATE under a new id | Lean F3 | LOW-MED | `test_i_moving_config_dir_is_not_delete_plus_create` |
| J | `sync pull --branch dev` reports untouched production configs as "removed" | Spec S4 | LOW | `test_j_branch_scoped_pull_does_not_report_other_branch_configs_removed` |
| K | A cosmetic local edit (raw vs normalized hash) blocks pull from ever applying a real remote change; `--force` raises a conflict | Spec S3, TLA I12 | LOW (documented, conservative behavior) | `test_k_cosmetic_edit_is_conservative_not_unsafe` (unmarked regression guard, not xfail) |

A..C, E, F, H and I reproduce on current code and are `xfail(strict=True)` (D is fixed) --
flipping to a hard failure the moment a fix lands is the point: delete the
`xfail` marker to adopt the fix. G is kept `xfail` too even though the fix
direction is a product decision (see the test's docstring). J reproduces and
is `xfail`. K is deliberate, documented behavior, so it is an ordinary
(unmarked) regression guard instead.
