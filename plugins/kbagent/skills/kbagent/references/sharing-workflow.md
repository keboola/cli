# Bucket Sharing Workflow

## Sharing types

| Type | Who can link | Use case |
|------|-------------|----------|
| `organization` | All members of the organization | Org-wide reference data |
| `organization-project` | Members of any project in the org | Project-scoped shared data |
| `selected-projects` | Only specified project IDs | Controlled sharing between specific projects |
| `selected-users` | Only specified user emails | Person-level access control |

## Token requirements

| Command | Token needed | How to provide |
|---------|-------------|----------------|
| `sharing list` | Regular project token | Already in config |
| `sharing link` | Regular project token | Already in config |
| `sharing unlink` | Regular project token | Already in config |
| `sharing share` | **Master token** (org membership) | `KBC_MASTER_TOKEN_{ALIAS}` or `KBC_MASTER_TOKEN` env var |
| `sharing unshare` | **Master token** (org membership) | Same as above |

Master token env var resolution order:
1. `KBC_MASTER_TOKEN_{ALIAS}` -- project-specific (alias uppercased, hyphens to underscores)
2. `KBC_MASTER_TOKEN` -- global fallback
3. Project's configured token -- may lack permissions (403)

Example: for project alias `padak-2-0`, set `KBC_MASTER_TOKEN_PADAK_2_0`.

## Typical workflow: share data from project A to project B

```bash
# Step 1: Share a bucket in project A (needs master token)
KBC_MASTER_TOKEN_PROJ_A="..." kbagent --json sharing share \
  --project proj-a --bucket-id out.c-shared-data --type organization-project

# Step 2: Discover shared buckets from project B
kbagent --json sharing list --project proj-b

# Step 3: Link the shared bucket into project B
kbagent --json sharing link \
  --project proj-b --source-project-id 123 --bucket-id out.c-shared-data

# The linked bucket appears as in.c-shared-shared-data (read-only)
kbagent --json storage buckets --project proj-b
```

## Unsharing workflow

```bash
# Must unlink from all consuming projects first
kbagent --json sharing unlink --project proj-b --bucket-id in.c-shared-shared-data

# Then disable sharing on the source
KBC_MASTER_TOKEN_PROJ_A="..." kbagent --json sharing unshare \
  --project proj-a --bucket-id out.c-shared-data
```

Unshare fails with "bucket is already linked in other projects" if any project
still has a linked bucket pointing to it.

## Share to specific projects

```bash
KBC_MASTER_TOKEN_PROJ_A="..." kbagent --json sharing share \
  --project proj-a --bucket-id out.c-data \
  --type selected-projects --target-project-ids 901,9621
```

## Share to specific users

```bash
KBC_MASTER_TOKEN_PROJ_A="..." kbagent --json sharing share \
  --project proj-a --bucket-id out.c-data \
  --type selected-users --target-users alice@example.com,bob@example.com
```

## Key behaviors

- Linked buckets are **read-only** in the consuming project
- Linked bucket name defaults to `shared-{bucket_name}` (e.g. `out.c-data` -> `in.c-shared-data`)
- Use `--name` to override the linked bucket name
- `sharing list` deduplicates across projects (same bucket listed once even if visible from multiple projects)
- All share/unshare operations are **async** -- CLI waits for completion automatically
