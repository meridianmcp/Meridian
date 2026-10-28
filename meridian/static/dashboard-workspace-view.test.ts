// DOM tests for the Local overview view (workspace-overview-spec-r2, section 9.2 items 13 to 18),
// and for the two dashboard.ts entry points that close the view (F1). The mount tests moved here
// from dashboard-workspace.test.ts (F6). dashboard.ts is loaded through source-harness, which lifts
// the named top-level functions from the shipped file and evaluates them with the scope supplied here.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountWorkspaceView, type RepoFacts, type SessionFacts, type WorkspaceOverview } from "./dashboard-workspace";
import { loadDashboardFunctions } from "./source-harness";

type FetchResult = { status: number; body: unknown };
type View = ReturnType<typeof mountWorkspaceView>;

// A small, valid overview body: one healthy repo, no sessions and no projects unless a test adds them.
function overview(over: Partial<WorkspaceOverview> = {}): WorkspaceOverview {
  return {
    schema: "meridian-workspace-overview/1",
    runtime: {
      version: "0.1.9",
      hostname: "host",
      location: "local",
      guard: { mode: "enforce" },
      tunnel: { client_process: "unknown", hosted_connection: "unknown" },
    },
    repos: {
      snapshot: { error: null, built_age_s: 60, stats: { read: 1 } },
      items: [{
        key: "c:/repos/alpha",
        path: "C:/repos/alpha",
        name: "alpha",
        guard: {
          settings_state: "present",
          registered: true,
          components: ["pre", "post", "brief"],
          installed_mode: "enforce",
          runtime_ok: true,
          problems: [],
        },
        codebase_memory: {
          managed_entry: true,
          index: {
            mode: "own", partial: false, present: true, fresh: true, age_days: 2.5,
            nodes: 1200, db_bytes: 2097152, covers_repo_root: true,
          },
        },
      }],
      truncated: false,
    },
    sessions: { items: [], truncated: false },
    projects: { items: [] },
    gaps: [],
    errors: [],
    ...over,
  };
}

function session(over: Partial<SessionFacts> & { name: string }): SessionFacts {
  return { id: over.name, status: "idle", age_s: 60, worktree: null, ...over };
}

function setupDom(): void {
  document.body.innerHTML = '<aside><button id="workspace-entry" type="button">Workspace</button></aside>'
    + '<main><div id="topbar"></div><div id="tab-bodies"></div></main>';
}

const overviewButton = (): HTMLButtonElement => document.getElementById("workspace-overview-btn") as HTMLButtonElement;
const viewElement = (): HTMLElement => document.getElementById("workspace-overview-view") as HTMLElement;
const tabBodies = (): HTMLElement => document.getElementById("tab-bodies") as HTMLElement;
const refreshButton = (): HTMLButtonElement =>
  document.querySelector<HTMLButtonElement>('[data-ws-action="refresh"]') as HTMLButtonElement;

function makeFetch(result: FetchResult = { status: 200, body: overview() }) {
  return vi.fn<() => Promise<FetchResult>>(async () => result);
}

// The free identifiers dashboard.ts openTab reads. The overview controls are the real ones, so the
// close openTab triggers is the real hide. The tab helpers are no-ops: openTab only sequences them.
function openTabScope(
  view: View,
  tabs: Array<{ id: string; project: unknown }> = [],
  activateTab: (id: string) => void = () => undefined,
): Record<string, unknown> {
  return {
    _workspaceOverview: view,
    state: { tabs },
    saveTabs: () => undefined,
    renderTabs: () => undefined,
    buildTabBody: () => undefined,
    activateTab,
    refreshProjectCountBadges: () => undefined,
  };
}

