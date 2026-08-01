"""CI guard: a `kbc-session://` sentinel must not slip through unguarded.

Four kinds of drift ship silently, and the first three happened during 0.81.0
development. Each check below is scoped to a call site or a class definition --
a broad "does this file mention a token" grep flags every service that correctly
hands `project.token` to its injected, bearer-aware client factory, which is
noise rather than a finding.

1. **A project's credential type gets swapped without anyone deciding.**
   Writing a token into a `config.json` entry can replace a
   `kbc-session://{project_id}` sentinel with a static Storage token, which
   silently converts a browser-login project into a permanent credential and
   makes it survive `auth logout --remove-projects`. Any scope calling
   `add_project` / `edit_project` on a config store must be sentinel-aware.

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

4. **A Storage client is constructed straight from `project.token`.**
   Check 2 cannot see this: the clients that legitimately speak bearer leave
   `SESSION_AUTH_FEATURE` unset, so the runtime guard is inert for exactly the
   class a direct `KeboolaClient(stack_url, project.token)` would use --
   bypassing `make_client_factory` and putting the sentinel string on the wire
   as an `X-StorageApi-Token` header.

Known limits -- this is a static check, so state them rather than imply coverage
it does not have. All three are absent from the tree today (verified), and the
runtime guards (`require_static_token`, `make_client_factory`) are the actual
protection; this gate exists to stop a NEW unguarded path from landing quietly.

- A `ConfigStore` reached through indirection (`self._cfg = provider.get_store()`)
  is not recognised as one: `_config_store_holders` resolves an annotation, a
  direct construction, or a parameter name, not an arbitrary call chain.
- A client constructed through a local binding (`ctor = KeboolaClient; ctor(...)`)
  or a factory wrapper is invisible to Check 4, which resolves literal call
  targets and import aliases only.
- Sentinel-awareness is judged by the names appearing in the enclosing function's
  source, so a guard applied in a helper the function calls does not count -- that
  errs towards a false finding, which is the safe direction, and
  `CREDENTIAL_WRITE_ALLOWED` is where such a case gets recorded with a reason.

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

# Check 1 exemptions that are a property of the CALL SITE rather than of the
# function holding it, keyed `<file>::<enclosing function>`. Each needs a reason:
# an entry here is a reviewed decision, and the reason is what a future reader
# checks against the code.
CREDENTIAL_WRITE_ALLOWED = {
    "services/org_service.py::_refresh_single_project": (
        "session projects are filtered out by `is_session_token` in "
        "`refresh_tokens`, before this per-project step runs"
    ),
    "services/org_service.py::_setup_single_project": (
        "`add_project` raises on an existing alias, so it cannot replace a "
        "credential, and the alias is freshly uniquified by `_unique_alias`"
    ),
    "services/project_service.py::add_project": (
        "`add_project` raises on an existing alias, so it cannot replace the "
        "credential of an already-registered session project"
    ),
}

# Check 4: clients constructed with a PROJECT credential, which is the value that
# can be a sentinel. `ManageClient` is bearer-capable too, but its credential is
# a manage token and never a sentinel, so its construction is not a risk here.
#
# Seeded with `_CoreClient`, not `KeboolaClient`: `_descendants_of` walks DOWN the
# hierarchy, while the construction that puts a sentinel on the wire
# (`headers["X-StorageApi-Token"] = token`) lives in `_CoreClient.__init__`, an
# ANCESTOR of `KeboolaClient`. Seeding the leaf left `_CoreClient(...)` and its ten
# endpoint-family mixins -- every one of them a class that runs that same
# `__init__` -- outside the check. `KeboolaClient` stays listed because it is the
# name a reader looks for here; it is already covered as a descendant.
PROJECT_CREDENTIAL_CLIENTS = {"_CoreClient", "KeboolaClient"}

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


def _py_files(root: Path = SRC_ROOT) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path, root: Path = SRC_ROOT) -> str:
    return path.relative_to(root).as_posix()


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

# Type name whose holders count as a config store however the attribute is spelled.
CONFIG_STORE_TYPE = "ConfigStore"


def _alias_map(tree: ast.AST) -> dict[str, str]:
    """Local name -> original name for every import in the module.

    Check 4 compares a constructor call against a set of class names, so an
    ``import ... as`` would otherwise hide the construction behind a name the set
    does not contain: `KeboolaClient as _Storage` is still a `KeboolaClient`.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for name in node.names:
                if name.asname:
                    aliases[name.asname] = name.name.rsplit(".", 1)[-1]
    return aliases


