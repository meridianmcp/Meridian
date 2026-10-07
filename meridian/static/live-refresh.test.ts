// 8a665a03 -- live-refresh behaviour of the dashboard.
//
// Owner bug: permanently deleting an item in the Backburner (Queue tab) did not
// live-refresh -- the row stayed until a reload. Root cause: the trash handler repainted
// only the Live tab, and the DELETE route published no event, so no other path ever told
// the Queue tab. The audit found the same shape (a mutation that never reaches one of the
// views showing its data) in eight sibling places; each is pinned here.
//
// The code under test is the REAL dashboard.ts: source-harness.ts lifts the named
// top-level functions out of it (dashboard.ts itself cannot be imported -- it is a 13.8k
// line script that wires the DOM at module scope) and evaluates them against the mocks
// below. File name deliberately has no `dashboard-` prefix: tests/dashboard_src.py
// concatenates every dashboard-*.ts into the text the source-scanning pytest checks, and
// this file spells out strings (live-pause-, ...) those checks assert are ABSENT.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { loadDashboardFunctions, topLevelFunctionSource } from "./source-harness";
// The REAL Queue renderer: the filter / repaint tests below must see the markup that ships
// (the filter input, data-bb-title rows, bb-group wrappers), not a stand-in. The sprint
// module reads escapeHtml / getPanelState / formatRelativeTime / QUEUE_DONE_PAGE_SIZE as
// window globals, which dashboard-utils installs on import (as the shipped bundle does).
import "./dashboard-utils";
import { renderQueue as realRenderQueue } from "./dashboard-sprint";

const DASH = "meridian/static/dashboard.ts";
const PID = "proj-live-1";
const OTHER = "proj-live-2";

type Mocks = Record<string, any>;

/** Fresh dashboard state: one project panel on the given vtab. */
function makeState(activeVtab: string, extra: Record<string, any> = {}) {
  return {
    panels: { [PID]: { activeVtab, taskCache: [], ...extra } } as Record<string, any>,
    tabs: [] as any[],
    projects: [] as any[],
  };
}

/** Mocks for every collaborator handleWsEvent / the sprint handlers call. */
function makeMocks(state: any, over: Mocks = {}): Mocks {
  return {
    state,
    api: vi.fn(async () => null),
    toast: vi.fn(),
    confirm: vi.fn(() => true),
    isDemoMode: vi.fn(() => false),
    hideDemoAdminControls: vi.fn(),
    renderTabs: vi.fn(),
    loadProjects: vi.fn(),
    loadQueue: vi.fn(async () => {}),
    refreshLiveTab: vi.fn(async () => {}),
    scheduleLiveRefresh: vi.fn(),
    refreshGoal: vi.fn(),
    refreshSessions: vi.fn(),
    refreshProjectCountBadges: vi.fn(),
    refreshHitl: vi.fn(),
    loadHitlTab: vi.fn(),
    loadNotesTab: vi.fn(),
    loadPinnedDecisions: vi.fn(),
    loadInsightsTab: vi.fn(),
    renderTasks: vi.fn(),
    updateLiveFeed: vi.fn(),
    repaintVisibleSprintViews: vi.fn(),
    scheduleGoalBoardReload: vi.fn(),
    reloadGoalSprintBoard: vi.fn(),
    applySprintItemDeleted: vi.fn(),
    dropTaskFromCaches: vi.fn(),
    refreshSprintSurfaces: vi.fn(async () => {}),
    renderQueue: vi.fn(),
    renderQueueBody: vi.fn(),
    wireQueueSectionToggles: vi.fn(),
    closeTab: vi.fn(),
    refreshTab: vi.fn(async () => {}),
    resyncProjectViews: vi.fn(),
    QUEUE_DONE_PAGE_SIZE: 25,
    _sprintBoardReloaders: {} as Record<string, any>,
    _repaintTimers: {} as Record<string, any>,
    ...over,
  };
}

function load(names: string[], mocks: Mocks) {
  return loadDashboardFunctions(DASH, names, mocks) as Record<string, (...a: any[]) => any>;
}

