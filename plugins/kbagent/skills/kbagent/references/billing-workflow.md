# Billing (PAYG Credits) workflow

> Audience: a developer or a kbagent agent asked about Keboola PAYG (pay-as-
> you-go) usage, balance, or invoices. Goal: know exactly what `kbagent
> billing` can and cannot answer *before* burning a loop hunting for a
> command that does not exist. (since v0.84.2; issue
> https://github.com/keboola/cli/issues/594)

## The shape of the gap (read this first)

`kbagent billing` is **one command**: `billing credits`. It reports the
*current PAYG balance*, nothing more. There is **no** `billing history`,
`billing invoices`, or anything that returns `idStripeInvoice` — and there is
no way to build one from the CLI today. If a user asks to "reconcile our
Stripe invoices against Keboola projects" or "when did we last top up," the
correct answer is **that data is not reachable from kbagent** (see
[What is NOT reachable](#what-is-not-reachable-and-why) below) — not a
workaround, not a guess, and not a hand-rolled HTTP call.

## What IS reachable today

```bash
kbagent --json billing credits --project prod --project staging
```

```json
{
  "credits": [
    {
      "project_alias": "prod",
      "project_id": 9621,
      "consumed": 12.5,
      "remaining": 25.5,
      "total": 38.0,
      "consumed_minutes": 750.0,
      "remaining_minutes": 1530.0,
      "component_jobs_consumed": 11.75,
      "workspace_jobs": [
        {"workspace_type": "sandbox-sql", "warehouse_size": "small", "consumed": 0.5},
        {"workspace_type": "writer", "warehouse_size": "small", "consumed": 0.25}
      ]
    },
    {
      "project_alias": "staging",
      "project_id": null,
      "consumed": 0.0,
      "remaining": 0.0,
      "total": 0.0,
      "consumed_minutes": 0.0,
      "remaining_minutes": 0.0,
      "component_jobs_consumed": 0.0,
      "workspace_jobs": []
    }
  ],
  "errors": []
}
```

- `--project ALIAS` is repeatable; omit it to fan out across every registered
  project in parallel.
- `consumed` / `remaining` come straight off `GET /credits` on the
  `billing.{stack}` host, which -- unlike the invoice endpoints below --
  **does** accept the CLI's normal per-project `X-StorageApi-Token`. No
  manage token, no extra login step.
- `total` is a client-side convenience: `consumed + remaining`. It is NOT
  named `purchased`, and must not be reported as an amount purchased. It
  equals the purchased amount only if every credit ever added is either
  still available or already consumed, so it silently mis-reports against
  expired, revoked, or promo credits -- and credit purchases are precisely
  what this command cannot see (see the gap section below). If a user asks
  "how much have we bought", the honest answer is that the CLI cannot tell
  them, not this number.
- Per-project failures land in `errors`, never abort the run -- always check
  both arrays, not just `credits`.

## The units trap

The API speaks **credits**. The Keboola UI speaks **minutes**. The
conversion is fixed: **1 credit = 60 minutes**. Every row already carries
both — never hand-convert, and never convert in the other direction (minutes
-> credits) on a value that already came from the CLI.

```
consumed_minutes  = consumed  * 60
remaining_minutes = remaining * 60
```

Money is a separate axis again, and this is where it is easy to mislead a
user. The credits-to-minutes factor above is invariant; **the price per credit
is not**. It is contract- and stack-specific, the API never returns it, and the
CLI has no price field. Issue #594 records **$8.40 ex. VAT per credit** on one
contract — that is a single historical observation, NOT a platform constant.
Never quote it to a user as their rate, and never derive a dollar figure from
it. It appears here only to make the UI's arithmetic legible: on that contract,
8 credits rendered as "480 minutes ($67.20)". If a user needs the money number,
it comes from their contract or invoice, not from `billing credits`.

## The PAYG gate

Not every project is PAYG. Before `billing credits` ever calls the billing
host, it checks the `pay-as-you-go` flag in the project token's
`owner.features` (`GET /v2/storage/tokens/verify`). A project without that
flag gets a per-project entry:

```json
{"project_alias": "legacy-project", "error_code": "PAYG_NOT_AVAILABLE",
 "message": "Project 'legacy-project' does not have the pay-as-you-go feature enabled; PAYG balance is only available on PAYG projects."}
```

**This is a feature-flag verdict, not a network failure.** On a non-PAYG
stack (e.g. plain `eu-central-1`) the service index still *advertises* a
`billing.eu-central-1.keboola.com` host, but it does not resolve (NXDOMAIN)
— the feature check exists specifically so a non-PAYG project never dials
that host at all. Do not treat `PAYG_NOT_AVAILABLE` as something to retry or
debug as connectivity; it means "this project has no PAYG balance to show."

## What is NOT reachable, and why

Purchase history and `idStripeInvoice` live on `connection.{stack}` under
`/pay-as-you-go/billing/*` — a **completely different host and API surface**
from `billing.{stack}/credits`. That endpoint does not accept a Storage API
token: a request with `X-StorageApi-Token` gets the byte-identical
302-to-login response as an unauthenticated request, and presenting the
token as a bearer credential gets a plain 401. There is no
project-token-based path to it, so there is nothing for `kbagent billing` to
wrap.

This is the **still-open primary ask of issue #594**:
https://github.com/keboola/cli/issues/594 — link it verbatim when a user
asks about invoice access so they can track the maintainer's answer.

**Do not improvise a substitute.** In particular:

- Do not attempt to reach `/pay-as-you-go/billing/*` with `kbagent http`, a
  raw `httpx`/`curl` call, or by asking the user to paste a manage/session
  token for it — none of those change what the endpoint accepts.
- Do not fall back to matching invoices to projects by `(date, amount)`
  heuristics. It silently breaks the moment two projects top up the same
  credit amount on the same day, and produces a wrong-but-confident answer
  instead of an honest "not available."
- If asked to reconcile invoices, tell the user directly: kbagent can report
  the current balance (`billing credits`) but not purchase history; that
  reconciliation needs the Stripe/billing portal directly until #594 lands.

## The money guardrail

`POST /credits` on the billing service triggers a **real automatic top-up**
— actual money is charged. It is deliberately not wrapped by any `kbagent`
command, CLI or REST. Never reach for it, and never construct a raw HTTP
call to it (via `kbagent http`, a manual `httpx` request, or otherwise) even
if a user asks "just top up my credits" — that action requires going through
Keboola's own billing UI, not an agent-driven CLI.

## Permission class

`billing.credits` = read. Safe to run under `--deny-writes` /
`--deny-destructive`; it makes no mutating calls.
