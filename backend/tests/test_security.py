from app.core.encryption import decrypt_payload, encrypt_payload
from app.core.security import (
    create_access_token,
    decode_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip():
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert verify_password("correct-horse-battery-staple", hashed)
    assert not verify_password("wrong-password", hashed)


def test_access_token_roundtrip():
    token = create_access_token(subject="user-123", roles=["trader"])
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["roles"] == ["trader"]
    assert payload["type"] == "access"


def test_decode_rejects_garbage_token():
    assert decode_token("not-a-real-token") is None


def test_refresh_token_hash_is_deterministic_and_one_way():
    token = generate_refresh_token()
    assert hash_refresh_token(token) == hash_refresh_token(token)
    assert hash_refresh_token(token) != token


def test_credential_encryption_roundtrip():
    plaintext = '{"api_key": "abc123", "api_secret": "shh"}'
    ciphertext = encrypt_payload(plaintext)
    assert ciphertext != plaintext
    assert decrypt_payload(ciphertext) == plaintext
