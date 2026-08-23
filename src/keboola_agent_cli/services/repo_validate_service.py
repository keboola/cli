"""Pre-flight validation for Keboola data-app git repositories.

Walks a GitHub repo via the public Contents + Trees API and verifies the
"Golden Rule" repository structure documented at
https://help.keboola.com/data-apps/python-js/. Each check emits one of
``BLOCKING`` / ``WARN`` / ``OK`` with a citation to the canon page that
defines the rule.

Rate-limit-aware fetch strategy:

1. ONE ``GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1`` to
   resolve every existence check against the same response.
2. UP TO 4 ``GET /repos/{owner}/{repo}/contents/{path}`` for files whose
   contents the rules need to inspect: setup.sh, pyproject.toml, plus
   nginx/default.conf and supervisord/app.conf (both fetched when both
   exist so the port-match check can compare them).

A typical run spends 1-5 GitHub API calls (1 tree + 0-4 contents) regardless of repo size; the
60/hour unauth limit is no longer the common-case failure mode it
otherwise would be. Pass ``--git-pat-env`` (resolved to a plaintext PAT)
to raise the limit to 5,000/hour.

Scope of this PR: ``--type python-js`` only. Streamlit / pure-Python /
R repo layouts differ and need their own per-type canon citations -- a
follow-up PR adds them.

TODO: extract ``GitHubContentsClient`` (defined below) into a top-level
``src/keboola_agent_cli/github_client.py`` module that inherits from
``BaseHttpClient`` so this stays consistent with the 3-layer architecture
(LAYER 3 = clients, LAYER 2 = services). The ``github_client_factory``
dependency-injection pattern in ``RepoValidateService`` already isolates
the client for testing; the extraction is a pure refactor with no
behaviour change.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..constants import DEFAULT_TIMEOUT
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from .base import BaseService

logger = logging.getLogger(__name__)


# Canonical citations for every rule the validator emits. Each enumerated
# rule includes the help-doc anchor users can click to read why we flag
# the issue. Kept here so commands and tests can reference them by name.
HELP_PYTHON_JS = "https://help.keboola.com/data-apps/python-js/"
HELP_BACKEND_VERSIONS = "https://help.keboola.com/components/data-apps/backend-versions/"
HELP_STORAGE_ACCESS = "https://help.keboola.com/data-apps/storage-access/"


# Files we may need to fetch *contents* for (not just existence).
_NGINX_CONF = "keboola-config/nginx/sites/default.conf"
_APP_CONF = "keboola-config/supervisord/services/app.conf"
_SETUP_SH = "keboola-config/setup.sh"
_PYPROJECT = "pyproject.toml"

# Heuristic regexes -- tightening any of these is preferred over a real
# parser because every Python framework names POST handlers differently
# (Flask blueprints, FastAPI decorators, dynamic registration, etc.) and
# false positives are operationally cheap (a WARN, not a BLOCKING).
_PIP_INSTALL_RE = re.compile(r"\bpip\s+install\b")
_UV_SYNC_RE = re.compile(r"\buv\s+sync\b")
_REQUIRES_PYTHON_RE = re.compile(r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
_NGINX_PROXY_PASS_PORT_RE = re.compile(r"proxy_pass\s+http://[^:]+:(\d+)")
_APP_CONF_PORT_RE = re.compile(r"--port[=\s]+(\d+)|:(\d+)\b")


def _strip_shell_comments(text: str) -> str:
    """Return ``text`` with ``#`` comments removed, quoted ``#`` preserved.

    The setup.sh rules below grep for what the script *runs*, so they have to
    see code and not prose. Without this, a comment warning against the wrong
    installer -- exactly what a careful author writes above the right one --
    is read as the violation it warns about and blocks the repo. The inverse
    also matters: a comment merely mentioning ``uv sync`` must not satisfy the
    check that the script actually invokes it.

    Deliberately not a shell parser. It tracks single/double quotes so a
    ``#`` inside a string survives, and treats a ``#`` at line start or after
    whitespace as starting a comment -- which covers real setup.sh files
    without pretending to handle heredocs or ``${...#...}`` expansions.
    """
    out: list[str] = []
    for line in text.splitlines():
        quote: str | None = None
        cut = len(line)
        for i, char in enumerate(line):
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "#" and (i == 0 or line[i - 1].isspace()):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


# Severities. Order matters for verdict aggregation (worst wins).
SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_BLOCKING = "BLOCKING"


@dataclass
class CheckResult:
    """One validate-repo check outcome."""

    name: str
    severity: str
    citation: str
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "severity": self.severity,
            "citation": self.citation,
        }
        if self.message:
            out["message"] = self.message
        if self.details:
            out["details"] = self.details
        return out


# ---------------------------------------------------------------------------
# GitHub client (intentionally minimal -- no fancy retry, just the calls
# validate-repo needs)
# ---------------------------------------------------------------------------


class GitHubContentsClient:
    """Read-only GitHub Contents + Trees client.

    Only used by :class:`RepoValidateService`. Refuses non-GitHub hosts
    (this PR ships GitHub support; GitLab / Bitbucket / etc. are tracked
    as a follow-up). PAT is sent only when present and never logged.
    """

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str | None = None, timeout: Any = DEFAULT_TIMEOUT) -> None:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self) -> GitHubContentsClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_tree_recursive(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        """``GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1``.

        Single call returns every path + blob SHA in the repo at ``ref``.
        Tree may be ``truncated`` for very large repos (~100k entries);
        callers fall back to per-file content fetches in that case.

        Slash-bearing refs (``feature/foo``) are URL-encoded so GitHub
        does not mis-route the request to a 404.
        """
        encoded_ref = quote(ref, safe="")
        path = f"/repos/{owner}/{repo}/git/trees/{encoded_ref}?recursive=1"
        return self._json_get(path, action=f"GET tree for {owner}/{repo}@{ref}")

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        """Return decoded text content of one file, or ``None`` if absent.

        Raises ``KeboolaApiError`` on auth / rate-limit / 5xx. ``ref`` is
        sent as a query param (httpx URL-encodes), so slash-bearing branches
        like ``feature/foo`` round-trip correctly.
        """
        url = f"/repos/{owner}/{repo}/contents/{path}"
        params = {"ref": ref}
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"GitHub fetch failed for {owner}/{repo}/{path}: {exc}",
                status_code=0,
                error_code=ErrorCode.CONNECTION_ERROR,
                retryable=True,
            ) from exc
        if response.status_code == 404:
            return None
        self._raise_for_status(response, action=f"GET contents {owner}/{repo}/{path}")
        body = response.json()
        if not isinstance(body, dict):
            return None
        encoding = body.get("encoding", "")
        content = body.get("content", "")
        if encoding == "base64" and isinstance(content, str):
            try:
                return base64.b64decode(content).decode("utf-8", errors="replace")
            except (ValueError, TypeError) as exc:
                logger.warning("Failed to decode %s/%s/%s: %s", owner, repo, path, exc)
                return ""
        if isinstance(content, str):
            return content
        return ""

    def _json_get(self, path: str, *, action: str) -> dict[str, Any]:
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"GitHub call failed ({action}): {exc}",
                status_code=0,
                error_code=ErrorCode.CONNECTION_ERROR,
                retryable=True,
            ) from exc
        self._raise_for_status(response, action=action)
        body = response.json()
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, action: str) -> None:
        if 200 <= response.status_code < 300:
            return
        # Surface rate limit explicitly so the operator can act on the
        # actionable hint (use a PAT). The unauthenticated GitHub limit is
        # 60/hour; even with the trees-recursive optimisation a CI loop
        # can exhaust it.
        rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
        message = (
            f"GitHub API returned {response.status_code} for {action}. Body: {response.text[:200]}"
        )
        if response.status_code == 403 and rate_limit_remaining == "0":
            message = (
                f"GitHub API rate-limit exceeded ({action}). The unauthenticated "
                "limit is 60 requests per hour per IP. Pass --git-pat-env to use a "
                "PAT (raises the limit to 5000/hour)."
            )
        raise KeboolaApiError(
            message=message,
            status_code=response.status_code,
            error_code=ErrorCode.API_ERROR,
            retryable=response.status_code >= 500,
        )


# ---------------------------------------------------------------------------
# Pure validation function (no I/O -- I/O lives in the service)
# ---------------------------------------------------------------------------


@dataclass
class _RepoSnapshot:
    """Just-enough subset of a GitHub repo for the validator to work on."""

    paths: set[str]
    truncated: bool
    setup_sh: str | None = None
    pyproject_toml: str | None = None
    nginx_conf: str | None = None
    app_conf: str | None = None


def validate_keboola_repo(
    snapshot: _RepoSnapshot,
    *,
    type_: str,
    runtime_python_pin: str | None = None,
) -> list[CheckResult]:
    """Run every validate-repo check against an in-memory snapshot.

    Pure: no I/O, no client. Tests pass hand-crafted snapshots; the
    service constructs snapshots from real GitHub responses.

    ``runtime_python_pin`` is optional; if absent the requires-python
    consistency check downgrades to a soft skip. The §1b probe locks the
    canonical pinned version live; offline fallback skips the check
    rather than fabricating one.
    """
    if type_ != "python-js":
        # The command layer rejects non-python-js types; the service
        # boundary check is defence-in-depth.
        return [
            CheckResult(
                name="meta.type-supported",
                severity=SEVERITY_BLOCKING,
                citation=HELP_PYTHON_JS,
                message=(
                    f"validate-repo currently supports --type python-js only "
                    f"(got: {type_!r}). Other types tracked as follow-up."
                ),
            )
        ]

    results: list[CheckResult] = []
    paths = snapshot.paths

    if snapshot.truncated:
        results.append(
            CheckResult(
                name="meta.tree-truncated",
                severity=SEVERITY_WARN,
                citation=HELP_PYTHON_JS,
                message=(
                    "Repo tree returned >100k entries (GitHub truncated the "
                    "recursive response). File-existence checks may have "
                    "false negatives; run with --git-pat-env to raise the "
                    "rate limit and retry."
                ),
            )
        )

    # 1. Golden-Rule existence checks ----------------------------------------
    for required, name in (
        (_NGINX_CONF, "golden-rule.nginx-default-conf"),
        (_APP_CONF, "golden-rule.supervisord-app-conf"),
        (_PYPROJECT, "golden-rule.pyproject-toml"),
    ):
        if required in paths:
            results.append(CheckResult(name=name, severity=SEVERITY_OK, citation=HELP_PYTHON_JS))
        else:
            results.append(
                CheckResult(
                    name=name,
                    severity=SEVERITY_BLOCKING,
                    citation=HELP_PYTHON_JS,
                    message=f"Required file not found at {required}.",
                )
            )

    # 2. setup.sh checks (depend on file presence + content) -----------------
    pyproject_has_deps = _pyproject_declares_deps(snapshot.pyproject_toml)
    setup_sh_present = _SETUP_SH in paths
    if setup_sh_present:
        results.append(
            CheckResult(
                name="golden-rule.setup-sh-present",
                severity=SEVERITY_OK,
                citation=HELP_PYTHON_JS,
            )
        )
    elif pyproject_has_deps:
        results.append(
            CheckResult(
                name="golden-rule.setup-sh-present",
                severity=SEVERITY_BLOCKING,
                citation=HELP_PYTHON_JS,
                message=(
                    "pyproject.toml declares dependencies but keboola-config/setup.sh "
                    "is missing; the runtime cannot install them without `uv sync`."
                ),
            )
        )
    else:
        results.append(
            CheckResult(
                name="golden-rule.setup-sh-present",
                severity=SEVERITY_WARN,
                citation=HELP_PYTHON_JS,
                message=(
                    "keboola-config/setup.sh is absent; intentional only if your "
                    "app has zero runtime dependencies."
                ),
            )
        )

    if setup_sh_present and snapshot.setup_sh is not None:
        # Both rules below ask what the script RUNS, so they read code only.
        setup_sh_code = _strip_shell_comments(snapshot.setup_sh)
        if _PIP_INSTALL_RE.search(setup_sh_code):
            results.append(
                CheckResult(
                    name="golden-rule.setup-sh-no-pip",
                    severity=SEVERITY_BLOCKING,
                    citation=HELP_PYTHON_JS,
                    message=(
                        "keboola-config/setup.sh contains `pip install`. The runtime "
                        "blocks pip; replace with `uv sync`."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="golden-rule.setup-sh-no-pip",
                    severity=SEVERITY_OK,
                    citation=HELP_PYTHON_JS,
                )
            )

        if pyproject_has_deps:
            if _UV_SYNC_RE.search(setup_sh_code):
                results.append(
                    CheckResult(
                        name="golden-rule.setup-sh-uv-sync",
                        severity=SEVERITY_OK,
                        citation=HELP_PYTHON_JS,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="golden-rule.setup-sh-uv-sync",
                        severity=SEVERITY_WARN,
                        citation=HELP_PYTHON_JS,
                        message=(
                            "pyproject.toml declares dependencies but setup.sh does "
                            "not invoke `uv sync`; deps will not install."
                        ),
                    )
                )

    # 3. requires-python <= runtime pin --------------------------------------
    if snapshot.pyproject_toml is not None and runtime_python_pin:
        declared = _extract_requires_python(snapshot.pyproject_toml)
        if declared and _requires_python_above_pin(declared, runtime_python_pin):
            results.append(
                CheckResult(
                    name="golden-rule.requires-python",
                    severity=SEVERITY_BLOCKING,
                    citation=HELP_BACKEND_VERSIONS,
                    message=(
                        f"pyproject.toml requires-python={declared!r} is above the "
                        f"data-app runtime pin ({runtime_python_pin}); `uv sync` will fail."
                    ),
                    details={"declared": declared, "runtime_pin": runtime_python_pin},
                )
            )
        else:
            results.append(
                CheckResult(
                    name="golden-rule.requires-python",
                    severity=SEVERITY_OK,
                    citation=HELP_BACKEND_VERSIONS,
                )
            )
    elif snapshot.pyproject_toml is not None:
        # No runtime pin available offline; skip silently rather than
        # fabricating a constraint.
        results.append(
            CheckResult(
                name="golden-rule.requires-python",
                severity=SEVERITY_WARN,
                citation=HELP_BACKEND_VERSIONS,
                message=(
                    "Runtime Python pin not available offline; skipped requires-python "
                    "consistency check. Re-run after the §1b probe locks the runtime version."
                ),
            )
        )

    # 4. nginx proxy_pass port matches app.conf port -------------------------
    if snapshot.nginx_conf is not None and snapshot.app_conf is not None:
        nginx_match = _NGINX_PROXY_PASS_PORT_RE.search(snapshot.nginx_conf)
        app_match = _APP_CONF_PORT_RE.search(snapshot.app_conf)
        if nginx_match and app_match:
            nginx_port = nginx_match.group(1)
            app_port = next((g for g in app_match.groups() if g), "")
            if nginx_port == app_port:
                results.append(
                    CheckResult(
                        name="golden-rule.nginx-app-port-match",
                        severity=SEVERITY_OK,
                        citation=HELP_PYTHON_JS,
                        details={"port": nginx_port},
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="golden-rule.nginx-app-port-match",
                        severity=SEVERITY_WARN,
                        citation=HELP_PYTHON_JS,
                        message=(
                            f"nginx proxy_pass port ({nginx_port}) does not match "
                            f"app.conf port ({app_port}); requests will reach a "
                            "different process than configured."
                        ),
                        details={"nginx_port": nginx_port, "app_port": app_port},
                    )
                )

    return results


def aggregate_verdict(results: list[CheckResult]) -> dict[str, Any]:
    """Return ``{verdict, blocking_count, warn_count, ok_count}`` from results."""
    counts = {SEVERITY_OK: 0, SEVERITY_WARN: 0, SEVERITY_BLOCKING: 0}
    for r in results:
        counts[r.severity] = counts.get(r.severity, 0) + 1
    if counts[SEVERITY_BLOCKING] > 0:
        verdict = SEVERITY_BLOCKING
    elif counts[SEVERITY_WARN] > 0:
        verdict = SEVERITY_WARN
    else:
        verdict = SEVERITY_OK
    return {
        "verdict": verdict,
        "blocking_count": counts[SEVERITY_BLOCKING],
        "warn_count": counts[SEVERITY_WARN],
        "ok_count": counts[SEVERITY_OK],
    }


# ---------------------------------------------------------------------------
# Service: glue between git URL + GitHub client + pure validator
# ---------------------------------------------------------------------------


@dataclass
class _GitHubLocator:
    owner: str
    repo: str
    ref: str


class RepoValidateService(BaseService):
    """Pre-flight validation of a Keboola data-app git repo.

    Read-only. The service only fetches from GitHub; it never touches a
    Keboola project. ``ConfigStore`` is accepted to match the
    :class:`BaseService` constructor signature but is unused.
    """

    GITHUB_HOSTS: tuple[str, ...] = ("github.com", "www.github.com")

    def __init__(
        self,
        config_store: Any,
        github_client_factory: Any | None = None,
    ) -> None:
        super().__init__(config_store=config_store)
        self._github_client_factory = github_client_factory or self._default_github_client

    @staticmethod
    def _default_github_client(token: str | None) -> GitHubContentsClient:
        return GitHubContentsClient(token=token)

    def validate_repo(
        self,
        *,
        git_repo: str,
        git_branch: str = "main",
        git_public: bool = True,
        git_pat: str | None = None,
        type_: str = "python-js",
        strict: bool = False,
    ) -> dict[str, Any]:
        del git_public  # kwarg accepted for command-layer call shape; unused here.
        if type_ != "python-js":
            raise KeboolaApiError(
                message=(
                    f"--type currently supports python-js only (got: {type_!r}). "
                    "Other types are tracked as a follow-up."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )

        locator = self._parse_github_url(git_repo, git_branch)
        # The command layer enforces "PAT requires --no-git-public"; here
        # we just forward whatever was supplied. Empty PAT -> anonymous.
        client = self._github_client_factory(git_pat or None)

        try:
            try:
                tree_response = client.get_tree_recursive(locator.owner, locator.repo, locator.ref)
            except KeboolaApiError as exc:
                # 404 from the trees endpoint usually means private repo
                # without a PAT, or a typo in the URL.
                if exc.status_code == 404 and not git_pat:
                    raise KeboolaApiError(
                        message=(
                            f"GitHub returned 404 for {locator.owner}/{locator.repo}@"
                            f"{locator.ref}. If this is a private repo, pass "
                            "--git-pat-env / --git-pat-file with a PAT that has "
                            "`repo` scope."
                        ),
                        status_code=404,
                        error_code=ErrorCode.VALIDATION_ERROR,
                        retryable=False,
                    ) from exc
                raise

            tree_entries = tree_response.get("tree", [])
            paths: set[str] = set()
            if isinstance(tree_entries, list):
                for entry in tree_entries:
                    if isinstance(entry, dict):
                        path = entry.get("path")
                        if isinstance(path, str):
                            paths.add(path)
            truncated = bool(tree_response.get("truncated"))

            # Up to 4 content fetches: setup.sh, pyproject.toml,
            # nginx-conf and app-conf -- capped at 4. Combined with the
            # tree fetch above the worst case is 5 GitHub calls, still
            # bounded regardless of repo size.
            setup_sh = (
                client.get_file_content(locator.owner, locator.repo, _SETUP_SH, locator.ref)
                if _SETUP_SH in paths
                else None
            )
            pyproject = (
                client.get_file_content(locator.owner, locator.repo, _PYPROJECT, locator.ref)
                if _PYPROJECT in paths
                else None
            )
            nginx_conf = (
                client.get_file_content(locator.owner, locator.repo, _NGINX_CONF, locator.ref)
                if _NGINX_CONF in paths
                else None
            )
            app_conf = (
                client.get_file_content(locator.owner, locator.repo, _APP_CONF, locator.ref)
                if _APP_CONF in paths
                else None
            )

            snapshot = _RepoSnapshot(
                paths=paths,
                truncated=truncated,
                setup_sh=setup_sh,
                pyproject_toml=pyproject,
                nginx_conf=nginx_conf,
                app_conf=app_conf,
            )
            results = validate_keboola_repo(snapshot, type_=type_, runtime_python_pin=None)
        finally:
            client.close()

        verdict = aggregate_verdict(results)
        is_failure = verdict["verdict"] == SEVERITY_BLOCKING or (
            strict and verdict["verdict"] == SEVERITY_WARN
        )

        return {
            "git_repo": git_repo,
            "git_branch": git_branch,
            "type": type_,
            "checks": [r.to_dict() for r in results],
            **verdict,
            "strict": strict,
            "is_failure": is_failure,
            "message": _format_verdict_message(verdict, strict=strict),
        }

    def _parse_github_url(self, url: str, ref: str) -> _GitHubLocator:
        if not url:
            raise KeboolaApiError(
                message="--git-repo is required.",
                status_code=0,
                error_code=ErrorCode.MISSING_PARAMETER,
                retryable=False,
            )
        # Allow common forms: https://github.com/owner/repo,
        # https://github.com/owner/repo.git, http://github.com/...
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError) as exc:
            raise KeboolaApiError(
                message=f"Cannot parse --git-repo {url!r}: {exc}",
                status_code=0,
                error_code=ErrorCode.INVALID_FORMAT,
                retryable=False,
            ) from exc

        host = (parsed.hostname or "").lower()
        if host not in self.GITHUB_HOSTS:
            raise KeboolaApiError(
                message=(
                    f"validate-repo currently supports github.com only "
                    f"(got: {parsed.hostname or url!r}). GitLab / Bitbucket "
                    "tracked as follow-up."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )

        path = (parsed.path or "").strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        parts = path.split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise KeboolaApiError(
                message=(
                    f"Cannot extract owner/repo from {url!r}; expected "
                    "https://github.com/<owner>/<repo>."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_FORMAT,
                retryable=False,
            )
        return _GitHubLocator(owner=parts[0], repo=parts[1], ref=ref)


def _pyproject_declares_deps(content: str | None) -> bool:
    """Heuristic: does the pyproject.toml declare runtime dependencies?

    Looks for a non-empty ``dependencies`` array under ``[project]``,
    ``[tool.poetry.dependencies]``, or ``[tool.uv]`` — the three places
    that matter for `uv sync`. False positives are operationally cheap
    (a WARN nudge); false negatives would skip the setup.sh check.
    """
    if not content:
        return False
    # Cheap regex match for any non-empty dependencies = [...] declaration.
    return bool(
        re.search(r"^\s*dependencies\s*=\s*\[\s*[^\]\s]", content, re.MULTILINE)
        or re.search(
            r"^\[tool\.poetry\.dependencies\]\s*\n[^\[]*?\b\w+\s*=",
            content,
            re.MULTILINE,
        )
    )


def _extract_requires_python(content: str) -> str | None:
    match = _REQUIRES_PYTHON_RE.search(content)
    return match.group(1).strip() if match else None


def _requires_python_above_pin(declared: str, pin: str) -> bool:
    """Return True if ``declared`` requires a Python newer than ``pin``.

    Heuristic only -- declared is a PEP 440 spec like ``>=3.13`` and pin
    is a concrete release like ``3.12.10``. We compare numeric major.minor
    pairs; if the declared lower bound's minor exceeds the pin's minor,
    we flag. False positives on patch-level constraints (``>=3.12.20``
    against pin ``3.12.10``) are accepted as a known limitation -- the
    Keboola runtime pin moves with patch bumps but the help canon does
    not commit to them.
    """
    bound_match = re.search(r">=\s*(\d+)\.(\d+)", declared)
    if not bound_match:
        return False
    declared_major = int(bound_match.group(1))
    declared_minor = int(bound_match.group(2))
    pin_match = re.match(r"(\d+)\.(\d+)", pin)
    if not pin_match:
        return False
    pin_major = int(pin_match.group(1))
    pin_minor = int(pin_match.group(2))
    if declared_major > pin_major:
        return True
    return declared_major == pin_major and declared_minor > pin_minor


def _format_verdict_message(verdict: dict[str, Any], *, strict: bool) -> str:
    blocking = verdict.get("blocking_count", 0)
    warn = verdict.get("warn_count", 0)
    if blocking:
        return (
            f"{blocking} BLOCKING and {warn} WARN check(s). Fix the BLOCKING "
            "entries before `kbagent data-app create`."
        )
    if warn and strict:
        return f"0 BLOCKING and {warn} WARN check(s); --strict treats WARNs as failures."
    if warn:
        return f"0 BLOCKING and {warn} WARN check(s). Repo is deployable; review WARNs."
    return "All checks passed."


# Re-export ConfigError so callers don't import from .errors twice (the
# service module is the natural seam between command and the validation
# function).
__all__ = [
    "SEVERITY_BLOCKING",
    "SEVERITY_OK",
    "SEVERITY_WARN",
    "CheckResult",
    "ConfigError",
    "GitHubContentsClient",
    "RepoValidateService",
    "aggregate_verdict",
    "validate_keboola_repo",
]
