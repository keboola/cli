/-
Diff classification + push planning, per config key.

Abstraction: one config key `(component_id, config_id)` on the TARGET branch.
Hashes are abstract `Nat`s standing for `config_hash(...)` values
(`diff_engine.py:142-145`); equal hash = equal normalized content. Row keys are
modelled separately at the end of the file.

All line numbers refer to this worktree as of 2026-09-26.
-/
namespace SyncModel

/-- `ConfigChange.change_type` (`diff_engine.py:308-443`). `none` = unchanged. -/
inductive Change where
  | added | modified | remoteModified | conflict | deleted
  deriving DecidableEq, Repr

/-- 3-way compare of an entry whose id resolves on the remote.
Mirrors `diff_engine.py:377-402`: equal hashes -> skipped (`:379-381`);
no base -> 2-way fallback `local_changed=True, remote_changed=False`
(`:389-391`), i.e. `modified`; otherwise conflict / remote_modified / modified
(`:393-402`). -/
def classifyExisting (lh rh : Nat) (base : Option Nat) : Option Change :=
  if lh = rh then none else
  match base with
  | none => some .modified
  | some b =>
    if lh ≠ b ∧ rh ≠ b then some .conflict
    else if rh ≠ b ∧ lh = b then some .remoteModified
    else some .modified

/-- One local entry fed to `compute_changeset`.
Mirrors `diff_engine.py:344-368`: no `config_id`, or id not in
`remote_configs` -> `added`; else `classifyExisting`. -/
def classifyLocal (idKnown : Bool) (remote : Option Nat) (lh : Nat) (base : Option Nat) :
    Option Change :=
  if idKnown then
    match remote with
    | none => some .added
    | some rh => classifyExisting lh rh base
  else some .added

/-- Tail loop `diff_engine.py:417-441`: a remote key not seen among local
entries is `deleted` iff it is in `tracked_keys`. (`diff()` always passes a
non-`None` set, `sync_service.py:1455-1461`.) -/
def sweep (remotePresent seen tracked : Bool) : Option Change :=
  if remotePresent && !seen && tracked then some .deleted else none

/-- Partition of one manifest entry, `scope_manifest` `branch_scope.py:163-201`. -/
inductive Part where
  | dropped | neverFetched | inTree | orphaned
  deriving DecidableEq, Repr

/-- `branch_scope.py:163-201`: ignored component -> dropped (`:174-175`);
empty `pull_hash` and no `_config.yml` -> never_fetched (`:183-193`);
tree == source tree -> in_tree (`:197-199`); else orphaned (`:201-203`). -/
def scopeEntry (ignored pullHashSet fileExists inSourceTree : Bool) : Part :=
  if ignored then .dropped
  else if !pullHashSet && !fileExists then .neverFetched
  else if inSourceTree then .inTree else .orphaned

/-- `classify_untracked` verdicts (`branch_scope.py:63-65`). -/
inductive Verdict where
  | create | adopt | orphan
  deriving DecidableEq, Repr

/-- `classify_untracked`, `branch_scope.py:263-279`. `held` = claims by any
tree; `sameTreeClaim` = a claim from the source tree. -/
def classifyUntracked (idKnown sameTreeClaim otherTreeClaim remoteHas : Bool) : Verdict :=
  if !idKnown then .create
  else if sameTreeClaim then .create
  else if remoteHas then .adopt
  else if sameTreeClaim || otherTreeClaim then .orphan
  else .create

/-- The manifest entry for the key (at most one per key per tree). -/
structure Entry where
  pullHashSet : Bool   -- `metadata.pull_hash` non-empty
  fileExists : Bool    -- `_config.yml` readable at `tree/cfg.path` (`sync_service.py:1325-1328`)
  inSourceTree : Bool  -- `branch_tree_path(cfg.branch_id) == source_branch_path`
  localHash : Nat      -- local_h: override hash or `config_hash(merged data)` (`:1364-1368`, `diff_engine.py:374-375`)
  base : Option Nat    -- `base_hashes[key]` (`sync_service.py:1440-1453`)
  deriving DecidableEq

