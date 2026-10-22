import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SECTION_TYPE_ORDER,
  STYLE_GUIDE,
  ENABLE_PUBLISHED_FRAMING_ENV_VAR,
  normalizeSectionType,
  getStyleGuide,
  resolveSectionText,
  checkSectionStyle,
  lookupPublishedFraming,
  type SectionTypeId,
  type StyleGuideEntry,
} from "./style-guide.js";

// --- normalizeSectionType / STYLE_GUIDE data shape --------------------------

test("STYLE_GUIDE: has exactly the 8 documented categories, each with a non-empty ordered move sequence", () => {
  assert.equal(SECTION_TYPE_ORDER.length, 8);
  assert.deepEqual(Object.keys(STYLE_GUIDE.guides).sort(), [...SECTION_TYPE_ORDER].sort());
  for (const key of SECTION_TYPE_ORDER) {
    const guide = STYLE_GUIDE.guides[key];
    assert.equal(typeof guide.label, "string");
    assert.ok(guide.label.length > 0);
    assert.ok(Array.isArray(guide.moves));
    assert.ok(guide.moves.length >= 3, `${key} should have a real move sequence, not a stub`);
    const ids = new Set<string>();
    for (const move of guide.moves) {
      assert.equal(typeof move.id, "string");
      assert.ok(!ids.has(move.id), `${key}: duplicate move id "${move.id}"`);
      ids.add(move.id);
      assert.equal(typeof move.name, "string");
      assert.equal(typeof move.description, "string");
      assert.ok(Array.isArray(move.cues) && move.cues.length > 0, `${key}.${move.id} needs at least one cue`);
      for (const cue of move.cues) assert.equal(typeof cue, "string");
    }
  }
});

test("normalizeSectionType: accepts common aliases case-insensitively", () => {
  assert.equal(normalizeSectionType("Abstract"), "abstract");
  assert.equal(normalizeSectionType("intro"), "introduction");
  assert.equal(normalizeSectionType("Related Work"), "related-work");
  assert.equal(normalizeSectionType("prior work"), "related-work");
  assert.equal(normalizeSectionType("Methodology"), "methods");
  assert.equal(normalizeSectionType("Discussion/Conclusion"), "discussion-conclusion");
  assert.equal(normalizeSectionType("conclusion"), "discussion-conclusion");
  assert.equal(normalizeSectionType("Broader Impact"), "ethics-broader-impact");
  assert.equal(normalizeSectionType("  Results  "), "results");
});

test("normalizeSectionType: an unrecognized string, or a non-string, returns null rather than throwing", () => {
  assert.equal(normalizeSectionType("not a real section type"), null);
  assert.equal(normalizeSectionType(""), null);
  assert.equal(normalizeSectionType(undefined), null);
  assert.equal(normalizeSectionType(42), null);
});

// --- getStyleGuide -----------------------------------------------------------

test("getStyleGuide: no argument returns the full structure", () => {
  const result = getStyleGuide() as { sectionTypes: SectionTypeId[]; guides: Record<SectionTypeId, StyleGuideEntry> };
  assert.deepEqual(result.sectionTypes, SECTION_TYPE_ORDER);
  assert.equal(Object.keys(result.guides).length, 8);
});

test("getStyleGuide: a valid section-type (or alias) filters to just that guide", () => {
  const result = getStyleGuide("Ethics") as { sectionType: SectionTypeId; guide: StyleGuideEntry };
  assert.equal(result.sectionType, "ethics-broader-impact");
  assert.equal(result.guide.label, "Ethics/Broader Impact");
  assert.ok(result.guide.moves.some((m) => m.id === "risks"));
});

test("getStyleGuide: an unrecognized section-type is a structured {error}, never a throw", () => {
  const result = getStyleGuide("not-a-real-type") as { error: string };
  assert.equal(typeof result.error, "string");
  assert.match(result.error, /unknown section-type/);
});

// --- resolveSectionText -------------------------------------------------------

test("resolveSectionText: 'text' passes through unchanged", () => {
  const result = resolveSectionText({ text: "Some raw section text." });
  assert.deepEqual(result, { ok: true, text: "Some raw section text.", resolvedFrom: "text" });
});

