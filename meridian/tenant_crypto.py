"""Per-tenant encryption of ``tenants.neon_db_url`` at rest.

Background
----------
Historically every tenant's Neon connection string was encrypted with a
single global Fernet key (``MERIDIAN_ENCRYPTION_KEY``) via
:func:`meridian.db.encrypt_field` / :func:`meridian.db.decrypt_field`. One
leaked global key would expose *every* tenant's database credentials.

This module upgrades that to a **per-tenant key** derived from a single root
secret (``MERIDIAN_MASTER_SECRET``) using HKDF-SHA256 salted with the tenant
id. A leaked derived key only exposes one tenant; the root secret never
touches the database.

Ciphertext format
-----------------
Per-tenant ciphertext is prefixed with the literal marker ``"v2:"`` followed
by the Fernet token. Legacy global-key ciphertext keeps its existing
``"enc:"`` prefix (or is bare plaintext on installs that never set
``MERIDIAN_ENCRYPTION_KEY``). The ``"v2:"`` marker lets every read decide
between the two key regimes and makes re-encryption idempotent.

SAFETY — ZERO-BEHAVIOR-CHANGE WHEN THE SECRET IS UNSET
------------------------------------------------------
``MERIDIAN_MASTER_SECRET`` is **unset in production today**. Deploying this
module to prod is a guaranteed no-op until a human sets that secret and
restarts:

* :func:`encrypt_tenant_db_url` with the secret unset falls back verbatim to
  the existing global :func:`meridian.db.encrypt_field` — identical bytes to
  the old code path.
* :func:`decrypt_tenant_db_url` always dual-reads: ``"v2:"`` values use the
  per-tenant key, everything else uses the existing global
  :func:`meridian.db.decrypt_field`. Legacy values stay readable forever.
* :func:`rekey_tenant_db_urls` is a NO-OP when the secret is unset (it logs a
  debug line and returns immediately) and is fully idempotent when set
  (rows already at ``"v2:"`` are skipped). It NEVER overwrites a row whose
  current value it could not first decrypt.

The re-key only activates once the human sets ``MERIDIAN_MASTER_SECRET`` and
restarts the server. Until then, behavior is byte-for-byte the legacy path.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

# Marker prefix for per-tenant (v2) ciphertext. Distinct from the legacy
# global-key "enc:" prefix used by db.encrypt_field.
V2_PREFIX = "v2:"

_ENV_MASTER_SECRET = "MERIDIAN_MASTER_SECRET"

# HKDF info string — domain-separates this key usage from any future derived
# key so the same root secret could safely derive keys for other purposes.
_HKDF_INFO = b"neon_db_url"


class TenantCryptoError(Exception):
    """Raised when per-tenant encryption/decryption fails irrecoverably."""


def _master_secret() -> bytes | None:
    """Return the raw master secret bytes, or None when unset/empty.

    Read fresh from the environment on every call so tests (and a human
    setting the secret then restarting) see changes without a process
    restart at the Python level. Returns None when unset or blank — the
    signal callers use to fall back to the legacy global key path.
    """
    raw = os.environ.get(_ENV_MASTER_SECRET, "")
    if not raw:
        return None
    return raw.encode() if isinstance(raw, str) else raw


def master_secret_is_set() -> bool:
    """True when MERIDIAN_MASTER_SECRET is present and non-empty."""
    return _master_secret() is not None


def derive_tenant_key(tenant_id: str) -> bytes:
    """Derive a Fernet key for ``tenant_id`` from the master secret.

    ``HKDF-SHA256(key_material=MERIDIAN_MASTER_SECRET, salt=tenant_id,
    info=b"neon_db_url")`` → 32 bytes → urlsafe-base64 → a valid Fernet key.

    Raises :class:`TenantCryptoError` when the master secret is unset — every
    caller that derives a key has already checked :func:`master_secret_is_set`,
    so reaching here without a secret is a programming error.
    """
    secret = _master_secret()
    if secret is None:
        raise TenantCryptoError(
            "MERIDIAN_MASTER_SECRET is not set — cannot derive a per-tenant key"
        )
    # Imported lazily so importing this module never forces the (heavy)
    # cryptography backend to load on code paths that don't encrypt.
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=tenant_id.encode(),
        info=_HKDF_INFO,
    )
    raw_key = hkdf.derive(secret)
    return base64.urlsafe_b64encode(raw_key)


def _tenant_fernet(tenant_id: str) -> Any:
    from cryptography.fernet import Fernet  # noqa: PLC0415

    return Fernet(derive_tenant_key(tenant_id))


def encrypt_tenant_db_url(tenant_id: str, plaintext: str | None) -> str | None:
    """Encrypt a tenant connection string for storage.

    * Master secret SET → per-tenant Fernet, returns ``"v2:" + token``.
    * Master secret UNSET → delegates to the legacy global
      :func:`meridian.db.encrypt_field` (byte-for-byte the old behavior).

    Empty/None values pass through unchanged (matching encrypt_field).
    """
    if not plaintext:
        return plaintext
    if not master_secret_is_set():
        from . import db as db_module  # noqa: PLC0415

        return db_module.encrypt_field(plaintext)
    token = _tenant_fernet(tenant_id).encrypt(plaintext.encode()).decode()
    return V2_PREFIX + token


def decrypt_tenant_db_url(tenant_id: str, stored: str | None) -> str | None:
    """Decrypt a stored tenant connection string (DUAL-READ).

    * ``"v2:"`` prefix → per-tenant key (requires the master secret).
    * anything else (``"enc:"`` or bare plaintext) → legacy global
      :func:`meridian.db.decrypt_field`.

    This keeps every legacy value readable during and after the transition.
    On failure raises :class:`TenantCryptoError` rather than returning
    garbage, so callers never silently route to a wrong/empty DB URL.
    """
    if not stored:
        return stored
    if stored.startswith(V2_PREFIX):
        if not master_secret_is_set():
            raise TenantCryptoError(
                f"tenant {tenant_id} has a v2 (per-tenant) ciphertext but "
                f"{_ENV_MASTER_SECRET} is not set — cannot decrypt"
            )
        try:
            token = stored[len(V2_PREFIX):]
            return _tenant_fernet(tenant_id).decrypt(token.encode()).decode()
        except Exception as exc:  # noqa: BLE001
            raise TenantCryptoError(
                f"failed to decrypt v2 neon_db_url for tenant {tenant_id}"
            ) from exc
    # Legacy path — global key (or plaintext passthrough).
    from . import db as db_module  # noqa: PLC0415

    try:
        return db_module.decrypt_field(stored)
    except Exception as exc:  # noqa: BLE001
        raise TenantCryptoError(
            f"failed to decrypt legacy neon_db_url for tenant {tenant_id}"
        ) from exc


async def rekey_tenant_db_urls(db: Any) -> dict[str, int]:
    """Re-encrypt every legacy tenant ``neon_db_url`` with its per-tenant key.

    Runs at server startup, but ONLY does work when
    ``MERIDIAN_MASTER_SECRET`` is set. When the secret is unset this is a
    pure NO-OP: it logs a debug line and returns immediately, guaranteeing
    a deploy to prod (where the secret is currently unset) changes nothing.

    For each tenant whose ``neon_db_url`` is non-empty and NOT already
    ``"v2:"``:

    1. dual-read decrypt the current value (legacy global key / plaintext),
    2. re-encrypt it under the per-tenant key,
    3. ``UPDATE`` the row.

    Idempotent — rows already at ``"v2:"`` are skipped, so re-running on a
    fully-migrated DB does nothing. Per-row try/except means one bad row
    cannot crash the others or server boot, and a row is NEVER overwritten
    with a value that could not first be decrypted.

    Returns a counts dict: ``{"rekeyed", "skipped", "failed"}``.
    """
    counts = {"rekeyed": 0, "skipped": 0, "failed": 0}
    if not master_secret_is_set():
        _log.debug(
            "%s unset — skipping per-tenant neon_db_url re-key (no-op)",
            _ENV_MASTER_SECRET,
        )
        return counts

    from . import db as db_module  # noqa: PLC0415

    # Pull every tenant that actually has a stored URL. Use the raw query
    # (not list_tenants_with_neon, which filters on neon_project_id) so
    # custom-connect tenants (URL present, no Neon project) are included too.
    async with db.execute(
        "SELECT id, neon_db_url FROM tenants WHERE neon_db_url IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()

    for row in rows:
        if row is None:
            continue
        rec = db_module._row_to_dict(row)
        tenant_id = rec.get("id")
        stored = rec.get("neon_db_url")
        if not tenant_id or not stored:
            counts["skipped"] += 1
            continue
        if stored.startswith(V2_PREFIX):
            counts["skipped"] += 1
            continue
        try:
            # Step 1: must successfully decrypt BEFORE we ever write.
            plaintext = decrypt_tenant_db_url(tenant_id, stored)
            if not plaintext:
                # Empty decrypt → don't touch the row; treat as skip.
                counts["skipped"] += 1
                continue
            # Step 2: re-encrypt per-tenant.
            new_value = encrypt_tenant_db_url(tenant_id, plaintext)
            # Step 3: persist. ? placeholder — pg_adapter converts to %s.
            await db.execute(
                "UPDATE tenants SET neon_db_url = ? WHERE id = ?",
                (new_value, tenant_id),
            )
            await db.commit()
            counts["rekeyed"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not crash boot
            counts["failed"] += 1
            _log.warning(
                "per-tenant re-key failed for tenant %s: %s",
                tenant_id, type(exc).__name__,
            )
            continue

    if counts["rekeyed"] or counts["failed"]:
        _log.info(
            "per-tenant neon_db_url re-key: %d rekeyed, %d skipped, %d failed",
            counts["rekeyed"], counts["skipped"], counts["failed"],
        )
    return counts
