"""Contract-parity tests for tools/meridian_fallbacks (sprint item 50f224ec,
proposal 1abedabe-2f82-40e5-a320-3b32d550cc40).

This file does NOT re-test what tools/meridian_fallbacks/tests/test_*.py
already cover module-by-module (zip integrity, paraId enumeration, render
receipts, apply/rollback, ...) -- it proves the things that span module
boundaries and were never checked anywhere else:

  1. capability_manifest.json's per-module ``sha256``/``byte_size`` match the
     REAL bytes of the tracked .py files right now (catches "manifest says
     one thing, code says another" drift), and every .py file the package
     actually ships is represented in the manifest (catches a new module
     landing without a manifest update).
  2. ``related_sprint_items`` reflects reality: d3374b0e and 8419f55f are
     landed (their files now exist on disk), not still "planned".
  3. ``fallback_chain_example.capability`` is a GENUINELY schema-valid
     capability entry per the real ``meridian.capability_manifest.
     normalize_capability`` -- not merely illustrative JSON that happens to
     look plausible. (Before this sprint item, it used a ``capability_id``
     field the real schema rejects as unknown and had no ``id`` at all --
     ``normalize_capability`` would have raised on it. That gap is fixed as
     part of this item; this test locks the fix in.)
  4. Every ``fallback_chain`` entry resolves to a real, live, callable
     Python object -- "every declared fallback is executable" is checked by
     actually importing and resolving it, not by pattern-matching the string.
  5. Status-string / on-disk-ledger-format parity against the real
     extensions this package mirrors: extensions/meridian-outputs's
     provenance status vocabulary and ledger filenames, and
     extensions/meridian-docs's render-receipt status vocabulary. Verified
     by parsing the REAL extension source files independently (regex over
     their own text, not a hardcoded copy of this package's own constants)
     and comparing the two independently-extracted sets.
  6. A regression guard on extensions/meridian-docs and extensions/meridian-
     outputs's own pyproject.toml mcp pin (root-caused by note bf797200:
     an unbounded ``mcp>=1.0`` pin let CI resolve mcp 2.x and break
     collection; both files are already fixed to ``mcp>=1.27,<2`` in this
     tree -- this guards against that regressing silently).
  7. copy/paste-ready ``implementation_notes`` name real, resolvable
     functions/classes for every module, and point at acceptance-test files
     that actually exist on disk.
  8. End-to-end scenarios named in this item's acceptance criteria that no
     single-module test file exercises together: a stale worktree (source
     changed, artifact not regenerated) flagged consistently by BOTH gates,
     full functioning with meridian-docs/meridian-outputs entirely
     unimportable ("missing tools"), missing Word/COM, an unconverged
     outputs scan staying inconclusive rather than confidently absent, a
     stale generating script, rollback, table-cell paraIds, and JSON
     round-tripping of every "receipt" (PatchManifest, MergeResult,
     WriteResult, and the completion-gate/provenance-gate report dicts
     themselves).

Ground rules (per this item's own acceptance criteria): never touches the
canonical thesis DOCX (every fixture here is synthetic, built in-memory or
under ``tmp_path``); never treats Serena/editor memories as authoritative
(every claim below is checked against the real tracked source on disk, read
fresh in this file).
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import tools.meridian_fallbacks as mf
from tools.meridian_fallbacks import docx_completion_gate as gate
from tools.meridian_fallbacks import output_provenance_gate as OPG
from meridian import capability_manifest as cm

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = Path(mf.__file__).resolve().parent
_MANIFEST_PATH = _PKG_DIR / "capability_manifest.json"

_MERIDIAN_OUTPUTS_DIR = _REPO_ROOT / "extensions" / "meridian-outputs" / "meridian_outputs"
_MERIDIAN_DOCS_DIR = _REPO_ROOT / "extensions" / "meridian-docs" / "meridian_docs"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _sha256_and_size(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _extract_str_constant(source: str, name: str) -> "str | None":
    """Extract ``NAME = "value"`` from a module's raw source text. Used to
    independently cross-check status-string/filename constants declared in a
    REAL sibling extension's source, without importing that extension (which
    may pull in duckdb/xxhash/other heavy deps not installed in this pixi
    env -- see the module docstrings this package's own modules carry for
    why they avoid that same trap)."""
    m = re.search(rf'^{re.escape(name)}\s*=\s*"([^"]*)"', source, re.MULTILINE)
    return m.group(1) if m else None


def _resolve_fallback_chain_entry(entry: str) -> Any:
    """Resolve a capability_manifest.json ``fallback_chain`` string of the
    form ``"<slash/path/package>:<submodule>.<qualname>"`` to the real, live
    Python object it names. Raises (via a plain assertion-friendly
    exception) if the entry cannot be resolved -- this is deliberately
    strict: an unresolvable fallback is exactly the defect this function
    exists to catch.
    """
    assert ":" in entry, f"fallback_chain entry has no ':' separator: {entry!r}"
    pkg_path, _, rest = entry.partition(":")
    assert "." in rest, f"fallback_chain entry has no '.'-qualified name after ':': {entry!r}"
    dotted_pkg = pkg_path.replace("/", ".")
    submodule, _, qualname = rest.partition(".")
    module = importlib.import_module(f"{dotted_pkg}.{submodule}")
    obj: Any = module
    for attr in qualname.split("."):
        obj = getattr(obj, attr)
    return obj


# ---------------------------------------------------------------------------
# 1. Manifest <-> real file bytes parity.
# ---------------------------------------------------------------------------

class TestManifestModuleByteParity:
    def test_every_manifest_module_hash_and_size_match_real_file(self, manifest):
        mismatches = []
        for mod_name, entry in manifest["modules"].items():
            path = _PKG_DIR / entry["file"]
            assert path.is_file(), f"manifest module {mod_name!r} names a file that does not exist: {path}"
            actual_sha256, actual_size = _sha256_and_size(path)
            if actual_sha256 != entry["sha256"] or actual_size != entry["byte_size"]:
                mismatches.append(
                    f"{mod_name}: manifest sha256={entry['sha256']} size={entry['byte_size']} "
                    f"but actual sha256={actual_sha256} size={actual_size}"
                )
        assert not mismatches, "capability_manifest.json has drifted from the tracked source:\n" + "\n".join(mismatches)

    def test_every_manifest_module_has_nonempty_operations_and_limitations(self, manifest):
        for mod_name, entry in manifest["modules"].items():
            assert entry.get("supported_operations"), f"{mod_name}: supported_operations must be non-empty"
            assert entry.get("limitations"), f"{mod_name}: limitations must be non-empty"

    def test_output_provenance_gate_and_docx_completion_gate_now_landed_in_modules(self, manifest):
        """The two sibling items (d3374b0e, 8419f55f) landed their modules
        after this package's own manifest was first authored -- this is the
        core drift this whole test item exists to catch and fix."""
        assert "output_provenance_gate" in manifest["modules"]
        assert "docx_completion_gate" in manifest["modules"]

    def test_no_untracked_py_module_is_missing_from_manifest(self, manifest):
        """Every top-level .py file this package ships (excluding tests/ and
        __pycache__) must be represented in the manifest -- a new module
        landing without a manifest update is exactly the drift class this
        item's acceptance criteria calls out."""
        on_disk = {
            p.stem for p in _PKG_DIR.glob("*.py")
            if p.name != "__init__.py"
        }
        documented = {entry["file"][:-len(".py")] for entry in manifest["modules"].values()}
        assert on_disk <= documented, f"module(s) on disk but undocumented: {on_disk - documented}"
        # __init__.py is tracked under the "__init__" key with its own file field.
        assert manifest["modules"]["__init__"]["file"] == "__init__.py"


