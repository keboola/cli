# Demo for docs/TUTORIAL.md §8 (Storage column types).
#
# Simple linear walkthrough of 0.25.0 `storage create-table` upgrades:
#   1. create a bucket (plain production, no branch)
#   2. create a table with native types: VARCHAR(40), NUMERIC(18,2),
#      TIMESTAMP_TZ, VARIANT, BOOLEAN + --not-null + --default
#   3. inspect column_details -- native_type / length / nullable / default
#      all round-trip through to Snowflake
#   4. cleanup (delete-bucket --force cascades to the table)
#
# Uses the existing /tmp/kbagent-e2e sandbox via KBAGENT_CONFIG_DIR so the
# demo does not touch the user's global kbagent config.
#
# IMPORTANT: VHS does NOT support backslash line continuation -- `Type "...\\"`
# sends literal backslashes to the shell. Each command must be a single
# `Type "..."` call. Append more arguments via additional `Type` calls
# WITHOUT `Enter` between them -- the shell receives one continuous line.
#
# Regenerate:
#   vhs docs/demos/demo-storage-types.tape

Output docs/assets/demo-storage-types.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 900
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms
Set CursorBlink false
Set PlaybackSpeed 1.0

Env PATH "/Users/padak/github/keboola_agent_cli/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
Env PS1 "$ "
Env KBAGENT_CONFIG_DIR "/tmp/kbagent-e2e/.kbagent"

Sleep 700ms

Type "# Step 1: create a fresh bucket"
Enter
Sleep 400ms
Type "kbagent storage create-bucket --project padak-2-0 --stage in --name demo-types"
Sleep 300ms
Enter
Sleep 4500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 2: create a typed table with native Snowflake types"
Enter
Sleep 200ms
Type "#   VARCHAR(40) / NUMERIC(18,2) / TIMESTAMP_TZ / VARIANT + --not-null / --default"
Enter
Sleep 600ms

Type "kbagent storage create-table --project padak-2-0 --bucket-id in.c-demo-types --name sales "
Type "--column 'pk:VARCHAR(40)' --column 'amount:NUMERIC(18,2)' "
Type "--column 'ts:TIMESTAMP_TZ' --column 'meta:VARIANT' --column 'is_paid:BOOLEAN' "
Type "--primary-key pk --not-null pk --not-null amount "
Type "--default amount=0 --default is_paid=false"
Sleep 400ms
Enter
Sleep 6500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 3: inspect what Snowflake actually stored"
Enter
Sleep 200ms
Type "#   native_type + length + nullable + default all survive the round-trip"
Enter
Sleep 500ms

Type "kbagent --json storage table-detail --project padak-2-0 --table-id in.c-demo-types.sales | jq '.data.column_details'"
Sleep 300ms
Enter
Sleep 4500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 4: cleanup (--force cascades to the sales table)"
Enter
Sleep 400ms
Type "kbagent storage delete-bucket --project padak-2-0 --bucket-id in.c-demo-types --force --yes"
Sleep 300ms
Enter
Sleep 2800ms