/-- An untracked `_config.yml` in the source tree (`find_untracked_configs`,
`branch_scope.py:307-373`) whose `_keboola.component_id` is this key's component.
`idKnown` = its `_keboola.config_id` is this key's id (false = id-less file). -/
structure Untracked where
  idKnown : Bool
  localHash : Nat
  deriving DecidableEq

/-- Everything `diff()` sees about one key. -/
structure KeyWorld where
  ignored : Bool                 -- component in `_effective_ignored_components` (`sync_service.py:468-480`)
  entry : Option Entry
  otherTreeClaim : Bool          -- another (non-never-fetched) manifest entry for this key in ANOTHER tree
  remote : Option Nat            -- remote config on the target branch (hash), before the ignore filter
  untracked : List Untracked

/-- `remote_configs` skips ignored components (`sync_service.py:1279-1282`). -/
def KeyWorld.effRemote (w : KeyWorld) : Option Nat :=
  if w.ignored then none else w.remote

def KeyWorld.part (w : KeyWorld) : Option Part :=
  w.entry.map fun e => scopeEntry w.ignored e.pullHashSet e.fileExists e.inSourceTree

/-- key ∈ `scope.tracked_keys` (`branch_scope.py:118-121`). -/
def KeyWorld.inTree (w : KeyWorld) : Bool := w.part == some .inTree

/-- claims held by another tree (`branch_scope.py:195`, orphaned entries claim too). -/
def KeyWorld.otherClaim (w : KeyWorld) : Bool :=
  !w.ignored && (w.otherTreeClaim || w.part == some .orphaned)

/-- A local config dict handed to `compute_changeset`. -/
structure LocalCfg where
  idKnown : Bool
  lh : Nat
  base : Option Nat
  deriving DecidableEq

/-- In-tree entry whose file is readable (`sync_service.py:1323-1376`). -/
def trackedLocals (w : KeyWorld) : List LocalCfg :=
  match w.entry with
  | some e => if w.inTree && e.fileExists then [⟨true, e.localHash, e.base⟩] else []
  | none => []

/-- Untracked walk, `sync_service.py:1382-1435`: ignored -> skipped (`:1391-1392`);
ORPHAN -> reported only (`:1411-1420`); CREATE -> id cleared (`:1421-1422`);
ADOPT -> keeps id. `base_hashes` is built only from `scope.in_tree`
(`:1443-1453`), and ADOPT implies no in-tree claim, so an adopted file has no base. -/
def untrackedLocal (w : KeyWorld) (u : Untracked) : Option LocalCfg :=
  if w.ignored then none else
  match classifyUntracked u.idKnown w.inTree w.otherClaim w.effRemote.isSome with
  | .orphan => none
  | .create => some ⟨false, u.localHash, none⟩
  | .adopt => some ⟨true, u.localHash, none⟩

def locals (w : KeyWorld) : List LocalCfg :=
  trackedLocals w ++ w.untracked.filterMap (untrackedLocal w)

/-- `seen_remote_keys` membership for this key (`diff_engine.py:365-366`, `:371`). -/
def seen (w : KeyWorld) : Bool := (locals w).any (·.idKnown)

/-- The changes `diff()` emits for this key (config level). -/
def diffKey (w : KeyWorld) : List Change :=
  (locals w).filterMap (fun l => classifyLocal l.idKnown w.effRemote l.lh l.base) ++
  (match sweep w.effRemote.isSome (seen w) w.inTree with
   | some c => [c]
   | none => [])

/-- Remote API writes issued by push. -/
inductive Action where
  | create | update | delete
  deriving DecidableEq, Repr