test("resolveSectionText: resolves a middle section's range from sourceText + headingTitle, stopping at the next heading", () => {
  const source =
    "\\section{Intro}\n" +
    "Intro line one.\n" +
    "Intro line two.\n" +
    "\\section{Methods}\n" +
    "Methods line one.\n" +
    "\\section{Results}\n" +
    "Results line one.\n";
  const result = resolveSectionText({ sourceText: source, headingTitle: "Methods" });
  assert.equal(result.ok, true);
  assert.ok(result.ok && result.resolvedFrom === "sourceText");
  assert.equal(result.ok && result.text, "\\section{Methods}\nMethods line one.");
  assert.equal(result.ok && result.resolvedFrom === "sourceText" && result.heading.title, "Methods");
});

test("resolveSectionText: the LAST heading's range runs to the end of the document", () => {
  const source = "\\section{Intro}\nIntro text.\n\\section{Conclusion}\nFinal thoughts here.\nMore final thoughts.\n";
  const result = resolveSectionText({ sourceText: source, headingTitle: "Conclusion" });
  assert.equal(result.ok, true);
  assert.equal(result.ok && result.text, "\\section{Conclusion}\nFinal thoughts here.\nMore final thoughts.");
});

test("resolveSectionText: resolves by headingId instead of headingTitle", () => {
  const source = "\\section{Intro}\nIntro text.\n\\section{Methods}\nMethods text.\n";
  // Get a real heading id the way a caller would -- from a first resolve by
  // title (or, in practice, from outline_tex's own node list) -- then prove
  // headingId alone resolves to the identical range.
  const byTitle = resolveSectionText({ sourceText: source, headingTitle: "Methods" });
  assert.equal(byTitle.ok, true);
  if (!byTitle.ok || byTitle.resolvedFrom !== "sourceText") throw new Error("expected a resolved sourceText result");
  const byId = resolveSectionText({ sourceText: source, headingId: byTitle.heading.id });
  assert.equal(byId.ok, true);
  assert.equal(byId.ok && byId.text, byTitle.text);
  assert.equal(byId.ok && byId.resolvedFrom === "sourceText" && byId.heading.id, byTitle.heading.id);
});

test("resolveSectionText: neither text nor sourceText+heading is a structured {ok:false,reason}, never a throw", () => {
  assert.equal(resolveSectionText({}).ok, false);
  const missingHeading = resolveSectionText({ sourceText: "\\section{Intro}\ntext\n" });
  assert.equal(missingHeading.ok, false);
  assert.match(!missingHeading.ok ? missingHeading.reason : "", /requires 'headingId' or 'headingTitle'/);
});

test("resolveSectionText: an unmatched heading title is a structured error, never a throw", () => {
  const result = resolveSectionText({ sourceText: "\\section{Intro}\ntext\n", headingTitle: "Nonexistent" });
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.reason : "", /no heading found matching/);
});

test("resolveSectionText: malformed sourceText degrades to a structured error rather than throwing", () => {
  // parse() itself is quite tolerant (see lint.test.js's own comments on
  // this), so this exercises the "no content array" / heading-not-found
  // branches rather than assuming a raw parser throw -- either way the
  // contract under test is the same: never let a bad input escape as an
  // uncaught exception.
  const result = resolveSectionText({ sourceText: "not valid latex at all {{{", headingTitle: "Intro" });
  assert.equal(result.ok, false);
});

// --- checkSectionStyle ---------------------------------------------------------

test("checkSectionStyle: a well-formed abstract with every move present, in order, reports nothing missing or out-of-order", () => {
  const text =
    "This problem has become increasingly important in recent years. " +
    "However, existing approaches fail to scale to real workloads. " +
    "In this paper, we propose a new lightweight method. " +
    "We show that our results outperform all baselines. " +
    "Our findings suggest this approach generalizes broadly.";
  const result = checkSectionStyle({ sectionType: "abstract", text }) as {
    sectionType: SectionTypeId;
    heuristic: true;
    disclaimer: string;
    movesMissing: string[];
    movesOutOfOrder: string[];
    movesPresent: string[];
  };
  assert.equal(result.sectionType, "abstract");
  assert.equal(result.heuristic, true);
  assert.match(result.disclaimer, /structural heuristic/i);
  assert.match(result.disclaimer, /not.*ground-truth/i);
  assert.deepEqual(result.movesMissing, []);
  assert.deepEqual(result.movesOutOfOrder, []);
  assert.equal(result.movesPresent.length, STYLE_GUIDE.guides.abstract.moves.length);
});

