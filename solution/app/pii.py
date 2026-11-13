from __future__ import annotations

import base64
import hashlib
import hmac
import os
from cryptography.fernet import Fernet, InvalidToken


class PiiProtector:
    """Encrypt PII at rest and derive a keyed, non-reversible phone lookup token.

    The encryption key must be supplied through PII_ENCRYPTION_KEY and kept stable
    for the lifetime of the database. Do not commit it or put it in campaigns.json.
    """

    def __init__(self, key: str | bytes):
        raw_key = key.encode("ascii") if isinstance(key, str) else key
        try:
            decoded = base64.urlsafe_b64decode(raw_key)
            if len(decoded) != 32:
                raise ValueError("Fernet key must decode to exactly 32 bytes")
            self._fernet = Fernet(raw_key)
        except Exception as exc:
            raise ValueError(
                "PII_ENCRYPTION_KEY must be a valid Fernet key; generate one with "
                "Fernet.generate_key() and keep it stable across restarts"
            ) from exc
        self._index_key = hmac.new(decoded, b"assignment-b/phone-index/v1", hashlib.sha256).digest()

    @classmethod
    def from_env(cls) -> "PiiProtector":
        key = os.getenv("PII_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "PII_ENCRYPTION_KEY is required. Generate one with "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        return cls(key)

    def encrypt(self, value: str) -> bytes:
        if not isinstance(value, str):
            raise TypeError("PII value must be a string")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes | bytearray | memoryview) -> str:
        try:
            return self._fernet.decrypt(bytes(value)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise ValueError("Unable to decrypt persisted PII; check PII_ENCRYPTION_KEY") from exc

    def phone_token(self, phone: str) -> str:
        if not isinstance(phone, str) or not phone.strip():
            raise ValueError("Phone number must be a non-empty string")
        # The mock provider compares fixture phone strings exactly. Preserve the exact
        # fixture value here rather than normalizing it differently from the provider.
        canonical = phone.encode("utf-8")
        return hmac.new(self._index_key, canonical, hashlib.sha256).hexdigest()
