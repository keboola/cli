# OAuth broker maintenance (`kbagent oauth`)

*(since vNEXT)*

The OAuth broker (`oauth.<stack>`) stores one **consumer** per OAuth component: app key, encrypted
app secret, auth and token URLs. `kbagent oauth` lets L1 support read consumers and rotate the
Microsoft Entra client secret behind one, without hand-made curl commands and without the Manage
token or the new secret passing through the agent.

Not the same thing as [oauth-workflow.md](oauth-workflow.md): that one authorizes a *configuration*
(an end user clicks through the consent screen, `config oauth-url`).

## Safety model -- what you, the agent, can and cannot do

- **The token is an application token whose only scope is `oauth:manage`.** kbagent refuses any other
  token (`OAUTH_TOKEN_TOO_BROAD`, exit 6) on `identity add` and again on every use. There is no
  override. Do not suggest a super-admin personal token or a token with more scopes.
- **The token lives in the macOS keychain only.** A human stores it once with
  `kbagent oauth identity add --stack HOST` (hidden prompt; refuses without a terminal; no option,
  pipe or env var). Every later command makes macOS ask for the login password before the token is
  released. You cannot answer that dialog; tell the human it is coming.
- **"Always Allow" breaks the gate.** If someone clicks it, kbagent refuses the item
  (`OAUTH_KEYCHAIN_UNPROTECTED`, exit 6). The fix is `identity add` again; tell the human never to
  click "Always Allow".
- **Writes need a random code typed at a terminal.** No `--yes`, no env bypass; from an agent shell
  `rotate-azure-secret` exits 6 before it reads anything. Run `--dry-run` yourself, then hand the
  exact command to the human to run in their own terminal.
- **Secrets are never printed.** `appSecret` / `appSecretDocker` show as `<encrypted>` (the broker
  only returns ciphertext anyway). The new Entra secret goes `az` -> 1Password -> broker inside one
  process.
- **macOS only** for now. Terminal-only: no `kbagent serve` route.

## Steps: rotate an expiring Entra secret

1. Find the consumer and its app: `kbagent --json oauth consumer get --stack HOST --component ID`.
   `appKey` is the Entra application (client) ID.
2. One Entra app can back several consumers (for example two components on one stack, each with its
   own secret whose `displayName` is the component ID). Rotate per component.
3. The human signs in: `az login` with a role that may change the app's secrets active (PIM), and
   `op signin` (1Password desktop integration).
4. Preview (agent-safe, read-only; still raises the keychain dialog):
   `kbagent --json oauth rotate-azure-secret --stack HOST --component ID --app-id APP --op-vault VAULT --dry-run`.
   It checks: token scope, keychain protection, the consumer's `appKey` equals `--app-id`, `az` can
   read the app in the signed-in tenant, `op` is signed in and the vault exists. Any failure is
   `OAUTH_PREFLIGHT_FAILED` (exit 2) and nothing was changed.
5. The human runs the same command without `--dry-run` in their terminal, reads the plan (three
   writes), and types the code.
6. Read the result:
   - `status: rotated` -- the broker ciphertext changed, `appKey` is unchanged, and Entra issued a
     token with the new secret.
   - `status: rotated_unverified` (exit 1) -- the writes ran but a check failed; see
     `entra_secret_check.detail` and `broker_secret_changed`. Do not remove the old secret.
   - `OAUTH_ROTATION_INCOMPLETE` (exit 1) -- `error.details.stage` says where it stopped
     (`entra`, `1password`, `broker`) and the message says what to clean up. The old secret stays
     valid in every case.
7. The old secret is **not** removed. `old_secrets_still_valid` lists its key IDs. Remove it after a
   soak period (a later kbagent release adds a command; until then
   `az ad app credential delete --id APP --key-id KEY`, by a human).

## Common errors

| Error | Meaning | Fix |
|---|---|---|
| `OAUTH_IDENTITY_MISSING` (5) | No keychain item for the stack, or the dialog was denied | `kbagent oauth identity add --stack HOST` (human) |
| `OAUTH_TOKEN_TOO_BROAD` (6) | Not an `oauth:manage`-only application token, or expired/disabled | Ask a super-admin for a new application token with only `oauth:manage` |
| `OAUTH_KEYCHAIN_UNPROTECTED` (6) | The keychain item trusts an app ("Always Allow") | `identity add` again |
| `OAUTH_PREFLIGHT_FAILED` (2) | Wrong app for the component, `az`/`op` not signed in, role not active | Fix and re-run; nothing changed |
| `OAUTH_ROTATION_INCOMPLETE` (1) | A write failed after the new secret was created | Follow the message; `details.stage` |
