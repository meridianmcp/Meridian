# meridian-docs

Standalone, **stdlib-only** OOXML (Word `.docx`) intelligence, packaged as an MCP
server. It's the parser layer extracted from [Meridian](https://usemeridian.us) —
free, MIT-licensed, no third-party dependencies for parsing (`python-docx` is
*not* required).

> **Status: skeleton.** The parser (`meridian_docs/docs_intel.py`) is vendored
> verbatim from the Meridian monorepo (`meridian/docs_intel.py`). A true
> extraction (making Meridian import this package instead of its in-tree copy) is
> deferred; until then keep the two copies in sync.

## Install

```bash
uvx meridian-docs          # run without installing
# or
uv tool install meridian-docs
```

## Tools

| Tool | What it does |
|------|--------------|
| `document_outline(path)` | Heading outline of a `.docx` (counts + ordered headings). Stateless. |
| `parse_document(path)` | Ordered paragraph records (`index`, `para_id`, `style`, `text`). |
| `index_document(path, index_db_path)` | Build a sidecar SQLite index for navigation. |
| `get_structure(index_db_path)` | Heading outline from a built index. |
| `get_paragraph(index_db_path, para_id)` | One paragraph by id. |
| `search_paragraphs(index_db_path, query, limit)` | Substring search over paragraphs. |
| `extract_equations(path)` / `index_equations(path, index_db_path)` | Extract native OMML equations and optionally persist a local sidecar inventory. |
| `audit_equation_integrity(path)` | Read-only raw-OOXML audit for equation structure, numbering, display/inline context, and duplicate/missing OMML signals. |
| `compare_equation_structures(reference_path, candidate_path)` | Compare equation semantic manifests between a reference and candidate document. |
| `lint_nomenclature(path, notation_manifest)` | Read-only, deterministic cross-check of a project-owned notation manifest against prose and native OMML text. |
| `validate_notation_manifest(notation_manifest)` | Read-only semantic registry validation for stable symbol IDs, roles, kinds, scopes, index classes, typography, and explicit glyph reuse. |
| `audit_equation_notation(path, notation_manifest)` | Read-only binding of native OMML equation occurrences to semantic roles, scopes, typography declarations, and exact equation locators. |
| `census_equation_scripts(path)` | Read-only complete census of native OMML subscripts, superscripts, combined scripts, counts, and equation locators before semantic renaming. |
| `convert_equation(source, source_format, target_format)` | Loss-aware LaTeX/OMML interchange through a bounded Meridian math IR (`ir` is an output format); unsupported constructs are reported instead of silently flattened. |
| `build_equation_graph(document_path, notation_manifest)` | Read-only deterministic equation inventory: line-separated/inline/table placement, stable section IDs, symbol links, lexical dependencies, Word `SEQ`/`REF` fields and bookmarks, duplicate/conflict reports, and explicit DAG validation. |

### Notation and equation contract status

`lint_nomenclature` requires an explicit manifest; it does not infer scientific
definitions or silently rename symbols. A minimal manifest looks like:

```json
{
  "version": "1",
  "case_sensitive": true,
  "symbols": [
    {"symbol": "R_ray", "role": "maximum ray-search radius", "required": true,
     "flattened_aliases": ["Rray"]},
    {"symbol": "R_depth", "role": "depth-recess signal", "required": true}
  ]
}
```

For higher-level notation governance, `validate_notation_manifest` accepts the
same basic symbol entries plus semantic fields. The expression grammar remains
in `math_ir`; this registry records the project meaning and scope of each
symbol:

```json
{
  "version": "2",
  "symbols": [
    {
      "id": "cost.distance_transform",
      "symbol": "C_DT",
      "role": "distance-transform cost",
      "kind": "quantity",
      "scope": ["section:3.2"],
      "indices": ["named_signal"],
      "typography": {"base": "italic", "subscript": "upright"}
    }
  ]
}
```

Overlapping reuse of one glyph for different roles or kinds is blocking;
disjoint-scope reuse is a review warning; explicit `allow_reuse` is recorded
and auditable. The validator never renames equations or edits a DOCX.

`audit_equation_notation` then joins that registry to each observed native OMML
equation. It reports the visible equation number, paragraph anchor, nearest
heading, scope match, canonical/alias/flattened-alias evidence, declared
typography, observed OMML style metadata, and script-aware terms such as
`R_i` versus a bare `R`. Native `sSub`, `sSup`, and `sSubSup` structures are
kept distinct from flattened text. This is a proposal audit: it does not infer
a scientific meaning from a glyph and does not mutate the file. Results now
include a schema version, stable source/manifest fingerprints, structured
versus textual binding counts, review status, and an explicit mutation=false
provenance flag.

`census_equation_scripts` answers the preceding forensic question independently:
what scripted OMML terms are actually present? It returns every unique native
scripted term, occurrence counts, visible-number/ordinal/paragraph locators,
subscript and superscript value counts, a source fingerprint, and
`document_mutated: false`. It deliberately runs without a notation manifest so
an incomplete role registry cannot hide unusual or malformed script forms.

The linter returns a stable manifest hash and typed findings such as
`missing_symbol`, `declared_symbol_unused`, `alias_used`,
`flattened_subscript`, `case_mismatch`, and `symbol_role_collision`. It is a
read-only local primitive; the larger `equation_contract` operation, selected
staging repairs, render-proof, and Meridian Outputs receipt binding remain
planned follow-up work. Native OMML is retained as the source representation;
diagnostic linearizations must not be treated as validated LaTeX.

## Why

Meridian's paid value-add is coordination + persistent memory. The document
parser is genuinely useful on its own (e.g. mapping a thesis chapter's structure
before reading it), so it's distributed free here. The Meridian `word` tunnel
slot can default to `uvx meridian-docs`.

## License

MIT — see [LICENSE](./LICENSE).