# ---------------------------------------------------------------------------
# 2. related_sprint_items reflects reality.
# ---------------------------------------------------------------------------

class TestRelatedSprintItemsStatus:
    @pytest.mark.parametrize("sprint_id,module_file", [
        ("d3374b0e", "output_provenance_gate.py"),
        ("8419f55f", "docx_completion_gate.py"),
    ])
    def test_landed_sibling_items_are_marked_landed_not_planned(self, manifest, sprint_id, module_file):
        entry = manifest["related_sprint_items"][sprint_id]
        assert (_PKG_DIR / module_file).is_file(), "precondition: the module must actually exist"
        assert entry["status"] == "landed", (
            f"{sprint_id} delivers {module_file!r}, which exists on disk, but the manifest still "
            f"says status={entry['status']!r}"
        )


# ---------------------------------------------------------------------------
# 3. fallback_chain_example.capability is a REAL, schema-valid capability.
# ---------------------------------------------------------------------------

class TestFallbackChainExampleSchemaParity:
    def test_capability_has_no_stray_capability_id_field(self, manifest):
        example = manifest["fallback_chain_example"]
        assert "capability" in example, "fallback_chain_example must nest a real 'capability' object"
        assert "capability_id" not in example["capability"], (
            "the real meridian.capability_manifest schema has no 'capability_id' field "
            "(it is 'id') -- this exact mismatch would have made the example unusable "
            "as a real set_capability_manifest() payload"
        )

    def test_capability_validates_against_real_normalize_capability(self, manifest):
        raw = manifest["fallback_chain_example"]["capability"]
        normalized = cm.normalize_capability(raw)  # raises CapabilityManifestError on any mismatch
        assert normalized["id"] == "docx_editing"
        assert normalized["availability_policy"] == "degraded_ok"
        assert normalized["fallback_chain"] == raw["fallback_chain"]
        assert set(normalized.keys()) == {
            "id", "purpose", "required_tools", "fallback_chain",
            "availability_policy", "verification_command", "provenance",
        }

    def test_capability_survives_a_real_set_capability_manifest_shaped_round_trip(self, manifest):
        """Simulates exactly what a caller doing
        ``set_capability_manifest(capabilities=[fallback_chain_example.capability])``
        would trigger server-side: normalize_manifest -> manifest_hash. Both
        must succeed and be stable, proving this is a genuinely usable
        example, not just JSON that happens to parse."""
        raw = manifest["fallback_chain_example"]["capability"]
        normalized_once = cm.normalize_manifest([raw])
        normalized_twice = cm.normalize_manifest([dict(raw)])
        assert normalized_once == normalized_twice
        assert cm.manifest_hash(normalized_once) == cm.manifest_hash(normalized_twice)
        assert cm.has_capability_manifest(normalized_once) is True

    def test_capability_availability_policy_is_a_valid_enum_member(self, manifest):
        policy = manifest["fallback_chain_example"]["capability"]["availability_policy"]
        assert policy in cm.VALID_AVAILABILITY_POLICIES


