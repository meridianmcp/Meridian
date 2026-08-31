# Meridian Docs equation/notation work — branch handoff

**Disposition:** preserved outside shared `dev`; do not merge or release
without a separate review decision.

- Branch: `codex/meridian-docs-equation-graph-20260831b`
- Worktree: `C:\Users\13144\Documents\Meridian\worktrees\meridian-docs-equation-graph-20260831`
- Base revision: `563b9d68fd4e07dd752d8f588ae671619c2ace34`
- Source: the 29 active Docs files formerly left uncommitted in the shared
  `C:\Users\13144\Documents\Meridian\repository` worktree.

## Included work

This branch contains the equation graph, equation-reference extraction,
notation/nomenclature manifests and audits, bounded LaTeX/OMML interchange,
math IR, document-workspace lineage, render-policy integration, script census,
README/API registration, the equation-graph contract, and their focused tests.
Native OMML remains authoritative; the new operations are read-only unless a
future, separately reviewed mutation workflow explicitly promotes them.

## Verification

The focused serial suite passed before relocation: **84 passed**. Re-run it in
this worktree before any merge or production action:

```text
pixi run python -m pytest extensions/meridian-docs/tests/test_document_workspace.py extensions/meridian-docs/tests/test_equation_graph.py extensions/meridian-docs/tests/test_equation_graph_stress.py extensions/meridian-docs/tests/test_equation_references.py extensions/meridian-docs/tests/test_equation_review_provenance.py extensions/meridian-docs/tests/test_math_interchange.py extensions/meridian-docs/tests/test_nomenclature_contract.py extensions/meridian-docs/tests/test_notation_audit.py extensions/meridian-docs/tests/test_notation_manifest.py extensions/meridian-docs/tests/test_render_policy.py extensions/meridian-docs/tests/test_render_policy_integration.py extensions/meridian-docs/tests/test_script_census.py -p no:xdist -q --maxfail=1
```

## Guardrails for the receiving planner/executor

- Do not stage or commit files from the shared `dev` root for this work.
- Do not merge this branch into `dev`, `main`, an Anthropic release candidate,
  or an Outputs release without explicit scope review.
- Do not touch OneDrive or canonical thesis files; experiment data and renders
  remain outside the repository on the configured local data drive.
- No version increase, push, deploy, or paper/Ooxml work is implied.
- Review integration with `meridian-outputs`, provenance receipts, and the
  actual stress-test DOCX before calling this production-ready.

The shared `dev` root should contain none of these branch files after the
relocation cleanup; this branch/worktree is the authoritative working copy for
the Docs wave.
