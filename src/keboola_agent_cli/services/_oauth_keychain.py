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
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import ConfigError, ErrorCode

KEYCHAIN_SERVICE = "kbagent-oauth"
_SECURITY = "/usr/bin/security"
# Exit status of `security find-generic-password` when no item matches.
_ITEM_NOT_FOUND = 44
# A Manage token is [0-9]+-[A-Za-z0-9]+; anything else could break the
# `security -i` command line we build on stdin.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Generous: the read waits for a human to type the login password.
_READ_TIMEOUT_SECONDS = 300
_TIMEOUT_SECONDS = 60

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

    def __init__(self, runner: Runner = subprocess.run, platform: str = sys.platform) -> None:
        self._run = runner
        self._platform = platform

    def _require_macos(self) -> None:
        if self._platform != "darwin":
            raise KeychainError(
                "kbagent oauth stores tokens in the macOS keychain; this platform is not "
                "supported yet.",
                ErrorCode.CONFIG_ERROR,
            )

    def store(self, host: str, token: str, comment: str) -> None:
        """Replace the item for ``host`` with a new one that trusts no application.

        Delete first: ``add-generic-password -U`` would keep an existing ACL,
        including an app someone allowed with "Always Allow".
        """
        self._require_macos()
        if not _TOKEN_RE.match(token):
            raise KeychainError("The token has unexpected characters.", ErrorCode.CONFIG_ERROR)
        self.delete(host)
        safe_comment = comment.replace('"', "'")
        command = (
            f'add-generic-password -s "{KEYCHAIN_SERVICE}" -a "{host}" '
            f'-l "kbagent oauth: {host}" -j "{safe_comment}" -T "" -w "{token}"\n'
        )
        result = self._run(
            [_SECURITY, "-i"],
            input=command,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        # `security -i` exits 0 even when the inner command fails; check the item exists.
        if result.returncode != 0 or self.find(host) is None:
            raise KeychainError(
                f"Could not store the token in the keychain: {result.stderr.strip()[:200]}",
                ErrorCode.CONFIG_ERROR,
            )

    def find(self, host: str) -> KeychainItem | None:
        """Return the item's non-secret attributes (no dialog), or None."""
        self._require_macos()
        result = self._run(
            [_SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", host],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        match = re.search(r'"icmt"<blob>="(.*)"', result.stdout)
        return KeychainItem(host=host, comment=match.group(1) if match else "")

    def trusted_apps(self, host: str) -> list[str] | None:
        """Applications the item lets read its secret without a dialog.

        ``[]`` is the safe state. ``None`` means the ACL could not be read.
        A ``decrypt`` entry with ``applications: <null>`` means "any app" and
        is reported as ``["<any application>"]``.
        """
        result = self._run(
            [_SECURITY, "dump-keychain", "-a"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        for block in result.stdout.split("keychain: "):
            if f'"svce"<blob>="{KEYCHAIN_SERVICE}"' in block and f'"acct"<blob>="{host}"' in block:
                return _decrypt_entry_apps(block)
        return None

    def read(self, host: str) -> str:
        """Return the token. macOS asks for the login password first."""
        self._require_macos()
        try:
            result = self._run(
                [_SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", host, "-w"],
                capture_output=True,
                text=True,
                timeout=_READ_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise KeychainError("Nobody answered the keychain dialog in time.") from None
        if result.returncode == _ITEM_NOT_FOUND:
            raise KeychainError(
                f"No token stored for {host}. Run `kbagent oauth identity add --stack {host}`."
            )
        if result.returncode != 0 or not result.stdout.strip():
            raise KeychainError("The keychain did not release the token (dialog denied?).")
        return result.stdout.strip()

    def delete(self, host: str) -> None:
        """Delete the item for ``host`` if it exists (no dialog)."""
        self._require_macos()
        self._run(
            [_SECURITY, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", host],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )


def _decrypt_entry_apps(block: str) -> list[str]:
    """Parse the applications of the ``decrypt`` ACL entry of one dumped item."""
    entries = re.split(r"\n\s+entry \d+:\n", block)
    for entry in entries:
        if "decrypt" not in entry.split("\n", 1)[0]:
            continue
        if re.search(r"applications: <null>", entry):
            return ["<any application>"]
        count = re.search(r"applications \((\d+)\):", entry)
        if count is None or count.group(1) == "0":
            return []
        return re.findall(r"\d+: (\S+)", entry.split("applications", 1)[1])
    return []
