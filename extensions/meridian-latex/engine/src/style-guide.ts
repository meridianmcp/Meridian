// Style-guide feature: a static, originally-written reference of the
// rhetorical "moves" academic papers conventionally make in each of 8
// section types, plus two tools built on top of it --
//   - getStyleGuide(sectionType?): pure data lookup, no network.
//   - checkSectionStyle(input, sectionType): heuristic present/missing/
//     out-of-order comparison of a section's text against its expected move
//     sequence.
// -- and a third, deliberately separate STRETCH tool:
//   - lookupPublishedFraming(sectionType, topic, options): an OPT-IN, OFF-BY-
//     DEFAULT live lookup of a short, attributed excerpt from a real
//     published paper, fetched through an injected research-provider
//     dependency (never a hardcoded provider -- see its own header below).
//
// Tier-1 content note (npm publishConfig.access is "public" -- see
// package.json -- so this data ships to anyone who installs the package):
// every move-sequence description below is ORIGINALLY WRITTEN from general
// knowledge of academic-writing conventions (the kind of structural guidance
// found in generic writing-center handouts and taught broadly, not lifted
// from or attributed to any specific paper). No verbatim excerpts of
// anyone's copyrighted text appear anywhere in this file. `cues` arrays are
// short, generic phrase fragments a checker might plausibly see used for
// that rhetorical move -- not quotations from a real paper either.

import { parse, extractOutline, type OutlineNode } from "./outline.js";

/** Canonical ids for the 8 section-type categories this guide covers. */
export type SectionTypeId =
  | "abstract"
  | "introduction"
  | "related-work"
  | "methods"
  | "results"
  | "limitations"
  | "discussion-conclusion"
  | "ethics-broader-impact";

/** Canonical, ordered list of the 8 section-type ids this guide covers --
 * exported so a caller (or a test) can iterate them without hand-copying the
 * list, the same "explicit array as the ordering contract" reasoning
 * lint.js's own SECTION_ORDER comment already documents for section levels. */
export const SECTION_TYPE_ORDER: SectionTypeId[] = [
  "abstract",
  "introduction",
  "related-work",
  "methods",
  "results",
  "limitations",
  "discussion-conclusion",
  "ethics-broader-impact",
];

/** Free-text aliases a caller might reasonably type for a section-type,
 * normalized to one of SECTION_TYPE_ORDER's canonical ids. Keys are already
 * lowercased/trimmed/single-spaced -- see normalizeSectionType, the only
 * place this map is read. */
const SECTION_TYPE_ALIASES: Record<string, SectionTypeId> = {
  abstract: "abstract",
  introduction: "introduction",
  intro: "introduction",
  "related work": "related-work",
  "related-work": "related-work",
  relatedwork: "related-work",
  "prior work": "related-work",
  background: "related-work",
  methods: "methods",
  method: "methods",
  methodology: "methods",
  approach: "methods",
  results: "results",
  result: "results",
  findings: "results",
  limitations: "limitations",
  limitation: "limitations",
  "threats to validity": "limitations",
  discussion: "discussion-conclusion",
  conclusion: "discussion-conclusion",
  conclusions: "discussion-conclusion",
  "discussion/conclusion": "discussion-conclusion",
  "discussion / conclusion": "discussion-conclusion",
  "discussion and conclusion": "discussion-conclusion",
  "discussion-conclusion": "discussion-conclusion",
  ethics: "ethics-broader-impact",
  "broader impact": "ethics-broader-impact",
  "broader impacts": "ethics-broader-impact",
  "ethics/broader impact": "ethics-broader-impact",
  "ethics / broader impact": "ethics-broader-impact",
  "ethics and broader impact": "ethics-broader-impact",
  "ethics statement": "ethics-broader-impact",
  "broader impact statement": "ethics-broader-impact",
  "ethics-broader-impact": "ethics-broader-impact",
};

/** Normalizes a free-text section-type string to one of SECTION_TYPE_ORDER's
 * canonical ids, or `null` if it doesn't match any known alias. Never
 * throws -- a non-string input is just treated as unmatched. Typed to accept
 * `unknown` (not `string`) so this file's own test suite can exercise that
 * non-string-input contract without the call being rejected at compile time
 * first. */
