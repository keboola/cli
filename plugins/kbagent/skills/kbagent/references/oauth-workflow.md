# OAuth authorization workflow (`config oauth-url`)

Getting an OAuth-based component (`keboola.ex-facebook-ads-v2`, `keboola.ex-google-analytics-v4`, ...) authorized for a configuration.

## Steps

1. `kbagent config oauth-url --project NAME --component-id ID --config-id ID`
   Needs a **master** token (`canManageTokens`); a non-master token fails fast with `MISSING_MASTER_TOKEN` (exit 3). The minted token lives 1 hour, so generate the link when the user is ready to click through, not ahead of time.
   Since vNEXT the command opens the URL in the user's default browser itself (interactive human mode only -- never under `--json`, never when stdout is not a terminal, suppressed by `--no-open`).
2. The user completes the provider's consent screen in that browser.
3. Verify: `kbagent config detail --project NAME --component-id ID --config-id ID` -- the `oauth_api.version` in the configuration is bumped (`3` -> `4`, ...) once credentials are stored. Do not claim success without this check; the browser tab tells you nothing.

## Reporting the link in an answer

Always give the user the URL as well as opening it -- they may be on a different machine, or the browser handoff may fail silently (an OS handler accepting a URL is not proof a window appeared).

Put it in a **fenced code block**, on its own, never as inline prose:

````
Opened the Facebook Ads authorization link in your browser. If it did not open:

```
https://external.keboola.com/oauth/index.html?token=...&sapiUrl=...#/keboola.ex-facebook-ads-v2/01m0zkt36y0mwcvya1apnrvn3k
```
````

The URL is ~200 chars and fits no terminal row. Inline text gets wrapped by the renderer, which then link-detects **only the first visual row** -- the click drops the trailing `#/<component>/<config>` and the wizard answers `Failed to load config data. Please contact us on support@keboola.com` even though the token and stack URL are fine. A fenced block is not reflowed, so a copy is intact; clickability does not matter because the command already opened it. See [gotchas](gotchas.md).

Never truncate, shorten or "prettify" the URL, and never split it across lines yourself.
