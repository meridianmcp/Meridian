// 8a665a03 -- the page's own WebSocket for project-LIST events.
//
// Gap found by independent verification of the live-refresh lane: the dashboard opened one
// socket per project TAB (connectWs), so with no tab open -- the last one closed, a brand-new
// account -- it held no socket at all and a project created, renamed, merged or deleted from
// another tab, an agent or the API never reached it. connectAccountWs is the socket that
// exists with or without a tab; handleAccountEvent is what it routes to.
//
// The code under test is the REAL dashboard.ts (see source-harness.ts). File name has no
// `dashboard-` prefix on purpose: tests/dashboard_src.py concatenates every dashboard-*.ts.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { loadDashboardFunctions } from "./source-harness";

const DASH = "meridian/static/dashboard.ts";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: ((ev?: { code?: number }) => void) | null = null;
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

const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

type Mocks = Record<string, any>;

function setup(stateOver: Record<string, any> = {}, over: Mocks = {}) {
  // NO panels, NO tabs: the dashboard the verifier reproduced the gap on.
  const state: any = { panels: {}, tabs: [], projects: [], activeWorkspaceTenantId: null, ...stateOver };
  const m: Mocks = {
    state,
    WebSocket: FakeWebSocket,
    loadProjects: vi.fn(),
    restoreTabs: vi.fn(),
    _repaintTimers: {} as Record<string, any>,
    ...over,
  };
  const fns = loadDashboardFunctions(
    DASH,
    [
      "connectAccountWs", "handleAccountEvent", "_debounceRepaint",
      "refreshProjectListFromAccount", "dismissEmptyAccountWizard",
    ],
    m,
  );
  return { state, m, fns };
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("connectAccountWs", () => {
  it("opens /ws-account with no project tab, no panel and no per-project socket", () => {
    const { state, fns } = setup();
    fns.connectAccountWs();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(last().url).toMatch(/^wss?:\/\/[^/]+\/ws-account$/);
    expect(state.accountWs).toBe(last());
  });

  it("names the active workspace, URL-encoded, so a workspace member hears the owner's list", () => {
    const { fns } = setup({ activeWorkspaceTenantId: "tenant id/with&odd=chars" });
    fns.connectAccountWs();
    expect(last().url).toMatch(/\/ws-account\?workspace=tenant%20id%2Fwith%26odd%3Dchars$/);
  });

  it("every open refetches the project list once (the first fetch can race the connect)", () => {
    const { m, fns } = setup();
    fns.connectAccountWs();
    last().onopen!();
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).toHaveBeenCalledTimes(1);
  });

  it("a projects_changed frame refetches the list: the verifier's zero-tab repro", () => {
    const { state, m, fns } = setup();
    fns.connectAccountWs();
    last().onopen!();
    vi.advanceTimersByTime(300);
    m.loadProjects.mockClear();
    expect(state.tabs).toHaveLength(0);
    last().onmessage!({ data: JSON.stringify({ type: "projects_changed", change: "created" }) });
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).toHaveBeenCalledTimes(1);
  });

  it("a burst (batch delete, merge) refetches once", () => {
    const { m, fns } = setup();
    fns.connectAccountWs();
    for (let i = 0; i < 10; i++) {
      last().onmessage!({ data: JSON.stringify({ type: "projects_changed", change: "deleted" }) });
    }
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).toHaveBeenCalledTimes(1);
  });

  it("ignores malformed frames and event types it does not own", () => {
    const { m, fns } = setup();
    fns.connectAccountWs();
    expect(() => last().onmessage!({ data: "not json" })).not.toThrow();
    last().onmessage!({ data: JSON.stringify({ type: "task_created", task: {} }) });
    last().onmessage!({ data: JSON.stringify({ type: "sprint_item_updated", project_id: "p" }) });
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).not.toHaveBeenCalled();
  });

  it("reconnects after 1.5 s when an established socket drops (server restart, sleep, blip)", () => {
    const { state, m, fns } = setup();
    fns.connectAccountWs();
    last().onopen!();
    last().onclose!({ code: 1006 });
    expect(state.accountWs).toBeNull();
    vi.advanceTimersByTime(1499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(2);
    expect(FakeWebSocket.instances).toHaveLength(2);
    // ...and the reopened socket resyncs what the gap swallowed.
    m.loadProjects.mockClear();
    last().onopen!();
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).toHaveBeenCalledTimes(1);
  });

  it("backs off while the server stays unreachable instead of retrying every 1.5 s forever", () => {
    const { fns } = setup();
    fns.connectAccountWs();
    const delays: number[] = [];
    for (let attempt = 0; attempt < 8; attempt++) {
      const before = FakeWebSocket.instances.length;
      last().onclose!({ code: 1006 }); // never opened
      let waited = 0;
      while (FakeWebSocket.instances.length === before && waited < 60_000) {
        vi.advanceTimersByTime(250);
        waited += 250;
      }
      delays.push(waited);
    }
    expect(delays[0]).toBe(1500);
    expect(delays[1]).toBe(3000);
    expect(delays[2]).toBe(6000);
    expect(Math.max(...delays)).toBeLessThanOrEqual(30_000);
    expect(delays[delays.length - 1]).toBe(30_000);
  });

  it("the backoff resets once a connection has been established", () => {
    const { fns } = setup();
    fns.connectAccountWs();
    last().onclose!({ code: 1006 }); // never opened -> 1.5 s
    vi.advanceTimersByTime(1500);
    last().onclose!({ code: 1006 }); // never opened -> 3 s
    vi.advanceTimersByTime(3000);
    expect(FakeWebSocket.instances).toHaveLength(3);
    last().onopen!();
    last().onclose!({ code: 1006 });
    vi.advanceTimersByTime(1500);
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  it("does not retry a refusal (4401: not signed in / not a member of that workspace)", () => {
    const { state, fns } = setup();
    fns.connectAccountWs();
    last().onclose!({ code: 4401 });
    vi.advanceTimersByTime(120_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(state.accountWs).toBeNull();
  });

  it("calling it again (workspace switch) closes the old socket, which neither reconnects nor clobbers the new one", () => {
    const { state, fns } = setup();
    fns.connectAccountWs();
    const first = last();
    first.onopen!();
    state.activeWorkspaceTenantId = "other-tenant";
    fns.connectAccountWs();
    const second = last();
    expect(first.closed).toBe(true);
    expect(second.url).toContain("?workspace=other-tenant");
    expect(state.accountWs).toBe(second);
    first.onclose!({ code: 1000 }); // the old socket's close arrives late
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(state.accountWs).toBe(second);
  });

  it("a reconnect timer that races a newer socket does not open a second one", () => {
    const { state, fns } = setup();
    fns.connectAccountWs();
    last().onopen!();
    last().onclose!({ code: 1006 });
    fns.connectAccountWs(); // someone reconnected first (workspace switch)
    vi.advanceTimersByTime(5000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(state.accountWs).toBe(last());
  });

  it("a socket constructor that throws never escapes into the dashboard's init", () => {
    // jsdom/browsers throw on a malformed URL; an init that dies here would skip restoreTabs.
    const Throwing = function () { throw new Error("SecurityError"); } as any;
    const { fns } = setup({}, { WebSocket: Throwing });
    expect(() => fns.connectAccountWs()).not.toThrow();
  });
});

describe("handleAccountEvent", () => {
  it("projects_changed refetches the list", () => {
    const { m, fns } = setup();
    fns.handleAccountEvent({ type: "projects_changed", change: "renamed" });
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).toHaveBeenCalledTimes(1);
  });

  it("anything else is ignored (a project's data never travels on this socket)", () => {
    const { m, fns } = setup();
    fns.handleAccountEvent({ type: "task_created" });
    fns.handleAccountEvent({});
    vi.advanceTimersByTime(300);
    expect(m.loadProjects).not.toHaveBeenCalled();
  });
});

describe("the first-run wizard when the first project appears from elsewhere", () => {
  const mountWizard = (display: string) => {
    document.body.innerHTML = `<div id="ez-wizard" style="display:${display}"></div>`;
    return document.getElementById("ez-wizard")!;
  };

  it("closes the wizard and opens the project (an agent's create_project is the usual way)", async () => {
    const wizard = mountWizard("flex");
    const { m, fns } = setup();
    // The refetch is what fills the project list the wizard check reads.
    m.loadProjects.mockImplementation(async () => { m.state.projects = [{ id: "p1", name: "first" }]; });
    fns.handleAccountEvent({ type: "projects_changed", change: "created" });
    await vi.advanceTimersByTimeAsync(300);
    expect(wizard.style.display).toBe("none");
    expect(m.restoreTabs).toHaveBeenCalledTimes(1);
  });

  it("leaves the wizard up while the account is still empty", async () => {
    const wizard = mountWizard("flex");
    const { m, fns } = setup();
    fns.handleAccountEvent({ type: "projects_changed", change: "deleted" });
    await vi.advanceTimersByTimeAsync(300);
    expect(wizard.style.display).toBe("flex");
    expect(m.restoreTabs).not.toHaveBeenCalled();
  });

  it("does nothing when no wizard is showing (the normal case)", async () => {
    mountWizard("none");
    const { m, fns } = setup({ projects: [{ id: "p1" }] });
    fns.handleAccountEvent({ type: "projects_changed", change: "renamed" });
    await vi.advanceTimersByTimeAsync(300);
    expect(m.restoreTabs).not.toHaveBeenCalled();
    document.body.innerHTML = "";
    fns.handleAccountEvent({ type: "projects_changed", change: "renamed" }); // no wizard element at all
    await vi.advanceTimersByTimeAsync(300);
    expect(m.restoreTabs).not.toHaveBeenCalled();
  });

  it("the open of the account socket also dismisses it (a project created while the page loaded)", async () => {
    const wizard = mountWizard("flex");
    const { m, fns } = setup();
    m.loadProjects.mockImplementation(async () => { m.state.projects = [{ id: "p1" }]; });
    fns.connectAccountWs();
    last().onopen!();
    await vi.advanceTimersByTimeAsync(300);
    expect(wizard.style.display).toBe("none");
    expect(m.restoreTabs).toHaveBeenCalledTimes(1);
  });
});
