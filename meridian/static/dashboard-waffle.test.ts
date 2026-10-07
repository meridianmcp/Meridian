// Unit + DOM tests for the waffle launcher (90952bad).
//
// Three layers:
//   1. the PURE ordering/filter/navigation/placement model (no DOM);
//   2. the launcher in jsdom, mounted over the REAL rail markup sliced out of
//      dashboard.ts (so a drift between the inline buttons and the waffle shows up
//      here), wired like production (click -> revealGroupForTab -> active);
//   3. a11y-oriented checks: roles, accessible names, roving tabindex, focus.
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ALL_GROUPED_TABS, VTAB_GROUPS, wireVtabGroups } from "./dashboard-tabgroups";
import {
  WAFFLE_COLUMNS,
  WAFFLE_DEFAULT_PINS,
  WAFFLE_DEFAULT_RANK,
  WAFFLE_RECENT_MAX,
  WAFFLE_SUBTABS,
  WAFFLE_TABS,
  WAFFLE_USAGE_THRESHOLD,
  activateWaffleTab,
  buildWaffleModel,
  computePopoverPlacement,
  countActiveSprintItems,
  emptyState,
  loadState,
  mountWaffle,
  moveFocus,
  orderTabs,
  parseState,
  readRailTabs,
  readWaffleBadges,
  recentTabs,
  recordUse,
  refreshWaffle,
  saveState,
  togglePin,
  waffleIconSvg,
  type WaffleController,
  type WaffleModel,
  type WaffleState,
} from "./dashboard-waffle";

const ALL_IDS = [...WAFFLE_DEFAULT_RANK];

function tabKeys(model: WaffleModel, sectionId: string): string[] {
  const section = model.sections.find((s) => s.id === sectionId);
  return section
    ? section.rows.filter((r) => r.kind === "tiles").flatMap((r) => r.entries.map((e) => e.tab))
    : [];
}

function state(partial: Partial<WaffleState> = {}): WaffleState {
  return { ...emptyState(), ...partial };
}

// ---------------------------------------------------------------------------
// 1. Pure model
// ---------------------------------------------------------------------------

