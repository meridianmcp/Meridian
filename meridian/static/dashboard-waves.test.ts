// Unit tests for the client-side parallel-execution wave/batch helper (e3355ccb).
// computeWaveProgress mirrors db.get_parallelizable_groups: it must produce the
// same conflict-free batches from a sprint-items payload the Live tab fetches.
import { describe, expect, it } from "vitest";
import {
  computeWaveProgress,
  parseTouchesResources,
  formatElapsedSince,
  buildWaveProgressHtml,
  buildWaveSummaryHtml,
  renderWaveSummary,
  type WaveItem,
  type WaveSummary,
} from "./dashboard-waves";

// Minimal item factory — mirrors the /sprint-items row shape.
const item = (over: Partial<WaveItem> & { id: string }): WaveItem => ({
  status: "pending",
  ...over,
});

describe("parseTouchesResources", () => {
  it("decodes a JSON array, a CSV string, and a python-ish list", () => {
    expect(parseTouchesResources('["file:a.py","file:b.py"]')).toEqual(["file:a.py", "file:b.py"]);
    expect(parseTouchesResources("file:a.py, file:b.py")).toEqual(["file:a.py", "file:b.py"]);
    expect(parseTouchesResources(["file:a.py", "file:b.py"])).toEqual(["file:a.py", "file:b.py"]);
  });
  it("dedupes first-seen and strips the inferred: provenance marker", () => {
    expect(parseTouchesResources('["file:a.py","inferred:file:a.py","file:b.py"]'))
      .toEqual(["file:a.py", "file:b.py"]);
  });
  it("null / empty / blank → []", () => {
    expect(parseTouchesResources(null)).toEqual([]);
    expect(parseTouchesResources("")).toEqual([]);
    expect(parseTouchesResources("   ")).toEqual([]);
    expect(parseTouchesResources("[]")).toEqual([]);
  });
});

describe("computeWaveProgress — batching (mirrors get_parallelizable_groups)", () => {
  it("two disjoint-resource items color into ONE parallel batch", () => {
    const wp = computeWaveProgress([
      item({ id: "1", touches_resources: '["file:a.py"]' }),
      item({ id: "2", touches_resources: '["file:b.py"]' }),
    ]);
    expect(wp.groupCount).toBe(1);
    expect(wp.batches[0].total).toBe(2);
    expect(wp.batches[0].parallel).toBe(true);
    expect(wp.strategy).toBe("parallel");
    expect(wp.maxFanOut).toBe(2);
    expect(wp.eligibleCount).toBe(2);
  });

  it("two items sharing a file conflict → TWO sequential batches", () => {
    const wp = computeWaveProgress([
      item({ id: "1", touches_resources: '["file:a.py"]' }),
      item({ id: "2", touches_resources: '["file:a.py"]' }),
    ]);
    expect(wp.groupCount).toBe(2);
    expect(wp.batches.every((b) => b.total === 1)).toBe(true);
    expect(wp.strategy).toBe("sequential");
    expect(wp.maxFanOut).toBe(1);
  });

  it("file lock conflicts with a symbol on that file (cross-type, 63b030a6)", () => {
    const wp = computeWaveProgress([
      item({ id: "1", touches_resources: '["file:a.py"]' }),
      item({ id: "2", touches_resources: '["symbol:a.py::foo"]' }),
    ]);
    // file:a.py ⊃ symbol:a.py::foo → conflict → 2 batches.
    expect(wp.groupCount).toBe(2);
  });

  it("two DISTINCT symbols on the same file co-schedule (one batch)", () => {
    const wp = computeWaveProgress([
      item({ id: "1", touches_resources: '["symbol:a.py::foo"]' }),
      item({ id: "2", touches_resources: '["symbol:a.py::bar"]' }),
    ]);
    expect(wp.groupCount).toBe(1);
    expect(wp.batches[0].total).toBe(2);
  });

  it("undeclared items each get their own sequential singleton batch (de730a25)", () => {
    const wp = computeWaveProgress([
      item({ id: "1" }), // no touches_resources
      item({ id: "2" }),
    ]);
    expect(wp.groupCount).toBe(2);
    expect(wp.undeclaredCount).toBe(2);
    expect(wp.batches.every((b) => b.total === 1)).toBe(true);
    expect(wp.strategy).toBe("sequential");
  });
});

