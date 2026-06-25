"""Tests for per-tenant neon_db_url encryption (security item 3dbe23e3).

Covers the two regimes:

* MERIDIAN_MASTER_SECRET UNSET (current prod state) → encrypt/decrypt fall
  back to the legacy global key (zero behavior change), the startup re-key is
  a no-op, and legacy values remain readable.
* MERIDIAN_MASTER_SECRET SET → per-tenant round-trip, "v2:" prefixing,
  dual-read of legacy values, and an idempotent re-key migration that skips
  already-v2 rows and survives one bad row.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from meridian import db as db_module
from meridian import tenant_crypto

PLAINTEXT = "postgresql://user:secret@ep-cool-host.neon.tech/dbname?sslmode=require"
MASTER_SECRET = "unit-test-master-secret-value-0123456789"


@pytest.fixture(autouse=True)
def _clean_crypto_env(monkeypatch):
    """Ensure both crypto env vars start unset and the Fernet cache is clear."""
    monkeypatch.delenv("MERIDIAN_MASTER_SECRET", raising=False)
    monkeypatch.delenv("MERIDIAN_ENCRYPTION_KEY", raising=False)
    db_module._FERNET_INSTANCE = None
    yield
    db_module._FERNET_INSTANCE = None


def _set_global_key(monkeypatch) -> None:
    """Configure a legacy global Fernet key and reset the cache."""
    monkeypatch.setenv("MERIDIAN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    db_module._FERNET_INSTANCE = None


async def _insert_tenant(db, email: str, neon_db_url: str | None) -> str:
    """Create a tenant and set its neon_db_url to a raw stored value."""
    tenant = await db_module.upsert_tenant(db, email)
    if neon_db_url is not None:
        await db.execute(
            "UPDATE tenants SET neon_db_url = ?, neon_project_id = ? WHERE id = ?",
            (neon_db_url, "proj-" + tenant["id"][:6], tenant["id"]),
        )
        await db.commit()
    return tenant["id"]


# ---------------------------------------------------------------------------
# Secret UNSET — zero-behavior-change (legacy global key path)
# ---------------------------------------------------------------------------

def test_secret_unset_encrypt_matches_global(monkeypatch):
    """With the secret unset, encrypt_tenant_db_url == legacy encrypt_field."""
    _set_global_key(monkeypatch)
    tid = "tenant-abc"
    out = tenant_crypto.encrypt_tenant_db_url(tid, PLAINTEXT)
    assert out is not None
    assert not out.startswith(tenant_crypto.V2_PREFIX)
    assert out.startswith("enc:")
    # Decrypts via the plain global path (no tenant key needed).
    assert db_module.decrypt_field(out) == PLAINTEXT


def test_secret_unset_no_key_passthrough(monkeypatch):
    """No global key + no master secret → plaintext passthrough (legacy)."""
    assert not tenant_crypto.master_secret_is_set()
    out = tenant_crypto.encrypt_tenant_db_url("t", PLAINTEXT)
    assert out == PLAINTEXT  # encrypt_field passthrough behavior
    assert tenant_crypto.decrypt_tenant_db_url("t", out) == PLAINTEXT


def test_secret_unset_roundtrip_with_global_key(monkeypatch):
    """Secret unset, global key set → round-trip through the legacy path."""
    _set_global_key(monkeypatch)
    stored = tenant_crypto.encrypt_tenant_db_url("t1", PLAINTEXT)
    assert tenant_crypto.decrypt_tenant_db_url("t1", stored) == PLAINTEXT


def test_secret_unset_legacy_values_readable(monkeypatch):
    """A value written by the old encrypt_field is still readable."""
    _set_global_key(monkeypatch)
    legacy = db_module.encrypt_field(PLAINTEXT)
    assert legacy.startswith("enc:")
    assert tenant_crypto.decrypt_tenant_db_url("any-tenant", legacy) == PLAINTEXT


def test_secret_unset_none_and_empty_passthrough(monkeypatch):
    _set_global_key(monkeypatch)
    assert tenant_crypto.encrypt_tenant_db_url("t", None) is None
    assert tenant_crypto.encrypt_tenant_db_url("t", "") == ""
    assert tenant_crypto.decrypt_tenant_db_url("t", None) is None
    assert tenant_crypto.decrypt_tenant_db_url("t", "") == ""


@pytest.mark.asyncio
async def test_secret_unset_migration_is_noop(db, monkeypatch, caplog):
    """Re-key with the secret unset must change nothing."""
    _set_global_key(monkeypatch)
    legacy = db_module.encrypt_field(PLAINTEXT)
    tid = await _insert_tenant(db, "noop@example.com", legacy)

    counts = await tenant_crypto.rekey_tenant_db_urls(db)
    assert counts == {"rekeyed": 0, "skipped": 0, "failed": 0}

    # Stored value untouched.
    tenant = await db_module.get_tenant_by_id(db, tid)
    assert tenant["neon_db_url"] == legacy


# ---------------------------------------------------------------------------
# Secret SET — per-tenant key path
# ---------------------------------------------------------------------------

def test_secret_set_per_tenant_roundtrip(monkeypatch):
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    assert tenant_crypto.master_secret_is_set()
    stored = tenant_crypto.encrypt_tenant_db_url("tenant-1", PLAINTEXT)
    assert stored.startswith(tenant_crypto.V2_PREFIX)
    assert tenant_crypto.decrypt_tenant_db_url("tenant-1", stored) == PLAINTEXT


def test_secret_set_keys_are_per_tenant(monkeypatch):
    """Different tenant ids derive different keys → cross-decrypt fails."""
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    k1 = tenant_crypto.derive_tenant_key("tenant-1")
    k2 = tenant_crypto.derive_tenant_key("tenant-2")
    assert k1 != k2
    stored = tenant_crypto.encrypt_tenant_db_url("tenant-1", PLAINTEXT)
    with pytest.raises(tenant_crypto.TenantCryptoError):
        tenant_crypto.decrypt_tenant_db_url("tenant-2", stored)


def test_secret_set_dual_read_of_legacy(monkeypatch):
    """Secret set, but a legacy 'enc:' value must still decrypt via global key."""
    _set_global_key(monkeypatch)
    legacy = db_module.encrypt_field(PLAINTEXT)
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    # Global key still configured → legacy dual-read path works.
    assert tenant_crypto.decrypt_tenant_db_url("tenant-1", legacy) == PLAINTEXT
    # And new writes are v2.
    new = tenant_crypto.encrypt_tenant_db_url("tenant-1", PLAINTEXT)
    assert new.startswith(tenant_crypto.V2_PREFIX)


def test_v2_value_without_secret_raises(monkeypatch):
    """A v2 ciphertext cannot be read once the secret is gone."""
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    stored = tenant_crypto.encrypt_tenant_db_url("tenant-1", PLAINTEXT)
    monkeypatch.delenv("MERIDIAN_MASTER_SECRET", raising=False)
    with pytest.raises(tenant_crypto.TenantCryptoError):
        tenant_crypto.decrypt_tenant_db_url("tenant-1", stored)


def test_derive_key_without_secret_raises(monkeypatch):
    with pytest.raises(tenant_crypto.TenantCryptoError):
        tenant_crypto.derive_tenant_key("tenant-1")


def test_corrupt_v2_value_raises(monkeypatch):
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    with pytest.raises(tenant_crypto.TenantCryptoError):
        tenant_crypto.decrypt_tenant_db_url("tenant-1", "v2:not-a-valid-token")


# ---------------------------------------------------------------------------
# Re-key migration with the secret SET
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_rekeys_legacy_to_v2(db, monkeypatch):
    """Legacy global-key rows get re-keyed to per-tenant v2 and stay decryptable."""
    _set_global_key(monkeypatch)
    legacy = db_module.encrypt_field(PLAINTEXT)
    tid = await _insert_tenant(db, "rekey@example.com", legacy)

    # Turn on the master secret (global key remains set so legacy is readable).
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    counts = await tenant_crypto.rekey_tenant_db_urls(db)
    assert counts["rekeyed"] == 1
    assert counts["failed"] == 0

    tenant = await db_module.get_tenant_by_id(db, tid)
    assert tenant["neon_db_url"].startswith(tenant_crypto.V2_PREFIX)
    # The re-keyed value decrypts back to the original plaintext.
    assert tenant_crypto.decrypt_tenant_db_url(tid, tenant["neon_db_url"]) == PLAINTEXT


@pytest.mark.asyncio
async def test_migration_is_idempotent(db, monkeypatch):
    """Re-running the migration skips rows already at v2."""
    _set_global_key(monkeypatch)
    legacy = db_module.encrypt_field(PLAINTEXT)
    tid = await _insert_tenant(db, "idem@example.com", legacy)
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)

    first = await tenant_crypto.rekey_tenant_db_urls(db)
    assert first["rekeyed"] == 1
    after_first = (await db_module.get_tenant_by_id(db, tid))["neon_db_url"]

    second = await tenant_crypto.rekey_tenant_db_urls(db)
    assert second["rekeyed"] == 0
    assert second["skipped"] == 1
    after_second = (await db_module.get_tenant_by_id(db, tid))["neon_db_url"]
    # Value unchanged on the second pass (idempotent, not re-encrypted).
    assert after_first == after_second


@pytest.mark.asyncio
async def test_migration_one_bad_row_does_not_crash(db, monkeypatch):
    """A row that cannot be decrypted is counted failed; others still rekey."""
    _set_global_key(monkeypatch)
    good = db_module.encrypt_field(PLAINTEXT)
    good_tid = await _insert_tenant(db, "good@example.com", good)
    # A corrupt legacy value — 'enc:' prefix but garbage token that fails decrypt.
    bad_tid = await _insert_tenant(db, "bad@example.com", "enc:not-valid-token")

    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    counts = await tenant_crypto.rekey_tenant_db_urls(db)
    assert counts["rekeyed"] == 1
    assert counts["failed"] == 1

    # Good row migrated.
    good_row = await db_module.get_tenant_by_id(db, good_tid)
    assert good_row["neon_db_url"].startswith(tenant_crypto.V2_PREFIX)
    # Bad row left UNTOUCHED — never overwritten with an undecryptable value.
    bad_row = await db_module.get_tenant_by_id(db, bad_tid)
    assert bad_row["neon_db_url"] == "enc:not-valid-token"


@pytest.mark.asyncio
async def test_migration_skips_already_v2_and_empty(db, monkeypatch):
    """Rows already at v2 (and rows with no URL) are skipped, not re-keyed."""
    monkeypatch.setenv("MERIDIAN_MASTER_SECRET", MASTER_SECRET)
    already = tenant_crypto.encrypt_tenant_db_url("seed", PLAINTEXT)
    v2_tid = await _insert_tenant(db, "v2@example.com", already)
    # Tenant with no neon_db_url at all is not selected by the query.
    await _insert_tenant(db, "nodb@example.com", None)

    counts = await tenant_crypto.rekey_tenant_db_urls(db)
    assert counts["rekeyed"] == 0
    assert counts["skipped"] == 1  # only the v2 row is selected + skipped

    # v2 row untouched.
    assert (await db_module.get_tenant_by_id(db, v2_tid))["neon_db_url"] == already
