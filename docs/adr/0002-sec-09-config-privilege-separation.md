# ADR 0002: sec-09 — Config Privilege Separation, Not an In-Process Lockout

## Status

Accepted

## Date

2026-05-25

## Context

Issue [#271](https://github.com/keboola/cli/issues/271), finding **sec-09**, questioned
whether the `permissions set` / `permissions reset` confirmation guard
(`commands/permissions.py:39-41`) constitutes a real security lockout.

It does not. The guard is **friction, not a lockout**, and it cannot be a lockout in
single-user mode, because the policy lives in a file the agent owns.

Verified against `main`:

- The guard is a `sys.stdin.isatty()` check only. Non-TTY → refuse. A PTY wrapper
  (`pty.openpty()`, `script`, `expect`) makes `isatty()` return `True`; the agent reads the
  random confirmation code from stderr and writes it to stdin → bypassed.
- The policy persists into `AppConfig.permissions` (`models.py:90`) →
  `ConfigStore.save()` → `config.json`, mode `0600`, **owned by the invoking user**.

The PTY trick is only the *harder* bypass. An agent with shell access in the **same user
context** skips the command entirely and rewrites the file:

```bash
python -c "import json,os; p=os.path.expanduser('~/.config/keboola-agent-cli/config.json'); \
c=json.load(open(p)); c['permissions']=None; json.dump(c,open(p,'w'))"
```

No TTY, no code. **No in-process guard** can fix this while the policy lives in an
agent-writable file — the guard is structurally in the wrong layer.

> A guard can only constrain a principal that lives in a **different trust domain**.

In single-user mode the agent and the policy file share one uid → one trust domain → no
guard is enforceable.

An earlier revision of this analysis proposed a privsep deployment with a bash wrapper that
blocklisted only the `permissions` subcommand. An independent security review (OpenAI Codex,
`gpt-5.3-codex`) rated that **Broken**: the agent could escape the lockout *without* ever
touching `permissions`, via (a) redirecting config discovery and (b) other config-writing
subcommands. That finding shapes the decision below.

## Decision

1. **Frame `permissions` honestly as guard rails, not a sandbox.** The in-process guard
   protects against an agent *mistake*, not an adversarial agent in the same trust domain.
   Rewrite the misleading docstring at `permissions.py:34` accordingly. **Do not** invest in
   a stronger in-process `isatty`-style guard — it is effort in the wrong layer.

2. **A real hard lockout is an OS-level privilege-separation deployment pattern, never an
   in-process feature.** kbagent cannot change its own runtime uid. The lockout rests on
   running the agent and kbagent under a **different OS user** than the one that owns
   `config.json`, plus three things the broken v1 missed:
   - **Pin the config path** — kbagent discovers config from four agent-influenceable
     sources; all must be neutralized.
   - **Allowlist, don't blocklist** — `init` and `project add/edit/remove` also write
     `config.json`; blocking only `permissions` leaves the policy writable by other names.
   - **Scrub the environment** (`env -i`); do not trust `sudo env_reset` alone.

3. **Push the robust enforcement into kbagent "locked mode" (follow-up feature), not bash.**
   Bash argv allowlists are defeatable (Codex flagged the scan). Triggered by an env var the
   wrapper sets via `env -i` (so the agent cannot pre-set or unset it), kbagent must:
   - **Pin the config dir** to `KBAGENT_LOCKED_CONFIG_DIR` and **ignore** `--config-dir`,
     `KBAGENT_CONFIG_DIR`, cwd `.kbagent` discovery, and `$HOME`-based fallback (kills all
     four redirect vectors in tested Python, not bash);
   - **Default-deny every config-writing / admin path** — `permissions set/reset`, `init`,
     `project add/edit/remove` — regardless of TTY, with a clear "locked mode" error;
   - Optionally refuse to print full tokens even where it otherwise might.

The trust model is two OS users, two trust domains:

| Principal | OS user | Owns | Can do |
|---|---|---|---|
| AI agent | `ai` | the agent's own workspace | run kbagent **operations** via a sudo-whitelisted wrapper |
| kbagent service identity | `kbagent-svc` | `config.json` (tokens **and** policy), `0600` | everything; only a human logs in as this user to change policy |

The agent (`ai`) never owns or can write `config.json`. It invokes kbagent through
`sudo -u kbagent-svc`, which runs the process **as** `kbagent-svc`. (A setuid Python
interpreter is not an option — the kernel ignores setuid on scripts, and a setuid interpreter
is a classic privilege-escalation surface. `sudo -u` is the battle-tested privilege broker.)

## Consequences

### What ships now (the sec-09 decision on #271)

- Rewrite the misleading docstring at `permissions.py:34`.
- Ship this ADR as the authoritative "Hardened multi-user setup" reference.

### Follow-up feature

- Implement **locked mode** (`KBAGENT_NO_ADMIN` + `KBAGENT_LOCKED_CONFIG_DIR`): config-path
  pin + ignore all redirect inputs + default-deny config writers. This is what makes the
  pattern robust; without it the bash wrapper is only a coarse outer gate.

### The redirect trap (why a file-only privsep is insufficient)

Privsep protects the *file*. It does **not**, by itself, guarantee kbagent *reads that file*.
kbagent's config-dir resolution (`config_store.py:63-96`) takes the **first match** of:
`--config-dir` flag → `KBAGENT_CONFIG_DIR` env → cwd walk-up for `.kbagent/config.json` →
`platformdirs` global default (expands `$HOME`). Every one is agent-influenceable, so the
agent can point kbagent at a config it controls.

**Verified distinction:** redirection is a **policy escape**, not a **token read**. Pointing
kbagent at an agent-owned config does not leak the protected tokens — the `0600`
service-owned file stays unreadable to `ai` (kernel `EACCES`), and the agent's own config has
no privileged tokens. Token confidentiality holds; what breaks is the assumption
"kbagent-run always operates under the protected policy." Hence the config-path pin in
locked mode is load-bearing.

### Reference: hardened setup

```bash
# 1. Dedicated service user, no login shell.
sudo useradd --system --create-home --home-dir /var/lib/kbagent-svc \
     --shell /usr/sbin/nologin kbagent-svc

# 2. kbagent + wrapper owned by root, NOT writable by 'ai' or 'kbagent-svc'.
sudo install -o root -g root -m 0755 /path/to/kbagent /usr/local/bin/kbagent
sudo install -o root -g root -m 0755 ./kbagent-run    /usr/local/bin/kbagent-run

# 3. Config (tokens + policy) created and owned by kbagent-svc, 0600, at a FIXED path.
sudo -iu kbagent-svc kbagent project add --project prod --url "$URL"   # prompts for token
sudo -iu kbagent-svc kbagent permissions set --deny-destructive        # human sets policy

# 4. sudoers: 'ai' may run ONLY the wrapper as kbagent-svc, passwordless, with NO env
#    pass-through (NOSETENV blocks `sudo VAR=val` / the wrapper can't be -E'd), and a
#    forced secure_path so a poisoned PATH cannot reach the wrapper.
cat | sudo tee /etc/sudoers.d/kbagent-agent >/dev/null <<'EOF'
Defaults!/usr/local/bin/kbagent-run secure_path="/usr/sbin:/usr/bin:/sbin:/bin"
ai ALL=(kbagent-svc) NOPASSWD: NOSETENV: /usr/local/bin/kbagent-run
EOF
sudo chmod 0440 /etc/sudoers.d/kbagent-agent
```

The wrapper scrubs the environment, pins the config, runs from a neutral CWD, and allowlists
read/operational subcommands. Treat it as the coarse outer gate; locked mode is the
authoritative enforcement.

```bash
#!/usr/bin/env bash
# /usr/local/bin/kbagent-run — operational entry point for 'ai'. root-owned, 0755.
set -euo pipefail
SVC_HOME=/var/lib/kbagent-svc
SVC_CONFIG_DIR="$SVC_HOME/.config/keboola-agent-cli"   # owner kbagent-svc, 0700
KBAGENT=/usr/local/bin/kbagent

# Allowlist of subcommands the agent may run. NOT listed (denied): init, permissions.
ALLOWED='project|config|search|job|storage|lineage|sharing|tool|branch|workspace|flow|schedule|component|data-app|semantic-layer|kai|context|doctor|version|changelog'

# Rebuild argv WITHOUT any agent-supplied --config-dir, and find the subcommand.
args=(); sub=""; skip=0
for tok in "$@"; do
  if [[ $skip == 1 ]]; then skip=0; continue; fi
  case "$tok" in
    --config-dir)   skip=1; continue ;;       # drop flag + its value
    --config-dir=*) continue ;;               # drop inline form
    -*) args+=("$tok") ;;
    *) [[ -z "$sub" ]] && sub="$tok"; args+=("$tok") ;;
  esac
done
[[ "$sub" =~ ^($ALLOWED)$ ]] || { echo "kbagent-run: '${sub:-<none>}' not allowed." >&2; exit 1; }
# Deny mutating project leaves explicitly.
if [[ "$sub" == project ]]; then
  for t in "${args[@]}"; do case "$t" in add|edit|remove|refresh)
    echo "kbagent-run: 'project $t' not allowed." >&2; exit 1 ;; esac; done
fi

cd /                                          # kill cwd-based .kbagent discovery
exec env -i \                                 # scrub ALL inherited env
  PATH=/usr/bin:/bin \
  HOME="$SVC_HOME" \
  KBAGENT_LOCKED_CONFIG_DIR="$SVC_CONFIG_DIR" \
  KBAGENT_NO_ADMIN=1 \
  "$KBAGENT" --config-dir "$SVC_CONFIG_DIR" "${args[@]}"
```

Usage:

```bash
# Agent: operations work, passwordless.
sudo -u kbagent-svc kbagent-run project list
sudo -u kbagent-svc kbagent-run tool call get_buckets --project prod

# Agent: every escalation path is blocked.
sudo -u kbagent-svc kbagent-run permissions reset        # wrapper: not allowlisted
sudo -u kbagent-svc kbagent-run init                     # wrapper: not allowlisted
sudo -u kbagent-svc kbagent-run --config-dir /home/ai/x project list
#   → wrapper drops --config-dir; locked mode ignores it anyway
sudo -u kbagent-svc kbagent permissions reset            # sudo: only kbagent-run whitelisted
cd /home/ai/x && sudo -u kbagent-svc kbagent-run config list
#   → wrapper cd / + locked-mode pin: planted ./.kbagent ignored
python -c "...rewrite config.json..."                    # EACCES: 0600, owned by kbagent-svc

# Human: changes policy by logging in AS the service user (interactive, audited).
sudo -iu kbagent-svc
  kbagent permissions reset
```

### Attack-vector analysis

| Vector | Single-user (today) | Privsep v2 (this decision) |
|---|---|---|
| Direct rewrite of `config.json` | open | **blocked** — `EACCES`, different owner |
| `permissions reset` non-TTY / PTY | open (sec-09) | **blocked** — raw kbagent not sudo-whitelisted |
| `init` / `project add/edit` rewrites policy | open | **blocked** — wrapper allowlist + locked-mode deny |
| `--config-dir` redirect | open | **blocked** — wrapper drops it + locked-mode ignores |
| `KBAGENT_CONFIG_DIR` redirect | open | **blocked** — `env -i` + locked-mode ignores |
| cwd `.kbagent/config.json` redirect | open | **blocked** — wrapper `cd /` + locked-mode pin |
| `$HOME`-based global-fallback redirect | open | **blocked** — `env -i` sets HOME + locked-mode pin |
| `sudo VAR=val` / `sudo -E` env injection | open | **blocked** — sudoers `NOSETENV` |
| Read protected tokens out of `config.json` | open | **blocked** — `0600`, owned by `kbagent-svc` |

Enforcement is done by the kernel (file perms), `sudo` (the privilege broker), and kbagent
locked mode (tested Python) — not by an application-level `isatty` check, which becomes a
redundant backstop for the human path. **Bonus:** tokens now live under a uid the agent can't
read, closing the "agent reads the token straight out of the config file" exfiltration path.

### Honest limits (what this does NOT solve)

- **Writable binary/wrapper/sudoers.** If `ai` can modify `/usr/local/bin/kbagent`,
  `kbagent-run`, or the sudoers drop-in, the scheme collapses. They MUST be root-owned and
  not writable by `ai` — same class of trust as trusting `sudo` itself.
- **`sudoers` drift.** If an operator later adds `env_keep` or relaxes the command path, the
  redirect vectors reopen. `env -i` is the backstop; locked mode is the real one. Audit sudoers.
- **A second kbagent the agent installs.** `ai` can `pip install` its own kbagent — but it
  reads `ai`'s own token-less config and cannot perform privileged operations. Harmless.
- **Compromised `sudo` / kernel / social-engineering the human operator.** Out of scope.
- **Desktop single-user.** This targets headless / server / CI agent deployments. On a
  developer's own laptop (the common case) the friction model is the right trade-off; document
  this, do not mandate it.
