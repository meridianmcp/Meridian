"""Tests for the tunnel-connect hardening: device-code validation and script-safe escaping.

The tunnel-connect page puts the device code and the hostname into an inline script, and the POST stores a token under
whatever device code it is given. These tests pin the two guards: a device code must be the client's UUID shape, and a
value placed in the script must be a JSON string that cannot end the script block.
"""
from __future__ import annotations

import json

import pytest

import meridian.server as srv  # import first -- avoids handler/server import cycle


# A fixed UUID, not uuid.uuid4(): pytest-xdist needs every worker to collect the same test IDs.
@pytest.mark.parametrize("value", ["3f2c9a4e-6b1d-4c7a-9e8f-1a2b3c4d5e6f", "abcdefgh", "a" * 64, "A1-b2-C3-d4"])
def test_device_codes_in_the_client_shape_are_accepted(value):
    assert srv._valid_tunnel_device_code(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abcdefg",            # 7 characters, one short of the minimum
        "a" * 65,             # one over the maximum
        "abc'def123",         # a quote would end a string in the script
        "abc</script>123",    # markup
        "abc&def12345",
        "abc def12345",       # space
        "abc\ndef12345",      # newline
        "ab١٢٣٤٥٦",  # Arabic-Indic digits: alnum but not ASCII
    ],
)
def test_device_codes_outside_the_client_shape_are_refused(value):
    assert srv._valid_tunnel_device_code(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "x'); alert(1); ('",
        "</script><script>alert(1)</script>",
        "a & b < c > d",
        'quote " and \\ backslash',
        "line\nbreak sep",
    ],
)
def test_js_string_round_trips_and_cannot_end_the_script_block(value):
    literal = srv._js_string(value)

    assert json.loads(literal) == value
    assert "<" not in literal and ">" not in literal and "&" not in literal
    assert " " not in literal and " " not in literal
