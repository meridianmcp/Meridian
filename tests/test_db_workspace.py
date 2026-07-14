"""Smoke tests for ARCH 3 module split (816de5d3).

Verifies that:
1. meridian.db.workspace exports all key workspace functions (canonical home after
   the ARCH 3 extraction).
2. meridian.db re-exports all of them so existing ``db_module.add_workspace_note(...)``
   call sites continue to work without modification.
3. Both references point to the *same* object (no accidental duplication/copy).
"""


def test_workspace_module_exports_expected_functions():
    """meridian.db.workspace exports all workspace functions, and meridian.db
    re-exports them so existing call sites are unaffected (ARCH 3, 816de5d3)."""
    import meridian.db.workspace as workspace
    import meridian.db as db

    # Private helpers
    private_helpers = [
        "_WORKSPACE_SETTINGS_ID",
        "_VALID_PROPOSAL_STATUSES",
        "_PROPOSAL_TRANSITIONS",
        "_VALID_WS_SPRINT_STATUSES",
        "_ws_tenant_clause",
        "_ws_settings_key",
    ]

    # Public workspace-note functions
    note_functions = [
        "add_workspace_note",
        "get_workspace_notes",
        "delete_workspace_note",
        "move_workspace_note_to_project",
        "update_workspace_note",
    ]

    # Public workspace-decision functions
    decision_functions = [
        "pin_workspace_decision",
        "get_workspace_decisions",
        "delete_workspace_decision",
    ]

    # Public workspace-proposal functions
    proposal_functions = [
        "add_workspace_proposal",
        "get_workspace_proposals",
        "advance_workspace_proposal_status",
        "promote_workspace_proposal",
        "delete_workspace_proposal",
    ]

    # Public workspace sprint-board functions
    sprint_functions = [
        "add_workspace_sprint_item",
        "get_workspace_sprint_items",
        "update_workspace_sprint_item",
        "complete_workspace_sprint_item",
    ]

    # Public workspace-settings functions
    settings_functions = [
        "get_workspace_settings",
        "update_workspace_settings",
        "seed_workspace_settings_from_toml",
    ]

    # Public workspace-member / invite functions
    member_functions = [
        "create_workspace_invite",
        "get_workspace_invite_by_token_hash",
        "accept_workspace_invite",
        "get_pending_invites_for_email",
        "resolve_member_role",
        "workspace_member_accepted_for_email",
        "get_workspace_member_by_id",
        "refresh_workspace_invite_token",
        "list_workspace_members",
        "count_workspace_members",
        "delete_workspace_member",
        "update_workspace_member",
        "get_workspaces_for_email",
        "get_scoped_project_ids_for_member",
    ]

    all_names = (
        private_helpers
        + note_functions
        + decision_functions
        + proposal_functions
        + sprint_functions
        + settings_functions
        + member_functions
    )

    for name in all_names:
        # Must exist in the submodule (canonical home after ARCH 3)
        assert hasattr(workspace, name), f"meridian.db.workspace missing: {name}"
        # Must be re-exported via db.__init__ (backward compat)
        assert hasattr(db, name), f"meridian.db missing re-export of: {name}"
        # Both references must point to the same object (not copies)
        assert getattr(workspace, name) is getattr(db, name), (
            f"meridian.db.{name} is not the same object as meridian.db.workspace.{name}"
        )
