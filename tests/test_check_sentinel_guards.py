"""Tests for scripts/check_sentinel_guards.py -- the sentinel-drift CI gate.

The gate is the safety net for the whole session-credential design: it rejects a
`config.json` credential write that is not sentinel-aware, a Storage client built
straight from `project.token`, a `BaseHttpClient` subclass that never decided
whether it accepts a session, and a `require_static_token` guard missing from
`SESSION_UNSUPPORTED_FEATURES`.

Following the convention its sibling gate's tests state, these pin two things:

  1. The committed repo is CLEAN -- otherwise the gate would block every PR.
  2. Each check actually DETECTS its drift class on synthetic inputs, including
     the credential-passing shapes this codebase really uses (a model object, a
     `**kwargs` unpacking, a positional argument) rather than only a literal
     ``token=`` keyword.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load scripts/check_sentinel_guards.py as a module without installing it
# (mirrors tests/test_check_command_sync.py).
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "_check_sentinel_guards_under_test",
    SCRIPTS_DIR / "check_sentinel_guards.py",
)
assert SPEC is not None and SPEC.loader is not None
_mod = importlib.util.module_from_spec(SPEC)
sys.modules["_check_sentinel_guards_under_test"] = _mod
SPEC.loader.exec_module(_mod)


def _tree(root: Path, **files: str) -> Path:
    """Write a synthetic `src/keboola_agent_cli`-shaped tree and return its root.

    Keys are relative posix paths (`services/foo.py`); values are module source.
    """
    for rel, source in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Live-tree integration -- the current repo must pass
# --------------------------------------------------------------------------


def test_live_tree_is_clean(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red `main()` would block every PR, so the committed tree must be clean."""
    monkeypatch.setattr(sys, "argv", ["check_sentinel_guards.py"])
    assert _mod.main() == 0
    assert "OK:" in capsys.readouterr().out