beforeEach(() => {
  setupDom();
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

// ---------------------------------------------------------------------------
// Item 13: the sidebar button and the hosted gate
// ---------------------------------------------------------------------------

describe("mountWorkspaceView: sidebar button and hosted gate (item 13)", () => {
  it("hosted: the sidebar button is hidden and the view never opens or fetches", () => {
    const fetchOverview = makeFetch();
    const view = mountWorkspaceView(document, { hosted: true, fetchOverview });
    expect(overviewButton().hidden).toBe(true);
    view.open();
    expect(fetchOverview).not.toHaveBeenCalled();
    expect(viewElement().hidden).toBe(true);
    expect(tabBodies().style.display).toBe("");
  });

  it("not hosted: the sidebar button is visible and sits right after #workspace-entry", () => {
    mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    expect(overviewButton().hidden).toBe(false);
    expect(overviewButton().textContent).toBe("Local overview");
    expect(document.getElementById("workspace-entry")!.nextElementSibling).toBe(overviewButton());
  });

  it("the view is created before #tab-bodies, hidden, with the ws-overview class", () => {
    mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    expect(viewElement().hidden).toBe(true);
    expect(viewElement().classList.contains("ws-overview")).toBe(true);
    expect(viewElement().nextElementSibling).toBe(tabBodies());
  });

  it("an existing #workspace-overview-btn is reused, not duplicated", () => {
    document.body.insertAdjacentHTML("beforeend", '<button id="workspace-overview-btn" type="button">x</button>');
    mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    expect(document.querySelectorAll("#workspace-overview-btn")).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Item 14: open, close and toggle
// ---------------------------------------------------------------------------

describe("mountWorkspaceView: open and close (item 14)", () => {
  it("open hides only #tab-bodies, and close restores it", () => {
    const view: View = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    view.open();
    expect(tabBodies().style.display).toBe("none");
    expect(document.getElementById("topbar")!.style.display).toBe("");
    expect(viewElement().hidden).toBe(false);
    expect(overviewButton().getAttribute("aria-pressed")).toBe("true");
    expect(overviewButton().classList.contains("active")).toBe(true);

    view.close();
    expect(tabBodies().style.display).toBe("");
    expect(viewElement().hidden).toBe(true);
    expect(overviewButton().getAttribute("aria-pressed")).toBe("false");
  });

  it("close restores the inline display that #tab-bodies had before open", () => {
    tabBodies().style.display = "flex";
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    view.open();
    view.close();
    expect(tabBodies().style.display).toBe("flex");
  });

  it("the sidebar button toggles the view: one click opens, a second closes", () => {
    mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    overviewButton().click();
    expect(viewElement().hidden).toBe(false);
    overviewButton().click();
    expect(viewElement().hidden).toBe(true);
  });

  it("the Close button inside the view closes it", () => {
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    view.open();
    viewElement().querySelector<HTMLButtonElement>('[data-ws-action="close"]')!.click();
    expect(viewElement().hidden).toBe(true);
    expect(tabBodies().style.display).toBe("");
  });

  it("openTab closes the open overview before it opens a new project tab (real dashboard.ts function)", () => {
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    view.open();
    expect(viewElement().hidden).toBe(false);
    const { openTab } = loadDashboardFunctions("meridian/static/dashboard.ts", ["openTab"], openTabScope(view));
    openTab({ id: "p9", name: "Nine" });
    expect(viewElement().hidden).toBe(true);
    expect(tabBodies().style.display).toBe("");
  });

  it("openTab on a tab that is already open hides the overview before it activates that tab", () => {
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch() });
    view.open();
    const activatedWhileHidden: boolean[] = [];
    const { openTab } = loadDashboardFunctions(
      "meridian/static/dashboard.ts",
      ["openTab"],
      openTabScope(view, [{ id: "p9", project: { id: "p9" } }], () => {
        activatedWhileHidden.push(viewElement().hidden);
      }),
    );
    openTab({ id: "p9" });
    expect(activatedWhileHidden).toEqual([true]);
  });
});

// ---------------------------------------------------------------------------
// Items 15 to 17: fetch on open, Refresh, no polling, in-flight state and errors
// ---------------------------------------------------------------------------

describe("mountWorkspaceView: fetch, refresh and polling (items 15 to 17)", () => {
  it("opening the view fetches exactly once and renders the overview", async () => {
    const fetchOverview = makeFetch();
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".ws-header")).not.toBeNull();
  });

  it("a refresh while the open fetch is in flight joins it instead of sending another", async () => {
    let settle!: (value: FetchResult) => void;
    const pending = vi.fn(() => new Promise<FetchResult>((resolve) => {
      settle = resolve;
    }));
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: pending });
    view.open();
    const first = view.refresh();
    const second = view.refresh();
    expect(pending).toHaveBeenCalledTimes(1);
    settle({ status: 200, body: overview() });
    await Promise.all([first, second]);
    expect(pending).toHaveBeenCalledTimes(1);
  });

  it("a refresh after the first fetch has settled makes exactly one more fetch", async () => {
    const fetchOverview = makeFetch();
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(1);
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(2);
  });

  it("the Refresh button sends one more fetch once the previous one has settled", async () => {
    const fetchOverview = makeFetch();
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    refreshButton().click();
    // Measured before any await: the click path itself must send the request.
    expect(fetchOverview).toHaveBeenCalledTimes(2);
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(2);
  });

  it("no polling: five minutes pass with no further fetch", async () => {
    vi.useFakeTimers();
    const fetchOverview = makeFetch();
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(5 * 60 * 1000);
    expect(fetchOverview).toHaveBeenCalledTimes(1);
  });

  it("Refresh is disabled while a fetch is in flight and enabled after it settles", async () => {
    let settle!: (value: FetchResult) => void;
    const pending = vi.fn(() => new Promise<FetchResult>((resolve) => {
      settle = resolve;
    }));
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: pending });
    view.open();
    expect(refreshButton().disabled).toBe(true);
    const inFlight = view.refresh();
    settle({ status: 200, body: overview() });
    await inFlight;
    expect(refreshButton().disabled).toBe(false);
  });

  it("a 500 shows HTTP 500 with Retry, and Retry fetches again", async () => {
    const fetchOverview = makeFetch({ status: 500, body: null });
    fetchOverview.mockResolvedValueOnce({ status: 500, body: null });
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    const body = document.querySelector("[data-ws-body]")!;
    expect(body.textContent).toContain("HTTP 500");
    const retry = body.querySelector<HTMLButtonElement>('[data-ws-action="retry"]');
    expect(retry).not.toBeNull();
    fetchOverview.mockResolvedValueOnce({ status: 200, body: overview() });
    retry!.click();
    // Measured before any await: the Retry click path itself must send the request.
    expect(fetchOverview).toHaveBeenCalledTimes(2);
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(2);
    expect(document.querySelector(".ws-header")).not.toBeNull();
  });

  it("a thrown fetch shows (no response)", async () => {
    const fetchOverview = vi.fn<() => Promise<FetchResult>>(async () => {
      throw new TypeError("network down");
    });
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    expect(document.querySelector("[data-ws-body]")!.textContent).toContain("(no response)");
  });

  it("a 200 with an unexpected body shows the status as a fetch error", async () => {
    const fetchOverview = makeFetch({ status: 200, body: { unexpected: true } });
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
    expect(document.querySelector("[data-ws-body]")!.textContent).toContain("(HTTP 200)");
  });
});

