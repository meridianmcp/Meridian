"""Tests for meridian_desktop/ed25519.py: the RFC 8032 section 7.1 vectors and the verify rules the updater and the
release publisher rely on, plus the shape of the release public key.

Pure Python and in memory only: no file, no network, no subprocess. The release key is read as a value, never a seed.
"""

from __future__ import annotations

import base64

import pytest

from meridian_desktop import ed25519
from meridian_desktop.release_key import RELEASE_PUBLIC_KEY_B64

GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

# RFC 8032 section 7.1, TEST 1 to TEST 3 and SHA(abc): secret key, public key, message, signature.
RFC_VECTORS = [
    pytest.param(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        id="rfc8032-test-1",
    ),
    pytest.param(
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
        id="rfc8032-test-2",
    ),
    pytest.param(
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
        id="rfc8032-test-3",
    ),
    pytest.param(
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
        id="rfc8032-sha-abc",
    ),
]

SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
MESSAGE = b"release SHA256SUMS line"
SIGNATURE = ed25519.sign(SEED, MESSAGE)


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_VECTORS)
def test_public_key_matches_the_rfc_vector(seed, public, message, signature):
    assert ed25519.public_key(bytes.fromhex(seed)).hex() == public


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_VECTORS)
def test_signing_matches_the_rfc_vector(seed, public, message, signature):
    assert ed25519.sign(bytes.fromhex(seed), bytes.fromhex(message)).hex() == signature


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC_VECTORS)
def test_verify_accepts_the_rfc_vector(seed, public, message, signature):
    assert ed25519.verify(bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)) is True


def test_signing_is_deterministic():
    assert ed25519.sign(SEED, MESSAGE) == ed25519.sign(SEED, MESSAGE)


def test_sizes_are_32_byte_keys_and_64_byte_signatures():
    assert len(PUBLIC) == 32
    assert len(SIGNATURE) == 64


def test_verify_accepts_a_fresh_signature():
    assert ed25519.verify(PUBLIC, MESSAGE, SIGNATURE) is True


def test_verify_rejects_a_changed_message():
    assert ed25519.verify(PUBLIC, MESSAGE + b"!", SIGNATURE) is False


def test_verify_rejects_a_one_octet_changed_message():
    assert ed25519.verify(PUBLIC, b"\x73", SIGNATURE) is False


@pytest.mark.parametrize("index", [0, 31, 32, 63])
def test_verify_rejects_a_changed_signature_byte(index):
    changed = bytearray(SIGNATURE)
    changed[index] ^= 0x01
    assert ed25519.verify(PUBLIC, MESSAGE, bytes(changed)) is False


def test_verify_rejects_another_key():
    other_public = ed25519.public_key(bytes(range(32)))
    assert ed25519.verify(other_public, MESSAGE, SIGNATURE) is False


def test_verify_rejects_a_public_key_that_is_not_the_signers_key():
    not_a_key = (2).to_bytes(32, "little")  # a small y: whatever it decodes to, it is not the signer's key
    assert ed25519.verify(not_a_key, MESSAGE, SIGNATURE) is False


def test_verify_rejects_an_s_value_at_or_above_the_group_order():
    s_value = int.from_bytes(SIGNATURE[32:], "little")
    malleated = SIGNATURE[:32] + (s_value + GROUP_ORDER).to_bytes(32, "little")
    assert s_value + GROUP_ORDER < 2**256  # the forged S fits in 32 bytes, so only the range check refuses it
    assert ed25519.verify(PUBLIC, MESSAGE, malleated) is False


@pytest.mark.parametrize("length", [0, 31, 33])
def test_verify_rejects_a_public_key_of_the_wrong_length(length):
    assert ed25519.verify(PUBLIC[:length] if length < 32 else PUBLIC + b"\x00" * (length - 32), MESSAGE, SIGNATURE) is False


@pytest.mark.parametrize("length", [0, 63, 65])
def test_verify_rejects_a_signature_of_the_wrong_length(length):
    assert ed25519.verify(PUBLIC, MESSAGE, SIGNATURE[:length] if length < 64 else SIGNATURE + b"\x00") is False


def test_verify_rejects_a_public_key_that_is_not_a_curve_point():
    not_a_point = (2**255 - 1).to_bytes(32, "little")  # y is not below the field prime
    assert ed25519.verify(not_a_point, MESSAGE, SIGNATURE) is False


def test_verify_rejects_a_non_canonical_point_encoding():
    # y = 2^255 - 18, which is p + 1: the same value as 1, not an encoding of a point, so it is refused.
    non_canonical = (2**255 - 18).to_bytes(32, "little")
    assert ed25519.verify(non_canonical, MESSAGE, SIGNATURE) is False
    assert ed25519.verify(PUBLIC, MESSAGE, non_canonical + SIGNATURE[32:]) is False


def test_verify_never_raises_on_wrong_types():
    assert ed25519.verify(None, MESSAGE, SIGNATURE) is False
    assert ed25519.verify(PUBLIC, None, SIGNATURE) is False
    assert ed25519.verify(PUBLIC, MESSAGE, "not bytes") is False


@pytest.mark.parametrize("length", [0, 31, 33])
def test_a_seed_must_be_32_bytes(length):
    with pytest.raises(ValueError):
        ed25519.public_key(bytes(length))
    with pytest.raises(ValueError):
        ed25519.sign(bytes(length), MESSAGE)


def test_a_round_trip_with_a_fresh_seed_verifies_and_a_wrong_message_does_not():
    seed = bytes(range(32))
    public = ed25519.public_key(seed)
    signature = ed25519.sign(seed, b"Meridian Desktop 0.3.0")
    assert ed25519.verify(public, b"Meridian Desktop 0.3.0", signature) is True
    assert ed25519.verify(public, b"Meridian Desktop 0.3.1", signature) is False


def test_the_release_public_key_is_a_32_byte_key():
    raw = base64.b64decode(RELEASE_PUBLIC_KEY_B64, validate=True)
    assert len(raw) == 32
