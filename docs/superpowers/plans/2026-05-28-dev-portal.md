# `kbagent dev-portal` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `kbagent dev-portal` command group that wraps the Keboola Developer Portal API (`apps-api.keboola.com`) with multi-identity credential storage and a random-code TTY confirm safety bar that an AI agent cannot bypass.

**Architecture:** Standard kbagent 3-layer: `commands/dev_portal.py` (Typer) → `services/dev_portal_service.py` (business logic, diff, prepare/apply) → `dev_portal_client.py` (HTTP, login + MFA). Identities persisted in `AppConfig.dev_portal_identities` next to KB projects. Every write is gated by `require_random_code_confirmation()` (extracted from `commands/permissions.py` into `commands/_helpers.py` so the primitive has one home).

**Tech Stack:** Python 3.12, Typer, Pydantic 2.x, httpx (inherits `BaseHttpClient`), pytest + `pytest-httpx`, `typer.testing.CliRunner`.

**Source spec:** `docs/superpowers/specs/2026-05-28-dev-portal-design.md`

---

## File Structure

### New files

| Path | Responsibility |
|------|----------------|
| `src/keboola_agent_cli/dev_portal_client.py` | Layer 3 HTTP client. Login (token + MFA), bearer in-memory, list/get/create/patch/upload-icon (two-hop)/publish/deprecate. Inherits `BaseHttpClient`. |
| `src/keboola_agent_cli/services/dev_portal_service.py` | Layer 2 business logic. Identity CRUD, `prepare_*`/`apply` pattern with diffing, publish pre-flight validation, verify-on-add. |
| `src/keboola_agent_cli/commands/dev_portal.py` | Layer 1 Typer commands. Identity subcommands; `list`/`get`; writes with `--dry-run` and random-code confirm. |
| `tests/test_dev_portal_client.py` | `pytest-httpx` mocked tests for the client. |
| `tests/test_dev_portal_service.py` | Mocked-client tests for the service. |
| `tests/test_dev_portal_cli.py` | `CliRunner` tests for the command layer. |
| `plugins/kbagent/skills/kbagent/references/dev-portal-workflow.md` | Workflow doc for the kbagent skill. |

### Modified files

| Path | What changes |
|------|--------------|
| `src/keboola_agent_cli/errors.py` | Add 5 new `ErrorCode` entries (`DP_LOGIN_FAILED`, `DP_MFA_REQUIRED`, `DP_APP_NOT_FOUND`, `DP_PUBLISH_REQUIREMENTS_MISSING`, `DP_ICON_UPLOAD_FAILED`). |
| `src/keboola_agent_cli/commands/_helpers.py` | Extract `require_random_code_confirmation()` from `commands/permissions.py`; add `resolve_identity_alias()` and `get_dev_portal_service()` factories. |
| `src/keboola_agent_cli/commands/permissions.py` | Replace local `_require_interactive_confirmation` with import from `_helpers`. |
| `src/keboola_agent_cli/models.py` | Add `DeveloperPortalIdentity`; extend `AppConfig` with `dev_portal_identities` + `default_dev_portal_identity`. |
| `src/keboola_agent_cli/config_store.py` | Add 5 mirror methods; extend `CLAUDE_CONFIG_WARNING` to mention DP credentials. |
| `src/keboola_agent_cli/permissions.py` | Add 14 entries to `OPERATION_REGISTRY`. |
| `src/keboola_agent_cli/cli.py` | Register `dev_portal_app` Typer sub-app under panel "Development". |
| `src/keboola_agent_cli/commands/context.py` | Extend `AGENT_CONTEXT` with `dev-portal` section. |
| `src/keboola_agent_cli/changelog.py` | Add release entry. |
| `pyproject.toml` | Bump to next minor (e.g. `0.45.0`). |
| `tests/test_config_store.py` | Add tests for DP identity mirror methods. |
| `tests/test_permissions.py` | Add tests for the 14 new ops. |
| `tests/test_helpers.py` | Add tests for `require_random_code_confirmation()`. |
| `tests/test_e2e.py` | Add identity-list smoke + optional portal-list (gated on `E2E_DP_USERNAME`/`E2E_DP_PASSWORD`). |
| `CLAUDE.md` | Update `## All CLI Commands` section. |
| `plugins/kbagent/.claude-plugin/plugin.json` | Auto-synced via `make version-sync`. |
| `plugins/kbagent/skills/kbagent/SKILL.md` | Decision-table row for "manage portal property / register app". |
| `plugins/kbagent/skills/kbagent/references/commands-reference.md` | New section for `dev-portal`. |
| `plugins/kbagent/skills/kbagent/references/gotchas.md` | Entry tagged `(since v0.45.0)` for the no-bypass write rule. |
| `plugins/kbagent/agents/keboola-expert.md` | Rule 6 version-gate update, tool-selection matrix entry, inline gotcha. |

---

## Task 1: Branch off main + smoke check

**Files:**
- Modify: none yet (branch setup only)

- [ ] **Step 1: Confirm clean tree on main**

```bash
git status
git checkout main
git pull --ff-only origin main
```

Expected: clean working tree on `main` at latest commit.

- [ ] **Step 2: Create feature branch**

```bash
git checkout -b feat/dev-portal
```

Expected: switched to `feat/dev-portal`. (Do NOT continue on the existing `feat/job-run-mode-debug` branch — that ships an unrelated change.)

- [ ] **Step 3: Verify dev install works**

```bash
uv pip install -e ".[dev]"
uv run pytest tests/test_cli.py -q
```

Expected: all tests pass. Establishes baseline so later failures are clearly caused by this work.

- [ ] **Step 4: Commit branch readme touch (optional, skip if your team policy is "no empty commits")**

Skipped — no commit, the branch is created lazily.

---

## Task 2: Add new ErrorCode entries

**Files:**
- Modify: `src/keboola_agent_cli/errors.py`
- Test: `tests/test_errors.py` (extend; check if file already covers enum membership — if not, the unit test in step 1 lives in `tests/test_dev_portal_client.py` later)

- [ ] **Step 1: Write failing test for enum membership**

Append to `tests/test_errors.py`:

```python
from keboola_agent_cli.errors import ErrorCode


def test_dev_portal_error_codes_present():
    assert ErrorCode.DP_LOGIN_FAILED == "DP_LOGIN_FAILED"
    assert ErrorCode.DP_MFA_REQUIRED == "DP_MFA_REQUIRED"
    assert ErrorCode.DP_APP_NOT_FOUND == "DP_APP_NOT_FOUND"
    assert ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING == "DP_PUBLISH_REQUIREMENTS_MISSING"
    assert ErrorCode.DP_ICON_UPLOAD_FAILED == "DP_ICON_UPLOAD_FAILED"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_errors.py::test_dev_portal_error_codes_present -v`
Expected: `AttributeError: DP_LOGIN_FAILED` (or `FAILED` with `AttributeError`).

- [ ] **Step 3: Add the 5 entries to ErrorCode**

Edit `src/keboola_agent_cli/errors.py`. After the existing `# Sync` block (line ~89), before the closing brace of the enum (look for the last `XXX = "XXX"`), add:

```python
    # Developer Portal (since 0.45.0)
    DP_LOGIN_FAILED = "DP_LOGIN_FAILED"
    DP_MFA_REQUIRED = "DP_MFA_REQUIRED"
    DP_APP_NOT_FOUND = "DP_APP_NOT_FOUND"
    DP_PUBLISH_REQUIREMENTS_MISSING = "DP_PUBLISH_REQUIREMENTS_MISSING"
    DP_ICON_UPLOAD_FAILED = "DP_ICON_UPLOAD_FAILED"
```

Also extend the `_ERROR_TYPE` mapping (search for `ErrorCode.INVALID_TOKEN: "authentication"` around line 183) by adding:

```python
    ErrorCode.DP_LOGIN_FAILED: "authentication",
    ErrorCode.DP_MFA_REQUIRED: "authentication",
    ErrorCode.DP_APP_NOT_FOUND: "not_found",
    ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING: "validation",
    ErrorCode.DP_ICON_UPLOAD_FAILED: "api",
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_errors.py -v`
Expected: all pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/errors.py tests/test_errors.py
uv run ruff format src/keboola_agent_cli/errors.py tests/test_errors.py
git add src/keboola_agent_cli/errors.py tests/test_errors.py
git commit -m "feat(dev-portal): add ErrorCode entries for Developer Portal"
```

---

## Task 3: Extract `require_random_code_confirmation()` into `_helpers.py`

> **CRITICAL:** This is the load-bearing safety primitive. It must exist and be tested before any DP write code is written.

**Files:**
- Modify: `src/keboola_agent_cli/commands/_helpers.py` (add helper)
- Modify: `src/keboola_agent_cli/commands/permissions.py` (replace local helper with import)
- Test: `tests/test_helpers.py` (extend)

- [ ] **Step 1: Write failing tests in `tests/test_helpers.py`**

Append:

```python
import io
from unittest.mock import patch

import pytest
import typer

from keboola_agent_cli.commands._helpers import require_random_code_confirmation


class TestRequireRandomCodeConfirmation:
    def test_non_tty_exits_with_permission_denied(self, monkeypatch):
        # stdin isatty -> False
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(typer.Exit) as exc:
            require_random_code_confirmation("delete the universe")
        assert exc.value.exit_code == 6  # EXIT_PERMISSION_DENIED

    def test_correct_code_accepted(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.commands._helpers.secrets.token_hex",
            lambda n: "deadbeef",
        )
        monkeypatch.setattr("builtins.input", lambda: "deadbeef")
        # Returns None on success
        assert require_random_code_confirmation("patch app") is None

    def test_wrong_code_exits(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.commands._helpers.secrets.token_hex",
            lambda n: "deadbeef",
        )
        monkeypatch.setattr("builtins.input", lambda: "wrongcode")
        with pytest.raises(typer.Exit) as exc:
            require_random_code_confirmation("patch app")
        assert exc.value.exit_code == 6

    def test_eof_exits(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.commands._helpers.secrets.token_hex",
            lambda n: "deadbeef",
        )
        def raise_eof():
            raise EOFError
        monkeypatch.setattr("builtins.input", raise_eof)
        with pytest.raises(typer.Exit) as exc:
            require_random_code_confirmation("patch app")
        assert exc.value.exit_code == 6
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_helpers.py::TestRequireRandomCodeConfirmation -v`
Expected: `ImportError: cannot import name 'require_random_code_confirmation'`.

- [ ] **Step 3: Add the helper to `_helpers.py`**

Add imports at the top of `src/keboola_agent_cli/commands/_helpers.py` if not present:

```python
import secrets
import sys
```

Add at the end of the file:

```python
_CONFIRM_CODE_LENGTH = 4