// ---------------------------------------------------------------------------
// Null index mode (index not read, scan budget reached): unknown with its reason, in the DOM
// ---------------------------------------------------------------------------

describe("mountWorkspaceView: a null index mode (index not read)", () => {
  it("settings present, not registered, index null: the chip and the row show the reason, and the repo is not Index only", async () => {
    const body = overview({
      repos: {
        snapshot: { error: null, built_age_s: 60, stats: { read: 1 } },
        items: [{
          key: "c:/repos/unread",
          path: "C:/repos/unread",
          name: "unread",
          guard: {
            settings_state: "present",
            registered: false,
            components: [],
            installed_mode: null,
            runtime_ok: null,
            problems: [],
          },
          codebase_memory: {
            managed_entry: true,
            index: {
              mode: null,
              why: "not read: scan budget reached",
              partial: null,
              present: null,
              fresh: null,
              age_days: null,
              nodes: null,
              db_bytes: null,
              covers_repo_root: null,
            },
          },
        }],
        truncated: false,
      },
    });
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch({ status: 200, body }) });
    view.open();
    await view.refresh();

    const chipTexts = Array.from(document.querySelectorAll(".ws-chip")).map((el) => el.textContent);
    expect(chipTexts).toContain("index: unknown (not read: scan budget reached)");
    expect(chipTexts).not.toContain("index: null");

    const indexOnly = Array.from(document.querySelectorAll("details.ws-group"))
      .find((group) => group.querySelector("summary")?.textContent?.startsWith("Index only, no guard"));
    expect(indexOnly?.querySelector(".ws-count")?.textContent).toBe("0");

    expect(document.querySelector(".ws-reason")?.textContent)
      .toBe("Nothing registered for this repo; index unknown (not read: scan budget reached)");
  });
});

