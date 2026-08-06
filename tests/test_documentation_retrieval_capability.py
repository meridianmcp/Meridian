"""92ac025c — optional, version-pinned documentation retrieval (Context7).

Covers:
  (a) the example capability declaration validates against
      capability_manifest.py's REAL, unmodified schema (proves no new schema
      field was needed, and that the constant can't silently drift).
  (b) synthesize_documentation_cache_key: deterministic, sensitive to each
      input, rejects empty library_id/query.
  (c) classify_documentation_response: every documented Context7 failure
      shape (taken from the live source — see documentation_retrieval.py's
      own docstring) is recognized; a genuine docs response is not
      misclassified as a failure.
  (d) agent_defaults.py: the stale get-library-docs tool name is gone, the
      corrected query-docs name is present, the module constant AND the
      text-embedded <!-- meridian-executor-standard --> marker agree (a real
      self-inconsistency bug would be a version bump that forgets the
      embedded marker), and staleness detection still works correctly for a
      pre-v17 stored copy.
  (e) executor_contract._DEFAULT_ROUTING_CATEGORIES: the new "documentation"
      category matches its own keywords, does not fire on unrelated item
      text, and does not shadow/get shadowed by an existing category
      (first-match-wins ordering integrity).
"""
from __future__ import annotations

import pytest

from meridian import agent_defaults
from meridian import capability_manifest
from meridian import documentation_retrieval as docret
from meridian import executor_contract


# ---------------------------------------------------------------------------
# (a) capability declaration validates against the real schema
# ---------------------------------------------------------------------------


def test_example_capability_validates_against_real_schema():
    normalized = capability_manifest.normalize_capability(
        docret.EXAMPLE_DOCUMENTATION_RETRIEVAL_CAPABILITY
    )
    assert normalized["id"] == docret.DOCUMENTATION_RETRIEVAL_CAPABILITY_ID
    assert normalized["availability_policy"] == "optional"
    assert normalized["fallback_chain"] == ["github_search", "paper_search"]
    assert normalized["required_tools"] == [
        "context7__resolve-library-id", "context7__query-docs",
    ]


def test_example_capability_carries_no_secret_or_local_path():
    # normalize_capability itself raises CapabilityManifestError on a secret-
    # shaped string or an absolute local path (_check_no_secrets_or_local_paths)
    # -- successfully normalizing above already proves this, but assert the
    # provenance text explicitly to guard against a future edit reintroducing one.
    provenance = docret.EXAMPLE_DOCUMENTATION_RETRIEVAL_CAPABILITY["provenance"]
    capability_manifest._check_no_secrets_or_local_paths(provenance, path="provenance")


def test_full_manifest_containing_the_example_capability_normalizes():
    manifest = capability_manifest.normalize_manifest(
        [docret.EXAMPLE_DOCUMENTATION_RETRIEVAL_CAPABILITY]
    )
    assert manifest[0]["id"] == "documentation_retrieval"


# ---------------------------------------------------------------------------
# (b) cache key synthesis
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic():
    a = docret.synthesize_documentation_cache_key("/vercel/next.js/v15.1.8", "how do I use middleware")
    b = docret.synthesize_documentation_cache_key("/vercel/next.js/v15.1.8", "how do I use middleware")
    assert a == b
    assert a.startswith("context7:")


def test_cache_key_differs_by_library_id():
    a = docret.synthesize_documentation_cache_key("/vercel/next.js/v15.1.8", "q")
    b = docret.synthesize_documentation_cache_key("/vercel/next.js/v14.0.0", "q")
    assert a != b


def test_cache_key_differs_by_query():
    a = docret.synthesize_documentation_cache_key("/vercel/next.js", "middleware")
    b = docret.synthesize_documentation_cache_key("/vercel/next.js", "routing")
    assert a != b


def test_cache_key_differs_by_last_update_date():
    a = docret.synthesize_documentation_cache_key("/vercel/next.js", "q", "2026-01-01")
    b = docret.synthesize_documentation_cache_key("/vercel/next.js", "q", "2026-06-01")
    assert a != b


def test_cache_key_last_update_date_optional_and_defaults_consistently():
    a = docret.synthesize_documentation_cache_key("/vercel/next.js", "q")
    b = docret.synthesize_documentation_cache_key("/vercel/next.js", "q", None)
    assert a == b


@pytest.mark.parametrize("library_id,query", [("", "q"), ("  ", "q"), ("/lib", ""), ("/lib", "   ")])
def test_cache_key_rejects_empty_library_id_or_query(library_id, query):
    with pytest.raises(ValueError):
        docret.synthesize_documentation_cache_key(library_id, query)


# ---------------------------------------------------------------------------
# (c) response classification — every failure shape from the live source
# ---------------------------------------------------------------------------


def test_classify_recognizes_library_not_found():
    text = "The library you are trying to access does not exist. Please check the library ID."
    result = docret.classify_documentation_response(text)
    assert result == {"ok": False, "reason": "library_not_found"}


def test_classify_recognizes_invalid_api_key():
    text = "Invalid API key. The key should start with 'ctx7sk'."
    result = docret.classify_documentation_response(text)
    assert result == {"ok": False, "reason": "invalid_api_key"}


def test_classify_recognizes_not_finalized():
    text = "This library is not yet finalized. Please try again shortly."
    result = docret.classify_documentation_response(text)
    assert result == {"ok": False, "reason": "not_finalized"}


