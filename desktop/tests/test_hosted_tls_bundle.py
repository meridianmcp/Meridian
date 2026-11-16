"""The default hosted host uses the certifi CA bundle. Other hosts keep the system default.

The live handshake is checked by hand (see the PR). These tests check the decisions with a fake _open_exchange,
so they need no network.
"""

from __future__ import annotations

import ssl
import urllib.request

import pytest

from meridian_desktop import hosted_client as h

DEFAULT_URL = "https://usemeridian.us/projects"
CUSTOM_URL = "https://hosted.example.com/projects"


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url)


def test_default_host_gets_a_verifying_context_built_from_certifi():
    context = h._tls_context_for(_request(DEFAULT_URL))
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    loaded = context.get_ca_certs()
    # certifi ships the full public root set, so this is far more than one certificate.
    assert len(loaded) > 100
    common_names = [dict(pair for rdn in cert["subject"] for pair in rdn).get("commonName") for cert in loaded]
    assert "ISRG Root X1" in common_names


def test_custom_host_uses_the_system_default():
    assert h._tls_context_for(_request(CUSTOM_URL)) is None


def test_transport_passes_the_bundle_for_the_default_host_and_none_for_custom(monkeypatch):
    seen: list[ssl.SSLContext | None] = []

    def fake(request, timeout, deadline, context):
        seen.append(context)
        return h.Reply(200, b"[]")

    monkeypatch.setattr(h, "_open_exchange", fake)
    h._urllib_transport(_request(DEFAULT_URL), 5)
    h._urllib_transport(_request(CUSTOM_URL), 5)
    assert isinstance(seen[0], ssl.SSLContext)
    assert seen[1] is None


def test_refused_certificate_maps_to_unreachable(monkeypatch):
    def fake(request, timeout, deadline, context):
        raise h._CertificateRefused()

    monkeypatch.setattr(h, "_open_exchange", fake)
    with pytest.raises(h.HostedUnreachable):
        h._send(h._urllib_transport, _request(DEFAULT_URL), 5)


def test_the_pinned_root_is_gone():
    assert not hasattr(h, "PINNED_ROOT_PEM")
    assert not hasattr(h, "_pinned_context")
