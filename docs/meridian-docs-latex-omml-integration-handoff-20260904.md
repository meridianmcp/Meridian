# Meridian Docs LaTeX/Word bridge — implementation handoff

**Status:** design and implementation handoff; no production merge, release,
DOCX mutation, Overleaf connection, or thesis promotion is implied.

**Receiving worktree**

- Branch: `codex/meridian-docs-equation-graph-20260831b`
- Worktree: `C:\Users\13144\Documents\Meridian\worktrees\meridian-docs-equation-graph-20260831`
- Starting revision: `cec0b66f`
- Existing branch handoff: `docs/meridian-docs-equation-graph-branch-handoff-20260831.md`
- Existing Outputs integration handoff: `docs/meridian-docs-outputs-integration-handoff-20260831.md`

The purpose of this note is to let a permanent LaTeX workstream continue the
existing Meridian Docs equation/provenance work instead of rebuilding a
second parser and second release model from scratch.

## Canonical starting points

| Responsibility | Existing implementation | Role |
|---|---|---|
| LaTeX structure, includes, headings, citations, BibTeX | `packages/docparse/docparse/latex_intel.py` | Permanent standalone parser package. Start with `parse_latex_structure()` and `analyze_latex()`. |
| Compatibility import | `meridian/latex_intel.py` | Thin compatibility shim; do not add new implementation here. |
| Bounded LaTeX/OMML interchange | `extensions/meridian-docs/meridian_docs/latex_bridge.py` | Existing loss-aware bridge: LaTeX/OMML ↔ `math_ir`; extend it rather than introducing another converter. |
| Neutral math representation | `extensions/meridian-docs/meridian_docs/math_ir.py` | Candidate identity layer for cross-format comparison. |
| Equation identity and placement | `extensions/meridian-docs/meridian_docs/equation_graph.py` and `equation_references.py` | Existing graph/reference model for equations, inline/display placement, and unresolved references. |
| Typography and notation rules | `math_typography.py`, `equation_typography_audit.py`, `notation_rules.py`, `notation_manifest.py` | Existing rule/audit vocabulary for upright/italic/bold symbols, subscripts, and venue-specific notation. |
| Workspace/release lineage | `document_workspace.py`, `equation_review_provenance.py`, `render_policy.py` | Existing lineage, receipts, and render-policy boundary. |

The current research manuscript anchors are external working artifacts, not
repository source files:

- Manuscript: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\staging\jcshm_v58_synchronized_cleanup_with_si_sections_20260903\JCSHM_Manuscript_v58_current_cleaned_literal_candidate.docx`
- SI candidate after the DA3 visual removal: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\staging\jcshm_v58_synchronized_cleanup_without_DA3_S5_20260904\JCSHM_Supplementary_Information_v58_current_synchronized_without_DA3_S5_candidate.docx`

## What already works

- LaTeX source can be read as a file or raw source, with `\\input`/`\\include`
  expansion, heading extraction, citation extraction, and basic bibliography
  inspection.
- The bridge has bounded, loss-aware conversions:
  `latex_to_ir()`, `ir_to_latex()`, `latex_to_omml()`, `omml_to_ir()`,
  `omml_to_latex()`, and `convert_equation()`.
- Unsupported constructs are reported as warnings/opaque nodes rather than
  silently flattened. This fail-closed behavior must remain.
- Native OMML remains authoritative for existing Word documents. LaTeX is an
  interchange/source representation, not a reason to replace a manually
  corrected OMML equation with a guessed rendering.
- Equation graph, notation manifests, document fingerprints, and Outputs
  provenance are already the intended integration points.

## Problems the permanent LaTeX workstream must solve

1. **The current parser is intentionally partial.** It is sufficient for
   structure and a stable math subset, not a full TeX engine. Unknown macros,
   package-defined operators, alignment environments, labels, and complex
   `cases`/matrix constructs need explicit diagnostics and source spans.

