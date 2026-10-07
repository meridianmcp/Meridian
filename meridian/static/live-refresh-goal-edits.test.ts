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

describe("refreshGoal keeps an unsaved edit", () => {
  it("control: clean fields take the server's values", async () => {
    const { dom, fns } = setup();
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe("CURRENT FOCUS\nserver focus text");
    expect(dom.ns.value).toBe("server north star");
    expect(dom.sprint.value).toBe("server sprint");
    expect(dom.version.textContent).toBe("v7");
  });

  it("a dirty Goal textarea keeps its text AND its dirty marker (the verifier's repro)", async () => {
    const { dom, state, fns } = setup();
    await fns.refreshGoal(PID);
    const baseline = state.panels[PID]._lastSaved;
    type(dom.goal, "UNSAVED GOAL TEXT");
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe("UNSAVED GOAL TEXT");
    expect(dom.goal.classList.contains("dirty")).toBe(true);
    // ...against the baseline it was typed over, so a later blur compares like with like.
    expect(state.panels[PID]._lastSaved).toBe(baseline);
  });

  it("a dirty North Star keeps its text, marker and baseline", async () => {
    const { dom, state, fns } = setup();
    await fns.refreshGoal(PID);
    type(dom.ns, "UNSAVED NS");
    state.panels[PID]._serverNorthStar = "the baseline it was typed over";
    await fns.refreshGoal(PID);
    expect(dom.ns.value).toBe("UNSAVED NS");
    expect(dom.ns.classList.contains("dirty")).toBe(true);
    expect(state.panels[PID]._serverNorthStar).toBe("the baseline it was typed over");
  });

  it("a dirty Sprint keeps its text and does not run the select syncer over it", async () => {
    const syncer = vi.fn();
    const { dom, fns } = setup({ _sprintSelectSyncers: { [PID]: syncer } });
    type(dom.sprint, "UNSAVED SPRINT");
    await fns.refreshGoal(PID);
    expect(dom.sprint.value).toBe("UNSAVED SPRINT");
    expect(dom.sprint.classList.contains("dirty")).toBe(true);
    expect(syncer).not.toHaveBeenCalled();
  });

  it("control: a clean Sprint does go through the select syncer", async () => {
    const syncer = vi.fn();
    const { fns } = setup({ _sprintSelectSyncers: { [PID]: syncer } });
    await fns.refreshGoal(PID);
    expect(syncer).toHaveBeenCalledWith("server sprint");
  });

  it("only the dirty field is kept: the others still refresh, and so do the read-only zones", async () => {
    const { dom, fns } = setup();
    type(dom.ns, "UNSAVED NS");
    await fns.refreshGoal(PID);
    expect(dom.ns.value).toBe("UNSAVED NS");
    expect(dom.goal.value).toBe("CURRENT FOCUS\nserver focus text");
    expect(dom.sprint.value).toBe("server sprint");
    expect(dom.title.textContent).toBe("v1.0 — the label");
    expect(dom.shipped.textContent).toBe("SHIPPED so far");
    expect(dom.version.textContent).toBe("v7");
  });

  it("the decisions table and pinned decisions still refresh under a dirty field", async () => {
    const { dom, m, fns } = setup();
    type(dom.goal, "UNSAVED GOAL TEXT");
    await fns.refreshGoal(PID);
    expect(m.renderDecisionsTable).toHaveBeenCalledWith(PID, "[2026-10-07] a logged decision");
    expect(m.loadPinnedDecisions).toHaveBeenCalledWith(PID);
  });

  it("judges 'dirty' AFTER the fetch: text typed while the request was in flight survives", async () => {
    let release: (g: any) => void = () => {};
    const { dom, fns } = setup({ projectApi: vi.fn(() => new Promise((r) => { release = r; })) });
    const pending = fns.refreshGoal(PID);
    type(dom.goal, "TYPED DURING THE FETCH"); // typed after the request left, before it returned
    release({ ...SERVER_GOAL });
    await pending;
    expect(dom.goal.value).toBe("TYPED DURING THE FETCH");
    expect(dom.goal.classList.contains("dirty")).toBe(true);
  });

  it("a fetch that fails after the goal loaded once blanks nothing (reconnect resync against a restarting server)", async () => {
    const projectApi = vi.fn(async () => ({ ...SERVER_GOAL }));
    const { dom, fns } = setup({ projectApi });
    await fns.refreshGoal(PID);
    projectApi.mockRejectedValueOnce(new Error("server warming up"));
    type(dom.goal, "UNSAVED GOAL TEXT");
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe("UNSAVED GOAL TEXT");
    expect(dom.version.textContent).toBe("v7");
    // and a CLEAN field keeps the last good view instead of showing "unavailable"
    dom.goal.classList.remove("dirty");
    dom.goal.value = "CURRENT FOCUS\nserver focus text";
    projectApi.mockRejectedValueOnce(new Error("blip"));
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe("CURRENT FOCUS\nserver focus text");
    expect(dom.title.textContent).toBe("v1.0 — the label");
  });

  it("a goal that never loaded still reports the failure", async () => {
    const { dom, fns } = setup({ projectApi: vi.fn(async () => { throw new Error("down"); }) });
    await fns.refreshGoal(PID);
    expect(dom.goal.placeholder).toBe("Goal state failed to load.");
    expect(dom.version.textContent).toBe("(load failed)");
    expect(dom.title.textContent).toBe("Goal state unavailable");
  });

  it("refreshTab (the reconnect resync path) keeps the unsaved edit too", async () => {
    const { dom, fns } = setup();
    await fns.refreshGoal(PID);
    type(dom.goal, "UNSAVED GOAL TEXT");
    type(dom.ns, "UNSAVED NS");
    await fns.refreshTab(PID);
    expect(dom.goal.value).toBe("UNSAVED GOAL TEXT");
    expect(dom.ns.value).toBe("UNSAVED NS");
  });
});

