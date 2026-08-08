// dashboard-documents.ts — DOCX review panel (b67ec6b5).
//
// Non-mutating DOCX review workflow for the Documents tab: loads a fresh
// GET /projects/{id}/document-review (docs_intel.build_document_review),
// groups findings by category/severity, and renders each finding with a
// human-readable locator (section_path, quoted text, Word Ctrl+F locator,
// bookmark/REF status) instead of a raw paragraph id alone.
//
// Pure grouping/formatting/locator-safety logic lives here (unit-tested by
// dashboard-documents.test.ts, no DOM needed) mirroring dashboard-notes.ts's
// notesLoadMoreState extraction; the DOM-wiring loader at the bottom calls
// into it. Symbols are re-exposed on window so inline handlers + cross-file
// references keep resolving after esbuild IIFE bundling.
//
// READ-ONLY, always: this panel never writes to the .docx. Every finding is
// a recommendation surfaced from a read-only snapshot — there is no draft
// overlay or committed-mutation tier in this first version (see the sprint
// item notes); the panel labels results "read-only recommendation" so a
// future write-capable tier is visually distinguishable when it lands.

export interface ReviewLocator {
  status?: string | null; // "resolved" | "ambiguous" | "not_found" | "stale" | "not_applicable"
  section_path?: string | null;
  heading_para_id?: string | null;
  target_para_id?: string | null;
  document_order?: number | null;
  element_type?: string | null;
  quoted_text?: string | null;
  leading_text_preview?: string | null;
  first_words?: string | null;
  word_search_locator?: string | null;
  bookmark_exists?: boolean | null;
  ref_status?: any;
  candidates?: Array<{
    target_para_id?: string | null;
    element_type?: string | null;
    document_order?: number | null;
    section_path?: string | null;
    leading_text_preview?: string | null;
  }> | null;
  reason?: string | null;
}

export interface ReviewFinding {
  category: string;
  severity: string;
  type: string;
  detail?: any;
  locator?: ReviewLocator | null;
}

export interface DocumentReviewResult {
  status?: string;
  docx_path?: string;
  source_fingerprint?: string;
  findings?: ReviewFinding[];
  finding_count?: number;
  findings_by_category?: Record<string, number>;
  findings_by_severity?: Record<string, number>;
  categories?: string[];
  reason?: string;
  error?: string;
}

// b67ec6b5 — fixed category set (matches docs_intel.REVIEW_CATEGORIES). Kept
// in display order — findings render structure first, integrity concerns
// last, so a reviewer sees big-picture issues before line-level ones.
export const REVIEW_CATEGORY_LABELS: Record<string, string> = {
  structure: 'Structure',
  section_page: 'Sections & pages',
  caption: 'Captions',
  equation: 'Equations',
  ownership: 'Ownership',
  provenance: 'Provenance',
  render_integrity: 'Render & integrity',
};
export const REVIEW_CATEGORY_ORDER: string[] = Object.keys(REVIEW_CATEGORY_LABELS);

export const REVIEW_SEVERITY_ORDER: Record<string, number> = { error: 0, warning: 1, info: 2 };
export const REVIEW_SEVERITY_LABELS: Record<string, string> = { error: 'Error', warning: 'Warning', info: 'Info' };
export const REVIEW_SEVERITY_COLOR: Record<string, string> = {
  error: 'var(--error, #f85149)',
  warning: 'var(--warning, #d29922)',
  info: 'var(--muted)',
};

const MAX_PREVIEW_CHARS = 140;

/**
 * Truncate a long paragraph preview to a safe display length WITHOUT hiding
 * that it was truncated — an un-truncated quote could blow out the review
 * panel's layout on a long paragraph, but silently cutting it is misleading.
 * Always returns whether truncation happened so a caller can show a "full
 * text" affordance rather than pretending the preview is complete.
 */
export function truncatePreview(
  text: string | null | undefined,
  max: number = MAX_PREVIEW_CHARS,
): { text: string; truncated: boolean; fullText: string } {
  const s = String(text || '');
  if (s.length <= max) return { text: s, truncated: false, fullText: s };
  return { text: s.slice(0, max).trimEnd() + '…', truncated: true, fullText: s };
}

/**
 * Group findings by category, preserving REVIEW_CATEGORY_ORDER and including
 * every category the review reports (even with zero findings) so the panel
 * renders a STABLE set of section headers — a document with no equations
 * still shows an empty "Equations" section rather than making the category
 * disappear (the sprint item's "framework-agnostic" requirement). Findings
 * within a category are sorted worst-severity-first.
 */
