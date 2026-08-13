"""RFC 6238 TOTP code computation, stdlib only.

Used by `login_password` to resolve a TOTP MFA challenge non-interactively
(the whole point of the password-grant login path -- see
`services/auth_service.py`'s `login_password`): given the account's base32
TOTP seed (stored as a CI secret alongside the password), compute the
current 6-digit code instead of prompting a human for one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time


def compute_totp_code(secret_b32: str, *, digits: int = 6, period: int = 30) -> str:
    """Compute the current TOTP code for a base32-encoded secret (RFC 6238).

    Raises ``ValueError`` on a blank or malformed secret -- callers should map
    that to a structured CLI error rather than letting it propagate raw.
    """
    cleaned = secret_b32.strip().upper().replace(" ", "").replace("-", "")
    if not cleaned:
        raise ValueError("TOTP secret is empty")
    # b32decode requires a length that is a multiple of 8; some enrollment
    # UIs hand out an unpadded seed, so pad it back rather than reject a
    # legitimate secret as "not valid base32".
    cleaned += "=" * (-len(cleaned) % 8)
    try:
        key = base64.b32decode(cleaned, casefold=True)
    except binascii.Error as exc:
        raise ValueError(f"TOTP secret is not valid base32: {exc}") from exc
    counter = int(time.time() // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)
