# OAuth authorization workflow (`config oauth-url`)

Getting an OAuth-based component (`keboola.ex-facebook-ads-v2`, `keboola.ex-google-analytics-v4`, ...) authorized for a configuration.

## Steps

1. `kbagent config oauth-url --project NAME --component-id ID --config-id ID`
   Needs a **master** token (`canManageTokens`); a non-master token fails fast with `MISSING_MASTER_TOKEN` (exit 3). The created token lives 1 hour, so generate the link when the user is ready to click through, not ahead of time.
   Since vNEXT the command opens the URL in the user's default browser itself (interactive human mode only -- never under `--json`, never when stdout is not a terminal, never over SSH, in a container or in WSL without a working `wslview`, suppressed by `--no-open`). Under `--json` the result has `browser_opened: false`.
   Before you give the link to the user, run `kbagent config detail --project NAME --component-id ID --config-id ID` and note the configuration `version`. Step 3 compares against it.
2. The user completes the provider's consent screen in the browser.
3. Verify: run the same `config detail` again. The authorization is stored when the configuration `version` is higher than the one you noted and `configuration.authorization.oauth_api.id` is set. Do not use `oauth_api.version` as the signal: it is the OAuth Broker API version, the wizard always writes `3`, so it does not change when the user authorizes. Do not claim success without this check; the browser tab tells you nothing.

## Reporting the link in an answer

Always give the user the URL. Tell the user that the link is open in the browser only when the result has `browser_opened: true`. Under `--json` it is always `false`: nothing was opened, so ask the user to open the link. Give the URL also when it is `true` -- the user may be on a different machine, or the browser handoff may fail silently (an OS handler accepting a URL is not proof a window appeared).

Put it in a **fenced code block**, on its own, never as inline prose:

````
Open this Facebook Ads authorization link in your browser:

```
https://external.keboola.com/oauth/index.html?token=...&sapiUrl=...#/keboola.ex-facebook-ads-v2/01m0zkt36y0mwcvya1apnrvn3k
```
````

The URL is ~200 chars and fits no terminal row. Inline text gets wrapped by the renderer, which then link-detects **only the first visual row** -- the click drops the trailing `#/<component>/<config>` and the wizard answers `Failed to load config data. Please contact us on support@keboola.com` even though the token and stack URL are fine. A fenced block is not reflowed, so a copy of it is intact. See [gotchas](gotchas.md).

Never truncate, shorten or "prettify" the URL, and never split it across lines yourself.
