"""Validation for environment-based hook bearer authentication."""
from __future__ import annotations

from meridian.hook_auth import bearer_headers_from_env


def test_meridian_token_precedes_bearer_token():
    assert bearer_headers_from_env(
        {"MERIDIAN_TOKEN": "meridian-token", "BEARER_TOKEN": "fallback-token"}
    ) == {"Authorization": "Bearer meridian-token"}


def test_bearer_token_is_supported_as_fallback():
    assert bearer_headers_from_env({"BEARER_TOKEN": "hosted-token"}) == {
        "Authorization": "Bearer hosted-token"
    }


def test_missing_or_invalid_token_produces_no_header():
    assert bearer_headers_from_env({}) == {}
    assert bearer_headers_from_env({"MERIDIAN_TOKEN": "bad\r\nHeader: injected"}) == {}