export function normalizeSectionType(input: unknown): SectionTypeId | null {
  if (typeof input !== "string") return null;
  const key = input.trim().toLowerCase().replace(/\s+/g, " ");
  return SECTION_TYPE_ALIASES[key] || null;
}

/** One rhetorical "move" within a section-type's expected sequence. */
export interface StyleMove {
  id: string;
  name: string;
  description: string;
  cues: string[];
}

/** One section-type's guide: a human-readable label plus its ordered move
 * sequence. */
export interface StyleGuideEntry {
  label: string;
  moves: StyleMove[];
}

/**
 * The 8 section-type guides. Each guide is `{ label, moves }`, where `moves`
 * is the ORDERED sequence a reader conventionally expects for that section
 * type -- `checkSectionStyle` below treats this array's own order as the
 * canonical order it heuristically checks a real section against. Each move
 * is `{ id, name, description, cues }`: `cues` is a short list of generic,
 * lowercase phrase fragments checkSectionStyle scans for (case-insensitively,
 * plain substring match) as heuristic evidence that move is present --
 * deliberately NOT an exhaustive or precise detector (see checkSectionStyle's
 * own header for the "structural heuristic, not ground truth" contract this
 * whole feature is built around).
 */
export const STYLE_GUIDE: { version: number; guides: Record<SectionTypeId, StyleGuideEntry> } = {
  version: 1,
  guides: {
    abstract: {
      label: "Abstract",
      moves: [
        {
          id: "context",
          name: "Context / motivation",
          description: "One or two sentences establishing the problem domain and why it matters.",
          cues: [
            "has become increasingly important",
            "plays a critical role",
            "is a fundamental problem",
            "in recent years",
            "remains a key challenge",
          ],
        },
        {
          id: "gap",
          name: "Gap / problem statement",
          description: "What's unresolved, or the specific problem this paper tackles.",
          cues: ["however,", "remains unclear", "is not well understood", "existing approaches", "fail to", "an open problem"],
        },
        {
          id: "approach",
          name: "Approach / method",
          description: "A brief, concrete statement of what was actually done.",
          cues: ["we propose", "we present", "we introduce", "in this paper, we", "our approach", "we develop"],
        },
        {
          id: "results",
          name: "Key results",
          description: "The main quantitative or qualitative findings.",
          cues: ["we show that", "our results", "experiments demonstrate", "we find that", "outperforms", "achieves"],
        },
        {
          id: "implications",
          name: "Implications / contribution",
          description: "Why the results matter, or what they enable going forward.",
          cues: ["these results suggest", "our findings", "has implications for", "opens the door to", "paves the way"],
        },
      ],
    },
    introduction: {
      label: "Introduction",
      moves: [
        {
          id: "context",
          name: "Broad context",
          description: "Motivates the topic's importance to a reader unfamiliar with the specific problem.",
          cues: ["has become increasingly important", "plays a central role", "is widely used", "has seen growing interest"],
        },
        {
          id: "prior-work-summary",
          name: "Prior work summary",
          description: "A brief survey of what has already been done in this space.",
          cues: ["prior work has", "previous studies", "researchers have", "a growing body of work", "existing methods"],
        },
        {
          id: "gap",
          name: "Gap identification",
          description: "What's missing from, or limited about, that prior work.",
          cues: [
            "however,",
            "despite this progress",
            "remains an open question",
            "have not fully addressed",
            "a key limitation",
            "little attention has been paid",
          ],
        },
        {
          id: "objective",
          name: "Objective / contribution statement",
          description: "A direct statement of what this paper does to address the identified gap.",
          cues: ["in this paper, we", "we propose", "our contribution", "we address this gap by", "this work introduces"],
        },
        {
          id: "approach-preview",
          name: "Approach preview",
          description: "A short preview of the method, ahead of the full Methods section.",
          cues: ["specifically, we", "our method", "we design", "our approach"],
        },
        {
          id: "roadmap",
          name: "Paper roadmap",
          description: "An outline of how the rest of the paper is organized.",
          cues: [
            "the rest of this paper",
            "the remainder of this paper is organized",
            "we organize the paper as follows",
            "section 2",
          ],
        },
      ],
    },
    "related-work": {
      label: "Related Work",
      moves: [
        {
          id: "organizing-framework",
          name: "Organizing framework",
          description: "Groups prior work into thematic or methodological clusters rather than listing it flatly.",
          cues: ["can be broadly categorized", "we group prior work into", "falls into two", "several lines of work"],
        },
        {
          id: "summary-per-cluster",
          name: "Summary per cluster",
          description: "Describes what each identified cluster of prior work actually does.",
          cues: ["one line of work", "another line of work", "a second category", "this line of research"],
        },
        {
          id: "positioning",
          name: "Positioning",
          description: "States how this paper relates to, differs from, or builds on each cluster.",
          cues: ["unlike", "in contrast to", "building on", "differs from", "complementary to"],
        },
        {
          id: "gap-restated",
          name: "Gap restated",
          description: "An explicit statement of what remains unaddressed, transitioning into this paper's contribution.",
          cues: ["none of these", "to the best of our knowledge, no prior work", "remains unaddressed", "we are the first to"],
        },
      ],
    },
    methods: {
      label: "Methods",
      moves: [
        {
          id: "overview",
          name: "Overview / design rationale",
          description: "A high-level description of the approach and why it was chosen.",
          cues: ["we design", "our method consists of", "the overall approach", "at a high level", "the key idea"],
        },
        {
          id: "setup",
          name: "Setup / materials",
          description: "The data, environment, participants, or resources used.",
          cues: ["we use", "our dataset", "participants were", "the experimental setup", "we collect"],
        },
        {
          id: "procedure",
          name: "Procedure",
          description: "A step-by-step description of what was actually done.",
          cues: ["first, we", "next, we", "we then", "the procedure consists of", "step 1"],
        },
        {
          id: "metrics",
          name: "Metrics / evaluation criteria",
          description: "How success or the outcome is measured.",
          cues: ["we measure", "we evaluate using", "the evaluation metric", "to assess"],
        },
        {
          id: "validity",
          name: "Validity / controls",
          description: "Steps taken to ensure correctness or rule out confounds.",
          cues: ["to control for", "to rule out", "we ensure", "as a sanity check", "to avoid confounds"],
        },
      ],
    },
    results: {
      label: "Results",
      moves: [
        {
          id: "overview",
          name: "Overview statement",
          description: "A brief framing of what this results section covers.",
          cues: ["in this section, we report", "table 1 shows", "figure 1 presents", "we present the results of"],
        },
        {
          id: "primary",
          name: "Primary findings",
          description: "The main result(s), tied directly to the paper's core research question.",
          cues: ["our main result", "the primary finding", "as shown in", "significantly outperforms"],
        },
        {
          id: "secondary",
          name: "Supporting / secondary findings",
          description: "Additional results, ablations, or breakdowns beyond the headline result.",
          cues: ["additionally, we find", "in a secondary analysis", "we also observe", "as an ablation"],
        },
        {
          id: "statistical",
          name: "Statistical / quantitative substantiation",
          description: "Numbers, comparisons, or significance backing up the claims made.",
          cues: ["p <", "statistically significant", "confidence interval", "standard deviation"],
        },
        {
          id: "negative",
          name: "Negative / unexpected results",
          description: "Anomalies or non-significant findings, reported honestly rather than omitted.",
          cues: ["we did not find", "no significant difference", "unexpectedly,", "contrary to our hypothesis"],
        },
      ],
    },
    limitations: {
      label: "Limitations",
      moves: [
        {
          id: "scope",
          name: "Scope statement",
          description: "What the study does, and explicitly does not, claim to cover.",
          cues: ["our study is limited to", "we focus only on", "this work does not address", "the scope of this study"],
        },
        {
          id: "threats",
          name: "Threats to validity",
          description: "Specific weaknesses -- sample size, generalizability, assumptions made.",
          cues: ["a threat to validity", "one limitation is", "our sample size", "generalizability", "we assume"],
        },
        {
          id: "mitigations",
          name: "Mitigations",
          description: "What was done, if anything, to reduce the impact of each named limitation.",
          cues: ["to mitigate this", "we partially address this by", "we attempted to reduce"],
        },
        {
          id: "consequences",
          name: "Consequences for interpretation",
          description: "How a reader should weigh the results given these limits.",
          cues: [
            "should be interpreted with caution",
            "caution should be exercised",
            "these limitations suggest",
            "readers should",
          ],
        },
      ],
    },
    "discussion-conclusion": {
      label: "Discussion/Conclusion",
      moves: [
        {
          id: "summary",
          name: "Summary of findings",
          description: "Restates the main results without simply repeating numbers verbatim.",
          cues: ["in summary,", "to summarize,", "we have shown", "this paper presented"],
        },
        {
          id: "interpretation",
          name: "Interpretation",
          description: "What the findings mean in the context of the broader field.",
          cues: ["these results suggest", "this indicates", "we interpret this as", "this finding implies"],
        },
        {
          id: "comparison",
          name: "Comparison to prior work",
          description: "How this work's findings square with, or contradict, earlier findings.",
          cues: ["consistent with prior work", "in contrast to previous findings", "aligns with", "contradicts"],
        },
        {
          id: "implications",
          name: "Broader implications",
          description: "The practical or theoretical significance of the work.",
          cues: ["these findings have implications for", "has practical implications", "broader impact"],
        },
        {
          id: "future-work",
          name: "Future work",
          description: "Open questions or next steps left for later work.",
          cues: ["future work", "we leave", "an interesting direction", "we plan to"],
        },
        {
          id: "closing",
          name: "Closing statement",
          description: "A concise wrap-up of the paper's overall contribution.",
          cues: ["in conclusion,", "we conclude", "overall, this work", "taken together"],
        },
      ],
    },
    "ethics-broader-impact": {
      label: "Ethics/Broader Impact",
      moves: [
        {
          id: "benefits",
          name: "Potential benefits",
          description: "Who or what stands to gain from this work.",
          cues: ["this work could benefit", "potential benefits include", "positive societal impact"],
        },
        {
          id: "risks",
          name: "Potential risks / harms",
          description: "Misuse, bias, dual-use, or societal risk the work could introduce.",
          cues: ["potential risks", "could be misused", "raises concerns about", "a possible harm"],
        },
        {
          id: "mitigations",
          name: "Mitigations",
          description: "Steps taken, or recommended, to reduce the identified risk.",
          cues: ["to mitigate these risks", "we recommend", "safeguards", "responsible use"],
        },
        {
          id: "disclosure",
          name: "Disclosure",
          description: "Conflicts of interest, funding sources, and data/consent considerations.",
          cues: ["conflict of interest", "this work was funded by", "consent was obtained", "irb approval"],
        },
      ],
    },
  },
};

