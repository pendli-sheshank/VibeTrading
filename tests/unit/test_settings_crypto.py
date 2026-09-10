from __future__ import annotations

import pytest

from vibetrading.config import get_settings
from vibetrading.settings.crypto import SecretCryptoError, _fernet, decrypt_value, encrypt_value


@pytest.fixture(autouse=True)
def _clear_fernet_cache():
    """The Fernet instance is cached off get_settings().app_secrets_key at
    first use; clear between tests so each test's key mutation takes effect."""
    _fernet.cache_clear()
    yield
    _fernet.cache_clear()


def test_encrypt_decrypt_round_trip():
    plaintext = "super-secret-dhan-token"
    ciphertext = encrypt_value(plaintext)
    assert ciphertext != plaintext
    assert decrypt_value(ciphertext) == plaintext


def test_ciphertext_does_not_contain_plaintext():
    ciphertext = encrypt_value("my-api-key-12345")
    assert "my-api-key-12345" not in ciphertext


def test_decrypt_with_wrong_key_raises_secret_crypto_error(monkeypatch):
    ciphertext = encrypt_value("value-a")

    monkeypatch.setattr(get_settings(), "app_secrets_key", "a-totally-different-key")
    _fernet.cache_clear()

    with pytest.raises(SecretCryptoError):
        decrypt_value(ciphertext)


def test_different_plaintexts_produce_different_ciphertexts():
    a = encrypt_value("value-a")
    b = encrypt_value("value-b")
    assert a != b
