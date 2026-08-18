# Flow Notification audit workflow (`kbagent notification list`)

Answers the fleet-wide question **"who gets paged when a production flow
breaks, and are those recipients still valid?"** -- across every registered
project, in one command.

Available since **v0.84.2** (issue #600). Read-only.

## Why this command exists

Auditing flow notifications used to be only half-possible from a CLI. Three
of the four surfaces were already reachable:

| Surface | Where it lives | Reachable before v0.84.2 |
|---|---|---|
| Owner / contact emails in flow descriptions | flow `description` | yes (`flow list`, `config detail`) |
| In-flow notification **task** (`type: "notification"`) | the flow's `configuration` JSON | yes (`flow detail`) |
| Email-sending component configs (e.g. `kds-team.app-email-smtp-sender`) | component configs | yes (`config search`) |
| **Notifications tab** (bell icon: Success / Error / Processing-delay) | **Notification Service** | **no -- UI only** |

The last row is the one that actually pages a human when a production flow
fails, and it was the only one that required opening each flow in the web UI
by hand. On the 20-project / 276-flow fleet that prompted the issue, that was
the entire cost of the audit.

## The command

```bash
# Every subscription, every registered project
kbagent notification list

# One project, only failures
kbagent notification list --project prod --event job-failed

# Everything pointed at one specific flow
kbagent notification list --project prod --component-id keboola.flow --config-id 9001

# Machine-readable, for joining against your own inventory
kbagent --json notification list > subscriptions.json
```

Rows carry `project_alias`, `subscription_id`, `event`, `scope`,
`component_id`, `config_id`, `config_name`, `branch_id`, `channel`,
`address`, `expires_at`, and the raw `filters` list.

## Reading the output

- **`scope: "project-wide"`** -- no config filter: the subscription fires for
  every job in the project. The catch-all "tell me about any failure". Often
  the most important row; never noise.
- **`config_name` empty while `config_id` is set** -- the subscription points
  at a configuration that no longer exists. A dangling recipient, i.e. a
  finding.
- **`branch_id` set** -- the subscription is filtered to a dev branch.
  Production subscriptions carry no branch filter. The endpoint is not
  branch-scoped, so both come back together unless you pass `--branch`.
- **`channel: "webhook"`** -- `address` holds the webhook URL rather than an
  email address; the two share a column because both answer "where does this
  go".

## A complete audit

1. `kbagent --json notification list > tab.json` -- the Notifications tab.
2. `kbagent --json flow list` + `flow detail` -- the in-flow notification
   **tasks** (a different mechanism; see gotchas.md).
3. `kbagent config search --query "@"` -- addresses hiding in descriptions
   and in email-sender component configs.
4. Join all three against your directory of valid addresses. Typical
   findings: placeholder addresses that were never replaced, recipients who
   have left, flows with no owner at all, and subscriptions surviving the
   flow they watched.

## What this command will not do

- **It cannot add or remove a recipient.** The Notification Service has
  create/delete endpoints; kbagent deliberately wraps neither, at every layer
  (the HTTP dispatcher takes no method argument). Changing who gets paged
  stays a deliberate UI/API action.
- **It does not read the in-flow notification task.** That is `flow detail`.
- **It needs no elevated token.** The read path works with the same plain
  Storage token every other project command uses.
