"""macOS keychain store for ``kbagent oauth`` Manage tokens.

One generic-password item per stack: service ``kbagent-oauth``, account = the
oauth host (``oauth.<stack>``). The token never touches ``config.json``.

The item is created with an EMPTY trusted-application list (``-T ""``), so
every read raises a macOS dialog that needs the login password. An AI agent
that runs as the same user cannot answer it. That dialog is the out-of-band
gate; the random-code prompt only shows the human the plan.

"Always Allow" in that dialog adds the reading binary (``/usr/bin/security``)
to the item's ACL and the dialog never comes back. ``trusted_apps`` reads the
ACL (``security dump-keychain -a`` shows it without a prompt and without
secrets) so the service can refuse such an item.

All calls go through ``/usr/bin/security``. The secret is passed on stdin via
``security -i``, never on argv, so it is not visible in ``ps``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import ConfigError, ErrorCode

KEYCHAIN_SERVICE = "kbagent-oauth"
_SECURITY = "/usr/bin/security"
# Exit status of `security find-generic-password` when no item matches.
_ITEM_NOT_FOUND = 44
# Everything we put on the `security -i` stdin line is allow-listed, so no
# value can close a quote, escape one, or start a second command (a newline).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_COMMENT_UNSAFE = re.compile(r"[^A-Za-z0-9 .,:()_+-]")
# Generous: the read waits for a human to type the login password.
_READ_TIMEOUT_SECONDS = 300
_TIMEOUT_SECONDS = 60
_DUMP_ATTEMPTS = 3
_DUMP_RETRY_SECONDS = 1.0

Runner = Callable[..., subprocess.CompletedProcess[str]]


class KeychainError(ConfigError):
    """A keychain operation failed; carries the ErrorCode the command reports."""

    def __init__(self, message: str, error_code: ErrorCode = ErrorCode.OAUTH_IDENTITY_MISSING):
        super().__init__(message)
        self.error_code = error_code


@dataclass
class KeychainItem:
    """Non-secret attributes of a stored identity."""

    host: str
    comment: str


class OAuthKeychain:
    """Read and write ``kbagent-oauth`` items in the login keychain."""

    def __init__(
        self,
        runner: Runner = subprocess.run,
        platform: str = sys.platform,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner = runner
        self._platform = platform
        self._sleep = sleep

    def require_supported(self) -> None:
        """Refuse before any token is sent anywhere on a platform we cannot store it on."""
        if self._platform != "darwin":
            raise KeychainError(
                "kbagent oauth stores tokens in the macOS keychain; this platform is not "
                "supported yet.",
                ErrorCode.CONFIG_ERROR,
            )

    def _run(
        self, args: list[str], *, stdin: str | None = None, timeout: int = _TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        self.require_supported()
        try:
            return self._runner(
                [_SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise KeychainError(
                f"`security {args[0]}` did not finish in {timeout} s (an unanswered dialog?).",
                ErrorCode.PERMISSION_DENIED,
            ) from None

    def store(self, host: str, token: str, comment: str) -> None:
        """Replace the item for ``host`` with a new one that trusts no application.

        Delete first: ``add-generic-password -U`` would keep an existing ACL,
        including an app someone allowed with "Always Allow". After the add,
        the ACL is read back; an item that trusts any app is deleted again.
        """
        if not _HOST_RE.match(host):
            raise KeychainError(f"'{host}' is not a valid host.", ErrorCode.CONFIG_ERROR)
        if not _TOKEN_RE.match(token):
            raise KeychainError("The token has unexpected characters.", ErrorCode.CONFIG_ERROR)
        safe_comment = _COMMENT_UNSAFE.sub("_", comment)[:200]
        self.delete(host)
        command = (
            f'add-generic-password -s "{KEYCHAIN_SERVICE}" -a "{host}" '
            f'-l "kbagent oauth: {host}" -j "{safe_comment}" -T "" -w "{token}"\n'
        )
        result = self._run(["-i"], stdin=command)
        # `security -i` exits 0 even when the inner command fails; check the item exists.
        if result.returncode != 0 or self.find(host) is None:
            raise KeychainError(
                f"Could not store the token in the keychain: {result.stderr.strip()[:200]}",
                ErrorCode.CONFIG_ERROR,
            )
        apps = self.trusted_apps(host)
        if apps != []:
            self.delete(host)
            raise KeychainError(
                f"The new keychain item for {host} does not have an empty trusted-app list "
                f"({apps}); it was deleted.",
                ErrorCode.OAUTH_KEYCHAIN_UNPROTECTED,
            )

    def find(self, host: str) -> KeychainItem | None:
        """Return the item's non-secret attributes (no dialog), or None."""
        result = self._run(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", host])
        if result.returncode != 0:
            return None
        match = re.search(r'"icmt"<blob>="(.*)"', result.stdout)
        return KeychainItem(host=host, comment=match.group(1) if match else "")

    def trusted_apps(self, host: str) -> list[str] | None:
        """Applications the item lets read its secret without a dialog.

        ``[]`` is the safe state. ``None`` means the ACL could not be read or
        parsed -- callers treat that as unsafe. A ``decrypt`` entry with
        ``applications: <null>`` means "any app": ``["<any application>"]``.
        """
        # A dump taken right after a write can miss the new item; look again
        # before reporting it unreadable (a parse failure is final).
        for attempt in range(_DUMP_ATTEMPTS):
            if attempt:
                self._sleep(_DUMP_RETRY_SECONDS)
            result = self._run(["dump-keychain", "-a"])
            if result.returncode != 0:
                return None
            for block in result.stdout.split("keychain: ")[1:]:
                if (
                    f'"svce"<blob>="{KEYCHAIN_SERVICE}"' in block
                    and f'"acct"<blob>="{host}"' in block
                ):
                    return _decrypt_entry_apps(block)
        return None

    def read(self, host: str) -> str:
        """Return the token. macOS asks for the login password first."""
        result = self._run(
            ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", host, "-w"],
            timeout=_READ_TIMEOUT_SECONDS,
        )
        if result.returncode == _ITEM_NOT_FOUND:
            raise KeychainError(
                f"No token stored for {host}. Run `kbagent oauth identity add --stack {host}`."
            )
        if result.returncode != 0 or not result.stdout.strip():
            raise KeychainError(
                "The keychain did not release the token (the dialog was denied or cancelled).",
                ErrorCode.PERMISSION_DENIED,
            )
        return result.stdout.strip()

    def delete(self, host: str) -> None:
        """Delete the item for ``host`` if it exists (no dialog)."""
        self._run(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", host])


def _decrypt_entry_apps(block: str) -> list[str] | None:
    """Union of the applications of every ``decrypt`` ACL entry of one dumped item.

    Fails closed: no decrypt entry at all (format drift) is ``None``, and a
    non-zero count whose names cannot be parsed is reported as unparsed apps.
    """
    entries = re.split(r"\n\s+entry \d+:\n", block)[1:]
    decrypt = [e for e in entries if "decrypt" in e.split("\n", 1)[0]]
    if not decrypt:
        return None
    apps: list[str] = []
    for entry in decrypt:
        if "applications: <null>" in entry:
            apps.append("<any application>")
            continue
        count = re.search(r"applications \((\d+)\):", entry)
        if count is None:
            return None
        if count.group(1) == "0":
            continue
        names = re.findall(r"\n\s+\d+: (\S+)", entry.split("applications", 1)[1])
        apps.extend(names or [f"<{count.group(1)} unparsed applications>"])
    return apps
