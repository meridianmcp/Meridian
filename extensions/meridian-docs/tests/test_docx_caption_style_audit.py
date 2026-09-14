"""Tests for audit_caption_style (8e2f4a17) and the user-settable
journal-style-preset CRUD surface it was built alongside.

Covers:
  - audit_caption_style: caption_label_not_bold /
    caption_label_unexpectedly_bold, caption_label_punctuation_mismatch,
    caption_terminal_punctuation_mismatch -- each in isolation, plus the
    "unset policy key -> don't check" gating.
  - Regression: a correctly-formatted "Fig. N Description" caption (bold
    label, non-bold description, no label punctuation) must NOT be flagged
    -- a first implementation of the bold check counted the separator space
    between the bold label run and the description run as part of the
    label, producing a false positive on every correctly-formatted caption
    with no label punctuation (caught testing against a real 124-caption
    document: 17 + 71 false positives before the fix).
  - Table vs figure captions are checked against their own independent
    policy keys.
  - Read-only invariant: audit_caption_style never mutates the .docx.
  - load_user_journal_style_presets / save_user_journal_style_preset /
    delete_user_journal_style_preset / list_journal_style_presets: round
    trip, missing-file-is-empty-not-error, malformed-file-raises,
    validation-before-write, and the AMEND (not replace) behavior when a
    user preset shares a built-in's name -- a first implementation did a
    flat dict-level replace that silently dropped every built-in key the
    user's override didn't restate; regression-tested explicitly below.

All tests use synthetic .docx bytes built inline -- no real files, no
network. Mirrors test_docx_equation_style_audit.py's fixture conventions.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import pytest

_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel, server  # noqa: E402

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_NS_HEADER = f'xmlns:w="{_W}" xmlns:w14="{_W14}"'


def _doc(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body>
{body_xml}
  </w:body>
</w:document>"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml))
    return str(path)


def _caption_para(label_bold: str, rest: str, para_id: str = "00000001") -> str:
    """A caption paragraph matching the real convention this function was
    built against: the "Fig. N"/"Table N" label in its own bold run,
    followed by a separate non-bold run for the rest of the text."""
    return (
        f'<w:p w14:paraId="{para_id}"><w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:r><w:rPr><w:b/></w:rPr><w:t>{label_bold}</w:t></w:r>'
        f'<w:r><w:t xml:space="preserve">{rest}</w:t></w:r>'
        f"</w:p>"
    )


def _body_para(text: str, para_id: str = "00000002") -> str:
    return f'<w:p w14:paraId="{para_id}"><w:r><w:t>{text}</w:t></w:r></w:p>'


# ---------------------------------------------------------------------------
# Basic detection + the false-positive regression
# ---------------------------------------------------------------------------

def test_correctly_formatted_caption_no_label_punctuation_no_findings(tmp_path):
    """Regression: "Fig. 1" bold + " Description" non-bold, no period after
    the number -- must NOT be flagged as caption_label_not_bold. A first
    implementation counted the boundary space into the bold label's
    expected length and flagged the (correctly non-bold) description run
    for it."""
    xml = _doc(_caption_para("Fig. 1", " A representative crack image"))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(
        path,
        style_policy={
            "figure_caption_bold": True,
            "figure_caption_label_punctuation": "none",
        },
    )
    assert result["caption_count"] == 1
    assert result["finding_count"] == 0, result["findings"]


def test_correctly_formatted_caption_with_period_label_no_findings(tmp_path):
    xml = _doc(_caption_para("Table 1.", " Summary statistics"))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(
        path,
        style_policy={
            "table_caption_bold": True,
            "table_caption_label_punctuation": "period",
        },
    )
    assert result["finding_count"] == 0, result["findings"]


def test_not_bold_label_is_flagged_when_policy_requires_bold(tmp_path):
    xml = _doc(
        f'<w:p w14:paraId="00000003"><w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:r><w:t>Fig. 1</w:t></w:r>'
        f'<w:r><w:t xml:space="preserve"> Not bold at all</w:t></w:r>'
        f"</w:p>"
    )
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(path, style_policy={"figure_caption_bold": True})
    assert result["finding_count"] == 1
    assert result["findings"][0]["type"] == "caption_label_not_bold"
    assert result["findings"][0]["kind"] == "figure"


def test_bold_when_policy_expects_non_bold_is_flagged(tmp_path):
    xml = _doc(_caption_para("Fig. 1", " Bold when it shouldn't be"))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(path, style_policy={"figure_caption_bold": False})
    assert result["finding_count"] == 1
    assert result["findings"][0]["type"] == "caption_label_unexpectedly_bold"


def test_label_punctuation_mismatch(tmp_path):
    xml = _doc(_caption_para("Fig. 1.", " Has a period, policy forbids it"))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(
        path, style_policy={"figure_caption_label_punctuation": "none"}
    )
    assert result["finding_count"] == 1
    f = result["findings"][0]
    assert f["type"] == "caption_label_punctuation_mismatch"
    assert f["expected_punctuation"] == "none"
    assert f["actual_punctuation"] == "period"


def test_terminal_punctuation_mismatch_independent_of_label(tmp_path):
    """A caption with a CORRECT label (no period after the number) but an
    INCORRECT trailing period at the very end must be flagged by the
    terminal check even though the label check passes -- the two rules are
    independent."""
    xml = _doc(_caption_para("Fig. 1", " Description ending badly."))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(
        path,
        style_policy={
            "figure_caption_label_punctuation": "none",
            "figure_caption_terminal_punctuation": "",
        },
    )
    assert result["finding_count"] == 1
    f = result["findings"][0]
    assert f["type"] == "caption_terminal_punctuation_mismatch"
    assert f["expected_terminal_punctuation"] == ""
    assert f["actual_terminal_punctuation"] == "."


def test_unset_policy_keys_skip_all_checks(tmp_path):
    """A caption that would fail every check still produces zero findings
    when every relevant policy key is left at its default (None/
    "unspecified") -- "no verified rule" must mean "don't check", not
    "assume a default and check against it"."""
    xml = _doc(_caption_para("Fig. 1.", " Not bold, has a period, ends badly."))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(path)  # no style_policy at all
    assert result["caption_count"] == 1
    assert result["finding_count"] == 0, result["findings"]


def test_table_and_figure_captions_checked_independently(tmp_path):
    body = _caption_para("Fig. 1", " ok figure", "00000004") + _caption_para(
        "Table 1.", " table with a label period", "00000005"
    )
    xml = _doc(body)
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(
        path,
        style_policy={
            "figure_caption_label_punctuation": "none",
            "table_caption_label_punctuation": "none",
        },
    )
    assert result["caption_count"] == 2
    assert result["finding_count"] == 1
    assert result["findings"][0]["kind"] == "table"


def test_non_caption_style_paragraphs_are_ignored(tmp_path):
    xml = _doc(_body_para("Fig. 1 mentioned in running prose, not a caption"))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(path, style_policy={"figure_caption_bold": True})
    assert result["caption_count"] == 0
    assert result["finding_count"] == 0


def test_caption_styled_paragraph_without_a_label_pattern_is_skipped(tmp_path):
    xml = _doc(
        f'<w:p w14:paraId="00000006"><w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve"> </w:t></w:r></w:p>'
    )
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(path, style_policy={"figure_caption_bold": True})
    assert result["caption_count"] == 0


def test_invalid_style_policy_returns_error_not_exception(tmp_path):
    xml = _doc(_caption_para("Fig. 1", " ok"))
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_caption_style(path, style_policy={"citation_style": "bogus"})
    assert "error" in result


def test_missing_file_returns_error():
    result = docs_intel.audit_caption_style("/no/such/file.docx")
    assert "error" in result


def test_read_only_never_mutates(tmp_path):
    xml = _doc(_caption_para("Fig. 1.", " will be flagged"))
    path = _write_docx(tmp_path, xml)
    before = open(path, "rb").read()
    docs_intel.audit_caption_style(path, style_policy={"figure_caption_label_punctuation": "none"})
    after = open(path, "rb").read()
    assert before == after


def test_server_wrapper_matches_docs_intel(tmp_path):
    xml = _doc(_caption_para("Fig. 1", " ok"))
    path = _write_docx(tmp_path, xml)
    policy = {"figure_caption_label_punctuation": "none"}
    assert server.audit_caption_style(path, style_policy=policy) == docs_intel.audit_caption_style(
        path, style_policy=policy
    )


def test_get_document_review_wrapper_journal_param_matches_manual_preset_resolution(tmp_path):
    """9c1a3fd2 -- server.get_document_review's new `journal` param must
    resolve to the SAME style_policy get_journal_style_preset("jcshm")
    returns directly, and produce the same findings build_document_review
    would given that policy explicitly -- the wrapper must not re-derive or
    diverge from the two functions it composes."""
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1.", " Ends with a period.")))
    via_journal = server.get_document_review(path, journal="jcshm")
    policy = docs_intel.get_journal_style_preset("jcshm")
    via_explicit_policy = docs_intel.build_document_review(path, style_policy=policy)
    assert via_journal == via_explicit_policy


def test_get_document_review_wrapper_unknown_journal_returns_inline_error(tmp_path):
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1", " ok")))
    result = server.get_document_review(path, journal="not-a-real-journal")
    assert "error" in result
    assert "not-a-real-journal" in result["error"]


def test_get_document_review_wrapper_malformed_user_presets_file_returns_inline_error(tmp_path):
    """Same malformed-file scenario as
    test_get_journal_style_preset_malformed_user_presets_file_raises, but
    routed through server.get_document_review's journal=/user_presets_path=
    wrapper: server.py catches only ValueError from get_journal_style_preset
    to produce {"error": ...} (see the unknown-journal-name case above), so a
    malformed *file* must degrade the same way an unknown *name* does, not
    escape as an unhandled exception."""
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1", " ok")))
    result = server.get_document_review(path, journal="jcshm", user_presets_path=str(p))
    assert "error" in result


def test_get_document_review_wrapper_omitting_journal_is_unchanged(tmp_path):
    """No journal= -- identical to calling build_document_review directly
    with no style_policy (the pre-9c1a3fd2 wrapper behavior)."""
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1.", " Ends with a period.")))
    assert server.get_document_review(path) == docs_intel.build_document_review(path)


def test_get_document_review_wrapper_empty_journal_matches_omitted_journal(tmp_path):
    """Regression: journal="" must behave exactly like journal omitted (the
    pre-9c1a3fd2 no-style-policy shape), matching
    meridian/routes/notes.py's document_review_endpoint's own
    `journal_name = (journal or "").strip() or None` normalization. A first
    implementation here treated "" as an explicit-but-unknown preset name
    and returned {"error": "unknown journal style preset ''..."} instead --
    a real divergence between the two "resolve journal to style_policy"
    call sites caught by an adversarial review pass, not by this test
    originally (it did not exist yet)."""
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1.", " Ends with a period.")))
    empty = server.get_document_review(path, journal="")
    omitted = server.get_document_review(path)
    assert "error" not in empty
    assert empty == omitted


# ---------------------------------------------------------------------------
# jcshm preset end-to-end: matches the real submission this was built for
# ---------------------------------------------------------------------------

def test_jcshm_preset_flags_a_real_violation_and_passes_a_compliant_caption(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")

    violating = _write_docx(
        tmp_path, _doc(_caption_para("Fig. 1.", " Ends with a period.")), "violating.docx"
    )
    result = docs_intel.audit_caption_style(violating, style_policy=policy)
    types = {f["type"] for f in result["findings"]}
    assert "caption_label_punctuation_mismatch" in types
    assert "caption_terminal_punctuation_mismatch" in types

    compliant = _write_docx(
        tmp_path, _doc(_caption_para("Fig. 1", " No terminal or label punctuation")), "compliant.docx"
    )
    result2 = docs_intel.audit_caption_style(compliant, style_policy=policy)
    assert result2["finding_count"] == 0, result2["findings"]


# ---------------------------------------------------------------------------
# 9c1a3fd2 -- build_document_review wiring: audit_caption_style is now a
# SOURCE for the review panel's "caption" category, gated by style_policy
# exactly like audit_equation_style already was. Regression coverage for the
# gap this closed: style_policy reached audit_equation_style since
# b67ec6b5, but never audit_caption_style, so the live dashboard review
# panel (GET /projects/{id}/document-review) could never surface a verified
# journal's caption-formatting rules even after audit_caption_style existed.
# ---------------------------------------------------------------------------

def test_build_document_review_omits_caption_style_findings_without_style_policy(tmp_path):
    """No style_policy passed -- unchanged pre-9c1a3fd2 behavior: a caption
    that would violate the jcshm preset produces zero caption-style findings
    (audit_caption_style's own keys all default to None/"unspecified")."""
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1.", " Ends with a period.")))
    review = docs_intel.build_document_review(path)
    assert review["status"] == "ok"
    types = {f["type"] for f in review["findings"]}
    assert "caption_label_punctuation_mismatch" not in types
    assert "caption_terminal_punctuation_mismatch" not in types


def test_build_document_review_surfaces_caption_style_findings_with_journal_policy(tmp_path):
    """style_policy=jcshm-preset -- the SAME violation audit_caption_style
    catches directly now also appears in build_document_review's "caption"
    category, alongside (not instead of) the pre-existing legacy plaintext
    caption detector."""
    policy = docs_intel.get_journal_style_preset("jcshm")
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1.", " Ends with a period.")))
    review = docs_intel.build_document_review(path, style_policy=policy)
    assert review["status"] == "ok"
    caption_findings = [f for f in review["findings"] if f["category"] == "caption"]
    types = {f["type"] for f in caption_findings}
    assert "caption_label_punctuation_mismatch" in types
    assert "caption_terminal_punctuation_mismatch" in types
    assert review["findings_by_category"]["caption"] == len(caption_findings)
    # Every caption-style finding still gets a RESOLVED locator, same as every
    # other build_document_review finding -- para_id must actually reach
    # _resolve_anchor_query, not silently degrade to the not_applicable
    # fallback (which is also a real dict, so "locator is not None" alone
    # can't tell the two apart).
    for f in caption_findings:
        assert f["locator"]["status"] == "resolved"
        assert f["locator"]["target_para_id"] == "00000001"


def test_build_document_review_compliant_caption_produces_no_caption_style_findings(tmp_path):
    """A correctly-formatted caption against the jcshm preset stays clean
    inside build_document_review too -- not just in audit_caption_style
    directly (guards against the wiring itself introducing a false
    positive, e.g. by double-passing an already-resolved policy through
    resolve_style_policy a second time)."""
    policy = docs_intel.get_journal_style_preset("jcshm")
    path = _write_docx(tmp_path, _doc(_caption_para("Fig. 1", " No terminal or label punctuation")))
    review = docs_intel.build_document_review(path, style_policy=policy)
    caption_style_types = {
        f["type"] for f in review["findings"]
        if f["category"] == "caption" and f["type"].startswith("caption_label")
        or f["type"] == "caption_terminal_punctuation_mismatch"
    }
    assert not caption_style_types, review["findings"]


# ---------------------------------------------------------------------------
# User-settable presets: load / save / delete / list, and the
# amend-not-replace shadowing behavior
# ---------------------------------------------------------------------------

def test_load_user_presets_missing_file_is_empty_not_error(tmp_path):
    assert docs_intel.load_user_journal_style_presets(str(tmp_path / "nope.json")) == {}


def test_load_user_presets_malformed_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        docs_intel.load_user_journal_style_presets(str(p))


def test_get_journal_style_preset_malformed_user_presets_file_raises(tmp_path):
    """get_journal_style_preset calls load_user_journal_style_presets with no
    try/except (docs_intel.py), relying on this ValueError propagating
    unmodified up to callers like server.get_document_review -- only the
    loader itself was covered for the malformed-file case above, leaving the
    propagation through get_journal_style_preset unverified."""
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        docs_intel.get_journal_style_preset("jcshm", user_presets_path=str(p))


def test_load_user_presets_wrong_top_level_shape_raises(tmp_path):
    p = tmp_path / "bad2.json"
    p.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        docs_intel.load_user_journal_style_presets(str(p))


def test_load_user_presets_invalid_override_raises_naming_the_preset(tmp_path):
    p = tmp_path / "bad3.json"
    p.write_text(json.dumps({"my_venue": {"citation_style": "not_valid"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="my_venue"):
        docs_intel.load_user_journal_style_presets(str(p))


def test_save_validates_before_writing(tmp_path):
    p = tmp_path / "presets.json"
    result = docs_intel.save_user_journal_style_preset(
        "bad", {"citation_style": "not_valid"}, str(p)
    )
    assert "error" in result
    assert not p.exists()


def test_save_load_round_trip(tmp_path):
    p = tmp_path / "presets.json"
    r = docs_intel.save_user_journal_style_preset(
        "my_lab_style", {"caption_centered": True, "citation_style": "author_date"}, str(p)
    )
    assert r["status"] == "ok"
    assert r["preset_count"] == 1

    loaded = docs_intel.load_user_journal_style_presets(str(p))
    assert loaded == {"my_lab_style": {"caption_centered": True, "citation_style": "author_date"}}

    resolved = docs_intel.get_journal_style_preset("my_lab_style", user_presets_path=str(p))
    assert resolved["caption_centered"] is True
    assert resolved["citation_style"] == "author_date"
    # every other key still resolves to the schema default
    assert resolved["heading_terminal_punctuation"] is None


def test_save_preserves_other_existing_presets(tmp_path):
    p = tmp_path / "presets.json"
    docs_intel.save_user_journal_style_preset("preset_a", {"caption_centered": True}, str(p))
    docs_intel.save_user_journal_style_preset("preset_b", {"caption_centered": False}, str(p))
    loaded = docs_intel.load_user_journal_style_presets(str(p))
    assert set(loaded) == {"preset_a", "preset_b"}


def test_user_preset_shadowing_builtin_amends_not_replaces(tmp_path):
    """Regression: a first implementation resolved a same-named user preset
    by fully replacing the built-in's override dict, silently dropping
    every built-in key the user's override didn't also restate (e.g. the
    verified figure_caption_bold=True on the real "jcshm" preset)."""
    p = tmp_path / "presets.json"
    docs_intel.save_user_journal_style_preset(
        "jcshm",
        {
            "table_caption_bold": True,
            "table_caption_label_punctuation": "none",
        },
        str(p),
    )
    amended = docs_intel.get_journal_style_preset("jcshm", user_presets_path=str(p))
    # the user's new fact is present...
    assert amended["table_caption_bold"] is True
    assert amended["table_caption_label_punctuation"] == "none"
    # ...and every built-in jcshm fact NOT mentioned in the override survives
    assert amended["figure_caption_bold"] is True
    assert amended["figure_caption_label_punctuation"] == "none"
    assert amended["heading_terminal_punctuation"] == ""
    assert amended["citation_style"] == "numbered_bracket"

    # without user_presets_path, the built-in is untouched
    builtin = docs_intel.get_journal_style_preset("jcshm")
    assert builtin["table_caption_bold"] is None


def test_get_journal_style_preset_unknown_name_lists_user_presets_too(tmp_path):
    p = tmp_path / "presets.json"
    docs_intel.save_user_journal_style_preset("my_venue", {"caption_centered": True}, str(p))
    with pytest.raises(ValueError) as exc_info:
        docs_intel.get_journal_style_preset("not_a_real_one", user_presets_path=str(p))
    assert "my_venue" in str(exc_info.value)


def test_delete_removes_only_the_named_preset(tmp_path):
    p = tmp_path / "presets.json"
    docs_intel.save_user_journal_style_preset("keep_me", {"caption_centered": True}, str(p))
    docs_intel.save_user_journal_style_preset("remove_me", {"caption_centered": False}, str(p))

    result = docs_intel.delete_user_journal_style_preset("remove_me", str(p))
    assert result == {"status": "ok", "name": "remove_me", "path": str(p), "deleted": True, "preset_count": 1}

    remaining = docs_intel.load_user_journal_style_presets(str(p))
    assert set(remaining) == {"keep_me"}


def test_delete_nonexistent_preset_is_a_noop_not_an_error(tmp_path):
    p = tmp_path / "presets.json"
    docs_intel.save_user_journal_style_preset("keep_me", {"caption_centered": True}, str(p))
    result = docs_intel.delete_user_journal_style_preset("not_there", str(p))
    assert result["deleted"] is False
    assert result["preset_count"] == 1


def test_delete_missing_file_is_a_noop(tmp_path):
    result = docs_intel.delete_user_journal_style_preset("x", str(tmp_path / "nope.json"))
    assert result == {"status": "ok", "name": "x", "path": str(tmp_path / "nope.json"), "deleted": False, "preset_count": 0}


def test_list_presets_builtin_only():
    listed = docs_intel.list_journal_style_presets()
    assert listed["builtin_count"] == 29
    assert listed["user_count"] == 0
    names = {e["name"] for e in listed["presets"]}
    assert "jcshm" in names
    assert "default" in names
    assert all(e["source"] == "built_in" for e in listed["presets"])


def test_list_presets_with_user_presets_flags_shadowing(tmp_path):
    p = tmp_path / "presets.json"
    docs_intel.save_user_journal_style_preset("jcshm", {"table_caption_bold": True}, str(p))
    docs_intel.save_user_journal_style_preset("my_venue", {"caption_centered": True}, str(p))

    listed = docs_intel.list_journal_style_presets(str(p))
    assert listed["builtin_count"] == 29
    assert listed["user_count"] == 2

    by_name = {e["name"]: e for e in listed["presets"]}
    assert by_name["jcshm"]["source"] == "user"
    assert by_name["jcshm"]["shadows_builtin"] is True
    assert by_name["my_venue"]["source"] == "user"
    assert by_name["my_venue"]["shadows_builtin"] is False
    # every other built-in is still listed once, untouched
    assert by_name["default"]["source"] == "built_in"


def test_server_preset_crud_wrappers_match_docs_intel(tmp_path):
    p = str(tmp_path / "presets.json")
    r1 = server.save_user_journal_style_preset("x", {"caption_centered": True}, p)
    assert r1["status"] == "ok"

    assert server.list_journal_style_presets(p) == docs_intel.list_journal_style_presets(p)
    assert server.get_journal_style_preset("x", user_presets_path=p) == docs_intel.get_journal_style_preset(
        "x", user_presets_path=p
    )

    r2 = server.delete_user_journal_style_preset("x", p)
    assert r2["deleted"] is True
