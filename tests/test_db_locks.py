"""Smoke tests for ARCH 2 module split (7f121d14).

Verifies that:
1. meridian.db.locks exports all key claim/lock functions (canonical home after
   the ARCH 2 extraction).
2. meridian.db re-exports all of them so existing ``db_module.claim_file(...)``
   call sites continue to work without modification.
3. Both references point to the *same* object (no accidental duplication/copy).
"""


def test_locks_module_exports_expected_functions():
    """meridian.db.locks exports all claim/lock functions, and meridian.db
    re-exports them so existing call sites are unaffected (ARCH 2, 7f121d14)."""
    import meridian.db.locks as locks
    import meridian.db as db

    # Public constants
    constants = [
        "_FILE_LOCK_TTL_HOURS",
        "_CLAIM_LIVE_HOURS",
    ]

    # Private helpers also referenced directly by tests and callers
    private_helpers = [
        "_cutoff_dt",
        "_normalize_file_path",
        "_code_notes_for_session_file",
        "_decision_notes_for_session_file",
        "_other_read_claims",
        "_all_read_claims",
        "_claim_file_read",
        "_live_symbol_claims_for_file",
        "_ranges_overlap",
        "_live_docx_region_claims_for_file",
        "_migrate_docx_region_claims",
    ]

    # Public file-lock functions
    file_lock_functions = [
        "expire_file_locks",
        "expire_stale_symbol_claims",
        "expire_file_read_claims",
        "claim_file",
        "release_file",
        "release_file_locks_for_session",
        "get_file_conflict_warnings",
        "get_file_claims",
    ]

    # Public resource-lock functions
    resource_lock_functions = [
        "expire_resource_locks",
        "claim_resource",
        "release_resource",
        "release_resource_locks_for_session",
        "get_resource_claims",
        "get_resource_conflicts",
    ]

    # Public symbol-claim functions
    symbol_claim_functions = [
        "claim_symbol",
        "get_symbol_claims",
        "release_symbol_claims_for_session",
        "get_symbol_hotspots",
        "get_hotspot_suggestions",
    ]

    # Public docx-region claim functions
    docx_claim_functions = [
        "claim_docx_region",
        "get_docx_region_claims",
        "release_docx_region_claims",
        "check_docx_region_write_conflict",
    ]

    # Session file claims view
    session_functions = [
        "get_session_file_claims",
    ]

    all_names = (
        constants
        + private_helpers
        + file_lock_functions
        + resource_lock_functions
        + symbol_claim_functions
        + docx_claim_functions
        + session_functions
    )

    for name in all_names:
        # Must exist in the submodule (canonical home after ARCH 2)
        assert hasattr(locks, name), f"meridian.db.locks missing: {name}"
        # Must be re-exported via db.__init__ (backward compat)
        assert hasattr(db, name), f"meridian.db missing re-export of: {name}"
        # Both references must point to the same object (not copies)
        assert getattr(locks, name) is getattr(db, name), (
            f"meridian.db.{name} is not the same object as meridian.db.locks.{name}"
        )
