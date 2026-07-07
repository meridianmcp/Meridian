// Unit tests for the vtab grouping IA (2d3b8424).
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  VTAB_GROUPS,
  ALL_GROUPED_TABS,
  groupForTab,
  wireVtabGroups,
} from "./dashboard-tabgroups";

// The full flat tab set that existed BEFORE grouping — this is the contract:
// grouping must not drop, rename, or duplicate any of these.
const ORIGINAL_FLAT_TABS = [
  "status",
  "live",
  "goal",
  "files",
  "devlog",
  "timeline",
  "rewind",
  "queue",
  "team",
  "notes",
  "hitl",
  "docs",
  "settings",
  "codeintel",
  "documents",
  "insights",
  "blog",
  "sessions",
];

// The intended IA — pinned so an accidental reshuffle is caught.
const EXPECTED_MEMBERSHIP: Record<string, string[]> = {
  overview: ["status", "live"],
  planning: ["goal", "insights", "blog"],
  work: ["queue", "hitl", "team", "sessions"],
  content: ["files", "notes", "devlog", "documents", "docs", "codeintel"],
  history: ["timeline", "rewind", "settings"],
};

describe("VTAB_GROUPS structure", () => {
  it("groups the flat tabs into a small number of logical groups", () => {
    // ~4-5 groups replacing ~18 flat tabs.
    expect(VTAB_GROUPS.length).toBeGreaterThanOrEqual(4);
    expect(VTAB_GROUPS.length).toBeLessThanOrEqual(6);
  });

  it("every group has a unique id, a label, and at least one tab", () => {
    const ids = new Set<string>();
    for (const g of VTAB_GROUPS) {
      expect(g.id).toBeTruthy();
      expect(g.label).toBeTruthy();
      expect(g.tabs.length).toBeGreaterThan(0);
      expect(ids.has(g.id)).toBe(false);
      ids.add(g.id);
    }
  });

  it("assigns every original flat tab to exactly one group (no drop, no dupe)", () => {
    // Coverage: the union of grouped tabs equals the original flat set.
    expect([...ALL_GROUPED_TABS].sort()).toEqual([...ORIGINAL_FLAT_TABS].sort());
    // Exactly-one: no tab appears in two groups.
    expect(ALL_GROUPED_TABS.length).toBe(ORIGINAL_FLAT_TABS.length);
    expect(new Set(ALL_GROUPED_TABS).size).toBe(ORIGINAL_FLAT_TABS.length);
  });

  it("keeps 'status' as the first tab of the first group (default active)", () => {
    expect(VTAB_GROUPS[0].tabs[0]).toBe("status");
  });

  it("each group contains exactly its expected tabs, in order", () => {
    const actual: Record<string, string[]> = {};
    for (const g of VTAB_GROUPS) actual[g.id] = [...g.tabs];
    expect(actual).toEqual(EXPECTED_MEMBERSHIP);
  });
});

describe("groupForTab", () => {
  it("resolves every original tab to its group", () => {
    for (const [gid, tabs] of Object.entries(EXPECTED_MEMBERSHIP)) {
      for (const tab of tabs) expect(groupForTab(tab)).toBe(gid);
    }
  });

  it("returns null for unknown/empty tabs", () => {
    expect(groupForTab("does-not-exist")).toBeNull();
    expect(groupForTab("")).toBeNull();
    expect(groupForTab(null)).toBeNull();
    expect(groupForTab(undefined)).toBeNull();
  });
});