def test_list_flag_prints_the_inventory(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_sentinel_guards.py", "--list"])
    assert _mod.main() == 0
    out = capsys.readouterr().out
    assert _mod.FEATURES_NAME in out
    assert "Bearer-capable clients" in out


def test_every_allowlisted_write_scope_still_exists() -> None:
    """An allowlist entry naming a function that was renamed away is a silent hole."""
    for scope in _mod.CREDENTIAL_WRITE_ALLOWED:
        rel, function = scope.split("::")
        source = (_mod.SRC_ROOT / rel).read_text(encoding="utf-8")
        assert f"def {function}(" in source, f"{scope} names a function that no longer exists"


# --------------------------------------------------------------------------
# Check 1 -- unguarded credential writes
# --------------------------------------------------------------------------

_GUARDED_WRITE = """
from ..auth.sentinel import is_session_token


def refresh(self, alias, token):
    if is_session_token(self._config_store.get_project(alias).token):
        return
    self._config_store.edit_project(alias, token=token)
"""

_UNGUARDED_KEYWORD_WRITE = """
def refresh(self, alias, token):
    self._config_store.edit_project(alias, token=token)
"""

_UNGUARDED_KWARGS_WRITE = """
def refresh(self, alias, updates):
    self._config_store.edit_project(alias, **updates)
"""

_UNGUARDED_MODEL_WRITE = """
def register(self, alias, url, token):
    self._config_store.add_project(alias, ProjectConfig(stack_url=url, token=token))
"""

_UNGUARDED_POSITIONAL_WRITE = """
def refresh(self, alias, token):
    self._config_store.edit_project(alias, token)
"""


class TestUnguardedCredentialWrites:
    def test_a_guarded_write_is_not_flagged(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, **{"services/ok_service.py": _GUARDED_WRITE})
        assert _mod._unguarded_credential_writers(root) == []

    @pytest.mark.parametrize(
        ("label", "source"),
        [
            ("token= keyword", _UNGUARDED_KEYWORD_WRITE),
            ("**kwargs unpacking", _UNGUARDED_KWARGS_WRITE),
            ("token inside a model object", _UNGUARDED_MODEL_WRITE),
            ("positional token", _UNGUARDED_POSITIONAL_WRITE),
        ],
    )
    def test_every_credential_passing_shape_is_detected(
        self, tmp_path: Path, label: str, source: str
    ) -> None:
        """`kw.arg == "token"` saw only the first of these four."""
        root = _tree(tmp_path, **{"services/bad_service.py": source})
        offenders = _mod._unguarded_credential_writers(root)
        assert len(offenders) == 1, f"{label} went undetected"
        assert "services/bad_service.py" in offenders[0]

    def test_a_guard_elsewhere_in_the_file_does_not_excuse_a_second_write(
        self, tmp_path: Path
    ) -> None:
        """The file-wide text exemption passed this whole file."""
        root = _tree(
            tmp_path,
            **{"services/mixed_service.py": _GUARDED_WRITE + _UNGUARDED_KEYWORD_WRITE},
        )
        offenders = _mod._unguarded_credential_writers(root)
        assert len(offenders) == 1
        assert "(in refresh)" in offenders[0]

    def test_a_guard_in_an_outer_scope_covers_a_nested_write(self, tmp_path: Path) -> None:
        """`make_client_factory`'s pattern: decide, then return a closure."""
        source = """
from ..auth.sentinel import is_session_token


def make_updater(self, alias):
    if is_session_token(self._config_store.get_project(alias).token):
        raise RuntimeError("session project")

    def _update(token):
        self._config_store.edit_project(alias, token=token)

    return _update
"""
        root = _tree(tmp_path, **{"services/closure_service.py": source})
        assert _mod._unguarded_credential_writers(root) == []

    def test_a_write_to_something_other_than_a_config_store_is_ignored(
        self, tmp_path: Path
    ) -> None:
        """`ProjectService.edit_project` IS the guarded layer; its callers are not writes."""
        source = """
def cmd(service, alias, token):
    service.edit_project(alias=alias, token=token)
"""
        root = _tree(tmp_path, **{"commands/project.py": source})
        assert _mod._unguarded_credential_writers(root) == []

    def test_exempt_files_are_skipped(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, **{"config_store.py": _UNGUARDED_KEYWORD_WRITE})
        assert _mod._unguarded_credential_writers(root) == []

    @pytest.mark.parametrize(
        ("label", "init"),
        [
            ("annotated parameter", "def __init__(self, config_store: ConfigStore) -> None:"),
            ("unannotated parameter", "def __init__(self, config_store):"),
        ],
    )
    def test_a_config_store_held_under_an_unconventional_name_is_still_seen(
        self, tmp_path: Path, label: str, init: str
    ) -> None:
        """`CONFIG_STORE_RECEIVERS` is a naming convention, not a fact about the object.

        A future `self._cfg = config_store` is an ordinary naming choice, not an
        attempt to evade the gate, and byte-identical unsafe logic must not become
        invisible because of it.
        """
        source = f"""
class NewService:
    {init}
        self._cfg = config_store

    def refresh(self, alias, token):
        self._cfg.edit_project(alias, token=token)
"""
        root = _tree(tmp_path, **{"services/new_service.py": source})
        offenders = _mod._unguarded_credential_writers(root)
        assert len(offenders) == 1, f"{label} went undetected"
        assert "(in refresh)" in offenders[0]

    def test_an_unconventionally_named_store_that_is_guarded_stays_clean(
        self, tmp_path: Path
    ) -> None:
        source = """
from ..auth.sentinel import is_session_token


class NewService:
    def __init__(self, config_store: ConfigStore) -> None:
        self._cfg = config_store

    def refresh(self, alias, token):
        if is_session_token(token):
            return
        self._cfg.edit_project(alias, token=token)
"""
        root = _tree(tmp_path, **{"services/new_service.py": source})
        assert _mod._unguarded_credential_writers(root) == []


# --------------------------------------------------------------------------
# Check 4 -- Storage clients built from a project credential
# --------------------------------------------------------------------------


class TestUnguardedProjectClients:
    def test_a_direct_construction_from_a_project_token_is_detected(self, tmp_path: Path) -> None:
        """The gap Check 2 structurally cannot see: it reads class defs, not call sites."""
        source = """
def _client_for(project):
    return KeboolaClient(stack_url=project.stack_url, token=project.token)
"""
        root = _tree(tmp_path, **{"services/rogue_service.py": source})
        offenders = _mod._unguarded_project_clients(root)
        assert len(offenders) == 1
        assert "(in _client_for)" in offenders[0]

    def test_a_positional_construction_is_detected(self, tmp_path: Path) -> None:
        source = """
def _client_for(project):
    return KeboolaClient(project.stack_url, project.token)
"""
        root = _tree(tmp_path, **{"services/rogue_service.py": source})
        assert len(_mod._unguarded_project_clients(root)) == 1

    def test_a_guarded_construction_is_not_flagged(self, tmp_path: Path) -> None:
        source = """
from .auth.sentinel import require_static_token


def build(stack_url, token):
    require_static_token(token, feature="Something static-only")
    return KeboolaClient(stack_url=stack_url, token=token)
"""
        root = _tree(tmp_path, **{"services/ok_service.py": source})
        assert _mod._unguarded_project_clients(root) == []

    def test_the_bearer_branch_construction_is_not_flagged(self, tmp_path: Path) -> None:
        """`token=""` carries no credential -- the bearer hook does."""
        source = """
def build(stack_url, provider, project_id):
    return KeboolaClient(stack_url=stack_url, token="", http_auth=BearerAuth(provider, project_id))
"""
        root = _tree(tmp_path, **{"services/bearer_service.py": source})
        assert _mod._unguarded_project_clients(root) == []

    def test_an_import_alias_does_not_hide_the_construction(self, tmp_path: Path) -> None:
        """`KeboolaClient as _Storage` still constructs a `KeboolaClient`."""
        root = _tree(
            tmp_path,
            **{
                "client/_client.py": "class KeboolaClient(BaseHttpClient):\n    pass\n",
                "services/rogue_service.py": (
                    "from ..client import KeboolaClient as _Storage\n\n\n"
                    "def _client_for(project):\n"
                    "    return _Storage(project.stack_url, project.token)\n"
                ),
            },
        )
        offenders = _mod._unguarded_project_clients(root)
        assert len(offenders) == 1
        assert "rogue_service.py" in offenders[0]

    def test_a_subclass_construction_is_the_same_finding(self, tmp_path: Path) -> None:
        """A subclass puts the same credential on the same wire."""
        root = _tree(
            tmp_path,
            **{
                "client/_client.py": "class KeboolaClient(BaseHttpClient):\n    pass\n",
                "client/sub.py": "class ProjectStorage(KeboolaClient):\n    pass\n",
                "services/rogue_service.py": (
                    "def _client_for(project):\n"
                    "    return ProjectStorage(project.stack_url, project.token)\n"
                ),
            },
        )
        assert len(_mod._unguarded_project_clients(root)) == 1

    def test_the_shared_base_construction_is_detected(self, tmp_path: Path) -> None:
        """`_CoreClient.__init__` is where the header is written, so it is the real seed.

        Seeding Check 4 with `KeboolaClient` alone left this uncovered:
        `_descendants_of` walks DOWN, and the vulnerable `__init__` lives one level
        UP. Constructing the base directly puts the sentinel on the wire exactly as
        the leaf class would.
        """
        root = _tree(
            tmp_path,
            **{
                "client/_core.py": "class _CoreClient(BaseHttpClient):\n    pass\n",
                "services/rogue_service.py": (
                    "def _client_for(project):\n"
                    "    return _CoreClient(project.stack_url, project.token)\n"
                ),
            },
        )
        assert len(_mod._unguarded_project_clients(root)) == 1

    def test_an_endpoint_family_mixin_construction_is_detected(self, tmp_path: Path) -> None:
        """The ten mixins each run `_CoreClient.__init__`, so each is the same finding."""
        root = _tree(
            tmp_path,
            **{
                "client/_core.py": "class _CoreClient(BaseHttpClient):\n    pass\n",
                "client/stream.py": "class _StreamMixin(_CoreClient):\n    pass\n",
                "services/rogue_service.py": (
                    "def _client_for(project):\n"
                    "    return _StreamMixin(project.stack_url, project.token)\n"
                ),
            },
        )
        assert len(_mod._unguarded_project_clients(root)) == 1

    def test_a_manage_client_is_not_a_project_credential_risk(self, tmp_path: Path) -> None:
        """A manage token is never a sentinel, so its construction is out of scope."""
        source = """
def build(stack_url, manage_token):
    return ManageClient(stack_url=stack_url, manage_token=manage_token)
"""
        root = _tree(tmp_path, **{"services/manage_service.py": source})
        assert _mod._unguarded_project_clients(root) == []


# --------------------------------------------------------------------------
# Check 2 -- clients that never decided about sessions
# --------------------------------------------------------------------------


class TestUndecidedClients:
    def test_a_subclass_without_a_decision_is_detected(self, tmp_path: Path) -> None:
        source = """
class NewThingClient(BaseHttpClient):
    def __init__(self, stack_url, token):
        super().__init__(base_url=stack_url, token=token)
"""
        root = _tree(tmp_path, **{"new_thing_client.py": source})
        offenders = _mod._undecided_clients(root)
        assert len(offenders) == 1
        assert "NewThingClient" in offenders[0]

    def test_declaring_the_feature_settles_it(self, tmp_path: Path) -> None:
        source = """
class NewThingClient(BaseHttpClient):
    SESSION_AUTH_FEATURE = "The New Thing Service"
"""
        root = _tree(tmp_path, **{"new_thing_client.py": source})
        assert _mod._undecided_clients(root) == []

    def test_an_allowlisted_bearer_capable_client_is_not_flagged(self, tmp_path: Path) -> None:
        source = """
class ManageClient(BaseHttpClient):
    pass
"""
        root = _tree(tmp_path, **{"manage_client.py": source})
        assert _mod._undecided_clients(root) == []

    def test_a_bearer_capable_subclass_inherits_the_decision(self, tmp_path: Path) -> None:
        """Behaviour is inherited, so the decision about it is too."""
        root = _tree(
            tmp_path,
            **{
                "client/_core.py": "class _CoreClient(BaseHttpClient):\n    pass\n",
                "client/_client.py": "class KeboolaClient(_CoreClient):\n    pass\n",
                "client/sub.py": "class ProjectStorage(KeboolaClient):\n    pass\n",
            },
        )
        assert _mod._undecided_clients(root) == []

    def test_an_indirect_subclass_of_an_undecided_client_still_needs_a_decision(
        self, tmp_path: Path
    ) -> None:
        """Check 2 read only the immediate base, so a two-step chain slipped past."""
        root = _tree(
            tmp_path,
            **{
                "thing_client.py": (
                    'class ThingClient(BaseHttpClient):\n    SESSION_AUTH_FEATURE = "Thing"\n'
                ),
                "other_client.py": "class OtherClient(ThingClient):\n    pass\n",
            },
        )
        offenders = _mod._undecided_clients(root)
        assert [o for o in offenders if "OtherClient" in o]

    def test_an_unrelated_class_is_ignored(self, tmp_path: Path) -> None:
        source = """
class NotAClient:
    pass
"""
        root = _tree(tmp_path, **{"models.py": source})
        assert _mod._undecided_clients(root) == []
