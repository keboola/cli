"""CI guard: a `kbc-session://` sentinel must not slip through unguarded.

Three kinds of drift ship silently, and all three happened during 0.77.0
development. Each check below is deliberately narrow -- a broad "does this file
mention a token" grep flags every service that correctly hands
`project.token` to its injected, bearer-aware client factory, which is noise
rather than a finding.

1. **A project's credential type gets swapped without anyone deciding.**
   Writing a token into a `config.json` entry can replace a
   `kbc-session://{project_id}` sentinel with a static Storage token, which
   silently converts a browser-login project into a permanent credential and
   makes it survive `auth logout --remove-projects`. Any caller of
   `add_project` / `edit_project` that passes a token must be sentinel-aware.

2. **A new HTTP client forgets to decide whether it supports sessions.**
   `BaseHttpClient.SESSION_AUTH_FEATURE` is the one place that keeps a sentinel
   off the wire. A subclass that neither declares it nor is listed here as
   bearer-capable is an unreviewed decision, not a default.

3. **`SESSION_UNSUPPORTED_FEATURES` rots away from the guards it describes.**
   That tuple is what `auth login` / `auth register-projects` disclose and ship
   as `session_unsupported_features` in `--json`, and every documentation
   surface defers to it. Drift means the CLI states a scope that is not the real
   one -- which is how `dev-portal` came to be listed as restricted although it
   has no guard, while the Scheduler Service and the `sharing` master-token path
   were real restrictions named nowhere.

Usage (run from repo root):
    python scripts/check_sentinel_guards.py          # exits 1 on violations
    python scripts/check_sentinel_guards.py --list   # print the guard inventory
"""

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "keboola_agent_cli"

FEATURES_PATH = SRC_ROOT / "services" / "_auth_registration.py"
FEATURES_NAME = "SESSION_UNSUPPORTED_FEATURES"

# Any one of these proves a file knows a project token may be a sentinel.
SENTINEL_AWARE_NAMES = (
    "is_session_token",
    "require_static_token",
    "parse_session_project_id",
    "make_session_token",
    "allow_credential_type_change",
    "SessionAuthUnsupportedError",
)

# Check 1: writers of a project credential. Exempt because they define the
# chokepoint itself, or because they only ever write a sentinel.
CREDENTIAL_WRITE_EXEMPT = {
    "config_store.py",  # owns the guard
    "services/auth_service.py",  # writes sentinels, never a static token
    "services/_auth_registration.py",
}
CREDENTIAL_WRITERS = {"add_project", "edit_project"}

# Check 2: clients that reach Storage or Manage over bearer, or authenticate
# with an identity of their own, and so must NOT be guarded. Anything else must
# declare SESSION_AUTH_FEATURE.
BEARER_CAPABLE_CLIENTS = {
    "_CoreClient",  # Storage + Queue: the supported bearer path
    "ManageClient",  # Manage: the supported bearer path
    "AuthClient",  # talks to the auth service itself, token=""
    "DeveloperPortalClient",  # own username/password identity, never a project token
}

# Check 3: guards that describe no user-reachable command surface.
FEATURE_EXEMPT_GUARDS = {
    "The static-token Storage API client",  # the static branch of the seam
}

# Guard wording -> the substring of the user-facing entry that covers it, so
# both can read naturally without being byte-identical.
FEATURE_ALIASES = {
    "The Keboola AI Service": "AI Service",
    "The Scheduler Service": "Scheduler Service",
    "The Metastore Service (semantic layer)": "Metastore Service",
    "semantic-layer token --encrypt": "Metastore Service",
    "The Data Science Service (data apps)": "Data Science Service",
    "The Data Streams Service": "Data Streams Service",
    "The MCP server subprocess": "MCP server subprocess",
    "The MCP HTTP transport": "MCP server subprocess",
    "The importable SDK Client": "importable SDK",
    "kbagent kai": "kbagent kai",
    "kbagent sharing (master-token path)": "kbagent sharing",
}


