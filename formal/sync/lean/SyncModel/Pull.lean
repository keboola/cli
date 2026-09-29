/-
`sync pull` per-config decision (plain / --force / --theirs), and the
stale-entry sweep. Hashes are abstract `Nat`s: `curHash` / `pullHash` are RAW
sha256 values of `_config.yml`; `cfgBase` / `apiHash` are normalized
`config_hash` values. `dry_run` does not change the decision, only whether the
chosen write happens, so it is not modelled.
-/
import SyncModel.Diff

namespace SyncModel

structure PullIn where
  isNew : Bool           -- key not in the old manifest (`sync_service.py:682`)
  theirs : Bool
  force : Bool
  fileExists : Bool      -- `_config.yml` present at the tracked path
  pullHash : Option Nat  -- stored `pull_hash` ("" = none)
  curHash : Nat          -- current raw hash of `_config.yml`
  extrasChanged : Bool   -- some `pull_extra_hashes` companion (transform.sql, code.py, ...) differs or is missing
  shapeMigration : Bool  -- `needs_shape_migration(...)` (`_sync_baseline.py:265-285`)
  cfgBase : Option Nat   -- effective stored `pull_config_hash`
  apiHash : Nat          -- `config_hash` of the freshly fetched remote
  branchSwitched : Bool  -- `sync_service.py:830-832`
  typeRewrite : Bool     -- data-app type rewrite (`:855-858`)
  deriving DecidableEq, Repr

inductive PullOut where
  | abort      -- SyncConflictError before any write
  | preserve   -- local files kept, old baseline kept
  | skip       -- idempotent, nothing written
  | write      -- `_config.yml` AND companion code files rewritten from remote
  deriving DecidableEq, Repr

/-- `_config.yml` differs from its recorded pull state (`sync_service.py:768-774`). -/
def ymlModified (p : PullIn) : Bool :=
  match p.pullHash with
  | some h => p.fileExists && decide (p.curHash ≠ h)
  | none => false

/-- Force-pull guard: `sync_service.py:654-667` calling `detect_force_pull_conflicts`
/ `_is_conflict` (`_sync_baseline.py:423-445`, `:447-507`). Only `_config.yml` is
hashed; brand-new remote configs are skipped (`:494-495`). -/
def forceConflict (p : PullIn) : Bool :=
  p.force && !p.theirs && !p.isNew &&
  match p.pullHash, p.cfgBase with
  | some h, some b => p.fileExists && decide (p.curHash ≠ h) && decide (p.apiHash ≠ b)
  | _, _ => false

/-- `locally_modified`, `sync_service.py:766-790`: only `_config.yml`, except
during a #686 shape migration where companions are checked too (`:780-790`). -/
def locallyModified (p : PullIn) : Bool :=
  !p.isNew && !p.theirs && (ymlModified p || (p.shapeMigration && p.extrasChanged))

/-- `_local_files_match_pull_state`, `sync_service.py:442-465`. -/
def bytesMatch (p : PullIn) : Bool :=
  match p.pullHash with
  | some h => p.fileExists && decide (p.curHash = h) && !p.extrasChanged
  | none => false

/-- `remote_unchanged`, `sync_service.py:829-858`. -/
def remoteUnchanged (p : PullIn) : Bool :=
  let r := !p.isNew && !p.branchSwitched && p.cfgBase == some p.apiHash && p.fileExists
  let r := if p.theirs then r && bytesMatch p else r
  r && !p.typeRewrite

/-- Per-config pull decision, `sync_service.py:654-667` then `:792-871`. -/
def pullDecide (p : PullIn) : PullOut :=
  if forceConflict p then .abort
  else if locallyModified p then .preserve
  else if remoteUnchanged p then .skip
  else .write

/-- Stale-entry sweep, `sync_service.py:1062-1094`: an OLD manifest entry whose
key is missing from this fetch gets `rmtree(branch_dir / old_cfg.path)` --
unconditionally (no local-modification check, no --theirs check). -/
def sweepRemovesDir (keyInFetch dirExists : Bool) : Bool := !keyInFetch && dirExists

