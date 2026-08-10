"""RFC 6238 TOTP code computation, stdlib only.

Used by `login_password` to resolve a TOTP MFA challenge non-interactively
(the whole point of the password-grant login path -- see
`services/auth_service.py`'s `login_password`): given the account's base32
TOTP seed (stored as a CI secret alongside the password), compute the
current 6-digit code instead of prompting a human for one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def compute_totp_code(secret_b32: str, *, digits: int = 6, period: int = 30) -> str:
    """Compute the current TOTP code for a base32-encoded secret (RFC 6238)."""
    key = base64.b32decode(secret_b32.strip().upper().replace(" ", ""), casefold=True)
    counter = int(time.time() // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)