def _config_store_holders(tree: ast.AST) -> set[str]:
    """Rendered receivers known to hold a `ConfigStore`, by type rather than by name.

    A receiver whose attribute name happens not to contain "store"
    (`self._cfg = config_store`) is invisible to `CONFIG_STORE_RECEIVERS`, which
    is a naming convention, not a fact about the object. This resolves the two
    ways the codebase actually acquires one: a parameter annotated `ConfigStore`,
    and an attribute assigned from such a parameter or from a direct
    `ConfigStore(...)` construction.
    """
    holders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            typed_params = {
                arg.arg
                for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                if isinstance(arg.annotation, ast.Name | ast.Constant)
                and CONFIG_STORE_TYPE in ast.unparse(arg.annotation)
            }
            holders |= typed_params
            for stmt in ast.walk(node):
                if not isinstance(stmt, ast.Assign):
                    continue
                value = stmt.value
                from_param = isinstance(value, ast.Name) and (
                    value.id in typed_params
                    # An unannotated parameter still betrays itself by name; the
                    # repo annotates everything, but the check must not depend on
                    # that to notice a credential write.
                    or any(marker in value.id for marker in CONFIG_STORE_RECEIVERS)
                )
                from_construction = (
                    isinstance(value, ast.Call) and _call_name(value) == CONFIG_STORE_TYPE
                )
                if not (from_param or from_construction):
                    continue
                for target in stmt.targets:
                    holders.add(ast.unparse(target))
    return holders


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """child -> parent for the whole tree; `ast` keeps no upward links."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_functions(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    """Functions containing `node`, innermost first."""
    functions: list[ast.AST] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append(current)
        current = parents.get(current)
    return functions


def _scope_is_sentinel_aware(node: ast.AST, parents: dict[ast.AST, ast.AST], source: str) -> bool:
    """True when a function containing `node` proves it knows about sentinels.

    Scoped to the enclosing functions, NOT the whole file: a file-wide text
    search passes any file that guards one call site correctly while leaving a
    second one wide open. An outer function counts as well as the innermost --
    `make_client_factory` deciding before it returns its closure is the correct
    pattern, not a miss.

    A call outside any function falls back to the module text; there are none
    today, and the fallback errs towards accepting rather than inventing a
    finding about module-level code that no check was designed for.
    """
    scopes = [
        ast.get_source_segment(source, fn) or "" for fn in _enclosing_functions(node, parents)
    ]
    return any(name in text for text in scopes or [source] for name in SENTINEL_AWARE_NAMES)


def _enclosing_function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    functions = _enclosing_functions(node, parents)
    return getattr(functions[0], "name", "<module>") if functions else "<module>"


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


def _unguarded_credential_writers(root: Path = SRC_ROOT) -> list[str]:
    """Check 1: project-entry writes whose own scope is unaware of sentinels.

    Every call to a config-store writer counts, whatever shape the credential
    arrives in. Keying on a literal ``token=`` keyword misses three shapes the
    codebase actually uses: `add_project(alias, ProjectConfig(...))` carries the
    token inside the model, `edit_project(alias, **updates)` hides it in a dict
    unpacking (`kw.arg` is None), and a positional argument has no keyword at
    all. Since the credential-carrying shape cannot be told apart reliably, the
    scope has to prove it thought about sentinels -- or be listed in
    `CREDENTIAL_WRITE_ALLOWED` with a reason.
    """
    offenders = []
    for path in _py_files(root):
        rel = _rel(path, root)
        if rel in CREDENTIAL_WRITE_EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = _parent_map(tree)
        holders = _config_store_holders(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in CREDENTIAL_WRITERS:
                continue
            receiver = _receiver_source(node)
            named_like_a_store = any(marker in receiver for marker in CONFIG_STORE_RECEIVERS)
            if not named_like_a_store and receiver not in holders:
                continue
            if _scope_is_sentinel_aware(node, parents, source):
                continue
            scope = f"{rel}::{_enclosing_function_name(node, parents)}"
            if scope in CREDENTIAL_WRITE_ALLOWED:
                continue
            offenders.append(f"{rel}:{node.lineno} (in {scope.split('::')[1]})")
    return offenders


def _unguarded_project_clients(root: Path = SRC_ROOT) -> list[str]:
    """Check 4: bearer-capable clients built with a project credential, unguarded.

    `SESSION_AUTH_FEATURE` cannot cover this: the clients that legitimately speak
    bearer leave it unset, so nothing stops a direct
    `KeboolaClient(stack_url, project.token)` that skips `make_client_factory`
    from putting the literal `kbc-session://{id}` on the wire as an
    `X-StorageApi-Token` header. Check 2 only reads class definitions and never
    looks at who constructs them, which is the gap this closes.

    `token=""` is the bearer branch's own construction and is not a finding.
    """
    offenders = []
    # A subclass constructs the same wire behaviour as its base, so a
    # `class ProjectStorage(KeboolaClient)` built from `project.token` is the very
    # same finding under a different name.
    credential_clients = _descendants_of(root, set(PROJECT_CREDENTIAL_CLIENTS))
    for path in _py_files(root):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = _parent_map(tree)
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _call_name(node)
            if aliases.get(called, called) not in credential_clients:
                continue
            token_args = [kw.value for kw in node.keywords if kw.arg == "token"]
            if token_args and all(
                isinstance(value, ast.Constant) and value.value == "" for value in token_args
            ):
                continue
            if _scope_is_sentinel_aware(node, parents, source):
                continue
            scope = _enclosing_function_name(node, parents)
            offenders.append(f"{_rel(path, root)}:{node.lineno} (in {scope})")
    return offenders


def _undecided_clients(root: Path = SRC_ROOT) -> list[str]:
    """Check 2: BaseHttpClient subclasses that never decided about sessions."""
    offenders = []
    http_clients = _descendants_of(root, {"BaseHttpClient"})
    settled = _descendants_of(root, set(BEARER_CAPABLE_CLIENTS))
    for path in _py_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
            bases = {aliases.get(base, base) for base in bases}
            # Transitively: a class inheriting from `KeboolaClient` inherits the
            # same bearer-vs-sentinel behaviour as one inheriting `BaseHttpClient`
            # directly, so it needs a decision just the same.
            if not (bases & http_clients):
                continue
            # ...and it inherits the DECISION too: everything under an entry in
            # BEARER_CAPABLE_CLIENTS is already settled (the Storage mixins and
            # the composed `KeboolaClient` all sit under `_CoreClient`).
            if node.name in settled:
                continue
            if _class_attr_string(node, "SESSION_AUTH_FEATURE") is None:
                offenders.append(f"{_rel(path, root)}:{node.lineno} {node.name}")
    return offenders


def _descendants_of(root: Path, seeds: set[str]) -> set[str]:
    """``seeds`` plus every class in the tree that transitively inherits one.

    A fixpoint rather than a single pass, because a subclass can be declared in a
    file parsed before its own base class.
    """
    edges: list[tuple[str, set[str]]] = []
    for path in _py_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
                edges.append((node.name, {aliases.get(base, base) for base in bases}))
    known = set(seeds)
    changed = True
    while changed:
        changed = False
        for name, bases in edges:
            if name not in known and bases & known:
                known.add(name)
                changed = True
    return known


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

    unguarded_clients = _unguarded_project_clients()
    if unguarded_clients:
        problems.append(
            "These call sites construct a Storage client with a project credential\n"
            "without proving the credential is not a `kbc-session://` sentinel, so the\n"
            "sentinel string can reach the wire as an X-StorageApi-Token header:\n"
            + "\n".join(f"    src/keboola_agent_cli/{c}" for c in unguarded_clients)
            + "\n\nBuild the client through `make_client_factory` (bearer-aware), or call\n"
            "`require_static_token(token, feature=...)` first if the path is static-only."
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
        f"OK: no unguarded credential writes; no unguarded Storage-client construction; "
        f"every BaseHttpClient subclass decided; "
        f"all {len(guards)} guards covered by {FEATURES_NAME} ({len(declared)} entries)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