/-- `sync_service.py:1620-1621` (pushable = added/modified/deleted) and the
Phase-A dispatch `:1726` / `:1786` / `:1826-1831`. `force` is accepted by
`push()` (`:1579`) but never read in its body -- modelled faithfully as unused. -/
def pushAction (_force : Bool) : Change → Option Action
  | .added => some .create
  | .modified => some .update
  | .deleted => some .delete
  | _ => none

def pushPlan (force : Bool) (w : KeyWorld) : List Action :=
  (diffKey w).filterMap (pushAction force)

/-! ## Proved theorems -/

/-- T1: the 3-way compare emits nothing iff the hashes agree. -/
theorem classifyExisting_none_iff (lh rh : Nat) (b : Option Nat) :
    classifyExisting lh rh b = none ↔ lh = rh := by
  unfold classifyExisting
  by_cases h : lh = rh
  · simp [h]
  · simp only [h, ite_false, iff_false]
    cases b with
    | none => simp
    | some b =>
      by_cases h1 : lh ≠ b ∧ rh ≠ b <;> by_cases h2 : rh ≠ b ∧ lh = b <;> simp [h1, h2]

/-- T2..T5: the spec's classification table rows (`sync_model_spec.md` §3), each proved. -/
theorem table_twoWay (lh rh : Nat) (h : lh ≠ rh) :
    classifyExisting lh rh none = some .modified := by simp [classifyExisting, h]

theorem table_remoteModified (lh rh b : Nat) (h : lh ≠ rh) (hl : lh = b) :
    classifyExisting lh rh (some b) = some .remoteModified := by
  subst hl; have : rh ≠ lh := fun e => h e.symm
  simp [classifyExisting, h, this]

theorem table_modified (lh rh b : Nat) (h : lh ≠ rh) (hl : lh ≠ b) (hr : rh = b) :
    classifyExisting lh rh (some b) = some .modified := by
  subst hr; simp [classifyExisting, h]

theorem table_conflict (lh rh b : Nat) (h : lh ≠ rh) (hl : lh ≠ b) (hr : rh ≠ b) :
    classifyExisting lh rh (some b) = some .conflict := by
  simp [classifyExisting, h, hl, hr]

/-- T6: the spec's "unreachable" row really is unreachable: L=B and R=B force L=R. -/
theorem table_unreachable (lh rh b : Nat) (hl : lh = b) (hr : rh = b) :
    classifyExisting lh rh (some b) = none := by
  subst hl; subst hr; simp [classifyExisting]

/-- T7 (3-way soundness): with a base, `modified` (the only push-UPDATE verdict)
implies the remote is still exactly at the base -- push never clobbers a
remote change it can see. -/
theorem modified_with_base_sound (lh rh b : Nat) :
    classifyExisting lh rh (some b) = some .modified → lh ≠ b ∧ rh = b := by
  unfold classifyExisting
  by_cases h : lh = rh
  · simp [h]
  · simp only [h, ite_false]
    by_cases hl : lh = b <;> by_cases hr : rh = b <;> simp [hl, hr]
    · subst hl; subst hr; exact absurd rfl h

/-- T8: `conflict` only when both sides moved away from the base. -/
theorem conflict_sound (lh rh : Nat) (b : Option Nat) :
    classifyExisting lh rh b = some .conflict → ∃ b', b = some b' ∧ lh ≠ b' ∧ rh ≠ b' := by
  unfold classifyExisting
  by_cases h : lh = rh
  · simp [h]
  · cases b with
    | none => simp [h]
    | some b =>
      simp only [h, ite_false]
      by_cases hl : lh = b <;> by_cases hr : rh = b <;> simp [hl, hr]

theorem classifyLocal_ne_deleted (i : Bool) (r : Option Nat) (lh : Nat) (b : Option Nat) :
    classifyLocal i r lh b ≠ some .deleted := by
  unfold classifyLocal classifyExisting
  cases i <;> cases r <;> simp
  split <;> (try split) <;> (try split) <;> simp