describe("default rank and metadata", () => {
  it("ranks every tab the rail renders exactly once (no drift from VTAB_GROUPS)", () => {
    expect([...WAFFLE_DEFAULT_RANK].sort()).toEqual([...ALL_GROUPED_TABS].sort());
    expect(new Set(WAFFLE_DEFAULT_RANK).size).toBe(WAFFLE_DEFAULT_RANK.length);
    expect(WAFFLE_TABS.map((t) => t.id).sort()).toEqual([...ALL_GROUPED_TABS].sort());
  });

  it("keeps the owner's order: goal, notes, insights, queue, live, hitl, status ... settings last", () => {
    expect(WAFFLE_DEFAULT_RANK.slice(0, 7)).toEqual(["goal", "notes", "insights", "queue", "live", "hitl", "status"]);
    expect(WAFFLE_DEFAULT_RANK[WAFFLE_DEFAULT_RANK.length - 1]).toBe("settings");
    // 'Active sessions' (status) is deliberately not first any more.
    expect(WAFFLE_DEFAULT_RANK.indexOf("status")).toBeGreaterThan(WAFFLE_DEFAULT_RANK.indexOf("hitl"));
  });

  it("pins the owner's top picks by default", () => {
    expect([...WAFFLE_DEFAULT_PINS]).toEqual(["goal", "notes", "insights"]);
  });

  it("offers the Goal sub-tabs as sub-entries and they match the Goal tab's own data-gtab buttons", () => {
    const src = readFileSync(path.resolve("meridian/static/dashboard.ts"), "utf-8");
    const gtabs = [...src.matchAll(/class="goal-subtab-btn[^"]*" data-gtab="([a-z-]+)"/g)].map((m) => m[1]);
    expect(WAFFLE_SUBTABS.map((s) => s.id)).toEqual(gtabs);
    expect(WAFFLE_SUBTABS.every((s) => s.parent === "goal")).toBe(true);
  });

  it("has an icon drawing for every tab and every icon is inline SVG, not a glyph", () => {
    for (const t of WAFFLE_TABS) {
      const svg = waffleIconSvg(t.icon);
      expect(svg.startsWith("<svg")).toBe(true);
      expect(svg).toContain('aria-hidden="true"');
      expect(svg).toContain("currentColor");
      // No text node: a Unicode glyph would show up as characters outside the tags.
      expect(svg.replace(/<[^>]*>/g, "")).toBe("");
    }
  });
});

describe("orderTabs", () => {
  it("pinned first (pin order), then the rest by default rank, below the usage threshold", () => {
    const { pinned, rest } = orderTabs(ALL_IDS, emptyState());
    expect(pinned).toEqual(["goal", "notes", "insights"]);
    expect(rest).toEqual(WAFFLE_DEFAULT_RANK.filter((id) => !pinned.includes(id)));
  });

  it("is independent of the order the rail lists its buttons in", () => {
    const shuffled = [...ALL_IDS].reverse();
    expect(orderTabs(shuffled, emptyState())).toEqual(orderTabs(ALL_IDS, emptyState()));
  });

  it("drops tabs the rail does not currently render (files hidden, codeintel off)", () => {
    const avail = ALL_IDS.filter((id) => id !== "files" && id !== "codeintel");
    const { pinned, rest } = orderTabs(avail, emptyState());
    expect([...pinned, ...rest]).not.toContain("files");
    expect([...pinned, ...rest]).not.toContain("codeintel");
    expect(pinned.length + rest.length).toBe(ALL_IDS.length - 2);
  });

  it("appends a tab it has never heard of after every ranked tab, in rail order", () => {
    const { rest } = orderTabs([...ALL_IDS, "zeta", "alpha"], emptyState());
    expect(rest.slice(-2)).toEqual(["zeta", "alpha"]);
  });

  it("ignores pins for tabs that are not available", () => {
    const { pinned } = orderTabs(["goal", "queue"], state({ pins: ["experiments", "queue", "goal"] }));
    expect(pinned).toEqual(["queue", "goal"]);
  });

  it("does not re-sort by usage before the threshold", () => {
    const usage = { sessions: WAFFLE_USAGE_THRESHOLD - 1 };
    const { rest } = orderTabs(ALL_IDS, state({ usage }));
    expect(rest).toEqual(orderTabs(ALL_IDS, emptyState()).rest);
  });

  it("re-sorts the middle by usage at the threshold; least used sink to the bottom", () => {
    const usage = { sessions: 12, settings: 8, timeline: 5 };
    expect(Object.values(usage).reduce((a, b) => a + b, 0)).toBeGreaterThanOrEqual(WAFFLE_USAGE_THRESHOLD);
    const { pinned, rest } = orderTabs(ALL_IDS, state({ usage }));
    expect(pinned).toEqual(["goal", "notes", "insights"]); // owner's picks never move
    expect(rest.slice(0, 3)).toEqual(["sessions", "settings", "timeline"]);
    // Never-used tabs keep their default relative order, after the used ones.
    const unused = rest.slice(3);
    expect(unused).toEqual(WAFFLE_DEFAULT_RANK.filter((id) => !pinned.includes(id) && !(id in usage)));
  });

  it("breaks usage ties with the default rank", () => {
    const { rest } = orderTabs(ALL_IDS, state({ usage: { settings: 13, blog: 13 } }));
    expect(rest.slice(0, 2)).toEqual(["blog", "settings"]); // blog outranks settings by default
  });

  it("keeps pinned tabs out of the usage sort even when heavily used", () => {
    const { pinned } = orderTabs(ALL_IDS, state({ usage: { goal: 1, settings: 99 } }));
    expect(pinned).toEqual(["goal", "notes", "insights"]);
  });

  it("honours a custom rank and threshold", () => {
    const { rest } = orderTabs(["a", "b", "c"], state({ pins: [] }), { rank: ["c", "b", "a"] });
    expect(rest).toEqual(["c", "b", "a"]);
    const adaptive = orderTabs(["a", "b", "c"], state({ pins: [], usage: { a: 2 } }), { rank: ["c", "b", "a"], threshold: 2 });
    expect(adaptive.rest).toEqual(["a", "c", "b"]);
  });
});

describe("pins, usage and recents", () => {
  it("unpinning a default sticks and is stored as the user's own list", () => {
    const s = togglePin(emptyState(), "goal");
    expect(s.pins).toEqual(["notes", "insights"]);
    expect(orderTabs(ALL_IDS, s).pinned).toEqual(["notes", "insights"]);
    expect(orderTabs(ALL_IDS, s).rest[0]).toBe("goal"); // back in the ranked list, first
  });

  it("pinning appends and a second toggle unpins; an empty list stays empty (not the defaults)", () => {
    let s = togglePin(emptyState(), "queue");
    expect(s.pins).toEqual(["goal", "notes", "insights", "queue"]);
    s = togglePin(s, "queue");
    expect(s.pins).toEqual(["goal", "notes", "insights"]);
    s = ["goal", "notes", "insights"].reduce((acc, id) => togglePin(acc, id), s);
    expect(s.pins).toEqual([]);
    expect(orderTabs(ALL_IDS, s).pinned).toEqual([]);
  });

  it("recordUse counts, moves to the front of recents and never mutates its input", () => {
    const s0 = emptyState();
    const s1 = recordUse(s0, "queue");
    const s2 = recordUse(recordUse(s1, "live"), "queue");
    expect(s0.usage).toEqual({});
    expect(s2.usage).toEqual({ queue: 2, live: 1 });
    expect(s2.recent).toEqual(["queue", "live"]);
  });

  it("recentTabs excludes pinned and unavailable tabs and caps the length", () => {
    const s = state({ recent: ["goal", "live", "queue", "files", "team", "status"] });
    const out = recentTabs(s, ["goal", "live", "queue", "team", "status"], ["goal"]);
    expect(out).toEqual(["live", "queue", "team"]);
    expect(out.length).toBeLessThanOrEqual(WAFFLE_RECENT_MAX);
  });

  it("parseState survives garbage and clamps what it keeps", () => {
    expect(parseState(null)).toEqual(emptyState());
    expect(parseState("nope")).toEqual(emptyState());
    expect(parseState([1, 2])).toEqual(emptyState());
    const s = parseState({
      pins: ["goal", "goal", 7, "x".repeat(80), "has space"],
      usage: { queue: "5", bad: -2, nan: "x", ["y".repeat(80)]: 3, live: 2.9 },
      recent: ["queue", "queue", null, "live"],
    });
    expect(s.pins).toEqual(["goal"]);
    expect(s.usage).toEqual({ queue: 5, live: 2 });
    expect(s.recent).toEqual(["queue", "live"]);
  });

  it("loadState/saveState never throw when storage is missing, blocked or corrupt", () => {
    expect(loadState(null, "k")).toEqual(emptyState());
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("quota");
      },
    } as unknown as Storage;
    expect(loadState(throwing, "k")).toEqual(emptyState());
    expect(() => saveState(throwing, "k", emptyState())).not.toThrow();
    expect(() => saveState(null, "k", emptyState())).not.toThrow();
    const corrupt = { getItem: () => "{not json", setItem: () => undefined } as unknown as Storage;
    expect(loadState(corrupt, "k")).toEqual(emptyState());
  });

  it("round-trips through storage", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    } as unknown as Storage;
    const s = recordUse(togglePin(emptyState(), "queue"), "live");
    saveState(storage, "k", s);
    expect(loadState(storage, "k")).toEqual(s);
  });
});

