// Unit tests for the vtab grouping IA (2d3b8424).
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  VTAB_GROUPS,
  VTAB_GROUPS_COLLAPSED_BY_DEFAULT,
  ALL_GROUPED_TABS,
  groupForTab,
  revealGroupInStrip,
  wireVtabGroups,
} from "./dashboard-tabgroups";

// The full flat tab set the rail renders — this is the contract: grouping must
// not drop, rename, or duplicate any of these. 'experiments' joined the rail
// after the grouping first shipped (90952bad: it was missing from VTAB_GROUPS, so
// revealGroupForTab could not expand its group).
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
  "experiments",
];

// The intended IA — pinned so an accidental reshuffle is caught.
const EXPECTED_MEMBERSHIP: Record<string, string[]> = {
  overview: ["status", "live"],
  planning: ["goal", "insights", "blog"],
  work: ["queue", "experiments", "hitl", "team", "sessions"],
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
// `active` marks that tab's button like the first paint does (status).
function buildStrip(active: string | null = "status"): HTMLElement {
  const strip = document.createElement("div");
  strip.className = "vtab-strip";
  strip.innerHTML = VTAB_GROUPS.map(
    (g) =>
      `<div class="vtab-group" data-vgroup="${g.id}">` +
      `<button class="vtab-group-header" data-vgroup-toggle="${g.id}" aria-expanded="true"></button>` +
      `<div class="vtab-group-tabs" style="display:flex">` +
      g.tabs
        .map((t) => `<button class="vtab-btn${t === active ? " active" : ""}" data-vtab="${t}"></button>`)
        .join("") +
      `</div></div>`,
  ).join("");
  return strip;
}

const groupEl = (strip: HTMLElement, id: string) =>
  strip.querySelector<HTMLElement>(`.vtab-group[data-vgroup="${id}"]`)!;
const tabsEl = (strip: HTMLElement, id: string) =>
  strip.querySelector<HTMLElement>(`.vtab-group[data-vgroup="${id}"] .vtab-group-tabs`)!;
const headerEl = (strip: HTMLElement, id: string) =>
  strip.querySelector<HTMLElement>(`.vtab-group[data-vgroup="${id}"] .vtab-group-header`)!;
const expandedIds = (strip: HTMLElement) =>
  VTAB_GROUPS.filter((g) => !groupEl(strip, g.id).classList.contains("collapsed")).map((g) => g.id);

describe("wireVtabGroups behaviour (jsdom)", () => {
  let strip: HTMLElement;

  beforeEach(() => {
    strip = buildStrip();
    document.body.appendChild(strip);
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("starts every group collapsed EXCEPT the one holding the active tab (90952bad)", () => {
    expect(VTAB_GROUPS_COLLAPSED_BY_DEFAULT).toBe(true);
    wireVtabGroups(strip);
    expect(expandedIds(strip)).toEqual(["overview"]); // status is active
    for (const g of VTAB_GROUPS) {
      const open = g.id === "overview";
      expect(tabsEl(strip, g.id).style.display).toBe(open ? "flex" : "none");
      expect(headerEl(strip, g.id).getAttribute("aria-expanded")).toBe(String(open));
    }
  });

  it("opens the group of whichever tab is active at wire-up", () => {
    strip = buildStrip("hitl");
    wireVtabGroups(strip);
    expect(expandedIds(strip)).toEqual(["work"]);
  });

  it("collapses everything when no tab is marked active", () => {
    strip = buildStrip(null);
    wireVtabGroups(strip);
    expect(expandedIds(strip)).toEqual([]);
  });

  it("collapseByDefault:false keeps the old all-expanded rail", () => {
    wireVtabGroups(strip, { collapseByDefault: false });
    expect(expandedIds(strip)).toEqual(VTAB_GROUPS.map((g) => g.id));
    expect(strip.hasAttribute("data-vaccordion")).toBe(false);
    // ...and navigation does not collapse anything in that mode.
    revealGroupInStrip(strip, "settings");
    expect(expandedIds(strip)).toEqual(VTAB_GROUPS.map((g) => g.id));
  });

  it("toggling a group header expands then re-collapses its tabs", () => {
    wireVtabGroups(strip);
    const header = headerEl(strip, "planning");
    const group = groupEl(strip, "planning");

    header.click(); // expand (it starts collapsed)
    expect(group.classList.contains("collapsed")).toBe(false);
    expect(tabsEl(strip, "planning").style.display).toBe("flex");
    expect(header.getAttribute("aria-expanded")).toBe("true");

    header.click(); // collapse
    expect(group.classList.contains("collapsed")).toBe(true);
    expect(tabsEl(strip, "planning").style.display).toBe("none");
    expect(header.getAttribute("aria-expanded")).toBe("false");
  });

  it("revealGroupForTab expands a collapsed group so its tab is reachable", () => {
    const { revealGroupForTab } = wireVtabGroups(strip);
    const group = groupEl(strip, "content");
    expect(group.classList.contains("collapsed")).toBe(true);

    // 'documents' lives in Content — revealing it must expand the group.
    revealGroupForTab("documents");
    expect(group.classList.contains("collapsed")).toBe(false);
    expect(tabsEl(strip, "content").style.display).toBe("flex");
  });

  it("revealing another group swaps the open group (accordion): the rail does not re-expand over a session", () => {
    const { revealGroupForTab } = wireVtabGroups(strip);
    revealGroupForTab("goal");
    expect(expandedIds(strip)).toEqual(["planning"]);
    revealGroupForTab("rewind");
    expect(expandedIds(strip)).toEqual(["history"]);
  });

  it("never collapses a group the user opened by hand", () => {
    const { revealGroupForTab } = wireVtabGroups(strip);
    headerEl(strip, "content").click(); // user opens Content
    revealGroupForTab("goal");
    expect(expandedIds(strip)).toEqual(["planning", "content"]);
    headerEl(strip, "content").click(); // user closes it again: back under accordion control
    revealGroupForTab("rewind");
    expect(expandedIds(strip)).toEqual(["history"]);
  });

  it("revealGroupForTab is a no-op for unknown tabs", () => {
    const { revealGroupForTab } = wireVtabGroups(strip);
    expect(() => revealGroupForTab("nope")).not.toThrow();
    expect(() => revealGroupForTab(null)).not.toThrow();
    expect(expandedIds(strip)).toEqual(["overview"]);
  });

  it("revealGroupInStrip works on a plain strip (the waffle's entry point) and ignores a missing group", () => {
    revealGroupInStrip(strip, "settings"); // not wired: no accordion, just expands
    expect(groupEl(strip, "history").classList.contains("collapsed")).toBe(false);
    expect(() => revealGroupInStrip(document.createElement("div"), "goal")).not.toThrow();
  });
});

// The same wiring over the REAL inline rail markup from dashboard.ts: this is
// what proves the collapsed default leaves every programmatic navigation path
// (tour, deep links, HITL/timeline jumps, the waffle) able to land on each tab.
describe("collapsed-by-default against the real rail markup", () => {
  const src = readFileSync(path.resolve("meridian/static/dashboard.ts"), "utf-8");
  const start = src.indexOf('<div class="vtab-strip"');
  const end = src.indexOf('<div class="vtab-drawer', start);
  const markup = src
    .slice(start, end)
    .replace(/\$\{project\.id\}/g, "p1")
    .replace(/\$\{\(window\.MERIDIAN_HOSTED[^}]*\}/, '<button class="vtab-btn" data-vtab="files" title="Files"></button>');

  let strip: HTMLElement;
  let reveal: (tab: string) => void;
  const landed: string[] = [];
  const openGroups = () =>
    Array.from(strip.querySelectorAll<HTMLElement>(".vtab-group"))
      .filter((g) => !g.classList.contains("collapsed"))
      .map((g) => g.dataset.vgroup);

  beforeEach(() => {
    landed.length = 0;
    document.body.innerHTML = markup;
    strip = document.getElementById("vtab-strip-p1")!;
    ({ revealGroupForTab: reveal } = wireVtabGroups(strip));
    // Same shape as the production onclick: reveal first, then switch the active tab.
    strip.querySelectorAll<HTMLElement>(".vtab-btn").forEach((btn) => {
      btn.onclick = () => {
        reveal(btn.dataset.vtab as string);
        strip.querySelectorAll(".vtab-btn").forEach((b) => b.classList.toggle("active", b === btn));
        landed.push(btn.dataset.vtab as string);
      };
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("first paint: only the Overview group (holding the active Status tab) is laid out", () => {
    expect(openGroups()).toEqual(["overview"]);
    const hidden = Array.from(strip.querySelectorAll<HTMLElement>(".vtab-group-tabs")).filter(
      (t) => t.style.display === "none",
    );
    expect(hidden.length).toBe(VTAB_GROUPS.length - 1);
  });

  it("a programmatic .click() on a button inside a collapsed group still navigates and lays its group out", () => {
    for (const tab of ALL_GROUPED_TABS) {
      const btn = strip.querySelector<HTMLElement>(`.vtab-btn[data-vtab="${tab}"]`)!;
      btn.click();
      expect(btn.classList.contains("active"), `${tab} not active after click`).toBe(true);
      const tabs = btn.closest<HTMLElement>(".vtab-group-tabs")!;
      expect(tabs.style.display, `${tab}'s group left hidden after navigating to it`).toBe("flex");
      expect(strip.querySelectorAll(".vtab-btn.active").length).toBe(1);
    }
    expect(landed).toEqual([...ALL_GROUPED_TABS]);
  });

  it("revealGroupForTab ahead of the click (tour / deep-link order) lays the target out before it is measured", () => {
    for (const tab of ALL_GROUPED_TABS) {
      reveal(tab);
      const btn = strip.querySelector<HTMLElement>(`.vtab-btn[data-vtab="${tab}"]`)!;
      expect(btn.closest<HTMLElement>(".vtab-group-tabs")!.style.display, tab).toBe("flex");
    }
  });

  it("restoring a saved tab (reveal then click) opens exactly that tab's group", () => {
    reveal("notes");
    strip.querySelector<HTMLElement>('.vtab-btn[data-vtab="notes"]')!.click();
    expect(openGroups()).toEqual(["content"]);
  });
});