theorem inTree_spec (w : KeyWorld) :
    w.inTree = true ↔ ∃ e, w.entry = some e ∧ w.ignored = false ∧
      e.inSourceTree = true ∧ (e.pullHashSet = true ∨ e.fileExists = true) := by
  unfold KeyWorld.inTree KeyWorld.part scopeEntry
  cases w.entry with
  | none => simp
  | some e =>
    cases hi : w.ignored <;> cases hp : e.pullHashSet <;> cases hf : e.fileExists <;>
      cases hs : e.inSourceTree <;> simp [hp, hf, hs]

/-- T9 (I2 as implemented + I7 + part of I4): a remote DELETE is planned only for
a key that is (a) of a non-ignored component, (b) tracked by a manifest entry in
the SOURCE tree, (c) whose `_config.yml` is gone, (d) that was materialized once
(non-empty pull_hash, so never_fetched entries are excluded), and (e) that exists
on the target remote. -/
theorem deleted_requires (w : KeyWorld) (h : Change.deleted ∈ diffKey w) :
    w.ignored = false ∧ ∃ e, w.entry = some e ∧ e.inSourceTree = true ∧
      e.fileExists = false ∧ e.pullHashSet = true ∧ w.remote.isSome = true := by
  unfold diffKey at h
  rcases List.mem_append.mp h with h1 | h2
  · obtain ⟨l, _, hl⟩ := List.mem_filterMap.mp h1
    exact absurd hl (classifyLocal_ne_deleted _ _ _ _)
  · unfold sweep at h2
    split at h2
    · rename_i c hc
      split at hc
      · rename_i hcond
        simp only [Bool.and_eq_true, Bool.not_eq_true'] at hcond
        obtain ⟨⟨hr, hs⟩, ht⟩ := hcond
        obtain ⟨e, he, hig, hsrc, hpf⟩ := (inTree_spec w).mp ht
        have hfe : e.fileExists = false := by
          cases hfx : e.fileExists
          · rfl
          · exfalso
            have : seen w = true := by
              unfold seen locals trackedLocals
              simp [he, ht, hfx]
            rw [this] at hs; exact Bool.noConfusion hs
        refine ⟨hig, e, he, hsrc, hfe, ?_, ?_⟩
        · rcases hpf with hp | hp
          · exact hp
          · rw [hfe] at hp; exact Bool.noConfusion hp
        · unfold KeyWorld.effRemote at hr; simpa [hig] using hr
      · simp at hc
    · simp at h2

/-- T10 (I7): a never-fetched manifest entry is never planned as a remote DELETE. -/
theorem neverFetched_no_delete (w : KeyWorld) (e : Entry) (he : w.entry = some e)
    (hp : e.pullHashSet = false) : Change.deleted ∉ diffKey w := by
  intro h
  obtain ⟨_, e', he', _, _, hp', _⟩ := deleted_requires w h
  rw [he] at he'; cases he'; rw [hp] at hp'; exact Bool.noConfusion hp'

/-- T11 (I3): an ignored component yields no change at all, hence no remote write
of any kind (create / update / delete). -/
theorem ignored_no_changes (w : KeyWorld) (hi : w.ignored = true) : diffKey w = [] := by
  have hr : w.effRemote = none := by simp [KeyWorld.effRemote, hi]
  have ht : w.inTree = false := by
    unfold KeyWorld.inTree KeyWorld.part scopeEntry
    cases w.entry <;> simp [hi]
  have hu : w.untracked.filterMap (untrackedLocal w) = [] := by
    simp [List.filterMap_eq_nil_iff, untrackedLocal, hi]
  have htl : trackedLocals w = [] := by
    unfold trackedLocals; cases w.entry <;> simp [ht]
  simp [diffKey, locals, htl, hu, sweep, hr, ht]

theorem ignored_no_remote_write (w : KeyWorld) (f : Bool) (hi : w.ignored = true) :
    pushPlan f w = [] := by
  simp [pushPlan, ignored_no_changes w hi]

/-- T12 (I4): an entry tracked only on ANOTHER branch's tree, with no untracked
file in the source tree, contributes nothing to the target branch's changeset. -/
theorem otherTree_no_changes (w : KeyWorld) (e : Entry) (he : w.entry = some e)
    (hs : e.inSourceTree = false) (hu : w.untracked = []) : diffKey w = [] := by
  have ht : w.inTree = false := by
    unfold KeyWorld.inTree KeyWorld.part scopeEntry
    rw [he]; cases hi : w.ignored <;> cases hp : e.pullHashSet <;> cases hf : e.fileExists <;> simp [hp, hf, hs]
  have htl : trackedLocals w = [] := by unfold trackedLocals; rw [he]; simp [ht]
  simp [diffKey, locals, htl, hu, sweep, ht]

/-- T13: an in-tree file untouched since pull (local hash = stored base) never
yields a pushable UPDATE/CONFLICT while its id still resolves remotely. -/
theorem unchanged_existing_no_update (lh rh : Nat) :
    classifyExisting lh rh (some lh) ≠ some .modified ∧
    classifyExisting lh rh (some lh) ≠ some .conflict := by
  unfold classifyExisting
  by_cases h : lh = rh
  · simp [h]
  · have : rh ≠ lh := fun e => h e.symm
    simp [h, this]

/-- T14: bounded output -- one verdict per local entry plus at most one delete. -/
theorem diffKey_length_le (w : KeyWorld) : (diffKey w).length ≤ w.untracked.length + 2 := by
  unfold diffKey
  have h1 := List.length_filterMap_le (fun l => classifyLocal l.idKnown w.effRemote l.lh l.base) (locals w)
  have h2 : (locals w).length ≤ w.untracked.length + 1 := by
    unfold locals
    have := List.length_filterMap_le (untrackedLocal w) w.untracked
    have ht : (trackedLocals w).length ≤ 1 := by
      unfold trackedLocals; split
      · split <;> simp
      · simp
    simp only [List.length_append]; omega
  have h3 : (match sweep w.effRemote.isSome (seen w) w.inTree with
      | some c => [c] | none => []).length ≤ 1 := by split <;> simp
  simp only [List.length_append]; omega

/-! ## Refuted properties (FINDINGS). Statement kept, negation proved. -/

/-- A world: tracked in the source tree, directory removed, remote still there. -/
def wDirDeleted : KeyWorld :=
  { ignored := false,
    entry := some { pullHashSet := true, fileExists := false, inSourceTree := true,
                    localHash := 0, base := some 0 },
    otherTreeClaim := false, remote := some 0, untracked := [] }

/-- F1 (spec S1). The CLI documents `sync push --force` as "Allow deletion of
remote configs that were removed locally" (`commands/sync.py:982-986`), i.e.
"without --force no remote DELETE". FALSE: `push()` never reads `force`. -/
def DeleteRequiresForce : Prop := ∀ w, Action.delete ∉ pushPlan false w

theorem deleteRequiresForce_refuted : ¬ DeleteRequiresForce := by
  intro h; exact h wDirDeleted (by decide)

/-- F2 (spec S2). "Push only sends LOCAL changes": a tracked config whose files
are untouched since pull never causes a remote write. FALSE: if another actor
deleted (or trashed) it remotely, `remote_key not in remote_configs` makes it
`added` (`diff_engine.py:355`) and push re-creates it under a new id. -/
def UnchangedLocalNoWrite : Prop :=
  ∀ w e, w.entry = some e → e.fileExists = true → e.base = some e.localHash →
    w.untracked = [] → pushPlan false w = []

def wRemoteGone : KeyWorld :=
  { ignored := false,
    entry := some { pullHashSet := true, fileExists := true, inSourceTree := true,
                    localHash := 7, base := some 7 },
    otherTreeClaim := false, remote := none, untracked := [] }

theorem unchangedLocalNoWrite_refuted : ¬ UnchangedLocalNoWrite := by
  intro h
  have := h wRemoteGone _ rfl rfl rfl rfl
  revert this; decide

/-- F3. "A config whose directory still exists in the source tree (e.g. after
`mv` / `git mv` to a new folder name) is never deleted remotely." FALSE: the old
path's entry has no file -> `deleted`; the moved copy carries the same id but is
claimed by the same-tree entry -> CREATE (`branch_scope.py:273-274`). Push
DELETEs the original and CREATEs a new id. -/
def ContentPresentNoDelete : Prop :=
  ∀ w, (∃ u ∈ w.untracked, u.idKnown = true) → Action.delete ∉ pushPlan false w