test("checkSectionStyle: moves with no cue match are reported present:false and listed in movesMissing", () => {
  const text = "In this paper, we propose a new method.";
  const result = checkSectionStyle({ sectionType: "abstract", text }) as {
    movesMissing: string[];
    details: { id: string; present: boolean; matchOffset: number | null }[];
  };
  assert.ok(result.movesMissing.includes("results"));
  assert.ok(result.movesMissing.includes("implications"));
  const resultsDetail = result.details.find((d) => d.id === "results")!;
  assert.equal(resultsDetail.present, false);
  assert.equal(resultsDetail.matchOffset, null);
});

test("checkSectionStyle: a move appearing before an earlier canonical move is flagged out-of-order", () => {
  // "results" (canonical move #4) appears in the text BEFORE "context"
  // (canonical move #1).
  const text = "We show that our method works. This problem has become increasingly important.";
  const result = checkSectionStyle({ sectionType: "abstract", text }) as {
    movesPresent: string[];
    movesOutOfOrder: string[];
  };
  assert.ok(result.movesPresent.includes("context"));
  assert.ok(result.movesPresent.includes("results"));
  assert.ok(result.movesOutOfOrder.includes("results"));
  assert.ok(!result.movesOutOfOrder.includes("context"));
});

test("checkSectionStyle: moves already in canonical order are never flagged, even with only some moves present", () => {
  const text =
    "This problem has become increasingly important. " + // context (move 0)
    "In this paper, we propose a solution. " + // approach (move 2)
    "Our findings suggest wide applicability."; // implications (move 4)
  const result = checkSectionStyle({ sectionType: "abstract", text }) as {
    movesOutOfOrder: string[];
    movesMissing: string[];
  };
  assert.deepEqual(result.movesOutOfOrder, []);
  assert.ok(result.movesMissing.includes("gap"));
  assert.ok(result.movesMissing.includes("results"));
});

test("checkSectionStyle: works across a non-abstract category (limitations) with its own move set", () => {
  const text =
    "Our study is limited to English-language papers. " +
    "One limitation is our modest sample size. " +
    "To mitigate this, we cross-validated across three independent corpora. " +
    "As a result, readers should treat the absolute numbers as approximate.";
  const result = checkSectionStyle({ sectionType: "limitations", text }) as {
    sectionType: SectionTypeId;
    movesMissing: string[];
    movesOutOfOrder: string[];
  };
  assert.equal(result.sectionType, "limitations");
  assert.deepEqual(result.movesMissing, []);
  assert.deepEqual(result.movesOutOfOrder, []);
});

test("checkSectionStyle: resolves text from sourceText + headingId end-to-end (not just headingTitle)", () => {
  const source = "\\section{Ethics}\nThis work could benefit clinicians. Potential risks include misuse for surveillance.\n";
  const outline = resolveSectionText({ sourceText: source, headingTitle: "Ethics" });
  assert.equal(outline.ok, true);
  if (!outline.ok || outline.resolvedFrom !== "sourceText") throw new Error("expected a resolved sourceText result");
  const heading = outline.heading;
  const result = checkSectionStyle({
    sectionType: "ethics-broader-impact",
    sourceText: source,
    headingId: heading.id,
  }) as { resolvedFrom: string; movesPresent: string[] };
  assert.equal(result.resolvedFrom, "sourceText");
  assert.ok(result.movesPresent.includes("benefits"));
  assert.ok(result.movesPresent.includes("risks"));
});

test("checkSectionStyle: an unrecognized section_type is a structured {error}, never a throw", () => {
  const result = checkSectionStyle({ sectionType: "not-a-real-type", text: "x" }) as { error: string };
  assert.match(result.error, /unknown section-type/);
});

