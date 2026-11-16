"""Ed25519 signatures in pure Python, as RFC 8032 section 5.1 defines them.

Used by the release publisher to sign SHA256SUMS and by tests. The updater checks the same signatures with
verify(). Standard library only (hashlib), so no dependency is added.

Points use extended coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z and x*y = T/Z. The arithmetic is not
constant time. That is acceptable here: the seed is used on the release machine only, never to answer
requests from an untrusted party.

    public_key(seed) -> 32 bytes
    sign(seed, message) -> 64 bytes
    verify(public, message, signature) -> bool, never raises for bad input
"""

from __future__ import annotations

import hashlib

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _recover_x(y: int, sign_bit: int) -> int | None:
    """The x with the given parity that lies on the curve for y, or None when there is none."""
    if y >= _P:
        return None
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - xx) % _P != 0:
        return None
    if x == 0 and sign_bit:
        return None
    if (x & 1) != sign_bit:
        x = _P - x
    return x


def _add(p: tuple, q: tuple) -> tuple:
    """Unified point addition, add-2008-hwcd-3 for a = -1."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    d = z1 * 2 * z2 % _P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _double(p: tuple) -> tuple:
    """Point doubling, dbl-2008-hwcd for a = -1."""
    x1, y1, z1, _ = p
    a = x1 * x1 % _P
    b = y1 * y1 % _P
    c = 2 * z1 * z1 % _P
    d = -a % _P
    e = ((x1 + y1) * (x1 + y1) - a - b) % _P
    g = (d + b) % _P
    f = (g - c) % _P
    h = (d - b) % _P
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


_IDENTITY = (0, 1, 1, 0)


def _scalar_mult(k: int, p: tuple) -> tuple:
    result = _IDENTITY
    addend = p
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _double(addend)
        k >>= 1
    return result


_BY = 4 * _inv(5) % _P
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % _P)


def _encode(p: tuple) -> bytes:
    x_coord, y_coord, z_coord, _ = p
    z_inv = _inv(z_coord)
    x = x_coord * z_inv % _P
    y = y_coord * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    sign_bit = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign_bit)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _hash_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _L


def _expand(seed: bytes) -> tuple[int, bytes]:
    """The clamped secret scalar and the prefix that the nonce is derived from (RFC 8032 section 5.1.5)."""
    digest = hashlib.sha512(seed).digest()
    scalar_bytes = bytearray(digest[:32])
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 127
    scalar_bytes[31] |= 64
    return int.from_bytes(scalar_bytes, "little"), digest[32:]


def _check_seed(seed: bytes) -> bytes:
    seed = bytes(seed)
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    return seed


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key for a 32-byte seed."""
    seed = _check_seed(seed)
    scalar, _ = _expand(seed)
    return _encode(_scalar_mult(scalar, _B))


def sign(seed: bytes, message: bytes) -> bytes:
    """The 64-byte signature of message under the 32-byte seed. Deterministic."""
    seed = _check_seed(seed)
    message = bytes(message)
    scalar, prefix = _expand(seed)
    public = _encode(_scalar_mult(scalar, _B))
    nonce = _hash_int(prefix + message)
    r_point = _encode(_scalar_mult(nonce, _B))
    challenge = _hash_int(r_point + public + message)
    s_value = (nonce + challenge * scalar) % _L
    return r_point + s_value.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only when signature is a valid Ed25519 signature of message under public. Bad input gives False."""
    try:
        public = bytes(public)
        message = bytes(message)
        signature = bytes(signature)
    except (TypeError, ValueError):
        return False
    if len(public) != 32 or len(signature) != 64:
        return False
    key_point = _decode(public)
    r_bytes = signature[:32]
    r_point = _decode(r_bytes)
    if key_point is None or r_point is None:
        return False
    s_value = int.from_bytes(signature[32:], "little")
    if s_value >= _L:
        return False
    challenge = _hash_int(r_bytes + public + message)
    left = _scalar_mult(s_value, _B)
    right = _add(r_point, _scalar_mult(challenge, key_point))
    return _encode(left) == _encode(right)