def wMovedDir : KeyWorld :=
  { wDirDeleted with untracked := [{ idKnown := true, localHash := 0 }] }

theorem contentPresentNoDelete_refuted : ¬ ContentPresentNoDelete := by
  intro h
  exact h wMovedDir ⟨_, List.mem_singleton.mpr rfl, rfl⟩ (by decide)

theorem movedDir_plan : pushPlan false wMovedDir = [.create, .delete] := by decide

/-- F4. "Push issues at most one UPDATE per remote config." FALSE: two untracked
directories carrying the same `_keboola.config_id` that resolves remotely and is
claimed by no manifest entry both ADOPT (`branch_scope.py:275-276`), so the
fork-by-copy protection of #482/#497 does not apply and both PUT the same id;
the last one silently wins. -/
def AtMostOneUpdate : Prop :=
  ∀ w, ((pushPlan false w).filter (· == .update)).length ≤ 1

def wTwoAdopts : KeyWorld :=
  { ignored := false, entry := none, otherTreeClaim := false, remote := some 5,
    untracked := [{ idKnown := true, localHash := 1 }, { idKnown := true, localHash := 2 }] }

theorem atMostOneUpdate_refuted : ¬ AtMostOneUpdate := by
  intro h; have := h wTwoAdopts; revert this; decide