/** A short, explicit disclaimer every checkSectionStyle result carries in its
 * own output (not just its tool description) -- see this feature's own task
 * description: BOTH the tool description AND the output must say this is a
 * structural heuristic, never a ground-truth/compiler-verified check. */
export const HEURISTIC_DISCLAIMER =
  "This is a structural heuristic based on plain keyword/cue matching over the section's text -- it is NOT a " +
  "ground-truth or compiler-verified check of the section's actual rhetorical content. A missing/out-of-order " +
  "finding is a prompt for human judgment, not a verdict; a real section can satisfy a move using phrasing this " +
  "heuristic's cue list doesn't happen to include, and can just as easily contain a cue phrase used for a wholly " +
  "unrelated purpose.";

export type GetStyleGuideResult =
  | { sectionTypes: SectionTypeId[]; guides: Record<SectionTypeId, StyleGuideEntry> }
  | { sectionType: SectionTypeId; guide: StyleGuideEntry }
  | { error: string };

/**
 * Pure data lookup -- no network, no file access. With no sectionType, returns
 * the full guide set; with one, returns just that section-type's guide (or a
 * structured `{error}` if it doesn't match any known section-type/alias --
 * never throws, matching this codebase's established tool-boundary
 * convention, see mcp-server.js's callTool doc comment).
 */
