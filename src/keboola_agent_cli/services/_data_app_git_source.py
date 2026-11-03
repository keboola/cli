"""Where a data app's code comes from: ``parameters.dataApp.git``.

Split out of ``data_app_service.py`` to respect the file-size budget
(CONTRIBUTING.md "File-size budgets"). Owns the validation of the git flags of
``data-app create``, the git block written into the Storage config (and its
dry-run preview), and the switch of an existing block to organization auth.

Four sources, mutually exclusive:

* **public** -- ``{repository, private: false, branch}``; no credential.
* **private, explicit credential** -- ``username`` + ``#password`` (a PAT
  encrypted under the target project's KMS).
* **organization** -- ``authType: organization``. The config carries NO
  credential: sandboxes-service mints a short-lived read-only token through
  the Keboola organization's GitHub App connection. Only an ``https://``
  GitHub URL qualifies, and ``username`` / ``#password`` / ``#sshKey`` must be
  absent (the backend rejects the combination).
* **managed** -- a Keboola-hosted repo; no git block at all.

Which hosts organization auth accepts is stack configuration (GitHub
Enterprise Server installs live on customer domains), so kbagent checks the
URL shape only and leaves the host to the backend, whose error lists the
hosts the stack accepts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._data_app_bodies import DATA_APP_COMPONENT_ID, ENCRYPTED_PASSWORD_PREFIXES, _check_text_field

GIT_AUTH_ORGANIZATION = "organization"
VALID_GIT_AUTH_TYPES: tuple[str, ...] = (GIT_AUTH_ORGANIZATION,)

# Explicit credentials; each one is mutually exclusive with organization auth.
GIT_CREDENTIAL_KEYS: tuple[str, ...] = ("username", "#password", "#sshKey")

MAX_GIT_REPO_LENGTH = 1024
MAX_GIT_BRANCH_LENGTH = 255
MAX_GIT_USERNAME_LENGTH = 255

# URL schemes accepted for ``--git-repo``. Anything else (file://, gopher://,
# bare ssh syntax like ``git@host:path``) is rejected at the service
# boundary -- the data-app runner only ever talks https / http / git / ssh.
ALLOWED_GIT_REPO_SCHEMES: tuple[str, ...] = (
    "https://",
    "http://",
    "ssh://",
    "git://",
)

# The repository path sandboxes-service accepts for organization auth
# (GitHubRepositoryUrl::REPOSITORY_PATH_PATTERN), so a bad URL fails here
# instead of at deploy time.
_ORG_REPO_PATH = re.compile(
    r"^/(?P<owner>[A-Za-z0-9][A-Za-z0-9_-]{0,38})/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?\Z"
)
_ORG_RESERVED_REPO_NAMES = frozenset({".", "..", ".git"})


@dataclass(frozen=True)
class GitSource:
    """The git flags of ``data-app create`` (and the serve ``DataAppCreate`` body)."""

    repo: str = ""
    branch: str = "main"
    public: bool = False
    username: str | None = None
    pat_plaintext: str | None = None
    pat_encrypted: str | None = None
    auth_type: str | None = None
    managed: bool = False

    @property
    def uses_organization_auth(self) -> bool:
        return self.auth_type == GIT_AUTH_ORGANIZATION


def _invalid(message: str, error_code: ErrorCode = ErrorCode.VALIDATION_ERROR) -> KeboolaApiError:
    return KeboolaApiError(message=message, status_code=0, error_code=error_code, retryable=False)


def _has_value(value: str | None) -> bool:
    return value is not None and value != ""


def _credential_flags(source: GitSource) -> list[str]:
    """The explicit-credential flags the caller set, by their CLI names."""
    flags = []
    if source.username:
        flags.append("--git-username")
    if _has_value(source.pat_plaintext):
        flags.append("--git-pat-env/--git-pat-file")
    if _has_value(source.pat_encrypted):
        flags.append("--git-pat-encrypted")
    return flags


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_git_flags(source: GitSource) -> None:
    """Reject conflicting or malformed git flags before any API call.

    Runs at the service boundary: the service is also reached through
    ``kbagent serve`` and the SDK, so the command layer's checks are not
    relied upon. :func:`validate_git_credentials` follows once the rest of
    the create input is checked.
    """
    _check_auth_type(source)
    _check_managed_exclusive(source)
    _check_organization_exclusive(source)
    _check_text_field("--git-repo", source.repo, MAX_GIT_REPO_LENGTH, False)
    _check_text_field("--git-branch", source.branch, MAX_GIT_BRANCH_LENGTH, False)
    _check_text_field("--git-username", source.username or "", MAX_GIT_USERNAME_LENGTH, False)
    _check_repo_scheme(source.repo)


def validate_git_credentials(source: GitSource) -> None:
    """Check that the source names exactly one valid way to authenticate."""
    if source.public:
        _check_public_exclusive(source)
    elif source.uses_organization_auth:
        validate_organization_repository(source.repo)
    elif not source.managed:
        _check_private_credentials(source)


def _check_auth_type(source: GitSource) -> None:
    if source.auth_type is not None and source.auth_type not in VALID_GIT_AUTH_TYPES:
        raise _invalid(
            f"Invalid --git-auth '{source.auth_type}'. Valid values: "
            f"{', '.join(VALID_GIT_AUTH_TYPES)}."
        )


def _check_managed_exclusive(source: GitSource) -> None:
    """A managed repo has no URL, branch override or credential to supply."""
    if not source.managed:
        return
    offenders = []
    if source.repo:
        offenders.append("--git-repo")
    if source.public:
        offenders.append("--git-public")
    offenders.extend(_credential_flags(source))
    if source.uses_organization_auth:
        offenders.append("--git-auth")
    if offenders:
        raise _invalid(
            "--use-managed-git-repo provisions an empty Keboola-hosted "
            "repository and is incompatible with external-git flags: "
            f"{', '.join(offenders)}."
        )


def _check_organization_exclusive(source: GitSource) -> None:
    """Organization auth carries no credential; the backend rejects one beside it."""
    if not source.uses_organization_auth or source.managed:
        return
    offenders = _credential_flags(source)
    if source.public:
        offenders.insert(0, "--git-public")
    if offenders:
        raise _invalid(
            "--git-auth organization clones through the Keboola organization's "
            "GitHub App connection and stores no credential; it is incompatible "
            f"with: {', '.join(offenders)}."
        )


def _check_repo_scheme(repo: str) -> None:
    if repo and not any(repo.startswith(scheme) for scheme in ALLOWED_GIT_REPO_SCHEMES):
        raise _invalid(
            f"--git-repo must use one of {', '.join(ALLOWED_GIT_REPO_SCHEMES)}."
            " Bare ssh syntax (git@host:path) and other schemes are not"
            " accepted.",
            ErrorCode.DATA_APP_INVALID_GIT,
        )


def _check_public_exclusive(source: GitSource) -> None:
    if _credential_flags(source):
        raise _invalid(
            "--git-public is incompatible with --git-username / "
            "--git-pat-env / --git-pat-file / --git-pat-encrypted."
        )


def _check_private_credentials(source: GitSource) -> None:
    if not source.username:
        raise _invalid(
            "--git-username is required for private repositories "
            "(or pass --git-auth organization to clone through the "
            "organization's GitHub connection)."
        )
    provided = sum(1 for v in (source.pat_plaintext, source.pat_encrypted) if _has_value(v))
    if provided == 0:
        raise _invalid(
            "Private repository requires a PAT. Pass one of: "
            "--git-pat-env VAR / --git-pat-file PATH / "
            "--git-pat-encrypted KBC::Project..."
        )
    if provided > 1:
        raise _invalid(
            "Specify exactly one of --git-pat-env / --git-pat-file / "
            "--git-pat-encrypted; they are mutually exclusive."
        )
    _check_pat_shape(source)


def _check_pat_shape(source: GitSource) -> None:
    if source.pat_plaintext is not None and source.pat_plaintext.startswith("KBC::"):
        # EncryptService passes a ``KBC::`` value through unchanged, so a stray
        # ciphertext from another project would reach Storage and fail at
        # runtime decrypt. Reject it up front.
        raise _invalid(
            "--git-pat-env / --git-pat-file expect plaintext PATs. "
            "The value starts with 'KBC::' which suggests an "
            "already-encrypted ciphertext; if so, pass it via "
            "--git-pat-encrypted instead so the prefix is validated.",
            ErrorCode.DATA_APP_INVALID_GIT,
        )
    if source.pat_encrypted is not None and not any(
        source.pat_encrypted.startswith(p) for p in ENCRYPTED_PASSWORD_PREFIXES
    ):
        raise _invalid(
            "--git-pat-encrypted must be a project-scoped Encryption "
            f"API ciphertext (one of: {', '.join(ENCRYPTED_PASSWORD_PREFIXES)}). "
            "Re-encrypt with `kbagent encrypt values --component-id "
            f"{DATA_APP_COMPONENT_ID}` against THIS project; ciphertext "
            "from another project will not decrypt (writeup §8)."
        )


def validate_organization_repository(repo: str) -> None:
    """Check that ``repo`` has the shape organization auth accepts.

    ``https://{host}/{owner}/{repo}`` only: the minted token is an HTTP
    password scoped to that one repository, so a port, query, fragment or
    embedded credential would change what is cloned. The host itself is
    checked by the backend (see the module docstring).
    """
    if not repo.startswith("https://"):
        raise _invalid(
            "--git-auth organization needs an https:// repository URL "
            "(https://github.com/OWNER/REPO); the minted token is used as an "
            "HTTP password, so ssh:// and git:// URLs cannot use it.",
            ErrorCode.DATA_APP_INVALID_GIT,
        )
    parts = urlsplit(repo)
    _check_org_url_parts(repo, parts)
    _check_org_repo_path(repo, parts)


def _check_org_url_parts(repo: str, parts: SplitResult) -> None:
    try:
        has_port = parts.port is not None
    except ValueError:
        has_port = True
    extras = [
        label
        for label, present in (
            ("embedded credentials", parts.username is not None or parts.password is not None),
            ("a port", has_port),
            ("a query", bool(parts.query) or repo.endswith("?")),
            ("a fragment", bool(parts.fragment) or repo.endswith("#")),
        )
        if present
    ]
    if not parts.hostname:
        extras.insert(0, "no host")
    if extras:
        raise _invalid(
            f"--git-repo '{repo}' cannot use --git-auth organization: it has "
            f"{', '.join(extras)}. Expected https://HOST/OWNER/REPO.",
            ErrorCode.DATA_APP_INVALID_GIT,
        )


def _check_org_repo_path(repo: str, parts: SplitResult) -> None:
    path = parts.path
    match = _ORG_REPO_PATH.match(path)
    if (
        "%" in path
        or {".", ".."} & set(path.split("/"))
        or match is None
        or match.group("repo") in _ORG_RESERVED_REPO_NAMES
    ):
        raise _invalid(
            f"--git-repo '{repo}' is not a repository URL organization auth "
            f"accepts. Expected https://{parts.hostname}/OWNER/REPO (ASCII "
            "letters, digits, '-', '_' and '.'; no percent-encoding).",
            ErrorCode.DATA_APP_INVALID_GIT,
        )


# ---------------------------------------------------------------------------
# Git blocks
# ---------------------------------------------------------------------------


def organization_git_block(repo: str, branch: str) -> dict[str, Any]:
    """The block kbc-ui writes for organization auth -- no credential key at all."""
    return {
        "repository": repo,
        "branch": branch,
        "authType": GIT_AUTH_ORGANIZATION,
        "private": True,
    }


def public_git_block(repo: str, branch: str) -> dict[str, Any]:
    return {"repository": repo, "private": False, "branch": branch}


def private_git_block(repo: str, branch: str, username: str, password: str) -> dict[str, Any]:
    return {
        "repository": repo,
        "private": True,
        "username": username,
        "#password": password,
        "branch": branch,
    }


def preview_git_block(source: GitSource) -> dict[str, Any] | None:
    """The git block ``--dry-run`` shows; equal to the real one but for the PAT."""
    if source.managed:
        return None
    if source.public:
        return public_git_block(source.repo, source.branch)
    if source.uses_organization_auth:
        return organization_git_block(source.repo, source.branch)
    return private_git_block(
        source.repo, source.branch, source.username or "<from input>", "<encrypted at runtime>"
    )


def build_git_block(source: GitSource, encrypt_service: Any, alias: str) -> dict[str, Any] | None:
    """The git block written into the Storage config; ``None`` for a managed repo.

    Only a private repo with an explicit credential costs an encryption call.
    """
    if source.managed:
        return None
    if source.public:
        return public_git_block(source.repo, source.branch)
    if source.uses_organization_auth:
        return organization_git_block(source.repo, source.branch)
    encrypted_pat = _encrypt_pat(source, encrypt_service, alias)
    return private_git_block(source.repo, source.branch, source.username or "", encrypted_pat)


def _encrypt_pat(source: GitSource, encrypt_service: Any, alias: str) -> str:
    """Encrypt the PAT under the target project's KMS, failing closed.

    A pre-encrypted ciphertext was prefix-validated in
    :func:`validate_git_source`; EncryptService passes any ``KBC::`` value
    through unchanged, so it costs no round-trip. The result is re-checked so
    a misconfigured input can never reach Storage as plaintext.
    """
    secret_input = source.pat_plaintext or source.pat_encrypted or ""
    try:
        encrypted = encrypt_service.encrypt(
            alias=alias,
            component_id=DATA_APP_COMPONENT_ID,
            input_data={"#password": secret_input},
        )
    except ConfigError as exc:
        raise _invalid(
            f"Failed to prepare git PAT for encryption: {exc.message}",
            ErrorCode.ENCRYPTION_FAILED,
        ) from exc
    encrypted_pat = encrypted.get("#password", "")
    if not encrypted_pat or not any(
        encrypted_pat.startswith(p) for p in ENCRYPTED_PASSWORD_PREFIXES
    ):
        raise _invalid(
            "Encryption API did not return a project-scoped ciphertext "
            "for the git PAT; refusing to write plaintext to Storage.",
            ErrorCode.ENCRYPTION_FAILED,
        )
    return encrypted_pat


def describe_git_auth(git_block: dict[str, Any]) -> str:
    """One word for how a git block authenticates: organization / ssh-key / pat / public."""
    if git_block.get("authType") == GIT_AUTH_ORGANIZATION:
        return GIT_AUTH_ORGANIZATION
    if git_block.get("#sshKey"):
        return "ssh-key"
    if git_block.get("#password"):
        return "pat"
    return "public" if git_block.get("private") is False else "none"


def switch_to_organization_auth(
    git_block: dict[str, Any], *, repo: str | None = None, branch: str | None = None
) -> dict[str, Any]:
    """Return ``git_block`` moved to organization auth, credentials removed.

    Every other key (``entrypoint``, ...) is kept. ``repo`` / ``branch``
    replace the stored values when given -- an SSH URL has to become an
    https one. The credential keys are dropped in the same write that sets
    ``authType``: the backend rejects a config that carries both, which is
    why a merge that only adds ``authType`` cannot do this.
    """
    switched = {k: v for k, v in git_block.items() if k not in GIT_CREDENTIAL_KEYS}
    if repo:
        switched["repository"] = repo
    if branch:
        switched["branch"] = branch
    switched["authType"] = GIT_AUTH_ORGANIZATION
    switched["private"] = True
    return switched


@dataclass(frozen=True)
class GitAuthSwitch:
    """What switching a Storage config body to organization auth changes."""

    before: dict[str, Any]
    after: dict[str, Any]
    body: dict[str, Any]
    removed_credentials: list[str]

    @property
    def changed(self) -> bool:
        return self.before != self.after


def plan_organization_switch(
    body: dict[str, Any], *, repo: str | None = None, branch: str | None = None
) -> GitAuthSwitch:
    """Plan the switch of a data-app config ``body`` to organization auth.

    ``body`` is the Storage ``configuration`` and is not mutated; the result's
    ``body`` is a copy with ``parameters.dataApp.git`` replaced and every
    other key kept. Raises when the config has no external git block (a
    managed repo has nothing to switch) or when the resulting repository URL
    cannot use organization auth.
    """
    data_app = (body.get("parameters") or {}).get("dataApp") or {}
    before = data_app.get("git") if isinstance(data_app, dict) else None
    if not isinstance(before, dict) or not before.get("repository"):
        raise _invalid(
            "The data app's config has no parameters.dataApp.git.repository to "
            "switch. A Keboola-managed repo stores no git block and needs no "
            "credential; for an external repo, check `kbagent data-app detail`."
        )
    _check_text_field("--git-repo", repo or "", MAX_GIT_REPO_LENGTH, False)
    _check_text_field("--git-branch", branch or "", MAX_GIT_BRANCH_LENGTH, False)
    after = switch_to_organization_auth(before, repo=repo, branch=branch)
    validate_organization_repository(str(after["repository"]))
    new_body = json.loads(json.dumps(body))
    new_body["parameters"]["dataApp"]["git"] = after
    removed = [key for key in GIT_CREDENTIAL_KEYS if key in before]
    return GitAuthSwitch(before=before, after=after, body=new_body, removed_credentials=removed)
