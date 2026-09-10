from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from vibetrading.config import get_settings
from vibetrading.core.exceptions import VibeTradingError


class SecretCryptoError(VibeTradingError):
    """Raised when a stored secret can't be decrypted — almost always means
    APP_SECRETS_KEY changed since the value was written."""


def _derive_fernet_key(app_secrets_key: str) -> bytes:
    """APP_SECRETS_KEY can be any free-form string (matching the
    RISK_TOKEN_SECRET convention) rather than requiring a pre-generated
    Fernet key literal; SHA-256 always produces the 32 raw bytes Fernet
    needs, url-safe-base64-encoded as Fernet expects."""
    digest = hashlib.sha256(app_secrets_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def _fernet() -> Fernet:
    return Fernet(_derive_fernet_key(get_settings().app_secrets_key))


def encrypt_value(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretCryptoError(
            "Could not decrypt a stored secret — APP_SECRETS_KEY likely changed since it was saved. "
            "Re-enter the affected credential(s) in Settings."
        ) from exc
