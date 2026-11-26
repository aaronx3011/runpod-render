"""Signed per-attempt tokens (fencing).

Token = base64url(attempt_id) "." fence "." hex(HMAC-SHA256(secret, attempt_id|fence)).
The control plane stores the current fence for each job; a callback whose fence is older
than the job's current fence is rejected even though its signature is valid. Workers never
hold the signing secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass


class InvalidToken(ValueError):
    pass


@dataclass(frozen=True)
class AttemptClaims:
    attempt_id: str
    fence: int


def _mac(secret: bytes, attempt_id: str, fence: int) -> str:
    return hmac.new(secret, f"{attempt_id}|{fence}".encode(), hashlib.sha256).hexdigest()


def sign_attempt(secret: bytes, attempt_id: str, fence: int) -> str:
    if not secret:
        raise ValueError("empty signing secret")
    if fence < 0:
        raise ValueError("fence must be >= 0")
    aid = base64.urlsafe_b64encode(attempt_id.encode()).decode().rstrip("=")
    return f"{aid}.{fence}.{_mac(secret, attempt_id, fence)}"


def verify_attempt(secret: bytes, token: str) -> AttemptClaims:
    try:
        aid_b64, fence_s, mac = token.split(".")
        attempt_id = base64.urlsafe_b64decode(aid_b64 + "=" * (-len(aid_b64) % 4)).decode()
        fence = int(fence_s)
    except Exception as exc:  # malformed in any way
        raise InvalidToken("malformed attempt token") from exc
    if not hmac.compare_digest(mac, _mac(secret, attempt_id, fence)):
        raise InvalidToken("bad attempt token signature")
    return AttemptClaims(attempt_id=attempt_id, fence=fence)