// ---------------------------------------------------------------------------
// handleWsEvent routing -- every event type repaints the views that show its data
// ---------------------------------------------------------------------------
describe("handleWsEvent: one event, every view that lists the data", () => {
  let state: ReturnType<typeof makeState>;
  let m: Mocks;
  // Note and decision events are coalesced through _debounceRepaint (real here), so the
  // fake clock is advanced past the window after each event.
  const route = (vtab: string, event: any, pid = PID) => {
    state = makeState(vtab);
    m = makeMocks(state);
    const { handleWsEvent } = load(["handleWsEvent", "_debounceRepaint"], m);
    handleWsEvent(pid, event);
    vi.advanceTimersByTime(200);
  };

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  describe.each(["sprint_item_updated", "sprint_item_added", "sprint_items_fanned_out"])("%s", (type) => {
    it("repaints the visible sprint views and schedules the Live refresh", () => {
      route("queue", { type });
      expect(m.repaintVisibleSprintViews).toHaveBeenCalledWith(PID);
      expect(m.scheduleLiveRefresh).toHaveBeenCalledWith(PID);
    });
    it("always delegates to the shared repaint (it decides which views are on screen)", () => {
      route("status", { type });
      expect(m.repaintVisibleSprintViews).toHaveBeenCalledWith(PID);
    });
  });

  it("sprint_item_added / fanned_out also refresh the count badges", () => {
    route("queue", { type: "sprint_item_added" });
    expect(m.refreshProjectCountBadges).toHaveBeenCalledWith(PID);
    route("queue", { type: "sprint_items_fanned_out" });
    expect(m.refreshProjectCountBadges).toHaveBeenCalledWith(PID);
  });

  it("sprint_item_deleted drops the row from the caches and repaints (every tab, every session)", () => {
    route("queue", { type: "sprint_item_deleted", item_id: "it-1" });
    expect(m.applySprintItemDeleted).toHaveBeenCalledWith(PID, "it-1");
    expect(m.scheduleLiveRefresh).toHaveBeenCalledWith(PID);
  });

  it("sprint_item_deleted also schedules a Goal-tab board reload (the scheduler skips it unless that tab is visible)", () => {
    route("goal", { type: "sprint_item_deleted", item_id: "it-1" });
    expect(m.scheduleGoalBoardReload).toHaveBeenCalledWith(PID);
  });

  describe.each(["session_started", "session_updated"])("%s", (type) => {
    it("also refreshes the Status drawer's Active Sessions list", () => {
      route("status", { type });
      expect(m.refreshSessions).toHaveBeenCalledWith(PID);
      expect(m.scheduleLiveRefresh).toHaveBeenCalledWith(PID);
    });
    it("repaints the Queue tab quietly when it is visible", () => {
      route("queue", { type });
      expect(m.loadQueue).toHaveBeenCalledWith(PID, { quiet: true });
    });
  });

  describe.each(["note_added", "note_updated", "note_deleted"])("%s", (type) => {
    it("reloads the Notes tab when it is visible and refreshes the badges", () => {
      route("notes", { type });
      expect(m.loadNotesTab).toHaveBeenCalledWith(PID);
      expect(m.refreshProjectCountBadges).toHaveBeenCalledWith(PID);
    });
    it("only refreshes the badges when the Notes tab is hidden", () => {
      route("status", { type });
      expect(m.loadNotesTab).not.toHaveBeenCalled();
      expect(m.refreshProjectCountBadges).toHaveBeenCalledWith(PID);
    });
  });

  describe.each(["decision_pinned", "decision_updated", "decision_deleted"])("%s", (type) => {
    it("reloads the pinned-decisions list and the badges", () => {
      route("goal", { type });
      expect(m.loadPinnedDecisions).toHaveBeenCalledWith(PID);
      expect(m.refreshProjectCountBadges).toHaveBeenCalledWith(PID);
    });
  });

  it("a burst of decision events (replace-all / archive-oldest) reloads the list once", () => {
    state = makeState("goal");
    m = makeMocks(state);
    const { handleWsEvent } = load(["handleWsEvent", "_debounceRepaint"], m);
    for (let i = 0; i < 20; i++) handleWsEvent(PID, { type: i % 2 ? "decision_updated" : "decision_pinned" });
    vi.advanceTimersByTime(500);
    expect(m.loadPinnedDecisions).toHaveBeenCalledTimes(1);
    expect(m.refreshProjectCountBadges).toHaveBeenCalledTimes(1);
  });

  it("a burst of note events (document re-ingest) reloads the Notes tab once", () => {
    state = makeState("notes");
    m = makeMocks(state);
    const { handleWsEvent } = load(["handleWsEvent", "_debounceRepaint"], m);
    for (let i = 0; i < 20; i++) handleWsEvent(PID, { type: "note_updated" });
    vi.advanceTimersByTime(500);
    expect(m.loadNotesTab).toHaveBeenCalledTimes(1);
  });

  it("insight_added reloads the Insights tab when it is visible", () => {
    route("insights", { type: "insight_added", insight_id: "i1", horizon: "year" });
    expect(m.loadInsightsTab).toHaveBeenCalledWith(PID);
  });

  it("insight_added leaves a hidden Insights tab alone (it reloads when opened)", () => {
    route("status", { type: "insight_added", insight_id: "i1", horizon: "year" });
    expect(m.loadInsightsTab).not.toHaveBeenCalled();
  });

  it("task_deleted forgets the task in this project's cache and repaints the Devlog", () => {
    route("devlog", { type: "task_deleted", task_id: "t-9" });
    expect(m.dropTaskFromCaches).toHaveBeenCalledWith("t-9", PID);
    expect(m.renderTasks).toHaveBeenCalledWith(PID);
    expect(m.scheduleLiveRefresh).toHaveBeenCalledWith(PID);
  });

  it("task_deleted refreshes the Queue tab's live feed when that tab is visible", () => {
    route("queue", { type: "task_deleted", task_id: "t-9" });
    expect(m.updateLiveFeed).toHaveBeenCalledWith(PID);
  });

  it("project_icon_changed updates the tab, the project list and re-renders the tab strip", () => {
    state = makeState("status");
    state.tabs = [{ id: PID, project: { id: PID, name: "P", icon: null } }];
    state.projects = [{ id: PID, name: "P", icon: null }];
    m = makeMocks(state);
    load(["handleWsEvent"], m).handleWsEvent(PID, { type: "project_icon_changed", project_id: PID, icon: "R" });
    expect(state.tabs[0].project.icon).toBe("R");
    expect(state.projects[0].icon).toBe("R");
    expect(m.renderTabs).toHaveBeenCalled();
  });

  it("project_parent_changed reloads the project list", () => {
    state = makeState("status");
    state.tabs = [{ id: PID, project: { id: PID, name: "P", parent_project_id: null } }];
    m = makeMocks(state);
    load(["handleWsEvent"], m).handleWsEvent(PID, { type: "project_parent_changed", project_id: PID, parent_project_id: OTHER });
    expect(state.tabs[0].project.parent_project_id).toBe(OTHER);
    expect(m.loadProjects).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The shared sprint repaint helpers
// ---------------------------------------------------------------------------
describe("repaintVisibleSprintViews / refreshSprintSurfaces", () => {
  const helpers = [
    "repaintVisibleSprintViews", "refreshSprintSurfaces", "reloadGoalSprintBoard",
    "scheduleGoalBoardReload", "_debounceRepaint",
  ];

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("repaints the Queue quietly when the Queue tab is visible", () => {
    const state = makeState("queue");
    const m = makeMocks(state);
    load(helpers, m).repaintVisibleSprintViews(PID);
    vi.advanceTimersByTime(200);
    expect(m.loadQueue).toHaveBeenCalledTimes(1);
    expect(m.loadQueue).toHaveBeenCalledWith(PID, { quiet: true });
  });

  it("reloads the Goal-tab sprint board when the Goal tab is visible", () => {
    const state = makeState("goal");
    const reload = vi.fn(async () => {});
    const m = makeMocks(state, { _sprintBoardReloaders: { [PID]: reload } });
    load(helpers, m).repaintVisibleSprintViews(PID);
    vi.advanceTimersByTime(200);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(m.loadQueue).not.toHaveBeenCalled();
  });

  it("skips views that are not on screen", () => {
    const state = makeState("status");
    const reload = vi.fn(async () => {});
    const m = makeMocks(state, { _sprintBoardReloaders: { [PID]: reload } });
    load(helpers, m).repaintVisibleSprintViews(PID);
    vi.advanceTimersByTime(1000);
    expect(m.loadQueue).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it("is a no-op for a project with no open panel", () => {
    const state = makeState("queue");
    const m = makeMocks(state);
    expect(() => load(helpers, m).repaintVisibleSprintViews("never-opened")).not.toThrow();
    vi.advanceTimersByTime(1000);
    expect(m.loadQueue).not.toHaveBeenCalled();
  });

  it("coalesces a burst of events (a batch of N item writes) into one repaint per view", () => {
    const state = makeState("queue");
    const reload = vi.fn(async () => {});
    const m = makeMocks(state, { _sprintBoardReloaders: { [PID]: reload } });
    const fns = load(helpers, m);
    for (let i = 0; i < 50; i++) fns.repaintVisibleSprintViews(PID);
    vi.advanceTimersByTime(500);
    expect(m.loadQueue).toHaveBeenCalledTimes(1);
    state.panels[PID].activeVtab = "goal";
    for (let i = 0; i < 50; i++) fns.repaintVisibleSprintViews(PID);
    vi.advanceTimersByTime(500);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("a burst for one project never swallows another project's repaint", () => {
    const state = makeState("queue");
    state.panels[OTHER] = { activeVtab: "queue", taskCache: [] };
    const m = makeMocks(state);
    const fns = load(helpers, m);
    fns.repaintVisibleSprintViews(PID);
    fns.repaintVisibleSprintViews(OTHER);
    vi.advanceTimersByTime(200);
    expect(m.loadQueue.mock.calls.map((c: any[]) => c[0]).sort()).toEqual([PID, OTHER].sort());
  });

  it("a failing Goal-board reload never throws into the caller", () => {
    const state = makeState("goal");
    const m = makeMocks(state, { _sprintBoardReloaders: { [PID]: () => { throw new Error("boom"); } } });
    expect(() => load(helpers, m).reloadGoalSprintBoard(PID)).not.toThrow();
    const rejecting = makeMocks(state, { _sprintBoardReloaders: { [PID]: () => Promise.reject(new Error("nope")) } });
    expect(() => load(helpers, rejecting).reloadGoalSprintBoard(PID)).not.toThrow();
  });

  it("refreshSprintSurfaces repaints the visible views AND always refreshes the Live tab", async () => {
    const state = makeState("queue");
    const m = makeMocks(state);
    await load(helpers, m).refreshSprintSurfaces(PID);
    expect(m.refreshLiveTab).toHaveBeenCalledWith(PID);
    vi.advanceTimersByTime(200);
    expect(m.loadQueue).toHaveBeenCalledWith(PID, { quiet: true });
  });
});

// ---------------------------------------------------------------------------
// Local mutation handlers: each ends in the shared repaint, not a Live-only one
// ---------------------------------------------------------------------------
describe("sprint mutation handlers repaint every view", () => {
  it("sprintAction (complete / skip / fail) refreshes through refreshSprintSurfaces", async () => {
    const state = makeState("queue");
    const m = makeMocks(state);
    await load(["sprintAction"], m).sprintAction(PID, "it-1", "skip");
    expect(m.api).toHaveBeenCalledWith(`/projects/${PID}/sprint-items/it-1/skip`, expect.objectContaining({ method: "POST" }));
    expect(m.refreshSprintSurfaces).toHaveBeenCalledWith(PID);
    expect(m.refreshLiveTab).not.toHaveBeenCalled();
  });

  it("sprintPushPrompt refreshes through refreshSprintSurfaces", async () => {
    const state = makeState("queue");
    const m = makeMocks(state);
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("v9.0");
    await load(["sprintPushPrompt"], m).sprintPushPrompt(PID, "it-1");
    expect(m.api).toHaveBeenCalledWith(`/projects/${PID}/sprint-items/it-1/push`, expect.objectContaining({ method: "POST" }));
    expect(m.refreshSprintSurfaces).toHaveBeenCalledWith(PID);
    prompt.mockRestore();
  });

  it("sprintResetPending (the 'Back to pending' button) PATCHes and repaints", async () => {
    const state = makeState("live");
    const m = makeMocks(state);
    await load(["sprintResetPending"], m).sprintResetPending(PID, "it-1");
    expect(m.api).toHaveBeenCalledWith(
      `/projects/${PID}/sprint-items/it-1`,
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ status: "pending" }) }),
    );
    expect(m.refreshSprintSurfaces).toHaveBeenCalledWith(PID);
  });

  it("sprintFeedback / sprintFeedbackNote refresh through refreshSprintSurfaces", async () => {
    const state = makeState("live");
    const m = makeMocks(state);
    const { sprintFeedback, sprintFeedbackNote } = load(["sprintFeedback", "sprintFeedbackNote"], m);
    await sprintFeedback(PID, "it-1", 1, null, null);
    await sprintFeedbackNote(PID, "it-1", " good ");
    expect(m.refreshSprintSurfaces).toHaveBeenCalledTimes(2);
    expect(m.refreshLiveTab).not.toHaveBeenCalled();
  });

  it("a failed action toasts the error and repaints nothing", async () => {
    const state = makeState("queue");
    const m = makeMocks(state, { api: vi.fn(async () => { throw new Error("409: conflict"); }) });
    await load(["sprintAction"], m).sprintAction(PID, "it-1", "complete");
    expect(m.toast).toHaveBeenCalledWith(expect.stringContaining("409"), true);
    expect(m.refreshSprintSurfaces).not.toHaveBeenCalled();
  });

  // The DOM-heavy inline editors cannot be driven through the harness; pin the
  // property that matters instead: none of them ends in a Live-only repaint.
  it.each([
    "sprintAction", "sprintArchive", "sprintPushPrompt", "sprintResetPending", "sprintFeedback",
    "sprintFeedbackNote", "sprintItemEdit", "sprintItemNotesEdit", "sprintItemResourcesEdit",
    "addSprintItemFromInput",
  ])("%s repaints through refreshSprintSurfaces, never refreshLiveTab alone", (name) => {
    const src = topLevelFunctionSource(DASH, [name])[name];
    expect(src).toContain("refreshSprintSurfaces(");
    expect(src).not.toContain("refreshLiveTab(");
  });
});

// ---------------------------------------------------------------------------
// The headline bug: delete a Backburner row -> the Queue repaints without a reload
// ---------------------------------------------------------------------------
describe("permanent delete in the Backburner (Queue tab)", () => {
  const names = [
    "sprintArchive", "refreshSprintSurfaces", "repaintVisibleSprintViews", "reloadGoalSprintBoard",
    "scheduleGoalBoardReload", "_debounceRepaint", "applySprintItemDeleted", "renderQueueBody",
    "applyBackburnerFilter", "queueSearchActive",
  ];
  const items = () => [
    { id: "bb-1", title: "backburner one", status: "skipped" },
    { id: "bb-2", title: "backburner two", status: "pushed" },
    { id: "p-1", title: "pending one", status: "pending" },
  ];
  // Stand-in for renderQueue (lives in dashboard-sprint.ts): one element per item.
  const renderQueue = (_pid: string, list: any[]) =>
    `<div class="queue-section">${list.map((i) => `<div class="queue-item" data-id="${i.id}">${i.title}</div>`).join("")}</div>`;

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = `<div id="queue-body-${PID}"></div>`;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function setup(vtab: string, over: Mocks = {}) {
    const state = makeState(vtab, { queueSprintItems: items(), queueTotalDoneCount: 0 });
    const m = makeMocks(state, { renderQueue, ...over });
    // Real renderQueueBody / applySprintItemDeleted / repaint helpers; only the fetching
    // loaders are mocks.
    const fns = load(names, m);
    fns.renderQueueBody(PID);
    return { state, m, fns };
  }

  const rows = () => Array.from(document.querySelectorAll(`#queue-body-${PID} .queue-item`)).map((e) => (e as HTMLElement).dataset.id);

  it("removes the row from the Queue tab without a reload", async () => {
    const { m, fns, state } = setup("queue");
    expect(rows()).toEqual(["bb-1", "bb-2", "p-1"]);
    await fns.sprintArchive(PID, "bb-1");
    expect(m.api).toHaveBeenCalledWith(`/projects/${PID}/sprint-items/bb-1`, { method: "DELETE" });
    expect(rows()).toEqual(["bb-2", "p-1"]);
    expect(state.panels[PID].queueSprintItems.map((i: any) => i.id)).toEqual(["bb-2", "p-1"]);
  });

  it("also repaints the Live tab and re-syncs the Queue quietly", async () => {
    const { m, fns } = setup("queue");
    await fns.sprintArchive(PID, "bb-1");
    vi.advanceTimersByTime(200);
    expect(m.refreshLiveTab).toHaveBeenCalledWith(PID);
    expect(m.loadQueue).toHaveBeenCalledWith(PID, { quiet: true });
  });

  it("reloads the Goal-tab sprint board when that tab is the visible one", async () => {
    const reload = vi.fn(async () => {});
    const { fns } = setup("goal", { _sprintBoardReloaders: { [PID]: reload } });
    await fns.sprintArchive(PID, "bb-1");
    vi.advanceTimersByTime(200);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("drops the cached row even when the Queue tab is hidden, so reopening never revives it", async () => {
    const { fns, state } = setup("live");
    await fns.sprintArchive(PID, "bb-2");
    expect(state.panels[PID].queueSprintItems.map((i: any) => i.id)).toEqual(["bb-1", "p-1"]);
  });

  it("does nothing when the confirmation is declined", async () => {
    const { m, fns } = setup("queue", { confirm: vi.fn(() => false) });
    await fns.sprintArchive(PID, "bb-1");
    expect(m.api).not.toHaveBeenCalled();
    expect(rows()).toEqual(["bb-1", "bb-2", "p-1"]);
  });

  it("keeps the row and toasts when the DELETE fails", async () => {
    const { m, fns, state } = setup("queue", { api: vi.fn(async () => { throw new Error("500: nope"); }) });
    await fns.sprintArchive(PID, "bb-1");
    expect(m.toast).toHaveBeenCalledWith(expect.stringContaining("Delete failed"), true);
    expect(rows()).toEqual(["bb-1", "bb-2", "p-1"]);
    expect(state.panels[PID].queueSprintItems).toHaveLength(3);
  });

  it("a sprint_item_deleted event (another tab / session / MCP) repaints this tab from its cache", () => {
    const { m, state } = setup("queue");
    const { handleWsEvent } = loadDashboardFunctions(DASH, ["handleWsEvent", ...names], m) as Record<string, any>;
    handleWsEvent(PID, { type: "sprint_item_deleted", project_id: PID, item_id: "bb-2" });
    expect(rows()).toEqual(["bb-1", "p-1"]);
    expect(state.panels[PID].queueSprintItems.map((i: any) => i.id)).toEqual(["bb-1", "p-1"]);
    // Repainted from the cache: no refetch of the whole list for a delete.
    expect(m.loadQueue).not.toHaveBeenCalled();
  });

  it("deleting a completed item keeps the 'N completed' counter honest", () => {
    const state = makeState("queue", {
      queueSprintItems: [{ id: "d-1", title: "done", status: "done" }, { id: "d-2", title: "done 2", status: "done" }],
      queueTotalDoneCount: 7,
    });
    const m = makeMocks(state, { renderQueue });
    const fns = load(names, m);
    fns.applySprintItemDeleted(PID, "d-1");
    expect(state.panels[PID].queueTotalDoneCount).toBe(6);
  });

  it("ignores a delete event for a project with no open panel", () => {
    const state = makeState("queue", { queueSprintItems: items() });
    const m = makeMocks(state, { renderQueue });
    const fns = load(names, m);
    expect(() => fns.applySprintItemDeleted("never-opened", "bb-1")).not.toThrow();
    expect(state.panels[PID].queueSprintItems).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// Devlog task delete: cache + event
// ---------------------------------------------------------------------------
describe("Devlog task delete", () => {
  beforeEach(() => {
    document.body.innerHTML = `<div id="task-row-t1">row</div><div id="task-row-t2">row</div>`;
  });

  it("dropTaskFromCaches removes the id from the cache and keeps the offset aligned", () => {
    const state = makeState("devlog", { taskCache: [{ id: "t1" }, { id: "t2" }, { id: "t3" }], taskOffset: 3 });
    state.panels[OTHER] = { taskCache: [{ id: "t1" }], taskOffset: 1 };
    const m = makeMocks(state);
    const { dropTaskFromCaches } = load(["dropTaskFromCaches"], m);
    dropTaskFromCaches("t1");
    expect(state.panels[PID].taskCache.map((t: any) => t.id)).toEqual(["t2", "t3"]);
    expect(state.panels[PID].taskOffset).toBe(2);
    expect(state.panels[OTHER].taskCache).toEqual([]);
    expect(state.panels[OTHER].taskOffset).toBe(0);
  });

  it("can be scoped to one project and ignores an id the cache never held", () => {
    const state = makeState("devlog", { taskCache: [{ id: "t1" }], taskOffset: 1 });
    state.panels[OTHER] = { taskCache: [{ id: "t1" }], taskOffset: 1 };
    const m = makeMocks(state);
    const { dropTaskFromCaches } = load(["dropTaskFromCaches"], m);
    dropTaskFromCaches("t1", PID);
    expect(state.panels[PID].taskCache).toEqual([]);
    expect(state.panels[OTHER].taskCache).toHaveLength(1);
    dropTaskFromCaches("not-there", PID);
    expect(state.panels[PID].taskOffset).toBe(0);
  });

  it("deleteTaskRow removes the DOM row AND the cached task", async () => {
    const state = makeState("devlog", { taskCache: [{ id: "t1" }, { id: "t2" }], taskOffset: 2 });
    const m = makeMocks(state);
    // real dropTaskFromCaches, mocked network
    const { deleteTaskRow } = load(["deleteTaskRow", "dropTaskFromCaches"], m);
    await deleteTaskRow({ stopPropagation() {} }, "t1", "done");
    expect(m.api).toHaveBeenCalledWith("/tasks/t1", { method: "DELETE" });
    expect(document.getElementById("task-row-t1")).toBeNull();
    expect(state.panels[PID].taskCache.map((t: any) => t.id)).toEqual(["t2"]);
    expect(state.panels[PID].taskOffset).toBe(1);
  });

  it("deleteTaskRow keeps everything when the confirmation is declined", async () => {
    const state = makeState("devlog", { taskCache: [{ id: "t1" }], taskOffset: 1 });
    const m = makeMocks(state, { confirm: vi.fn(() => false) });
    const { deleteTaskRow } = load(["deleteTaskRow", "dropTaskFromCaches"], m);
    await deleteTaskRow({ stopPropagation() {} }, "t1", "pending");
    expect(m.api).not.toHaveBeenCalled();
    expect(state.panels[PID].taskCache).toHaveLength(1);
  });

  it("a stale cache can no longer resurrect a deleted row on the next task event", async () => {
    // The old behaviour: delete removed the DOM row only; the next task_updated event
    // re-rendered the whole list from the cache, bringing the row back.
    const state = makeState("devlog", { taskCache: [{ id: "t1" }, { id: "t2" }], taskOffset: 2 });
    const rendered: string[][] = [];
    const m = makeMocks(state, {
      renderTasks: vi.fn((pid: string) => rendered.push(state.panels[pid].taskCache.map((t: any) => t.id))),
    });
    const { deleteTaskRow, handleWsEvent } = load(["deleteTaskRow", "dropTaskFromCaches", "handleWsEvent"], m);
    await deleteTaskRow({ stopPropagation() {} }, "t1", "done");
    handleWsEvent(PID, { type: "task_updated", task: { id: "t2", status: "done" } });
    expect(rendered[rendered.length - 1]).toEqual(["t2"]);
  });
});

// ---------------------------------------------------------------------------
// Removed placeholders and the inline-handler window exports
// ---------------------------------------------------------------------------
describe("Live tab header", () => {
  it("no longer renders or wires the Pause / Run All stub buttons", () => {
    const dash = readSource(DASH);
    expect(dash).not.toContain("live-pause-");
    expect(dash).not.toContain("live-run-");
    expect(dash).not.toContain("is a stub");
    expect(dash).not.toContain("UI stub");
    // The real auto-refresh toggle stays.
    expect(dash).toContain("live-auto-btn-");
  });
});

describe("inline sprint handlers are reachable from markup", () => {
  it("every function the sprint markup calls from onclick is exported on window", () => {
    const dash = readSource(DASH);
    const sprint = readSource("meridian/static/dashboard-sprint.ts");
    const exported = new Set(
      (dash.match(/Object\.assign\(window, \{([^}]*)\}/s)?.[1] ?? "").split(",").map((s) => s.trim()).filter(Boolean),
    );
    const called = new Set<string>();
    for (const m of sprint.matchAll(/on(?:click|input|change)="(\w+)\(/g)) called.add(m[1]);
    const missing = [...called].filter((n) => !exported.has(n) && !sprint.includes(`window.${n}`));
    expect(missing).toEqual([]);
    // Spot-check the three that were silently dead before this change.
    for (const n of ["sprintItemNotesEdit", "sprintItemResourcesEdit", "resourceChipClick"]) {
      expect(exported.has(n)).toBe(true);
    }
  });

  it("the 'Back to pending' button no longer relies on out-of-scope variables", () => {
    const sprint = readSource("meridian/static/dashboard-sprint.ts");
    expect(sprint).toContain("sprintResetPending(");
    expect(sprint).not.toContain("items.map(x=>x.id===it.id");
  });
});

// ---------------------------------------------------------------------------
// A repaint must not reset the view state the user built up (Backburner filter)
// ---------------------------------------------------------------------------
describe("Backburner filter survives every Queue repaint", () => {
  const names = [
    "filterBackburner", "applyBackburnerFilter", "renderQueueBody", "applySprintItemDeleted",
    "queueSearchActive", "loadQueue", "handleWsEvent", "_debounceRepaint",
  ];
  const TITLES = ["filterme one", "filterme two", "other three", "bb alpha"];
  const rowItems = () => [
    ...TITLES.map((title, i) => ({
      id: `bb-${i}`, title, status: "skipped", item_group: i < 2 ? "grp-a" : "grp-b",
    })),
    { id: "p-1", title: "pending one", status: "pending" },
  ];
  const sectionState = () => ({ backburner: false, pending: false, in_progress: false, done: true, failed: true });

  function setup(extra: Record<string, any> = {}) {
    document.body.innerHTML = `<div id="queue-body-${PID}"></div><input id="task-search-${PID}">`;
    const state = makeState("queue", {
      queueSprintItems: rowItems(), queueTotalDoneCount: 0, queueSectionState: sectionState(), ...extra,
    });
    (window as any).state = state; // renderQueue reads panel state through window.state
    const m = makeMocks(state, {
      renderQueue: realRenderQueue,
      projectApi: vi.fn(async (_pid: string, path: string) =>
        path.includes("sprint-items") ? { items: state.panels[PID].queueSprintItems, total_done_count: 0 } : []),
      isLiveSession: vi.fn(() => false),
      loadRecentSessions: vi.fn(),
      renderSearchResults: vi.fn(() => '<div class="search-hit">hit</div>'),
      renderProjectLoadError: vi.fn(() => "err"),
      wireProjectLoadRetry: vi.fn(),
      runReconcile: vi.fn(),
      getPanelState: (pid: string) => state.panels[pid],
    });
    const fns = load(names, m);
    fns.renderQueueBody(PID);
    return { state, m, fns };
  }

  const bbRows = (pid = PID) =>
    Array.from(document.querySelectorAll(`#queue-body-${pid} .queue-section[data-section="backburner"] .queue-item`)) as HTMLElement[];
  const shown = (pid = PID) => bbRows(pid).filter((e) => e.style.display !== "none").map((e) => e.dataset.bbTitle);
  const filterInput = (pid = PID) => document.getElementById(`backburner-search-${pid}`) as HTMLInputElement;

  /** What the oninput attribute does when the user types. */
  const type = (fns: any, text: string, pid = PID) => {
    filterInput(pid).value = text;
    fns.filterBackburner(pid, text);
  };

  it("hides the rows that do not match while typing", () => {
    const { fns } = setup();
    type(fns, "filterme");
    expect(shown()).toEqual(["filterme one", "filterme two"]);
  });

  it("deleting a visible row keeps the filter text AND keeps the other rows hidden", () => {
    // The verifier's repro: type a filter, trash a visible row -> the whole body was
    // rebuilt, the new input was empty and every hidden row came back.
    const { fns, state } = setup();
    type(fns, "filterme");
    fns.applySprintItemDeleted(PID, "bb-0");
    expect(filterInput().value).toBe("filterme");
    expect(shown()).toEqual(["filterme two"]);
    expect(bbRows().map((e) => e.dataset.bbTitle)).toContain("other three"); // present, just hidden
    expect(state.panels[PID].backburnerFilter).toBe("filterme");
  });

  it("an empty group header disappears with its last visible row, and the filter outlives that too", () => {
    const { fns } = setup();
    type(fns, "filterme");
    const groups = () => Array.from(document.querySelectorAll(`#queue-body-${PID} .bb-group`)) as HTMLElement[];
    expect(groups().filter((g) => g.style.display !== "none")).toHaveLength(1); // only grp-a
    fns.applySprintItemDeleted(PID, "bb-0");
    fns.applySprintItemDeleted(PID, "bb-1");
    expect(shown()).toEqual([]);
    expect(groups().every((g) => g.style.display === "none")).toBe(true);
    expect(filterInput().value).toBe("filterme");
  });

  it("deleting every visible row one after another never resurrects a hidden one", () => {
    const { fns } = setup();
    type(fns, "filterme");
    for (const id of ["bb-0", "bb-1"]) {
      fns.applySprintItemDeleted(PID, id);
      expect(shown().every((t) => (t as string).startsWith("filterme"))).toBe(true);
    }
    expect(shown()).toEqual([]);
  });

  it("a sprint_item_deleted event from another tab / session keeps the filter", () => {
    const { fns, state } = setup();
    type(fns, "alpha");
    expect(shown()).toEqual(["bb alpha"]);
    fns.handleWsEvent(PID, { type: "sprint_item_deleted", item_id: "bb-1" });
    expect(shown()).toEqual(["bb alpha"]);
    expect(filterInput().value).toBe("alpha");
    expect(state.panels[PID].queueSprintItems.map((i: any) => i.id)).not.toContain("bb-1");
  });

  it("a quiet loadQueue repaint (any WebSocket event) keeps the filter", async () => {
    const { fns } = setup();
    type(fns, "filterme");
    await fns.loadQueue(PID, { quiet: true });
    expect(filterInput().value).toBe("filterme");
    expect(shown()).toEqual(["filterme one", "filterme two"]);
  });

  it("matches the group name as well as the title", () => {
    const { fns } = setup();
    type(fns, "grp-b");
    expect(shown()).toEqual(["other three", "bb alpha"]);
  });

  it("clearing the filter shows every row again, and the cleared state persists across a repaint", () => {
    const { fns, state } = setup();
    type(fns, "filterme");
    type(fns, "");
    expect(shown()).toHaveLength(4);
    fns.renderQueueBody(PID);
    expect(shown()).toHaveLength(4);
    expect(state.panels[PID].backburnerFilter).toBe("");
  });

  it("keeps the filter box focused, with its caret, across a repaint that rebuilds it", () => {
    const { fns } = setup();
    const before = filterInput();
    before.focus();
    type(fns, "filterme");
    before.setSelectionRange(3, 6);
    fns.renderQueueBody(PID);
    const after = filterInput();
    expect(after).not.toBe(before); // a NEW node: this is what used to lose everything
    expect(document.activeElement).toBe(after);
    expect([after.selectionStart, after.selectionEnd]).toEqual([3, 6]);
  });

  it("does not steal focus when the filter box was not focused", () => {
    const { fns } = setup();
    const other = document.getElementById(`task-search-${PID}`) as HTMLInputElement;
    other.focus();
    type(fns, "filterme");
    fns.renderQueueBody(PID);
    expect(document.activeElement).toBe(other);
  });

  it("renders a filter containing quotes and markup back into the input without breaking the page", () => {
    const { fns } = setup();
    type(fns, `"><img src=x onerror=alert(1)>`);
    fns.renderQueueBody(PID);
    expect(filterInput().value).toBe(`"><img src=x onerror=alert(1)>`);
    expect(document.querySelector(`#queue-body-${PID} img`)).toBeNull();
  });

  it("the filter is per project: typing in one Queue never hides rows in another open project", () => {
    document.body.innerHTML = `<div id="queue-body-${PID}"></div><div id="queue-body-${OTHER}"></div>`;
    const state = makeState("queue", { queueSprintItems: rowItems(), queueSectionState: sectionState() });
    state.panels[OTHER] = {
      activeVtab: "queue", taskCache: [], queueSprintItems: rowItems(), queueSectionState: sectionState(),
    };
    (window as any).state = state;
    const m = makeMocks(state, { renderQueue: realRenderQueue, getPanelState: (pid: string) => state.panels[pid] });
    const fns = load(names, m);
    fns.renderQueueBody(PID);
    fns.renderQueueBody(OTHER);
    type(fns, "filterme", OTHER);
    expect(shown(OTHER)).toEqual(["filterme one", "filterme two"]);
    expect(shown(PID)).toHaveLength(4);
    expect(state.panels[PID].backburnerFilter).toBeUndefined();
  });

  it("keeps the Queue body's scroll offset across a repaint", () => {
    const { fns } = setup();
    const body = document.getElementById(`queue-body-${PID}`) as HTMLElement;
    body.scrollTop = 120;
    fns.renderQueueBody(PID);
    expect(body.scrollTop).toBe(120);
  });

  describe("universal search results on screen", () => {
    it("a delete event does not wipe the search results the user is reading", () => {
      const { fns, state } = setup();
      const body = document.getElementById(`queue-body-${PID}`) as HTMLElement;
      (document.getElementById(`task-search-${PID}`) as HTMLInputElement).value = "needle";
      body.innerHTML = '<div class="search-hit">hit</div>';
      fns.applySprintItemDeleted(PID, "bb-0");
      expect(body.querySelector(".search-hit")).not.toBeNull();
      // ...but the cache is already current, so clearing the search shows the right queue.
      expect(state.panels[PID].queueSprintItems.map((i: any) => i.id)).not.toContain("bb-0");
      (document.getElementById(`task-search-${PID}`) as HTMLInputElement).value = "";
      fns.renderQueueBody(PID);
      expect(bbRows().map((e) => e.dataset.bbTitle)).not.toContain("filterme one");
    });

    it("a quiet loadQueue refreshes the cache but leaves the results (and does not flash 'loading')", async () => {
      const { fns, state } = setup();
      const body = document.getElementById(`queue-body-${PID}`) as HTMLElement;
      (document.getElementById(`task-search-${PID}`) as HTMLInputElement).value = "needle";
      body.innerHTML = '<div class="search-hit">hit</div>';
      state.panels[PID].queueSprintItems = state.panels[PID].queueSprintItems.slice(1);
      await fns.loadQueue(PID, { quiet: true });
      expect(body.querySelector(".search-hit")).not.toBeNull();
      expect(body.textContent).not.toContain("loading");
    });

    it("an explicit (non-quiet) reload still repaints over the search results", async () => {
      const { fns } = setup();
      const body = document.getElementById(`queue-body-${PID}`) as HTMLElement;
      (document.getElementById(`task-search-${PID}`) as HTMLInputElement).value = "needle";
      body.innerHTML = '<div class="search-hit">hit</div>';
      await fns.loadQueue(PID);
      expect(body.querySelector(".queue-section")).not.toBeNull();
    });
  });
});

// ---------------------------------------------------------------------------
// WebSocket reconnect: events missed while the socket was down are never replayed
// ---------------------------------------------------------------------------
describe("WebSocket reconnect resyncs every view", () => {
  class FakeWebSocket {
    static instances: FakeWebSocket[] = [];
    onopen: (() => void) | null = null;
    onclose: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onmessage: ((ev: { data: string }) => void) | null = null;
    closed = false;
    constructor(public url: string) {
      FakeWebSocket.instances.push(this);
    }
    close() {
      this.closed = true;
    }
  }

  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.useFakeTimers();
    document.body.innerHTML = `<span id="ws-${PID}"></span>`;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const wsNames = ["connectWs", "resyncProjectViews", "_debounceRepaint"];
  function setup(vtab = "status", over: Mocks = {}) {
    const state = makeState(vtab);
    const m = makeMocks(state, { WebSocket: FakeWebSocket, ...over });
    // resyncProjectViews is the real one; mocks for the loaders it fans out to.
    delete (m as any).resyncProjectViews;
    const fns = load([...wsNames, "repaintVisibleSprintViews", "scheduleGoalBoardReload", "reloadGoalSprintBoard"], m);
    return { state, m, fns };
  }

  it("the first open does not resync (the tab just loaded fresh data) but marks the panel connected", () => {
    const { state, m, fns } = setup("queue");
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    expect(m.refreshTab).not.toHaveBeenCalled();
    expect(m.loadQueue).not.toHaveBeenCalled();
    expect(state.panels[PID].wsOpenedBefore).toBe(true);
    expect(document.getElementById(`ws-${PID}`)!.classList.contains("connected")).toBe(true);
  });

  it("a socket that opens AGAIN resyncs: the 1.5 s reconnect timer path", () => {
    const { m, fns } = setup("queue");
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    FakeWebSocket.instances[0].onclose!();
    expect(document.getElementById(`ws-${PID}`)!.classList.contains("connected")).toBe(false);
    vi.advanceTimersByTime(1500);
    expect(FakeWebSocket.instances).toHaveLength(2); // the reconnect
    FakeWebSocket.instances[1].onopen!();
    vi.advanceTimersByTime(300); // past the coalescing window of the debounced repaints
    expect(m.refreshTab).toHaveBeenCalledWith(PID);
    expect(m.loadQueue).toHaveBeenCalledWith(PID, { quiet: true });
    expect(m.loadPinnedDecisions).toHaveBeenCalledWith(PID);
    expect(m.refreshProjectCountBadges).toHaveBeenCalledWith(PID);
    expect(m.refreshHitl).toHaveBeenCalled();
    expect(m.loadProjects).toHaveBeenCalledTimes(1);
  });

  it("also resyncs when connectWs is called directly a second time (no timer involved)", () => {
    const { m, fns } = setup("queue");
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    fns.connectWs(PID);
    FakeWebSocket.instances[1].onopen!();
    vi.advanceTimersByTime(300);
    expect(m.refreshTab).toHaveBeenCalledTimes(1);
  });

  it("resyncs only the views that are on screen", () => {
    for (const [vtab, loader] of [["live", "refreshLiveTab"], ["notes", "loadNotesTab"], ["insights", "loadInsightsTab"]] as const) {
      const { m, fns } = setup(vtab);
      fns.connectWs(PID);
      FakeWebSocket.instances[FakeWebSocket.instances.length - 1].onopen!();
      fns.connectWs(PID);
      FakeWebSocket.instances[FakeWebSocket.instances.length - 1].onopen!();
      vi.advanceTimersByTime(300);
      expect(m[loader]).toHaveBeenCalledWith(PID);
      for (const other of ["refreshLiveTab", "loadNotesTab", "loadInsightsTab"].filter((n) => n !== loader)) {
        expect(m[other]).not.toHaveBeenCalled();
      }
    }
  });

  it("reloads the Goal-tab sprint board on reconnect when that tab is visible", () => {
    const reload = vi.fn(async () => {});
    const { fns } = setup("goal", { _sprintBoardReloaders: { [PID]: reload } });
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    fns.connectWs(PID);
    FakeWebSocket.instances[1].onopen!();
    vi.advanceTimersByTime(300);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("a failing refresh neither stops the others nor throws out of onopen", async () => {
    const { m, fns } = setup("live", { refreshTab: vi.fn(async () => { throw new Error("warming up"); }) });
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    fns.connectWs(PID);
    expect(() => FakeWebSocket.instances[1].onopen!()).not.toThrow();
    await vi.advanceTimersByTimeAsync(300);
    expect(m.loadPinnedDecisions).toHaveBeenCalledWith(PID);
    expect(m.refreshLiveTab).toHaveBeenCalledWith(PID);
  });

  it("a resync that throws synchronously still leaves the socket usable (panel stays marked)", () => {
    const { state, fns } = setup("status", { refreshHitl: vi.fn(() => { throw new Error("sync boom"); }) });
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    fns.connectWs(PID);
    expect(() => FakeWebSocket.instances[1].onopen!()).not.toThrow();
    expect(state.panels[PID].wsOpenedBefore).toBe(true);
  });

  it("does not reconnect (or resync) for a tab that was closed", () => {
    const { state, fns } = setup();
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    delete state.panels[PID];
    FakeWebSocket.instances[0].onclose!();
    vi.advanceTimersByTime(5000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("a reopen that races a closed tab neither throws nor resyncs", () => {
    const { state, m, fns } = setup();
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    fns.connectWs(PID);
    delete state.panels[PID];
    expect(() => FakeWebSocket.instances[1].onopen!()).not.toThrow();
    expect(m.refreshTab).not.toHaveBeenCalled();
  });

  it("routes incoming frames to handleWsEvent and ignores malformed ones", () => {
    const handleWsEvent = vi.fn();
    const { fns } = setup("status", { handleWsEvent });
    fns.connectWs(PID);
    const sock = FakeWebSocket.instances[0];
    sock.onmessage!({ data: JSON.stringify({ type: "x" }) });
    sock.onmessage!({ data: "not json" });
    expect(handleWsEvent).toHaveBeenCalledTimes(1);
  });

  it("the verifier's repro: a Queue that missed a delete while disconnected is repainted on reconnect", async () => {
    // Tab B shows 3 backburner rows; the socket is down while one is deleted through
    // REST; after the reconnect the server has 2 and the DOM must say 2 without a reload.
    document.body.innerHTML = `<span id="ws-${PID}"></span><div id="queue-body-${PID}"></div>`;
    const server = [
      { id: "bb-1", title: "one", status: "skipped" },
      { id: "bb-2", title: "two", status: "skipped" },
      { id: "bb-3", title: "three", status: "skipped" },
    ];
    const state = makeState("queue", { queueSprintItems: server.slice(), queueSectionState: { backburner: false } });
    (window as any).state = state;
    const m = makeMocks(state, {
      WebSocket: FakeWebSocket,
      renderQueue: realRenderQueue,
      getPanelState: (pid: string) => state.panels[pid],
      projectApi: vi.fn(async (_pid: string, path: string) =>
        path.includes("sprint-items") ? { items: server.slice(), total_done_count: 0 } : []),
      isLiveSession: vi.fn(() => false),
      loadRecentSessions: vi.fn(),
    });
    delete (m as any).loadQueue;
    delete (m as any).renderQueueBody;
    delete (m as any).resyncProjectViews;
    const fns = load([
      ...wsNames, "repaintVisibleSprintViews", "scheduleGoalBoardReload", "reloadGoalSprintBoard",
      "loadQueue", "renderQueueBody", "applyBackburnerFilter", "queueSearchActive",
    ], m);
    const rows = () => document.querySelectorAll(`#queue-body-${PID} .queue-item`).length;
    fns.renderQueueBody(PID);
    expect(rows()).toBe(3);
    fns.connectWs(PID);
    FakeWebSocket.instances[0].onopen!();
    FakeWebSocket.instances[0].onclose!();
    server.splice(0, 1); // deleted through REST while the socket was down
    vi.advanceTimersByTime(1500);
    FakeWebSocket.instances[1].onopen!();
    await vi.advanceTimersByTimeAsync(400);
    expect(rows()).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// Project-level events: the sidebar list, a deleted project's own tab, a merge
// ---------------------------------------------------------------------------
describe("handleWsEvent: project list events", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const run = (event: any, over: Mocks = {}, stateOver: (s: any) => void = () => {}) => {
    const state = makeState("queue");
    state.tabs = [{ id: PID, project: { id: PID, name: "P" } }];
    stateOver(state);
    const m = makeMocks(state, over);
    const { handleWsEvent } = load(["handleWsEvent", "_debounceRepaint"], m);
    handleWsEvent(PID, event);
    vi.advanceTimersByTime(300);
    return { state, m };
  };

  // projects_changed is the PROJECT LIST, not a project's data: it rides the page's own
  // account socket (connectAccountWs -> handleAccountEvent), which exists with no project tab
  // open. Its routing is pinned in live-refresh-account-socket.test.ts, and that a project's
  // own stream no longer carries it in tests/test_8a665a03_project_and_sweep_events.py.

  it("project_deleted closes the open tab for that project, says so, and refreshes the list", () => {
    const { m } = run({ type: "project_deleted", project_id: PID });
    expect(m.closeTab).toHaveBeenCalledWith(PID);
    expect(m.toast).toHaveBeenCalledWith("This project was deleted");
    expect(m.loadProjects).toHaveBeenCalled();
  });

  it("project_deleted in the tab that issued the delete closes silently (it reports the delete itself)", () => {
    const { m } = run({ type: "project_deleted", project_id: PID }, {}, (s) => {
      s.deletingProjects = { [PID]: true };
    });
    expect(m.closeTab).toHaveBeenCalledWith(PID);
    expect(m.toast).not.toHaveBeenCalled();
  });

  it("project_deleted for a project with no open tab only refreshes the list", () => {
    const state = makeState("queue");
    state.tabs = [];
    const m = makeMocks(state);
    const { handleWsEvent } = load(["handleWsEvent", "_debounceRepaint"], m);
    handleWsEvent(PID, { type: "project_deleted", project_id: PID });
    vi.advanceTimersByTime(300);
    expect(m.closeTab).not.toHaveBeenCalled();
    expect(m.loadProjects).toHaveBeenCalled();
  });

  it("project_merged resyncs every view of the project", () => {
    const { m } = run({ type: "project_merged", source_project_id: OTHER, target_project_id: PID });
    expect(m.resyncProjectViews).toHaveBeenCalledWith(PID);
  });
});

function readSource(rel: string): string {
  return readFileSync(resolve(process.cwd(), rel), "utf8");
}
