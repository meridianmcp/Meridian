"""Integration checks for explicit render-policy deferral."""

from meridian_docs import docs_intel
from meridian_docs.render_policy import render_policy_scope


def test_structural_scope_defers_visual_render_after_structural_write() -> None:
    error, info = docs_intel._enforce_render_verification(
        "unused.docx",
        promoted_sha256=None,
        allow_degraded_render=False,
        degraded_render_reason=None,
    )

    # Direct legacy callers retain the existing render gate; only an explicit
    # workflow scope changes the behavior.
    assert error is not None

    with render_policy_scope("structural"):
        error, info = docs_intel._enforce_render_verification(
            "unused.docx",
            promoted_sha256=None,
            allow_degraded_render=False,
            degraded_render_reason=None,
        )

    assert error is None
    assert info == {
        "render_status": "deferred",
        "render_verified": False,
        "render_deferred": True,
        "render_policy": "structural",
        "render_reason": "visual rendering deferred by the active document-workflow policy 'structural'",
    }


def test_policy_scope_is_context_local_and_restored() -> None:
    from meridian_docs import render_policy

    assert render_policy.current_render_policy() is None
    with render_policy_scope("targeted") as active:
        assert active.render is True
        assert render_policy.current_render_policy() is active
    assert render_policy.current_render_policy() is None
