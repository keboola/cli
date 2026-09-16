# Review — PR #703 (merge-request Layer 2, DMD-1899)

Self-review requested 2026-08-27. Branch `ms/dmd-1899/cli-layer-2`, 8 commits, 16 files,
+2545/−19. Verified locally: `ruff check` + `ruff format --check` clean, `make loc-check`
clean (service is under the 1000-line services budget), `scripts/check_error_codes.py` OK,
`make check-sentinel-guards` OK, **full suite green: 5768 passed / 61 skipped**.

Overall: this is well above the bar. Layer separation is exact (no HTTP in L2, no logic in
L3), the derivation polyfill is pure + module-level as the RFC decided, every non-obvious
decision has a comment pointing at the backend evidence or the Linear issue, and the
`compute_diff` refactor is a genuine behaviour-preserving extraction. The findings below are
mostly about the conflict-resolution half, which is the part with real blast radius.

---

## 1. `resolve_conflict` — the conflict-set guard does not cover the branch it writes to

`merge_request_service.py:714`, guard at `:822`

`_require_in_conflict_set` validates `component_id`/`config_id` against **merge request N's**
conflict set, but `get_config_diff` and the rebase then target the independently supplied
`branch_id`. Nothing checks that `branch_id == mr.branches.branchFromId`.

