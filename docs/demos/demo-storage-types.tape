# Demo for docs/TUTORIAL.md §8 (Storage column types).
#
# Live walkthrough of the 0.25.0 `storage create-table` upgrades:
#   1. create a fresh short-lived dev branch
#   2. create-table with native types (VARCHAR(40), NUMBER(18,2), TIMESTAMP_TZ,
#      VARIANT, BOOLEAN) + --not-null + --default; bucket auto-materializes
#      in the branch on the first write
#   3. table-detail (raw API column_details) showing type + length + nullable
#   4. branch cleanup (cascades bucket + table)
#
# Uses the existing /tmp/kbagent-e2e sandbox (project "padak-2-0" already
# registered) via KBAGENT_CONFIG_DIR so the demo does NOT touch the user's
# global kbagent config.
#
# Regenerate:
#   vhs docs/demos/demo-storage-types.tape

Output docs/assets/demo-storage-types.gif

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
Env KBAGENT_CONFIG_DIR "/tmp/kbagent-e2e/.kbagent"

Sleep 700ms

Type "# Step 1: create a short-lived dev branch to isolate the demo"
Enter
Sleep 300ms
Type "BRANCH_ID=$(kbagent --json branch create --project padak-2-0 --name demo-types | jq -r '.data.branch_id')"
Sleep 200ms
Enter
Sleep 3500ms
Type "echo BRANCH_ID=$BRANCH_ID"
Sleep 200ms
Enter
Sleep 1500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 2: create a typed table with native Snowflake types"
Enter
Sleep 200ms
Type "#   VARCHAR(40) / NUMERIC(18,2) / TIMESTAMP_TZ / VARIANT / BOOLEAN"
Enter
Sleep 200ms
Type "#   + --not-null and --default (maps to definition.nullable / .default)"
Enter
Sleep 300ms
Type "#   Bucket in.c-demo-orders is NOT materialized in this branch yet --"
Enter
Sleep 200ms
Type "#   kbagent auto-creates it (EnsureBucketExists pattern)."
Enter
Sleep 800ms

Type "kbagent --json storage create-table --project padak-2-0 --branch $BRANCH_ID \\"
Enter
Sleep 100ms
Type "  --bucket-id in.c-demo-orders --name sales \\"
Enter
Sleep 100ms
Type "  --column 'pk:VARCHAR(40)' --column 'amount:NUMERIC(18,2)' \\"
Enter
Sleep 100ms
Type "  --column 'ts:TIMESTAMP_TZ' --column 'is_paid:BOOLEAN' --column 'meta:VARIANT' \\"
Enter
Sleep 100ms
Type "  --primary-key pk --not-null pk --not-null amount \\"
Enter
Sleep 100ms
Type "  --default amount=0 --default is_paid=false \\"
Enter
Sleep 100ms
Type "  | jq '{ auto_created_bucket: .data.auto_created_bucket, table_id: .data.table_id }'"
Sleep 300ms
Enter
Sleep 5500ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 3: inspect the stored column definitions"
Enter
Sleep 200ms
Type "#   definition.length carried through; Snowflake picked the right native type."
Enter
Sleep 500ms

Type "kbagent --json storage table-detail --project padak-2-0 --branch $BRANCH_ID \\"
Enter
Sleep 100ms
Type "  --table-id in.c-demo-orders.sales | jq '.data.column_details'"
Sleep 300ms
Enter
Sleep 4000ms

Type "clear"
Sleep 200ms
Enter
Sleep 200ms

Type "# Step 4: cleanup (deleting the branch cascades to its bucket and table)"
Enter
Sleep 300ms
Type "kbagent branch delete --project padak-2-0 --branch $BRANCH_ID"
Sleep 300ms
Enter
Sleep 3000ms