/-! ## Proved -/

/-- P1 (I5): `pull --force` aborts on a true conflict in `_config.yml`. -/
theorem force_conflict_aborts (p : PullIn) (h b : Nat)
    (hf : p.force = true) (ht : p.theirs = false) (hn : p.isNew = false)
    (hp : p.pullHash = some h) (he : p.fileExists = true) (hc : p.curHash ≠ h)
    (hb : p.cfgBase = some b) (ha : p.apiHash ≠ b) : pullDecide p = .abort := by
  simp [pullDecide, forceConflict, hf, ht, hn, hp, hb, he, hc, ha]

/-- P2: without --theirs an edited `_config.yml` of a tracked config is never
overwritten by pull (abort or preserve). -/
theorem yml_edit_not_overwritten (p : PullIn) (ht : p.theirs = false) (hn : p.isNew = false)
    (hm : ymlModified p = true) : pullDecide p = .abort ∨ pullDecide p = .preserve := by
  unfold pullDecide
  by_cases hc : forceConflict p = true
  · simp [hc]
  · have : locallyModified p = true := by simp [locallyModified, ht, hn, hm]
    simp [hc, this]

/-- P3: --theirs never preserves and never aborts (remote wins). -/
theorem theirs_remote_wins (p : PullIn) (ht : p.theirs = true) :
    pullDecide p = .skip ∨ pullDecide p = .write := by
  have h1 : forceConflict p = false := by simp [forceConflict, ht]
  have h2 : locallyModified p = false := by simp [locallyModified, ht]
  unfold pullDecide; rw [h1, h2]; simp only [Bool.false_eq_true, ite_false]
  split <;> simp

/-- P4: under --theirs, an idempotent skip implies every tracked file is
byte-identical to its pull state (edited companions are re-materialized). -/
theorem theirs_skip_requires_clean (p : PullIn) (ht : p.theirs = true)
    (hs : pullDecide p = .skip) : bytesMatch p = true := by
  have h1 : forceConflict p = false := by simp [forceConflict, ht]
  have h2 : locallyModified p = false := by simp [locallyModified, ht]
  unfold pullDecide at hs; rw [h1, h2] at hs
  simp only [Bool.false_eq_true, ite_false] at hs
  split at hs
  · rename_i hr
    simp only [remoteUnchanged, ht, ite_true, Bool.and_eq_true] at hr
    exact hr.1.2
  · cases hs

/-- P5: pull is idempotent -- nothing changed on either side => nothing written. -/
theorem idempotent_skip (p : PullIn) (h : Nat)
    (hn : p.isNew = false) (ht : p.theirs = false) (hp : p.pullHash = some h)
    (he : p.fileExists = true) (hc : p.curHash = h) (hx : p.shapeMigration = false)
    (hb : p.cfgBase = some p.apiHash) (hs : p.branchSwitched = false) (hty : p.typeRewrite = false) :
    pullDecide p = .skip := by
  simp [pullDecide, forceConflict, locallyModified, ymlModified, remoteUnchanged,
    hn, ht, hp, he, hc, hx, hb, hs, hty]

/-! ## Refuted (FINDINGS) -/

/-- F6. Documented: "Pull protects local edits: locally-modified files are
skipped by default" (sync-workflow.md "Key behaviors") and "--force: local
edited AND remote changed -> abort". Stated over ALL tracked files: FALSE.
Only `_config.yml` is compared, so an edit to `transform.sql` / `code.py` /
`_description.md` is overwritten whenever the remote changed -- by plain pull
AND by `pull --force` (no SYNC_CONFLICT). Acknowledged in a code comment
(`_sync_baseline.py:289-294`) but not in user docs. Reproduced against the real
SyncService (scratch repro R1). -/
def PullNeverOverwritesLocalEdits : Prop :=
  ∀ p : PullIn, p.theirs = false → p.isNew = false →
    (ymlModified p || p.extrasChanged) = true → pullDecide p ≠ .write

