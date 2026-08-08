// Unit tests for the DOCX review panel's pure helpers (b67ec6b5).
//
// Covers the sprint item's required scenarios: empty findings, stale
// snapshots, duplicate text (ambiguous locator), long paragraphs (preview
// truncation), missing IDs (not_found locator), and mixed native/legacy
// captions (caption category + type label).
import { describe, expect, it } from "vitest";
import {
  groupFindingsByCategory,
  summarizeLocator,
  truncatePreview,
  isReviewEmpty,
  isReviewStale,
  isReviewError,
  REVIEW_CATEGORY_ORDER,
  type ReviewFinding,
  type ReviewLocator,
  type DocumentReviewResult,
} from "./dashboard-documents";

const resolvedLocator = (over: Partial<ReviewLocator> = {}): ReviewLocator => ({
  status: "resolved",
  section_path: "3.2",
  heading_para_id: "h1",
  target_para_id: "p42",
  document_order: 42,
  element_type: "paragraph",
  quoted_text: "The quick brown fox jumps over the lazy dog.",
  leading_text_preview: "The quick brown fox jumps over the lazy dog.",
  first_words: "The quick brown fox jumps over the lazy dog.",
  word_search_locator: "The quick brown fox jumps over the lazy dog.",
  bookmark_exists: false,
  ref_status: { checked: true, reference_count: 0 },
  candidates: [],
  ...over,
});

const finding = (over: Partial<ReviewFinding> = {}): ReviewFinding => ({
  category: "equation",
  severity: "warning",
  type: "misaligned_equation",
  detail: {},
  locator: resolvedLocator(),
  ...over,
});

// ---------------------------------------------------------------------------
// truncatePreview — long paragraphs.
// ---------------------------------------------------------------------------
describe("truncatePreview", () => {
  it("passes short text through unchanged", () => {
    const r = truncatePreview("short paragraph");
    expect(r).toEqual({ text: "short paragraph", truncated: false, fullText: "short paragraph" });
  });

  it("truncates a long paragraph and flags it, never silently", () => {
    const long = "word ".repeat(60).trim(); // well over the 140-char default
    const r = truncatePreview(long);
    expect(r.truncated).toBe(true);
    expect(r.text.length).toBeLessThan(long.length);
    expect(r.text.endsWith("…")).toBe(true);
    // The full, un-truncated text is still available for a "show more" affordance.
    expect(r.fullText).toBe(long);
  });

  it("null/undefined text never throws", () => {
    expect(truncatePreview(null).text).toBe("");
    expect(truncatePreview(undefined).text).toBe("");
  });
});

// ---------------------------------------------------------------------------
// summarizeLocator — resolved / ambiguous (duplicate text) / not_found
// (missing IDs) / stale / not_applicable.
// ---------------------------------------------------------------------------
describe("summarizeLocator — resolved", () => {
  it("pairs target_para_id context with section_path + quoted text (never a bare id)", () => {
    const s = summarizeLocator(resolvedLocator());
    expect(s.kind).toBe("resolved");
    expect(s.sectionPath).toBe("3.2");
    expect(s.preview).toContain("quick brown fox");
  });

  it("falls back to leading_text_preview/first_words when quoted_text is absent", () => {
    const s = summarizeLocator(resolvedLocator({ quoted_text: null, leading_text_preview: "fallback preview text" }));
    expect(s.preview).toBe("fallback preview text");
  });

  it("missing section_path renders a document-root placeholder, not a blank", () => {
    const s = summarizeLocator(resolvedLocator({ section_path: null }));
    expect(s.sectionPath).toBe("(document root)");
  });

  it("truncates a long resolved paragraph and surfaces the flag", () => {
    const long = "sentence ".repeat(40).trim();
    const s = summarizeLocator(resolvedLocator({ quoted_text: long, leading_text_preview: long, first_words: long }));
    expect(s.truncated).toBe(true);
    expect(s.fullText).toBe(long);
  });
});

describe("summarizeLocator — ambiguous (duplicate text)", () => {
  it("shows candidate rows instead of guessing a unique target", () => {
    const s = summarizeLocator({
      status: "ambiguous",
      reason: "2 elements matched this query; narrow it",
      candidates: [
        { target_para_id: "p10", section_path: "1", leading_text_preview: "Duplicate caption text" },
        { target_para_id: "p88", section_path: "4.1", leading_text_preview: "Duplicate caption text" },
      ],
    });
    expect(s.kind).toBe("ambiguous");
    expect(s.candidateCount).toBe(2);
    expect(s.candidates.map(c => c.label)).toEqual(["p10", "p88"]);
    expect(s.reason).toContain("matched");
    // No single preview/sectionPath is asserted as authoritative for an
    // ambiguous match — the panel must render candidates, not a pick.
    expect(s.preview).toBe("");
  });

  it("handles an empty candidates array without crashing", () => {
    const s = summarizeLocator({ status: "ambiguous", reason: "ambiguous with no listed candidates" });
    expect(s.kind).toBe("ambiguous");
    expect(s.candidateCount).toBe(0);
    expect(s.candidates).toEqual([]);
  });
});