def _py_files() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _receiver_source(node: ast.Call) -> str:
    """Rendered receiver of a method call, e.g. ``self._config_store``.

    `ProjectService` exposes `add_project` / `edit_project` of its own, and those
    ARE the guarded layer, so only calls landing on a config store count.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        try:
            return ast.unparse(func.value)
        except AttributeError:
            return ""
    return ""


CONFIG_STORE_RECEIVERS = ("config_store", "store", "_store")


def _class_attr_string(cls: ast.ClassDef, name: str) -> str | None:
    for item in cls.body:
        if isinstance(item, ast.AnnAssign):
            targets = [item.target]
        elif isinstance(item, ast.Assign):
            targets = list(item.targets)
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
            return item.value.value
    return None


def _guard_features() -> dict[str, list[str]]:
    """Every `require_static_token(feature=...)` and `SESSION_AUTH_FEATURE`."""
    found: dict[str, list[str]] = {}
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "require_static_token":
                for kw in node.keywords:
                    if kw.arg == "feature" and isinstance(kw.value, ast.Constant):
                        found.setdefault(str(kw.value.value), []).append(_rel(path))
            elif isinstance(node, ast.ClassDef):
                feature = _class_attr_string(node, "SESSION_AUTH_FEATURE")
                if feature:
                    found.setdefault(feature, []).append(_rel(path))
    return found


def _declared_features() -> list[str]:
    tree = ast.parse(FEATURES_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == FEATURES_NAME for t in targets):
            continue
        if isinstance(node.value, ast.Tuple):
            return [
                el.value
                for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
    return []


def _unguarded_credential_writers() -> list[str]:
    """Check 1: files that write a token into a project entry unaware of sentinels."""
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        if rel in CREDENTIAL_WRITE_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        if any(name in text for name in SENTINEL_AWARE_NAMES):
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in CREDENTIAL_WRITERS:
                continue
            receiver = _receiver_source(node)
            if not any(marker in receiver for marker in CONFIG_STORE_RECEIVERS):
                continue
            if any(kw.arg == "token" for kw in node.keywords):
                offenders.append(f"{rel}:{node.lineno}")
    return offenders


def _undecided_clients() -> list[str]:
    """Check 2: BaseHttpClient subclasses that never decided about sessions."""
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
            if "BaseHttpClient" not in bases:
                continue
            if node.name in BEARER_CAPABLE_CLIENTS:
                continue
            if _class_attr_string(node, "SESSION_AUTH_FEATURE") is None:
                offenders.append(f"{_rel(path)}:{node.lineno} {node.name}")
    return offenders


def main() -> int:
    guards = _guard_features()
    declared = _declared_features()

    if "--list" in sys.argv:
        print(f"{FEATURES_NAME} ({len(declared)} entries):")
        for entry in declared:
            print(f"  - {entry}")
        print(f"\nGuards ({len(guards)}):")
        for feature, files in sorted(guards.items()):
            print(f"  - {feature}  <- {', '.join(sorted(set(files)))}")
        print(f"\nBearer-capable clients (deliberately unguarded): {len(BEARER_CAPABLE_CLIENTS)}")
        for name in sorted(BEARER_CAPABLE_CLIENTS):
            print(f"  - {name}")
        return 0

    problems: list[str] = []

    writers = _unguarded_credential_writers()
    if writers:
        problems.append(
            "These call sites write a token into a config.json project entry without\n"
            "mentioning a sentinel helper, so they can silently replace a browser-login\n"
            "credential with a static one:\n"
            + "\n".join(f"    src/keboola_agent_cli/{w}" for w in writers)
            + "\n\nPass `allow_credential_type_change=True` if the conversion is what the\n"
            "user asked for, or skip session projects before you get here."
        )

    undecided = _undecided_clients()
    if undecided:
        problems.append(
            "These BaseHttpClient subclasses neither declare SESSION_AUTH_FEATURE nor\n"
            "appear in BEARER_CAPABLE_CLIENTS in this script, so nobody decided whether\n"
            "they accept a browser-login session:\n"
            + "\n".join(f"    src/keboola_agent_cli/{u}" for u in undecided)
            + "\n\nDeclare the feature name to reject a sentinel on construction, or add the\n"
            "class to BEARER_CAPABLE_CLIENTS with a comment saying why it is safe."
        )

    if not declared:
        problems.append(f"Could not parse {FEATURES_NAME} from {FEATURES_PATH}.")
    else:
        haystack = "\n".join(declared)
        uncovered = [
            f"{feature}  (guarded in {', '.join(sorted(set(files)))})"
            for feature, files in sorted(guards.items())
            if feature not in FEATURE_EXEMPT_GUARDS
            and FEATURE_ALIASES.get(feature, feature) not in haystack
        ]
        if uncovered:
            problems.append(
                f"These guards are not covered by {FEATURES_NAME}, so `auth login` and\n"
                "`auth register-projects` understate the restrictions they disclose:\n"
                + "\n".join(f"    {u}" for u in uncovered)
                + f"\n\nAdd an entry to {FEATURES_PATH.relative_to(REPO_ROOT)}, or map the\n"
                "wording via FEATURE_ALIASES in this script."
            )

    if problems:
        print("FAIL: sentinel guard check\n")
        print("\n\n".join(problems))
        return 1

    print(
        f"OK: no unguarded credential writes; every BaseHttpClient subclass decided; "
        f"all {len(guards)} guards covered by {FEATURES_NAME} ({len(declared)} entries)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
