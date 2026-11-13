import pytest
from cryptography.fernet import Fernet

from app.pii import PiiProtector


def test_pii_encryption_round_trip_and_randomized_ciphertext():
    protector = PiiProtector(Fernet.generate_key())
    value = "+919876543210"
    first = protector.encrypt(value)
    second = protector.encrypt(value)
    assert first != second
    assert value.encode() not in first
    assert protector.decrypt(first) == value
    assert protector.decrypt(second) == value


def test_phone_token_is_deterministic_and_keyed():
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()
    protector_a = PiiProtector(key_a)
    protector_b = PiiProtector(key_b)
    phone = "+919876543210"
    assert protector_a.phone_token(phone) == protector_a.phone_token(phone)
    assert protector_a.phone_token(phone) != protector_a.phone_token("+919876543211")
    assert protector_a.phone_token(phone) != protector_b.phone_token(phone)
    assert phone not in protector_a.phone_token(phone)


def test_phone_token_rejects_empty_or_non_string_values():
    protector = PiiProtector(Fernet.generate_key())
    with pytest.raises(ValueError, match="non-empty"):
        protector.phone_token("   ")
    with pytest.raises(ValueError, match="non-empty"):
        protector.phone_token("")
    with pytest.raises(ValueError, match="non-empty"):
        protector.phone_token(None)


def test_invalid_encryption_key_fails_closed():
    with pytest.raises(ValueError, match="valid Fernet key"):
        PiiProtector("not-a-valid-fernet-key")