describe("computeWaveProgress — eligibility / running / blocked", () => {
  it("done + in_progress items are not eligible; in_progress counts as running", () => {
    const wp = computeWaveProgress([
      item({ id: "1", status: "done" }),
      item({ id: "2", status: "in_progress", claimed_at: "2026-07-11 20:00:00" }),
      item({ id: "3", status: "pending", touches_resources: '["file:x.py"]' }),
    ]);
    expect(wp.doneCount).toBe(1);
    expect(wp.runningCount).toBe(1);
    expect(wp.eligibleCount).toBe(1); // only item 3
    expect(wp.groupCount).toBe(1);
  });

  it("a claimed pending item is running and NOT offered to a new batch", () => {
    const wp = computeWaveProgress([
      item({ id: "1", status: "pending", claimed_at: "2026-07-11 20:00:00", touches_resources: '["file:x.py"]' }),
    ]);
    expect(wp.runningCount).toBe(1);
    expect(wp.eligibleCount).toBe(0);
    expect(wp.groupCount).toBe(0);
  });

  it("an item blocked on a non-terminal parent is counted blocked, not eligible", () => {
    const wp = computeWaveProgress([
      item({ id: "parent", status: "in_progress", claimed_at: "2026-07-11 20:00:00" }),
      item({ id: "child", status: "pending", depends_on: "parent", touches_resources: '["file:y.py"]' }),
    ]);
    expect(wp.blockedCount).toBe(1);
    expect(wp.eligibleCount).toBe(0);
    expect(wp.runningCount).toBe(1); // the parent
  });

  it("a child of a DONE parent is eligible (dependency satisfied)", () => {
    const wp = computeWaveProgress([
      item({ id: "parent", status: "done" }),
      item({ id: "child", status: "pending", depends_on: "parent", touches_resources: '["file:y.py"]' }),
    ]);
    expect(wp.blockedCount).toBe(0);
    expect(wp.eligibleCount).toBe(1);
  });

  it("failure_mode=continue lets a child of a FAILED parent stay eligible", () => {
    const wp = computeWaveProgress([
      item({ id: "parent", status: "failed" }),
      item({ id: "child", status: "pending", depends_on: "parent", failure_mode: "continue", touches_resources: '["file:z.py"]' }),
    ]);
    expect(wp.eligibleCount).toBe(1);
    expect(wp.blockedCount).toBe(0);
  });

  it("failure_mode=stop blocks a child of a FAILED parent", () => {
    const wp = computeWaveProgress([
      item({ id: "parent", status: "failed" }),
      item({ id: "child", status: "pending", depends_on: "parent", failure_mode: "stop", touches_resources: '["file:z.py"]' }),
    ]);
    expect(wp.eligibleCount).toBe(0);
    expect(wp.blockedCount).toBe(1);
  });

  it("manual-blocker items are excluded entirely (2282a636)", () => {
    const wp = computeWaveProgress([
      item({ id: "1", status: "pending", blocker_kind: "manual", touches_resources: '["file:x.py"]' }),
      item({ id: "2", status: "pending", touches_resources: '["file:y.py"]' }),
    ]);
    expect(wp.eligibleCount).toBe(1);
    expect(wp.groupCount).toBe(1);
  });
});

describe("computeWaveProgress — priority ordering + active batch", () => {
  it("higher-priority eligible items color into the earliest batch", () => {
    // Both touch the same file so they can't co-schedule → the urgent one leads.
    const wp = computeWaveProgress([
      item({ id: "low", priority: "low", touches_resources: '["file:a.py"]', added_at: "2026-01-01" }),
      item({ id: "urgent", priority: "urgent", touches_resources: '["file:a.py"]', added_at: "2026-06-01" }),
    ]);
    expect(wp.groupCount).toBe(2);
    expect(wp.batches[0].items[0].id).toBe("urgent");
    expect(wp.batches[1].items[0].id).toBe("low");
  });

  it("activeBatchIndex points at the first batch with a running member", () => {
    const wp = computeWaveProgress([
      item({ id: "a", touches_resources: '["file:a.py"]' }),
      item({ id: "b", status: "in_progress", claimed_at: "2026-07-11 20:00:00", touches_resources: '["file:b.py"]' }),
    ]);
    // 'b' is running (in_progress) so it's not batched; only 'a' forms a batch,
    // and with no running batch member the active index falls to the first
    // non-empty batch.
    expect(wp.activeBatchIndex).toBe(0);
  });

  it("empty board → no batches, sequential, active index -1", () => {
    const wp = computeWaveProgress([]);
    expect(wp.groupCount).toBe(0);
    expect(wp.batches).toEqual([]);
    expect(wp.activeBatchIndex).toBe(-1);
    expect(wp.strategy).toBe("sequential");
  });

  it("never claims token telemetry exists (honest data gap)", () => {
    const wp = computeWaveProgress([item({ id: "1", touches_resources: '["file:a.py"]' })]);
    expect(wp.hasTokenTelemetry).toBe(false);
  });
});

describe("formatElapsedSince", () => {
  const base = Date.parse("2026-07-11T20:00:00");
  it("formats sub-hour as Nm and >=1h as Hh Mm", () => {
    expect(formatElapsedSince("2026-07-11 20:00:00", base + 5 * 60000)).toBe("5m");
    expect(formatElapsedSince("2026-07-11 20:00:00", base + 95 * 60000)).toBe("1h 35m");
  });
  it("missing / invalid input → ''", () => {
    expect(formatElapsedSince(null, base)).toBe("");
    expect(formatElapsedSince("not-a-date", base)).toBe("");
  });
  it("never negative (clamps to 0m)", () => {
    expect(formatElapsedSince("2026-07-11 20:10:00", base)).toBe("0m");
  });
});