def require_random_code_confirmation(action_description: str) -> None:
    """Require the user to type a random hex code to confirm a high-risk action.

    Prevents AI agents from programmatically approving production-affecting
    writes (Developer Portal updates, permission policy changes). The agent
    cannot predict the code and cannot type it into stdin.

    Behaviour:
    - No TTY -> raise typer.Exit(EXIT_PERMISSION_DENIED).
    - TTY + correct code -> return None (caller proceeds).
    - TTY + wrong code / EOF / interrupt -> raise typer.Exit(EXIT_PERMISSION_DENIED).

    Args:
        action_description: Short verb phrase shown in the prompt
            (e.g. "patch keboola.ex-foo", "update permission policy").
    """
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if not is_tty:
        sys.stderr.write(
            f"\nRefusing to {action_description}: this action requires a "
            "real terminal so a human can type the confirmation code. "
            "There is no --yes bypass by design.\n"
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)

    code = secrets.token_hex(_CONFIRM_CODE_LENGTH)
    sys.stderr.write(f"\nTo {action_description}, type this code: {code}\n")
    sys.stderr.write("Confirmation: ")
    sys.stderr.flush()

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    if user_input != code:
        sys.stderr.write("Confirmation failed. Aborting.\n")
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)
```

(`EXIT_PERMISSION_DENIED` is already imported in `_helpers.py` for the existing error mapping; double-check the existing imports and add it if missing — it lives in `..constants`.)

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_helpers.py::TestRequireRandomCodeConfirmation -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Replace the old helper in `commands/permissions.py`**

In `src/keboola_agent_cli/commands/permissions.py`:

(a) Delete the local `_require_interactive_confirmation` function (lines ~31–53 — the one that returns `bool`).
(b) Delete unused `import secrets` if it was only used by the deleted function.
(c) Add import at top:

```python
from ._helpers import require_random_code_confirmation
```

(d) Replace the three call sites (`permissions_set`, `permissions_reset`) — they currently look like:

```python
    if not _require_interactive_confirmation("update permission policy"):
        formatter.error(
            message="Confirmation failed. Permission policy not changed.",
            error_code=ErrorCode.PERMISSION_DENIED,
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None
```

Change each to:

```python
    require_random_code_confirmation("update permission policy")   # raises on failure
```

(Same shape for `permissions_reset` with `"remove permission policy"`.)

- [ ] **Step 6: Run existing permissions tests to confirm no regression**

Run: `uv run pytest tests/test_permissions.py -v`
Expected: all pass. (Some tests may have asserted on the old `_require_interactive_confirmation` return-bool behaviour — if so, update them to assert on `typer.Exit` and the user-facing prompt copy.)

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/commands/_helpers.py src/keboola_agent_cli/commands/permissions.py tests/test_helpers.py
uv run ruff format src/keboola_agent_cli/commands/_helpers.py src/keboola_agent_cli/commands/permissions.py tests/test_helpers.py
git add src/keboola_agent_cli/commands/_helpers.py src/keboola_agent_cli/commands/permissions.py tests/test_helpers.py
git commit -m "refactor(safety): extract require_random_code_confirmation() to _helpers"
```

---

## Task 4: Add `DeveloperPortalIdentity` model + `AppConfig` fields

**Files:**
- Modify: `src/keboola_agent_cli/models.py`
- Test: `tests/test_models.py` (extend)

- [ ] **Step 1: Write failing model tests**

Append to `tests/test_models.py`:

```python
import pytest

from keboola_agent_cli.models import AppConfig, DeveloperPortalIdentity


class TestDeveloperPortalIdentity:
    def test_minimal_construction(self):
        ident = DeveloperPortalIdentity(username="service.keboola.x", password="p")
        assert ident.username == "service.keboola.x"
        assert ident.password == "p"
        assert ident.role_hint == "vendor"
        assert ident.vendor is None
        assert ident.portal_url == "https://apps-api.keboola.com"

    def test_rejects_non_https_portal_url(self):
        with pytest.raises(ValueError, match="https"):
            DeveloperPortalIdentity(
                username="u", password="p",
                portal_url="http://apps-api.keboola.com",
            )

    def test_accepts_staging_https_portal_url(self):
        ident = DeveloperPortalIdentity(
            username="u", password="p",
            portal_url="https://apps-api.staging.keboola.dev",
        )
        assert ident.portal_url == "https://apps-api.staging.keboola.dev"


class TestAppConfigDevPortalFields:
    def test_defaults_empty(self):
        cfg = AppConfig()
        assert cfg.dev_portal_identities == {}
        assert cfg.default_dev_portal_identity == ""

    def test_round_trip(self):
        ident = DeveloperPortalIdentity(username="u", password="p", vendor="keboola")
        cfg = AppConfig(
            dev_portal_identities={"vendor-keboola": ident},
            default_dev_portal_identity="vendor-keboola",
        )
        round = AppConfig.model_validate(cfg.model_dump(mode="json"))
        assert round.dev_portal_identities["vendor-keboola"].vendor == "keboola"
        assert round.default_dev_portal_identity == "vendor-keboola"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_models.py::TestDeveloperPortalIdentity tests/test_models.py::TestAppConfigDevPortalFields -v`
Expected: `ImportError: cannot import name 'DeveloperPortalIdentity'`.

- [ ] **Step 3: Add the model**

In `src/keboola_agent_cli/models.py`, before `class AppConfig`, add:

```python
class DeveloperPortalIdentity(BaseModel):
    """One Developer Portal identity (service account or admin email).

    DP login is email + password (with MFA on personal accounts), producing
    a short-lived bearer that lives only in process memory. The username +
    password are persisted in config.json under the same 0600 protection as
    KB Storage tokens; the bearer is never written to disk.
    """

    username: str = Field(
        description="Email or service-account id used as the login subject"
    )
    password: str = Field(description="DP password — same protection as KB tokens")
    role_hint: str = Field(
        default="vendor",
        description=(
            "Free-text label shown in `dev-portal identity list` "
            "(e.g. 'vendor', 'admin'). Not validated against the portal."
        ),
    )
    vendor: str | None = Field(
        default=None,
        description=(
            "Optional default vendor for this identity (e.g. 'keboola'). "
            "Used as a default for commands that take --vendor; never "
            "overrides an explicit flag."
        ),
    )
    portal_url: str = Field(
        default="https://apps-api.keboola.com",
        description="DP base URL. Override for staging/test portals.",
    )

    @field_validator("portal_url")
    @classmethod
    def validate_portal_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(
                f"Portal URL must use https:// scheme, got: {v!r}"
            )
        return v
```

In the same file, extend `class AppConfig` with two new fields (insert after the existing `projects:` field):

```python
    dev_portal_identities: dict[str, DeveloperPortalIdentity] = Field(
        default_factory=dict,
        description="Map of alias -> DeveloperPortalIdentity",
    )
    default_dev_portal_identity: str = Field(
        default="",
        description="Alias of the default identity for `kbagent dev-portal` commands",
    )
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: all pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/models.py tests/test_models.py
uv run ruff format src/keboola_agent_cli/models.py tests/test_models.py
git add src/keboola_agent_cli/models.py tests/test_models.py
git commit -m "feat(dev-portal): add DeveloperPortalIdentity model + AppConfig fields"
```

---

## Task 5: Add `ConfigStore` methods for DP identities

**Files:**
- Modify: `src/keboola_agent_cli/config_store.py`
- Test: `tests/test_config_store.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config_store.py`:

```python
import pytest

from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import DeveloperPortalIdentity


class TestDevPortalIdentityCrud:
    def test_add_first_identity_becomes_default(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        cfg = config_store.load()
        assert cfg.dev_portal_identities["alpha"].username == "u"
        assert cfg.default_dev_portal_identity == "alpha"

    def test_add_duplicate_alias_raises(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        with pytest.raises(ConfigError, match="already exists"):
            config_store.add_dev_portal_identity("alpha", ident)

    def test_remove_identity(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        config_store.add_dev_portal_identity("beta", ident)
        config_store.remove_dev_portal_identity("alpha")
        cfg = config_store.load()
        assert "alpha" not in cfg.dev_portal_identities
        assert cfg.default_dev_portal_identity == "beta"

    def test_remove_unknown_raises(self, config_store):
        with pytest.raises(ConfigError, match="not found"):
            config_store.remove_dev_portal_identity("missing")

    def test_remove_last_clears_default(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        config_store.remove_dev_portal_identity("alpha")
        cfg = config_store.load()
        assert cfg.default_dev_portal_identity == ""

    def test_edit_identity(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        config_store.edit_dev_portal_identity("alpha", vendor="keboola", password="p2")
        cfg = config_store.load()
        assert cfg.dev_portal_identities["alpha"].vendor == "keboola"
        assert cfg.dev_portal_identities["alpha"].password == "p2"
        assert cfg.dev_portal_identities["alpha"].username == "u"

    def test_rename_identity(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        config_store.rename_dev_portal_identity("alpha", "alpha-prod")
        cfg = config_store.load()
        assert "alpha" not in cfg.dev_portal_identities
        assert "alpha-prod" in cfg.dev_portal_identities
        assert cfg.default_dev_portal_identity == "alpha-prod"

    def test_rename_collision_raises(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        config_store.add_dev_portal_identity("beta", ident)
        with pytest.raises(ConfigError, match="already in use"):
            config_store.rename_dev_portal_identity("alpha", "beta")

    def test_set_default_unknown_raises(self, config_store):
        with pytest.raises(ConfigError, match="not found"):
            config_store.set_default_dev_portal_identity("ghost")

    def test_set_default(self, config_store):
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)
        config_store.add_dev_portal_identity("beta", ident)
        config_store.set_default_dev_portal_identity("beta")
        cfg = config_store.load()
        assert cfg.default_dev_portal_identity == "beta"
```

Note: the `config_store` fixture lives in `tests/conftest.py` — reuse it.

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_config_store.py::TestDevPortalIdentityCrud -v`
Expected: `AttributeError: 'ConfigStore' object has no attribute 'add_dev_portal_identity'`.

- [ ] **Step 3: Add the 5 mirror methods to `ConfigStore`**

In `src/keboola_agent_cli/config_store.py`, after `rename_project()`, add:

```python
    def add_dev_portal_identity(
        self, alias: str, identity: "DeveloperPortalIdentity"
    ) -> None:
        """Add a Developer Portal identity to the configuration.

        Sets it as default if no default identity is set.

        Raises:
            ConfigError: If the alias already exists.
        """
        config = self.load()
        if alias in config.dev_portal_identities:
            raise ConfigError(
                f"Developer Portal identity '{alias}' already exists. "
                "Use 'dev-portal identity edit' to modify it."
            )
        config.dev_portal_identities[alias] = identity
        if not config.default_dev_portal_identity:
            config.default_dev_portal_identity = alias
        self.save(config)

    def remove_dev_portal_identity(self, alias: str) -> None:
        config = self.load()
        if alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{alias}' not found.")
        del config.dev_portal_identities[alias]
        if config.default_dev_portal_identity == alias:
            config.default_dev_portal_identity = next(
                iter(config.dev_portal_identities), ""
            )
        self.save(config)

    def get_dev_portal_identity(
        self, alias: str
    ) -> "DeveloperPortalIdentity | None":
        config = self.load()
        return config.dev_portal_identities.get(alias)

    def edit_dev_portal_identity(
        self, alias: str, **kwargs: str | None
    ) -> None:
        config = self.load()
        if alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{alias}' not found.")
        ident = config.dev_portal_identities[alias]
        for key, value in kwargs.items():
            if hasattr(ident, key) and value is not None:
                setattr(ident, key, value)
        config.dev_portal_identities[alias] = ident
        self.save(config)

    def rename_dev_portal_identity(self, old_alias: str, new_alias: str) -> None:
        config = self.load()
        if old_alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{old_alias}' not found.")
        if new_alias in config.dev_portal_identities:
            raise ConfigError(
                f"Cannot rename '{old_alias}' to '{new_alias}': "
                f"alias '{new_alias}' is already in use."
            )
        config.dev_portal_identities[new_alias] = (
            config.dev_portal_identities.pop(old_alias)
        )
        if config.default_dev_portal_identity == old_alias:
            config.default_dev_portal_identity = new_alias
        self.save(config)

    def set_default_dev_portal_identity(self, alias: str) -> None:
        config = self.load()
        if alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{alias}' not found.")
        config.default_dev_portal_identity = alias
        self.save(config)
```

Also at the top of the file, add:

```python
from .models import AppConfig, DeveloperPortalIdentity, ProjectConfig
```

(The existing import line already imports `AppConfig, ProjectConfig` — extend it.)

- [ ] **Step 4: Extend the `_warning` header**

Edit `CLAUDE_CONFIG_WARNING` near the top of `src/keboola_agent_cli/config_store.py` to append:

```
" Developer Portal credentials stored here have the SAME risk profile -- "
"never call apps-api.keboola.com directly; use `kbagent dev-portal ...`."
```

(Append inside the existing parenthesised string literal — keep it one logical sentence so JSON serialisation stays clean.)

- [ ] **Step 5: Run, expect pass**

Run: `uv run pytest tests/test_config_store.py -v`
Expected: all pass (existing project tests + new DP tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/config_store.py tests/test_config_store.py
uv run ruff format src/keboola_agent_cli/config_store.py tests/test_config_store.py
git add src/keboola_agent_cli/config_store.py tests/test_config_store.py
git commit -m "feat(dev-portal): ConfigStore methods for identity CRUD"
```

---

## Task 6: `DeveloperPortalClient` skeleton + login (token path)

**Files:**
- Create: `src/keboola_agent_cli/dev_portal_client.py`
- Create: `tests/test_dev_portal_client.py`

- [ ] **Step 1: Write failing test for token-path login**

Create `tests/test_dev_portal_client.py`:

```python
"""Tests for DeveloperPortalClient — login, MFA, CRUD against apps-api."""

from __future__ import annotations

import pytest

from keboola_agent_cli.dev_portal_client import DeveloperPortalClient
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import DeveloperPortalIdentity


def _identity(**overrides) -> DeveloperPortalIdentity:
    defaults = dict(username="service.keboola.x", password="p")
    defaults.update(overrides)
    return DeveloperPortalIdentity(**defaults)


class TestLoginTokenPath:
    def test_login_returns_bearer(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
            status_code=200,
        )
        with DeveloperPortalClient(_identity()) as client:
            client._ensure_authenticated()
            assert client._bearer == "Bearer abc"
            assert len(httpx_mock.get_requests()) == 1

    def test_login_bad_credentials_raises(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"error": "invalid credentials"},
            status_code=401,
        )
        with DeveloperPortalClient(_identity()) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client._ensure_authenticated()
            assert exc.value.error_code == ErrorCode.DP_LOGIN_FAILED
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_client.py::TestLoginTokenPath -v`
Expected: `ModuleNotFoundError: No module named 'keboola_agent_cli.dev_portal_client'`.

- [ ] **Step 3: Create the client skeleton**

Create `src/keboola_agent_cli/dev_portal_client.py`:

```python
"""Keboola Developer Portal HTTP client (apps-api.keboola.com).

Auth model:
- Login (email + password) returns a bearer token. On a personal account, the
  first login returns an MFA session; we prompt the user via /dev/tty and
  re-login with {email, session, code} to obtain the bearer.
- The bearer lives ONLY on this client instance (in self._bearer). It is
  never written to disk, never logged, and discarded when the client closes.
- Each kbagent invocation logs in fresh; there is no token cache.

The client is intentionally dumb: dry-run, diff, and confirm logic belong to
the service and command layers.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any

import httpx

from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient
from .models import DeveloperPortalIdentity

logger = logging.getLogger(__name__)


class DeveloperPortalClient(BaseHttpClient):
    """HTTP client for the Keboola Developer Portal."""

    def __init__(self, identity: DeveloperPortalIdentity) -> None:
        # We don't have a bearer yet — pass empty token. Login populates it.
        super().__init__(
            base_url=identity.portal_url,
            token="",
            headers={"Accept": "application/json"},
        )
        self._identity = identity
        self._bearer: str | None = None

    def _ensure_authenticated(self) -> None:
        """Log in if not already authenticated. Idempotent on the instance."""
        if self._bearer is not None:
            return
        self._bearer = self._login(self._identity.username, self._identity.password)
        self._client.headers["Authorization"] = self._bearer

    def _login(self, username: str, password: str) -> str:
        try:
            resp = self._client.post(
                "/auth/login",
                json={"email": username, "password": password},
            )
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Developer Portal login transport error: {exc}",
                error_code=ErrorCode.CONNECTION_ERROR,
            ) from exc
        if resp.status_code != 200:
            raise KeboolaApiError(
                message=(
                    f"Developer Portal login failed (HTTP {resp.status_code}). "
                    "Check the identity credentials."
                ),
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("token"):
            return payload["token"]
        # MFA path — implemented in Task 7.
        if isinstance(payload, dict) and payload.get("session"):
            return self._login_with_mfa(username, payload["session"])
        raise KeboolaApiError(
            message="Developer Portal login response missing token and session",
            error_code=ErrorCode.DP_LOGIN_FAILED,
        )

    def _login_with_mfa(self, username: str, session: str) -> str:
        # Placeholder — implemented in Task 7.
        raise KeboolaApiError(
            message="MFA login not implemented yet",
            error_code=ErrorCode.DP_MFA_REQUIRED,
        )
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_client.py::TestLoginTokenPath -v`
Expected: both tests pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
uv run ruff format src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git add src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git commit -m "feat(dev-portal): client skeleton + token-path login"
```

---

## Task 7: MFA login path + TTY prompt

**Files:**
- Modify: `src/keboola_agent_cli/dev_portal_client.py`
- Modify: `tests/test_dev_portal_client.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dev_portal_client.py`:

```python
class TestLoginMfaPath:
    def test_mfa_prompt_completes_login(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"session": "sess-1"},
            status_code=200,
            match_json={"email": "u@k.com", "password": "p"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer xyz"},
            status_code=200,
            match_json={"email": "u@k.com", "session": "sess-1", "code": "123456"},
        )
        # Mock the /dev/tty MFA prompt.
        monkeypatch.setattr(
            "keboola_agent_cli.dev_portal_client._tty_prompt",
            lambda label, secret=False: "123456",
        )
        ident = DeveloperPortalIdentity(username="u@k.com", password="p")
        with DeveloperPortalClient(ident) as client:
            client._ensure_authenticated()
            assert client._bearer == "Bearer xyz"

    def test_mfa_no_tty_raises_mfa_required(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"session": "sess-1"},
            status_code=200,
        )
        # _tty_prompt returns None when no terminal is available.
        monkeypatch.setattr(
            "keboola_agent_cli.dev_portal_client._tty_prompt",
            lambda label, secret=False: None,
        )
        ident = DeveloperPortalIdentity(username="u@k.com", password="p")
        with DeveloperPortalClient(ident) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client._ensure_authenticated()
            assert exc.value.error_code == ErrorCode.DP_MFA_REQUIRED
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_client.py::TestLoginMfaPath -v`
Expected: first test fails (no `_tty_prompt` exists yet); second fails because `_login_with_mfa` raises immediately.

- [ ] **Step 3: Implement `_tty_prompt` + full MFA path**

In `src/keboola_agent_cli/dev_portal_client.py`, add at module level (above the class):

```python
def _tty_prompt(label: str, *, secret: bool = False) -> str | None:
    """Prompt via the controlling terminal so a redirected stdin can't break it.

    Returns None when no /dev/tty is available (non-interactive shell, no
    controlling terminal). Caller must treat None as "cannot prompt".
    """
    try:
        with open("/dev/tty", "w") as out:
            if secret:
                import getpass

                return getpass.getpass(label, stream=out)
            out.write(label)
            out.flush()
            with open("/dev/tty", "r") as tin:
                return tin.readline().rstrip("\n")
    except OSError:
        return None