// A goal with NO version-label line (and no CURRENT FOCUS / KEY FILES heading) takes the
// other branch of refreshGoal: the title bar is hidden and the whole content is the editable
// text. That branch has its own `ta.value = ...` assignment, so it needs its own dirty guard --
// dropping it left every test above green, because SERVER_GOAL always has a version label and
// a CURRENT FOCUS heading.
describe("refreshGoal keeps an unsaved edit in a free-text goal (no version-label line)", () => {
  const FREE_TEXT = "Ship the importer first.\nThen the exporter.";
  const freeGoal = (content: any, over: Record<string, any> = {}) => ({ ...SERVER_GOAL, content, version: 3, ...over });

  it("control: a clean field takes the whole free text and no title bar is shown", async () => {
    const { dom, fns } = setup({ projectApi: vi.fn(async () => freeGoal(FREE_TEXT)) });
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe(FREE_TEXT);
    expect(dom.title.textContent).toBe("");
    expect(dom.title.style.display).toBe("none");
    expect(dom.shipped.style.display).toBe("none");
  });

  it("a dirty textarea keeps its text, its dirty marker and its baseline (the user types, an agent POSTs /goal)", async () => {
    const projectApi = vi.fn(async () => freeGoal(FREE_TEXT));
    const { dom, state, fns } = setup({ projectApi });
    await fns.refreshGoal(PID);
    const baseline = state.panels[PID]._lastSaved;
    expect(baseline).toBe(FREE_TEXT);

    type(dom.goal, "MY HALF-WRITTEN GOAL");
    projectApi.mockImplementation(async () => freeGoal("The agent rewrote the whole goal.", { version: 4 }));
    await fns.refreshGoal(PID);

    expect(dom.goal.value).toBe("MY HALF-WRITTEN GOAL");
    expect(dom.goal.classList.contains("dirty")).toBe(true);
    expect(state.panels[PID]._lastSaved).toBe(baseline);
    // ...while the parts the user is not editing do move on to the agent's version.
    expect(dom.version.textContent).toBe("v4");
  });

  it("arriving through the real goal_updated event handler (the agent's POST /goal), the edit survives", async () => {
    const projectApi = vi.fn(async () => freeGoal(FREE_TEXT));
    const { dom, fns } = setup({ projectApi }, ["handleWsEvent", "_debounceRepaint"]);
    await fns.refreshGoal(PID);
    type(dom.goal, "MY HALF-WRITTEN GOAL");
    projectApi.mockImplementation(async () => freeGoal("The agent rewrote the whole goal.", { version: 4 }));

    fns.handleWsEvent(PID, { type: "goal_updated", project_id: PID, version: 4 });
    await new Promise((r) => setTimeout(r, 0));

    expect(projectApi).toHaveBeenCalledTimes(2);
    expect(dom.version.textContent).toBe("v4"); // the refresh did run...
    expect(dom.goal.value).toBe("MY HALF-WRITTEN GOAL"); // ...and left the edit alone
    expect(dom.goal.classList.contains("dirty")).toBe(true);
  });

  it("a clean field still follows the agent's rewrite (nothing is frozen)", async () => {
    const projectApi = vi.fn(async () => freeGoal(FREE_TEXT));
    const { dom, fns } = setup({ projectApi });
    await fns.refreshGoal(PID);
    projectApi.mockImplementation(async () => freeGoal("The agent rewrote the whole goal.", { version: 4 }));
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe("The agent rewrote the whole goal.");
  });

  it("a goal stored as JSON has no version-label line either, and keeps the edit too", async () => {
    const projectApi = vi.fn(async () => freeGoal({ focus: "importer", next: ["exporter"] }));
    const { dom, fns } = setup({ projectApi });
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toContain('"focus": "importer"');
    type(dom.goal, "MY HALF-WRITTEN GOAL");
    projectApi.mockImplementation(async () => freeGoal({ focus: "rewritten by an agent" }));
    await fns.refreshGoal(PID);
    expect(dom.goal.value).toBe("MY HALF-WRITTEN GOAL");
    expect(dom.goal.classList.contains("dirty")).toBe(true);
  });

  it("the text typed while the request was in flight survives too (judged after the fetch)", async () => {
    let release: (g: any) => void = () => {};
    const { dom, fns } = setup({ projectApi: vi.fn(() => new Promise((r) => { release = r; })) });
    const pending = fns.refreshGoal(PID);
    type(dom.goal, "TYPED DURING THE FETCH");
    release(freeGoal(FREE_TEXT));
    await pending;
    expect(dom.goal.value).toBe("TYPED DURING THE FETCH");
    expect(dom.goal.classList.contains("dirty")).toBe(true);
  });
});