2. **Typography is currently lossy.** The bridge can parse style commands, but
   OMML emission does not yet carry the complete style semantics into `m:rPr`,
   and OMML inspection does not yet treat italic/bold/upright run properties
   as part of the IR. This is the root class of failure behind earlier
   `DT`, `DT+Depth`, `argmin`, `S`, `\\Omega_D`, and subscript-style regressions.

3. **Math identity is not string identity.** A Word OMML payload, a LaTeX
   expression, and a rendered equation need one stable semantic identity while
   preserving meaningful distinctions such as `R_{depth}` versus
   `R_{Depth}`, function application versus a subscript, and punctuation owned
   by the surrounding prose versus punctuation embedded in the math.

4. **The current bridge is not a release pipeline.** It does not yet prove the
   exact TeX snapshot, included-file set, `.bib` inputs, compiler/toolchain,
   log, warnings, PDF hash, and page count used for a manuscript release.
   “The browser/Overleaf preview looked right” must never count as a compile
   receipt.

5. **Manual edits must survive.** A source-to-target conversion must not
   overwrite a manually corrected equation, caption, table, or prose block.
   The system needs artifact IDs, source hashes, supersession links, and an
   explicit conflict state before any staged merge.

6. **DOCX and LaTeX have different layout authorities.** Word layout depends
   on OOXML styles, section geometry, native OMML, field behavior, and Word's
   pagination. LaTeX layout depends on class/package options, engine, and
   compilation. The bridge should compare semantic/typographic manifests; it
   should not promise pixel identity across formats.

## Required design

### 1. One neutral equation artifact

Extend `math_ir` into a versioned `EquationArtifact` envelope containing at
least:

- stable equation ID and document/workspace ID;
- source format and exact source hash;
- normalized semantic tree;
- placement (`inline`, `display`, `line_separated`, or table-associated);
- surrounding punctuation ownership;
- typography roles for every token/run: italic, upright, bold, operator,
  function, unit, abbreviation, vector/matrix/tensor, and subscript role;
- source span/paragraph anchor where available;
- conversion warnings, unsupported constructs, and loss flags;
- supersedes/superseded-by links for manual corrections.

The semantic tree must represent fractions, scripts, delimiters, matrices,
cases, accents, n-ary operators with limits, named operators, text conditions,
and explicit punctuation. Do not normalize case or identifier spelling merely
to make two equations compare equal.

### 2. Explicit serialization rules

- LaTeX output must use explicit commands where typography matters:
  `\\mathrm{}`, `\\mathbf{}`, `\\operatorname{}`, and grouped scripts rather
  than relying on ambient math-mode defaults.
- OMML output must emit the corresponding `m:rPr` semantics and validate the
  resulting `m:oMath` structure before it can be staged.
- `argmin`, `argmax`, `min`, `max`, `log`, and similar functions must remain
  single named operators; a large operator such as `\\sum` must retain a
  limit placement model rather than being reconstructed from plain text.
- Matrices/cases must retain row/column structure and braces/delimiters.
- Punctuation must have one owner. A conversion must not append a second
  period/comma/semicolon outside an OMML payload when it already belongs to the
  source equation, and it must not silently delete punctuation that belongs to
  surrounding prose.

### 3. Cross-format comparison before mutation

Add a read-only comparison operation:

`compare_equation_artifacts(word_omml, latex_source, profile)` → semantic,
typography, placement, punctuation, and loss findings.

The comparison should block automatic insertion on any semantic mismatch,
unknown macro, unsupported structure, or unresolved style role. A typography
only mismatch may be repairable, but it still needs a deterministic finding and
an explicit repair receipt. Automatic repair must operate on the IR/OMML
builder, never on regex substitutions over serialized XML or TeX.

### 4. Venue profile and compile receipt

Add a JCSHM profile alongside the existing notation rules. It should cover
document-level and equation-level requirements, but keep rights clearance and
visual pagination as separate gates. A LaTeX receipt must bind:

- root `.tex` hash;
- recursively resolved `\\input`/`\\include` files and hashes;
- bibliography files and hashes;
- class/package/toolchain/engine versions;
- exact compile command or reproducible command description;
- compiler log, warnings, and errors;
- produced PDF hash, page count, and output path;
- profile/version and the equation/reference manifests used.

If the compiler is unavailable, the receipt must say `unavailable` or
`degraded`; it must never be recorded as a passing compile.

### 5. Staged merge model

Use the existing workspace/provenance model for a two-way or three-way merge:

1. ingest Word OMML and/or LaTeX into immutable source artifacts;
2. build equation/reference/notation manifests;
3. generate candidate output in an isolated staging path;
4. compare candidate against the source and manual-edit manifest;
5. emit a review packet containing proposed additions, deletions, and repairs;
6. require explicit acceptance of conflicts before promotion;
7. write a release manifest tying source hashes, code hash, profile, receipts,
   and output hashes together.

No conversion step should write directly to the canonical thesis or a live
Overleaf project.

## Implementation order

1. Add source spans and resolved-input file graph to
   `docparse.latex_intel`; retain its current non-raising behavior.
2. Version the `math_ir`/`EquationArtifact` schema and add fixtures from the
   existing thesis equation bank, including scripts, operators, matrices,
   cases, punctuation, and inline/display placement.
3. Extend `latex_bridge` to preserve and validate typography and structure in
   both directions; add round-trip tests before adding mutation.
4. Connect the artifact IDs and hashes to `equation_graph`, notation audits,
   document workspace lineage, and Outputs provenance.
5. Add the JCSHM LaTeX profile and compile receipt; test missing compiler and
   stale-input behavior as failures/degraded states.
6. Implement a read-only Word↔LaTeX comparison/review packet.
7. Only after the above is stable, implement an isolated candidate generator
   and a separately reviewed DOCX/LaTeX promotion transaction.
8. Treat Overleaf Git/API integration as an adapter over the same local source
   snapshot and receipt model. Never place an Overleaf token in this handoff,
   source tree, or provenance artifact.

## Minimum acceptance tests

- LaTeX→IR→OMML→IR and OMML→IR→LaTeX preserve semantic structure for the
  equation fixtures; any loss is explicit and blocks promotion.
- Typography fixtures preserve upright `DT`, `DT+Depth`, named functions,
  bold symbols, Greek symbols, and script roles without case/italic drift.
- N-ary limits, matrices, braces, `\\in`, `\\cdot`, and nested parentheses do
  not collapse to blank squares, exponents, or flat text.
- Punctuation appears exactly once and is attributed to the correct owner.
- `\\input`/`\\include` and `.bib` changes alter the receipt/hash and make an
  old receipt stale.
- A manually corrected equation remains unchanged unless its artifact is
  explicitly accepted as superseded.
- Read-only analysis leaves the source DOCX/TeX bytes unchanged.
- Generated DOCX packages load without Word repair/unreadable-content errors;
  native OMML validation passes.
- No rasterization is used in the inner equation conversion/lint loop. Visual
  rendering remains a final QA capability, not semantic proof.
- Existing focused equation/notation/provenance tests pass in the receiving
  worktree before merge or release.

## Guardrails for the receiving executor

- This branch/worktree is dirty and contains uncommitted equation/notation
  work. Inspect and preserve it; do not claim it is production-integrated.
- Do not merge into shared `dev`, `main`, an Outputs release, or a thesis
  canonical path without a separate scope/review decision.
- Do not reimplement `latex_intel` in `meridian/latex_intel.py`; it is a shim.
- Do not create a second math IR, second equation-numbering scheme, or
  second provenance ledger.
- Do not infer publication rights from format conversion or SI placement;
  rights clearance remains a separate evidence-backed gate.
