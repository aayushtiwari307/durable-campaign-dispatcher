from __future__ import annotations

import hashlib
import hmac
import re


def signature_for(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, secret: str, supplied_signature: str | None) -> bool:
    if not supplied_signature:
        return False
    candidate = supplied_signature.strip().lower()
    # The provider uses a bare 64-character hexadecimal SHA-256 digest.
    # Validate ASCII/hex input first so malformed Unicode headers fail closed
    # instead of making compare_digest raise TypeError and returning HTTP 500.
    if re.fullmatch(r"[0-9a-f]{64}", candidate, flags=re.ASCII) is None:
        return False
    expected = signature_for(raw_body, secret)
    return hmac.compare_digest(expected, candidate)
