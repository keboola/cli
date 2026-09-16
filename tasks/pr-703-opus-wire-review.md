# Opus wire-truth review — PR #703 vs. reálné Connection API (2026-08-27)

Zadání: ověřit každý wire předpoklad L2 proti zdrojákům Connection (poučení z nálezu #2
prvního review — fixtures nejsou důkaz). 10 položek, verdikty: 7× CONFIRMED, 3× MISMATCH.

## Souhrn verdiktů

| # | Položka | Verdikt |
|---|---|---|
| 1 | MergeRequestResponse tvar; list == detail; activityLog aditivní | CONFIRMED (creator.id/reviewers[].id int, approverId string, branchFromId null přes FK ON DELETE SET NULL; detail/conflicts chtějí ADMIN token — voter; list ne) |
| 2 | Conflicts entry (bare array; componentId/configurationId string) | CONFIRMED (isDeleted = DEV strana) |
| 3 | Merge job results = MR dict s changeLog, state published | CONFIRMED |
| 4 | Merge 409 dva tvary | **MISMATCH** — konfliktní 409 MÁ code `storage.mergeRequests.validation` + konfliktní configy v `params.errors`; CLI else-branch mislabeloval cizí 409 a data zahazoval |
| 5 | Diff strany, rows round-trip, rebase kontrakt, nested JSON | CONFIRMED (3 ostré hrany: name:null projde presence checkem → 400; rebase chce config v dev branchi; forward-only version guard) |
| 6 | tokens/verify admin blok (id int; chybí pro non-admin) | CONFIRMED |
| 7 | Stavový automat pro allowed_actions | **MISMATCH** — `approve` z `approved` = 422 (transition jen in_review→in_review self-loop... resp. from: in_review); in_merge lock jen na SOX cestě; s 0 required approvals je approve 422 VŠUDE a in_review nedosažitelný |
| 8 | Create: 201 = MR response; non-default target 404; 1 MR/branch ever | CONFIRMED (+nepopsaná 422: creator nesmí být reviewer) |
| 9 | List bez server-side filtru | CONFIRMED (createdAt DESC, bez stránkování) |
| 10 | reviewers[].status + creator „cancel" trik | **MISMATCH** — creator nikdy nemůže být explicitní reviewer; explicitní revieweři stíní cizí decisions; decisions se zahazují bez `review_requested` kotvy, kterou skip_review NIKDY nezapíše ⇒ v default non-SOX projektu (0 approvals) jsou `rejected`/`closed`(self) NEDERIVOVATELNÉ z reviewers[] — status je vždy null. UI má TUTÉŽ vadu. Pravda žije v activity logu (changes_requested event, bez kotvy a stínění). |

## Klíčové citace (Connection)

- 409 serializace: `legacy-app/src/Common/Exception/JsonExceptionConverter/ExceptionConverter.php:99-125`
  (error = message, code = stringCode, params = context → konfliktní configy v params.errors);
  `MergeValidationException::getStringCode` → `storage.mergeRequests.validation` (`:174-177`).
- approve transition: `MergeRequestLifecycleStateMachine.php:47-50` (from: in_review only);
  `AddApprovalGuard.php:32-56` (ne creator, ne duplicitně, canReceiveApproval);
  `RequiredApprovalsCountProvider.php:136,166-169` (default 0 → canReceiveApproval vždy false).
- reviewers stínění: `MergeRequestResponseProvider.php:27-63`; kotva review_requested:
  `MergeRequestActivityLogRepository::findLastReviewRoundDecisions:66-91`; skip_review bez
  activity logu: `MergeRequestService.php:130-138`, `MergeRequestActivityEventType.php:25-28`.
- branchFromId null mechanismus: FK `ON DELETE SET NULL` (`init2025.sql:1596`) → nuluje ho až
  async smazání branche, ne stav → `_branch_from_id_of` guard je racy (čerstvě published MR
  může branchFromId ještě mít).
- Voter: `MergeRequestVoter.php:54-56` — detail/conflicts 403 pro non-admin token; list bez voteru.
- update privilege: non-SOX blokuje jen published/canceled (in_merge updatable); rebase in_merge
  lock jen v `canAccessInProtectedBranch` (SOX) — `StorageRouteGuard.php:99-125,297,400-421`.

## Zapracování (commit navazující na tenhle soubor)

- #4: `_CONFLICT_CODE = storage.mergeRequests.validation`; explicitní match obou tvarů, None
  fallback pro starší stacky, cizí 409 propadá nemapované; http_base nově nese `params` →
  `details["api_error_params"]`; MR_MERGE_CONFLICT details nesou konfliktní configy rovnou.
  Opraveny lživé komentáře (service, client, errors.py) + docs (error-codes, notes, RFC).
- #7: `approve` vyhozen z `approved` tuple; `update` přidán do `in_merge` (server ho tam bere);
  docstring: AddApprovalGuard gating + „s 0 approvals je approve 422 všude, in_review
  nedosažitelný". RFC decision table opravena.
- #10: fallback derivace ponechána, ale poctivě označena jako best-effort (docstring +
  RFC): rejected/closed(self) fungují jen při required-approvals ≥ 1 + bez stínění.
  Backend fix (DMD-1988) musí derivovat z activity logu / interního stavu — do issue
  přidán komentář; activity-log derivaci v CLI neděláme (draží detail, na list nejde vůbec).
- #5 hrany: falsy `name` na take straně = contract violation; v resolved body = caller error.
- #1 extras: race komentář u `_branch_from_id_of`; viewer docstring (detail stejně chce
  admin token — voter).
