"""Shared low-level HTTP + auth client for Bitnob's API - SANDBOX ONLY.

DO NOT add a live-mode base URL or a "use_sandbox" toggle to this file.
This platform is not licensed for cross-border money transmission or
card issuance (full reasoning in the conversation history). Every
feature built on Bitnob exists to demo the product vision to investors
with zero real money ever moving for real, and should stay that way
until a funded, licensed version of the business is a deliberate
decision, not an accident of someone changing a config value.

IMPORTANT - safety note specific to this provider: Bitnob's API has a
SINGLE host (https://api.bitnob.com) for both sandbox and production -
there is no separate sandbox subdomain to hardcode. Sandbox-vs-live is
determined entirely by which CLIENT_ID/CLIENT_SECRET pair you
configure (generated from Bitnob's dashboard while toggled to
sandbox/test mode, plus that app's IP allowlist). The code cannot
enforce that for you - verify it in the Bitnob dashboard before ever
setting BITNOB_CLIENT_ID/BITNOB_CLIENT_SECRET in .env.

Auth (confirmed from https://bitnob.dev/api-reference/authentication -
NOT the Bearer-token scheme an earlier web search incorrectly
suggested, which cost a real debugging round-trip against a live 401):
HMAC-SHA256, four headers on every request:
  X-Auth-Client:    CLIENT_ID
  X-Auth-Timestamp: unix timestamp in seconds
  X-Auth-Nonce:     16-byte hex-encoded random value
  X-Auth-Signature: hex(HMAC-SHA256(CLIENT_SECRET, message))
message = "{CLIENT_ID}:{TIMESTAMP}:{NONCE}:{PAYLOAD}"
PAYLOAD = exact JSON request body string, or "" if there is no body.

Shared by src/crossborder/bitnob.py (payouts) and src/cards/bitnob_cards.py
(virtual cards) - kept in one place so a fix to the signing logic (or a
bug in it) applies everywhere at once, not just wherever it's noticed first.
"""

import hashlib
import hmac
import json as _json
import secrets
import time
from typing import Any

import httpx

from src.config import settings

BASE_URL = "https://api.bitnob.com"


class BitnobError(Exception):
    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body

    def user_message(self) -> str:
        """The generic "Bitnob API error (400)" from str(self) is useless
        to an API caller - Bitnob's own error bodies carry a real, specific
        `detail` (e.g. "rate limit exceeded: please wait 2 seconds..."),
        confirmed present across every error hit while building this
        integration. This surfaces that instead, wherever it's available."""
        if isinstance(self.response_body, str):
            try:
                parsed = _json.loads(self.response_body)
                if isinstance(parsed, dict) and parsed.get("detail"):
                    return parsed["detail"]
            except ValueError:
                pass
        return str(self)


def _headers(payload: str) -> dict[str, str]:
    client_id = settings.BITNOB_CLIENT_ID
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)  # 16 bytes -> 32 hex chars

    message = f"{client_id}:{timestamp}:{nonce}:{payload}"
    signature = hmac.new(
        settings.BITNOB_CLIENT_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "X-Auth-Client": client_id,
        "X-Auth-Timestamp": timestamp,
        "X-Auth-Nonce": nonce,
        "X-Auth-Signature": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def request(method: str, path: str, json_body: dict | None = None) -> dict:
    if not settings.BITNOB_CLIENT_ID or not settings.BITNOB_CLIENT_SECRET:
        raise BitnobError("Bitnob sandbox credentials not configured")

    payload = _json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
    headers = _headers(payload)

    # Network failures (confirmed live: intermittent ConnectTimeouts) and
    # non-JSON bodies become BitnobError, which callers already handle -
    # otherwise they escaped as raw httpx errors and surfaced as 500s.
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
            resp = await client.request(method, path, content=payload if json_body is not None else None, headers=headers)
    except httpx.HTTPError as e:
        raise BitnobError(f"Couldn't reach Bitnob ({type(e).__name__})") from e

    if resp.status_code >= 400:
        raise BitnobError(f"Bitnob API error ({resp.status_code})", resp.text)

    try:
        return resp.json()
    except ValueError as e:
        raise BitnobError("Bitnob returned an unreadable response", resp.text) from e