# ---------------------------------------------------------------------------
# 4. Every fallback_chain entry is executable.
# ---------------------------------------------------------------------------

class TestFallbackChainEntriesExecutable:
    def test_every_fallback_chain_entry_resolves_to_a_real_callable(self, manifest):
        chain = manifest["fallback_chain_example"]["capability"]["fallback_chain"]
        assert chain, "fallback_chain_example must declare at least one fallback"
        for entry in chain:
            obj = _resolve_fallback_chain_entry(entry)
            assert callable(obj), f"{entry!r} resolved to a non-callable: {obj!r}"

    def test_fallback_chain_covers_apply_insert_and_writer_primitives(self, manifest):
        """Not just 'resolvable' -- resolves to the SPECIFIC public API named
        in the string, proving the string wasn't just renamed/typo'd to some
        other real-but-wrong symbol."""
        chain = manifest["fallback_chain_example"]["capability"]["fallback_chain"]
        resolved = {entry: _resolve_fallback_chain_entry(entry) for entry in chain}
        names = {obj.__qualname__ if hasattr(obj, "__qualname__") else obj.__name__: obj for obj in resolved.values()}
        assert "apply_patch_manifest" in names
        assert "insert_image" in names
        assert "SafeOoxmlWriter" in names

    def test_fallback_chain_entry_actually_runs_end_to_end(self, docx_path):
        """The FIRST fallback in the example chain
        (transactional_merge.apply_patch_manifest) is not just importable --
        actually apply a trivial manifest through it against a real synthetic
        .docx, proving 'executable' end-to-end, not just import-resolvable."""
        from tools.meridian_fallbacks import PatchManifest, apply_patch_manifest

        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        new_bytes = b"<?xml version=\"1.0\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p/></w:body></w:document>"
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "trivial replace", payload=new_bytes,
        )
        result = apply_patch_manifest(manifest_obj, payloads={op.op_id: new_bytes})
        assert result.success, result.error
        assert manifest_obj.status == "applied"


