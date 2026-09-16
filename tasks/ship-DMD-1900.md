# ship-task: DMD-1900 — kbagent merge-request command group (Layer 1)

Plan: docs/merge-requests-layer1.md @ 6fa0e55. Branch ms/dmd-1900/cli-layer-1 (in place, no worktree).
PR: #709 (draft; retitle from docs(rfc) to feat when implementation lands). Base: ms/dmd-1899/cli-layer-2
until #703 merges, then retarget to main.

- [x] Phase 0 — context
- [x] Phase 1 — plan (GATE) — approved 2026-09-03
- [x] Phase 2 — branch: ms/dmd-1900/cli-layer-1, in place
- [x] Phase 3 — implement
    - [x] 3.1 L2: resolution_candidate, warnings rename, get_merge_request_row (+tests, +layer2 doc)
    - [x] 3.2 skeleton: _handle_error, target resolution, FLAG_ESCALATIONS + registry, cli.py wiring
    - [x] 3.3 reads: list, detail, conflicts, diff + _merge_request_render.py
    - [x] 3.4 writes: create, update, request-review, approve, request-changes, merge, resolve
    - [x] 3.5 serve router + dependencies + OPENAPI_TAGS + test_server_router_calls
    - [x] 3.6 branch merge deprecation; convention #17 surfaces; gotchas (vNEXT); endpoints-gen, skill-gen
- [x] Phase 4 — make check green
- [x] Phase 5 — self-review loop: Opus+Sonnet+code-review, all in-scope fixed @ f32d013
- [x] Phase 6 — E2E: written (TestE2EMergeRequestLifecycle), skip-gated on feature + credentials; deferred (feature absent on kbagent-e2e, no local E2E creds)
- [x] Phase 7 — #735 (merged into L2 07:36 then undone by the L2 re-purification); replacement draft PR #736 vs ms/dmd-1899/cli-layer-2 @ d5291a5
- [~] Phase 8 — #736 ready for review 2026-09-04 (Zajca, padak requested; CI green); monitor armed; Copilot threads → me, human threads → Martin

## Hard stops
draft only · never merge/approve · --force-with-lease only (rejected → stop) · breaking existing tests → escalate · human review thread → ask

