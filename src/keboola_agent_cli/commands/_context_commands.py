"""The command part of `kbagent context`, generated from docs/cli-manifest.json.

Do not edit. `make cli-manifest` writes this file, and the rule
`help.context-from-manifest` of `make cli-spec-check` fails when it differs from
the text generated from the manifest (AG1 of docs/cli-interface-spec.md).
Hand-written notes per group live in `commands/context.py`.
"""

# Hidden alias -> the group it stands for.
ALIASES: dict[str, str] = {
    "mr": "merge-request",
    "sl": "semantic-layer",
}

# Top-level group or command -> its commands: usage and the first help paragraph.
COMMAND_SECTIONS: dict[str, str] = {
    "agent": """\
### agent

Scheduled agent tasks (cron / manual / chained)

  kbagent agent create --name NAME [--description DESCRIPTION] [--cron CRON]
      [--manual] [--enabled/--disabled] [--type TYPE] [--from-file FILE]
      [--cli CLI] [--prompt PROMPT] [--extra-arg ARG ...] [--argv ARGV ...]
      [--timeout N] [--trigger-task-id ID] [--trigger-on ON]
    Register a new scheduled task.

  kbagent agent cron-preview --cron CRON [--count N]
    Show the next N firings of a cron expression.

  kbagent agent delete [TASK_ID] [--task-id ID] [--yes]
    Remove a task. Run history on disk is preserved.

  kbagent agent list
    List all registered agent tasks.

  kbagent agent prompt-improve --goal GOAL [--draft DRAFT] [--cli CLI]
      [--project PROJECT] [--extra-arg ARG ...] [--stream/--no-stream]
    Polish a plain-English goal into an unattended-agent-ready prompt.

  kbagent agent run [TASK_ID] [--task-id ID] [--stream]
      [--runtime-prompt PROMPT] [--runtime-input INPUT]
    Trigger a task immediately (does not wait for the next cron firing).

  kbagent agent run-detail [TASK_ID] [RUN_ID] [--task-id ID] [--run-id ID]
    Show a single AgentRun record (status, summary, output, error).

  kbagent agent run-events [TASK_ID] [RUN_ID] [--task-id ID] [--run-id ID]
    Replay the persisted event timeline of an ai_agent run (line-by-line).

  kbagent agent runs [TASK_ID] [--task-id ID] [--limit N]
    Show the run history of a task (most recent first).

  kbagent agent show [TASK_ID] [--task-id ID]
    Show one task's full configuration.

  kbagent agent test [--name NAME] [--stream] [--type TYPE] [--from-file FILE]
      [--cli CLI] [--prompt PROMPT] [--extra-arg ARG ...] [--argv ARGV ...]
      [--timeout N]
    Execute an action ad-hoc (no persistence, no scheduling).

  kbagent agent update [TASK_ID] [--task-id ID] [--name NAME]
      [--description DESCRIPTION] [--cron CRON] [--enabled/--disabled]
      [--manual/--auto] [--clear-trigger] [--trigger-task-id ID]
      [--trigger-on ON]
    Patch one or more fields on a task. Omitted flags leave the field
    unchanged.
""",
    "auth": """\
### auth

Programmatic browser login (PKCE / device code) -- user-scoped sessions

  kbagent auth login [--stack STACK] [--device-code] [--register-projects]
      [--no-open]
    Sign in to a Keboola stack via browser login (PKCE) or device code.

  kbagent auth login-password --email EMAIL [--password PASSWORD]
      [--password-stdin] [--totp-secret SECRET] [--stack STACK]
      [--register-projects]
    Sign in via email + password (+ TOTP if the account has MFA) -- no
    browser.

  kbagent auth logout [--stack STACK] [--remove-projects] [--yes]
    Revoke and clear the local programmatic-auth session for a stack.

  kbagent auth register-projects [--stack STACK] [--all] [--project-id N ...]
      [--alias ALIAS ...] [--yes]
    Register accessible projects from the current session as local aliases.

  kbagent auth status [--stack STACK]
    Show the programmatic-auth session health for a stack.
""",
    "billing": """\
### billing

PAYG credit balance across projects (issue #594). Balance only -- purchase
history / Stripe invoice IDs are not reachable with a project token and are
not exposed by this command.

  kbagent billing credits [--project PROJECT ...] [--all-projects]
    Show the current PAYG credit balance for one or more projects.
""",
    "branch": """\
### branch

Manage development branches

  kbagent branch create [--project PROJECT] --name NAME
      [--description DESCRIPTION]
    Create a new development branch and auto-activate it.

  kbagent branch delete [--project PROJECT] --branch N [--dry-run] [--yes]
    Delete a development branch.

  kbagent branch list [--project PROJECT ...] [--all-projects]
    List development branches of the active project, the given projects, or
    all projects.

  kbagent branch metadata-delete [--project PROJECT] --metadata-id N [--yes]
      [--branch BRANCH]
    Delete a branch metadata entry by its numeric ID.

  kbagent branch metadata-get [--project PROJECT] --key KEY [--branch BRANCH]
    Read a single metadata value by key.

  kbagent branch metadata-list [--project PROJECT] [--branch BRANCH]
    List all metadata entries on a branch.

  kbagent branch metadata-set [--project PROJECT] --key KEY [--text TEXT]
      [--file PATH] [--stdin] [--branch BRANCH]
    Set a metadata key/value on a branch.

  kbagent branch reset [--project PROJECT]
    Reset the active branch back to main/production.

  kbagent branch use [--project PROJECT] --branch N
    Set an existing development branch as active.
""",
    "changelog": """\
### changelog

  kbagent changelog [--limit N] [--full]
    Show recent changelog (what changed in each version).
""",
    "component": """\
### component

Discover and inspect Keboola components

  kbagent component detail --component-id ID [--project PROJECT]
    Show detailed information about a specific component.

  kbagent component list [--project PROJECT ...] [--all-projects]
      [--type TYPE] [--query QUERY]
    List the components of the active project, the given projects, or all
    projects.

  kbagent component sync-action ACTION_NAME --component-id ID [--config-id ID]
      [--row-id ID] [--project PROJECT] [--branch N] [--config-data DATA]
      [--timeout N]
    Run a synchronous component action such as testConnection.
""",
    "config": """\
### config

Browse and inspect configurations

  kbagent config clone [--project PROJECT] --component-id ID --config-id ID
      --name NAME [--target-project PROJECT] [--description DESCRIPTION]
      [--set SET ...] [--secret SECRET ...] [--branch N] [--target-branch N]
      [--dry-run] [--allow-plaintext-on-encrypt-failure]
    Duplicate a configuration, whole -- including runtime, storage and
    authorization.

  kbagent config delete [--project PROJECT] --component-id ID --config-id ID
      [--branch N] [--dry-run]
    Soft-delete a configuration into the trash (restorable).

  kbagent config delete-metadata [--project PROJECT] --component-id ID
      --config-id ID --metadata-id N [--branch N] [--yes]
    Delete a configuration metadata entry by its numeric ID.

  kbagent config detail [--project PROJECT ...] --component-id ID
      [--config-id ID] [--branch N] [--with-state]
    Show detailed information about one or many configurations.

  kbagent config examples --component-id ID [--project PROJECT] [--row]
    Show sample configuration JSON examples for a component.

  kbagent config get-metadata [--project PROJECT] --component-id ID
      --config-id ID --key KEY [--branch N]
    Read a single metadata value by key.

  kbagent config list [--project PROJECT ...] [--all-projects]
      [--component-type TYPE] [--component-id ID] [--branch N]
      [--include-rows]
    List configurations of the active project, the given projects, or all
    projects.

  kbagent config metadata-list [--project PROJECT] --component-id ID
      --config-id ID [--branch N]
    List all metadata entries on a configuration.

  kbagent config new --component-id ID [--name NAME] [--project PROJECT]
      [--output-dir DIR] [--push] [--no-files] [--description DESCRIPTION]
      [--configuration CONFIGURATION] [--configuration-file PATH]
      [--no-validate] [--branch N] [--dry-run]
      [--allow-plaintext-on-encrypt-failure]
    Generate boilerplate configuration files for a Keboola component,
    optionally creating the config remotely in one shot.

  kbagent config oauth-url [--project PROJECT] --component-id ID
      --config-id ID [--redirect-url URL] [--no-open]
    Requires master token. Generate an OAuth authorization URL for a component
    configuration and open it in the default browser.

  kbagent config rename [--project PROJECT] --component-id ID --config-id ID
      --name NAME [--branch N] [--directory PATH]
    Rename a configuration (update name via API + rename local sync
    directory).

  kbagent config restore [--project PROJECT] --component-id ID --config-id ID
      [--branch N]
    Restore a configuration from the trash (undo of 'config delete').

  kbagent config row-create [--project PROJECT] --component-id ID
      --config-id ID --name NAME [--description DESCRIPTION]
      [--configuration CONFIGURATION] [--is-disabled] [--branch N]
      [--allow-plaintext-on-encrypt-failure]
    Create a new configuration row.

  kbagent config row-delete [--project PROJECT] --component-id ID
      --config-id ID --row-id ID [--branch N] [--yes]
    Delete a configuration row.

  kbagent config row-update [--project PROJECT] --component-id ID
      --config-id ID --row-id ID [--name NAME] [--description DESCRIPTION]
      [--configuration CONFIGURATION] [--set SET ...] [--merge]
      [--change-description DESCRIPTION] [--dry-run] [--is-disabled]
      [--is-enabled] [--branch N] [--allow-plaintext-on-encrypt-failure]
    Update an existing configuration row.

  kbagent config search --query QUERY [--project PROJECT ...] [--all-projects]
      [--component-type TYPE] [--component-id ID] [--ignore-case] [--regex]
      [--branch N]
    Search through configuration bodies for a string or pattern.

  kbagent config set-default-bucket [--project PROJECT] --component-id ID
      --config-id ID [--bucket BUCKET] [--clear] [--dry-run] [--branch N]
    Set or clear ``storage.output.default_bucket`` on a configuration.

  kbagent config set-folder [--project PROJECT] --component-id ID
      --config-id ID --name NAME [--branch N]
    Set the UI folder of a configuration (its `folder` field).

  kbagent config set-metadata [--project PROJECT] --component-id ID
      --config-id ID --key KEY --value VALUE [--branch N]
    Set a metadata key/value on a configuration (upsert).

  kbagent config state-get [--project PROJECT] --component-id ID
      --config-id ID [--row-id ID] [--branch N]
    Read the runtime ``state`` dict of a configuration or one of its rows.

  kbagent config state-set [--project PROJECT] --component-id ID
      --config-id ID --state STATE [--row-id ID] [--branch N] [--dry-run]
      [--yes]
    Overwrite the runtime ``state`` dict of a configuration or one of its
    rows.

  kbagent config trash-list [--project PROJECT] [--component-id ID]
      [--branch N]
    List configurations in the trash (restorable via 'config restore').

  kbagent config update [--project PROJECT] --component-id ID --config-id ID
      [--name NAME] [--description DESCRIPTION]
      [--configuration CONFIGURATION] [--configuration-file PATH]
      [--set SET ...] [--merge] [--change-description DESCRIPTION] [--dry-run]
      [--branch N] [--allow-plaintext-on-encrypt-failure]
    Update a configuration's metadata and/or content.

  kbagent config variables-clear [--project PROJECT] --component-id ID
      --config-id ID [--branch N] [--yes]
    Unlink variables from a config (does NOT delete the underlying
    keboola.variables).

  kbagent config variables-get [--project PROJECT] --component-id ID
      --config-id ID [--branch N]
    Read the current variable values attached to a config.

  kbagent config variables-set [--project PROJECT] --component-id ID
      --config-id ID [--var VAR ...] [--replace] [--variables-id ID]
      [--values-id ID] [--branch N] [--dry-run]
      [--allow-plaintext-on-encrypt-failure]
    Assign variables to a config (auto-creates backing keboola.variables on
    first call).
""",
    "context": """\
### context

  kbagent context [GROUP]
    Show usage instructions for AI agents interacting with Keboola.
""",
    "data-app": """\
### data-app

Keboola data-app lifecycle (create, deploy, manage)

  kbagent data-app create [--project PROJECT] --name NAME
      [--description DESCRIPTION] [--description-file PATH] --slug SLUG
      [--git-repo REPO] [--use-managed-git-repo] [--git-branch BRANCH]
      [--git-public/--no-git-public] [--git-username USERNAME]
      [--git-pat-env ENV] [--git-pat-file PATH]
      [--git-pat-encrypted ENCRYPTED] [--auth AUTH] [--size SIZE]
      [--auto-suspend N] [--type TYPE] [--workspace/--no-workspace]
      [--branch N] [--no-deploy] [--wait] [--timeout N] [--keep-on-failure]
      [--dry-run] [--copy] [--reveal]
    Create a Keboola data app end-to-end (POST + encrypt + PUT + deploy).

  kbagent data-app delete [--project PROJECT] --app-id ID [--yes]
    Delete the deployment AND the Storage config (cascade, irreversible).

  kbagent data-app deploy [--project PROJECT] --app-id ID
      [--config-version VERSION] [--wait] [--timeout N] [--branch N]
      [--git-branch BRANCH] [--copy] [--reveal]
    Deploy the latest Storage config (the §9 redeploy contract).

  kbagent data-app detail [--project PROJECT] --app-id ID [--branch N]
    Show merged Data Science + Storage detail for one data app.

  kbagent data-app git-credentials [--project PROJECT] --app-id ID
    List the credentials of a data app's MANAGED git repository.

  kbagent data-app git-credentials-create [--project PROJECT] --app-id ID
      --type TYPE --permissions PERMISSIONS [--public-key KEY]
      [--public-key-file PATH] [--name NAME] [--yes]
    Create a git credential (SSH key or HTTP token) for a MANAGED repo.

  kbagent data-app git-repo [--project PROJECT] --app-id ID
    Show the clone URLs of a data app's configured git repository.

  kbagent data-app list [--project PROJECT ...] [--all-projects] [--branch N]
    List data apps across one or more registered projects.

  kbagent data-app logs [--project PROJECT] --app-id ID [--lines N]
      [--since SINCE]
    Tail the container logs for a deployed data app.

  kbagent data-app password [--project PROJECT] --app-id ID [--copy]
      [--reveal] [--open]
    Copy the password of a password-protected data app to the clipboard.

  kbagent data-app runs [--project PROJECT] --app-id ID [--limit N]
    List a data app's recent deployment attempts (runs), newest first.

  kbagent data-app secrets-get [--project PROJECT] --app-id ID --key KEY
      [--branch N]
    Show ONE key from parameters.dataApp.secrets.

  kbagent data-app secrets-list [--project PROJECT] --app-id ID [--branch N]
      [--show-fingerprint]
    List the keys in parameters.dataApp.secrets, with derived runtime env-var
    names.

  kbagent data-app secrets-remove [--project PROJECT] --app-id ID
      --key KEY ... [--branch N] [--yes] [--dry-run]
    Remove one or more app-runtime secrets. Idempotent (missing keys are exit
    0).

  kbagent data-app secrets-set [--project PROJECT] --app-id ID
      [--secret SECRET ...] [--secrets-file PATH] [--branch N]
      [--allow-plaintext-on-encrypt-failure] [--dry-run] [--no-hint-next]
    Encrypt and write app-runtime secrets to the linked Storage config.

  kbagent data-app start [--project PROJECT] --app-id ID [--wait]
      [--timeout N]
    Wake an auto-suspended data app at its currently-pinned configVersion.

  kbagent data-app stop [--project PROJECT] --app-id ID [--wait] [--timeout N]
    Stop a running data app (preserves the URL and Storage config).

  kbagent data-app validate-repo --git-repo REPO [--git-branch BRANCH]
      [--git-public/--no-git-public] [--git-pat-env ENV] [--git-pat-file PATH]
      [--type TYPE] [--strict]
    Pre-flight check that a git repo follows the Keboola data-app Golden Rule.
""",
    "dev-portal": """\
### dev-portal

Keboola Developer Portal — multi-identity, production-safe writes.

  kbagent dev-portal create --vendor VENDOR --data DATA [--identity IDENTITY]
      [--dry-run]
    Create (register) a new app in the Developer Portal. Requires TTY confirm;
    --dry-run for preview.

  kbagent dev-portal deprecate --app APP [--identity IDENTITY] [--dry-run]
    Deprecate an app in the Developer Portal (hides it, blocks new configs).
    Requires TTY confirm; --dry-run for preview.

  kbagent dev-portal get --app APP [--identity IDENTITY]
    Show the full Developer Portal entry for one app.

  kbagent dev-portal identity add --alias ALIAS --username USERNAME
      [--password PASSWORD] [--password-stdin] [--role-hint vendor|admin]
      [--vendor VENDOR] [--portal-url URL]
    Add a Developer Portal identity (verifies creds before persisting).

  kbagent dev-portal identity current
    Show the alias of the default Developer Portal identity.

  kbagent dev-portal identity edit --alias ALIAS [--username USERNAME]
      [--password PASSWORD] [--password-stdin] [--role-hint vendor|admin]
      [--vendor VENDOR] [--new-alias ALIAS]
    Edit fields on a Developer Portal identity (or rename it).

  kbagent dev-portal identity list
    List configured Developer Portal identities.

  kbagent dev-portal identity remove --alias ALIAS
    Remove a Developer Portal identity.

  kbagent dev-portal identity use ALIAS
    Set the default Developer Portal identity.

  kbagent dev-portal identity verify [--identity IDENTITY]
    Probe a Developer Portal identity by logging in.

  kbagent dev-portal list --vendor VENDOR [--identity IDENTITY]
    List Developer Portal apps for a vendor.

  kbagent dev-portal patch --app APP [--data DATA] [--property PROPERTY]
      [--value VALUE] [--value-file FILE] [--identity IDENTITY] [--dry-run]
    Patch one or more properties of an existing Developer Portal app. Requires
    TTY confirm; --dry-run for preview.

  kbagent dev-portal publish --app APP [--identity IDENTITY] [--dry-run]
    Publish an app in the Developer Portal (requests Keboola review). Requires
    TTY confirm; --dry-run for preview.

  kbagent dev-portal upload-icon --app APP --file FILE [--identity IDENTITY]
      [--dry-run]
    Upload a 128x128 PNG icon for a Developer Portal app. Requires TTY
    confirm; --dry-run for preview.
""",
    "docs": """\
### docs

Ask the Keboola documentation natural-language questions

  kbagent docs query QUESTION [--project PROJECT]
    Ask the Keboola documentation a natural language question.
""",
    "doctor": """\
### doctor

  kbagent doctor
    Run health checks on CLI configuration and project connectivity.
""",
    "encrypt": """\
### encrypt

Encrypt secret values via Keboola Encryption API (one-way, no decrypt)

  kbagent encrypt values [--project PROJECT] --component-id ID --input INPUT
      [--output-file PATH]
    Encrypt #-prefixed secret values for a Keboola component.
""",
    "feature": """\
### feature

Feature flag management (requires super-admin Manage API token)

  kbagent feature list [--project PROJECT]
    List all feature flags defined on the stack.

  kbagent feature project-add [--project PROJECT] --feature FEATURE
      [--dry-run] [--yes]
    Enable a feature flag on a project.

  kbagent feature project-remove [--project PROJECT] --feature FEATURE
      [--dry-run] [--yes]
    Disable a feature flag on a project (destructive).

  kbagent feature project-show [--project PROJECT]
    Show feature flags assigned to a project.

  kbagent feature user-add [--project PROJECT] --email EMAIL --feature FEATURE
      [--dry-run] [--yes]
    Enable a feature flag on a user.

  kbagent feature user-remove [--project PROJECT] --email EMAIL
      --feature FEATURE [--dry-run] [--yes]
    Disable a feature flag on a user (destructive).

  kbagent feature user-show [--project PROJECT] --email EMAIL
    Show feature flags assigned to a user.
""",
    "flow": """\
### flow

Manage conditional flows (keboola.flow)

  kbagent flow delete [--project PROJECT] --flow-id ID [--branch N]
      [--dry-run] [--yes]
    Delete a conditional-flow (keboola.flow) configuration.

  kbagent flow detail [--project PROJECT] --flow-id ID [--branch N]
    Show detailed conditional-flow information including phases and tasks.

  kbagent flow examples [--component-id ID]
    Show bundled example flow configurations (offline, no project needed).

  kbagent flow list [--project PROJECT ...] [--all-projects] [--branch N]
      [--with-schedules]
    List conditional flows (keboola.flow) across projects.

  kbagent flow new [--project PROJECT] --name NAME [--description DESCRIPTION]
      [--file FILE] [--branch N]
    Create a new conditional-flow (keboola.flow) configuration.

  kbagent flow schedule [--project PROJECT] --flow-id ID --cron CRON
      [--timezone TIMEZONE] [--enabled/--disabled] [--name NAME] [--branch N]
    Bind a cron schedule to a flow (upsert: creates or updates).

  kbagent flow schedule-remove [--project PROJECT] --flow-id ID [--branch N]
      [--dry-run] [--yes]
    Remove all schedules bound to a flow (deletes keboola.scheduler configs).

  kbagent flow schema [--full] [--project PROJECT]
    Print the conditional-flow YAML template, or --full for the JSON Schema.

  kbagent flow triggers [--project PROJECT] --flow-id ID [--branch N]
    Show every trigger kbagent can see for a flow (cron + table triggers).

  kbagent flow update [--project PROJECT] --flow-id ID [--name NAME]
      [--description DESCRIPTION] [--file FILE] [--branch N]
    Update a flow's name, description, or phases/tasks.

  kbagent flow validate --file FILE [--project PROJECT]
    Validate a conditional-flow definition (schema + semantic checks).
""",
    "http": """\
### http

Raw HTTP client against the running `kbagent serve` (uses KBAGENT_SERVE_URL +
KBAGENT_SERVE_TOKEN env vars).

  kbagent http delete PATH [--timeout N]
    DELETE an endpoint on the running kbagent serve.

  kbagent http get PATH [--timeout N]
    GET an endpoint on the running kbagent serve.

  kbagent http patch PATH [--body BODY] [--timeout N]
    PATCH an endpoint on the running kbagent serve.

  kbagent http post PATH [--body BODY] [--timeout N]
    POST to an endpoint on the running kbagent serve.
""",
    "init": """\
### init

  kbagent init [--from-global] [--project PROJECT ...] [--read-only]
    Initialize a local .kbagent/ workspace in the current directory.
""",
    "job": """\
### job

Browse job history and run jobs

  kbagent job detail [--project PROJECT] --job-id ID [--log-tail-lines N]
    Show detailed information about a specific job.

  kbagent job list [--project PROJECT ...] [--all-projects]
      [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]
      [--offset N] [--sort-by BY] [--sort-order ORDER]
    List jobs of the active project, the given projects, or all projects.

  kbagent job run [--project PROJECT] --component-id ID --config-id ID
      [--row-id ID ...] [--wait] [--timeout N] [--branch N] [--mode run|debug]
      [--variable-values-id ID] [--no-variables]
      [--poll-strategy exponential|fixed] [--log-tail-lines N]
      [--idempotency-key KEY] [--force-rerun]
    Run a job for a component configuration.

  kbagent job terminate [--project PROJECT] [--job-id ID ...]
      [--status STATUS] [--component-id ID] [--config-id ID] [--limit N]
      [--branch N] [--dry-run] [--yes]
    Terminate one or more Queue API jobs (use to stop runaway or stuck jobs).
""",
    "kai": """\
### kai

(BETA) Keboola AI Assistant (Kai) — ask questions about your project.

  kbagent kai ask --message MESSAGE [--project PROJECT]
    Ask Kai a one-shot question and get the full response.

  kbagent kai chat --message MESSAGE [--chat-id ID] [--project PROJECT]
    Send a message to Kai in a chat session.

  kbagent kai chat-detail --chat-id ID [--project PROJECT]
    Fetch the full message history of a single Kai chat.

  kbagent kai history [--project PROJECT] [--limit N]
    List recent Kai chat sessions.

  kbagent kai ping [--project PROJECT]
    Check Kai server health and MCP connection status.

  kbagent kai preflight [--project PROJECT]
    Check whether the configured token can use Kai (master token + AI Agent
    Chat).
""",
    "lineage": """\
### lineage

Column-level data lineage across projects.

  kbagent lineage build [--directory PATH] --output PATH [--ai] [--refresh]
    Build column-level lineage graph from sync'd data.

  kbagent lineage info --load PATH
    Show what's in a cached lineage graph.

  kbagent lineage server --load PATH [--port N] [--host HOST]
    Start a local web server with interactive lineage browser.

  kbagent lineage show --load PATH [--upstream UPSTREAM]
      [--downstream DOWNSTREAM] [--column COLUMN] [--columns]
      [--project PROJECT] [--depth N] [--format FORMAT]
    Query upstream/downstream dependencies from a cached lineage graph.
""",
    "merge-request": """\
### merge-request

Merge requests: merge a development branch into production with review
(Branches 2.0, non-SOX)

  kbagent merge-request approve [--project PROJECT] [--merge-request-id N]
      [--branch N]
    Add your approval (destructive: the last approval is what a merge waits
    for).

  kbagent merge-request auto-merge [--project PROJECT] [--merge-request-id N]
      [--branch N] --strategy STRATEGY [--at AT] [--yes]
    Arm or disarm automatic merging of this merge request (destructive).

  kbagent merge-request conflicts [--project PROJECT] [--merge-request-id N]
      [--branch N]
    List the configurations changed on both sides (computed live by the
    backend).

  kbagent merge-request create [--project PROJECT] --title TITLE [--branch N]
      [--description DESCRIPTION] [--reviewer-id N ...] [--external-id ID]
    Open a merge request from a development branch into production.

  kbagent merge-request detail [--project PROJECT] [--merge-request-id N]
      [--branch N] [--activity-log]
    Show one merge request: readiness, blockers, reviewers, change log,
    conflicts.

  kbagent merge-request diff [--project PROJECT] [--merge-request-id N]
      [--branch N] --component-id ID --config-id ID [--format FORMAT]
      [--output PATH]
    Three-way diff of one conflicting configuration, classified per path.

  kbagent merge-request list [--project PROJECT] [--state STATE]
    List the project's merge requests, newest first.

  kbagent merge-request merge [--project PROJECT] [--merge-request-id N]
      [--branch N] [--yes]
    Merge the merge request into production and delete its source branch.

  kbagent merge-request request-changes [--project PROJECT]
      [--merge-request-id N] [--branch N] [--reason REASON]
    Send the merge request back to development; existing approvals are
    removed.

  kbagent merge-request request-review [--project PROJECT]
      [--merge-request-id N] [--branch N]
    Send the merge request for review (destructive: it moves the MR toward
    production).

  kbagent merge-request resolve [--project PROJECT] [--merge-request-id N]
      [--branch N] --component-id ID --config-id ID [--take TAKE]
      [--resolved RESOLVED] [--change-description DESCRIPTION]
    Resolve one conflicting configuration (destructive: it removes a merge
    blocker).

  kbagent merge-request update [--project PROJECT] [--merge-request-id N]
      [--branch N] [--title TITLE] [--description DESCRIPTION]
      [--reviewer-id N ...] [--external-id ID]
    Change a merge request's title, description, reviewers or external id.
""",
    "notification": """\
### notification

Manage notification subscriptions across projects (Flow Notifications tab).
'list'/'detail' are read-only; 'create'/'replace-recipient' are gated as
write, 'delete' as destructive.

  kbagent notification create [--project PROJECT] --event EVENT
      --channel CHANNEL --address ADDRESS [--component-id ID] [--config-id ID]
      [--branch N] [--expires-at AT]
    Create a notification subscription.

  kbagent notification delete [--project PROJECT] --subscription-id ID [--yes]
    Delete a notification subscription.

  kbagent notification detail [--project PROJECT] --subscription-id ID
    Show one notification subscription, including its raw filter list.

  kbagent notification list [--project PROJECT ...] [--all-projects]
      [--event EVENT] [--component-id ID] [--config-id ID]
    List notification subscriptions (Flow Notifications tab) across projects.

  kbagent notification replace-recipient [--project PROJECT]
      --subscription-id ID --address ADDRESS [--channel CHANNEL] [--yes]
    Replace a subscription's recipient.
""",
    "oauth": """\
### oauth

OAuth broker maintenance: stored oauth:manage identities, consumer reads,
Entra secret rotation. Writes need a typed code at a terminal; no --yes.

  kbagent oauth consumer get --stack STACK --component COMPONENT
    Show one broker consumer. appSecret and appSecretDocker show as
    <encrypted>.

  kbagent oauth identity add --stack STACK
    Store an oauth:manage application token for a stack in the macOS keychain.

  kbagent oauth identity verify --stack STACK
    Check the stored token again: keychain protection, scopes, broker access.

  kbagent oauth rotate-azure-secret --stack STACK --component COMPONENT
      --app-id ID --op-vault VAULT [--expires-in-days N] [--dry-run]
    Rotate an Entra client secret behind one broker consumer.
""",
    "org": """\
### org

Organization management

  kbagent org setup [--org-id N] [--project-ids IDS ...] --url URL [--dry-run]
      [--yes] [--token-description DESCRIPTION] [--token-expires-in N]
      [--refresh]
    Set up projects and register them in the kbagent config.
""",
    "permissions": """\
### permissions

Manage operation permissions (firewall rules)

  kbagent permissions check OPERATION
    Check if a specific operation is allowed.

  kbagent permissions list [--category CATEGORY]
    List all operations with their risk category and current allowed/denied
    status.

  kbagent permissions reset
    Remove all permission restrictions.

  kbagent permissions set --mode MODE [--allow ALLOW ...] [--deny DENY ...]
    Set the permission policy (firewall rules).

  kbagent permissions show
    Show the current active permission policy.
""",
    "project": """\
### project

Manage connected Keboola projects

  kbagent project add --alias ALIAS [--url URL] [--token TOKEN]
      [--token-stdin] [--token-file PATH] [--token-env ENV]
      [--keep-token-file]
    Add a new Keboola project connection.

  kbagent project create --url URL [--alias ALIAS] [--name NAME]
      [--backend snowflake|bigquery] [--sync-backend-init]
    Create a brand-new Keboola project -- no account, no token needed.

  kbagent project current
    Show the active project.

  kbagent project description-get [--project PROJECT]
    Get the Keboola dashboard project description.

  kbagent project description-set [--project PROJECT] [--text TEXT]
      [--file PATH] [--stdin]
    Set the Keboola dashboard project description (markdown).

  kbagent project edit [--project PROJECT] [--url URL] [--token TOKEN]
      [--token-stdin] [--token-file PATH] [--token-env ENV]
      [--keep-token-file] [--new-alias ALIAS] [--dry-run]
    Edit an existing Keboola project connection.

  kbagent project info [--project PROJECT]
    Show detailed project metadata.

  kbagent project invitation-cancel [--project PROJECT] --email EMAIL
      [--invitation-id N] [--dry-run] [--yes]
    Cancel a pending invitation.

  kbagent project invitation-list [--project PROJECT]
    List pending project invitations.

  kbagent project invite [--project PROJECT] [--email EMAIL]
      [--role admin|guest|readOnly|share] [--reason REASON] [--from-csv PATH]
      [--default-role admin|guest|readOnly|share] [--workers N] [--dry-run]
    Invite a user (or many users via CSV) to one or more projects.

  kbagent project list
    List all connected Keboola projects.

  kbagent project member-list [--project PROJECT] [--include-pending]
    List active members of a project (and optionally pending invitations).

  kbagent project member-remove [--project PROJECT] --email EMAIL [--yes]
    Remove an active member from a project (destructive).

  kbagent project member-set-role [--project PROJECT] --email EMAIL
      --role admin|guest|readOnly|share [--dry-run]
    Change an existing member's role (PATCH).

  kbagent project refresh [--project PROJECT] [--all-projects] [--dry-run]
      [--force] [--yes] [--token-description DESCRIPTION]
      [--token-expires-in N]
    Refresh expired or invalid Storage API tokens.

  kbagent project remove [--project PROJECT] [--dry-run] [--yes]
    Remove a Keboola project connection and its stored token.

  kbagent project status [--project PROJECT] [--all-projects]
    Test connectivity to the active project, one project, or all projects.

  kbagent project use ALIAS
    Set <alias> as the active project for later commands.
""",
    "repl": """\
### repl

  kbagent repl
    Start interactive REPL mode for kbagent.
""",
    "schedule": """\
### schedule

Discover and audit cron schedules across projects (keboola.scheduler)

  kbagent schedule detail [--project PROJECT] --schedule-id ID [--branch N]
    Show full detail for a single cron schedule.

  kbagent schedule find [--project PROJECT ...] [--all-projects]
      [--cron-window WINDOW] [--not-run-since N] [--branch N]
    Audit schedules by cron window or job-freshness.

  kbagent schedule list [--project PROJECT ...] [--all-projects]
      [--enabled-only] [--branch N]
    List cron schedules (keboola.scheduler configs) across projects.
""",
    "search": """\
### search

  kbagent search QUERY [--project PROJECT ...] [--all-projects]
      [--type TYPE ...] [--search-type TYPE] [--limit N] [--regex]
      [--scope SCOPE ...]
    Search for items (tables, buckets, configs, flows, …) by name or content.
""",
    "semantic-layer": """\
### semantic-layer

Manage Keboola semantic layer (metastore) models -- datasets, metrics,
relationships, constraints, and glossary terms.

  kbagent semantic-layer add constraint [--project PROJECT] [--model MODEL]
      --name NAME --constraint-type TYPE --rule RULE --metrics METRICS ...
      [--severity SEVERITY] [--scope project|organization|targeted]
      [--target-project PROJECT ...]
    Add a constraint.

  kbagent semantic-layer add dataset [--project PROJECT] [--model MODEL]
      --name NAME --table-id ID [--description DESCRIPTION] [--grain GRAIN]
      [--primary-key KEY ...] [--deep-fields] [--fqn FQN]
      [--scope project|organization|targeted] [--target-project PROJECT ...]
    Add a dataset (FQN read from the table's Storage location).

  kbagent semantic-layer add glossary [--project PROJECT] [--model MODEL]
      --term TERM --definition DEFINITION
      [--scope project|organization|targeted] [--target-project PROJECT ...]
    Add a glossary term.

  kbagent semantic-layer add metric [--project PROJECT] [--model MODEL]
      --name NAME --sql SQL --dataset DATASET [--description DESCRIPTION]
      [--yes] [--scope project|organization|targeted]
      [--target-project PROJECT ...]
    Add a metric to a semantic-layer model.

  kbagent semantic-layer add relationship [--project PROJECT] [--model MODEL]
      --name NAME --from FROM --to TO --on ON [--type TYPE]
      [--scope project|organization|targeted] [--target-project PROJECT ...]
    Add a relationship between two datasets.

  kbagent semantic-layer build [--project PROJECT] [--model MODEL]
      [--tables TABLES ...] [--name NAME] [--dry-run] [--keep-on-failure]
      [--output PATH] [--types-workspace N] [--auto-types-workspace]
    Build a semantic-layer model from a list of storage tables
    (non-interactive).

  kbagent semantic-layer diff [--project-a A] [--project-b B] [--model-a A]
      [--model-b B] [--file-a PATH] [--file-b PATH]
    Diff two semantic-layer snapshots (project↔project, project↔file,
    file↔file).

  kbagent semantic-layer edit constraint [--project PROJECT] [--model MODEL]
      --name NAME [--new-name NAME] [--new-rule RULE]
      [--new-constraint-type TYPE] [--new-severity SEVERITY]
      [--new-metrics METRICS ...]
    Edit a constraint (in-place update, with local validators).

  kbagent semantic-layer edit dataset [--project PROJECT] [--model MODEL]
      --name NAME [--new-name NAME] [--new-description DESCRIPTION]
      [--new-grain GRAIN]
    Edit a dataset (no cascade — metric.dataset uses tableId, not name).

  kbagent semantic-layer edit glossary [--project PROJECT] [--model MODEL]
      --term TERM [--new-term TERM] [--new-definition DEFINITION] [--yes]
    Edit a glossary term. ``--new-term`` is destructive for downstream joins.

  kbagent semantic-layer edit metric [--project PROJECT] [--model MODEL]
      --name NAME [--new-name NAME] [--new-sql SQL] [--new-dataset DATASET]
      [--new-description DESCRIPTION] [--yes]
    Edit a metric. Rename cascades to any constraint that references it.

  kbagent semantic-layer edit relationship [--project PROJECT] [--model MODEL]
      --name NAME [--new-name NAME] [--new-from FROM] [--new-to TO]
      [--new-on ON] [--new-type TYPE]
    Edit a relationship (in-place update). Validates ``--new-type`` locally.

  kbagent semantic-layer export [--project PROJECT] [--model MODEL]
      [--output PATH]
    Snapshot a semantic-layer model to a self-describing JSON file.

  kbagent semantic-layer get-context [--project PROJECT] --context-id ID
    Fetch a single semantic-layer entity by id, irrespective of its type.

  kbagent semantic-layer import [--project PROJECT] --file PATH
      [--model MODEL] [--types TYPES ...] [--dry-run] [--overwrite]
      [--scope project|organization|targeted] [--target-project PROJECT ...]
    Replay a snapshot into a project. Default: skip on conflict (no surprise
    overwrites).

  kbagent semantic-layer model create [--project PROJECT] --name NAME
      [--description DESCRIPTION] [--sql-dialect DIALECT]
      [--scope project|organization|targeted] [--target-project PROJECT ...]
    Create a new semantic-layer model.

  kbagent semantic-layer model delete [--project PROJECT] --model MODEL
      [--yes]
    Delete a semantic-layer model and cascade-delete its children.

  kbagent semantic-layer model list [--project PROJECT]
    List all semantic-layer models in a project.

  kbagent semantic-layer promote --from-project PROJECT --to-project PROJECT
      [--from-model MODEL] [--to-model MODEL] [--types TYPES ...] [--dry-run]
      [--yes] [--scope project|organization|targeted]
      [--target-project PROJECT ...]
    Promote a model from one project to another (NEW + overwrite CHANGED;
    never deletes).

  kbagent semantic-layer reference-data delete [--project PROJECT] --id ID
      [--yes]
    Delete a reference-data record by UUID (server-side soft-delete).

  kbagent semantic-layer reference-data get [--project PROJECT] [--id ID]
      [--dimension DIMENSION]
    Fetch one record (all members) by ``--id`` or by ``--dimension``.

  kbagent semantic-layer reference-data list [--project PROJECT]
      [--model MODEL]
    List reference-data records (dimension summaries; use ``get`` for
    members).

  kbagent semantic-layer reference-data set [--project PROJECT]
      [--model MODEL] --dimension DIMENSION --members-file FILE
      [--dataset-id ID] [--description DESCRIPTION]
    Create or replace a reference-data record (keyed by dimension).

  kbagent semantic-layer remove constraint [--project PROJECT] [--model MODEL]
      --name NAME [--yes]
    Remove a constraint.

  kbagent semantic-layer remove dataset [--project PROJECT] [--model MODEL]
      --name NAME [--yes]
    Remove a dataset.

  kbagent semantic-layer remove glossary [--project PROJECT] [--model MODEL]
      --term TERM [--yes]
    Remove a glossary term. No orphan-check (glossary is a leaf entity).

  kbagent semantic-layer remove metric [--project PROJECT] [--model MODEL]
      --name NAME [--yes]
    Remove a metric. Prints an orphan-warning when constraints reference it.

  kbagent semantic-layer remove relationship [--project PROJECT]
      [--model MODEL] --name NAME [--yes]
    Remove a relationship. No orphan-check (relationships are leaf entities).

  kbagent semantic-layer schema [--project PROJECT] [--type TYPE ...] [--all]
    Fetch the server-side JSON Schema of semantic object types.

  kbagent semantic-layer scope add [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary
      --context-id ID [--target-project PROJECT ...]
    Add target projects to a targeted-scope item (merges with the current
    grants).

  kbagent semantic-layer scope get [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary
      --context-id ID
    Show an item's current scope, target-project grants, and pending
    elevation.

  kbagent semantic-layer scope remove [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary
      --context-id ID [--target-project PROJECT ...]
    Remove target projects from a targeted-scope item (merges with the current
    grants).

  kbagent semantic-layer scope request-create [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary
      --context-id ID
    Flag a project-scoped item as awaiting an org-admin's step-up decision.

  kbagent semantic-layer scope request-delete [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary
      --context-id ID
    Withdraw a pending scope-elevation request. Idempotent no-op if none is
    pending.

  kbagent semantic-layer scope request-list [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary [--limit N]
      [--offset N]
    List items of --type awaiting an org-admin's elevation decision, across
    the org.

  kbagent semantic-layer scope set [--project PROJECT]
      --type model|dataset|metric|relationship|constraint|glossary
      --context-id ID [--scope organization] [--target-project PROJECT ...]
      [--clear] [--dry-run] [--yes]
    Write an item's scope: elevate to organization, or replace/clear its
    target projects.

  kbagent semantic-layer search-context [--project PROJECT]
      [--pattern PATTERN ...] [--type TYPE] [--limit N]
    Search semantic-layer entities across a project by name pattern.

  kbagent semantic-layer show [--project PROJECT] [--model MODEL]
      [--type TYPE]
    Show the entities in a semantic-layer model.

  kbagent semantic-layer token [--encrypt] [--project PROJECT]
      --component-id ID
    Encrypt the project's storage token for transformation `user_properties`.

  kbagent semantic-layer validate [--project PROJECT] [--model MODEL] [--deep]
    Validate a semantic-layer model.
""",
    "serve": """\
### serve

  kbagent serve [--host HOST] [--port N] [--reload] [--log-level LEVEL]
      [--cors-origin ORIGIN ...] [--config-dir DIR] [--ui] [--ui-dist DIST]
      [--no-banner]
    Launch the kbagent HTTP API server.
""",
    "sharing": """\
### sharing

Cross-project bucket sharing and linking.

  kbagent sharing edges [--project PROJECT ...] [--all-projects]
    Show cross-project data flow edges via bucket sharing.

  kbagent sharing link [--project PROJECT] --source-project-id N
      --bucket-id ID [--name NAME] [--stage STAGE]
    Link a shared bucket into a project.

  kbagent sharing list [--project PROJECT ...] [--all-projects]
    List shared buckets available for linking.

  kbagent sharing share [--project PROJECT] --bucket-id ID --type TYPE
      [--target-project-ids IDS ...] [--target-users USERS ...]
    Enable sharing on a bucket.

  kbagent sharing unlink [--project PROJECT] --bucket-id ID
    Remove a linked bucket from a project.

  kbagent sharing unshare [--project PROJECT] --bucket-id ID
    Disable sharing on a bucket.
""",
    "storage": """\
### storage

Browse and manage storage buckets, tables, and files

  kbagent storage add-column [--project PROJECT] --table-id ID --column COLUMN
      [--not-null] [--default DEFAULT] [--branch N]
    Add a single column to an existing table (synchronous, typed).

  kbagent storage bucket-detail [--project PROJECT] --bucket-id ID
      [--branch N]
    Show detailed bucket info including backend-native direct access paths.

  kbagent storage buckets [--project PROJECT ...] [--all-projects]
      [--branch N]
    List storage buckets with sharing/linked bucket information.

  kbagent storage clone-table [--project PROJECT] --table-id ID [--branch N]
      [--dry-run]
    Clone (pull) a production table into a development branch.

  kbagent storage create-bucket [--project PROJECT] --stage STAGE --name NAME
      [--description DESCRIPTION] [--backend BACKEND] [--branch N]
    Create a new storage bucket.

  kbagent storage create-table [--project PROJECT] --bucket-id ID --name NAME
      [--column COLUMN ...] [--primary-key KEY ...] [--not-null NULL ...]
      [--default DEFAULT ...] [--source-table-id ID] [--source-branch-id N]
      [--time-partitioning-type TYPE] [--time-partitioning-field FIELD]
      [--time-partitioning-expiration-ms MS]
      [--range-partitioning-field FIELD] [--range-partitioning-start START]
      [--range-partitioning-end END] [--range-partitioning-interval INTERVAL]
      [--clustering-field FIELD ...] [--branch N] [--if-not-exists]
      [--timeout N]
    Create a new storage table with typed columns.

  kbagent storage delete-bucket [--project PROJECT] --bucket-id ID ...
      [--force] [--dry-run] [--yes] [--branch N]
    Delete one or more storage buckets.

  kbagent storage delete-column [--project PROJECT] --table-id ID
      --column COLUMN ... [--force] [--dry-run] [--yes] [--branch N]
    Delete one or more columns from a storage table.

  kbagent storage delete-table [--project PROJECT] --table-id ID ... [--force]
      [--dry-run] [--yes] [--branch N]
    Delete one or more storage tables.

  kbagent storage describe-batch [--project PROJECT] --from-file PATH
      [--branch N]
    Apply descriptions to buckets, tables, and columns from a YAML file.

  kbagent storage describe-bucket [--project PROJECT] --bucket-id ID
      [--text TEXT] [--file PATH] [--stdin] [--branch N]
    Set the description on a storage bucket.

  kbagent storage describe-column [--project PROJECT] --table-id ID
      --column COLUMN ... [--branch N]
    Set descriptions on one or more columns of a storage table.

  kbagent storage describe-migrate [--project PROJECT] [--table-id ID ...]
      [--bucket-id ID] [--prune-orphans] [--dry-run] [--yes] [--branch N]
    Convert legacy KBC.column.* descriptions to the native definition
    endpoint.

  kbagent storage describe-table [--project PROJECT] --table-id ID
      [--text TEXT] [--file PATH] [--stdin] [--branch N]
    Set the description on a storage table.

  kbagent storage download-table [--project PROJECT] --table-id ID
      [--output OUTPUT] [--columns COLUMNS ...] [--limit N] [--branch N]
      [--keep-slices] [--where-column COLUMN] [--where-operator OPERATOR]
      [--where-value VALUE ...] [--changed-since SINCE]
      [--changed-until UNTIL] [--progress]
    Export a storage table to a local CSV file.

  kbagent storage file-delete [--project PROJECT] --file-id N ... [--dry-run]
      [--yes]
    Delete one or more Storage Files.

  kbagent storage file-detail [--project PROJECT] --file-id N
    Show Storage File metadata (without downloading).

  kbagent storage file-download [--project PROJECT] [--file-id N]
      [--tag TAG ...] [--output OUTPUT] [--progress]
    Download a Storage File to local disk.

  kbagent storage file-tag [--project PROJECT] --file-id N [--add ADD ...]
      [--remove REMOVE ...]
    Add and/or remove tags on a Storage File.

  kbagent storage file-upload [--project PROJECT] --file FILE [--name NAME]
      [--tag TAG ...] [--permanent] [--branch N] [--progress]
    Upload a local file to Storage Files.

  kbagent storage files [--project PROJECT] [--tag TAG ...] [--limit N]
      [--offset N] [--query QUERY] [--branch N]
    List Storage Files with optional tag filtering.

  kbagent storage job-detail [--project PROJECT] --job-id N [--wait]
      [--timeout N]
    Show a Storage job -- status, table, rows imported, timing, error.

  kbagent storage load-file [--project PROJECT] --file-id N --table-id ID
      [--incremental] [--delimiter DELIMITER] [--enclosure ENCLOSURE]
      [--branch N] [--wait/--no-wait] [--timeout N]
    Load a Storage File into a table.

  kbagent storage snapshot-create [--project PROJECT] --table-id ID
      [--description DESCRIPTION] [--branch N]
    Create a snapshot of a table (data + columns + primary key).

  kbagent storage snapshot-delete [--project PROJECT] --snapshot-id ID ...
      [--dry-run] [--yes]
    Delete one or more table snapshots (the source tables are untouched).

  kbagent storage snapshot-detail [--project PROJECT] --snapshot-id ID
    Show one snapshot's detail (source table, creation time, description).

  kbagent storage snapshots [--project PROJECT] --table-id ID [--limit N]
      [--branch N]
    List snapshots of a table.

  kbagent storage swap-tables [--project PROJECT] --table-id ID
      --target-table-id ID [--branch N] [--dry-run] [--yes] [--timeout N]
    Swap two storage tables (any branch, including the default/production
    branch).

  kbagent storage table-detail [--project PROJECT] --table-id ID [--branch N]
    Show detailed table info including columns, types and physical layout.

  kbagent storage table-from-snapshot [--project PROJECT] --snapshot-id ID
      --bucket-id ID --name NAME [--branch N] [--dry-run]
    Create a NEW table from an existing snapshot (snapshot restore).

  kbagent storage tables [--project PROJECT ...] [--all-projects]
      [--bucket-id ID] [--branch N] [--include-usage]
    List storage tables from one or more projects.

  kbagent storage truncate-table [--project PROJECT] --table-id ID ...
      [--dry-run] [--yes] [--branch N]
    Truncate (delete all rows from) one or more storage tables.

  kbagent storage unload-table [--project PROJECT] --table-id ID
      [--columns COLUMNS ...] [--limit N] [--tag TAG ...] [--download]
      [--output OUTPUT] [--branch N] [--file-type TYPE] [--keep-slices]
      [--progress]
    Export a table to a Storage File.

  kbagent storage upload-table [--project PROJECT] --table-id ID --file FILE
      [--incremental] [--delimiter DELIMITER] [--enclosure ENCLOSURE]
      [--auto-create/--no-auto-create] [--branch N] [--wait/--no-wait]
      [--timeout N] [--progress]
    Upload a CSV (or gzipped CSV) file into a storage table.
""",
    "stream": """\
### stream

Data Streams (OTLP) source management

  kbagent stream create-source [--project PROJECT] --name NAME
      [--type otlp|http] [--branch BRANCH] [--if-not-exists] [--no-sinks]
      [--reveal]
    Create an OTLP (or HTTP) source and return its endpoint.

  kbagent stream delete SOURCE_ID [--project PROJECT] [--branch BRANCH]
      [--dry-run] [--yes]
    Delete a Data Streams source (destructive).

  kbagent stream detail [SOURCE_ID] [--project PROJECT] [--name NAME]
      [--branch BRANCH] [--reveal]
    Show a source's endpoints, protocol, and destination tables.

  kbagent stream list [--project PROJECT] [--branch BRANCH]
    List Data Streams sources in a project.
""",
    "sync": """\
### sync

Sync project configurations with local filesystem

  kbagent sync branch-link [--project PROJECT] [--directory PATH]
      [--branch-id N] [--branch-name NAME]
    Link the current git branch to a Keboola development branch.

  kbagent sync branch-status [--directory PATH]
    Show the branch mapping status for the current git branch.

  kbagent sync branch-unlink [--directory PATH]
    Remove the branch mapping for the current git branch.

  kbagent sync clone --source PATH --target TARGET --target-dir PATH
      [--bucket-map PATH] [--variable-values PATH] [--instance-rename PATH]
      [--dry-run] [--branch N] [--create-buckets/--no-create-buckets]
    Clone a reference project into a fresh target, parameterised by overrides.

  kbagent sync diff [--project PROJECT] [--all-projects] [--directory PATH]
      [--branch N]
    Show detailed diff between local and remote configurations.

  kbagent sync init [--project PROJECT] [--directory PATH] [--git-branching]
      [--adopt-existing] [--with-workspaces]
    Initialize a sync working directory for a Keboola project.

  kbagent sync pull [--project PROJECT] [--all-projects] [--directory PATH]
      [--force] [--theirs] [--dry-run] [--job-limit N] [--no-storage]
      [--no-jobs] [--with-samples] [--sample-limit N] [--max-samples N]
      [--branch N]
    Download configurations from a Keboola project to local files.

  kbagent sync push [--project PROJECT] [--all-projects] [--directory PATH]
      [--dry-run] [--force] [--allow-plaintext-on-encrypt-failure]
      [--branch N] [--no-name-drift-warnings]
    Push local configuration changes to a Keboola project.

  kbagent sync status [--directory PATH]
    Show which local configurations have been modified, added, or deleted.
""",
    "token": """\
### token

Storage API token management (scoped mint / revoke / rotate)

  kbagent token create [--project PROJECT] --description DESCRIPTION
      [--bucket-write WRITE ...] [--bucket-read READ ...]
      [--component-access ACCESS ...] [--can-read-all-file-uploads]
      [--expires-in N]
    Mint a scoped Storage API token (secret shown once).

  kbagent token delete [--project PROJECT] --token-id ID [--yes]
    Revoke a Storage API token immediately (destructive; only non-master
    tokens).

  kbagent token list [--project PROJECT] [--with-last-used]
      [--columns COLUMNS ...]
    List the project's Storage API tokens (no secrets -- those are mint-only).

  kbagent token refresh [--project PROJECT] --token-id ID [--yes]
    Rotate a token: generate a new value and invalidate the old one (secret
    shown once).
""",
    "transformation": """\
### transformation

SQL transformations - create, inspect, and edit blocks/codes

  kbagent transformation create [--project PROJECT] --name NAME [--sql SQL]
      [--sql-file PATH] [--created-table TABLE ...] [--component-id ID]
      [--description DESCRIPTION] [--branch N] [--dry-run]
    Create a SQL transformation from a SQL script.

  kbagent transformation edit [--project PROJECT] --config-id ID
      [--component-id ID] [--branch N] --change-description DESCRIPTION
      [--op OP ...] [--op-file PATH] [--storage STORAGE] [--dry-run]
    Edit a SQL transformation's blocks/codes with positional operations.

  kbagent transformation show [--project PROJECT] --config-id ID
      [--component-id ID] [--branch N]
    Show a SQL transformation's block/code tree with positional IDs.
""",
    "update": """\
### update

  kbagent update [--beta] [--major]
    Update kbagent to the latest version.
""",
    "version": """\
### version

  kbagent version [--beta]
    Show the kbagent version and check for updates.
""",
    "workspace": """\
### workspace

Workspace lifecycle for SQL debugging

  kbagent workspace create [--project PROJECT] [--name NAME]
      [--backend BACKEND] [--read-only/--no-read-only] [--ui]
    Create a new workspace.

  kbagent workspace delete [--project PROJECT] --workspace-id N [--dry-run]
      [--yes]
    Delete a workspace.

  kbagent workspace detail [--project PROJECT] --workspace-id N [--branch N]
    Show workspace details (password NOT included).

  kbagent workspace from-transformation [--project PROJECT] --component-id ID
      --config-id ID [--row-id ID] [--backend BACKEND]
    Create a workspace from a transformation config.

  kbagent workspace gc [--project PROJECT ...] [--all-projects] [--dry-run]
      [--yes]
    Garbage-collect orphaned workspaces.

  kbagent workspace list [--project PROJECT ...] [--all-projects] [--orphaned]
      [--branch N] [--qs-compatible]
    List workspaces of the active project, the given projects, or all
    projects.

  kbagent workspace load [--project PROJECT] --workspace-id N
      --tables TABLES ... [--preserve] [--load-type TYPE] [--force]
      [--timeout N]
    Load tables into a workspace.

  kbagent workspace password [--project PROJECT] --workspace-id N
    Reset workspace password and show the new one.

  kbagent workspace query [--project PROJECT] --workspace-id N [--sql SQL]
      [--file PATH] [--transactional] [--full] [--limit N]
    Execute SQL query in a workspace via Query Service.
""",
}