describe("buildWaveProgressHtml", () => {
  it("renders one wave row per batch with the parallel tag + running elapsed", () => {
    const nowMs = Date.parse("2026-07-11T20:30:00");
    const wp = computeWaveProgress([
      item({ id: "1", title: "Alpha", status: "in_progress", claimed_at: "2026-07-11 20:00:00", touches_resources: '["file:a.py"]' }),
      item({ id: "2", title: "Beta", touches_resources: '["file:b.py"]' }),
    ]);
    // item 1 is running (in_progress) so it's not offered to a batch; only Beta batches.
    const html = buildWaveProgressHtml(wp, nowMs);
    expect(html).toContain("Wave 1");
    expect(html).toContain("Beta");
    // Honest gap note is always present.
    expect(html.toLowerCase()).toContain("token");
  });

  it("distinguishes 'all blocked' from 'nothing parallelizable' in the empty state", () => {
    const blocked = computeWaveProgress([
      item({ id: "parent", status: "in_progress", claimed_at: "2026-07-11 20:00:00" }),
      item({ id: "child", status: "pending", depends_on: "parent", touches_resources: '["file:y.py"]' }),
    ]);
    expect(buildWaveProgressHtml(blocked)).toContain("blocked on dependencies");

    const nothing = computeWaveProgress([item({ id: "1", status: "done" })]);
    expect(buildWaveProgressHtml(nothing)).toContain("No parallelizable work");
  });

  it("shows the up-to-N× parallel summary when a batch fans out", () => {
    const wp = computeWaveProgress([
      item({ id: "1", touches_resources: '["file:a.py"]' }),
      item({ id: "2", touches_resources: '["file:b.py"]' }),
      item({ id: "3", touches_resources: '["file:c.py"]' }),
    ]);
    expect(wp.maxFanOut).toBe(3);
    expect(buildWaveProgressHtml(wp)).toContain("up to 3× parallel");
  });
});

// ---------------------------------------------------------------------------
// buildWaveSummaryHtml / renderWaveSummary (bbb447ec) — read-only display of
// the durable, authoritative wave_run_summaries record. Distinct from the
// live batch-progress panel above: this renders a POST-HOC, immutable record
// (explicit outcome enum, structured test receipts, correction lineage), not
// a live recomputation from in-flight sprint items.
// ---------------------------------------------------------------------------

const summary = (over: Partial<WaveSummary> = {}): WaveSummary => ({
  wave_id: "wave-5",
  items: [
    { item_id: "a", outcome: "completed" },
    { item_id: "b", outcome: "completed" },
    { item_id: "c", outcome: "blocked" },
  ],
  test_receipts: [
    { command: "pytest -q", exit_code: 0, passed: 42, failed: 0, scope: "targeted" },
  ],
  ...over,
});

describe("buildWaveSummaryHtml", () => {
  it("renders 'no summary' state for null/undefined", () => {
    expect(buildWaveSummaryHtml(null)).toContain("No wave summary recorded");
    expect(buildWaveSummaryHtml(undefined)).toContain("No wave summary recorded");
  });

  it("renders one outcome chip per distinct outcome with correct counts", () => {
    const html = buildWaveSummaryHtml(summary());
    expect(html).toContain("wave-5");
    expect(html).toContain("completed 2");
    expect(html).toContain("blocked 1");
  });

  it("renders a test receipt with pass/fail counts and a pass/fail glyph", () => {
    const html = buildWaveSummaryHtml(summary());
    expect(html).toContain("pytest -q");
    expect(html).toContain("42 passed");
    expect(html).toContain("0 failed");
    expect(html).toContain("✓"); // exit_code 0 → pass glyph

    const failing = buildWaveSummaryHtml(
      summary({ test_receipts: [{ command: "pytest -q", exit_code: 1, passed: 40, failed: 2 }] }),
    );
    expect(failing).toContain("✗");
  });

  it("flags a superseded record and shows the handoff_status when present", () => {
    const html = buildWaveSummaryHtml(
      summary({ superseded_by: "newer-id", handoff_status: "generated" }),
    );
    expect(html).toContain("superseded");
    expect(html).toContain("handoff: generated");
  });

  it("an empty items list renders the 'no items recorded' fallback, not a crash", () => {
    const html = buildWaveSummaryHtml(summary({ items: [] }));
    expect(html).toContain("no items recorded");
  });
});

describe("renderWaveSummary", () => {
  it("is a no-op outside a DOM context (never throws)", () => {
    expect(() => renderWaveSummary("proj-1", "wave-5", summary())).not.toThrow();
  });
});