## Log
- 3.1 done: d165c3d (112 service tests green)
- 3.2+3.3 done: d165c3d (35 CLI tests)
- 3.2+3.3 done: 0610a07 (35 CLI tests)
- 3.4 done: 4f98986 (73 CLI tests)
- split at soft ceiling: ea27d52 (common/reads/writes/render)
- 3.5 done: 7497fa1 (12 routes, 14 router tests, endpoints-check green)
- 3.6 done: f9eb01d (all #17 surfaces, skill-check + command-sync green; E2E written, credential- and feature-gated skip)
- Phase 4: make check exit 0 @ 9509c69 (6626 passed; ty warning on scripts/hatch_build.py is pre-existing)
- Phase 5: three reviews (Opus 14, Sonnet 4, /code-review 10 -- NOTE: /code-review took the branch arg as TARGET and reviewed the L2 branch; findings still real). All in-scope findings fixed + pinned; L2 design/refactor items deferred to #703 (listed in the pending commit message).
- BLOCKED: `git commit` fails -- commit signing via 1Password unavailable to this process ("failed to fill whole buffer"). Review-round changes are STAGED, not committed. Same cause makes tests/test_release_kbagent_ai_kit_sync.py fail (9) -- environmental, unrelated. Needs Martin: unlock 1Password (or decide on an unsigned commit).
- Phase 7: PR #735 opened (draft). #709 title/body accidentally edited then restored.
- rebased onto L2 7cd1855 (Zajca 3rd review); conflicts in service+tests resolved (L2 logic kept, warnings rename kept, duplicated test classes removed); make check exit 0, 6661 passed; force-with-lease pushed d5291a5
- PR #736 opened (draft) after rebase; #735 is a dead MERGED record
- post-rebase alignment with L2 3rd review: 17cadc1 (error-code split, empty-envelope render, RFC wording); pushed
- rebased onto main (L2 squash 5281eef) via --onto; make check exit 0 (6663); force-with-lease pushed 9734b81; #736 retargeted to main, merge-order warning replaced
- RFC docs consolidated: ms/merge-requests-rfcs @ 2f62123 = truth (my L1 deltas merged in, followups F1 marked done); L1 rebuilt by Martin so docs live only in the first commit 445cd2f (identical to 2f62123); make check exit 0 on 2e2c808; NOT yet pushed
- followups F2-F5,F7 implemented (34f3240 -> replayed); RFC docs updated on ms/merge-requests-rfcs @ 647ddaf and L1 first commit rebuilt from it (d58fd98); make check exit 0 (6672); pushed
- rebased by Martin onto main 6e3d131 (#731): fixed ruff 0.16 ISC004 (render), md code-block formatting (RFCs @ d7547f2, first commit rebuilt), SERVE_COMMAND_MAP entries; make check exit 0 (6694); pushed
- Phase 8: ready for review @ df6e018, 13 commits
- Copilot Balanced round: 8 findings, all confirmed + fixed @ 2042c3b (E2E covers all 11 commands with a real conflict; L2 RFC rationale fixed on RFCs @ 76a2adb, first commit rebuilt); threads replied; pushed
- Zajca review (CHANGES_REQUESTED) + addendum: must-fix 1-5 implemented LOCALLY (not pushed, threads not answered -- human review hard stop); design push-backs pending Martin's decision
- 2026-09-11: static destructive model + auto-merge command (Martin's decision 2026-09-10); Zajca must-fixes 1-5 + non-blocking design push-backs resolved by the model change; RFCs @ d895f9e, L1 first commit rebuilt; make check green except changelog-check (pre-existing on main: 0.93.0/0.93.1 entries owed by a release PR); pushed 2e8dc0e (15 commits); monitor re-armed. PENDING: PR body refresh; replies to Zajca (human -> Martin decides).
- 2026-09-10: Zajca reply posted (issuecomment-5626484983), review re-requested from zajca; requested set now zajca,padak,soustruh
- 2026-09-11: Zajca round 2 (CHANGES_REQUESTED @ 2e8dc0e, 2 threads): 3984272224 `_envelope_holes` presence-only vs resolve guard -> mirrors guard (absent|None|blank name), pinned; 3984272227 `--external-id` cap exit 1 vs `--reason` exit 2 -> `_check_external_id` pre-check on create/update (narrow fix; global INVALID_ARGUMENT->2 remap NOT taken: ~20 raise sites in other groups), pinned x3. Commit 502cad5 -> replayed as e6954b4; RFCs @ fccb6b3 (both rules recorded), L1 first commit rebuilt e36ff48; make check green except changelog-check (pre-existing); pushed. PENDING Martin: approve replies (scratchpad/zajca-round2-replies.md), re-request review. Still owed: D (feature project-add on kbagent-e2e).
- 2026-09-14: Zajca round-2 replies posted (4002532057, 4002532194) under Martin's account; review re-requested from zajca (requested set: padak, soustruh, zajca). Waiting on round 3 / approval. Still owed: D (feature project-add on kbagent-e2e).
- 2026-09-15: rebased onto main fd280aa (releases 0.93.0/0.93.1/0.93.2 + CLI-12 telemetry + CLI-5 sync clone + CODEOWNERS). NO conflicts; L1's own files byte-identical (verified e6954b4..291026d touches only main's files); RFC docs still only in first commit 17494e2, identical to ms/merge-requests-rfcs @ fccb6b3. `make check` exit 0 -- changelog-check now PASSES (the pre-existing 0.93.0/0.93.1 debt was paid by main's release PRs). Force-pushed 291026d; Zajca's APPROVED survived the force-push.