// Guard against DOM<->model drift: the inline rail markup in dashboard.ts must
// place each tab's button inside its group's <div data-vgroup="…"> block, and
// every original tab must still render as a .vtab-btn somewhere in the strip.
describe("inline rail markup matches the model (dashboard.ts)", () => {
  // vitest runs from the repo root; dashboard.ts sits next to this test file.
  const src = readFileSync(path.resolve("meridian/static/dashboard.ts"), "utf-8");

  // Slice the vtab-strip block out of the buildTabBody template.
  const stripStart = src.indexOf('<div class="vtab-strip"');
  const stripEnd = src.indexOf('<div class="vtab-drawer', stripStart);
  const strip = src.slice(stripStart, stripEnd);

  it("renders every original tab as a .vtab-btn in the strip", () => {
    for (const tab of ORIGINAL_FLAT_TABS) {
      expect(strip).toContain(`data-vtab="${tab}"`);
    }
  });

  it("renders exactly one group container per model group", () => {
    for (const g of VTAB_GROUPS) {
      expect(strip).toContain(`data-vgroup="${g.id}"`);
      expect(strip).toContain(`data-vgroup-toggle="${g.id}"`);
    }
    const groupCount = (strip.match(/data-vgroup="[a-z]+"/g) || []).length;
    expect(groupCount).toBe(VTAB_GROUPS.length);
  });

  it("nests each tab's button under its assigned group block, in order", () => {
    // Split the strip on group boundaries; each slice is one group's markup.
    for (const g of VTAB_GROUPS) {
      const gStart = strip.indexOf(`data-vgroup="${g.id}"`);
      expect(gStart).toBeGreaterThanOrEqual(0);
      // The group's markup runs until the next group's data-vgroup (or end).
      const nextStarts = VTAB_GROUPS.map((o) => strip.indexOf(`data-vgroup="${o.id}"`))
        .filter((i) => i > gStart)
        .sort((a, b) => a - b);
      const gEnd = nextStarts.length ? nextStarts[0] : strip.length;
      const block = strip.slice(gStart, gEnd);
      // Every expected tab appears in this block, in the declared order.
      let cursor = 0;
      for (const tab of g.tabs) {
        const at = block.indexOf(`data-vtab="${tab}"`, cursor);
        expect(at, `tab ${tab} missing/out-of-order in group ${g.id}`).toBeGreaterThanOrEqual(0);
        cursor = at + 1;
      }
    }
  });

  it("marks only the status button active on first paint", () => {
    const actives = strip.match(/class="vtab-btn active"/g) || [];
    expect(actives.length).toBe(1);
    expect(strip).toContain('class="vtab-btn active" data-vtab="status"');
  });
});

// Build a jsdom rail that mirrors the inline structure to exercise the wiring.
function buildStrip(): HTMLElement {
  const strip = document.createElement("div");
  strip.className = "vtab-strip";
  strip.innerHTML = VTAB_GROUPS.map(
    (g) =>
      `<div class="vtab-group" data-vgroup="${g.id}">` +
      `<button class="vtab-group-header" data-vgroup-toggle="${g.id}" aria-expanded="true"></button>` +
      `<div class="vtab-group-tabs" style="display:flex">` +
      g.tabs.map((t) => `<button class="vtab-btn" data-vtab="${t}"></button>`).join("") +
      `</div></div>`,
  ).join("");
  return strip;
}

describe("wireVtabGroups behaviour (jsdom)", () => {
  let strip: HTMLElement;

  beforeEach(() => {
    strip = buildStrip();
    document.body.appendChild(strip);
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  const tabsEl = (groupId: string) =>
    strip.querySelector<HTMLElement>(`.vtab-group[data-vgroup="${groupId}"] .vtab-group-tabs`)!;

  it("leaves every group expanded on wire-up", () => {
    wireVtabGroups(strip);
    for (const g of VTAB_GROUPS) {
      const groupEl = strip.querySelector(`.vtab-group[data-vgroup="${g.id}"]`)!;
      expect(groupEl.classList.contains("collapsed")).toBe(false);
    }
  });

  it("toggling a group header collapses then re-expands its tabs", () => {
    wireVtabGroups(strip);
    const header = strip.querySelector<HTMLElement>(
      '.vtab-group[data-vgroup="planning"] .vtab-group-header',
    )!;
    const groupEl = strip.querySelector('.vtab-group[data-vgroup="planning"]')!;

    header.click(); // collapse
    expect(groupEl.classList.contains("collapsed")).toBe(true);
    expect(tabsEl("planning").style.display).toBe("none");
    expect(header.getAttribute("aria-expanded")).toBe("false");

    header.click(); // expand
    expect(groupEl.classList.contains("collapsed")).toBe(false);
    expect(tabsEl("planning").style.display).toBe("flex");
    expect(header.getAttribute("aria-expanded")).toBe("true");
  });

  it("revealGroupForTab re-expands a collapsed group so its tab is reachable", () => {
    const { revealGroupForTab } = wireVtabGroups(strip);
    const header = strip.querySelector<HTMLElement>(
      '.vtab-group[data-vgroup="content"] .vtab-group-header',
    )!;
    const groupEl = strip.querySelector('.vtab-group[data-vgroup="content"]')!;

    header.click(); // user collapses Content
    expect(groupEl.classList.contains("collapsed")).toBe(true);

    // 'documents' lives in Content — revealing it must re-expand the group.
    revealGroupForTab("documents");
    expect(groupEl.classList.contains("collapsed")).toBe(false);
    expect(tabsEl("content").style.display).toBe("flex");
  });

  it("revealGroupForTab is a no-op for unknown tabs", () => {
    const { revealGroupForTab } = wireVtabGroups(strip);
    expect(() => revealGroupForTab("nope")).not.toThrow();
    expect(() => revealGroupForTab(null)).not.toThrow();
  });
});
