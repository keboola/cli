# Write Guard + Single-Config Sync: Implementation Plan

Epic: [#63](https://github.com/padak/keboola_agent_cli/issues/63)
Issues: [#59](https://github.com/padak/keboola_agent_cli/issues/59) (single-config sync), [#60](https://github.com/padak/keboola_agent_cli/issues/60) (branch protection)

## Why these must be built together

Issue #59 adds new write paths (`config push`). Issue #60 gates all write paths.
Building them separately creates either an unprotected window or retrofit cost.

```
ALL WRITE PATHS:
  tool call (create_*, update_*, delete_*)     ← existing, needs guard
  sync push                                     ← existing, needs guard
  config update / config delete                 ← existing, needs guard
  config push (NEW from #59)                    ← new, born with guard
  branch delete / branch merge                  ← existing, needs guard
  workspace load / workspace query              ← existing, evaluate if guard needed
```

## Architecture: WriteGuard

Central Policy Enforcement Point that ALL write operations call before executing.

### Placement in 3-layer architecture

```
Commands (CLI)  →  Services (business logic)  →  Clients (HTTP)
                       ↑
                   WriteGuard.check()
                   called by each service
                   before mutating operations
```

WriteGuard is a service (`services/write_guard.py`), injected via DI into services
that perform mutations. NOT a decorator, NOT middleware -- explicit `check()` calls.

### Decision logic

```python
class WriteGuard:
    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store

    def check(self, alias: str, *, branch_id: int | None, operation: str) -> None:
        """Raise ProtectionError if write to main is blocked.

        Rules (in order):
        1. branch_id is not None → ALLOWED (dev branch, always safe)
        2. Project not protected  → ALLOWED
        3. Protected + main       → BLOCKED (prompt passphrase via /dev/tty)
        """
        if branch_id is not None:
            return  # writing to dev branch

        protection = self._load_protection(alias)
        if protection is None or not protection.enabled:
            return  # not protected

        # Protected project, writing to main → challenge
        self._challenge_passphrase(alias, protection, operation)

    def _challenge_passphrase(self, alias, protection, operation):
        """Read passphrase from /dev/tty (not stdin). Agent can't pipe to this."""
        import getpass
        try:
            tty = open("/dev/tty", "r")
            passphrase = getpass.getpass(
                f"Write to main blocked. Enter passphrase for '{alias}': ",
                stream=tty,
            )
        except OSError:
            raise ProtectionError(
                f"Write to main branch blocked for project '{alias}'. "
                f"Operation: {operation}. No TTY available for passphrase."
            )
        if not bcrypt.checkpw(passphrase.encode(), protection.passphrase_hash.encode()):
            raise ProtectionError("Invalid passphrase.")
```

### DI wiring (in cli.py)

```python
write_guard = WriteGuard(config_store=config_store)

sync_service = SyncService(config_store=config_store, write_guard=write_guard, ...)
mcp_service = McpService(config_store=config_store, write_guard=write_guard, ...)
config_service = ConfigService(config_store=config_store, write_guard=write_guard, ...)
branch_service = BranchService(config_store=config_store, write_guard=write_guard, ...)
```

### Enforcement points (exhaustive list)

| Write path | Service | Method | Guard call |
|------------|---------|--------|------------|
| MCP write tool | `mcp_service.py` | `validate_and_call_tool()` | Before dispatch |
| Sync push | `sync_service.py` | `push()` | Before API calls |
| Config update | `config_service.py` | `update_config()` | Before `client.update_config()` |
| Config delete | `config_service.py` | `delete_config()` | Before `client.delete_config()` |
| Config push (NEW) | `config_sync_service.py` | `push_single_config()` | Before create/update |
| Branch merge | `branch_service.py` | `merge_branch()` | Before merge |
| Branch delete | `branch_service.py` | `delete_branch()` | Before delete |
| Workspace load | `workspace_service.py` | `load_tables()` | Evaluate: writes to workspace, not project |

## Protection storage (simplified from original proposal)

### Why NOT HMAC

Original proposal used HMAC(enabled + hash + salt, key=passphrase) to detect tampering.
Multi-model review (Codex, Claude Code) identified this as over-engineering:

- Agent can't reverse bcrypt hash → can't create valid new hash
- Agent can't know passphrase → can't create valid HMAC either
- HMAC only protects against deletion of protection block → simpler solutions exist
- HMAC complicates key rotation (must recompute on passphrase change)
- Separate salt field redundant (bcrypt already contains salt)

### Simplified scheme: bcrypt + marker file

```json
// In config.json, per project:
{
  "projects": {
    "production": {
      "url": "https://connection.keboola.com",
      "token": "...",
      "protection": {
        "enabled": true,
        "passphrase_hash": "$2b$12$..."
      }
    }
  }
}
```

```
~/.config/keboola-agent-cli/
  config.json                    # Contains protection block
  .protection-markers/
    production.enabled           # Empty file, indicates protection was ever enabled
```

**Fail-closed logic:**

```python
def _load_protection(self, alias):
    project = self._config_store.get_project(alias)
    protection = project.protection if project else None

    if protection is None and self._marker_exists(alias):
        # Protection block deleted but marker exists → tampering
        raise ProtectionError(
            f"Protection data missing for '{alias}'. "
            "This may indicate tampering. Run 'kbagent protect enable' to restore."
        )
    return protection
```

Agent would need to delete BOTH config.json block AND marker file. This is
detection-equivalent to HMAC but trivially simple to implement.

### Why the agent can't bypass

| Attack vector | Why it fails |
|---|---|
| Read config.json | Only sees bcrypt hash, can't reverse |
| Delete protection block | Marker file detects deletion → fail-closed |
| Delete marker file + protection block | Possible, but agent must know about marker files AND their location |
| Set env var | No env var bypass exists |
| Use --force flag | Triggers /dev/tty passphrase prompt |
| Pipe passphrase via stdin | /dev/tty reads from terminal, not stdin |
| Call Keboola API directly | Out of scope -- WriteGuard is a guardrail, not a jail (see Limitations) |

### Limitations (document explicitly)

WriteGuard is **defense-in-depth for UX**, not cryptographic enforcement.
An agent with shell access and a Storage API token can always call the Keboola API
directly, bypassing the CLI entirely. This is inherent to any client-side protection.

Long-term solution: **branch-scoped Storage API tokens** enforced server-side by Keboola.
WriteGuard is the best stopgap until that exists.

## Branch Resolution: Unified Function

Currently duplicated in 3 places:
- `commands/tool.py` `_resolve_branch()` (lines 37-95)
- `services/sync_service.py` `_resolve_branch_id()` (lines 1531-1570)
- Various ad-hoc logic in commands

### New: `resolve_effective_branch()`

```python
# In services/branch_resolver.py or services/base.py

def resolve_effective_branch(
    *,
    explicit_branch: int | None = None,     # --branch flag
    project: ProjectConfig | None = None,   # active_branch_id
    project_root: Path | None = None,       # for git-branching lookup
    manifest: Manifest | None = None,       # for sync workspace context
) -> int | None:
    """Resolve branch ID with clear priority chain.

    Priority:
    1. Explicit --branch flag (always wins)
    2. Git-branching: branch-mapping.json for current git branch
    3. active_branch_id from config store (set by 'branch use')
    4. None = production/main

    Raises ConfigError if git-branching enabled but branch not linked.
    """
    # 1. Explicit flag
    if explicit_branch is not None:
        return explicit_branch

    # 2. Git-branching (if in sync workspace)
    if manifest and manifest.git_branching.enabled and project_root:
        git_branch = get_current_branch(project_root)
        if git_branch:
            mapping = load_branch_mapping(project_root)
            entry = mapping.get(git_branch)
            if entry is not None:
                return entry.keboola_id  # None for production
            raise ConfigError(
                f"Git branch '{git_branch}' is not linked to a Keboola branch. "
                "Run 'kbagent sync branch-link' first."
            )

    # 3. Active branch from config
    if project and project.active_branch_id:
        return project.active_branch_id

    # 4. Production (None)
    return None
```

All call sites (tool.py, sync_service.py, config commands, new config pull/push)
use this single function.

## Single-Config Pull/Push Design

### New service: `services/config_sync_service.py`

Separate from `sync_service.py` (2000 lines). Different responsibility:
single-config operations vs full-project sync.

### CLI commands (added to existing `commands/config.py`)

```bash
# Pull single config to directory
kbagent config pull --project my-proj \
  --component-id keboola.python-transformation-v2 \
  --config-id 12345 \
  --output-dir ./my-config/

# Push single config from directory
kbagent config push --project my-proj \
  --component-id keboola.python-transformation-v2 \
  --config-id 12345 \
  --input-dir ./my-config/

# Push with auto-create (new config)
kbagent config push --project my-proj \
  --component-id keboola.python-transformation-v2 \
  --input-dir ./my-config/ \
  --create

# Both support --branch and --dry-run
kbagent config pull ... --branch 456
kbagent config push ... --branch 456 --dry-run
```

### Two operating modes

#### Mode A: Inside sync workspace (manifest detected)

When `.keboola/manifest.json` exists in current or parent directory:

- Branch resolution: uses `resolve_effective_branch()` including git-branching
- Manifest interaction: upserts config entry after pull/push (stays consistent with full sync)
- Safety: blocked if git branch not linked (same as `sync pull/push`)

#### Mode B: Standalone (no workspace)

When no manifest found:

- Branch resolution: `--branch` flag only. **Writes without `--branch` are REFUSED** (not defaulting to production)
- Reads (pull) without `--branch`: default to production (safe -- read-only)
- No manifest interaction
- Metadata sidecar (`.kbagent/pull_meta.json`) for roundtrip, but all values overridable via CLI flags

```
WRITE safety matrix:
                     | Inside workspace    | Standalone
  --branch provided  | Use --branch        | Use --branch
  git-branching      | Auto from mapping   | N/A
  active branch      | Use active          | Use active
  none of above      | Production (guarded)| REFUSED (must specify --branch or --allow-main)
```

### Pull flow

```
1. Resolve branch_id (via resolve_effective_branch)
2. Fetch: client.get_config_detail(component_id, config_id, branch_id)
3. Fetch rows: client.list_config_rows(component_id, config_id, branch_id)
4. Convert: api_config_to_local(component_id, config_data, config_id)
5. Extract code: extract_code_files(component_id, local_data, output_dir)
6. Write _config.yml + code files + _description.md
7. Handle rows → rows/{sanitized_name}/_config.yml
8. Write .kbagent/pull_meta.json (sidecar)
9. If in workspace: upsert manifest entry with hashes
```

### Push flow

```
1. Resolve branch_id
2. WriteGuard.check(alias, branch_id=branch_id, operation="config push")
3. Read _config.yml from input_dir
4. Merge code files: merge_code_files(component_id, local_data, input_dir)
5. Convert: local_config_to_api(local_data) → (name, description, configuration)
6. Encrypt: _encrypt_secrets_in_config(client, project_id, component_id, configuration)
7. Detect remote changes (if .kbagent/pull_meta.json exists):
   - Fetch remote config, compute hash, compare with pull_meta.config_hash
   - If remote changed: warn or fail (unless --force)
8. Create or update:
   - --create flag: client.create_config(...)
   - Existing: client.update_config(...)
9. Handle rows: create/update/delete based on local vs remote
10. Writeback encrypted values to local _config.yml
11. Update .kbagent/pull_meta.json
12. If in workspace: update manifest entry
```

### Output directory structure

```
my-config/
├── _config.yml          # Config: parameters, storage, processors
├── _description.md      # Description (extracted)
├── transform.sql        # SQL code (if transformation)
├── .kbagent/
│   └── pull_meta.json   # Roundtrip metadata (gitignored)
└── rows/
    ├── my-row-1/
    │   └── _config.yml
    └── my-row-2/
        └── _config.yml
```

### `.kbagent/pull_meta.json` schema

```json
{
  "version": 1,
  "project_alias": "my-proj",
  "stack_url": "https://connection.keboola.com",
  "project_id": 12345,
  "component_id": "keboola.python-transformation-v2",
  "config_id": "67890",
  "branch_id": null,
  "config_hash": "sha256...",
  "row_ids": {
    "my-row-1": "row-id-1",
    "my-row-2": "row-id-2"
  },
  "pulled_at": "2026-03-26T15:00:00Z"
}
```

All values overridable via CLI flags. If sidecar missing, flags are required for push.

## Phase 3: Filter mode (`sync pull --config` / `sync push --config`)

Lightweight addition to existing sync commands.

```bash
kbagent sync pull --project my-proj --config keboola.python-transformation-v2/12345
kbagent sync push --project my-proj --config keboola.python-transformation-v2/12345
```

### Implementation

**Pull with filter:**
- Instead of `client.list_components_with_configs()` (bulk), call `client.get_config_detail()` (single)
- Process single config through existing pipeline (format, extract, write)
- Partial manifest update: upsert only the target config entry, preserve all others

**Push with filter:**
- Run existing `diff()` → `compute_changeset()` (computes all changes)
- Filter changeset to only the target config
- Push filtered changeset through existing pipeline (encrypt, create/update, writeback)

## Phased delivery plan

### Phase 0: Write-surface inventory (1 day)

**Goal:** Map every write path, add contract tests ensuring guard is called.

Tasks:
- [ ] Audit all service methods that call mutating client methods
- [ ] Create `tests/test_write_guard_coverage.py` with contract tests:
  - Each write service method must accept `write_guard` parameter
  - Each write service method must call `write_guard.check()` before mutation
- [ ] Document the exhaustive write-path table (update this doc)

### Phase 1: WriteGuard + protection (2-3 days)

**Goal:** All existing write paths are guarded. Protection can be enabled/disabled.

Tasks:
- [ ] Create `services/write_guard.py` (WriteGuard class)
- [ ] Create `ProtectionError` in `errors.py`
- [ ] Add `protection` field to `ProjectConfig` model (optional)
- [ ] Marker file logic in config store
- [ ] CLI commands: `kbagent protect enable`, `kbagent protect disable`, `kbagent protect status`
- [ ] Inject WriteGuard into all mutating services via DI
- [ ] Add `write_guard.check()` calls to all enforcement points
- [ ] Create `resolve_effective_branch()` unified function
- [ ] Replace existing branch resolution duplicates with unified function
- [ ] Update `project status` to show protection state
- [ ] Update `doctor` to check protection integrity
- [ ] Tests: WriteGuard unit tests, integration tests per enforcement point
- [ ] Dependency: add `bcrypt` to pyproject.toml

### Phase 2: Single-config pull/push (4-5 days)

**Goal:** `config pull` and `config push` commands work in both standalone and workspace modes.

Tasks:
- [ ] Create `services/config_sync_service.py`
- [ ] `pull_single_config()` method (fetch, convert, extract, write, rows)
- [ ] `push_single_config()` method (read, merge, convert, encrypt, create/update, rows, writeback)
- [ ] Workspace detection (find manifest upward)
- [ ] Manifest upsert logic (partial update, not replace)
- [ ] `.kbagent/pull_meta.json` read/write
- [ ] CLI commands in `commands/config.py`: `config pull`, `config push`
- [ ] `--create` flag for new config creation
- [ ] `--allow-main` flag for standalone writes to production
- [ ] `--dry-run` support
- [ ] Row handling (CRUD based on local vs remote)
- [ ] WriteGuard integration (automatic from Phase 1)
- [ ] Tests: service tests, CLI tests, workspace-aware vs standalone tests
- [ ] Update plugin SKILL.md with new commands

### Phase 3: Filter mode (2 days)

**Goal:** `sync pull --config` and `sync push --config` work within sync workspace.

Tasks:
- [ ] Add `--config` option to `sync pull` and `sync push` commands
- [ ] Single-config fetch path in `SyncService.pull()` (get_config_detail instead of bulk)
- [ ] Changeset filtering in `SyncService.push()`
- [ ] Partial manifest update (upsert single entry)
- [ ] Tests: filtered pull/push, manifest consistency
- [ ] Update plugin SKILL.md

## Design review sources

This plan was validated by three independent AI models:

- **OpenAI Codex (gpt-5.3)**: Emphasized GuardedClient wrapper, Phase 0 inventory, audit logging, default-deny for standalone writes
- **Google Gemini**: Confirmed architectural soundness, suggested protection in manifest, recommended WriteGuard injection into BaseService
- **Anthropic Claude Code (opus-4-6)**: Identified HMAC as over-engineering, proposed bcrypt + marker file simplification, emphasized WriteGuard as guardrail not jail, recommended unified branch resolution

Key consensus: WriteGuard is the right pattern. Simplify crypto. Protect first, add features second.