# ---------------------------------------------------------------------------
# 5a. Status-string / ledger-format parity with extensions/meridian-outputs.
# ---------------------------------------------------------------------------

class TestMeridianOutputsParity:
    @pytest.fixture(scope="class")
    def annotate_source(self) -> str:
        path = _MERIDIAN_OUTPUTS_DIR / "annotate.py"
        if not path.is_file():
            pytest.skip(f"extensions/meridian-outputs not present in this checkout: {path}")
        return path.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def fingerprint_source(self) -> str:
        path = _MERIDIAN_OUTPUTS_DIR / "fingerprint.py"
        if not path.is_file():
            pytest.skip(f"extensions/meridian-outputs not present in this checkout: {path}")
        return path.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def provenance_status_source(self) -> str:
        path = _MERIDIAN_OUTPUTS_DIR / "provenance_status.py"
        if not path.is_file():
            pytest.skip(f"extensions/meridian-outputs not present in this checkout: {path}")
        return path.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def outputs_local_source(self) -> str:
        path = _MERIDIAN_OUTPUTS_DIR / "outputs_local.py"
        if not path.is_file():
            pytest.skip(f"extensions/meridian-outputs not present in this checkout: {path}")
        return path.read_text(encoding="utf-8")

    def test_cache_dirname_matches_real_extension(self, annotate_source):
        real = _extract_str_constant(annotate_source, "_CACHE_DIRNAME")
        assert real is not None, "could not find _CACHE_DIRNAME in the real annotate.py"
        assert OPG._CACHE_DIRNAME == real

    def test_provenance_ledger_filename_matches_real_extension(self, annotate_source):
        real = _extract_str_constant(annotate_source, "_LEDGER_FILENAME")
        assert real is not None
        assert OPG._PROVENANCE_LEDGER_FILENAME == real

    def test_fingerprint_ledger_filename_matches_real_extension(self, fingerprint_source):
        real = _extract_str_constant(fingerprint_source, "_LEDGER_FILENAME")
        assert real is not None
        assert OPG._FINGERPRINT_LEDGER_FILENAME == real

    def test_meridian_notes_filename_matches_real_extension(self, outputs_local_source):
        real = _extract_str_constant(outputs_local_source, "MERIDIAN_NOTES_FILENAME")
        assert real is not None
        assert OPG._MERIDIAN_NOTES_FILENAME == real

    @pytest.mark.parametrize("const_name,local_value", [
        ("EXACT", OPG.EXACT),
        ("DIRECTORY_FALLBACK", OPG.DIRECTORY_FALLBACK),
        ("UNREGISTERED", OPG.UNREGISTERED),
        ("UNKNOWN", OPG.UNKNOWN),
        ("STALE_BY_SCRIPT", OPG.STALE_BY_SCRIPT),
    ])
    def test_provenance_type_status_strings_match_real_extension(
        self, provenance_status_source, const_name, local_value,
    ):
        real = _extract_str_constant(provenance_status_source, const_name)
        assert real is not None, f"could not find {const_name} in the real provenance_status.py"
        assert local_value == real, (
            f"{const_name}: fallback module says {local_value!r}, real extension says {real!r} -- "
            "this breaks the documented cross-tool parity claim"
        )

    def test_status_vocabulary_is_documented_in_the_manifest(self, manifest):
        text = json.dumps(manifest["modules"]["output_provenance_gate"])
        for value in (OPG.EXACT, OPG.DIRECTORY_FALLBACK, OPG.UNREGISTERED, OPG.UNKNOWN, OPG.STALE_BY_SCRIPT):
            assert value in text, f"status {value!r} is not documented in the manifest's module entry"


# ---------------------------------------------------------------------------
# 5b. Render-status parity with extensions/meridian-docs.
# ---------------------------------------------------------------------------