export function getStyleGuide(sectionType?: unknown): GetStyleGuideResult {
  if (sectionType === undefined || sectionType === null || sectionType === "") {
    return { sectionTypes: SECTION_TYPE_ORDER, guides: STYLE_GUIDE.guides };
  }
  const normalized = normalizeSectionType(sectionType);
  if (!normalized) {
    return { error: `unknown section-type "${sectionType}" -- expected one of: ${SECTION_TYPE_ORDER.join(", ")}` };
  }
  return { sectionType: normalized, guide: STYLE_GUIDE.guides[normalized] };
}

export interface ResolveSectionTextInput {
  /** already-resolved raw section text, used as-is. */
  text?: string;
  sourceText?: string;
  headingId?: string;
  headingTitle?: string;
}

export type ResolveSectionTextResult =
  | { ok: true; text: string; resolvedFrom: "text" }
  | { ok: true; text: string; resolvedFrom: "sourceText"; heading: OutlineNode }
  | { ok: false; reason: string };

/**
 * Resolves the raw text checkSectionStyle should run against, from one of two
 * shapes:
 *   - `input.text`: already-resolved raw section text, used as-is.
 *   - `input.sourceText` + (`input.headingId` or `input.headingTitle`):
 *     resolved from a heading node's range in a fresh outline of
 *     `sourceText` (via outline.js's own parse()/extractOutline(), the same
 *     pair lint.js reuses rather than reimplementing) -- the range runs from
 *     that heading's own source line (inclusive, so the `\section{...}` line
 *     itself is included) up to the line just before the NEXT heading found
 *     anywhere later in the document (or end of document, if it's the last
 *     heading). This is a simple, document-order heuristic -- it does not
 *     account for a heading's nesting DEPTH (e.g. a `\subsection` under the
 *     target heading is treated as ending the section, same as a sibling
 *     `\section` would), a known, documented v0 limitation rather than a
 *     silent gap.
 *
 * Never throws -- a parse failure or an unmatched heading is a structured
 * `{ ok: false, reason }` return, exactly like every other check in this file.
 */
