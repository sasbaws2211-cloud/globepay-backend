"""Decryption of Bitnob's GET /api/cards/{id}/secure payload, built exactly as
https://bitnob.dev/docs/card-issuing/decrypt-card-secured-details describes:
RSA-OAEP(SHA-256) wrapped AES-256 key, data = nonce(12) || ciphertext || tag(16)."""

import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.cards.secure_details import CardDecryptError, decrypt_secure_details

CARD = {
    "card_id": "test-card", "card_number": "4111111111111111", "cvv": "123",
    "expiry_month": "09", "expiry_year": "2029", "name": "AMA MENSAH", "balance": 5.0,
}


def _keypair() -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return key, base64.b64encode(pem).decode()


def _bitnob_encrypt(public_key, payload: dict) -> dict:
    aes_key, nonce = os.urandom(32), os.urandom(12)
    sealed = AESGCM(aes_key).encrypt(nonce, json.dumps(payload).encode(), None)  # ciphertext || tag
    wrapped = public_key.encrypt(
        aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    return {
        "encrypted_key": base64.b64encode(wrapped).decode(),
        "encrypted_data": base64.b64encode(nonce + sealed).decode(),
        "algorithm": "RSA-OAEP-256+AES-256-GCM",
    }


def test_round_trip():
    key, b64 = _keypair()
    assert decrypt_secure_details(_bitnob_encrypt(key.public_key(), CARD), b64) == CARD


def test_wrong_private_key_is_refused():
    key, _ = _keypair()
    _, other_b64 = _keypair()
    with pytest.raises(CardDecryptError):
        decrypt_secure_details(_bitnob_encrypt(key.public_key(), CARD), other_b64)


def test_tampered_data_is_refused():
    key, b64 = _keypair()
    enc = _bitnob_encrypt(key.public_key(), CARD)
    blob = bytearray(base64.b64decode(enc["encrypted_data"]))
    blob[20] ^= 0x01
    enc["encrypted_data"] = base64.b64encode(bytes(blob)).decode()
    with pytest.raises(CardDecryptError):
        decrypt_secure_details(enc, b64)


@pytest.mark.parametrize("bad", [{}, {"encrypted_key": "not base64!", "encrypted_data": "x"}, {"encrypted_key": None}])
def test_malformed_payload_is_refused(bad):
    _, b64 = _keypair()
    with pytest.raises(CardDecryptError):
        decrypt_secure_details(bad, b64)


def test_error_never_contains_card_data():
    key, _ = _keypair()
    _, other_b64 = _keypair()
    with pytest.raises(CardDecryptError) as exc:
        decrypt_secure_details(_bitnob_encrypt(key.public_key(), CARD), other_b64)
    assert CARD["card_number"] not in str(exc.value) and CARD["cvv"] not in str(exc.value)
