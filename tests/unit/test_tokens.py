from __future__ import annotations

import time

from vibetrading.risk.tokens import mint_token, verify_and_consume_token


def test_valid_token_verifies_once():
    token = mint_token(signal_id=1, stock_symbol="TCS", quantity=10, secret="test-secret")
    assert verify_and_consume_token(token, secret="test-secret") is True


def test_token_cannot_be_reused():
    token = mint_token(signal_id=1, stock_symbol="TCS", quantity=10, secret="test-secret")
    assert verify_and_consume_token(token, secret="test-secret") is True
    assert verify_and_consume_token(token, secret="test-secret") is False


def test_missing_token_is_rejected():
    assert verify_and_consume_token(None, secret="test-secret") is False


def test_token_signed_with_wrong_secret_is_rejected():
    token = mint_token(signal_id=1, stock_symbol="TCS", quantity=10, secret="right-secret")
    assert verify_and_consume_token(token, secret="wrong-secret") is False


def test_forged_token_with_tampered_field_is_rejected():
    token = mint_token(signal_id=1, stock_symbol="TCS", quantity=10, secret="test-secret")
    tampered = token.model_copy(update={"quantity": 999})
    assert verify_and_consume_token(tampered, secret="test-secret") is False


def test_expired_token_is_rejected():
    token = mint_token(signal_id=1, stock_symbol="TCS", quantity=10, ttl_seconds=0, secret="test-secret")
    time.sleep(0.01)
    assert verify_and_consume_token(token, secret="test-secret") is False


def test_different_tokens_are_independent():
    token_a = mint_token(signal_id=1, stock_symbol="TCS", quantity=10, secret="test-secret")
    token_b = mint_token(signal_id=2, stock_symbol="INFY", quantity=5, secret="test-secret")
    assert verify_and_consume_token(token_a, secret="test-secret") is True
    assert verify_and_consume_token(token_b, secret="test-secret") is True
