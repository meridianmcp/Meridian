// 8a665a03 -- a background refresh must not throw away what the user is typing.
//
// Regression found by independent verification of the live-refresh lane: unsaved text in the
// Goal tab (goal, north star, sprint) was overwritten, and its 'dirty' marker cleared, by
//   (a) set_decision publishing goal_updated {field: 'decisions'} -- the client ignored the
//       field and ran the whole refreshGoal for it, and
//   (b) a WebSocket reconnect -- resyncProjectViews -> refreshTab -> refreshGoal.
// (Every task event already ran refreshGoal too: it was a latent bug the new triggers made
// reachable from an ordinary agent session.)
//
// The code under test is the REAL dashboard.ts (see source-harness.ts). File name has no
// `dashboard-` prefix on purpose: tests/dashboard_src.py concatenates every dashboard-*.ts.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { loadDashboardFunctions } from "./source-harness";
import "./dashboard-utils";

const DASH = "meridian/static/dashboard.ts";
const PID = "proj-goal-1";

const SERVER_GOAL = {
  content: "v1.0 — the label\n\nSHIPPED so far\n\nCURRENT FOCUS\nserver focus text",
  version: 7,
  north_star: "server north star",
  sprint: "server sprint",
  decisions: "[2026-10-07] a logged decision",
  updated_at: "2026-10-07 06:00:00",
};

function mountGoalTab() {
  document.body.innerHTML = `
    <div id="goal-title-${PID}"></div>
    <div id="goal-shipped-${PID}"></div>
    <textarea id="goal-${PID}"></textarea>
    <div id="goal-autoblocks-wrapper-${PID}"></div>
    <pre id="goal-autoblocks-${PID}"></pre>
    <span id="goal-version-${PID}"></span><span id="goal-state-${PID}"></span>
    <textarea id="goal-north-star-${PID}"></textarea>
    <span id="goal-ns-lock-${PID}"></span><span id="goal-ns-inherited-${PID}"></span>
    <textarea id="goal-sprint-${PID}"></textarea>
    <span id="goal-ns-ts-${PID}"></span><span id="goal-vg-ts-${PID}"></span><span id="goal-sp-ts-${PID}"></span>
    <div id="decisions-table-${PID}"></div>`;
  return {
    goal: document.getElementById(`goal-${PID}`) as HTMLTextAreaElement,
    ns: document.getElementById(`goal-north-star-${PID}`) as HTMLTextAreaElement,
    sprint: document.getElementById(`goal-sprint-${PID}`) as HTMLTextAreaElement,
    shipped: document.getElementById(`goal-shipped-${PID}`)!,
    version: document.getElementById(`goal-version-${PID}`)!,
    title: document.getElementById(`goal-title-${PID}`)!,
  };
}

/** Mark a textarea the way the real `input` handler does after the user typed. */
function type(el: HTMLTextAreaElement, text: string) {
  el.value = text;
  el.classList.add("dirty");
}

type Mocks = Record<string, any>;

function setup(over: Mocks = {}, extraFunctions: string[] = []) {
  const dom = mountGoalTab();
  const state: any = { panels: { [PID]: { activeVtab: "goal", taskCache: [] } }, tabs: [], projects: [] };
  const m: Mocks = {
    state,
    projectApi: vi.fn(async () => ({ ...SERVER_GOAL })),
    api: vi.fn(async () => ({})),
    toast: vi.fn(),
    confirm: vi.fn(() => true), // "North star is intended to be stable. Save changes?"
    autosizeGoalField: vi.fn(),
    noteGoalEvent: vi.fn(), // goal lane (fc779141): records who changed which field; not under test here
    formatRelativeTime: () => "just now",
    renderDecisionsTable: vi.fn(),
    loadPinnedDecisions: vi.fn(),
    refreshSessions: vi.fn(async () => {}),
    refreshTasks: vi.fn(async () => {}),
    _sprintSelectSyncers: {} as Record<string, any>,
    _repaintTimers: {} as Record<string, any>,
    ...over,
  };
  const fns = loadDashboardFunctions(
    DASH,
    ["refreshGoal", "refreshTab", "saveGoal", "saveNorthStar", "saveSprint", ...extraFunctions],
    m,
  ) as Record<string, (...a: any[]) => any>;
  return { dom, state, m, fns };
}

// The three describe blocks that used to be here tested the Goal tab's unsaved-text protection as the
// live-refresh lane first built it (a 'dirty' class on the textareas). The goal lane (fc779141) replaced that
// with the GoalField editors (dashboard-goal-conflict.ts, tested in dashboard-goal-conflict.test.ts and
// dashboard-goal-wiring.test.ts), which is the implementation that ships, so those blocks were removed at
// integration. What remains below is independent of it: the decisions-only repaint, refreshDecisionsLog and
// the inline pinned-decision editor.