describe("buildWaffleModel", () => {
  it("default model: Pinned (goal, notes, insights) then All tabs in rank order, no Recent", () => {
    const m = buildWaffleModel({ available: ALL_IDS, state: emptyState() });
    expect(m.sections.map((s) => s.id)).toEqual(["pinned", "all"]);
    expect(tabKeys(m, "pinned")).toEqual(["goal", "notes", "insights"]);
    expect(tabKeys(m, "all")).toEqual(WAFFLE_DEFAULT_RANK.filter((id) => !WAFFLE_DEFAULT_PINS.includes(id)));
  });

  it("never lists a tab twice across sections", () => {
    const s = recordUse(recordUse(emptyState(), "live"), "queue");
    const m = buildWaffleModel({ available: ALL_IDS, state: s });
    const seen = m.sections.flatMap((sec) => tabKeys(m, sec.id));
    expect(new Set(seen).size).toBe(seen.length);
    expect(seen.length).toBe(ALL_IDS.length);
    expect(m.sections.map((x) => x.id)).toEqual(["pinned", "recent", "all"]);
    expect(tabKeys(m, "recent")).toEqual(["queue", "live"]);
  });

  it("lays tiles out three to a row", () => {
    const m = buildWaffleModel({ available: ALL_IDS, state: emptyState() });
    for (const row of m.sections.flatMap((s) => s.rows).filter((r) => r.kind === "tiles")) {
      expect(row.entries.length).toBeLessThanOrEqual(WAFFLE_COLUMNS);
    }
  });

  it("puts Goal's sub-entries in rows directly under the row holding Goal", () => {
    const m = buildWaffleModel({ available: ALL_IDS, state: emptyState() });
    const pinned = m.sections[0];
    expect(pinned.rows[0].kind).toBe("tiles");
    expect(pinned.rows[0].entries.map((e) => e.tab)).toEqual(["goal", "notes", "insights"]);
    const subs = pinned.rows.filter((r) => r.kind === "subs");
    expect(subs.length).toBe(2); // 4 sub-entries, 3 per row
    expect(subs.flatMap((r) => r.entries.map((e) => e.sub))).toEqual(WAFFLE_SUBTABS.map((s) => s.id));
    expect(subs.every((r) => r.parent === "goal" && r.parentLabel === "Goal")).toBe(true);
    expect(pinned.rows[1].kind).toBe("subs");
  });

  it("shows the sub-entries in All tabs when Goal is unpinned, and none when Goal is absent", () => {
    const unpinned = buildWaffleModel({ available: ALL_IDS, state: togglePin(emptyState(), "goal") });
    expect(unpinned.sections.find((s) => s.id === "all")!.rows.some((r) => r.kind === "subs")).toBe(true);
    const noGoal = buildWaffleModel({ available: ALL_IDS.filter((i) => i !== "goal"), state: emptyState() });
    expect(noGoal.sections.flatMap((s) => s.rows).some((r) => r.kind === "subs")).toBe(false);
  });

  it("attaches badges for pending work (HITL, queue) and ignores junk counts", () => {
    const m = buildWaffleModel({
      available: ALL_IDS,
      state: emptyState(),
      badges: { hitl: 3, queue: 7, live: -1, status: Number.NaN, team: 2.9 },
    });
    const byTab = Object.fromEntries(
      m.sections.flatMap((s) => s.rows).flatMap((r) => r.entries).filter((e) => e.kind === "tab").map((e) => [e.tab, e.badge]),
    );
    expect(byTab.hitl).toBe(3);
    expect(byTab.queue).toBe(7);
    expect(byTab.team).toBe(2);
    expect(byTab.live).toBe(0);
    expect(byTab.status).toBe(0);
    expect(byTab.goal).toBe(0);
  });

  it("marks the active tab and the pinned tabs", () => {
    const m = buildWaffleModel({ available: ALL_IDS, state: emptyState(), activeTab: "queue" });
    const entries = m.sections.flatMap((s) => s.rows).flatMap((r) => r.entries).filter((e) => e.kind === "tab");
    expect(entries.filter((e) => e.active).map((e) => e.tab)).toEqual(["queue"]);
    expect(entries.filter((e) => e.pinned).map((e) => e.tab)).toEqual(["goal", "notes", "insights"]);
  });

  it("gives an unknown tab a generic icon and a readable label", () => {
    const m = buildWaffleModel({ available: [...ALL_IDS, "ml-lab"], state: emptyState(), labels: { "ml-lab": "ML Lab" } });
    const e = m.sections.flatMap((s) => s.rows).flatMap((r) => r.entries).find((x) => x.tab === "ml-lab")!;
    expect(e.label).toBe("ML Lab");
    expect(e.icon).toBe("generic");
    const bare = buildWaffleModel({ available: ["ml-lab"], state: emptyState(), rank: [] });
    expect(bare.sections[0].rows[0].entries[0].label).toBe("Ml lab");
  });

  it("reports an empty model when nothing is available", () => {
    const m = buildWaffleModel({ available: [], state: emptyState() });
    expect(m.sections).toEqual([]);
    expect(m.count).toBe(0);
  });
});

describe("type-to-filter", () => {
  const model = (query: string, st: WaffleState = emptyState()) => buildWaffleModel({ available: ALL_IDS, state: st, query });

  it("collapses to one flat Results section", () => {
    const m = model("note");
    expect(m.sections.map((s) => s.id)).toEqual(["results"]);
    expect(tabKeys(m, "results")).toEqual(["notes"]);
  });

  it("matches label, id and keywords, case-insensitively, all words required", () => {
    expect(tabKeys(model("HITL"), "results")).toEqual(["hitl"]);
    expect(tabKeys(model("review"), "results")).toContain("hitl");
    expect(tabKeys(model("tool reference"), "results")).toEqual(["docs"]);
    expect(tabKeys(model("run history"), "results")).toContain("sessions");
    expect(tabKeys(model("sessions active"), "results")).toEqual(["status"]);
  });

  it("keeps the usefulness order inside the results (pinned first)", () => {
    const keys = tabKeys(model("history"), "results");
    expect(keys.length).toBeGreaterThan(2);
    const rank = (id: string) => WAFFLE_DEFAULT_RANK.indexOf(id);
    expect(keys).toEqual([...keys].sort((a, b) => rank(a) - rank(b)));
  });

  it("finds Goal with all its sub-entries for 'targets'", () => {
    const m = model("targets");
    expect(tabKeys(m, "results")).toContain("goal");
    const subs = m.sections[0].rows.filter((r) => r.kind === "subs").flatMap((r) => r.entries.map((e) => e.sub));
    // Goal matched on its own keyword, so it keeps every sub-entry.
    expect(subs).toEqual(["north-star", "version-goal", "sprint", "decisions"]);
  });

  it("surfaces a sub-entry alone when only it matches ('vision' finds North Star under Goal)", () => {
    const m = model("vision");
    expect(tabKeys(m, "results")).toEqual([]);
    const rows = m.sections[0].rows;
    expect(rows.every((r) => r.kind === "subs")).toBe(true);
    expect(rows[0].entries.map((e) => e.sub)).toEqual(["north-star"]);
    expect(rows[0].parentLabel).toBe("Goal");
  });

  it("returns no sections for a query that matches nothing, and ignores surrounding whitespace", () => {
    expect(model("zzzz-no-such-tab").sections).toEqual([]);
    expect(tabKeys(model("  notes  "), "results")).toEqual(["notes"]);
  });
});

