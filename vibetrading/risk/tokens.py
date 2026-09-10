from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from pydantic import BaseModel

from vibetrading.config import get_settings

DEFAULT_TTL_SECONDS = 30


class RiskApprovalToken(BaseModel):
    """Proof that a specific order was approved by RiskEngine.approve_and_execute.

    Every BrokerClient implementation must verify-and-consume this (via
    verify_and_consume_token) as the first step of place_order, so no code
    path can reach a real broker order without going through the Risk Agent
    — see broker.base.BrokerClient.place_order.
    """

    token_id: str
    signal_id: int | None
    stock_symbol: str
    quantity: int
    issued_at: float
    expires_at: float
    signature: str


def _payload(token_id: str, signal_id: int | None, stock_symbol: str, quantity: int, issued_at: float, expires_at: float) -> str:
    return f"{token_id}|{signal_id}|{stock_symbol}|{quantity}|{issued_at}|{expires_at}"


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_token(
    *,
    signal_id: int | None,
    stock_symbol: str,
    quantity: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    secret: str | None = None,
) -> RiskApprovalToken:
    """Only RiskEngine.approve_and_execute should call this."""
    secret = secret or get_settings().risk_token_secret
    token_id = secrets.token_hex(16)
    issued_at = time.time()
    expires_at = issued_at + ttl_seconds
    signature = _sign(_payload(token_id, signal_id, stock_symbol, quantity, issued_at, expires_at), secret)
    return RiskApprovalToken(
        token_id=token_id,
        signal_id=signal_id,
        stock_symbol=stock_symbol,
        quantity=quantity,
        issued_at=issued_at,
        expires_at=expires_at,
        signature=signature,
    )


def _verify_signature_and_expiry(token: RiskApprovalToken, secret: str) -> bool:
    expected = _sign(
        _payload(token.token_id, token.signal_id, token.stock_symbol, token.quantity, token.issued_at, token.expires_at),
        secret,
    )
    if not hmac.compare_digest(expected, token.signature):
        return False
    return time.time() <= token.expires_at


# Process-wide single-use tracking. This is a single deployable service (see
# the project plan's scheduling rationale), so in-process state is
# sufficient — no distributed coordination needed.
_used_token_ids: set[str] = set()


def verify_and_consume_token(token: RiskApprovalToken | None, *, secret: str | None = None) -> bool:
    """Returns True exactly once for a given valid, unexpired token; False on
    any reuse, forgery, expiry, or missing token. BrokerClient implementations
    must call this before executing an order and refuse to proceed on False.
    """
    if token is None:
        return False
    secret = secret or get_settings().risk_token_secret
    if not _verify_signature_and_expiry(token, secret):
        return False
    if token.token_id in _used_token_ids:
        return False
    _used_token_ids.add(token.token_id)
    return True