describe("handleWsEvent: goal_updated", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function route(event: any) {
    const state: any = { panels: { [PID]: { activeVtab: "goal", taskCache: [] } }, tabs: [], projects: [] };
    const m: Mocks = {
      state,
      refreshGoal: vi.fn(), noteGoalEvent: vi.fn(),
      refreshDecisionsLog: vi.fn(),
      _repaintTimers: {} as Record<string, any>,
    };
    const { handleWsEvent, _debounceRepaint } = loadDashboardFunctions(DASH, ["handleWsEvent", "_debounceRepaint"], m);
    expect(_debounceRepaint).toBeTypeOf("function");
    handleWsEvent(PID, event);
    vi.advanceTimersByTime(300);
    return m;
  }

  it("a logged decision repaints only the Decisions table, never the editable goal fields", () => {
    const m = route({ type: "goal_updated", project_id: PID, field: "decisions" });
    expect(m.refreshDecisionsLog).toHaveBeenCalledWith(PID);
    expect(m.refreshGoal).not.toHaveBeenCalled();
  });

  it("a burst of logged decisions repaints the table once", () => {
    const state: any = { panels: { [PID]: { activeVtab: "goal", taskCache: [] } }, tabs: [], projects: [] };
    const m: Mocks = { state, refreshGoal: vi.fn(), noteGoalEvent: vi.fn(), refreshDecisionsLog: vi.fn(), _repaintTimers: {} };
    const { handleWsEvent } = loadDashboardFunctions(DASH, ["handleWsEvent", "_debounceRepaint"], m);
    for (let i = 0; i < 8; i++) handleWsEvent(PID, { type: "goal_updated", project_id: PID, field: "decisions" });
    vi.advanceTimersByTime(300);
    expect(m.refreshDecisionsLog).toHaveBeenCalledTimes(1);
  });

  it("a new goal version (set_goal / north star / sprint) still refreshes the goal", () => {
    const m = route({ type: "goal_updated", project_id: PID, version: 9 });
    expect(m.refreshGoal).toHaveBeenCalledWith(PID);
    expect(m.refreshDecisionsLog).not.toHaveBeenCalled();
  });
});

describe("refreshDecisionsLog", () => {
  it("renders only the Decisions table: the goal textareas are never touched", async () => {
    const { dom, m, fns } = setup({}, ["refreshDecisionsLog"]);
    type(dom.goal, "UNSAVED GOAL TEXT");
    type(dom.ns, "UNSAVED NS");
    await fns.refreshDecisionsLog(PID);
    expect(m.projectApi).toHaveBeenCalledWith(PID, `/projects/${PID}/goal`);
    expect(m.renderDecisionsTable).toHaveBeenCalledWith(PID, "[2026-10-07] a logged decision");
    expect(dom.goal.value).toBe("UNSAVED GOAL TEXT");
    expect(dom.ns.value).toBe("UNSAVED NS");
    expect(dom.goal.classList.contains("dirty")).toBe(true);
    expect(dom.ns.classList.contains("dirty")).toBe(true);
  });

  it("does nothing when the Goal tab is not built, and survives a failed fetch", async () => {
    const { m, fns } = setup({ projectApi: vi.fn(async () => { throw new Error("x"); }) }, ["refreshDecisionsLog"]);
    await expect(fns.refreshDecisionsLog(PID)).resolves.toBeUndefined();
    expect(m.renderDecisionsTable).not.toHaveBeenCalled();
    document.body.innerHTML = "";
    await fns.refreshDecisionsLog(PID);
    expect(m.projectApi).toHaveBeenCalledTimes(1);
  });
});

describe("loadPinnedDecisions keeps an open inline editor", () => {
  const items = [
    { id: "d1", title: "first", body: "first body", category: "TECHNICAL", priority: "normal", status: "active", created_at: "2026-10-01" },
  ];

  function setupDecisions(list: any[]) {
    document.body.innerHTML = `<div id="pinned-decisions-${PID}"></div><div id="decisions-view-archived-${PID}"></div>`;
    const state: any = { panels: { [PID]: {} } };
    const m: Mocks = {
      state,
      loadProjectSettings: vi.fn(async () => {}),
      api: vi.fn(async () => list),
      getPanelState: () => state.panels[PID],
      setVtabCountBadge: vi.fn(),
      renderConstitutionWarning: vi.fn(),
      toast: vi.fn(),
      supersedePinnedDecision: vi.fn(),
      _DECISION_CATEGORY_COLORS: { TECHNICAL: "#38bdf8" },
      _DECISION_PRIORITY_ORDER: ["urgent", "normal", "low"],
      _DECISION_PRIORITY_COLORS: { normal: "#888", urgent: "#f00", low: "#0f0" },
    };
    // The Cancel handler calls loadPinnedDecisions again: it resolves to this same function.
    const { loadPinnedDecisions } = loadDashboardFunctions(DASH, ["loadPinnedDecisions"], m);
    return { m, load: loadPinnedDecisions };
  }

  it("a repaint while an editor is open leaves the draft alone; Cancel catches the list up", async () => {
    const { m, load } = setupDecisions(items);
    await load(PID);
    const host = document.getElementById(`pinned-decisions-${PID}`)!;
    (host.querySelector(".decision-body-view") as HTMLElement).click(); // open the editor
    const body = host.querySelector(".decision-edit-body") as HTMLTextAreaElement;
    body.value = "my unsaved rewrite";

    // An event arrives: a second decision was pinned elsewhere.
    m.api.mockResolvedValue([...items, { ...items[0], id: "d2", title: "second", body: "second body" }]);
    await load(PID);
    expect((host.querySelector(".decision-edit-body") as HTMLTextAreaElement).value).toBe("my unsaved rewrite");
    expect(host.querySelectorAll("[data-decision-card]")).toHaveLength(1); // not repainted

    // Cancel closes the editor and the deferred repaint lands.
    (host.querySelector(".decision-edit-cancel") as HTMLElement).click();
    await vi.waitFor(() => expect(host.querySelectorAll("[data-decision-card]")).toHaveLength(2));
  });

  it("with no editor open the list repaints as before", async () => {
    const { m, load } = setupDecisions(items);
    await load(PID);
    m.api.mockResolvedValue([...items, { ...items[0], id: "d2", title: "second", body: "second body" }]);
    await load(PID);
    expect(document.querySelectorAll("[data-decision-card]")).toHaveLength(2);
  });
});