export function resolveSectionText(input: ResolveSectionTextInput = {}): ResolveSectionTextResult {
  const { text, sourceText, headingId, headingTitle } = input;

  if (typeof text === "string" && text.trim()) {
    return { ok: true, text, resolvedFrom: "text" };
  }

  if (typeof sourceText !== "string" || !sourceText.trim()) {
    return {
      ok: false,
      reason:
        "provide either 'text' (already-resolved raw section text) or 'sourceText' plus 'headingId'/'headingTitle' " +
        "to resolve a section's range from an outlined document",
    };
  }
  if (typeof headingId !== "string" && typeof headingTitle !== "string") {
    return { ok: false, reason: "resolving from 'sourceText' requires 'headingId' or 'headingTitle'" };
  }

  let ast;
  try {
    ast = parse(sourceText);
  } catch (err) {
    return { ok: false, reason: `could not parse 'sourceText': ${err instanceof Error ? err.message : String(err)}` };
  }
  if (!ast || !Array.isArray(ast.content)) {
    return { ok: false, reason: "parser returned a malformed AST for 'sourceText' (no content array)" };
  }

  const headings = extractOutline(ast).filter((n) => n.kind === "heading");
  const target = headingId
    ? headings.find((h) => h.id === headingId)
    : headings.find((h) => (h.title as string).toLowerCase() === String(headingTitle).toLowerCase());
  if (!target) {
    const which = headingId ? `headingId "${headingId}"` : `headingTitle "${headingTitle}"`;
    return { ok: false, reason: `no heading found matching ${which} in a fresh outline of 'sourceText'` };
  }
  if (typeof target.line !== "number") {
    return { ok: false, reason: "the matched heading has no source line position -- cannot resolve a range" };
  }
  // Captured into its own variable (rather than relying on TS narrowing
  // `target.line` across the closures below, which the type checker doesn't
  // reliably preserve through function-call boundaries).
  const targetLine: number = target.line;

  const sourceLines = sourceText.split(/\r\n|\r|\n/);
  // A trailing newline produces one spurious trailing "" element (not a real
  // content line) -- drop it so the LAST heading's range doesn't tack on an
  // extra blank line the source text itself doesn't have. Only ever pops
  // ONE element (never more), and only when the source genuinely ends in a
  // line terminator, so a document with real trailing blank lines keeps
  // them.
  if (sourceLines.length > 0 && sourceLines[sourceLines.length - 1] === "" && /[\r\n]$/.test(sourceText)) {
    sourceLines.pop();
  }
  const laterHeadingLines = headings
    .filter((h) => typeof h.line === "number" && h.line > targetLine)
    .map((h) => h.line as number);
  const endLineExclusive = laterHeadingLines.length > 0 ? Math.min(...laterHeadingLines) : sourceLines.length + 1;
  const sectionLines = sourceLines.slice(targetLine - 1, endLineExclusive - 1);

  return { ok: true, text: sectionLines.join("\n"), resolvedFrom: "sourceText", heading: target };
}