// ---------------------------------------------------------------------------
// S3: a render exception is a render error, not a fetch error
// ---------------------------------------------------------------------------

describe("mountWorkspaceView: render exception (S3)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  // The body passes the structural check, but its repo row has no guard object, so the real
  // renderer throws while it groups the repos.
  function brokenBody(): WorkspaceOverview {
    const brokenRow = { key: "c:/bad", path: "C:/bad", name: "bad" } as unknown as RepoFacts;
    return overview({ repos: { snapshot: overview().repos.snapshot, items: [brokenRow], truncated: false } });
  }

  it("shows a render error with Retry, logs the error, and shows no HTTP status", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview: makeFetch({ status: 200, body: brokenBody() }) });
    view.open();
    await view.refresh();

    const text = document.querySelector("[data-ws-body]")!.textContent ?? "";
    expect(text).toContain("Could not render this overview.");
    expect(text).not.toContain("HTTP");
    expect(document.querySelector('[data-ws-body] [data-ws-action="retry"]')).not.toBeNull();
    expect(errorSpy).toHaveBeenCalledWith("Local overview: render failed", expect.any(Error));
  });

  it("Retry after a render error sends the request again, measured before any await", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const fetchOverview = makeFetch({ status: 200, body: brokenBody() });
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();

    fetchOverview.mockResolvedValueOnce({ status: 200, body: overview() });
    document.querySelector<HTMLButtonElement>('[data-ws-body] [data-ws-action="retry"]')!.click();
    expect(fetchOverview).toHaveBeenCalledTimes(2);
    await view.refresh();
    expect(fetchOverview).toHaveBeenCalledTimes(2);
    expect(document.querySelector(".ws-header")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Item 18 and session ordering: session rows
// ---------------------------------------------------------------------------

describe("session rows: age text and ordering (item 18)", () => {
  async function openWithSessions(sessions: SessionFacts[]): Promise<void> {
    const fetchOverview = makeFetch({
      status: 200,
      body: overview({
        repos: { ...overview().repos, items: [] },
        sessions: { items: sessions, truncated: false },
      }),
    });
    const view = mountWorkspaceView(document, { hosted: false, fetchOverview });
    view.open();
    await view.refresh();
  }

  it("a session row's age text comes from formatAgeSeconds", async () => {
    await openWithSessions([
      session({ name: "five-minutes", age_s: 300, live: false, status: "idle" }),
      session({ name: "forty-two", age_s: 42, live: false, status: "idle" }),
      session({ name: "never-parsed", age_s: null, live: false, status: "idle" }),
    ]);
    const text = document.querySelector("[data-ws-body]")!.textContent ?? "";
    expect(text).toContain("last seen 5m ago");
    expect(text).toContain("last seen 42s ago");
    expect(text).toContain("last seen unknown: last_seen did not parse");
  });

  it("sessions are ordered live, then active but not live, then idle, then other statuses; server order breaks ties", async () => {
    await openWithSessions([
      session({ name: "z-idle", status: "idle", live: false }),
      session({ name: "y-stale", status: "active", age_s: 900, live: false }),
      session({ name: "x-live-2", status: "active", age_s: 30, live: true }),
      session({ name: "w-closed", status: "closed", live: false }),
      session({ name: "x-live-1", status: "active", age_s: 45, live: true }),
    ]);
    const names = [...document.querySelectorAll(".ws-row-head strong")].map((el) => el.textContent);
    expect(names).toEqual(["x-live-2", "x-live-1", "y-stale", "z-idle", "w-closed"]);
  });

  it("with no live flag, liveness falls back to the shared isLiveSession rule", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-10-27T12:00:00Z"));
    await openWithSessions([
      { id: "old", name: "no-flag-old", status: "active", last_seen: "2026-10-27T11:00:00Z", age_s: 3600, worktree: null },
      { id: "live", name: "no-flag-live", status: "active", last_seen: "2026-10-27T11:59:00Z", age_s: 60, worktree: null },
    ]);
    const names = [...document.querySelectorAll(".ws-row-head strong")].map((el) => el.textContent);
    expect(names).toEqual(["no-flag-live", "no-flag-old"]);
  });
});

// ---------------------------------------------------------------------------
// F1: a project tab or the Workspace entry closes the overview (dashboard.ts)
// ---------------------------------------------------------------------------

describe("dashboard.ts entry points close the Local overview (F1)", () => {
  const calls: string[] = [];
  const dashboardScope = {
    state: { activeTab: null as string | null, projects: [] as Array<{ id: string; name: string }> },
    openTab: (project: { id: string }): void => {
      calls.push(`openTab:${project.id}`);
    },
    activateTab: (id: string): void => {
      calls.push(`activateTab:${id}`);
    },
    _workspaceOverview: {
      close: (): void => {
        calls.push("close");
      },
    },
  };
  const fns = loadDashboardFunctions(
    "meridian/static/dashboard.ts",
    ["selectProjectTab", "openWorkspaceEntry"],
    dashboardScope,
  ) as {
    selectProjectTab: (id: string) => void;
    openWorkspaceEntry: () => void;
  };

  beforeEach(() => {
    calls.length = 0;
    dashboardScope.state.activeTab = null;
    dashboardScope.state.projects = [{ id: "p1", name: "Alpha" }];
  });

  it("selecting a project tab closes the overview before it activates the tab", () => {
    fns.selectProjectTab("p2");
    expect(calls).toEqual(["close", "activateTab:p2"]);
  });

  it("the Workspace entry closes the overview and opens Settings when the tab body already exists", () => {
    dashboardScope.state.activeTab = "p1";
    document.body.insertAdjacentHTML(
      "beforeend",
      '<div id="tab-body-p1"></div><div id="vtab-strip-p1"><button type="button" data-vtab="settings">s</button></div>',
    );
    let settingsClicks = 0;
    document.querySelector<HTMLButtonElement>('[data-vtab="settings"]')!.addEventListener("click", () => {
      settingsClicks += 1;
    });
    fns.openWorkspaceEntry();
    expect(calls).toEqual(["close"]);
    expect(settingsClicks).toBe(1);
  });

  it("the Workspace entry opens the project's tab first when its body is not mounted yet", () => {
    dashboardScope.state.activeTab = "p1";
    document.body.insertAdjacentHTML(
      "beforeend",
      '<div id="vtab-strip-p1"><button type="button" data-vtab="settings">s</button></div>',
    );
    fns.openWorkspaceEntry();
    expect(calls).toEqual(["close", "openTab:p1"]);
  });

  it("the Workspace entry with no project still closes the overview and does nothing else", () => {
    dashboardScope.state.activeTab = null;
    dashboardScope.state.projects = [];
    fns.openWorkspaceEntry();
    expect(calls).toEqual(["close"]);
  });
});
