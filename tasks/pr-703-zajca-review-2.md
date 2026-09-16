# Zajcovo druhé review PR #703 (2026-09-02) — triage a zapracování

Verdikt reviewera: NEEDS-WORK (1 blocking). Moje validace: **blocking + 3 non-blocking
+ 5 nitů OPRÁVNĚNÝCH a opravených; 1 non-blocking ODMÍTNUT s odůvodněním; 1 nit
odložen jako follow-up.** Commit `01fb0fa`.

## BLOCKING #1 — isDisabled/description bez guardu → OPRÁVNĚNÝ, opraveno
Přesný zásah: guard kryl 3 z 5 replace klíčů a `is_disabled=bool(body.get(..., False))`
tiše re-enabloval vypnutý config (L3 docstring před tím explicitně varuje). Fix:
všech 5 klíčů povinných — name/rows/configuration/isDisabled odmítají absent-or-null,
`description` odmítá jen ABSENCI (explicitní null je rozhodnutí, pole je nullable —
jemnější než reviewerův návrh, který by nullové isDisabled pustil do bool()=False).
`is_disabled` teď forwarduje verbatim. Testy: missing isDisabled / missing description /
explicit-null description legal / take strana forwarduje isDisabled=True.

## #2 — allowed_actions ukazuje na write, který selže → ODMÍTNUTO (s dokumentací)
Popsaný scénář je v aktuálním kódu NEMOŽNÝ: `feature_enabled: false` se emituje jen
při prázdném listu — a prázdný list žádné řádky s allowed_actions nenese. Reálná
zbytková varianta je projekt, kterému feature ODEBRALI po vzniku MRs (neprázdný list,
feature off) — tam by fix stál verify_token GET na KAŽDÝ neprázdný list kvůli vzácné
konfiguraci, a pre-flight každý pokus o write stejně zodpoví přesnou chybou
(FEATURE_NOT_ENABLED se SOX rozlišením). „Features cache already warm" v nálezu
neplatí (na neprázdné cestě se verify_token nevolá). Správný domov fixu je server-side
`allowedActions` (DMD-1988), který features zohlední. Rozhodnutí zapsáno do docstringu
`_enrich_row` (state-only = feature-blind, deliberate).

## #3 — api_error_params bez changelog položky → OPRÁVNĚNÝ, částečně opraveno
- Unmasked pass-through je teď VYSLOVENÉ rozhodnutí v komentáři u raise-site
  (server-authored error context, stejná důvěra jako message text).
- PR body: bump-PR poznámka rozšířena — changelog entry musí pokrýt
  details.api_error_params(+_truncated) jako cross-cutting změnu výstupního kontraktu.
- Samotný changelog nejde napsat teď (feature PR nesmí, konvence).

## #4 — null strana × neprázdná base = rozporné signály → OPRÁVNĚNÝ, opraveno
`_classify_three_way` při null ours/theirs vrací [] — side-level fakt nesou
ours_deleted/theirs_deleted (None = strana neexistuje), per-path řádky proti prázdné
náhražce se nefabrikují. Docstring + test aktualizovány.

## #5 — change_description u take=delete tiše zahozen → OPRÁVNĚNÝ, opraveno warningem
Plumbing přes L3 NENÍ možný: changeDescription žije UVNITŘ diff envelope a delete
tombstone je přesně {"version": N, "diff": {}} — wire ho nemá kudy nést. Výsledek
nese warnings[] („change_description ignored: ... server records its default
message"). L1 navíc plánuje kombinaci odmítat na exit 2 (findings doc).

## Nity
- History-narrating komentáře (Opus/Zajca/datumy, „Renamed from", „previously
  copy-pasted") → vyčištěno; věcná fakta (verified against StateMachine) zůstala.
- search_service raw "FEATURE_NOT_ENABLED" → ErrorCode member. ✔
- merge(): dva nezávislé try bloky pro cleanupy (+ test: failed config write
  nepřeskočí mapping unlink). ✔
- int(raw_branch_from) ValueError → `_coerce_branch_id` helper (non-numeric →
  None → cleanup skip; v `_branch_from_id_of` → čitelná VALIDATION_ERROR). + test ✔
- find_default_branch_id: isDefault entry bez id → přeskočí (None), žádný KeyError. ✔
- cleanup_branch_id_from_mapping bez project scope → NEOPRAVENO zde: zděděno
  z BranchService.delete_branch; oprava patří oběma call sites najednou
  (follow-up kandidát), divergence v tomto PR by byla horší než status quo.

## Stav
107 testů v test_merge_request_service.py, ruff/ty čisté, pushnuto `01fb0fa`,
odpověď postnutá na PR.