describe("moveFocus", () => {
  const rows = [
    ["a", "b", "c"],
    ["d", "e", "f"],
    ["g"],
  ];

  it("walks left/right in reading order across row ends and clamps at the ends", () => {
    expect(moveFocus(rows, "a", "ArrowRight")).toBe("b");
    expect(moveFocus(rows, "c", "ArrowRight")).toBe("d");
    expect(moveFocus(rows, "d", "ArrowLeft")).toBe("c");
    expect(moveFocus(rows, "a", "ArrowLeft")).toBe("a");
    expect(moveFocus(rows, "g", "ArrowRight")).toBe("g");
  });

  it("moves up/down keeping the column and clamping on a shorter row", () => {
    expect(moveFocus(rows, "b", "ArrowDown")).toBe("e");
    expect(moveFocus(rows, "f", "ArrowDown")).toBe("g");
    expect(moveFocus(rows, "g", "ArrowUp")).toBe("d");
    expect(moveFocus(rows, "b", "ArrowUp")).toBe("b");
    expect(moveFocus(rows, "g", "ArrowDown")).toBe("g");
  });

  it("Home/End go to the row ends, Ctrl+Home/End to the grid ends", () => {
    expect(moveFocus(rows, "e", "Home")).toBe("d");
    expect(moveFocus(rows, "e", "End")).toBe("f");
    expect(moveFocus(rows, "e", "Home", true)).toBe("a");
    expect(moveFocus(rows, "e", "End", true)).toBe("g");
  });

  it("starts at the first entry when nothing is focused and ignores other keys", () => {
    expect(moveFocus(rows, null, "ArrowDown")).toBe("a");
    expect(moveFocus(rows, "zz", "ArrowRight")).toBe("a");
    expect(moveFocus(rows, "a", "x")).toBeNull();
    expect(moveFocus(rows, null, "x")).toBeNull();
    expect(moveFocus([], "a", "ArrowDown")).toBeNull();
  });
});

describe("computePopoverPlacement", () => {
  const viewports = [
    { width: 320, height: 568 },
    { width: 375, height: 667 },
    { width: 768, height: 1024 },
    { width: 1280, height: 720 },
    { width: 1920, height: 1080 },
  ];

  it("keeps the popover inside the viewport at every width, wherever the anchor is", () => {
    for (const viewport of viewports) {
      for (const left of [0, 52, 286, viewport.width - 40, viewport.width]) {
        const p = computePopoverPlacement({ anchor: { left, bottom: 40 }, width: 352, viewport });
        expect(p.left).toBeGreaterThanOrEqual(8);
        expect(p.left + p.width).toBeLessThanOrEqual(viewport.width - 8);
        expect(p.top).toBeGreaterThanOrEqual(8);
        expect(p.top + p.maxHeight).toBeLessThanOrEqual(viewport.height - 8 + 0.001);
      }
    }
  });

  it("narrows to the viewport on a 320px phone and anchors under the button on desktop", () => {
    const phone = computePopoverPlacement({ anchor: { left: 52, bottom: 40 }, width: 352, viewport: viewports[0] });
    expect(phone.width).toBe(304);
    expect(phone.left).toBe(8);
    const desktop = computePopoverPlacement({ anchor: { left: 286, bottom: 40 }, width: 352, viewport: viewports[4] });
    expect(desktop.left).toBe(286);
    expect(desktop.top).toBe(46);
    expect(desktop.width).toBe(352);
  });

  it("limits the height (the popover scrolls inside) on a short viewport", () => {
    const p = computePopoverPlacement({ anchor: { left: 10, bottom: 40 }, width: 352, viewport: { width: 1280, height: 300 } });
    expect(p.maxHeight).toBe(300 - 46 - 8);
  });
});