export function groupFindingsByCategory(
  findings: ReviewFinding[] | null | undefined,
  categories: string[] | null | undefined,
): Array<{ category: string; label: string; findings: ReviewFinding[] }> {
  const cats = categories && categories.length ? categories.slice() : REVIEW_CATEGORY_ORDER.slice();
  const byCat = new Map<string, ReviewFinding[]>();
  for (const c of cats) byCat.set(c, []);
  for (const f of findings || []) {
    if (!f) continue;
    if (!byCat.has(f.category)) byCat.set(f.category, []);
    (byCat.get(f.category) as ReviewFinding[]).push(f);
  }
  const ordered = [...byCat.keys()].sort((a, b) => {
    const ia = REVIEW_CATEGORY_ORDER.indexOf(a);
    const ib = REVIEW_CATEGORY_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  return ordered.map((category) => ({
    category,
    label: REVIEW_CATEGORY_LABELS[category] || category,
    findings: (byCat.get(category) as ReviewFinding[]).slice().sort(
      (a, b) => (REVIEW_SEVERITY_ORDER[a.severity] ?? 9) - (REVIEW_SEVERITY_ORDER[b.severity] ?? 9),
    ),
  }));
}

/** True when the review completed cleanly with zero findings across every
 *  category — an explicit "clean" state, distinct from "review failed"
 *  (error) or "stale" (needs a re-check before it can be trusted). */
export function isReviewEmpty(review: DocumentReviewResult | null | undefined): boolean {
  if (!review || review.error) return false;
  if (review.status && review.status !== 'ok') return false;
  return !(review.findings && review.findings.length);
}

export function isReviewStale(review: DocumentReviewResult | null | undefined): boolean {
  return !!review && review.status === 'stale';
}

export function isReviewError(review: DocumentReviewResult | null | undefined): boolean {
  return !!review && !!review.error;
}

export interface LocatorSummary {
  kind: 'resolved' | 'ambiguous' | 'not_found' | 'stale' | 'not_applicable' | 'unknown';
  sectionPath: string;
  preview: string;
  fullText: string;
  truncated: boolean;
  wordSearchLocator: string;
  bookmarkExists: boolean;
  candidateCount: number;
  candidates: Array<{ label: string; sectionPath: string; preview: string }>;
  reason: string;
}

/**
 * Render-safe locator summary for one finding.
 *
 * Deliberately never exposes a raw ``target_para_id`` alone — a resolved
 * locator always pairs it with ``sectionPath``/``preview`` (the caller can
 * still read ``target_para_id`` off the original locator for a copy-button,
 * but nothing here surfaces the bare id as the PRIMARY label) — and never
 * infers a unique target when the match is ambiguous: an "ambiguous" status
 * returns the ``candidates`` list instead of picking one, exactly mirroring
 * the sprint item's "do not expose raw paragraph IDs alone / do not infer a
 * unique target when text is ambiguous; show candidate rows instead" rule.
 */
export function summarizeLocator(locator: ReviewLocator | null | undefined): LocatorSummary {
  const status = (locator && locator.status) || 'unknown';

  if (status === 'resolved') {
    const raw = (locator!.quoted_text || locator!.leading_text_preview || locator!.first_words || '');
    const { text, truncated, fullText } = truncatePreview(raw);
    return {
      kind: 'resolved',
      sectionPath: locator!.section_path || '(document root)',
      preview: text,
      fullText,
      truncated,
      wordSearchLocator: locator!.word_search_locator || locator!.first_words || '',
      bookmarkExists: !!locator!.bookmark_exists,
      candidateCount: 0,
      candidates: [],
      reason: '',
    };
  }

  if (status === 'ambiguous') {
    const candidates = (locator?.candidates || []).map((c) => ({
      label: c.target_para_id || c.element_type || 'candidate',
      sectionPath: c.section_path || '(document root)',
      preview: truncatePreview(c.leading_text_preview).text,
    }));
    return {
      kind: 'ambiguous',
      sectionPath: '', preview: '', fullText: '', truncated: false, wordSearchLocator: '',
      bookmarkExists: false,
      candidateCount: candidates.length,
      candidates,
      reason: locator?.reason || 'multiple elements matched this location — showing candidates instead of guessing',
    };
  }

  if (status === 'stale') {
    return {
      kind: 'stale',
      sectionPath: '', preview: '', fullText: '', truncated: false, wordSearchLocator: '',
      bookmarkExists: false, candidateCount: 0, candidates: [],
      reason: locator?.reason || 'the document changed since this location was resolved',
    };
  }

  if (status === 'not_applicable') {
    return {
      kind: 'not_applicable',
      sectionPath: '', preview: '', fullText: '', truncated: false, wordSearchLocator: '',
      bookmarkExists: false, candidateCount: 0, candidates: [],
      reason: 'this finding has no single paragraph-level location',
    };
  }

  // "not_found" (missing/unresolvable id) or any unrecognized status — never
  // fabricate a location; report the reason (or a safe default) only.
  return {
    kind: 'not_found',
    sectionPath: '', preview: '', fullText: '', truncated: false, wordSearchLocator: '',
    bookmarkExists: false, candidateCount: 0, candidates: [],
    reason: locator?.reason || 'location could not be resolved',
  };
}

// ---------------------------------------------------------------------------
// DOM wiring — fetches the review and renders it via the pure helpers above.
// ---------------------------------------------------------------------------

// b67ec6b5 — remembers the last-seen source_fingerprint per target element so
// a "Re-check" click can pass expected_source_fingerprint and honestly
// surface staleness (the document changed underneath the first review)
// instead of silently re-resolving against new content as if nothing moved.
const _reviewFingerprints = new Map<string, string>();

function _findingRow(f: ReviewFinding): string {
  const sev = String(f.severity || 'info');
  const color = REVIEW_SEVERITY_COLOR[sev] || 'var(--muted)';
  const loc = summarizeLocator(f.locator);
  let locHtml: string;
  if (loc.kind === 'resolved') {
    const copyText = escapeHtml(loc.wordSearchLocator || '');
    locHtml = `<div style="margin-top:4px;font-size:9px;color:var(--muted)">${escapeHtml(loc.sectionPath)}</div>
      <div style="margin-top:2px;font-size:10px;color:var(--text);font-style:italic">&ldquo;${escapeHtml(loc.preview)}&rdquo;${loc.truncated ? ' <span style="color:var(--muted);font-style:normal">(truncated)</span>' : ''}</div>
      <div style="margin-top:4px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <button class="review-copy-locator-btn secondary" data-locator="${copyText}" style="font-size:8px;padding:1px 6px">Copy Word Ctrl+F text</button>
        <span style="font-size:8px;color:var(--muted)">${loc.bookmarkExists ? 'bookmark/REF found' : 'no bookmark/REF'}</span>
      </div>`;
  } else if (loc.kind === 'ambiguous') {
    locHtml = `<div style="margin-top:4px;font-size:9px;color:var(--warning,#d29922)">${escapeHtml(loc.reason)}</div>
      <div style="margin-top:2px">${loc.candidates.map(c =>
        `<div style="font-size:9px;color:var(--muted);padding:2px 0 2px 8px;border-left:2px solid var(--border)">${escapeHtml(c.sectionPath)} &mdash; &ldquo;${escapeHtml(c.preview)}&rdquo;</div>`
      ).join('')}</div>`;
  } else {
    locHtml = `<div style="margin-top:4px;font-size:9px;color:var(--muted)">${escapeHtml(loc.reason)}</div>`;
  }
  return `<div style="border:1px solid var(--border);border-left:3px solid ${color};border-radius:0 4px 4px 0;padding:6px 10px;margin-bottom:6px;background:var(--surface-1)">
    <div style="display:flex;align-items:center;gap:6px">
      <span style="font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:${color}">${escapeHtml(REVIEW_SEVERITY_LABELS[sev] || sev)}</span>
      <span style="font-size:10px;color:var(--text);font-weight:600">${escapeHtml(String(f.type || '').replace(/_/g, ' '))}</span>
    </div>
    ${locHtml}
  </div>`;
}

/** Render one loaded DocumentReviewResult into a target element (innerHTML). */
export function renderDocumentReview(review: DocumentReviewResult | null | undefined, targetId: string): void {
  const target = document.getElementById(targetId);
  if (!target) return;

  if (isReviewError(review)) {
    target.innerHTML = `<div style="font-size:9px;color:var(--error)">${escapeHtml(String(review!.error))}</div>`;
    return;
  }
  if (isReviewStale(review)) {
    _reviewFingerprints.delete(targetId);
    target.innerHTML = `<div style="font-size:9px;color:var(--warning,#d29922)">Document changed since the last review (${escapeHtml(String(review!.reason || 'stale snapshot'))}). <button class="review-recheck-btn secondary" data-target="${escapeHtml(targetId)}" style="font-size:8px;padding:1px 6px;margin-left:4px">Re-check</button></div>`;
    return;
  }

  if (review && review.source_fingerprint) _reviewFingerprints.set(targetId, review.source_fingerprint);

  const banner = `<div style="font-size:8px;color:var(--muted);margin-bottom:6px;padding:3px 6px;border:1px solid var(--border);border-radius:3px;display:inline-block">READ-ONLY RECOMMENDATION &mdash; no DOCX writes; draft-overlay and committed-mutation review tiers are not yet available</div>`;

  if (isReviewEmpty(review)) {
    target.innerHTML = `${banner}<div class="empty" style="color:var(--muted);padding:6px 0">No findings — this document looks clean across every checked category.</div>`;
    return;
  }

  const groups = groupFindingsByCategory(review!.findings, review!.categories);
  let html = banner;
  html += `<div style="font-size:9px;color:var(--muted);margin-bottom:8px">${review!.finding_count ?? (review!.findings || []).length} finding(s) across ${groups.filter(g => g.findings.length).length} categor${groups.filter(g => g.findings.length).length === 1 ? 'y' : 'ies'}.</div>`;
  for (const g of groups) {
    if (!g.findings.length) continue;
    html += `<div style="margin:8px 0 4px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--accent)">${escapeHtml(g.label)} <span style="color:var(--muted);font-weight:400">${g.findings.length}</span></div>`;
    for (const f of g.findings) html += _findingRow(f);
  }
  target.innerHTML = html;

  target.querySelectorAll('.review-copy-locator-btn').forEach((el: any) => {
    el.addEventListener('click', () => {
      const txt = el.getAttribute('data-locator') || '';
      try { navigator.clipboard.writeText(txt); } catch (_) { /* best-effort */ }
      const prev = el.textContent;
      el.textContent = 'Copied ✓';
      setTimeout(() => { el.textContent = prev || 'Copy Word Ctrl+F text'; }, 1500);
    });
  });
}

/**
 * Fetch + render the DOCX review for one document into `#${targetId}`.
 * `expectedFingerprint` re-checks against a previously-seen
 * source_fingerprint (passed by the "Re-check" button); omit for a fresh,
 * unconditional review.
 */
export async function loadDocumentReview(
  projectId: string,
  filePath: string,
  targetId: string,
  expectedFingerprint?: string | null,
): Promise<void> {
  const target = document.getElementById(targetId);
  if (!target) return;
  target.innerHTML = '<span style="font-size:9px;color:var(--muted)">loading review…</span>';
  try {
    let url = `/projects/${projectId}/document-review?path=${encodeURIComponent(filePath)}`;
    if (expectedFingerprint) url += `&expected_source_fingerprint=${encodeURIComponent(expectedFingerprint)}`;
    const review = await api(url);
    renderDocumentReview(review, targetId);
  } catch (e: any) {
    target.innerHTML = `<span style="font-size:9px;color:var(--error)">Review failed: ${escapeHtml(String(e))}</span>`;
  }
}

/** Wire "Review findings" / "Re-check" buttons scoped under `root` (defaults
 *  to the whole document) — called after the Documents tab re-renders its
 *  document cards. */
export function wireDocumentReviewButtons(projectId: string, root: ParentNode = document): void {
  root.querySelectorAll('.doc-review-btn').forEach((btn: any) => {
    btn.addEventListener('click', async () => {
      const fp = btn.getAttribute('data-fp') || '';
      const did = btn.getAttribute('data-did') || '';
      const targetId = `doc-review-${did}`;
      await loadDocumentReview(projectId, fp, targetId);
    });
  });
  root.querySelectorAll('.review-recheck-btn').forEach((btn: any) => {
    btn.addEventListener('click', async () => {
      const targetId = btn.getAttribute('data-target') || '';
      const target = document.getElementById(targetId);
      const fp = target ? target.getAttribute('data-fp') || '' : '';
      if (!fp) return;
      const prevFingerprint = _reviewFingerprints.get(targetId) || null;
      await loadDocumentReview(projectId, fp, targetId, prevFingerprint);
    });
  });
}

// --- esbuild: re-expose top-level symbols as globals so inline handlers and
// cross-file references keep resolving after IIFE bundling.
try {
  Object.assign(window, {
    groupFindingsByCategory, summarizeLocator, truncatePreview,
    isReviewEmpty, isReviewStale, isReviewError,
    renderDocumentReview, loadDocumentReview, wireDocumentReviewButtons,
  });
} catch (e) { /* non-browser test environment */ }