def test_classify_recognizes_rate_limited():
    text = "You have been rate limited. Visit the dashboard for higher limits."
    result = docret.classify_documentation_response(text)
    assert result == {"ok": False, "reason": "rate_limited"}


def test_classify_recognizes_unprocessable():
    text = "This library is too large to process."
    result = docret.classify_documentation_response(text)
    assert result == {"ok": False, "reason": "unprocessable"}


def test_classify_recognizes_empty_response():
    assert docret.classify_documentation_response("") == {"ok": False, "reason": "empty_response"}
    assert docret.classify_documentation_response(None) == {"ok": False, "reason": "empty_response"}
    assert docret.classify_documentation_response("   ") == {"ok": False, "reason": "empty_response"}


def test_classify_a_genuine_docs_response_is_ok():
    text = (
        "## Middleware\n\nNext.js middleware allows you to run code before a "
        "request is completed...\n\n```js\nexport function middleware(request) {\n"
        "  return NextResponse.next()\n}\n```"
    )
    assert docret.classify_documentation_response(text) == {"ok": True}


def test_classify_case_insensitive():
    assert docret.classify_documentation_response("RATE LIMIT EXCEEDED") == {
        "ok": False, "reason": "rate_limited",
    }


# ---------------------------------------------------------------------------
# (d) agent_defaults.py — stale tool name fixed, marker/constant agree
# ---------------------------------------------------------------------------


def test_stale_get_library_docs_name_is_gone():
    """get-library-docs may still appear as an explicit "don't use this,
    Context7 retired it" warning -- what must be gone is the OLD broken
    instruction telling a session to actually call it."""
    assert "resolve-library-id` then `get-library-docs" not in agent_defaults.DEFAULT_AGENT_INSTRUCTIONS
    assert "NOT `get-library-docs`" in agent_defaults.DEFAULT_AGENT_INSTRUCTIONS


def test_corrected_query_docs_name_is_present():
    assert "query-docs" in agent_defaults.DEFAULT_AGENT_INSTRUCTIONS
    assert "resolve-library-id" in agent_defaults.DEFAULT_AGENT_INSTRUCTIONS


def test_version_constant_and_embedded_marker_agree():
    """A version bump that forgets the text-embedded marker is a real,
    self-inconsistent bug: parse_standard_version reads ONLY the embedded
    marker, so the freshly-generated default would immediately register as
    stale against its own governing constant."""
    embedded = agent_defaults.parse_standard_version(agent_defaults.DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == agent_defaults.AGENT_INSTRUCTIONS_STANDARD_VERSION == 17


def test_fresh_default_instructions_are_not_stale():
    assert agent_defaults.agent_instructions_stale(agent_defaults.DEFAULT_AGENT_INSTRUCTIONS) is False


def test_a_stored_v16_copy_is_now_correctly_flagged_stale():
    # Must "look like" a standard doc (agent_instructions_stale's own gate:
    # contains "Meridian" and "start_session") or it's treated as genuinely
    # bespoke instructions and never flagged, regardless of any marker.
    stored_v16 = (
        "# Meridian executor rules\nCall start_session first.\n"
        "<!-- meridian-executor-standard: v16 -->"
    )
    assert agent_defaults.agent_instructions_stale(stored_v16) is True


def test_untrusted_content_and_no_write_authorization_language_present():
    text = agent_defaults.DEFAULT_AGENT_INSTRUCTIONS
    assert "untrusted" in text.lower()
    assert "authoriz" in text.lower()  # matches "authorize" or "authorization"


# ---------------------------------------------------------------------------
# (e) deterministic routing category
# ---------------------------------------------------------------------------


def test_documentation_category_matches_framework_docs_phrase():
    hint = executor_contract.infer_default_routing_category({
        "title": "Look up the framework docs for this library's API",
        "notes": "",
    })
    assert hint is not None
    assert hint["routing_category"] == "documentation"
    assert hint["server_or_namespace"] == "context7"
    assert hint["name"] == "resolve-library-id"
    assert hint["required_or_preferred"] == "preferred"
    assert "meridian: github_search" in hint["fallback"]


def test_documentation_category_matches_context7_mention():
    hint = executor_contract.infer_default_routing_category({
        "title": "Check context7 for the latest version",
        "notes": "",
    })
    assert hint is not None
    assert hint["routing_category"] == "documentation"


def test_documentation_category_does_not_fire_on_unrelated_text():
    hint = executor_contract.infer_default_routing_category({
        "title": "Fix the login button color",
        "notes": "It should be blue, not red.",
    })
    assert hint is None


def test_documentation_category_does_not_shadow_code_investigation():
    """A code-investigation item mentioning an unrelated word must still hit
    the EARLIER code_investigation category, not documentation -- proves
    the new category's keywords were chosen narrowly enough not to
    false-positive against existing categories (first-match-wins integrity)."""
    hint = executor_contract.infer_default_routing_category({
        "title": "Investigate why the auth library raises an exception",
        "notes": "trace the root cause",
    })
    assert hint is not None
    assert hint["routing_category"] == "code_investigation"


def test_documentation_category_is_not_shadowed_by_docx():
    hint = executor_contract.infer_default_routing_category({
        "title": "Read the library documentation for this third-party library",
        "notes": "",
    })
    assert hint is not None
    assert hint["routing_category"] == "documentation"
