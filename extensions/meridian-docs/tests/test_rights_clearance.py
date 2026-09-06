"""Focused tests for fail-closed figure/table rights clearance."""
from __future__ import annotations

import io
import zipfile

from meridian_docs.rights_clearance import (
    SPRINGER_JCSHM_PROFILE,
    audit_docx_rights,
    build_rights_manifest_template,
    evaluate_artifact,
    evaluate_manifest,
)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _write_docx(tmp_path, body: str, media: bytes = b"figure-bytes") -> str:
    path = tmp_path / "candidate.docx"
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:r="{R}" xmlns:a="{A}">
  <w:body>{body}<w:sectPr/></w:body>
</w:document>'''
    rels = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{R}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", rels)
        archive.writestr("word/media/image1.png", media)
    path.write_bytes(buf.getvalue())
    return str(path)


def _base_artifact(**overrides):
    artifact = {
        "artifact_id": "figure:1",
        "source_kind": "cc_license",
        "use_class": "reproduced",
        "source_identity_status": "confirmed",
        "source_reference_id": "1",
        "source_url": "https://doi.org/10.1234/example",
        "asset_sha256": "asset-hash",
        "license_name": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "permitted_uses": [
            "journal_print",
            "journal_online",
            "supplementary_information",
        ],
        "credit_line": "Reproduced from Example et al. [1], CC BY 4.0.",
        "evidence": [{"type": "license", "scopes": [
            "journal_print", "journal_online", "supplementary_information"
        ]}],
    }
    artifact.update(overrides)
    return artifact


def test_cc_by_reproduced_artifact_is_allowed_with_credit_and_scope():
    result = evaluate_artifact(_base_artifact())

    assert result["decision"] == "clear_with_attribution"
    assert result["allowed"] is True


def test_zotero_identity_does_not_clear_missing_rights():
    artifact = _base_artifact(
        source_kind="third_party",
        license_name="",
        license_url="",
        evidence=[],
        zotero_record={"key": "ABC", "title": "Example"},
    )

    result = evaluate_artifact(artifact)

    assert result["allowed"] is False
    assert result["decision"] == "permission_required"


def test_cc_by_nd_blocks_adaptation():
    result = evaluate_artifact(_base_artifact(license_name="CC BY-ND 4.0", use_class="adapted"))

    assert result["decision"] == "blocked"
    assert result["allowed"] is False


def test_non_original_asset_without_source_identity_confirmation_blocks():
    result = evaluate_artifact(_base_artifact(source_identity_status="not_checked"))

    assert result["decision"] == "unresolved"
    assert result["allowed"] is False
    assert "source identity" in result["reasons"][0]


def test_source_identity_mismatch_blocks_even_with_license():
    result = evaluate_artifact(_base_artifact(source_identity_status="mismatch"))

    assert result["decision"] == "blocked"
    assert result["allowed"] is False
    assert "does not match" in result["reasons"][0]


def test_release_action_redraw_blocks_current_embedded_asset():
    result = evaluate_artifact(_base_artifact(release_action="redraw"))

    assert result["decision"] == "blocked"
    assert result["allowed"] is False
    assert "requires replacing" in result["reasons"][0]


def test_original_requires_author_confirmation_and_scopes():
    result = evaluate_artifact(_base_artifact(
        source_kind="original",
        source_reference_id="",
        source_url="",
        author_confirmed=True,
        permitted_uses=["journal_online"],
        evidence=[],
    ))

    assert result["decision"] == "inconclusive"
    assert result["allowed"] is False


def test_docx_caption_uses_own_citation_and_binds_media(tmp_path):
    body = f'''
<w:p w14:paraId="IMG000001"><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
<w:p w14:paraId="CAP000001"><w:r><w:t>Figure 1. Test panel [1].</w:t></w:r></w:p>
<w:p><w:r><w:t>[1] Example et al. Source title. https://doi.org/10.1234/example</w:t></w:r></w:p>
'''
    path = _write_docx(tmp_path, body)
    manifest = {"schema_version": "1.0", "artifacts": [_base_artifact(asset_sha256="wrong-hash") ]}

    result = audit_docx_rights(path, manifest)

    assert result["submission_allowed"] is False
    row = result["artifacts"][0]
    assert row["source_reference_ids"] == ["1"]
    assert row["asset_binding_status"] == "bound"
    assert row["rights"]["decision"] == "inconclusive"


def test_docx_missing_manifest_record_blocks_caption(tmp_path):
    body = '<w:p><w:r><w:t>Figure 1. Unmapped panel.</w:t></w:r></w:p>'
    path = _write_docx(tmp_path, body)

    result = audit_docx_rights(path, {"schema_version": "1.0", "artifacts": []})

    assert result["submission_allowed"] is False
    assert result["artifacts"][0]["rights"]["decision"] == "unresolved"


def test_template_captures_caption_local_citation_reference_and_asset_hash(tmp_path):
    body = f'''
<w:p w14:paraId="IMG000001"><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
<w:p w14:paraId="CAP000001"><w:r><w:t>Fig. 1. Test panel [1].</w:t></w:r></w:p>
<w:p><w:r><w:t>1. Example et al. Source title. https://doi.org/10.1234/example</w:t></w:r></w:p>
'''
    path = _write_docx(tmp_path, body)

    result = build_rights_manifest_template(path)

    assert result["status"] == "template_only"
    assert result["submission_allowed"] is False
    row = result["artifacts"][0]
    assert row["artifact_id"].endswith(":figure:1")
    assert row["caption_para_id"] == "CAP000001"
    assert row["source_reference_candidates"] == ["1"]
    assert row["source_urls"] == ["https://doi.org/10.1234/example"]
    assert row["asset_sha256"]
    assert row["source_identity_status"] == "not_checked"
    assert row["release_action"] == "review"


def test_manifest_status_is_blocked_if_any_artifact_is_unresolved():
    manifest = {"schema_version": "1.0", "artifacts": [_base_artifact(), _base_artifact(artifact_id="table:1", source_kind="unknown")]}

    result = evaluate_manifest(manifest, profile=SPRINGER_JCSHM_PROFILE)

    assert result["status"] == "blocked"
    assert result["submission_allowed"] is False
    assert result["blocking_count"] == 1
