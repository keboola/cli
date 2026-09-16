# DMD-1899 — Layer 2 (merge_request_service): plán a fronta rozhodnutí

Brainstorm plán; pracovní poznámky s fakty jsou v `docs/merge-requests-layer2.md`
(+ `-notes.md` backend fakta, `-layer3.md` as-built klient, `-layer1.md` budoucí UX).
Tenhle soubor drží **stav brainstormu**: co je rozhodnuté, co je ve frontě.

## Kostra: `services/merge_request_service.py`

DI: `ConfigStore` + `client_factory` (jako každá service). Metody 1:1 k budoucím L1
příkazům (DMD-1900):

| Metoda | Nad čím z L3 | Co přidává L2 |
|---|---|---|
| `list_merge_requests(state=None)` | `merge_requests.list()` | client-side `state` filtr, derived state na řádku |
| `find_merge_request_for_branch(branch_id)` | `merge_requests.list()` | filtr `branches.branchFromId` (branch má nejvýš 1 MR); podklad pro volitelný `--mr-id` v L1 |
| `get_merge_request(id)` | `.get()` (+ `conflicts()`) | derived state + merge readiness, defenzivní čtení `changeLog` / budoucích server polí |
| `create_merge_request(...)` | `.create()` | feature pre-flight, resoluce source branch (bod 2) |
| `update_merge_request(...)` | `.update()` | pre-flight, empty-string-clears sémantika |
| `request_review(id)` | `.request_review()` | pre-flight, mapování 422 |
| `approve(id)` | `.approve()` | pre-flight, mapování 422 |
| `request_changes(id, reason=None)` | `.request_changes()` | pre-flight, mapování 422 (reason cap 1000 hlídá server) |
| `merge(id)` | `.merge()` (awaituje job) | pre-flight, 409 → nové ErrorCode (bod 3), post-merge cleanup (bod 5) |
| `list_conflicts(id)` | `.conflicts()` | obohacení o názvy konfigurací? |
| `get_config_diff(...)` | `configs.get_config_diff` | flatten `base/ours/theirs` (každá strana nullable) |
| `resolve_conflict(...)` | `rebase_config[_delete]` | `version` z `theirs.version`, kontrola conflict setu, případně take ours/theirs (bod 4) |

Průřezově: feature pre-flight `has_feature(FEATURE_BRANCHES_MERGE_REQUESTS)` jen před
writes; dataclass návratové typy; nové `ErrorCode` členy + `docs/error-codes.md`.

ROZHODNUTO (2026-08-26) — tvar service: domácí vzor beze změn. Jedna třída
`MergeRequestService`, plná `verb_noun` jména metod (call site má obecnou proměnnou
`service`, noun jinde není), čistá logika (derivace, flatten, kompozice payloadů) jako
module-level funkce, jeden soubor dokud loc-check nedonutí štípnout (čára: lifecycle vs
conflict resolution). Zapsáno v RFC: docs/merge-requests-layer2.md → „Service shape".

## Audit pokrytí workflow (2026-08-26)

Kostra ověřena proti stavovému automatu: každý volatelný endpoint má metodu, žádná
metoda nemíří na neexistující endpoint (skip_review/finish_review/rollback_merge/publish
jsou interní — správně chybí). Nálezy:

