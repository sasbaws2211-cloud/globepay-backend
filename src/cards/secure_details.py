"""Showing a card's full number / expiry / CVV to its owner, e.g. to set up a
subscription.

Bitnob's GET /api/cards/{id}/secure returns those fields encrypted to the RSA
public key registered in its dashboard (Settings -> Controls -> Card
encryption key); settings.BITNOB_CARD_PRIVATE_KEY_B64 holds the private half.
Scheme, per https://bitnob.dev/docs/card-issuing/decrypt-card-secured-details:
  encrypted_key  = RSA-OAEP(SHA-256, MGF1 SHA-256, no label) of a 32-byte AES key
  encrypted_data = nonce(12) || AES-256-GCM ciphertext || tag(16), no AAD

The decrypted values are PCI data: they're returned to the owner once, after
re-entering their password, and are never logged or stored anywhere.
"""

import base64
import binascii
import json
import logging
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from src.auth.models import User
from src.auth.utils import verify_password
from src.cards import bitnob_cards
from src.cards.models import CardStatus, VirtualCard
from src.common.bitnob_client import BitnobError
from src.common.rate_limit import check_rate_limit
from src.config import settings

logger = logging.getLogger(__name__)

# A wrong password costs a slot too, so 5 per 15 minutes also caps guessing.
REVEAL_LIMIT = 5
REVEAL_WINDOW_SECONDS = 15 * 60

_NOT_SET_UP = "Showing card details isn't set up yet. Please try again later."


class CardDecryptError(Exception):
    pass


@lru_cache(maxsize=1)
def _private_key(b64_pem: str) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(base64.b64decode(b64_pem), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CardDecryptError("BITNOB_CARD_PRIVATE_KEY_B64 is not an RSA key")
    return key


def decrypt_secure_details(encrypted_details: dict, b64_pem: str) -> dict:
    """Bitnob's encrypted_details -> the plain card JSON. Raises CardDecryptError
    (wrong key, corrupted payload) - never includes any of the data in it."""
    try:
        aes_key = _private_key(b64_pem).decrypt(
            base64.b64decode(encrypted_details["encrypted_key"]),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        blob = base64.b64decode(encrypted_details["encrypted_data"])
        # AESGCM takes ciphertext with the tag still appended - exactly blob[12:].
        plaintext = AESGCM(aes_key).decrypt(blob[:12], blob[12:], None)
        return json.loads(plaintext)
    except (KeyError, TypeError, ValueError, binascii.Error, InvalidTag) as e:
        raise CardDecryptError(type(e).__name__) from None


async def reveal_card_details(user: User, card: VirtualCard, password: str) -> dict:
    check_rate_limit(f"card-reveal:{user.id}", limit=REVEAL_LIMIT, window_seconds=REVEAL_WINDOW_SECONDS)
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=403, detail="Incorrect password")
    if card.status not in (CardStatus.ACTIVE, CardStatus.FROZEN) or not card.bitnob_card_id:
        raise HTTPException(status_code=400, detail="Details are only available for an active or frozen card")
    if not settings.BITNOB_CARD_PRIVATE_KEY_B64:
        logger.error("Card details requested but BITNOB_CARD_PRIVATE_KEY_B64 is not configured")
        raise HTTPException(status_code=503, detail=_NOT_SET_UP)

    try:
        secure = await bitnob_cards.get_card_secure(card.bitnob_card_id)
    except BitnobError as e:
        message = e.user_message()
        if "encryption key" in message.lower():
            # Public key not (or wrongly) registered in Bitnob's dashboard.
            logger.error("Bitnob card secure details refused: %s", message)
            raise HTTPException(status_code=503, detail=_NOT_SET_UP) from None
        raise HTTPException(status_code=502, detail=f"Couldn't load card details: {message}") from None

    data = secure.get("data") or {}
    if data.get("encrypted_details"):
        try:
            details = decrypt_secure_details(data["encrypted_details"], settings.BITNOB_CARD_PRIVATE_KEY_B64)
        except CardDecryptError as e:
            # Almost always the dashboard key doesn't match our private key.
            logger.error("Couldn't decrypt card secure details for card %s (%s)", card.id, e)
            raise HTTPException(status_code=503, detail=_NOT_SET_UP) from None
    elif isinstance(data.get("details"), dict):
        # Seen live 2026-09-29 (sandbox, no key registered yet): Bitnob sends the
        # details unencrypted under data.details instead of the documented
        # "no encryption key is registered" error. Still TLS end to end.
        logger.warning("Bitnob returned card details unencrypted - register the card encryption key in its dashboard")
        details = data["details"]
    else:
        logger.error("Bitnob card secure details had neither encrypted_details nor details (card %s)", card.id)
        raise HTTPException(status_code=503, detail=_NOT_SET_UP)

    # Billing address isn't sensitive and isn't in the secure payload; the
    # normal card lookup has it. Subscriptions often ask for it (AVS).
    card_data: dict = {}
    try:
        card_resp = (await bitnob_cards.get_card(card.bitnob_card_id)).get("data") or {}
        card_data = card_resp.get("card") or card_resp  # GET /api/cards/{id} nests it under data.card
    except BitnobError:
        pass

    logger.info("Card details revealed to owner (card %s, user %s)", card.id, user.id)
    return {
        "card_number": str(details.get("card_number") or ""),
        "cvv": str(details.get("cvv") or ""),
        "expiry_month": str(details.get("expiry_month") or ""),
        "expiry_year": str(details.get("expiry_year") or ""),
        "name": details.get("name") or card_data.get("name"),
        "card_brand": card.card_brand or card_data.get("card_brand"),
        "billing_address": card_data.get("billing_address"),
    }
