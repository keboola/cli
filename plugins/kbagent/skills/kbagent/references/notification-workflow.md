# Notification workflow: who gets alerted, and setting up alerts

Notification subscriptions are the recipients behind the Flow Builder's
Notifications tab (bell icon). They live in the Notification Service, not in
the flow's configuration, so `flow detail` and `config detail` never show
them. The in-flow `type: "notification"` task is a different mechanism.

Read commands work since 0.86.0; `create`, `delete` and `replace-recipient`
since 0.91.0; `--overtime-percent`, the `overtime_percent` key, `--filter` and
`clone` since vNEXT (check `kbagent version` before you recommend them).

## 1. Audit: who gets alerted for this flow

```bash
kbagent --json notification list --project P --config-id FLOW_ID
kbagent --json notification list --project P            # unfiltered
```

- Run both. `--config-id` / `--component-id` drop every subscription without
  a config filter and only count them in `project_wide_excluded`. Those rows
  can still alert for this flow, but `scope: project-wide` only means "no
  config filter": read each row's `filters` (component, branch, phase,
  overtime) before adding it to the answer.
- Check `errors` before you conclude that nobody is subscribed. A blank
  `config_name` does not mean the subscription is inactive.
- `branch_id` is filled on production subscriptions too (the default branch's
  id). Compare it with `kbagent branch list` before calling an alert
  "dev-branch only".
- `overtime_percent` is the "longer than usual" threshold of a
  `job-processing-long` / `phase-job-processing-long` row. `null` does not
  prove there is no threshold: an `==` filter, two overtime filters or an
  unreadable value are `null` too. Read the raw `filters`
  (`notification detail --project P --subscription-id ID` prints them all).

## 2. Create an alert

Failure alert for one flow:

```bash
kbagent --json notification create --project P --event job-failed \
  --channel email --address team@example.com \
  --component-id keboola.flow --config-id FLOW_ID
```

Slow-job alert, as the UI's "job runs longer than usual" (vNEXT+):

```bash
kbagent --json notification create --project P --event job-processing-long \
  --channel email --address team@example.com \
  --component-id keboola.flow --config-id FLOW_ID --overtime-percent 50
```

- Always pass `--overtime-percent` with `job-processing-long` /
  `phase-job-processing-long`. Without it the subscription has no threshold
  and fires at every overtime step the platform reports (each new whole
  percent up to 500 %, then every minute): a flood of mail.
- The value is a whole percent, 1 to 10000 (`50` = at least 50 % longer than
  usual). Never pass the ratio you see in `filters` (`0.5`): it exits 2.
  `0` exits 2 too; the service refuses it.
- On any other event the option exits 2 (`INVALID_ARGUMENT`) and creates
  nothing, because no other event carries the overtime value.
- On kbagent older than vNEXT the option does not exist (`No such option`).
  There is no CLI workaround; tell the user to set the threshold in the
  Flow Builder, or to upgrade.
- `--channel webhook` takes the URL in `--address`.
- Without `--branch` the service stores the default branch's id, so the
  alert fires for production jobs only. Pass `--branch ID` for a dev branch.
- `create` is not idempotent: every call adds a subscription. Run step 1
  first and do not create a second identical one.

Any other filter the service accepts for the event (vNEXT+), e.g. only for
jobs whose usual run time is at least a minute:

```bash
kbagent --json notification create --project P --event job-processing-long \
  --channel email --address team@example.com \
  --config-id FLOW_ID --overtime-percent 50 --filter 'averageDuration>=60'
```

- Form `FIELD<OP>VALUE`, OP one of `==`, `=`, `<=`, `>=`, `<`, `>`.
  Repeat `--filter` for more; all filters must match.
- Always single-quote it: `<` and `>` are shell redirections.
- VALUE is sent as a string; numeric fields compare it as a number.
- kbagent checks only the syntax. The service refuses a field it does not
  know for the event: report its error, do not guess another field name.
- Do not set one field twice: `--config-id` with
  `--filter 'job.configuration.id=...'`, or `--overtime-percent` with
  `--filter 'durationOvertimePercentage...'`, exits 2. An upper bound alone
  (`--filter 'durationOvertimePercentage<5'`) is fine.

Verify with `notification detail --project P --subscription-id ID` (the `overtime_percent`
and the raw filters must match what you asked for).

## 3. Change the recipient

```bash
kbagent --json notification replace-recipient --project P \
  --subscription-id ID --address new@example.com --yes
```

- The service has no update, so this creates a new subscription and then
  deletes the old one. The id always changes: use `new_subscription_id` from
  the result, never the old id.
- Ask the user before you run it; `--yes` stands for their confirmation.
- It exits 0 even when the old one could not be deleted: read
  `old_deleted` in the result. `false` means a duplicate now exists. Do not
  run the replace again; delete the old one with
  `notification delete --project P --subscription-id OLD_ID --yes`.
- The channel is kept unless you pass `--channel`. To move from email to a
  webhook, pass `--channel webhook --address URL`.
- Filters, the overtime threshold included, are copied unchanged. To change
  the threshold, `clone` it with `--overtime-percent N` (section 4) and delete
  the old one.

## 4. Clone an alert onto another flow

Since vNEXT.

```bash
kbagent --json notification clone --project P --subscription-id ID \
  --config-id OTHER_FLOW_ID --dry-run
kbagent --json notification clone --project P --subscription-id ID \
  --config-id OTHER_FLOW_ID
```

- Run `--dry-run` first and show the user `planned`, `filters_replaced` and
  `warnings`. Ask before the real run: every run creates one more
  subscription, also a run with no override (that duplicates the alert).
- Overrides: `--component-id`, `--config-id`, `--branch`,
  `--overtime-percent`, `--filter`, `--address` (`--channel`), `--expires-at`
  or `--clear-expires-at`. The event and the project never change.
- Each override replaces EVERY source filter on its field (a two-bound range
  is replaced as a whole); all other filters are copied verbatim, unknown
  ones included. There is no option to remove a filter: create a new
  subscription instead.
- The branch is kept as the source has it. `branch use` does not change it;
  pass `--branch ID` to move the alert to another branch.
- A `phase.id` filter is copied too, and the result warns: that phase id
  belongs to the old flow. Pass `--filter 'phase.id=NEW_PHASE'` or create a
  new subscription.
- `--address` alone keeps the source channel. The source expiry is kept,
  even when it is in the past: pass `--expires-at` or `--clear-expires-at`.
- After an error that says the subscription may have been created, run
  `notification list` before you run `clone` again.
- Use `new_subscription_id` from the result. The source is not changed;
  delete it separately if the user wants it moved, not copied.

## 5. Delete

```bash
kbagent --json notification delete --project P --subscription-id ID --yes
```

`delete` is destructive-class: `--deny-destructive` blocks it.
`create`, `replace-recipient` and `clone` are write-class. `delete` and
`replace-recipient` ask for confirmation on a terminal; without a terminal
they refuse with `CONFIRMATION_REQUIRED` (exit 2). An agent has no terminal
to answer the prompt, so ask the user first (name the subscription id, event
and recipient) and pass `--yes` only after they confirm. `--json` skips the
prompt too, so the same rule applies to every `--json` call above.

More detail: the notification sections of [gotchas.md](gotchas.md) and
[commands-reference.md](commands-reference.md).
