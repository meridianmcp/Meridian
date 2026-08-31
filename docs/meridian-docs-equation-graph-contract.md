# Meridian Docs equation graph contract

`build_equation_graph` is a read-only preflight primitive for research-grade
DOCX work. It inventories native OMML without rewriting the document and
returns a deterministic JSON graph with `graph_sha256`.

## What it records

- equation nodes with a stable location/fingerprint, visible number, paragraph
  anchor, heading-derived `section_path` plus stable `section_path_ids`, and
  native OMML structure hash;
- placement classification: `line_separated`, `inline`,
  `table_line_separated`, `table_embedded`, or `table_numbered`, plus separate
  `display_modes` and `containers` views so table layout does not hide whether
  an equation is display-style or inline;
- paragraph and manifest-declared symbol nodes;
- Word `SEQ`/`REF` field and bookmark nodes, with the raw extraction preserved
  under `reference_extraction`;
- `contains`, `contains_reference_signal`, `contains_bookmark`, `uses`,
  `defines_candidate`, `references`, `depends_on`, `refers_to_bookmark`, and
  unresolved-reference edges;
- duplicate equation numbers, duplicate native structures, placement conflicts,
  and unresolved references;
- a numbering summary with recognized visible numbers and advisory gaps. Gap
  detection is explicitly limited to recognized pure-integer numbers, because
  unnumbered formulas and section-scoped numbering can be intentional;
- an explicit DAG view over only equation-to-equation `depends_on` edges.

## Safety and interpretation

Native OMML remains authoritative. A structure hash is a deterministic identity
signal, not a proof that two expressions are mathematically equivalent. Reuse
of the same structure in different contexts is reported under `observations`
as an informational placement variation; it is not automatically treated as a
release-blocking conflict.
References are lexical evidence. Definition edges are named
`defines_candidate` and carry `confidence: heuristic`; they must be reviewed
before promotion to a scientific nomenclature record. A document-wide graph may
contain cycles, so callers should use the returned `dag` object instead of
assuming that all edges are topologically sortable.
Word fields and bookmarks are exact OOXML observations. A `REF` field is linked
to a bookmark only when exactly one matching bookmark is present in
`word/document.xml`; the graph does not infer that the bookmark encloses an
equation, and it does not inspect headers, footers, footnotes, or endnotes.

The optional `notation_manifest` uses the same project-owned manifest accepted
by `lint_nomenclature`. Omitting it is supported for equation/layout inventory,
but arbitrary tokens are never promoted to symbols by inference.

The operation is bounded by `max_nodes` (default 10,000; hard maximum 50,000)
and performs no writes to the DOCX, sidecar index, or hosted Meridian state.