/-- F5. "A push UPDATE only happens when the remote is still at the base the
local edit was made against" (the 3-way guarantee). FALSE for adopted untracked
files: they never have a base (2-way fallback, `diff_engine.py:389-391`), so a
remote edit made by someone else is overwritten without a `conflict`. (T7 shows
it DOES hold for tracked entries that have a base.) -/
def UpdateOnlyAtBase : Prop :=
  ∀ w, ∀ l ∈ locals w, classifyLocal l.idKnown w.effRemote l.lh l.base = some .modified →
    ∃ b, l.base = some b ∧ w.effRemote = some b

def wAdoptRemoteMoved : KeyWorld :=
  { ignored := false, entry := none, otherTreeClaim := false, remote := some 9,
    untracked := [{ idKnown := true, localHash := 1 }] }

theorem updateOnlyAtBase_refuted : ¬ UpdateOnlyAtBase := by
  intro h
  obtain ⟨b, hb, _⟩ := h wAdoptRemoteMoved ⟨true, 1, none⟩ (by decide) (by decide)
  cases hb

/-! ## Rows (`compute_row_changeset`, `diff_engine.py:446-575`) -/

/-- Row-level delete for one row key: `tracked_row_keys` is filled only from
parents in `scope.in_tree` (`sync_service.py:1472-1476`); a row is `seen` iff
its parent is in-tree and its file is readable (`:1478-1490`); tail loop
`diff_engine.py:555-575`. -/
def rowDeleted (parent : Part) (rowFileExists remoteRowPresent : Bool) : Bool :=
  let tracked := parent == .inTree
  let seenRow := tracked && rowFileExists
  remoteRowPresent && !seenRow && tracked

/-- T15 (I10): a row delete is planned only when its parent is in the source tree
(never for never-fetched / other-branch / ignored parents). -/
theorem rowDeleted_requires_parent_inTree (p : Part) (f r : Bool) :
    rowDeleted p f r = true → p = .inTree := by
  cases p <;> simp [rowDeleted]

end SyncModel