export interface CheckSectionStyleInput extends ResolveSectionTextInput {
  sectionType?: unknown;
}

export interface CheckSectionStyleDetail {
  id: string;
  name: string;
  present: boolean;
  matchOffset: number | null;
}

export type CheckSectionStyleResult =
  | {
      sectionType: SectionTypeId;
      heuristic: true;
      disclaimer: string;
      resolvedFrom: "text" | "sourceText";
      movesPresent: string[];
      movesMissing: string[];
      movesOutOfOrder: string[];
      details: CheckSectionStyleDetail[];
    }
  | { error: string };

/**
 * Heuristically compares a section's text against get_style_guide's expected
 * move sequence for its declared section-type: which moves are present
 * (matched via at least one of that move's `cues`), which are missing, and
 * which appear OUT OF ORDER relative to the canonical sequence (detected as
 * the moves whose first-cue-match position breaks a monotonically-increasing
 * walk over the canonical move order -- i.e. a present move that starts
 * earlier in the text than a present move the guide says should come before
 * it).
 *
 * `input` is the same `{text}` / `{sourceText, headingId|headingTitle}` shape
 * resolveSectionText accepts, plus the required `sectionType` this section is
 * declared to be (NOT inferred from the heading's own title text -- a real
 * paper's heading might read "Prior Art" for what the caller knows is really
 * its Related Work section, so the caller states the type explicitly rather
 * than this function guessing from wording).
 *
 * STRUCTURAL HEURISTIC, NOT GROUND TRUTH -- see HEURISTIC_DISCLAIMER above,
 * included verbatim in every successful result's own `disclaimer` field so a
 * caller surfacing this result to a human sees the caveat without having to
 * separately read this function's tool description.
 */