test("checkSectionStyle: no resolvable text source is a structured {error}, never a throw", () => {
  const result = checkSectionStyle({ sectionType: "abstract" }) as { error: string };
  assert.match(result.error, /provide either 'text'/);
});

// --- lookupPublishedFraming ----------------------------------------------------

test("lookupPublishedFraming: disabled by default (no env var, no options.enabled) fails closed with a DISABLED code", async () => {
  const previous = process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR];
  delete process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR];
  try {
    const result = await lookupPublishedFraming("introduction", "diffusion models");
    assert.equal(result.code, "DISABLED");
    assert.match("error" in result ? result.error : "", /disabled by default/);
  } finally {
    if (previous === undefined) delete process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR];
    else process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR] = previous;
  }
});

test("lookupPublishedFraming: options.enabled:true with no injected researchProvider fails closed with NO_PROVIDER, never a silent empty result", async () => {
  const result = await lookupPublishedFraming("introduction", "diffusion models", { enabled: true });
  assert.equal(result.code, "NO_PROVIDER");
  assert.match("error" in result ? result.error : "", /no research-tool dependency is available/);
});

test("lookupPublishedFraming: reads the env var gate when options.enabled is not explicitly set", async () => {
  const previous = process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR];
  process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR] = "1";
  try {
    const result = await lookupPublishedFraming("introduction", "diffusion models");
    // Enabled via env var, but still no provider injected -- still fails
    // closed, just via the NO_PROVIDER path instead of DISABLED.
    assert.equal(result.code, "NO_PROVIDER");
  } finally {
    if (previous === undefined) delete process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR];
    else process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR] = previous;
  }
});

test("lookupPublishedFraming: an injected researchProvider returns a live, attributed, never-stored result", async () => {
  const fakeProvider = async ({ sectionType, topic }: { sectionType: SectionTypeId; topic: string }) => ({
    excerpt: `An excerpt discussing ${topic} within a ${sectionType} section.`,
    citation: "Doe & Roe, 2024",
    sourceUrl: "https://example.org/doe-roe-2024",
  });
  const result = await lookupPublishedFraming("methods", "gradient checkpointing", {
    enabled: true,
    researchProvider: fakeProvider,
  });
  assert.ok(!("error" in result), "expected a successful result, not an error");
  if ("error" in result) throw new Error("unreachable");
  assert.equal(result.sectionType, "methods");
  assert.equal(result.topic, "gradient checkpointing");
  assert.match(result.excerpt, /gradient checkpointing/);
  assert.equal(result.citation, "Doe & Roe, 2024");
  assert.equal(result.sourceUrl, "https://example.org/doe-roe-2024");
  assert.equal(result.stored, false);
  assert.equal(typeof result.fetchedAt, "string");
  assert.ok(!Number.isNaN(Date.parse(result.fetchedAt)));
});

test("lookupPublishedFraming: a provider that throws is caught and surfaced as a structured PROVIDER_ERROR", async () => {
  const throwingProvider = async (): Promise<never> => {
    throw new Error("network unreachable");
  };
  const result = await lookupPublishedFraming("methods", "gradient checkpointing", {
    enabled: true,
    researchProvider: throwingProvider,
  });
  assert.equal(result.code, "PROVIDER_ERROR");
  assert.match("error" in result ? result.error : "", /network unreachable/);
});

test("lookupPublishedFraming: a provider that returns no usable excerpt is a structured EMPTY_RESULT, never a throw", async () => {
  const emptyProvider = async () => ({ excerpt: "" });
  const result = await lookupPublishedFraming("methods", "gradient checkpointing", {
    enabled: true,
    researchProvider: emptyProvider,
  });
  assert.equal(result.code, "EMPTY_RESULT");
});

test("lookupPublishedFraming: missing topic, or an unrecognized section-type, is a structured {error}, never a throw", async () => {
  const missingTopic = await lookupPublishedFraming("introduction", "", { enabled: true });
  assert.match("error" in missingTopic ? missingTopic.error : "", /missing 'topic'/);

  const badType = await lookupPublishedFraming("not-a-real-type", "something", { enabled: true });
  assert.match("error" in badType ? badType.error : "", /unknown section-type/);
});