describe("saving clears the unsaved marker, so the refresh after it shows the saved text", () => {
  it("saveGoal: POSTs the edit, then the server's version replaces it and 'dirty' is gone", async () => {
    const { dom, m, fns } = setup();
    await fns.refreshGoal(PID);
    type(dom.goal, "CURRENT FOCUS\nmy edit");
    // After the save the server returns what was posted.
    m.projectApi.mockImplementation(async () => ({
      ...SERVER_GOAL, version: 8, content: "v1.0 — the label\n\nSHIPPED so far\n\nCURRENT FOCUS\nmy edit",
    }));
    await fns.saveGoal(PID);
    await Promise.resolve();
    await Promise.resolve();
    expect(m.api).toHaveBeenCalledWith(`/projects/${PID}/goal`, expect.objectContaining({ method: "POST" }));
    expect(dom.goal.classList.contains("dirty")).toBe(false);
    expect(dom.version.textContent).toBe("v8");
    expect(dom.goal.value).toBe("CURRENT FOCUS\nmy edit");
  });

  it("saveGoal: text typed while the POST was in flight stays marked unsaved", async () => {
    let finish: (v?: any) => void = () => {};
    const { dom, fns } = setup({ api: vi.fn(() => new Promise((r) => { finish = r; })) });
    await fns.refreshGoal(PID);
    type(dom.goal, "CURRENT FOCUS\nfirst edit");
    const saving = fns.saveGoal(PID);
    type(dom.goal, "CURRENT FOCUS\nfirst edit, and more typed during the POST");
    finish({});
    await saving;
    expect(dom.goal.classList.contains("dirty")).toBe(true);
    expect(dom.goal.value).toContain("typed during the POST");
  });

  it("saveGoal: a failed save keeps the edit marked unsaved", async () => {
    const { dom, m, fns } = setup({ api: vi.fn(async () => { throw new Error("offline"); }) });
    await fns.refreshGoal(PID);
    type(dom.goal, "CURRENT FOCUS\nmy edit");
    await fns.saveGoal(PID);
    expect(m.toast).toHaveBeenCalledWith("save failed: offline", true);
    expect(dom.goal.classList.contains("dirty")).toBe(true);
    expect(dom.goal.value).toBe("CURRENT FOCUS\nmy edit");
  });

  it("saveGoal: typing and reverting to what the server has is not an unsaved edit any more", async () => {
    const { dom, state, fns } = setup();
    await fns.refreshGoal(PID);
    // The shipped/title zones are display-only; with none, the textarea IS the document.
    document.getElementById(`goal-title-${PID}`)!.textContent = "";
    document.getElementById(`goal-shipped-${PID}`)!.style.display = "none";
    document.getElementById(`goal-autoblocks-${PID}`)!.style.display = "none";
    state.panels[PID]._lastSaved = "reverted text";
    type(dom.goal, "reverted text");
    await fns.saveGoal(PID);
    expect(dom.goal.classList.contains("dirty")).toBe(false);
  });

  it("saveNorthStar and saveSprint clear their marker after the server has the text", async () => {
    const { dom, m, fns } = setup();
    await fns.refreshGoal(PID);
    type(dom.ns, "a new north star");
    type(dom.sprint, "a new sprint");
    await fns.saveNorthStar(PID);
    await fns.saveSprint(PID);
    expect(m.api).toHaveBeenCalledWith(`/projects/${PID}/goal/north-star`, expect.anything());
    expect(m.api).toHaveBeenCalledWith(`/projects/${PID}/goal/sprint`, expect.anything());
    expect(dom.ns.classList.contains("dirty")).toBe(false);
    expect(dom.sprint.classList.contains("dirty")).toBe(false);
  });

  it("saveNorthStar: a failed save keeps the edit marked unsaved", async () => {
    const { dom, fns } = setup({ api: vi.fn(async () => { throw new Error("offline"); }) });
    await fns.refreshGoal(PID);
    type(dom.ns, "a new north star");
    await fns.saveNorthStar(PID);
    expect(dom.ns.classList.contains("dirty")).toBe(true);
  });
});

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
      refreshGoal: vi.fn(),
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
    const m: Mocks = { state, refreshGoal: vi.fn(), refreshDecisionsLog: vi.fn(), _repaintTimers: {} };
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