/-- Only transform.sql edited; remote changed since the pull. -/
def pSqlEdit (force : Bool) : PullIn :=
  { isNew := false, theirs := false, force := force, fileExists := true,
    pullHash := some 1, curHash := 1, extrasChanged := true, shapeMigration := false,
    cfgBase := some 10, apiHash := 11, branchSwitched := false, typeRewrite := false }

theorem pullNeverOverwritesLocalEdits_refuted : ¬ PullNeverOverwritesLocalEdits := by
  intro h; exact h (pSqlEdit false) rfl rfl rfl (by decide)

theorem forcePull_sqlEdit_writes : pullDecide (pSqlEdit true) = .write := by decide

/-- F7. Pull-side loss of a `_config.yml` edit, including the stale sweep:
"pull without --theirs never destroys an edited `_config.yml`". P2 proves it for
configs still on the remote; FALSE when the remote config was deleted: the sweep
`rmtree`s the edited directory (`sync_service.py:1078-1094`). Reproduced (R2). -/
def PullNeverDeletesEditedDir : Prop :=
  ∀ (p : PullIn) (remoteDeleted : Bool), p.theirs = false → p.isNew = false →
    ymlModified p = true →
    (if remoteDeleted then sweepRemovesDir false p.fileExists else pullDecide p == .write) = false

theorem pullNeverDeletesEditedDir_refuted : ¬ PullNeverDeletesEditedDir := by
  intro h
  have := h { pSqlEdit false with curHash := 2, extrasChanged := false } true rfl rfl (by decide)
  revert this; decide

/-- The same property restricted to configs still on the remote holds (= P2). -/
theorem pullNeverDeletesEditedDir_liveRemote (p : PullIn) (ht : p.theirs = false)
    (hn : p.isNew = false) (hm : ymlModified p = true) : (pullDecide p == .write) = false := by
  rcases yml_edit_not_overwritten p ht hn hm with h | h <;> simp [h]

/-! ### F8: same-name re-create -> fresh config deleted locally -> push DELETEs it

`used_paths` is per-pull (`sync_service.py:724-728`) and never contains the
paths of OLD entries that disappeared from the fetch. When the remote config X
at path P was deleted and a NEW config Y of the same component with the same
name appears (UI delete + re-create, or a rename collision), Y is generated at
the same P and WRITTEN (`:862-871`); the sweep then `rmtree`s P for X
(`:1078-1094`), deleting Y's fresh files. Y stays in the manifest with a
non-empty pull_hash, so the next `diff` classifies Y `deleted` and `push`
DELETEs the config that was just created remotely. Reproduced (R3). -/

/-- Directory state at P after one pull: write loop first, sweep second. -/
def dirAfterPull (writtenThisPull sweptOldEntryAtSamePath existedBefore : Bool) : Bool :=
  if sweptOldEntryAtSamePath then false else writtenThisPull || existedBefore

/-- Y's manifest entry after the pull, when its generated path collides (or not)
with a vanished old entry's path. -/
def yEntryAfterPull (collides : Bool) (h : Nat) : Entry :=
  { pullHashSet := true, fileExists := dirAfterPull true collides true,
    inSourceTree := true, localHash := h, base := some h }

def yWorld (collides : Bool) (h : Nat) : KeyWorld :=
  { ignored := false, entry := some (yEntryAfterPull collides h), otherTreeClaim := false,
    remote := some h, untracked := [] }

/-- "A config just brought in by `sync pull` and not touched locally is never
planned as a remote DELETE by the next push." -/
def FreshPullNeverDeleted : Prop :=
  ∀ collides h, Action.delete ∉ pushPlan false (yWorld collides h)

theorem freshPullNeverDeleted_refuted : ¬ FreshPullNeverDeleted := by
  intro hf; exact hf true 0 (by decide)

/-- Without a path collision the property holds. -/
theorem freshPull_noCollision_safe (h : Nat) : pushPlan false (yWorld false h) = [] := by
  simp [pushPlan, diffKey, locals, trackedLocals, yWorld, yEntryAfterPull, dirAfterPull,
    KeyWorld.inTree, KeyWorld.part, scopeEntry, KeyWorld.effRemote, classifyLocal,
    classifyExisting, seen, sweep]

end SyncModel