Failure scenario: an agent (or the future L1 with a stale `active_branch_id`) passes
`--mr-id 7` and a `--branch` that belongs to a *different* dev branch. The guard says "yes,
that config is conflicting" — for MR 7 — and the rebase then **replaces** that config's
content in the unrelated branch (rebase replaces, per `rebase_config`'s own docstring). Silent
data loss, with the PR's advertised service-level guard reporting success.

Recommended fix: drop `branch_id` from the signature and derive it from the MR
(`client.merge_requests.get(id)["branches"]["branchFromId"]`) — same "make the illegal state
unrepresentable in the signature" reasoning `get_config_diff`'s L3 docstring uses for the
default branch. It costs one GET, and `_require_in_conflict_set` already spends one. If you
want to keep the parameter, at minimum assert it against `branchFromId` and refuse on
mismatch. Consider the same for the public `get_config_diff` for symmetry (there it is only a
misleading read, not a write).

## 2. The `missing`-keys check blames the caller for a server-shaped problem

`:790`

The message — *"A resolved body must spell out the full replaced content (rebase REPLACES):
missing rows."* — is correct for `resolved={...}` and wrong for `take=ours|theirs`, where the
caller supplied no body at all and the keys are missing from the **diff side**.

Underneath that is a load-bearing unverified assumption: that `ConfigurationDiffResponse`
serializes `name`, `rows` and `configuration` on every side. `merge-requests-notes.md:201`
records the diff shape only as "`base`/`ours`/`theirs`, each nullable" — the per-side key set
is nowhere in the notes. If the live response omits `rows` (plausible — the diff is about
content), **both take modes are dead on arrival** and the unit tests cannot catch it, because
the `_side()` fixture is what defines the shape.

Two things: split the error so a take-mode failure reads "the diff's `theirs` side carries no
`rows` — cannot compose a replace body" (and points at `resolve --file`), and verify the
per-side key set against connection, recording it in the notes table like every other wire
fact there.

## 3. `take=theirs` has no deleted-side handling, `take=ours` does

`:777` handles `ours is None or ours.get("isDeleted")` → delete resolution. There is no
mirror for `theirs`. Per `DefaultConflictValidator`
(`merge-requests-notes.md:52-62`) a conflict is *not* raised only when **both** sides are
deleted — so "production deleted it, dev changed it" is a live conflict shape, and the
conflicts entry itself carries `isDeleted`. In that case `take=theirs` either composes a
replace body out of a tombstone or trips finding #2's message. Symmetric handling is a
two-line change.

## 4. `isDeleted` is excluded from the per-path classification — the code contradicts itself

`_DIFF_CONTENT_KEYS` at `:620` omits `isDeleted`, while `resolve_conflict:777` reads
`ours["isDeleted"]` as decisive. Consequence: for a config deleted on one side, `changes` shows
only content movement and the deletion — the single most consequential difference — produces
no entry at all. The L1 rendering ("Both changed / Only you changed / Only production
changed") would then be actively misleading.

Either add `isDeleted` to the tuple, or surface explicit `ours_deleted` / `theirs_deleted`
booleans on the `get_config_diff` result. I lean toward the booleans: a `changed_by` row for a
boolean flag reads oddly next to `configuration.limit`.

## 5. `changed_by: "both"` also fires when both sides made the *identical* change

`:690`. Two entries exist (each side differs from base) even when `ours.new == theirs.new` —
which in three-way terms is agreement, not a conflict. The RFC puts `both` in the "actual
conflict hotspots" section of the L1 table, so agreed changes get rendered as hotspots.
Cheapest fix: compare the two entries' `new` (and presence) and emit `both_same`, or keep
`both` and add an `agreed: bool`.

## 6. Wire-id comparisons bypass the module's own `_same_id`

`:537` — `project.active_branch_id == branch_from_id`, where `branch_from_id` comes straight
off the wire — and `:284` — `branchFromId == branch_id`. The module defines `_same_id` at `:65`
*precisely because* MR payload ids mix int and str (`approverId` is a string while `creator.id`
is a number). If `branches.branchFromId` ever serializes as a string, `was_active` silently
becomes `False` and a stale `active_branch_id` survives the merge, pointing at a branch the
server is deleting — the exact state the cleanup exists to prevent, and it would fail silently
(no warning path). `int()` coercion or `_same_id` at both sites.

## 7. Feature pre-flight has no machine-readable code

`_require_merge_requests_feature` raises a bare `ConfigError`, so a caller cannot tell
"feature not enabled" from "unknown project alias" — both arrive as the same shape with no
code. In-repo precedent goes the other way for exactly this case: `search_service.py:188`
emits `error_code: "FEATURE_NOT_ENABLED"`, and `PAYG_NOT_AVAILABLE` was added in #594 with the
rationale "a missing project feature is a configuration problem" so it *inherits the right
category instead of taking the default". Given the MCP/agent audience the docs keep invoking,
this one deserves a code.

## 8. Smaller things

- **`from ..config_store import ConfigError`** (`:26`) — the only service in the repo that does
  not import it from `..errors`; 20+ others do.
- **`_default_branch_id`** (`:476`) is the fifth copy of the `isDefault`-scan over
  `list_dev_branches()` (`config_service` ×2, `sync_service`, `workspace_service`, `lib.py`).
  Good moment to hoist it onto `BaseService`.
- **`verify_token()` is always paid** (`:325`) even when the server already serialized
  `viewer`, and even for terminal MRs where the flags are near-useless. Guard it with
  `if not isinstance(mr.get("viewer"), dict)` so the polyfill's *cost* disappears with the
  polyfill when DMD-1988 lands, not later.
- **`list_merge_requests` accepts any `state` string**; a typo returns `count: 0` with no hint.
  The vocabulary is closed and known here — a validation error naming the accepted values beats
  a silent empty list, and L2 is where the guard belongs.
- **Doc drift introduced by this PR**: `docs/merge-requests-layer2.md:96` still writes
  `has_feature(FEATURE_BRANCHES_MERGE_REQUESTS)` and cites `client/tokens.py:200` (now `:233`);
  `layer3.md:10, :121, :145` keep the old name, and `:145` lists the rename as an open nit that
  this PR just closed.

## 9. Tests

69 service tests, tight and well-named; the "server field wins" test per derivation is exactly
the right shape for a polyfill that gets deleted later.

- **Mid-file imports with 12 × `# noqa: E402`** (`:184-195`) — no precedent anywhere in
  `tests/`; the three `src/` files doing this are commands with a specific reason. Hoisting
  them to the top costs nothing.
- **`MagicMock()` without `spec`** for the client: this PR's whole value sits on the L3 seam
  shipped in #556, and a renamed/removed L3 method would keep every test green.
  `MagicMock(spec=KeboolaClient)` is cheap drift insurance at exactly the seam that matters.
- Gaps worth one test each: `_default_branch_id` with no default branch; `onto_version is None`
  (null `theirs`); a take mode against a diff side missing `rows` (finding #2); and the
  branch/MR mismatch of finding #1 once it is guarded.

## 10. On the PR body's CI caveats

- `make changelog-check` failure **confirmed** — the branch is **68 commits / 6 releases**
  behind `origin/main` (0.87.0–0.91.0 missing). A rebase fixes it. Heads-up on churn in the
  files this PR touches: `http_base.py` (+23/−4, `retry_safe` override), `json_utils.py` (+25,
  `find_matches_in_json` appended right where `compute_diff` moved), `client/tokens.py`
  (+73/−4, per-token last-used), `errors.py` (+8), `constants.py` (+69). Textual conflicts
  only, nothing structural.
- The two `tests/test_changelog_render.py` failures **did not reproduce** — full suite green
  here. Either narrow the claim to the environment that produces them, or drop it.

---

# Resolution (2026-08-27, commit 51eaa2c)

Vše zapracováno, force-pushed, PR body aktualizován. Po nálezech:

1. **PŘIJATO** — `branch_id` odstraněn ze signatury `resolve_conflict`, odvozuje se z
   `mr.branches.branchFromId` (`_branch_from_id_of`); published/canceled MR → čitelná chyba.
   `get_config_diff` parametr nechán (nemá mr_id, jen čtení).
2. **PŘIJATO + POVÝŠENO** — wire tvar ověřen v connection: strany NEJSOU ploché, obsah je
   vnořený pod `diff` ({version, isDeleted, diff:{name,description,changeDescription,
   isDisabled,configuration,rows}}). Původní kód i fixtures byly špatně → přepsáno obojí,
   fakt zapsán do notes wire-truth tabulky. Chybová hláška rozdělená (take = server
   contract violation + odkaz na resolved body; resolved = caller error).
3. **PŘIJATO** — take=theirs smazané strany → delete resoluce, symetricky s ours.
4. **PŘIJATO** — `ours_deleted`/`theirs_deleted` booleany (None = strana neexistuje);
   `changeDescription` vyloučen z klasifikace (commit message, ne obsah).
5. **PŘIJATO (varianta b)** — `agreed: bool` na `both` řádcích; changed_by zůstává 3hodnotový.
6. **PŘIJATO** — `_same_id` ve find; int koerce `branchFromId` v merge() (+ regresní testy).
7. **PŘIJATO (tvar dle precedentu)** — `ErrorCode.FEATURE_NOT_ENABLED` + `FeatureNotEnabledError(ConfigError)`
   (vzor SessionAuthUnsupportedError); hodnota shodná se stringem v search_service.
   Follow-up (nezapracováno): migrovat search_service na enum member.
8. **PŘIJATO vše** — import z ..errors; `find_default_branch_id` v services.base + migrace
   config/sync/workspace (lib.py záměrně ne — SDK neimportuje services); verify_token guard
   (přeskočen při serializovaném viewer); --state validace proti uzavřenému slovníku;
   doc drift (layer2:96, layer3:10/121/145, tokens.py:302).
9. **PŘIJATO** — importy nahoru bez noqa; MagicMock(spec=KeboolaClient) + spec=MergeRequests;
   gap testy: no-default-branch, null theirs, take side bez klíčů, closed MR, string wire id ×2,
   state typo, viewer guard. Celkem 82 testů.
10. Rebase udělal Martin; changelog-check zelený. Render faily: claim v PR zúžen na
    „v některých prostředích" (u mě reprodukovatelné na čistém mainu, u reviewera ne).