export function checkSectionStyle(input: CheckSectionStyleInput = {}): CheckSectionStyleResult {
  const { sectionType } = input;
  const normalized = normalizeSectionType(sectionType);
  if (!normalized) {
    return { error: `unknown section-type "${sectionType}" -- expected one of: ${SECTION_TYPE_ORDER.join(", ")}` };
  }

  const resolved = resolveSectionText(input);
  if (!resolved.ok) {
    return { error: resolved.reason };
  }

  const guide = STYLE_GUIDE.guides[normalized];
  const textLower = resolved.text.toLowerCase();
  const matchIndex = new Map<string, number>();
  for (const move of guide.moves) {
    let earliest = -1;
    for (const cue of move.cues) {
      const idx = textLower.indexOf(cue.toLowerCase());
      if (idx !== -1 && (earliest === -1 || idx < earliest)) earliest = idx;
    }
    if (earliest !== -1) matchIndex.set(move.id, earliest);
  }

  const present = guide.moves.filter((m) => matchIndex.has(m.id));
  const missing = guide.moves.filter((m) => !matchIndex.has(m.id)).map((m) => m.id);

  // Out-of-order detection: walk the guide's own moves in CANONICAL order,
  // tracking the highest actual text position seen so far among the present
  // moves already reviewed. A present move whose own text position is
  // EARLIER than that running maximum has shown up in the text before a
  // move the guide says should have come before IT -- e.g. a "results" move
  // (late in the canonical sequence) whose cue text appears at the very
  // start of the section, ahead of any "context" move, reads as jumping
  // ahead too soon, and is exactly what gets flagged here (as "results",
  // the move that's out of place -- not "context", which is exactly where
  // it should be). This is a single-pass greedy walk, not a full
  // longest-increasing-subsequence solve: with typically 4-6 moves per
  // guide, that's the more obviously-correct thing for a caller to read
  // (and to explain in a finding message) than an LIS DP would be, at the
  // cost of occasionally attributing an anomaly to one move in a pair where
  // a full LIS might attribute it to the other -- acceptable for a
  // heuristic that already disclaims itself as approximate.
  let maxPositionSeen = -1;
  const outOfOrder: string[] = [];
  for (const move of guide.moves) {
    if (!matchIndex.has(move.id)) continue;
    const position = matchIndex.get(move.id)!;
    if (position < maxPositionSeen) {
      outOfOrder.push(move.id);
    } else {
      maxPositionSeen = position;
    }
  }

  return {
    sectionType: normalized,
    heuristic: true,
    disclaimer: HEURISTIC_DISCLAIMER,
    resolvedFrom: resolved.resolvedFrom,
    movesPresent: present.map((m) => m.id),
    movesMissing: missing,
    movesOutOfOrder: outOfOrder,
    details: guide.moves.map((m) => ({
      id: m.id,
      name: m.name,
      present: matchIndex.has(m.id),
      matchOffset: matchIndex.has(m.id) ? (matchIndex.get(m.id) as number) : null,
    })),
  };
}

/** The env var that gates lookup_published_framing on -- see its own header
 * comment below for why this is an env var (a human-controlled setting on
 * the MCP server's own startup environment / the CLI process's shell) rather
 * than a per-call tool argument. */
export const ENABLE_PUBLISHED_FRAMING_ENV_VAR = "MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING";

function isPublishedFramingEnabled(options: LookupPublishedFramingOptions): boolean {
  if (typeof options.enabled === "boolean") return options.enabled;
  const raw = process.env[ENABLE_PUBLISHED_FRAMING_ENV_VAR];
  return raw === "1" || raw === "true";
}

export interface PublishedFramingExcerpt {
  excerpt: string;
  citation?: string;
  sourceUrl?: string;
  url?: string;
}

export interface LookupPublishedFramingOptions {
  enabled?: boolean;
  /** injected research/paper-search capability -- see this function's own
   * header for why this is never a hardcoded provider. */
  researchProvider?: (args: { sectionType: SectionTypeId; topic: string }) => Promise<PublishedFramingExcerpt>;
}

export type LookupPublishedFramingResult =
  | { error: string; code?: "DISABLED" | "NO_PROVIDER" | "PROVIDER_ERROR" | "EMPTY_RESULT" }
  | {
      sectionType: SectionTypeId;
      topic: string;
      excerpt: string;
      citation: string | null;
      sourceUrl: string | null;
      fetchedAt: string;
      stored: false;
      /** never present on a successful result -- declared here (always
       * `undefined`) purely so callers can read `.code` off this union
       * without narrowing first, matching how this file's own test suite
       * checks it. */
      code?: undefined;
    };