describe("summarizeLocator — not_found / missing ids", () => {
  it("a locator with no resolvable para_id reports not_found with a reason, never a fabricated location", () => {
    const s = summarizeLocator({ status: "not_found", reason: "para_id 'p999' not found" });
    expect(s.kind).toBe("not_found");
    expect(s.sectionPath).toBe("");
    expect(s.reason).toContain("not found");
  });

  it("a null/undefined locator degrades to not_found with a safe default reason", () => {
    expect(summarizeLocator(null).kind).toBe("not_found");
    expect(summarizeLocator(undefined).reason).toBeTruthy();
  });

  it("an unrecognized status also degrades to not_found rather than throwing", () => {
    const s = summarizeLocator({ status: "some_future_status" } as ReviewLocator);
    expect(s.kind).toBe("not_found");
  });
});

describe("summarizeLocator — stale / not_applicable", () => {
  it("stale carries the mismatch reason and no location", () => {
    const s = summarizeLocator({ status: "stale", reason: "source_fingerprint_mismatch" });
    expect(s.kind).toBe("stale");
    expect(s.reason).toBe("source_fingerprint_mismatch");
  });

  it("not_applicable (a finding with no para_id, e.g. a render finding) never invents a location", () => {
    const s = summarizeLocator({ status: "not_applicable" });
    expect(s.kind).toBe("not_applicable");
    expect(s.reason).toContain("no single paragraph-level location");
  });
});

// ---------------------------------------------------------------------------
// groupFindingsByCategory — mixed native/legacy captions + stable category set.
// ---------------------------------------------------------------------------
describe("groupFindingsByCategory", () => {
  it("every declared category appears even with zero findings (framework-agnostic display)", () => {
    const groups = groupFindingsByCategory([], REVIEW_CATEGORY_ORDER);
    expect(groups.map(g => g.category)).toEqual(REVIEW_CATEGORY_ORDER);
    expect(groups.every(g => g.findings.length === 0)).toBe(true);
  });

  it("groups mixed native/legacy caption findings under one 'caption' category", () => {
    const findings: ReviewFinding[] = [
      finding({ category: "caption", type: "legacy_plaintext_caption", severity: "warning", detail: { kind: "Figure" } }),
      finding({ category: "caption", type: "legacy_plaintext_caption", severity: "warning", detail: { kind: "Table" } }),
      finding({ category: "equation", type: "misaligned_equation", severity: "warning" }),
    ];
    const groups = groupFindingsByCategory(findings, REVIEW_CATEGORY_ORDER);
    const captionGroup = groups.find(g => g.category === "caption")!;
    expect(captionGroup.findings).toHaveLength(2);
    expect(captionGroup.findings.every(f => f.type === "legacy_plaintext_caption")).toBe(true);
    const equationGroup = groups.find(g => g.category === "equation")!;
    expect(equationGroup.findings).toHaveLength(1);
  });

  it("sorts findings within a category worst-severity-first", () => {
    const findings: ReviewFinding[] = [
      finding({ category: "equation", type: "missing_trailing_punctuation", severity: "warning" }),
      finding({ category: "equation", type: "duplicate_equation_number", severity: "error" }),
    ];
    const groups = groupFindingsByCategory(findings, REVIEW_CATEGORY_ORDER);
    const eq = groups.find(g => g.category === "equation")!;
    expect(eq.findings[0].severity).toBe("error");
    expect(eq.findings[1].severity).toBe("warning");
  });

  it("an unknown category not in the declared list still renders (not silently dropped)", () => {
    const groups = groupFindingsByCategory(
      [finding({ category: "future_category" })],
      REVIEW_CATEGORY_ORDER,
    );
    const extra = groups.find(g => g.category === "future_category");
    expect(extra).toBeTruthy();
    expect(extra!.findings).toHaveLength(1);
  });

  it("falls back to the default category order when categories is null/empty", () => {
    const groups = groupFindingsByCategory([finding()], null);
    expect(groups.map(g => g.category)).toEqual(REVIEW_CATEGORY_ORDER);
  });
});

// ---------------------------------------------------------------------------
// isReviewEmpty / isReviewStale / isReviewError — top-level review states.
// ---------------------------------------------------------------------------
describe("review-level state helpers", () => {
  it("isReviewEmpty: true only for a clean ok review with zero findings", () => {
    const clean: DocumentReviewResult = { status: "ok", findings: [], finding_count: 0 };
    expect(isReviewEmpty(clean)).toBe(true);
  });

  it("isReviewEmpty: false when findings exist", () => {
    const withFindings: DocumentReviewResult = { status: "ok", findings: [finding()], finding_count: 1 };
    expect(isReviewEmpty(withFindings)).toBe(false);
  });

  it("isReviewEmpty: false for a stale or errored review — those are distinct states", () => {
    expect(isReviewEmpty({ status: "stale", findings: [] })).toBe(false);
    expect(isReviewEmpty({ error: "could not read file" })).toBe(false);
    expect(isReviewEmpty(null)).toBe(false);
  });

  it("isReviewStale: true only when status === 'stale'", () => {
    expect(isReviewStale({ status: "stale", reason: "source_fingerprint_mismatch" })).toBe(true);
    expect(isReviewStale({ status: "ok" })).toBe(false);
    expect(isReviewStale(null)).toBe(false);
  });

  it("isReviewError: true only when an error field is present", () => {
    expect(isReviewError({ error: "file not found on server: x.docx" })).toBe(true);
    expect(isReviewError({ status: "ok" })).toBe(false);
    expect(isReviewError(undefined)).toBe(false);
  });
});