class TestMeridianDocsRenderParity:
    @pytest.fixture(scope="class")
    def render_gate_source(self) -> str:
        path = _MERIDIAN_DOCS_DIR / "render_gate.py"
        if not path.is_file():
            pytest.skip(f"extensions/meridian-docs not present in this checkout: {path}")
        return path.read_text(encoding="utf-8")

    def test_render_gate_raw_statuses_match_status_map_keys(self, render_gate_source):
        real_rendered = _extract_str_constant(render_gate_source, "RENDERED")
        real_unavailable = _extract_str_constant(render_gate_source, "UNAVAILABLE_WITH_REASON")
        real_failed = _extract_str_constant(render_gate_source, "FAILED")
        assert {real_rendered, real_unavailable, real_failed} == {"rendered", "unavailable-with-reason", "failed"}

        gate_source = (_PKG_DIR / "docx_completion_gate.py").read_text(encoding="utf-8")
        block_match = re.search(r"status_map\s*=\s*\{(.*?)\}", gate_source, re.DOTALL)
        assert block_match, "could not find status_map literal in docx_completion_gate.py"
        mapped_keys = set(re.findall(r'"([^"]+)"\s*:', block_match.group(1)))
        assert mapped_keys == {real_rendered, real_unavailable, real_failed}, (
            f"docx_completion_gate.status_map keys {mapped_keys} no longer match the real "
            f"render_gate.py raw statuses {{real_rendered, real_unavailable, real_failed}}"
        )

    def test_render_status_vocabulary_is_documented_in_the_manifest(self, manifest):
        text = json.dumps(manifest["modules"]["docx_completion_gate"])
        for value in gate.RENDER_STATUSES:
            assert value in text, f"render status {value!r} is not documented in the manifest's module entry"


# ---------------------------------------------------------------------------
# 6. Extension mcp dependency pin regression guard (note bf797200).
# ---------------------------------------------------------------------------

class TestExtensionDependencyPinGuard:
    @pytest.mark.parametrize("pyproject_rel", [
        "extensions/meridian-docs/pyproject.toml",
        "extensions/meridian-outputs/pyproject.toml",
    ])
    def test_mcp_dependency_pin_has_an_upper_bound(self, pyproject_rel):
        path = _REPO_ROOT / pyproject_rel
        assert path.is_file(), f"missing {pyproject_rel}"
        text = path.read_text(encoding="utf-8")
        # Match the actual dependency specifier ("mcp>=1.27,<2"), not the bare
        # "mcp" keyword/tag string that also appears in these files (e.g. in a
        # `keywords = [...]` list) -- require a version-operator right after
        # the package name so an empty capture group can never match.
        m = re.search(r'"mcp(>=[^"]*)"', text)
        assert m, f"{pyproject_rel}: no quoted mcp>=... dependency spec found"
        spec = m.group(1)
        assert "<" in spec, (
            f"{pyproject_rel}: mcp dependency spec {spec!r} has no upper bound -- this is exactly "
            "the regression documented in note bf797200 (an unbounded mcp>=1.0 pin let CI resolve "
            "mcp 2.x, which removed mcp.server.fastmcp and broke collection)"
        )
        assert ">=1.27" in spec or ">= 1.27" in spec, (
            f"{pyproject_rel}: expected an mcp>=1.27 floor, got {spec!r}"
        )


# ---------------------------------------------------------------------------
# 7. implementation_notes names real, resolvable functions/tests.
# ---------------------------------------------------------------------------

class TestImplementationNotesFunctionBoundaries:
    @pytest.mark.parametrize("name,module", [
        ("PatchManifest", mf),
        ("apply_patch_manifest", mf),
        ("insert_image", mf),
        ("SafeOoxmlWriter", mf),
        ("check_output_provenance", OPG),
        ("run_completion_gate", gate),
        ("CompletionRequirements", gate),
    ])
    def test_named_function_boundary_exists_and_is_mentioned(self, manifest, name, module):
        notes = manifest["implementation_notes"]
        assert name in notes, f"implementation_notes claims to demonstrate {name!r} but never mentions it"
        assert hasattr(module, name), f"implementation_notes names {name!r}, which does not exist on {module!r}"
        assert callable(getattr(module, name))

    @pytest.mark.parametrize("test_file", [
        "tools/meridian_fallbacks/tests/test_output_provenance_gate.py",
        "tools/meridian_fallbacks/tests/test_docx_completion_gate.py",
        "tools/meridian_fallbacks/tests/test_capability_parity.py",
    ])
    def test_referenced_acceptance_test_files_exist(self, manifest, test_file):
        assert test_file in manifest["implementation_notes"]
        assert (_REPO_ROOT / test_file).is_file(), f"implementation_notes points at a nonexistent file: {test_file}"


