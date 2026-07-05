// Unit tests for the strict-typed dashboard-utils module (8e29733e).
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  escapeHtml,
  formatRelativeTime,
  sessionAgeMs,
  isLiveSession,
  _colorForHuman,
  _HUMAN_COLORS,
  getPanelState,
  toast,
  SESSION_LIVE_WINDOW_MS,
  suggestedFsRoots,
} from "./dashboard-utils";

describe("escapeHtml", () => {
  it("escapes the five HTML-significant characters", () => {
    expect(escapeHtml(`<a href="x">'&'</a>`)).toBe(
      "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;",
    );
  });
  it("coerces non-strings", () => {
    expect(escapeHtml(42)).toBe("42");
    expect(escapeHtml(null)).toBe("null");
  });
});

describe("formatRelativeTime", () => {
  it("returns '' for falsy input", () => {
    expect(formatRelativeTime("")).toBe("");
    expect(formatRelativeTime(null)).toBe("");
  });
  it("buckets seconds / minutes / hours / days", () => {
    const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
    expect(formatRelativeTime(ago(5_000))).toMatch(/^\d+s ago$/);
    expect(formatRelativeTime(ago(5 * 60_000))).toMatch(/^\d+m ago$/);
    expect(formatRelativeTime(ago(5 * 3_600_000))).toMatch(/^\d+h ago$/);
    expect(formatRelativeTime(ago(3 * 86_400_000))).toBe("3d ago");
  });
  it("accepts space-separated SQL timestamps", () => {
    expect(formatRelativeTime("2020-01-01 00:00:00")).toMatch(/d ago$/);
  });
});

describe("sessionAgeMs / isLiveSession", () => {
  it("returns Infinity for missing last_seen", () => {
    expect(sessionAgeMs(null)).toBe(Number.POSITIVE_INFINITY);
    expect(sessionAgeMs({})).toBe(Number.POSITIVE_INFINITY);
  });
  it("computes a small positive age for a recent session", () => {
    const age = sessionAgeMs({ last_seen: new Date(Date.now() - 1000).toISOString() });
    expect(age).toBeGreaterThanOrEqual(0);
    expect(age).toBeLessThan(10_000);
  });
  it("is live only when active and within the window", () => {
    const recent = new Date(Date.now() - 1000).toISOString();
    expect(isLiveSession({ status: "active", last_seen: recent })).toBe(true);
    expect(isLiveSession({ status: "idle", last_seen: recent })).toBe(false);
    expect(isLiveSession({ status: "active", last_seen: "2000-01-01 00:00:00" })).toBe(false);
    expect(isLiveSession(null)).toBe(false);
  });
  it("honours an explicit ageMs override", () => {
    expect(isLiveSession({ status: "active" }, 0)).toBe(true);
    expect(isLiveSession({ status: "active" }, SESSION_LIVE_WINDOW_MS + 1)).toBe(false);
  });
});

// 59ab2f9f — Recent Runs "live" fallback consistency. loadRecentRuns' dur
// fallback previously showed "live" forever on any run with duration_s === null
// and raw status === 'running', ignoring the session's status and any recency
// window. The fix reuses isLiveSession on the run's SessionLike projection
// ({ status: run.session_status, last_seen: run.started_at }) so it matches the
// displayRunStatus cross-check. These tests pin that projection's behaviour.
describe("Recent Runs live fallback (59ab2f9f)", () => {
  // Mirror the exact mapping the dur fallback applies to a run row.
  const runIsLive = (run: { session_status?: string | null; started_at?: string | null }) =>
    isLiveSession({ status: run.session_status, last_seen: run.started_at });

  it("is NOT live for a stale run (duration null, started long ago) even when raw status is running", () => {
    // Session went inactive and the run started well outside the live window.
    const stale = { status: "running", session_status: "idle", started_at: "2000-01-01 00:00:00", duration_s: null };
    expect(runIsLive(stale)).toBe(false);
  });

  it("is NOT live when the session is still active but the run is outside the recency window", () => {
    const old = new Date(Date.now() - (SESSION_LIVE_WINDOW_MS + 60_000)).toISOString();
    expect(runIsLive({ session_status: "active", started_at: old })).toBe(false);
  });

  it("is live for a fresh run whose session is still active", () => {
    const recent = new Date(Date.now() - 1_000).toISOString();
    expect(runIsLive({ session_status: "active", started_at: recent })).toBe(true);
  });

  it("is NOT live when the session status is missing", () => {
    const recent = new Date(Date.now() - 1_000).toISOString();
    expect(runIsLive({ session_status: null, started_at: recent })).toBe(false);
  });
});

describe("_colorForHuman", () => {
  it("returns a palette color, stable per id", () => {
    const c = _colorForHuman("alice");
    expect(_HUMAN_COLORS).toContain(c);
    expect(_colorForHuman("alice")).toBe(c);
  });
  it("handles empty id without throwing", () => {
    expect(_HUMAN_COLORS).toContain(_colorForHuman(""));
  });
});

describe("toast (8e29733e null-safety fix)", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });
  it("no-ops instead of throwing when #toast is absent", () => {
    expect(() => toast("hi")).not.toThrow();
  });
  it("writes the message and toggles classes when #toast exists", () => {
    const el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
    toast("saved", false);
    expect(el.textContent).toBe("saved");
    expect(el.classList.contains("show")).toBe(true);
    expect(el.classList.contains("error")).toBe(false);
    toast("boom", true);
    expect(el.classList.contains("error")).toBe(true);
  });
});

describe("getPanelState", () => {
  beforeEach(() => {
    (window as any).state = { panels: {} };
  });
  it("lazily creates and returns a per-project panel bag", () => {
    const a = getPanelState("p1");
    a.open = true;
    expect(getPanelState("p1").open).toBe(true);
    expect(getPanelState("p2")).toEqual({});
  });
});

describe("suggestedFsRoots", () => {
  it("suggests repo_paths[].cwd not already a root", () => {
    const cfg = { repo_paths: [{ cwd: "/a/repo", hostname: "h" }, { cwd: "/b/repo" }] };
    expect(suggestedFsRoots(cfg, ["/b/repo"])).toEqual(["/a/repo"]);
  });
  it("includes the legacy single repo_path", () => {
    expect(suggestedFsRoots({ repo_path: "/solo/repo" }, [])).toEqual(["/solo/repo"]);
  });
  it("trims, dedupes, and drops blanks and existing roots", () => {
    const cfg = { repo_paths: [{ cwd: " /a " }, { cwd: "/a" }, { cwd: "  " }], repo_path: "/a" };
    expect(suggestedFsRoots(cfg, [])).toEqual(["/a"]);
    expect(suggestedFsRoots(cfg, ["/a"])).toEqual([]);
  });
  it("is null-safe on missing/garbage config", () => {
    expect(suggestedFsRoots(null, null)).toEqual([]);
    expect(suggestedFsRoots("nope", ["/x"])).toEqual([]);
    expect(suggestedFsRoots({ repo_paths: "nope" }, [])).toEqual([]);
  });
});