```

Replace the placeholder `_login_with_mfa` body:

```python
    def _login_with_mfa(self, username: str, session: str) -> str:
        code = _tty_prompt("MFA code: ")
        if not code:
            raise KeboolaApiError(
                message=(
                    "Developer Portal identity requires an MFA code, but no "
                    "interactive terminal is available. Run from a real "
                    "terminal, or switch to a service.{vendor}.{id} "
                    "account (no MFA)."
                ),
                error_code=ErrorCode.DP_MFA_REQUIRED,
            )
        try:
            resp = self._client.post(
                "/auth/login",
                json={"email": username, "session": session, "code": code.strip()},
            )
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Developer Portal MFA login transport error: {exc}",
                error_code=ErrorCode.CONNECTION_ERROR,
            ) from exc
        if resp.status_code != 200:
            raise KeboolaApiError(
                message=f"Developer Portal MFA login failed (HTTP {resp.status_code})",
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("token"):
            raise KeboolaApiError(
                message="Developer Portal MFA login response missing token",
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        return payload["token"]
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_client.py -v`
Expected: all 4 tests pass (2 token-path + 2 MFA).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
uv run ruff format src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git add src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git commit -m "feat(dev-portal): MFA login path via /dev/tty"
```

---

## Task 8: Client reads + standard writes (list/get/create/patch/publish/deprecate)

**Files:**
- Modify: `src/keboola_agent_cli/dev_portal_client.py`
- Modify: `tests/test_dev_portal_client.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dev_portal_client.py`:

```python
class TestPortalReads:
    def test_list_apps(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://apps-api.keboola.com/vendors/keboola/apps?limit=1000",
            json={"apps": [{"id": "keboola.ex-foo"}]},
        )
        with DeveloperPortalClient(_identity()) as client:
            apps = client.list_apps("keboola")
            assert apps == [{"id": "keboola.ex-foo"}]

    def test_get_app_404(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.missing",
            status_code=404,
            json={"error": "not found"},
        )
        with DeveloperPortalClient(_identity()) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client.get_app("keboola", "keboola.missing")
            assert exc.value.error_code == ErrorCode.DP_APP_NOT_FOUND


class TestPortalWrites:
    def test_create_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps",
            json={"id": "ex-foo", "name": "Foo"},
        )
        with DeveloperPortalClient(_identity()) as client:
            resp = client.create_app("keboola", {"id": "ex-foo", "name": "Foo", "type": "extractor"})
            assert resp["id"] == "ex-foo"

    def test_patch_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="PATCH",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo",
            json={"id": "ex-foo", "name": "Foo 2"},
        )
        with DeveloperPortalClient(_identity()) as client:
            resp = client.patch_app("keboola", "keboola.ex-foo", {"name": "Foo 2"})
            assert resp["name"] == "Foo 2"

    def test_publish_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/publish",
            json={"status": "submitted"},
        )
        with DeveloperPortalClient(_identity()) as client:
            assert client.publish_app("keboola", "keboola.ex-foo")["status"] == "submitted"

    def test_deprecate_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/deprecate",
            json={"status": "deprecated"},
        )
        with DeveloperPortalClient(_identity()) as client:
            assert client.deprecate_app("keboola", "keboola.ex-foo")["status"] == "deprecated"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_client.py::TestPortalReads tests/test_dev_portal_client.py::TestPortalWrites -v`
Expected: `AttributeError: 'DeveloperPortalClient' object has no attribute 'list_apps'`.

- [ ] **Step 3: Implement the methods**

Append to the `DeveloperPortalClient` class in `src/keboola_agent_cli/dev_portal_client.py`:

```python
    # ----- Reads -----

    def list_apps(self, vendor: str) -> list[dict[str, Any]]:
        self._ensure_authenticated()
        resp = self._do_request("GET", f"/vendors/{vendor}/apps?limit=1000")
        if resp.status_code != 200:
            self._raise_dp_error(resp, action="list apps", vendor=vendor)
        payload = resp.json()
        if isinstance(payload, dict) and "apps" in payload:
            return list(payload["apps"])
        if isinstance(payload, list):
            return payload
        return []

    def get_app(self, vendor: str, app_id: str) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request("GET", f"/vendors/{vendor}/apps/{app_id}")
        if resp.status_code == 404:
            raise KeboolaApiError(
                message=f"Developer Portal app '{app_id}' not found in vendor '{vendor}'",
                error_code=ErrorCode.DP_APP_NOT_FOUND,
            )
        if resp.status_code != 200:
            self._raise_dp_error(resp, action="get app", vendor=vendor, app_id=app_id)
        return resp.json()

    # ----- Writes -----

    def create_app(self, vendor: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request(
            "POST", f"/vendors/{vendor}/apps", json=payload
        )
        if resp.status_code not in (200, 201):
            self._raise_dp_error(resp, action="create app", vendor=vendor)
        return resp.json()

    def patch_app(
        self, vendor: str, app_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request(
            "PATCH", f"/vendors/{vendor}/apps/{app_id}", json=payload
        )
        if resp.status_code not in (200, 204):
            self._raise_dp_error(
                resp, action="patch app", vendor=vendor, app_id=app_id
            )
        return resp.json() if resp.content else {}

    def publish_app(self, vendor: str, app_id: str) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request(
            "POST", f"/vendors/{vendor}/apps/{app_id}/publish"
        )
        if resp.status_code not in (200, 202):
            self._raise_dp_error(
                resp, action="publish app", vendor=vendor, app_id=app_id
            )
        return resp.json() if resp.content else {"status": "submitted"}

    def deprecate_app(self, vendor: str, app_id: str) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request(
            "POST", f"/vendors/{vendor}/apps/{app_id}/deprecate"
        )
        if resp.status_code not in (200, 202):
            self._raise_dp_error(
                resp, action="deprecate app", vendor=vendor, app_id=app_id
            )
        return resp.json() if resp.content else {"status": "deprecated"}

    # ----- Error mapping -----

    def _raise_dp_error(
        self,
        resp: httpx.Response,
        *,
        action: str,
        vendor: str | None = None,
        app_id: str | None = None,
    ) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        ctx = f"{action}"
        if vendor:
            ctx += f" (vendor={vendor})"
        if app_id:
            ctx += f" (app={app_id})"
        raise KeboolaApiError(
            message=f"Developer Portal {ctx} failed (HTTP {resp.status_code}): {body}",
            error_code=ErrorCode.API_ERROR,
        )
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_client.py -v`
Expected: all tests pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
uv run ruff format src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git add src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git commit -m "feat(dev-portal): client reads + create/patch/publish/deprecate"
```

---

## Task 9: Client icon upload (two-hop)

**Files:**
- Modify: `src/keboola_agent_cli/dev_portal_client.py`
- Modify: `tests/test_dev_portal_client.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_dev_portal_client.py`:

```python
class TestIconUpload:
    def test_upload_icon_two_hop(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST", url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/icon",
            json={"link": "https://s3.example/presigned"},
        )
        # The S3 PUT bypasses httpx; we mock urllib.request.urlopen.
        seen = {}
        class _FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def fake_urlopen(req):
            seen["url"] = req.full_url
            seen["data"] = req.data
            seen["method"] = req.method
            return _FakeResp()
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with DeveloperPortalClient(_identity()) as client:
            client.upload_icon("keboola", "keboola.ex-foo", b"\x89PNG\r\n\x1a\nrest")
        assert seen["url"] == "https://s3.example/presigned"
        assert seen["data"] == b"\x89PNG\r\n\x1a\nrest"
        assert seen["method"] == "PUT"

    def test_upload_icon_presign_failure(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/icon",
            status_code=500,
            json={"error": "boom"},
        )
        with DeveloperPortalClient(_identity()) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client.upload_icon("keboola", "keboola.ex-foo", b"data")
            assert exc.value.error_code == ErrorCode.DP_ICON_UPLOAD_FAILED
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_client.py::TestIconUpload -v`
Expected: `AttributeError: ... 'upload_icon'`.

- [ ] **Step 3: Implement `upload_icon`**

Append to the `DeveloperPortalClient` class:

```python
    def upload_icon(self, vendor: str, app_id: str, png_bytes: bytes) -> None:
        """Two-hop icon upload: ask the portal for a presigned S3 URL, then PUT bytes there.

        The S3 PUT does NOT use this client's httpx instance (no retry, no auth,
        no User-Agent injection). We use urllib directly so the wire shape stays
        exactly what S3 expects.
        """
        self._ensure_authenticated()
        resp = self._do_request(
            "POST", f"/vendors/{vendor}/apps/{app_id}/icon"
        )
        if resp.status_code != 200:
            raise KeboolaApiError(
                message=(
                    f"Developer Portal failed to mint icon-upload URL "
                    f"(HTTP {resp.status_code})"
                ),
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            )
        payload = resp.json()
        link = payload.get("link") if isinstance(payload, dict) else None
        if not link:
            raise KeboolaApiError(
                message="Developer Portal icon-upload response missing 'link'",
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            )
        req = urllib.request.Request(
            link, data=png_bytes,
            headers={"Content-Type": "image/png"}, method="PUT",
        )
        try:
            with urllib.request.urlopen(req) as s3_resp:
                if getattr(s3_resp, "status", 200) >= 300:
                    raise KeboolaApiError(
                        message=f"Icon S3 PUT failed (HTTP {s3_resp.status})",
                        error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
                    )
        except urllib.error.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Icon S3 PUT failed (HTTP {exc.code}): {exc.reason}",
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            ) from exc
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_client.py -v`
Expected: all client tests pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
uv run ruff format src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git add src/keboola_agent_cli/dev_portal_client.py tests/test_dev_portal_client.py
git commit -m "feat(dev-portal): icon upload (two-hop, presigned S3 PUT)"
```

---

## Task 10: `DeveloperPortalService` identity CRUD + verify-on-add

**Files:**
- Create: `src/keboola_agent_cli/services/dev_portal_service.py`
- Create: `tests/test_dev_portal_service.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_dev_portal_service.py`:

```python
"""Tests for DeveloperPortalService — identity CRUD, prepare/apply, diff, validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import DeveloperPortalIdentity
from keboola_agent_cli.services.dev_portal_service import DeveloperPortalService


@pytest.fixture
def fake_client():
    return MagicMock()


@pytest.fixture
def service(config_store, fake_client):
    factory = lambda identity: fake_client
    return DeveloperPortalService(config_store=config_store, client_factory=factory)


class TestIdentityCrud:
    def test_add_and_list(self, service, fake_client):
        # add_identity also runs verify (login probe).
        fake_client.list_apps.return_value = []
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)
        result = service.list_identities()
        assert "alpha" in result
        assert result["alpha"].username == "u"

    def test_add_verify_failure_does_not_persist(self, service, fake_client, config_store):
        fake_client._ensure_authenticated.side_effect = KeboolaApiError(
            message="bad creds", error_code=ErrorCode.DP_LOGIN_FAILED,
        )
        ident = DeveloperPortalIdentity(username="u", password="bad")
        with pytest.raises(KeboolaApiError) as exc:
            service.add_identity("alpha", ident)
        assert exc.value.error_code == ErrorCode.DP_LOGIN_FAILED
        assert config_store.load().dev_portal_identities == {}

    def test_use_sets_default(self, service, fake_client, config_store):
        fake_client._ensure_authenticated.return_value = None
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)
        service.add_identity("beta", ident)
        service.use_identity("beta")
        assert config_store.load().default_dev_portal_identity == "beta"

    def test_remove(self, service, fake_client, config_store):
        fake_client._ensure_authenticated.return_value = None
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)
        service.remove_identity("alpha")
        assert "alpha" not in config_store.load().dev_portal_identities
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_service.py::TestIdentityCrud -v`
Expected: `ModuleNotFoundError: No module named 'keboola_agent_cli.services.dev_portal_service'`.

- [ ] **Step 3: Create the service skeleton with identity CRUD**

Create `src/keboola_agent_cli/services/dev_portal_service.py`:

```python
"""Developer Portal business logic.

Identity CRUD + prepare/apply discipline for portal writes. Commands stay
thin; this module owns diff computation, publish pre-flight validation,
and the verify-on-add login probe.
"""

from __future__ import annotations

from typing import Any, Callable

from ..config_store import ConfigStore
from ..dev_portal_client import DeveloperPortalClient
from ..errors import ConfigError
from ..models import DeveloperPortalIdentity


ClientFactory = Callable[[DeveloperPortalIdentity], DeveloperPortalClient]


class DeveloperPortalService:
    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory,
    ) -> None:
        self._store = config_store
        self._client_factory = client_factory

    # ----- Identity management -----

    def add_identity(self, alias: str, identity: DeveloperPortalIdentity) -> None:
        """Verify creds (login probe) BEFORE persisting.

        Same UX as `kbagent project add` (which calls verify_token first):
        bad creds fail fast and never land in config.json.
        """
        with self._client_factory(identity) as client:
            client._ensure_authenticated()  # raises on bad creds / MFA failure
        self._store.add_dev_portal_identity(alias, identity)

    def list_identities(self) -> dict[str, DeveloperPortalIdentity]:
        return dict(self._store.load().dev_portal_identities)

    def remove_identity(self, alias: str) -> None:
        self._store.remove_dev_portal_identity(alias)

    def edit_identity(self, alias: str, **fields: Any) -> None:
        self._store.edit_dev_portal_identity(alias, **fields)

    def rename_identity(self, old_alias: str, new_alias: str) -> None:
        self._store.rename_dev_portal_identity(old_alias, new_alias)

    def use_identity(self, alias: str) -> None:
        self._store.set_default_dev_portal_identity(alias)

    def current_identity(self) -> str:
        return self._store.load().default_dev_portal_identity

    def verify_identity(self, alias: str) -> dict[str, str]:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            client._ensure_authenticated()
        return {"alias": alias, "username": ident.username}

    # ----- Internal -----

    def _resolve_identity(self, alias: str) -> DeveloperPortalIdentity:
        ident = self._store.get_dev_portal_identity(alias)
        if ident is None:
            raise ConfigError(
                f"Developer Portal identity '{alias}' not found. "
                "Run `kbagent dev-portal identity list` to see configured identities."
            )
        return ident
```

Also create `src/keboola_agent_cli/services/__init__.py` if it doesn't already exist (it does — this is just a sanity check).

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_service.py::TestIdentityCrud -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/services/dev_portal_service.py tests/test_dev_portal_service.py
uv run ruff format src/keboola_agent_cli/services/dev_portal_service.py tests/test_dev_portal_service.py
git add src/keboola_agent_cli/services/dev_portal_service.py tests/test_dev_portal_service.py
git commit -m "feat(dev-portal): service with identity CRUD + verify-on-add"
```

---

## Task 11: Service reads + `prepare_*`/`apply` (diff + publish pre-flight)

**Files:**
- Modify: `src/keboola_agent_cli/services/dev_portal_service.py`
- Modify: `tests/test_dev_portal_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dev_portal_service.py`:

```python
class TestReadsAndPrepareApply:
    def _setup(self, service, fake_client):
        fake_client._ensure_authenticated.return_value = None
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)

    def test_list_apps(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.list_apps.return_value = [{"id": "ex-a"}]
        assert service.list_apps("alpha", "keboola") == [{"id": "ex-a"}]
        fake_client.list_apps.assert_called_with("keboola")

    def test_get_app(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {"id": "ex-a", "name": "Hello"}
        assert service.get_app("alpha", "keboola", "keboola.ex-a")["name"] == "Hello"

    def test_prepare_create_requires_id_name_type(self, service, fake_client):
        self._setup(service, fake_client)
        with pytest.raises(KeboolaApiError, match="payload must include 'id'"):
            service.prepare_create("alpha", "keboola", {"name": "F", "type": "extractor"})

    def test_prepare_create_rejects_banned_words_in_name(self, service, fake_client):
        self._setup(service, fake_client)
        with pytest.raises(KeboolaApiError, match="must not contain"):
            service.prepare_create(
                "alpha", "keboola",
                {"id": "x", "name": "Foo extractor", "type": "extractor"},
            )

    def test_prepare_patch_diff(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {
            "id": "ex-a", "name": "Old", "shortDescription": "same",
        }
        pending = service.prepare_patch(
            "alpha", "keboola", "keboola.ex-a",
            {"name": "New", "shortDescription": "same"},
        )
        keys = {d.key for d in pending.diff}
        assert keys == {"name"}  # shortDescription unchanged is filtered out
        assert pending.diff[0].current == "Old"
        assert pending.diff[0].new == "New"

    def test_apply_patch_calls_client(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {"id": "ex-a", "name": "Old"}
        fake_client.patch_app.return_value = {"id": "ex-a", "name": "New"}
        pending = service.prepare_patch(
            "alpha", "keboola", "keboola.ex-a", {"name": "New"}
        )
        result = service.apply(pending)
        assert result["name"] == "New"
        fake_client.patch_app.assert_called_with("keboola", "keboola.ex-a", {"name": "New"})

    def test_prepare_publish_missing_fields(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {
            "id": "ex-a", "name": "Foo", "type": "extractor",
            # missing icon, repository, descriptions, license, docs
        }
        with pytest.raises(KeboolaApiError) as exc:
            service.prepare_publish("alpha", "keboola", "keboola.ex-a")
        assert exc.value.error_code == ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING
        assert "icon" in str(exc.value)
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_service.py::TestReadsAndPrepareApply -v`
Expected: `AttributeError: ... 'list_apps'`.

- [ ] **Step 3: Implement `prepare_*` + `apply` + helpers + dataclasses**

Add at the top of `src/keboola_agent_cli/services/dev_portal_service.py`, near the imports:

```python
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ErrorCode, KeboolaApiError
```

After the existing imports/class declarations (above `class DeveloperPortalService`), add the dataclasses:

```python
@dataclass(frozen=True)
class FieldDiff:
    key: str
    current: Any
    new: Any


@dataclass(frozen=True)
class PendingWrite:
    """Base for any prepared portal write. apply() in the service dispatches on the subclass."""
    alias: str
    vendor: str


@dataclass(frozen=True)
class PendingCreate(PendingWrite):
    payload: dict[str, Any]


@dataclass(frozen=True)
class PendingPatch(PendingWrite):
    app_id: str
    payload: dict[str, Any]
    current: dict[str, Any]
    diff: list[FieldDiff] = field(default_factory=list)


@dataclass(frozen=True)
class PendingIconUpload(PendingWrite):
    app_id: str
    png_path: Path
    png_bytes: bytes


@dataclass(frozen=True)
class PendingPublish(PendingWrite):
    app_id: str
    current: dict[str, Any]


@dataclass(frozen=True)
class PendingDeprecate(PendingWrite):
    app_id: str


_BANNED_NAME_WORDS = ("extractor", "writer")
_REQUIRED_PUBLISH_FIELDS = (
    "icon", "name", "type", "repository",
    "shortDescription", "longDescription",
    "licenseUrl", "documentationUrl",
)
```

Append methods to `DeveloperPortalService`:

```python
    # ----- Reads -----

    def list_apps(self, alias: str, vendor: str) -> list[dict[str, Any]]:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            return client.list_apps(vendor)

    def get_app(
        self, alias: str, vendor: str, app_id: str
    ) -> dict[str, Any]:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            return client.get_app(vendor, app_id)

    # ----- Prepare (no portal write yet) -----

    def prepare_create(
        self, alias: str, vendor: str, payload: dict[str, Any]
    ) -> PendingCreate:
        for required in ("id", "name", "type"):
            if required not in payload:
                raise KeboolaApiError(
                    message=f"create payload must include '{required}'",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
        name_lower = str(payload["name"]).lower()
        for banned in _BANNED_NAME_WORDS:
            if banned in name_lower:
                raise KeboolaApiError(
                    message=(
                        f"App name must not contain {_BANNED_NAME_WORDS!r}; "
                        f"got {payload['name']!r}"
                    ),
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
        # Confirm identity exists; defer login until apply().
        self._resolve_identity(alias)
        return PendingCreate(alias=alias, vendor=vendor, payload=payload)

    def prepare_patch(
        self,
        alias: str,
        vendor: str,
        app_id: str,
        payload: dict[str, Any],
    ) -> PendingPatch:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            current = client.get_app(vendor, app_id)
        diff = [
            FieldDiff(key=k, current=current.get(k), new=v)
            for k, v in payload.items()
            if current.get(k) != v
        ]
        return PendingPatch(
            alias=alias, vendor=vendor, app_id=app_id,
            payload=payload, current=current, diff=diff,
        )

    def prepare_upload_icon(
        self, alias: str, vendor: str, app_id: str, path: str | Path
    ) -> PendingIconUpload:
        p = Path(path)
        if not p.is_file():
            raise KeboolaApiError(
                message=f"Icon file not found: {p}",
                error_code=ErrorCode.FILE_NOT_FOUND,
            )
        data = p.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise KeboolaApiError(
                message=f"Icon file is not a PNG: {p}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        # Soft dimension check via PNG IHDR (bytes 16-24 of a valid PNG).
        if len(data) >= 24:
            import struct
            width, height = struct.unpack(">II", data[16:24])
            if (width, height) != (128, 128):
                # Soft warning only — apps-api will reject if it's strict.
                import logging
                logging.getLogger(__name__).warning(
                    "Icon is %dx%d, not 128x128 — portal may reject it.",
                    width, height,
                )
        self._resolve_identity(alias)
        return PendingIconUpload(
            alias=alias, vendor=vendor, app_id=app_id, png_path=p, png_bytes=data,
        )

    def prepare_publish(
        self, alias: str, vendor: str, app_id: str
    ) -> PendingPublish:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            current = client.get_app(vendor, app_id)
        missing = [f for f in _REQUIRED_PUBLISH_FIELDS if not current.get(f)]
        if missing:
            raise KeboolaApiError(
                message=(
                    f"Cannot publish {app_id}: missing required fields "
                    f"{missing}. Fix them via `kbagent dev-portal patch` first."
                ),
                error_code=ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING,
            )
        return PendingPublish(
            alias=alias, vendor=vendor, app_id=app_id, current=current
        )

    def prepare_deprecate(
        self, alias: str, vendor: str, app_id: str
    ) -> PendingDeprecate:
        self._resolve_identity(alias)
        return PendingDeprecate(alias=alias, vendor=vendor, app_id=app_id)

    # ----- Apply (calls the portal write) -----

    def apply(self, pending: PendingWrite) -> dict[str, Any]:
        ident = self._resolve_identity(pending.alias)
        with self._client_factory(ident) as client:
            if isinstance(pending, PendingCreate):
                return client.create_app(pending.vendor, pending.payload)
            if isinstance(pending, PendingPatch):
                return client.patch_app(
                    pending.vendor, pending.app_id, pending.payload
                )
            if isinstance(pending, PendingIconUpload):
                client.upload_icon(
                    pending.vendor, pending.app_id, pending.png_bytes
                )
                return {"status": "uploaded", "app": pending.app_id}
            if isinstance(pending, PendingPublish):
                return client.publish_app(pending.vendor, pending.app_id)
            if isinstance(pending, PendingDeprecate):
                return client.deprecate_app(pending.vendor, pending.app_id)
        raise KeboolaApiError(
            message=f"Unknown pending write type: {type(pending).__name__}",
            error_code=ErrorCode.INTERNAL_ERROR,
        )
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/services/dev_portal_service.py tests/test_dev_portal_service.py
uv run ruff format src/keboola_agent_cli/services/dev_portal_service.py tests/test_dev_portal_service.py
git add src/keboola_agent_cli/services/dev_portal_service.py tests/test_dev_portal_service.py
git commit -m "feat(dev-portal): service reads + prepare/apply + diff + publish pre-flight"
```

---

## Task 12: Permission registry + helper factories

**Files:**
- Modify: `src/keboola_agent_cli/permissions.py`
- Modify: `src/keboola_agent_cli/commands/_helpers.py`
- Modify: `tests/test_permissions.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_permissions.py`:

```python
class TestDevPortalPermissions:
    DP_OPS = {
        "dev-portal.identity-add": "admin",
        "dev-portal.identity-list": "read",
        "dev-portal.identity-edit": "admin",
        "dev-portal.identity-remove": "admin",
        "dev-portal.identity-use": "write",
        "dev-portal.identity-verify": "read",
        "dev-portal.list": "read",
        "dev-portal.get": "read",
        "dev-portal.create": "write",
        "dev-portal.patch": "write",
        "dev-portal.upload-icon": "write",
        "dev-portal.publish": "admin",
        "dev-portal.deprecate": "destructive",
    }

    def test_registry_contains_all_dev_portal_ops(self):
        from keboola_agent_cli.permissions import OPERATION_REGISTRY
        for op, expected_cat in self.DP_OPS.items():
            assert OPERATION_REGISTRY.get(op) == expected_cat, op
```

(Note: that's 13 ops — same count as the spec table.)

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_permissions.py::TestDevPortalPermissions -v`
Expected: `AssertionError: dev-portal.identity-add`.

- [ ] **Step 3: Add entries to `OPERATION_REGISTRY`**

In `src/keboola_agent_cli/permissions.py`, find a suitable place in `OPERATION_REGISTRY` (alphabetically near `data-app.*` or at the end) and add:

```python
    # Developer Portal (since 0.45.0)
    "dev-portal.identity-add": "admin",
    "dev-portal.identity-list": "read",
    "dev-portal.identity-edit": "admin",
    "dev-portal.identity-remove": "admin",
    "dev-portal.identity-use": "write",
    "dev-portal.identity-verify": "read",
    "dev-portal.list": "read",
    "dev-portal.get": "read",
    "dev-portal.create": "write",
    "dev-portal.patch": "write",
    "dev-portal.upload-icon": "write",
    "dev-portal.publish": "admin",
    "dev-portal.deprecate": "destructive",
```

- [ ] **Step 4: Add factories to `_helpers.py`**

Append to `src/keboola_agent_cli/commands/_helpers.py`:

```python
def resolve_identity_alias(ctx: typer.Context, explicit: str | None) -> str:
    """Resolve the dev-portal identity alias for this invocation.

    Order: explicit --identity flag > default from config > error.
    """
    if explicit:
        return explicit
    config_store: ConfigStore = get_service(ctx, "config_store")
    default = config_store.load().default_dev_portal_identity
    if not default:
        raise typer.BadParameter(
            "No Developer Portal identity selected. Pass --identity <alias>, "
            "or set a default via `kbagent dev-portal identity use <alias>`."
        )
    return default


def get_dev_portal_service(ctx: typer.Context):
    """Build a DeveloperPortalService bound to the current ConfigStore."""
    from ..dev_portal_client import DeveloperPortalClient
    from ..services.dev_portal_service import DeveloperPortalService

    config_store: ConfigStore = get_service(ctx, "config_store")
    return DeveloperPortalService(
        config_store=config_store,
        client_factory=lambda identity: DeveloperPortalClient(identity),
    )
```

(`ConfigStore` import is already at the top of `_helpers.py`.)

- [ ] **Step 5: Run, expect pass**

Run: `uv run pytest tests/test_permissions.py -v`
Expected: all permission tests pass.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/permissions.py src/keboola_agent_cli/commands/_helpers.py tests/test_permissions.py
uv run ruff format src/keboola_agent_cli/permissions.py src/keboola_agent_cli/commands/_helpers.py tests/test_permissions.py
git add src/keboola_agent_cli/permissions.py src/keboola_agent_cli/commands/_helpers.py tests/test_permissions.py
git commit -m "feat(dev-portal): permission registry entries + identity resolver"
```

---

## Task 13: Command layer — identity subcommands + reads

**Files:**
- Create: `src/keboola_agent_cli/commands/dev_portal.py`
- Modify: `src/keboola_agent_cli/cli.py`
- Create: `tests/test_dev_portal_cli.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_dev_portal_cli.py`:

```python
"""Tests for `kbagent dev-portal` command layer via CliRunner."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app


runner = CliRunner()


class TestIdentityCommands:
    def test_identity_add_and_list_json(self, tmp_config_dir):
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.add_identity"
        ) as add_:
            r = runner.invoke(
                app,
                [
                    "--config-dir", str(tmp_config_dir),
                    "--json", "dev-portal", "identity", "add",
                    "--alias", "alpha",
                    "--username", "service.keboola.x",
                    "--password", "p",
                ],
            )
        assert r.exit_code == 0, r.output
        add_.assert_called_once()

    def test_identity_use_sets_default(self, tmp_config_dir, config_store):
        from keboola_agent_cli.models import DeveloperPortalIdentity
        config_store.add_dev_portal_identity(
            "alpha", DeveloperPortalIdentity(username="u", password="p")
        )
        config_store.add_dev_portal_identity(
            "beta", DeveloperPortalIdentity(username="u", password="p")
        )
        r = runner.invoke(
            app,
            [
                "--config-dir", str(tmp_config_dir),
                "dev-portal", "identity", "use", "beta",
            ],
        )
        assert r.exit_code == 0, r.output
        assert config_store.load().default_dev_portal_identity == "beta"


class TestReadCommands:
    def test_list_apps_json(self, tmp_config_dir, config_store):
        from keboola_agent_cli.models import DeveloperPortalIdentity
        config_store.add_dev_portal_identity(
            "alpha", DeveloperPortalIdentity(username="u", password="p", vendor="keboola")
        )
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.list_apps",
            return_value=[{"id": "keboola.ex-a"}],
        ):
            r = runner.invoke(
                app,
                [
                    "--config-dir", str(tmp_config_dir),
                    "--json", "dev-portal", "list", "--vendor", "keboola",
                ],
            )
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert data == [{"id": "keboola.ex-a"}]
```

(`tmp_config_dir` and `config_store` fixtures live in `tests/conftest.py`.)

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_cli.py::TestIdentityCommands -v`
Expected: command does not exist (`No such command 'dev-portal'`).

- [ ] **Step 3: Create the command module (identity + reads only — writes in Task 14)**

Create `src/keboola_agent_cli/commands/dev_portal.py`:

```python
"""`kbagent dev-portal` — Developer Portal command surface.

Identity management mirrors `kbagent project`; portal writes are gated by
`require_random_code_confirmation()` from _helpers — there is no `--yes`
bypass and no env-var override.
"""

from __future__ import annotations

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import DeveloperPortalIdentity
from ._helpers import (
    get_dev_portal_service,
    get_formatter,
    map_error_to_exit_code,
    resolve_identity_alias,
)

dev_portal_app = typer.Typer(
    help="Keboola Developer Portal — multi-identity, production-safe writes.",
    no_args_is_help=True,
)

identity_app = typer.Typer(help="Manage Developer Portal identities (login credentials).")
dev_portal_app.add_typer(identity_app, name="identity")


def _split_app(app: str) -> tuple[str, str]:
    """Split `VENDOR.APP_ID` into (vendor, app_id)."""
    if "." not in app:
        raise typer.BadParameter(
            f"--app must be in VENDOR.APP_ID form (e.g. keboola.ex-foo), got: {app!r}"
        )
    vendor, _ = app.split(".", 1)
    return vendor, app


# ----- Identity subcommands -----

@identity_app.command("add")
def identity_add(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--alias"),
    username: str = typer.Option(..., "--username"),
    password: str | None = typer.Option(None, "--password"),
    password_stdin: bool = typer.Option(
        False, "--password-stdin",
        help="Read password from stdin (paste from a secrets manager).",
    ),
    role_hint: str = typer.Option("vendor", "--role-hint"),
    vendor: str | None = typer.Option(None, "--vendor"),
    portal_url: str = typer.Option(
        "https://apps-api.keboola.com", "--portal-url",
    ),
) -> None:
    formatter = get_formatter(ctx)
    if password_stdin:
        import sys as _sys
        password = _sys.stdin.read().strip()
    if not password:
        raise typer.BadParameter("Pass --password or --password-stdin.")
    identity = DeveloperPortalIdentity(
        username=username,
        password=password,
        role_hint=role_hint,
        vendor=vendor,
        portal_url=portal_url,
    )
    svc = get_dev_portal_service(ctx)
    try:
        svc.add_identity(alias, identity)
    except (ConfigError, KeboolaApiError) as exc:
        formatter.error(message=str(exc), error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR))
        raise typer.Exit(code=map_error_to_exit_code(exc) if isinstance(exc, KeboolaApiError) else 5) from None
    formatter.output({"status": "ok", "alias": alias, "username": username})


@identity_app.command("list")
def identity_list(ctx: typer.Context) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    identities = svc.list_identities()
    default = svc.current_identity()
    rows = [
        {
            "alias": alias,
            "username": ident.username,
            "vendor": ident.vendor or "",
            "role_hint": ident.role_hint,
            "portal_url": ident.portal_url,
            "default": alias == default,
        }
        for alias, ident in identities.items()
    ]
    formatter.output(rows)


@identity_app.command("remove")
def identity_remove(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--alias"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    try:
        svc.remove_identity(alias)
    except ConfigError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output({"status": "ok", "removed": alias})


@identity_app.command("edit")
def identity_edit(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--alias"),
    username: str | None = typer.Option(None, "--username"),
    password: str | None = typer.Option(None, "--password"),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
    role_hint: str | None = typer.Option(None, "--role-hint"),
    vendor: str | None = typer.Option(None, "--vendor"),
    new_alias: str | None = typer.Option(None, "--new-alias"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    if password_stdin:
        import sys as _sys
        password = _sys.stdin.read().strip()
    try:
        if new_alias:
            svc.rename_identity(alias, new_alias)
            alias = new_alias
        svc.edit_identity(
            alias,
            username=username,
            password=password,
            role_hint=role_hint,
            vendor=vendor,
        )
    except ConfigError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output({"status": "ok", "alias": alias})


@identity_app.command("use")
def identity_use(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Identity alias to set as default"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    try:
        svc.use_identity(alias)
    except ConfigError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output({"status": "ok", "default": alias})


@identity_app.command("current")
def identity_current(ctx: typer.Context) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    formatter.output({"default": svc.current_identity()})


@identity_app.command("verify")
def identity_verify(
    ctx: typer.Context,
    identity: str | None = typer.Option(None, "--identity"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    try:
        info = svc.verify_identity(alias)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", **info})


# ----- Read commands -----

@dev_portal_app.command("list")
def list_apps(
    ctx: typer.Context,
    vendor: str = typer.Option(..., "--vendor"),
    identity: str | None = typer.Option(None, "--identity"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    try:
        apps = svc.list_apps(alias, vendor)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(apps)


@dev_portal_app.command("get")
def get_app_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app", help="VENDOR.APP_ID, e.g. keboola.ex-foo"),
    identity: str | None = typer.Option(None, "--identity"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        result = svc.get_app(alias, vendor, app_id)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result)
```

- [ ] **Step 4: Register the sub-app in `cli.py`**

In `src/keboola_agent_cli/cli.py`, find the `_DEV = "Development"` block and add:

```python
from .commands.dev_portal import dev_portal_app
```

(Add the import near the other `from .commands.X import X_app` lines.)

In the `_DEV` registration block, add (e.g. right after `app.add_typer(workspace_app, name="workspace", rich_help_panel=_DEV)`):

```python
app.add_typer(dev_portal_app, name="dev-portal", rich_help_panel=_DEV)
```

- [ ] **Step 5: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_cli.py -v`
Expected: all tests in `TestIdentityCommands` and `TestReadCommands` pass.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/commands/dev_portal.py src/keboola_agent_cli/cli.py tests/test_dev_portal_cli.py
uv run ruff format src/keboola_agent_cli/commands/dev_portal.py src/keboola_agent_cli/cli.py tests/test_dev_portal_cli.py
git add src/keboola_agent_cli/commands/dev_portal.py src/keboola_agent_cli/cli.py tests/test_dev_portal_cli.py
git commit -m "feat(dev-portal): identity subcommands + list/get"
```

---

## Task 14: Command layer — write commands (gated by random-code confirm)

**Files:**
- Modify: `src/keboola_agent_cli/commands/dev_portal.py`
- Modify: `tests/test_dev_portal_cli.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dev_portal_cli.py`:

```python
class TestWriteCommands:
    """Every write must require the random-code confirm. No --yes."""

    def _seed_identity(self, config_store):
        from keboola_agent_cli.models import DeveloperPortalIdentity
        config_store.add_dev_portal_identity(
            "alpha", DeveloperPortalIdentity(username="u", password="p", vendor="keboola"),
        )

    def test_patch_non_tty_exits_6(self, tmp_config_dir, config_store):
        """Without a TTY there is NO bypass — exit 6, no portal call."""
        self._seed_identity(config_store)
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.prepare_patch"
        ) as prep:
            from keboola_agent_cli.services.dev_portal_service import PendingPatch, FieldDiff
            prep.return_value = PendingPatch(
                alias="alpha", vendor="keboola", app_id="keboola.ex-a",
                payload={"name": "New"}, current={"name": "Old"},
                diff=[FieldDiff(key="name", current="Old", new="New")],
            )
            with patch(
                "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.apply"
            ) as apply_:
                # CliRunner provides a non-TTY stdin.
                r = runner.invoke(
                    app,
                    [
                        "--config-dir", str(tmp_config_dir),
                        "dev-portal", "patch",
                        "--app", "keboola.ex-a",
                        "--data", "/tmp/does-not-matter.json",
                    ],
                    input="",
                )
        assert r.exit_code == 6, r.output
        apply_.assert_not_called()

    def test_patch_dry_run_no_confirm(self, tmp_config_dir, config_store, tmp_path):
        """--dry-run prints diff and exits 0 without any confirm prompt."""
        self._seed_identity(config_store)
        data_file = tmp_path / "patch.json"
        data_file.write_text(json.dumps({"name": "New"}))
        with patch(
            "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.prepare_patch"
        ) as prep:
            from keboola_agent_cli.services.dev_portal_service import PendingPatch, FieldDiff
            prep.return_value = PendingPatch(
                alias="alpha", vendor="keboola", app_id="keboola.ex-a",
                payload={"name": "New"}, current={"name": "Old"},
                diff=[FieldDiff(key="name", current="Old", new="New")],
            )
            with patch(
                "keboola_agent_cli.services.dev_portal_service.DeveloperPortalService.apply"
            ) as apply_:
                r = runner.invoke(
                    app,
                    [
                        "--config-dir", str(tmp_config_dir),
                        "--json", "dev-portal", "patch",
                        "--app", "keboola.ex-a",
                        "--data", str(data_file),
                        "--dry-run",
                    ],
                )
        assert r.exit_code == 0, r.output
        apply_.assert_not_called()
        # JSON output should advertise the dry-run status
        assert "dry-run" in r.stdout
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_dev_portal_cli.py::TestWriteCommands -v`
Expected: command does not exist yet.

- [ ] **Step 3: Add write commands**

Append to `src/keboola_agent_cli/commands/dev_portal.py`:

```python
import json
from pathlib import Path

from ._helpers import require_random_code_confirmation


def _load_payload(data: str | None) -> dict:
    if data is None:
        raise typer.BadParameter("--data is required")
    if data == "-":
        import sys as _sys
        return json.loads(_sys.stdin.read())
    return json.loads(Path(data).read_text())


def _render_pending(formatter, pending) -> None:
    """Write a stderr-only preview of the pending write."""
    from ..services.dev_portal_service import (
        PendingCreate, PendingPatch, PendingIconUpload,
        PendingPublish, PendingDeprecate,
    )
    err = formatter.err_console
    if isinstance(pending, PendingPatch):
        err.print(f"[bold]PATCH[/bold] /vendors/{pending.vendor}/apps/{pending.app_id}")
        for d in pending.diff:
            err.print(f"  [yellow]{d.key}[/yellow]: {d.current!r} -> {d.new!r}")
        if not pending.diff:
            err.print("  [dim]no field-level changes (payload matches current state)[/dim]")
    elif isinstance(pending, PendingCreate):
        err.print(f"[bold]POST[/bold] /vendors/{pending.vendor}/apps")
        err.print_json(json.dumps(pending.payload))
    elif isinstance(pending, PendingIconUpload):
        err.print(
            f"[bold]UPLOAD ICON[/bold] {pending.png_path} -> "
            f"{pending.vendor}/{pending.app_id} ({len(pending.png_bytes)} bytes)"
        )
    elif isinstance(pending, PendingPublish):
        err.print(
            f"[bold red]PUBLISH[/bold red] /vendors/{pending.vendor}/apps/"
            f"{pending.app_id}/publish (requests Keboola review)"
        )
    elif isinstance(pending, PendingDeprecate):
        err.print(
            f"[bold red]DEPRECATE[/bold red] /vendors/{pending.vendor}/apps/"
            f"{pending.app_id}/deprecate (hides app, blocks new configs)"
        )


def _pending_as_json(pending) -> dict:
    """Serialise a pending write for --json --dry-run output."""
    from dataclasses import asdict
    raw = asdict(pending)
    if "png_bytes" in raw:
        raw["png_bytes"] = f"<{len(raw['png_bytes'])} bytes>"
    if "png_path" in raw:
        raw["png_path"] = str(raw["png_path"])
    return {"status": "dry-run", "pending": raw}


@dev_portal_app.command("create")
def create_cmd(
    ctx: typer.Context,
    vendor: str = typer.Option(..., "--vendor"),
    data: str = typer.Option(..., "--data", help="Path to JSON payload, or '-' for stdin"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    try:
        pending = svc.prepare_create(alias, vendor, _load_payload(data))
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"create app in vendor '{vendor}'")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", "created": result})


@dev_portal_app.command("patch")
def patch_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    data: str | None = typer.Option(None, "--data"),
    property_: str | None = typer.Option(None, "--property"),
    value: str | None = typer.Option(None, "--value"),
    value_file: str | None = typer.Option(None, "--value-file"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)

    if data:
        payload = _load_payload(data)
    elif property_:
        if value_file:
            raw = Path(value_file).read_text()
        elif value is not None:
            raw = value
        else:
            raise typer.BadParameter("--property requires --value or --value-file")
        try:
            parsed = json.loads(raw) if raw.strip()[:1] in "[{" else raw
        except json.JSONDecodeError:
            parsed = raw
        payload = {property_: parsed}
    else:
        raise typer.BadParameter("Provide --data, or --property with --value/--value-file")

    try:
        pending = svc.prepare_patch(alias, vendor, app_id, payload)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"patch {app}")
    try:
        svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({
        "status": "ok",
        "app": app,
        "patched_keys": [d.key for d in pending.diff],
    })


@dev_portal_app.command("upload-icon")
def upload_icon_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    file: str = typer.Option(..., "--file"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        pending = svc.prepare_upload_icon(alias, vendor, app_id, file)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"upload icon for {app}")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result)


@dev_portal_app.command("publish")
def publish_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        pending = svc.prepare_publish(alias, vendor, app_id)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"publish {app}")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", "published": result})


@dev_portal_app.command("deprecate")
def deprecate_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        pending = svc.prepare_deprecate(alias, vendor, app_id)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"deprecate {app}")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", "deprecated": result})
```

- [ ] **Step 4: Run, expect pass**

Run: `uv run pytest tests/test_dev_portal_cli.py -v`
Expected: all tests pass (including write-command non-TTY exit-6 and --dry-run).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/keboola_agent_cli/commands/dev_portal.py tests/test_dev_portal_cli.py
uv run ruff format src/keboola_agent_cli/commands/dev_portal.py tests/test_dev_portal_cli.py
git add src/keboola_agent_cli/commands/dev_portal.py tests/test_dev_portal_cli.py
git commit -m "feat(dev-portal): write commands gated by random-code confirm"
```

---

## Task 15: E2E test + version bump + changelog + all rule #17 doc surfaces

**Files:**
- Modify: `tests/test_e2e.py`
- Modify: `pyproject.toml`
- Modify: `src/keboola_agent_cli/changelog.py`
- Modify: `src/keboola_agent_cli/commands/context.py`
- Modify: `CLAUDE.md`
- Modify: `plugins/kbagent/agents/keboola-expert.md`
- Modify: `plugins/kbagent/skills/kbagent/SKILL.md`
- Modify: `plugins/kbagent/skills/kbagent/references/commands-reference.md`
- Modify: `plugins/kbagent/skills/kbagent/references/gotchas.md`
- Create: `plugins/kbagent/skills/kbagent/references/dev-portal-workflow.md`

- [ ] **Step 1: Add E2E test**

Append to `tests/test_e2e.py`:

```python
class TestDevPortalE2E:
    def test_identity_list_smoke(self, e2e_runner):
        """Unconditional smoke: dev-portal identity list must not crash."""
        result = e2e_runner.invoke(["--json", "dev-portal", "identity", "list"])
        assert result.exit_code == 0

    @pytest.mark.skipif(
        not (os.environ.get("E2E_DP_USERNAME") and os.environ.get("E2E_DP_PASSWORD")),
        reason="Set E2E_DP_USERNAME and E2E_DP_PASSWORD to run real-portal test",
    )
    def test_list_apps_against_real_portal(self, e2e_runner, tmp_path):
        """Optional: list apps for vendor 'keboola' if creds supplied."""
        result = e2e_runner.invoke([
            "dev-portal", "identity", "add",
            "--alias", "e2e",
            "--username", os.environ["E2E_DP_USERNAME"],
            "--password", os.environ["E2E_DP_PASSWORD"],
            "--vendor", "keboola",
        ])
        assert result.exit_code == 0, result.output
        result = e2e_runner.invoke([
            "--json", "dev-portal", "list",
            "--vendor", "keboola", "--identity", "e2e",
        ])
        assert result.exit_code == 0, result.output
```

(Reuse whatever `e2e_runner` fixture pattern the existing E2E tests use; adapt names to match `tests/test_e2e.py` conventions if different.)

- [ ] **Step 2: Bump version + sync**

Edit `pyproject.toml`:

```toml
version = "0.45.0"
```

Run:

```bash
make version-sync
```

Expected: `plugins/kbagent/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (if it tracks the version) updated.

- [ ] **Step 3: Add changelog entry**

In `src/keboola_agent_cli/changelog.py`, add a `"0.45.0"` block following the existing entry format. Example structure (look at the existing top entry for the exact dict shape):

```python
    "0.45.0": [
        "New: `kbagent dev-portal` command group wraps the Keboola Developer Portal "
        "(apps-api.keboola.com) with multi-identity credential storage (same 0600 "
        "config.json as KB project tokens) and a random-code TTY confirm safety bar "
        "on every write. There is intentionally no `--yes` flag and no env-var "
        "bypass: writes (`create`, `patch`, `upload-icon`, `publish`, `deprecate`) "
        "always require a human at a real terminal. Reads (`identity list`, `list "
        "--vendor`, `get --app`) run freely so agents can research peer components "
        "by composing `list` + `get`. Identities support service accounts (no MFA) "
        "and personal accounts (MFA prompt via /dev/tty).",
        "Refactor: the random-code interactive-confirmation primitive previously "
        "embedded in `commands/permissions.py` is now in `commands/_helpers.py` as "
        "`require_random_code_confirmation()`. `permissions set/reset` and every "
        "dev-portal write share the same implementation. The function now raises "
        "`typer.Exit(EXIT_PERMISSION_DENIED)` on failure instead of returning a "
        "bool, so call sites are one line shorter.",
        "Permission registry: 13 new ops under `dev-portal.*` (`identity-add`/`-edit`/"
        "`-remove` = admin; `identity-list`/`-verify`/`list`/`get` = read; "
        "`identity-use`/`create`/`patch`/`upload-icon` = write; `publish` = admin; "
        "`deprecate` = destructive). `--deny-writes` blocks every dev-portal write "
        "automatically.",
    ],
```

- [ ] **Step 4: Update `AGENT_CONTEXT`**

In `src/keboola_agent_cli/commands/context.py`, find the `AGENT_CONTEXT` string and append a `dev-portal` section that describes:

- The safety contract: agents can call read commands and any `--dry-run` directly; writes (`create`, `patch`, `upload-icon`, `publish`, `deprecate`) always require a human to type a random code into a TTY.
- The peer-research pattern: use `list --vendor` then `get --app VENDOR.APP_ID` to compare configurations from existing components.
- Identity selection: `--identity <alias>`, or default via `dev-portal identity use`.

Keep the tone consistent with the existing sections; ~10 lines.

- [ ] **Step 5: Update `CLAUDE.md` `## All CLI Commands`**

In `CLAUDE.md`, find the `## All CLI Commands` section and add (alphabetically near `data-app` or in a logical position):

```
kbagent dev-portal identity add --alias A --username U [--password P | --password-stdin]
                                [--role-hint vendor|admin] [--vendor V] [--portal-url URL]
kbagent dev-portal identity list
kbagent dev-portal identity remove --alias A
kbagent dev-portal identity edit --alias A [--username U] [--password P|--password-stdin]
                                 [--role-hint H] [--vendor V] [--new-alias N]
kbagent dev-portal identity use ALIAS
kbagent dev-portal identity current
kbagent dev-portal identity verify [--identity A]

kbagent dev-portal list --vendor V [--identity A]
kbagent dev-portal get --app VENDOR.APP_ID [--identity A]

kbagent dev-portal create --vendor V --data FILE [--identity A] [--dry-run]
kbagent dev-portal patch --app VENDOR.APP_ID (--data FILE | --property KEY (--value V | --value-file F))
                         [--identity A] [--dry-run]
kbagent dev-portal upload-icon --app VENDOR.APP_ID --file PATH [--identity A] [--dry-run]
kbagent dev-portal publish --app VENDOR.APP_ID [--identity A] [--dry-run]
kbagent dev-portal deprecate --app VENDOR.APP_ID [--identity A] [--dry-run]
# All writes require an interactive random-code TTY confirm; no --yes / no env bypass.
```

- [ ] **Step 6: Update `plugins/kbagent/agents/keboola-expert.md`**

(a) Rule 6 VERSION GATE — bump the minimum version referenced in the rule to `0.45.0` so the agent knows `dev-portal` is available.

(b) Tool-selection matrix — add a row:

```
| User mentions Developer Portal, apps-api, register app, vendor app, ui-options, encryption, defaultBucket, app icon, configurationSchema in portal, publish/deprecate component | `kbagent dev-portal …` | Always prepare writes with `--dry-run` first; never attempt to `--yes` a write — there is no such flag, and writes refuse on non-TTY (exit 6). |
```

(c) Inline gotcha — add: "Developer Portal writes are direct production. The agent's job ends at `--dry-run` + showing the preview; the human types the confirm code."

- [ ] **Step 7: Update `plugins/kbagent/skills/kbagent/SKILL.md` decision-table**

Add a row:

```
| manage portal property / register new component in portal | `kbagent dev-portal …` | see `references/dev-portal-workflow.md` |
```

- [ ] **Step 8: Update `plugins/kbagent/skills/kbagent/references/commands-reference.md`**

Add a new section "### dev-portal" listing all commands with one-line descriptions. Mirror the formatting of the existing sections (e.g. "### data-app").

- [ ] **Step 9: Update `plugins/kbagent/skills/kbagent/references/gotchas.md`**

Add a new entry:

```markdown
### Developer Portal: writes require a human, no exceptions (since v0.45.0)

`kbagent dev-portal {create,patch,upload-icon,publish,deprecate}` always print
the request preview and then require the user to type a random hex code on a
real terminal. There is no `--yes` flag. There is no env-var override. The
command exits 6 (`EXIT_PERMISSION_DENIED`) on a non-TTY shell.

For agentic use: stop at the preview. Use `--dry-run` to get a clean
exit-0 preview you can show the user. Then ask the user to run the same
command without `--dry-run` themselves.

Reads (`dev-portal list`, `dev-portal get`) are unrestricted — peer-research
patterns ("show me how MySQL and Postgres extractors configure themselves")
are agent-friendly via `list --vendor` + `get --app`.
```

- [ ] **Step 10: Create `plugins/kbagent/skills/kbagent/references/dev-portal-workflow.md`**

Create the file with the workflow doc:

```markdown
# Developer Portal workflow

> Audience: a Keboola component developer or a kbagent agent acting on their
> behalf. Goal: safely register, inspect, and update components in the
> Keboola Developer Portal (`apps-api.keboola.com`).

## Identity model

Developer Portal logins are email + password (with MFA on personal
accounts). kbagent stores identities per-alias in the same `config.json`
as KB project tokens, under 0600 protection:

```
kbagent dev-portal identity add --alias vendor-keboola --username service.keboola.xxxxx --password ... --vendor keboola
kbagent dev-portal identity add --alias vendor-kds     --username service.kds-team.xxxxx --password ... --vendor kds-team
kbagent dev-portal identity add --alias admin-foo      --username admin@keboola.com --password-stdin
kbagent dev-portal identity use vendor-keboola         # default for subsequent commands
```

Service accounts (`service.{vendor}.{id}`) skip MFA. Personal admin
accounts prompt for the MFA code on /dev/tty at login time.

## Safety contract (read this before issuing any write)

- Reads are free: `dev-portal list`, `dev-portal get`.
- Writes (`create`, `patch`, `upload-icon`, `publish`, `deprecate`) always:
  1. Print the exact pending request to stderr (full diff for `patch`).
  2. Require the user to type a random hex code into the TTY.
  3. Exit 6 on a non-TTY shell.
- There is no `--yes`. There is no env-var bypass. By design.
- `--dry-run` prints the same preview and exits 0 without prompting. This
  is the agent-safe path.

## The loop

1. Identify the component (vendor + app id). For an existing repo, check
   `.github/workflows/*.yml` for `KBC_DEVELOPERPORTAL_VENDOR` and `KBC_DEVELOPERPORTAL_APP`.
2. `kbagent --json dev-portal list --vendor <V>` and/or
   `kbagent --json dev-portal get --app VENDOR.APP_ID` to inspect.
3. Build a payload file (a JSON file — never inline JSON, shell quoting
   is unsafe with portal property names that contain spaces).
4. `kbagent dev-portal patch --app VENDOR.APP_ID --data /tmp/p.json --dry-run`
   — print the diff, show it to the user.
5. The user runs the same command without `--dry-run` and types the code.

## Peer-config research

Designing a new component? Pull reference configurations from existing
peers:

```
# List candidates
kbagent --json dev-portal list --vendor keboola | jq '.[] | select(.type=="extractor") | .id'

# Pull two peers in full
kbagent --json dev-portal get --app keboola.ex-db-mysql > /tmp/peer-mysql.json
kbagent --json dev-portal get --app keboola.ex-db-pgsql > /tmp/peer-postgres.json
```

Compare them yourself — the agent has the reasoning ability to spot
patterns. No dedicated `peers` command needed.

## Boundaries (what this surface does NOT own)

- Image push to ECR — stays in component GitHub Actions.
- Bulk repo-file -> property sync on deploy — stays in
  `scripts/developer_portal/update_properties.sh` (Cookiecutter-backed files).
- Writes to `component_config/` — never. That directory is governed by the
  Cookiecutter template; portal-direct properties (`uiOptions`,
  `encryption`, `defaultBucket`, …) live only in the portal.
```

- [ ] **Step 11: Run full test suite + CI checks**

```bash
uv run pytest tests/ -q
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
make changelog-check
```

Expected: all pass.

- [ ] **Step 12: Commit doc-sync as one atomic commit**

```bash
git add CLAUDE.md tests/test_e2e.py pyproject.toml plugins/ src/keboola_agent_cli/changelog.py src/keboola_agent_cli/commands/context.py plugins/kbagent/.claude-plugin/plugin.json .claude-plugin/marketplace.json 2>/dev/null || true
git status
# Verify only the expected files are staged; nothing else.
git commit -m "feat(dev-portal): version 0.45.0, E2E test, AGENT_CONTEXT, plugin docs"
```

---

## Task 16: Final cross-check + push + PR

**Files:** none (workflow only)

- [ ] **Step 1: Re-run the full check pipeline**

```bash
make check
```

Expected: passes (lint + format + changelog + test).

- [ ] **Step 2: Smoke the CLI manually**

```bash
uv run kbagent dev-portal --help
uv run kbagent dev-portal identity --help
uv run kbagent dev-portal patch --help
```

Expected: help text shows all subcommands and flags as designed.

- [ ] **Step 3: Push branch + open PR**

```bash
git push -u origin feat/dev-portal
gh pr create --title "feat(dev-portal): kbagent Developer Portal support with no-bypass write safety" \
  --body "$(cat <<'EOF'
## Summary
- Adds `kbagent dev-portal` command group wrapping apps-api.keboola.com.
- Multi-identity credential storage (mirrors KB project tokens).
- Writes always require a random-code TTY confirm — no `--yes`, no env bypass.
- v1 ops: identity CRUD, list, get, create, patch, upload-icon, publish, deprecate.

## Test plan
- [ ] `make check` passes
- [ ] `uv run pytest tests/test_dev_portal_client.py tests/test_dev_portal_service.py tests/test_dev_portal_cli.py -v` passes
- [ ] `uv run pytest tests/test_helpers.py::TestRequireRandomCodeConfirmation -v` passes
- [ ] Manual: `kbagent dev-portal patch --app foo.bar --data /tmp/p.json` on a non-TTY exits 6
- [ ] Manual: `kbagent dev-portal patch --app foo.bar --data /tmp/p.json --dry-run` exits 0

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

### 1. Spec coverage

Every spec section maps to a task:

- **Data model & storage** (spec §Architecture > Data model) → Tasks 4, 5.
- **`DeveloperPortalClient`** (spec §Architecture > Client layer) → Tasks 6, 7, 8, 9.
- **`DeveloperPortalService`** (spec §Architecture > Service layer) → Tasks 10, 11.
- **Command layer + write safety** (spec §Architecture > Command layer) → Tasks 13, 14 (Task 14 explicitly tests non-TTY exit-6 and `--dry-run` no-confirm).
- **CLI wiring & permission registry** (spec §CLI wiring) → Tasks 12, 13.
- **Security** (spec §Security) → covered by Tasks 5 (`_warning` extension), 6/7 (bearer-only-in-memory in client construction), 14 (random-code confirm, no env bypass).
- **Testing layout** (spec §Testing) → every layer has its own test file; `tests/test_helpers.py`, `tests/test_config_store.py`, `tests/test_permissions.py` extensions covered.
- **Documentation sync** (spec §Documentation sync) → Task 15 enumerates every silent-drift surface.
- **Out of scope** (spec §Out of scope) → preserved by not having tasks for `dev-portal sync`, `peers`, or audit log.
- **Open questions** (spec §Open questions) → PNG dimension check resolved as soft-warn in Task 11; identity `vendor` field defaults handled in command layer (Task 13 — identity-add requires --vendor explicitly per call).

### 2. Placeholder scan

- No "TBD" / "TODO" / "fill in details" / "similar to Task N".
- Every code step shows complete code.
- Every test step shows complete tests.
- Every command step shows the exact command.

### 3. Type consistency

- `DeveloperPortalIdentity` fields (`username`, `password`, `role_hint`, `vendor`, `portal_url`) used consistently in Tasks 4, 5, 6, 10, 13.
- `PendingPatch.diff` is `list[FieldDiff]` everywhere (Tasks 11, 14).
- `require_random_code_confirmation(action_description: str) -> None` (raises on failure) — signature consistent across Tasks 3, 14.
- `ErrorCode` entries defined in Task 2 and used by name in Tasks 6, 7, 8, 9, 10, 11.
- `dev_portal_app` Typer instance name consistent in Tasks 13 (creation), 13 (registration in cli.py).
- `_split_app()` helper defined once in Task 13, used in Task 14.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-28-dev-portal.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
