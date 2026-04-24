# Demo for docs/TUTORIAL.md (GitOps: sync pull + branch-link).
# Live workflow against the "Padak 2.0" demo project. Shows:
#   1. project add -> register an isolated workspace
#   2. git init + sync init --git-branching (bind git to Keboola sync)
#   3. sync pull -> pull production snapshot into main/
#   4. git checkout -b feature/... + sync branch-link
#      (creates a real Keboola dev branch under the hood)
#   5. sync branch-status -> verifies the git <-> Keboola link
#
# The Storage API token shown in this tape is for an isolated demo project
# and is rotated after every recording. Do NOT copy it.
#
# The tape keeps the sandbox isolated via KBAGENT_CONFIG_DIR so it does
# not touch the user's global kbagent config (~/Library/Application Support/..).
#
# Regenerate:
#   rm -rf /tmp/kbagent-sync-live && mkdir /tmp/kbagent-sync-live
#   vhs docs/demos/demo-sync-pull.tape
#
# After recording: delete the dev branch created in step 4
#   kbagent --config-dir /tmp/kbagent-sync-live/.kbagent \
#     branch delete --project padak-2-0 --branch <id-from-gif>

Output docs/assets/demo-sync-pull.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 900
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set CursorBlink false
Set PlaybackSpeed 1.0

Env PATH "/Users/padak/github/keboola_agent_cli/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
Env PS1 "$ "
Env KBAGENT_CONFIG_DIR "/tmp/kbagent-sync-live/.kbagent"

Sleep 700ms

Type "cd /tmp/kbagent-sync-live"
Sleep 300ms
Enter
Sleep 400ms

Type "# Step 1: register the project in an isolated local workspace"
Enter
Sleep 300ms
Type "export KBC_TOKEN=10539-10732660-rmA8oxNQ2GwqdctNcxVOR0TyDjGl6DgCsE7WQBoB"
Sleep 200ms
Enter
Sleep 500ms
Type "kbagent project add --project padak-2-0 --url https://connection.keboola.com"
Sleep 300ms
Enter
Sleep 2500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 2: bind git to the Keboola sync workspace (git-branching mode)"
Enter
Sleep 300ms
Type "git init -q -b main"
Sleep 200ms
Enter
Sleep 500ms

Type "kbagent sync init --project padak-2-0 --git-branching"
Sleep 300ms
Enter
Sleep 2500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 3: pull the production snapshot into main/ (~50 configs)"
Enter
Sleep 300ms
Type "kbagent sync pull --project padak-2-0 --no-storage --no-jobs 2>&1 | tail -15"
Sleep 300ms
Enter
Sleep 3500ms

Type "# The configs are now tracked as local files:"
Enter
Sleep 200ms
Type "ls main/"
Sleep 300ms
Enter
Sleep 2000ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 4: create a feature git branch and link it to a Keboola dev branch"
Enter
Sleep 300ms
Type "git add -A && git commit -qm 'initial sync'"
Sleep 200ms
Enter
Sleep 1500ms

Type "git checkout -b feature/update-etl 2>&1 | tail -1"
Sleep 300ms
Enter
Sleep 1200ms

Type "kbagent sync branch-link --project padak-2-0"
Sleep 300ms
Enter
Sleep 6500ms

Type "# Verify the link:"
Enter
Sleep 200ms
Type "kbagent sync branch-status"
Sleep 300ms
Enter
Sleep 3000ms

Type "# From now on 'sync pull/push' target only the dev branch."
Enter
Sleep 200ms
Type "# Edits go through git: commit, push, PR, merge back via 'kbagent branch merge'."
Enter
Sleep 2500ms
