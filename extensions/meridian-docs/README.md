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
| `audit_figure_table_spacing(path, style_policy)` | Read-only, profile-aware spacing audit for figures/tables and captions. |
| `preflight_document(path, style_policy)` | Package, equation, and selected figure/table layout preflight before render. |
| `audit_rights_clearance(docx_paths, rights_manifest, profile, zotero_records)` | Read-only, fail-closed clearance for every figure/table caption and embedded asset binding. |
| `build_rights_manifest_template(docx_paths, profile)` | Build a reviewable ledger from captions, embedded hashes, and reference-list links. |
| `evaluate_rights_manifest(rights_manifest, profile, zotero_records)` | Evaluate the rights ledger without opening a DOCX. |
| `inspect_rights_sources(urls, timeout_seconds)` | Collect DOI/publisher-page license signals for human review; never grants permission automatically. |

### Figure/table rights clearance

Structural ownership, citation identity, and legal reuse permission are three
different claims. `audit_rights_clearance` keeps them separate and will return
`submission_allowed: false` for any missing, ambiguous, conflicting, or
scope-mismatched record. A DOI, citation, Zotero item, “open access” label, or
nearby prose citation is discovery/identity evidence only; it is not permission.

The rights manifest is JSON so it can be versioned beside a manuscript and
reviewed without mutating the `.docx`:

```json
{
  "schema_version": "1.0",
  "artifacts": [
    {
      "artifact_id": "figure:2.3",
      "source_kind": "cc_license",
      "use_class": "adapted",
      "source_identity_status": "confirmed",
      "source_reference_id": "17",
      "source_urls": ["https://doi.org/10.xxxx/example"],
      "zotero_item_key": "ABC123",
      "asset_sha256": "sha256-of-the-embedded-submitted-asset",
      "license_name": "CC BY 4.0",
      "license_url": "https://creativecommons.org/licenses/by/4.0/",
      "permitted_uses": [
        "journal_print",
        "journal_online",
        "supplementary_information"
      ],
      "credit_line": "Adapted from ... [17], CC BY 4.0; changes made.",
      "release_action": "retain",
      "evidence": [
        {
          "type": "license",
          "url": "https://creativecommons.org/licenses/by/4.0/",
          "scopes": ["journal_print", "journal_online", "supplementary_information"]
        }
      ]
    }
  ]
}
```

For author-created material, use `source_kind: "original"` with an
`author_attestation` evidence record and explicit publication scopes. For
reproduced/adapted third-party material, use the exact license or permission
document, set `source_identity_status: "confirmed"` only after checking that
the embedded asset is actually the cited source, and preserve any
panel-specific credit-line restrictions. Use `source_identity_status: "mismatch"`
when the cited reference does not substantiate the asset; the
gate will block it even if a license field is present. The optional
`release_action` field records the intended resolution (`retain`,
`permission_request`, `redraw`, `remove`, `prose_only`, or `review`); actions
other than `retain` remain blocked while the current asset is still embedded.
Zotero
records may be passed as CSL-JSON through `zotero_records`; metadata is joined
to source references/DOIs for identity and link normalization, but never
counts as rights evidence. `inspect_rights_sources` can collect page hashes,
license links, DOI metadata, and rights-language signals; those signals must be
reviewed and entered into the manifest before the artifact can pass.

The built-in `springer_jcshm` profile requires `journal_print`,
`journal_online`, and `supplementary_information`. Pass another profile for a
different venue or release target. The engine is an operational release gate,
not a legal opinion; an allowed result means the recorded evidence satisfies
the configured policy, not that Meridian replaces the copyright holder or
publisher.

### Figure/table spacing profiles

Whitespace is not universal across venues, so the spacing audit is disabled by
default. Select a target profile explicitly, for example:

```json
{"figure_table_spacing_profile": "mst_thesis"}
```

Built-in profiles are `mst_thesis`, `mst_thesis_v11_legacy`, `drexel_thesis`,
`chicago_turabian`, `asce_manuscript`, `jcshm_springer`, `journal_generic`,
and `none`. The audit measures direct-body OOXML blank paragraphs and explicit
paragraph spacing in single-line equivalents. It does not claim to verify
rendered page-bottom whitespace, floating-object wrapping, automatic pagination,
or renderer-specific style/theme effects; those remain render/manual-review
checks.

For the MST profile, the same above/below spacing preference applies to both
figures and tables; the caption relationship remains venue-specific (figure
captions below figures, table titles above tables).

## Why

Meridian's paid value-add is coordination + persistent memory. The document
parser is genuinely useful on its own (e.g. mapping a thesis chapter's structure
before reading it), so it's distributed free here. The Meridian `word` tunnel
slot can default to `uvx meridian-docs`.

## License

MIT — see [LICENSE](./LICENSE).