- **Close/cancel MR — vyřešeno, žádná L2 práce.** Cancel endpoint neexistuje; UI Cancel
  = creator zavolá request-changes na vlastní MR (kbc-ui helpers.ts, „reuses
  request-changes under the hood"). Naše `request_changes(id)` to mechanicky umí.
  Pojmenování (`merge-request close` alias?) je L1 otázka → DMD-1900.
- **Resoluce MR podle branche — rozhodnuto: jako všude jinde.** L1 používá standardní
  `resolve_branch()` (`commands/_helpers.py:269`; --branch → active_branch_id).
  Nový krok branch→MR patří do L2: +1 metoda `find_merge_request_for_branch(branch_id)`
  (list + filtr `branches.branchFromId`; jednoznačné — branch má nejvýš 1 MR kdy).
  L1: `--mr-id` explicitně → jinak resolve_branch + find. Ostatní metody dál berou mr_id.
- `get_merge_request` musí propustit `include_activity_log` (L3 to umí) — doplnit param.
- **Known limitation (scope out):** „co MR mergne" ve stavu `development` neumíme —
  changeLog se plní až při request-review; UI si to počítá client-side porovnáním
  branch vs. produkce (SendForReviewButton.tsx), žádný endpoint. Po submitu pokrývá
  changeLog.

## Fronta rozhodovacích bodů

### 1. Status derivation — ROZHODNUTO (2026-08-26, plný inventář)
Finální inventář 4 derivátů (viz RFC sekce „Derived status", kompletní tabulky tam):
`derived_state` (7 hodnot, UI decision table) · `merge_blockers` seznam + `mergeable`
(conflicts/approvals/state; jen detail) · `allowed_actions` (mechanicky ze stavového
automatu, bez rolí/features — ty drží pre-flight) · `viewer` ({is_creator, has_approved}
z verify_token identity). Vzor GitHub API/MCP (mergeable_state, reviewDecision, viewer*).
Vše aditivní pole v --json, syrový `state` vždy vedle. Nejistota „bude to stačit?" krytá
polyfillem: změna = jedna čistá funkce + nové klíče, žádný breaking change.
Evidence pro backend: UI list badge vs. detail panel si odporují (Merged/Published,
Closed/Canceled, chybějící in_merge badge, panel nederivuje rejected/closed).

Původní směr (2026-08-25/26):
Rozhodnuto (2026-08-25/26):
- Derivace stavů dlouhodobě patří na **backend** (3 klienti: UI/CLI/MCP = 1 logika).
  Backend teď nemá kapacitu → CLI jede **polyfill pattern**: jedna čistá funkce
  `derive_state(mr)`, server-first (`mr.get("derivedState")`), lokální fallback
  = port decision table z UI, komentář s odkazem na Connection issue, smazat až
  backend serializuje. Precedens: defenzivní čtení required-approvals-count (DMD-1969).
- Kanonická decision table = UI (`kbc-ui .../merge-requests/components/MergeRequestRow.tsx`,
  `helpers.ts`): state + dvě derivace z `reviewers[]` —
  **Rejected** (development + non-creator reviewer rejected),
  **Closed** (development + creator self-rejection = UI „cancel" trik).
  `in_merge` v UI badge chybí (Partial record). Konflikty do badge nevstupují.
- Dvě osy: `derived_state` (UI-kompatibilní enum) × `merge_readiness`
  (mergeable / blocked_conflicts — jen v detailu, konflikty se na list nevolají).
  Readiness NENÍ guard — autorita merge zůstává backend 409.
Zbývá:
- [x] Connection issue: DMD-1988 (serialize derived status; kontrakt = tabulky z RFC,
      evidence UI nekonzistence; provázáno s DMD-1899/1969/1987)
- [x] enum hodnoty + jména polí v kbagent --json výstupu (v RFC)
- [x] sekce do docs/merge-requests-layer2.md (přepsána 2026-08-26)

### 2. Source branch pro `create` — ROZHODNUTO (2026-08-26)
Stejně jako všude jinde: standardní `resolve_branch()` idiom. MR se zakládá na vybrané
větvi — explicitní `--branch` → jinak `active_branch_id`; když není ani jedno, chyba
(„pass --branch or run branch use"). Žádný další fallback. Výstup create jasně vypíše,
ze které větve MR založil.
### 3. Error mapping — ROZHODNUTO (2026-08-26)
Dva nové ErrorCode členy, mapované **v service** (jen ta ví, že 409 přišla z merge
endpointu; konfliktní tvar nemá string code, v generickém `http_base` nerozpoznatelný):
- `MR_NOT_READY_TO_MERGE` — 409 se string codem `storage.mergeRequests.notReadyToMerge`
  (3 příčiny: merge lock / špatný stav / jiný MR běží; rozlišitelné jen textem →
  jeden kód). Přechodné stavy → `retryable=True`.
- `MR_MERGE_CONFLICT` — 409 bez string codu na merge endpointu (tělo nese konfliktní
  konfigurace). `retryable=False` + hint „run merge-request conflicts" v detailech.
Oba do `docs/error-codes.md` (`scripts/check_error_codes.py` vynucuje v CI).
Přesná jména kódů se můžou doladit při implementaci podle konvence enumu.
Backend protikus: DMD-1984 (audit string codů na všech MR endpointech; až konfliktní
409 dostane vlastní code, heuristika „409 bez codu" se v service zjednoduší).

### 4. Conflict-resolution API — ROZHODNUTO (2026-08-26)
`resolve_conflict` se čtyřmi režimy, **všechny přes rebase** (jednotný mechanismus,
žádná nová L3 metoda):
- `take=theirs` — theirs strana diffu (produkce) → rebase s `theirs.version`,
- `take=ours` — ours strana (dev) → rebase s `theirs.version`,
- `delete` — `rebase_config_delete` (tombstone `{}`),
- vlastní tělo (JSON/@file) — pass-through s kontrolou conflict setu.
Edge case: ours strana s `isDeleted` → take=ours přechází v delete resoluci.
Vědomá odchylka od UI: tlačítko „Keep production version" volá reset-to-default
(config vypadne z MR); naše take=theirs rebasem nechá config v changeLogu a merge
zapíše obsahový no-op. Které chování je správně, ověřuje DMD-1987 — podle výsledku
se případně přidá L3 `reset_config_to_default`.
Hromadné `--all` ve v1 vynecháno (triviální smyčka v L1, přidat jde kdykoli).

Prezentace 3-way diffu (rozhodnuto 2026-08-26): žádná tři okna — **klasifikace per
cesta**. L2 čistá funkce spočítá dva párové diffy (`base→ours`, `base→theirs`) a každé
dotčené cestě dá `changed_by: ours | theirs | both`; skutečný konflikt = jen `both`.
Tooling ukrást: `json_utils.compute_diff` má tu rekurzivní procházku — refactor na
strukturovanou variantu (per-path entries jako data), stávající string výstup zůstane
formatterem nad ní (užívá ho config_service). Human výstup (L1, → DMD-1900): tabulka
ve třech sekcích (Both changed / Only you / Only production), dlouhé hodnoty jen
„differs" + `--format full`. `--json`: entries + celé tři strany. Ruční merge bez
markerů: `diff --output resolved.json` (předvyplněno ours) → edit → `resolve --file`.

### 5. Post-merge cleanup — ROZHODNUTO (2026-08-26)
Zrcadlo `delete_branch` (`branch_service.py:255-307`), po úspěšném merge:
- reset `active_branch_id` **jen podmíněně** (== mergovaná source branch; `was_active`
  logika z delete_branch; NEkopírovat bezpodmínečný reset z `get_merge_url:349`),
- `branches.branchFromId` číst z MR payloadu PŘED merge (po publish je nullable),
- sync mapping přes `cleanup_branch_id_from_mapping` — helper polyká chyby čtení,
  ale `save_branch_mapping` umí vyhodit IO chybu → merge() obalí celý cleanup
  try/exceptem; chyba úklidu = warning ve výsledku, exit code zůstává úspěch,
- selhání merge = žádný cleanup (branch žije),
- workspaces na branchi neřešíme (server je maže s branchí; sirotci → workspace gc),
- výstup: „source branch is being deleted" (nikdy „is deleted"); strukturovaný
  výsledek jako delete_branch (`was_active`, `mapping_cleanup`, `message`).

### 6. Drobnosti — ROZHODNUTO (2026-08-26)
- Konstanta: rename `FEATURE_BRANCHES_MERGE_REQUESTS` → `BRANCHES_MERGE_REQUESTS_FEATURE`
  (konvence souboru: STORAGE_BRANCHES_FEATURE, GLOBAL_SEARCH_FEATURE, PAYG_FEATURE;
  dotčena 2 místa: constants.py:439 + docstring merge_requests.py:132). Rozhodl Claude
  na Martinovo pověření.
- `mcp_parity.py`: BEZPŘEDMĚTNÉ — mapa, check skript i weekly canary smazány v 0.85.0
  (#609, 2026-08-19, po sepsání notes). Žádné parity záznamy se nikam nepřidávají;
  historická mapa je docs/mcp-migration.md. Sekci v RFC označit jako zastaralou.
- SOX-fence komentář: implementační povinnost u pre-flightu (předpoklad „SOX projekt
  nikdy nemá zároveň branches-merge-requests" + přísnost vůči čistému
  protected-default-branch do chybové hlášky).

## Implementace — HOTOVO (2026-08-26)

Draft PR: https://github.com/keboola/cli/pull/703 (6 commitů po celcích:
json_utils DiffEntry → error plumbing (kódy + api_error_code v details +
rename konstanty) → derivace + admin_id na verify → reads → create/update/
transitions → merge (409 map + cleanup) → conflicts/diff/resolve).
69 unit testů v tests/test_merge_request_service.py; make check zelený až na:
- changelog-check: base předchází releases 0.87–0.91 → vyřeší rebase na main,
- test_changelog_render: 2 faily reprodukovatelné i na čistém base (env).
Bez verze bumpu — changelog entry patří bump PR ve stacku s DMD-1900.

## Proces
Bod po bodu: analýza + doporučení → Martin rozhodne → zápis sem + do RFC dokumentu
(`docs/merge-requests-layer2.md`, TEMP RFC commit, amend + force-push). Neřešit víc
bodů najednou.

## MERGED (2026-09-03)

PR #703 squash-merged do main jako `5281eef` (Martin). Remote větev smazána. Linear DMD-1899 → Done.
Cesta: 4 review kola (self, Opus wire-truth, Zajca ×3 vč. approvalu), 18 commitů,
6544 testů zelených. Non-blocking leftovers + odklady → `docs/merge-requests-layer2-followups.md`
na `ms/merge-requests-rfcs` (10f0ba7), převzít v DMD-1900.
Pozn. pro L1 rebase: vypustit 7b2bba9 (duplikát 7cd1855), sjednotit soft-failure klíč na `warnings`.
Žádný release — L2 leží na mainu dark; ven jde s L1 + release PR.