describe("data helpers", () => {
  it("countActiveSprintItems counts pending, todo and in_progress only", () => {
    expect(
      countActiveSprintItems([
        { status: "pending" },
        { status: "todo" },
        { status: "in_progress" },
        { status: "done" },
        { status: "skipped" },
        { status: "failed" },
        null,
      ]),
    ).toBe(3);
    expect(countActiveSprintItems(undefined)).toBe(0);
    expect(countActiveSprintItems({ items: [] })).toBe(0);
  });

  it("readWaffleBadges reads the HITL chip and the stored queue count", () => {
    const host = document.createElement("div");
    host.innerHTML = '<span class="hitl-vtab-badge" style="display:inline-block">4</span>';
    expect(readWaffleBadges(host, { sprintActiveCount: 6 })).toEqual({ hitl: 4, queue: 6 });
    host.innerHTML = '<span class="hitl-vtab-badge" style="display:none">4</span>';
    expect(readWaffleBadges(host, { sprintActiveCount: 0 })).toEqual({});
    expect(readWaffleBadges(null, null)).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// 2. The launcher over the real rail markup (jsdom)
// ---------------------------------------------------------------------------

const dashboardSrc = readFileSync(path.resolve("meridian/static/dashboard.ts"), "utf-8");

/** The inline rail markup from buildTabBody, with its two template expressions resolved. */
function realRailMarkup(): string {
  const start = dashboardSrc.indexOf('<div class="vtab-strip"');
  const end = dashboardSrc.indexOf('<div class="vtab-drawer', start);
  expect(start).toBeGreaterThan(0);
  return dashboardSrc
    .slice(start, end)
    .replace(/\$\{project\.id\}/g, "p1")
    .replace(/\$\{\(window\.MERIDIAN_HOSTED[^}]*\}/, '<button class="vtab-btn" data-vtab="files" title="Files">F</button>');
}

interface Page {
  strip: HTMLElement;
  clicks: string[];
  subClicks: string[];
  /** Per vtab click: was the tab's group already expanded when its onclick ran? */
  expandedAtClick: Record<string, boolean>;
  storage: Storage;
  store: Map<string, string>;
  badges: Record<string, number>;
  activeProject: { id: string | null };
  mount: (extra?: Partial<Parameters<typeof mountWaffle>[0]>) => WaffleController;
}

function buildPage(): Page {
  document.body.innerHTML =
    '<div id="topbar"><div id="tabs"></div></div><div id="tab-body-p1">' +
    realRailMarkup() +
    '<div id="drawer-goal-p1">' +
    WAFFLE_SUBTABS.map((s) => `<button class="goal-subtab-btn" data-gtab="${s.id}"></button>`).join("") +
    "</div></div>" +
    '<button id="outside-btn">outside</button>';
  const strip = document.getElementById("vtab-strip-p1")!;
  const clicks: string[] = [];
  const subClicks: string[] = [];
  const expandedAtClick: Record<string, boolean> = {};
  const { revealGroupForTab } = wireVtabGroups(strip);
  strip.querySelectorAll<HTMLElement>(".vtab-btn").forEach((btn) => {
    // Mirrors the production onclick in buildTabBody: reveal the group, mark active.
    btn.onclick = () => {
      const tab = btn.dataset.vtab as string;
      const group = btn.closest(".vtab-group") as HTMLElement;
      expandedAtClick[tab] = !group.classList.contains("collapsed");
      revealGroupForTab(tab);
      strip.querySelectorAll(".vtab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      clicks.push(tab);
    };
  });
  document.querySelectorAll<HTMLElement>(".goal-subtab-btn").forEach((b) => {
    b.onclick = () => subClicks.push(b.dataset.gtab as string);
  });
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  } as unknown as Storage;
  const page: Page = {
    strip,
    clicks,
    subClicks,
    expandedAtClick,
    storage,
    store,
    badges: {},
    activeProject: { id: "p1" },
    mount: (extra = {}) => {
      const c = mountWaffle({
        host: document.getElementById("topbar"),
        getStrip: () => (page.activeProject.id ? document.getElementById(`vtab-strip-${page.activeProject.id}`) : null),
        getBadges: () => page.badges,
        storage,
        storageKey: "k",
        ...extra,
      });
      expect(c).not.toBeNull();
      return c as WaffleController;
    },
  };
  return page;
}

function key(el: Element, k: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const ev = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init });
  el.dispatchEvent(ev);
  return ev;
}

const tile = (tab: string) => document.querySelector<HTMLElement>(`.waffle-tile[data-waffle-tab="${tab}"]`);
const sub = (id: string) => document.querySelector<HTMLElement>(`.waffle-sub[data-waffle-sub="${id}"]`);
const tileOrder = () => Array.from(document.querySelectorAll<HTMLElement>(".waffle-tile")).map((t) => t.dataset.waffleTab);
const filterInput = () => document.querySelector<HTMLInputElement>(".waffle-filter")!;

function type(text: string): void {
  const input = filterInput();
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("launcher: open, close, focus", () => {
  let page: Page;
  let waffle: WaffleController;

  beforeEach(() => {
    page = buildPage();
    waffle = page.mount();
  });

  afterEach(() => {
    waffle.destroy();
    document.body.innerHTML = "";
  });

  it("puts the 9-dot button first in the top bar, closed, with popup semantics", () => {
    const slot = document.getElementById("topbar")!.firstElementChild!;
    expect(slot.classList.contains("waffle-slot")).toBe(true);
    expect(document.getElementById("tabs")).toBe(slot.nextElementSibling);
    const btn = waffle.button;
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.getAttribute("aria-haspopup")).toBe("dialog");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.getAttribute("aria-controls")).toBe(waffle.popover.id);
    expect(btn.getAttribute("aria-label")).toBeTruthy();
    expect(btn.querySelectorAll("svg circle").length).toBe(9);
    expect(waffle.popover.hidden).toBe(true);
  });

  it("click opens (aria-expanded true) and a second click closes", () => {
    waffle.button.click();
    expect(waffle.isOpen()).toBe(true);
    expect(waffle.popover.hidden).toBe(false);
    expect(waffle.button.getAttribute("aria-expanded")).toBe("true");
    waffle.button.click();
    expect(waffle.isOpen()).toBe(false);
    expect(waffle.popover.hidden).toBe(true);
    expect(waffle.button.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens with focus in the filter box; ArrowDown on the button also opens", () => {
    waffle.button.click();
    expect(document.activeElement).toBe(filterInput());
    waffle.close(false);
    waffle.button.focus();
    key(waffle.button, "ArrowDown");
    expect(waffle.isOpen()).toBe(true);
  });

  it("Escape closes and returns focus to the button, from the filter and from a tile", () => {
    waffle.button.click();
    key(filterInput(), "Escape");
    expect(waffle.isOpen()).toBe(false);
    expect(document.activeElement).toBe(waffle.button);

    waffle.button.click();
    key(filterInput(), "ArrowDown");
    expect(document.activeElement).toBe(tile("goal"));
    key(document.activeElement as Element, "Escape");
    expect(waffle.isOpen()).toBe(false);
    expect(document.activeElement).toBe(waffle.button);
  });

  it("closes on a click outside without stealing focus", () => {
    waffle.button.click();
    const outside = document.getElementById("outside-btn")!;
    outside.focus();
    outside.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(waffle.isOpen()).toBe(false);
    expect(document.activeElement).toBe(outside);
  });

  it("closes when focus moves out of the popover", () => {
    waffle.button.click();
    document.getElementById("outside-btn")!.focus();
    expect(waffle.isOpen()).toBe(false);
  });

  it("stays open for clicks inside the popover (including the gaps)", () => {
    waffle.button.click();
    waffle.popover.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    document.querySelector(".waffle-heading")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(waffle.isOpen()).toBe(true);
  });

  it("traps Tab: wraps from the last stop to the filter and Shift+Tab from the filter to the last stop", () => {
    waffle.button.click();
    const roving = document.querySelector<HTMLElement>('.waffle-tile[tabindex="0"]')!;
    expect(roving).toBe(tile("goal"));
    roving.focus();
    const fwd = key(roving, "Tab");
    expect(fwd.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(filterInput());
    const back = key(filterInput(), "Tab", { shiftKey: true });
    expect(back.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(roving);
    // In the middle of the trap Tab is left to the browser (filter -> tile).
    filterInput().focus();
    const mid = key(filterInput(), "Tab");
    expect(mid.defaultPrevented).toBe(false);
  });

  it("mounting twice leaves exactly one launcher", () => {
    const again = page.mount();
    expect(document.querySelectorAll(".waffle-slot").length).toBe(1);
    expect(document.querySelectorAll(".waffle-popover").length).toBe(1);
    again.destroy();
    expect(document.querySelectorAll(".waffle-slot").length).toBe(0);
  });

  it("returns null without a host element", () => {
    expect(mountWaffle({ host: null, getStrip: () => null })).toBeNull();
  });
});

describe("launcher: content and keyboard navigation", () => {
  let page: Page;
  let waffle: WaffleController;

  beforeEach(() => {
    page = buildPage();
    waffle = page.mount();
    waffle.button.click();
  });

  afterEach(() => {
    waffle.destroy();
    document.body.innerHTML = "";
  });

  it("renders Pinned then All tabs in the owner's order; codeintel stays out while the rail hides it", () => {
    expect(tileOrder().slice(0, 3)).toEqual(["goal", "notes", "insights"]);
    const expected = WAFFLE_DEFAULT_RANK.filter((id) => id !== "codeintel");
    expect(tileOrder()).toEqual(expected);
    expect(Array.from(document.querySelectorAll(".waffle-heading")).map((h) => h.textContent)).toEqual(["Pinned", "All tabs"]);
  });

  it("offers every rail tab that is displayed, and picks up codeintel once the rail shows it", () => {
    (page.strip.querySelector('[data-vtab="codeintel"]') as HTMLElement).style.display = "";
    waffle.refresh();
    expect(tileOrder()).toEqual([...WAFFLE_DEFAULT_RANK]);
  });

  it("shows Goal's sub-entries right under the Goal row", () => {
    const subs = document.querySelector('.waffle-subs[data-waffle-subs-of="goal"]')!;
    expect(subs).not.toBeNull();
    expect(subs.previousElementSibling!.querySelector('[data-waffle-tab="goal"]')).not.toBeNull();
    expect(Array.from(subs.querySelectorAll(".waffle-sub")).map((e) => e.textContent)).toEqual(
      WAFFLE_SUBTABS.map((s) => s.label),
    );
  });

  it("arrow keys walk the grid: Right, Down (+3), Left, Up, Home, End", () => {
    key(filterInput(), "ArrowDown");
    expect(document.activeElement).toBe(tile("goal"));
    key(document.activeElement as Element, "ArrowRight");
    expect(document.activeElement).toBe(tile("notes"));
    key(document.activeElement as Element, "ArrowLeft");
    expect(document.activeElement).toBe(tile("goal"));
    key(document.activeElement as Element, "ArrowDown"); // into the Goal sub-row
    expect((document.activeElement as HTMLElement).dataset.waffleSub).toBe("north-star");
    key(document.activeElement as Element, "ArrowRight");
    expect((document.activeElement as HTMLElement).dataset.waffleSub).toBe("version-goal");
    key(document.activeElement as Element, "ArrowUp");
    expect(document.activeElement).toBe(tile("notes"));
    key(document.activeElement as Element, "End");
    expect(document.activeElement).toBe(tile("insights"));
    key(document.activeElement as Element, "Home");
    expect(document.activeElement).toBe(tile("goal"));
    key(document.activeElement as Element, "End", { ctrlKey: true });
    expect(document.activeElement).toBe(tile("settings"));
  });

  it("ArrowUp from the first row returns to the filter box", () => {
    key(filterInput(), "ArrowDown");
    key(document.activeElement as Element, "ArrowUp");
    expect(document.activeElement).toBe(filterInput());
  });

  it("keeps exactly one roving tab stop that follows focus", () => {
    key(filterInput(), "ArrowDown");
    key(document.activeElement as Element, "ArrowRight");
    const stops = document.querySelectorAll('[data-waffle-key][tabindex="0"]');
    expect(stops.length).toBe(1);
    expect(stops[0]).toBe(tile("notes"));
  });

  it("the filter narrows the grid and Enter in the box opens the first match", () => {
    type("tim");
    expect(tileOrder()).toEqual(["timeline"]);
    expect(document.querySelector(".waffle-sr")!.textContent).toBe("1 match");
    key(filterInput(), "Enter");
    expect(page.clicks).toEqual(["timeline"]);
    expect(waffle.isOpen()).toBe(false);
  });

  it("filtering for something absent shows a message, and clearing the box restores the grid", () => {
    type("zzzz-nothing");
    expect(document.querySelector(".waffle-empty")!.textContent).toContain("No tabs match");
    expect(tileOrder()).toEqual([]);
    key(filterInput(), "Enter"); // nothing to open: must not throw or click
    expect(page.clicks).toEqual([]);
    type("");
    expect(tileOrder().length).toBeGreaterThan(10);
  });

  it("reopening starts from a clean filter", () => {
    type("notes");
    waffle.close(false);
    waffle.open();
    expect(filterInput().value).toBe("");
    expect(tileOrder().length).toBeGreaterThan(10);
  });

  it("shows an empty-state message and does not throw when no project is open", () => {
    waffle.close(false);
    page.activeProject.id = null;
    waffle.open();
    expect(document.querySelector(".waffle-empty")!.textContent).toContain("Open a project");
    key(filterInput(), "ArrowDown");
    key(filterInput(), "Enter");
    expect(page.clicks).toEqual([]);
  });
});

describe("launcher: badges", () => {
  let page: Page;
  let waffle: WaffleController;

  beforeEach(() => {
    page = buildPage();
    waffle = page.mount();
  });

  afterEach(() => {
    waffle.destroy();
    document.body.innerHTML = "";
  });

  it("badges the HITL and Queue tiles with pending work and puts a dot on the button for HITL", () => {
    expect(document.querySelector(".waffle-dot")!.hasAttribute("hidden")).toBe(true);
    page.badges = { hitl: 3, queue: 12 };
    waffle.refresh();
    expect((document.querySelector(".waffle-dot") as HTMLElement).hidden).toBe(false);
    expect(waffle.button.getAttribute("aria-label")).toContain("3 review requests");
    waffle.open();
    expect(tile("hitl")!.querySelector(".waffle-badge")!.textContent).toBe("3");
    expect(tile("queue")!.querySelector(".waffle-badge")!.textContent).toBe("12");
    expect(tile("hitl")!.getAttribute("aria-label")).toBe("HITL, 3 pending");
    expect(tile("goal")!.querySelector(".waffle-badge")).toBeNull();
  });

  it("caps huge counts at 99+ and keeps the exact number in the accessible name", () => {
    page.badges = { queue: 250 };
    waffle.open();
    expect(tile("queue")!.querySelector(".waffle-badge")!.textContent).toBe("99+");
    expect(tile("queue")!.getAttribute("aria-label")).toContain("250 pending");
  });

  it("survives a badge source that throws (no badges, launcher still works)", () => {
    const w = page.mount({
      getBadges: () => {
        throw new Error("boom");
      },
    });
    expect(() => w.open()).not.toThrow();
    expect(tileOrder().length).toBeGreaterThan(10);
    expect(document.querySelector(".waffle-badge")).toBeNull();
    expect((document.querySelector(".waffle-dot") as HTMLElement).hidden).toBe(true);
  });

  it("module-level refreshWaffle() updates the mounted launcher", () => {
    page.badges = { hitl: 1 };
    refreshWaffle();
    expect((document.querySelector(".waffle-dot") as HTMLElement).hidden).toBe(false);
    expect(waffle.button.getAttribute("aria-label")).toContain("1 review request pending");
  });
});

describe("launcher: activating a tile", () => {
  let page: Page;
  let waffle: WaffleController;

  beforeEach(() => {
    page = buildPage();
    waffle = page.mount();
    waffle.button.click();
  });

  afterEach(() => {
    waffle.destroy();
    document.body.innerHTML = "";
  });

  it("clicks the matching .vtab-btn (the same code path as the rail) and closes", () => {
    const onclick = vi.spyOn(page.strip.querySelector<HTMLElement>('.vtab-btn[data-vtab="notes"]')!, "click");
    tile("notes")!.click();
    expect(onclick).toHaveBeenCalledTimes(1);
    expect(page.clicks).toEqual(["notes"]);
    expect(page.strip.querySelector('.vtab-btn[data-vtab="notes"]')!.classList.contains("active")).toBe(true);
    expect(waffle.isOpen()).toBe(false);
    expect(document.activeElement).toBe(waffle.button);
  });

  it("reveals the tab's collapsed group BEFORE the button's own click handler runs", () => {
    // 'rewind' lives in History, which starts collapsed (status is the active tab).
    const history = page.strip.querySelector('.vtab-group[data-vgroup="history"]')!;
    expect(history.classList.contains("collapsed")).toBe(true);
    tile("rewind")!.click();
    expect(page.expandedAtClick.rewind).toBe(true);
    expect(history.classList.contains("collapsed")).toBe(false);
    expect((history.querySelector(".vtab-group-tabs") as HTMLElement).style.display).toBe("flex");
  });

  it("Enter on a focused tile opens it (native button activation)", () => {
    key(filterInput(), "ArrowDown");
    key(document.activeElement as Element, "ArrowRight");
    (document.activeElement as HTMLElement).click(); // Enter on a <button> dispatches click
    expect(page.clicks).toEqual(["notes"]);
  });

  it("a Goal sub-entry clicks the Goal tab, then that Goal sub-tab button", () => {
    sub("version-goal")!.click();
    expect(page.clicks).toEqual(["goal"]);
    expect(page.subClicks).toEqual(["version-goal"]);
  });

  it("counts the use, persists it, and keeps working with storage blocked", () => {
    tile("queue")!.click();
    expect(JSON.parse(page.store.get("k")!)).toMatchObject({ usage: { queue: 1 }, recent: ["queue"] });
    waffle.destroy();

    const blocked = buildPage();
    const w = blocked.mount({
      storage: {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
      } as unknown as Storage,
    });
    w.open();
    tile("live")!.click();
    expect(blocked.clicks).toEqual(["live"]);
    w.destroy();

    const noStorage = buildPage();
    const w2 = noStorage.mount({ storage: null });
    w2.open();
    tile("team")!.click();
    expect(noStorage.clicks).toEqual(["team"]);
    w2.destroy();
  });

  it("does nothing (and does not throw) when the strip has no such button", () => {
    expect(activateWaffleTab(page.strip, "no-such-tab")).toBe(false);
    expect(page.clicks).toEqual([]);
  });

  it("lands on EVERY tab: waffle activation + revealGroupForTab navigation reaches each button", () => {
    (page.strip.querySelector('[data-vtab="codeintel"]') as HTMLElement).style.display = "";
    for (const id of WAFFLE_DEFAULT_RANK) {
      waffle.close(false);
      waffle.open();
      const t = tile(id);
      expect(t, `no tile for ${id}`).not.toBeNull();
      t!.click();
      const btn = page.strip.querySelector(`.vtab-btn[data-vtab="${id}"]`)!;
      expect(btn.classList.contains("active"), `${id} did not become active`).toBe(true);
      const group = btn.closest(".vtab-group")!;
      expect(group.classList.contains("collapsed"), `${id}: group left collapsed`).toBe(false);
      expect((group.querySelector(".vtab-group-tabs") as HTMLElement).style.display, `${id}: tabs not laid out`).toBe("flex");
    }
    expect(page.clicks).toEqual([...WAFFLE_DEFAULT_RANK]);
  });
});

describe("launcher: pins and adaptive order", () => {
  let page: Page;
  let waffle: WaffleController | null = null;

  beforeEach(() => {
    page = buildPage();
  });

  afterEach(() => {
    waffle?.destroy();
    waffle = null;
    document.body.innerHTML = "";
  });

  it("P on a focused tile pins it (moves to Pinned, persisted, focus kept); P again unpins", () => {
    waffle = page.mount();
    waffle.open();
    key(filterInput(), "ArrowDown");
    // walk to the Queue tile in All tabs
    const q = tile("queue")!;
    q.focus();
    key(q, "p");
    expect(tileOrder().slice(0, 4)).toEqual(["goal", "notes", "insights", "queue"]);
    expect(document.activeElement).toBe(tile("queue"));
    expect(tile("queue")!.dataset.pinned).toBe("true");
    expect(JSON.parse(page.store.get("k")!).pins).toEqual(["goal", "notes", "insights", "queue"]);
    key(document.activeElement as Element, "P");
    expect(tile("queue")!.dataset.pinned).toBe("false");
    expect(tileOrder().slice(0, 3)).toEqual(["goal", "notes", "insights"]);
  });

  it("clicking a tile's pin glyph toggles the pin without activating the tab", () => {
    waffle = page.mount();
    waffle.open();
    (tile("goal")!.querySelector("[data-waffle-pin]") as HTMLElement).click();
    expect(page.clicks).toEqual([]);
    expect(waffle.getState().pins).toEqual(["notes", "insights"]);
    expect(waffle.isOpen()).toBe(true);
    expect(tileOrder().slice(0, 2)).toEqual(["notes", "insights"]);
  });

  it("P on a sub-entry does nothing", () => {
    waffle = page.mount();
    waffle.open();
    const s = sub("sprint")!;
    s.focus();
    key(s, "p");
    expect(waffle.getState().pins).toBeNull();
  });

  it("restores pins, recents and usage on the next page load", () => {
    page.store.set("k", JSON.stringify({ v: 1, pins: ["queue"], usage: { live: 3 }, recent: ["live", "team"] }));
    waffle = page.mount();
    waffle.open();
    expect(tileOrder().slice(0, 3)).toEqual(["queue", "live", "team"]);
    expect(Array.from(document.querySelectorAll(".waffle-heading")).map((h) => h.textContent)).toEqual([
      "Pinned",
      "Recent",
      "All tabs",
    ]);
  });

  it("re-sorts the middle by usage once the threshold is reached, leaving the owner's picks on top", () => {
    page.store.set("k", JSON.stringify({ v: 1, pins: null, usage: { sessions: 20, settings: 10 }, recent: [] }));
    waffle = page.mount();
    waffle.open();
    expect(tileOrder().slice(0, 5)).toEqual(["goal", "notes", "insights", "sessions", "settings"]);
    expect(tileOrder()[tileOrder().length - 1]).not.toBe("settings");
  });

  it("an unpinned owner pick stays unpinned across reloads", () => {
    waffle = page.mount();
    waffle.open();
    (tile("notes")!.querySelector("[data-waffle-pin]") as HTMLElement).click();
    waffle.destroy();
    waffle = page.mount();
    waffle.open();
    expect(tileOrder().slice(0, 2)).toEqual(["goal", "insights"]);
    expect(tile("notes")!.dataset.pinned).toBe("false");
  });
});

describe("rail helpers", () => {
  it("readRailTabs skips display:none buttons, reads the active tab and uses titles as fallback labels", () => {
    const page = buildPage();
    const rail = readRailTabs(page.strip);
    expect(rail.ids).not.toContain("codeintel"); // inline display:none in the markup
    expect(rail.ids).toContain("experiments");
    expect(rail.active).toBe("status");
    expect(rail.labels.hitl).toBe("Researcher Review Queue");
    document.body.innerHTML = "";
  });

  it("the real rail renders every tab the waffle knows about (and nothing it does not)", () => {
    const page = buildPage();
    const ids = Array.from(page.strip.querySelectorAll<HTMLElement>(".vtab-btn")).map((b) => b.dataset.vtab).sort();
    expect(ids).toEqual([...WAFFLE_DEFAULT_RANK].sort());
    const groupIds = VTAB_GROUPS.flatMap((g) => g.tabs).sort();
    expect(ids).toEqual(groupIds);
    document.body.innerHTML = "";
  });
});

// ---------------------------------------------------------------------------
// 3. Accessibility
// ---------------------------------------------------------------------------

describe("a11y: roles, names and focus", () => {
  let page: Page;
  let waffle: WaffleController;

  beforeEach(() => {
    page = buildPage();
    page.badges = { hitl: 2 };
    waffle = page.mount();
    waffle.open();
  });

  afterEach(() => {
    waffle.destroy();
    document.body.innerHTML = "";
  });

  it("the popover is a labelled dialog containing a labelled filter and a labelled menu", () => {
    const p = waffle.popover;
    expect(p.getAttribute("role")).toBe("dialog");
    expect(p.getAttribute("aria-label")).toBeTruthy();
    expect(p.getAttribute("aria-modal")).toBe("true");
    const input = filterInput();
    expect(input.type).toBe("search");
    expect(input.getAttribute("aria-label")).toBeTruthy();
    const menu = p.querySelector('[role="menu"]')!;
    expect(menu.getAttribute("aria-label")).toBeTruthy();
    expect(input.getAttribute("aria-controls")).toBe(menu.id);
  });

  it("the menu only contains groups, presentational rows and menuitems", () => {
    const menu = waffle.popover.querySelector('[role="menu"]')!;
    const roles = new Set(Array.from(menu.querySelectorAll("[role]")).map((e) => e.getAttribute("role")));
    expect([...roles].sort()).toEqual(["group", "menuitem", "none"].sort());
    for (const g of menu.querySelectorAll('[role="group"]')) expect(g.getAttribute("aria-label")).toBeTruthy();
  });

  it("every control has an accessible name and every tile pairs an SVG icon with a visible text label", () => {
    const controls = [waffle.button, filterInput(), ...Array.from(document.querySelectorAll<HTMLElement>('[role="menuitem"]'))];
    for (const c of controls) {
      const name = c.getAttribute("aria-label") || c.textContent || "";
      expect(name.trim().length, `unnamed control: ${c.outerHTML.slice(0, 80)}`).toBeGreaterThan(0);
    }
    for (const t of document.querySelectorAll<HTMLElement>(".waffle-tile")) {
      expect(t.querySelector(".waffle-icon-wrap svg")).not.toBeNull();
      expect(t.querySelector(".waffle-label")!.textContent!.trim().length).toBeGreaterThan(0);
      expect(t.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("exposes pinned state, pending counts, the pin shortcut and the current tab to assistive tech", () => {
    expect(tile("goal")!.getAttribute("aria-label")).toBe("Goal, pinned");
    expect(tile("hitl")!.getAttribute("aria-label")).toBe("HITL, 2 pending");
    expect(tile("goal")!.getAttribute("aria-keyshortcuts")).toBe("P");
    expect(tile("status")!.getAttribute("aria-current")).toBe("true");
    expect(document.querySelectorAll('[aria-current="true"]').length).toBe(1);
    expect(document.querySelector(".waffle-sr")!.getAttribute("role")).toBe("status");
  });

  it("decorative text (section headings, pin glyph) is hidden from the accessibility tree", () => {
    for (const h of document.querySelectorAll(".waffle-heading")) expect(h.getAttribute("aria-hidden")).toBe("true");
    for (const p of document.querySelectorAll(".waffle-pin")) expect(p.getAttribute("aria-hidden")).toBe("true");
  });

  it("only one tile is tabbable at a time (roving tabindex), the rest are tabindex -1", () => {
    const tabbable = Array.from(document.querySelectorAll<HTMLElement>("[data-waffle-key]")).filter((e) => e.tabIndex >= 0);
    expect(tabbable.length).toBe(1);
  });

  it("the live region announces how many tabs match the filter", () => {
    type("t");
    expect(document.querySelector(".waffle-sr")!.textContent).toMatch(/^\d+ matches$/);
  });
});

// ---------------------------------------------------------------------------
// 4. Source guards (the pieces dashboard.ts must keep wired)
// ---------------------------------------------------------------------------

describe("dashboard.ts wiring", () => {
  it("mounts the launcher first thing in init(), before any await", () => {
    const initAt = dashboardSrc.indexOf("(async function init() {");
    const mountAt = dashboardSrc.indexOf("_mountDashboardWaffle();", initAt);
    const firstAwait = dashboardSrc.indexOf("await ", initAt);
    expect(initAt).toBeGreaterThan(0);
    expect(mountAt).toBeGreaterThan(initAt);
    expect(mountAt).toBeLessThan(firstAwait);
  });

  it("feeds the Queue badge from the Goal, Live and Queue loaders and refreshes on HITL chip updates", () => {
    expect((dashboardSrc.match(/_setWaffleQueueCount\(/g) || []).length).toBeGreaterThanOrEqual(4); // def + 3 loaders
    const from = dashboardSrc.indexOf("function setVtabCountBadge");
    const to = dashboardSrc.indexOf("function _setWaffleQueueCount", from);
    expect(from).toBeGreaterThan(0);
    expect(dashboardSrc.slice(from, to)).toContain("refreshWaffle();");
  });

  it("refreshes the badge dot when the active project tab changes (the dot reads the active project)", () => {
    const at = dashboardSrc.indexOf("function activateTab(id: any) {");
    const end = dashboardSrc.indexOf("function buildTabBody", at);
    expect(at).toBeGreaterThan(0);
    expect(dashboardSrc.slice(at, end)).toContain("refreshWaffle();");
  });

  it("the rail's own onclick reveals the tab's group before running any loader", () => {
    const at = dashboardSrc.indexOf("btn.onclick = () => {", dashboardSrc.indexOf("wireVtabGroups(vtabStrip)"));
    const reveal = dashboardSrc.indexOf("revealGroupForTab(vtab);", at);
    const firstLoader = dashboardSrc.indexOf("if (vtab === 'files') loadFilesTab", at);
    expect(reveal).toBeGreaterThan(at);
    expect(reveal).toBeLessThan(firstLoader);
  });
});