# ---------------------------------------------------------------------------
# 8a. Stale worktree -- both gates agree an unregenerated artifact is stale.
# ---------------------------------------------------------------------------

class TestStaleWorktreeScenario:
    def test_stale_worktree_docx_blocked_by_completion_gate(self, tmp_path):
        worktree = tmp_path / "worktree_50f224ec"
        worktree.mkdir()
        source = worktree / "source.md"
        source.write_text("# Section 1\ncontent", encoding="utf-8")

        docx_path = worktree / "report.docx"
        import zipfile
        with zipfile.ZipFile(str(docx_path), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                b'<Default Extension="xml" ContentType="application/xml"/>'
                b'<Override PartName="/word/document.xml" '
                b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                b'</Types>',
            )
            zf.writestr(
                "_rels/.rels",
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                b'</Relationships>',
            )
            zf.writestr(
                "word/document.xml",
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b'<w:body><w:p><w:r><w:t>Stale build</w:t></w:r></w:p></w:body></w:document>',
            )

        # The docx predates the source it was supposedly built from -- the
        # classic "checked out a stale worktree, never rebuilt" scenario.
        import os
        old_time = time.time() - 7200
        os.utime(docx_path, (old_time, old_time))

        report = gate.run_completion_gate(
            str(docx_path),
            gate.CompletionRequirements(source_path=str(source)),
            skip_render_check=True,
        )
        assert report["ready"] is False
        assert any("stale_source_refused" in r for r in report["reasons"])
        assert report["stale_source_report"]["stale"] is True
        # The report itself is the "receipt" -- must be a clean JSON payload.
        json.dumps(report)

    def test_stale_worktree_output_flagged_by_provenance_gate(self, tmp_path):
        """The companion half of a stale worktree: a data output whose
        generating script changed after the output was tagged, but the
        output itself was never regenerated -- STALE_BY_SCRIPT, not a
        generic content mismatch."""
        outputs_dir = tmp_path / "worktree_50f224ec" / "outputs"
        outputs_dir.mkdir(parents=True)
        cache_dir = outputs_dir / ".meridian-outputs-cache"
        cache_dir.mkdir()

        script = tmp_path / "worktree_50f224ec" / "build.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        original_script_hash = OPG._sha256_file(str(script))

        output = outputs_dir / "table.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")
        output_hash = OPG._sha256_file(str(output))

        (cache_dir / "provenance_ledger.json").write_text(json.dumps({
            OPG._normalize_path(str(output)): {
                "path": str(output), "generating_script": str(script),
                "params": {}, "content_hash": output_hash,
            },
        }), encoding="utf-8")
        (cache_dir / "fingerprint_ledger.json").write_text(json.dumps({
            str(output): {
                "path": str(output), "script_path": str(script), "script_hash": original_script_hash,
            },
        }), encoding="utf-8")

        # Worktree rebased/rebuilt -- the script changed, the output did not.
        script.write_text("print('v2 -- bug fix')\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(outputs_dir), str(output))
        assert status["provenance_type"] == OPG.STALE_BY_SCRIPT
        json.dumps(status)


# ---------------------------------------------------------------------------
# 8b. Missing tools -- the fallback package has no hard import on the thing
#     it is a fallback FOR, and keeps functioning when that thing is absent.
# ---------------------------------------------------------------------------

class TestMissingToolsDegradation:
    _FORBIDDEN_TOP_LEVEL_IMPORTS = {"meridian_outputs", "meridian_docs", "duckdb", "xxhash"}

    @pytest.mark.parametrize("filename", [
        "output_provenance_gate.py",
        "docx_completion_gate.py",
    ])
    def test_module_has_no_hard_top_level_import_on_the_extension_it_falls_back_for(self, filename):
        source = (_PKG_DIR / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        overlap = found & self._FORBIDDEN_TOP_LEVEL_IMPORTS
        assert not overlap, f"{filename} has a hard top-level import on {overlap} -- defeats its purpose as a fallback"

    def test_provenance_gate_produces_full_report_regardless_of_extension_availability(self, tmp_path):
        f = tmp_path / "orphan.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["provenance_type"] == OPG.UNREGISTERED
        # Every documented top-level field is present -- a caller relying on
        # this fallback gets the SAME shape it would from a live extension.
        for key in (
            "path", "provenance_type", "record", "directory_note", "staleness",
            "script_staleness", "output_sha256", "generator_script", "archival",
            "convergence", "inconclusive",
        ):
            assert key in status

    def test_completion_gate_degrades_cleanly_when_meridian_docs_import_fails(self, tmp_path, monkeypatch):
        """Force meridian_docs to be unimportable regardless of what is
        actually installed in this environment (sys.modules poisoning is the
        standard, safe way to simulate ImportError without touching the real
        environment) and confirm the gate still returns a complete report."""
        monkeypatch.setitem(sys.modules, "meridian_docs", None)
        monkeypatch.setitem(sys.modules, "meridian_docs.render_gate", None)

        import zipfile
        docx_path = tmp_path / "doc.docx"
        with zipfile.ZipFile(str(docx_path), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                b'<Default Extension="xml" ContentType="application/xml"/>'
                b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                b'</Types>',
            )
            zf.writestr(
                "_rels/.rels",
                b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
            )
            zf.writestr(
                "word/document.xml",
                b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b'<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>',
            )

        report = gate.run_completion_gate(str(docx_path))
        assert report["render_report"]["status"] == gate.RENDER_UNAVAILABLE
        # Not requiring a verified render -- unavailability alone must not block readiness.
        assert report["ready"] is True, report["reasons"]
        json.dumps(report)


# ---------------------------------------------------------------------------
# 8c. Missing Word/COM -- explicit, never silently folded into success.
# ---------------------------------------------------------------------------

class TestMissingWordCom:
    def test_require_render_verified_blocks_when_com_unavailable(self, docx_path):
        report = gate.run_completion_gate(
            str(docx_path),
            gate.CompletionRequirements(require_render_verified=True),
            render_checker=lambda p: {"status": gate.RENDER_UNAVAILABLE, "reason": "no Word/pywin32 on this host"},
        )
        assert report["ready"] is False
        assert report["render_report"]["status"] == gate.RENDER_UNAVAILABLE
        assert any("require_render_verified" in r for r in report["reasons"])

    def test_render_unavailable_is_environment_fact_not_document_fact(self, docx_path):
        """RENDER_UNAVAILABLE must never be conflated with RENDER_FAILED --
        one says 'this environment can't check', the other says 'the check
        ran and the document failed it'."""
        report = gate.run_completion_gate(
            str(docx_path),
            render_checker=lambda p: {"status": gate.RENDER_UNAVAILABLE, "reason": "non-Windows"},
        )
        assert report["render_report"]["status"] != gate.RENDER_FAILED
        assert report["render_report"]["status"] == gate.RENDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# 8d. Unconverged outputs scan stays inconclusive, never confidently absent.
# ---------------------------------------------------------------------------

class TestUnconvergedOutputsScan:
    def test_truncated_scan_is_inconclusive_and_documented(self, tmp_path, manifest):
        (tmp_path / "aaa.csv").write_text("1\n", encoding="utf-8")
        (tmp_path / "bbb.csv").write_text("2\n", encoding="utf-8")
        target = tmp_path / "zzz_target.csv"
        target.write_text("3\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(target), max_scan_files=2)
        assert status["convergence"]["converged"] is False
        assert status["provenance_type"] == OPG.UNKNOWN
        assert status["inconclusive"] is True
        # The manifest itself must not claim UNKNOWN is a confident answer.
        limitations_text = json.dumps(manifest["modules"]["output_provenance_gate"]["limitations"])
        assert "inconclusive" in limitations_text


# ---------------------------------------------------------------------------
# 8e. Rollback + receipt (PatchManifest / MergeResult / WriteResult) JSON
#     round-tripping -- everything a caller would persist or hand to another
#     tool must survive json.dumps/json.loads with no data loss.
# ---------------------------------------------------------------------------

class TestRollbackAndReceiptSerialization:
    def test_rollback_restores_original_content_after_a_real_apply(self, docx_path, minimal_docx_parts):
        from tools.meridian_fallbacks import PatchManifest, apply_patch_manifest, rollback, SafeOoxmlWriter

        original_bytes = Path(docx_path).read_bytes()
        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        new_doc_xml = minimal_docx_parts["word/document.xml"].replace(b"Introduction", b"Replaced")
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "swap intro paragraph", payload=new_doc_xml,
        )
        result = apply_patch_manifest(manifest_obj, payloads={op.op_id: new_doc_xml})
        assert result.success
        assert Path(docx_path).read_bytes() != original_bytes

        write_result = rollback(manifest_obj, result)
        assert Path(docx_path).read_bytes() == original_bytes
        # WriteResult ("receipt") is a clean JSON payload.
        json.dumps(write_result.to_dict())

    def test_patch_manifest_round_trips_through_json_with_no_loss(self, docx_path, tmp_path):
        from tools.meridian_fallbacks import PatchManifest

        manifest_obj = PatchManifest.create_from_file(str(docx_path), notes="receipt round-trip check")
        manifest_obj.add_operation(
            "replace_part", "word/document.xml", "test op",
            payload=b"payload-bytes", metadata={"k": "v"},
        )
        manifest_path = tmp_path / "manifest.json"
        manifest_obj.save(str(manifest_path))

        reloaded = PatchManifest.load(str(manifest_path))
        assert reloaded.to_dict() == manifest_obj.to_dict()
        # Round-trip through plain json too, not just the class's own save/load.
        assert PatchManifest.from_json(json.dumps(manifest_obj.to_dict())).to_dict() == manifest_obj.to_dict()

    def test_merge_result_receipt_is_fully_json_serializable(self, docx_path):
        from tools.meridian_fallbacks import PatchManifest, apply_patch_manifest

        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "test op", payload=b"<doc/>",
        )
        result = apply_patch_manifest(manifest_obj, payloads={op.op_id: b"<doc/>"}, dry_run=True)
        payload = result.to_dict()
        text = json.dumps(payload)
        assert json.loads(text) == payload

    def test_provenance_gate_report_is_fully_json_serializable(self, tmp_path):
        f = tmp_path / "receipt.csv"
        f.write_text("x\n", encoding="utf-8")
        status = OPG.check_output_provenance(str(tmp_path), str(f))
        text = json.dumps(status)
        assert json.loads(text) == status


# ---------------------------------------------------------------------------
# 8f. Table-cell paraIds -- the known gap class the manifest now documents.
# ---------------------------------------------------------------------------

class TestTableCellParaIds:
    def test_duplicate_para_id_across_body_and_table_cell_blocks_completion(self, tmp_path):
        import zipfile
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
            '<w:body>'
            '<w:p w14:paraId="DUP0001"><w:r><w:t>body para</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p w14:paraId="DUP0001"><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:sectPr/></w:body></w:document>'
        ).encode("utf-8")
        docx_path = tmp_path / "table.docx"
        with zipfile.ZipFile(str(docx_path), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                b'<Default Extension="xml" ContentType="application/xml"/>'
                b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                b'</Types>',
            )
            zf.writestr(
                "_rels/.rels",
                b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
            )
            zf.writestr("word/document.xml", document_xml)

        report = gate.run_completion_gate(str(docx_path), skip_render_check=True)
        assert report["ready"] is False
        assert any("duplicate_para_ids_found" in r for r in report["reasons"])
        assert report["para_id_report"]["body_paragraph_count"] == 1
        assert report["para_id_report"]["table_cell_paragraph_count"] == 1
        assert report["para_id_report"]["duplicate_para_ids"] == ["DUP0001"]

    def test_table_cell_paraids_are_documented_in_the_manifest(self, manifest):
        text = json.dumps(manifest["modules"]["docx_completion_gate"])
        assert "table-cell" in text or "table_cell" in text