/**
 * STRETCH, OFF BY DEFAULT -- clearly separated from get_style_guide/
 * checkSectionStyle's safe, static, no-network default tier.
 *
 * Given a section-type + topic, fetches a short, properly attributed excerpt
 * from a REAL published paper at call time, via a research/paper-search
 * capability injected as `options.researchProvider` -- deliberately never a
 * hardcoded provider (no direct arXiv/Semantic Scholar/etc. client lives in
 * this file, or anywhere in this package): this standalone engine has no
 * paper-search capability of its own, so "detect availability at call time"
 * means exactly that -- check, on THIS call, whether the caller wired one in,
 * never assume one exists or cache that assumption across calls. This is the
 * same "swap the real implementation for a test double via an argument"
 * shape mcp-server.js's own callTool() already uses for its `deps` parameter
 * (see that file's own doc comment) -- `researchProvider` is that same kind
 * of injected dependency, just with no real default implementation to fall
 * back to, because none exists in this package.
 *
 * Gated OFF by default behind ENABLE_PUBLISHED_FRAMING_ENV_VAR (or an
 * explicit `options.enabled: true`) -- deliberately an environment-level
 * gate a human controls when configuring the MCP server's own startup
 * environment or the CLI's shell, NOT a tool-call argument an agent could
 * just set to true itself on any given call (that would defeat the entire
 * point of this being opt-in). This mirrors the human-owns-the-gate pattern
 * already used elsewhere in this codebase for genuinely risky capability
 * (see cli.js's own `login` command / mcp-server.js's CRITICAL SAFETY
 * SCOPING header) -- fetching and surfacing live excerpts of someone else's
 * copyrighted text is a materially different, more sensitive operation than
 * this file's other two, purely-local tools, and should not be silently on
 * by default just because this package is installed.
 *
 * If no research-tool dependency is available (the common case for this
 * package out of the box, since none is bundled), this FAILS CLOSED with a
 * clear, explicit `{error, code: "NO_PROVIDER"}` -- never a silent empty
 * result, per this feature's own design requirement.
 *
 * Never caches, stores, or bundles the fetched excerpt anywhere -- the
 * returned object is the ONLY place it ever lives; nothing here writes it to
 * disk, to the local claims/provenance SQLite store, or into any file this
 * npm package would ship. `stored: false` is included in every successful
 * result as an explicit, machine-readable confirmation of that contract.
 */
export async function lookupPublishedFraming(
  sectionType: unknown,
  topic: unknown,
  options: LookupPublishedFramingOptions = {},
): Promise<LookupPublishedFramingResult> {
  const normalized = normalizeSectionType(sectionType);
  if (!normalized) {
    return { error: `unknown section-type "${sectionType}" -- expected one of: ${SECTION_TYPE_ORDER.join(", ")}` };
  }
  if (typeof topic !== "string" || !topic.trim()) {
    return { error: "missing 'topic' -- lookup_published_framing needs a topic to search published papers for" };
  }

  if (!isPublishedFramingEnabled(options)) {
    return {
      error:
        "lookup_published_framing is disabled by default -- it is an opt-in stretch feature that performs a " +
        `live, attributed fetch from a real published paper. Set ${ENABLE_PUBLISHED_FRAMING_ENV_VAR}=1 in the ` +
        "environment this process runs in (or pass { enabled: true } explicitly) to turn it on.",
      code: "DISABLED",
    };
  }

  const provider = options.researchProvider;
  if (typeof provider !== "function") {
    return {
      error:
        "no research-tool dependency is available in the calling session -- lookup_published_framing requires a " +
        "paper-search/research capability to be injected via options.researchProvider (this standalone package " +
        "never hardcodes a specific provider). Failing closed rather than returning a silent empty result.",
      code: "NO_PROVIDER",
    };
  }

  let raw: PublishedFramingExcerpt;
  try {
    raw = await provider({ sectionType: normalized, topic });
  } catch (err) {
    return {
      error: `research provider threw while fetching: ${err instanceof Error ? err.message : String(err)}`,
      code: "PROVIDER_ERROR",
    };
  }
  if (!raw || typeof raw.excerpt !== "string" || !raw.excerpt.trim()) {
    return { error: "research provider returned no usable excerpt", code: "EMPTY_RESULT" };
  }

  return {
    sectionType: normalized,
    topic,
    excerpt: raw.excerpt,
    citation: typeof raw.citation === "string" ? raw.citation : null,
    sourceUrl: typeof raw.sourceUrl === "string" ? raw.sourceUrl : typeof raw.url === "string" ? raw.url : null,
    fetchedAt: new Date().toISOString(),
    stored: false,
  };
}
