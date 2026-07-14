// Tests for 116e0245 — settings-tabs lazy-load per-tab data on click.
//
// Verified behaviours:
//   (1) On initial mount/loadSettingsTab, only project-tab data is fetched;
//       account-specific endpoints (/settings/notifications, /projects/:id/ntfy)
//       are NOT called.
//   (2) Clicking the Account tab triggers the account-specific fetch
//       (_loadSettingsAccountPane), which populates the lazy placeholders.
//   (3) Re-clicking an already-loaded Account tab does not re-fetch (cache hit).
//
// Implementation note: api, loadProjectSettings, getActiveWorkspaceRole, and the
// other globals called by loadSettingsTab are ambient window functions that we stub
// in each test. We do NOT depend on __file__ or cwd — all project IDs are
// synthetic constants.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  _settingsTabDataCache,
  _loadSettingsAccountPane,
  loadSettingsTab,
  _settingsNotificationsCardHtml,
} from "./dashboard-settings";

const PID = "test-proj-lazy-001";

// Minimal escapeHtml for tests
const esc = (s: unknown) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[
      c
    ] as string)
  );

// Stubs for the ambient globals used inside dashboard-settings.ts
const makeApiStub = (
  notifCalls: string[],
  ntfyCalls: string[],
  mcpCalls: string[]
) =>
  vi.fn(async (url: string) => {
    if (url === "/settings/notifications") {
      notifCalls.push(url);
      return { prefs: { hitl: true, sprint: false } };
    }
    if (url.includes("/ntfy")) {
      ntfyCalls.push(url);
      return { notify_url: "", notify_email: "" };
    }
    if (url === "/settings/mcp-config") {
      mcpCalls.push(url);
      return { base_url: "https://usemeridian.us" };
    }
    if (url.includes("/github/status")) {
      return { connected: false };
    }
    return {};
  });

function setupGlobals(
  notifCalls: string[],
  ntfyCalls: string[],
  mcpCalls: string[]
) {
  const g = globalThis as any;
  const w = window as any;

  g.escapeHtml = esc;
  w.escapeHtml = esc;
  g._PLAN_LABELS = { free: "Free", standard: "Standard" };
  w._PLAN_LABELS = g._PLAN_LABELS;
  g.displayNotifyTarget = (v: unknown) => String(v || "");
  w.displayNotifyTarget = g.displayNotifyTarget;
  g.STORAGE_KEY = (k: string) => `meridian.${k}`;
  w.STORAGE_KEY = g.STORAGE_KEY;

  g.state = {
    tenantEmail: "",
    tenantPlan: "free",
    projects: [{ id: PID, name: "Lazy Test Project" }],
    serverConfig: {},
  };
  w.state = g.state;

  const apiStub = makeApiStub(notifCalls, ntfyCalls, mcpCalls);
  g.api = apiStub;
  w.api = apiStub;

  g.loadProjectSettings = vi.fn(async (_pid: any) => ({
    project_id: _pid,
    executor_config: {},
    max_pinned_decisions: 50,
  }));
  w.loadProjectSettings = g.loadProjectSettings;

  g.saveProjectSettings = vi.fn(async () => ({}));
  w.saveProjectSettings = g.saveProjectSettings;

  g.getActiveWorkspaceRole = vi.fn(async () => "owner");
  w.getActiveWorkspaceRole = g.getActiveWorkspaceRole;

  g.isHostedMode = () => false;
  w.isHostedMode = g.isHostedMode;

  g.isDemoMode = () => false;
  w.isDemoMode = g.isDemoMode;

  g.hideDemoAdminControls = () => {};
  w.hideDemoAdminControls = g.hideDemoAdminControls;

  g.osExecutorHintBanner = () => "";
  w.osExecutorHintBanner = g.osExecutorHintBanner;

  g.githubIconSvg = () => "";
  w.githubIconSvg = g.githubIconSvg;

  g.toast = () => {};
  w.toast = g.toast;

  g.DEFAULT_MAX_PINNED_DECISIONS = 100;
  w.DEFAULT_MAX_PINNED_DECISIONS = g.DEFAULT_MAX_PINNED_DECISIONS;

  g.DEFAULT_CONTEXT_THRESHOLD = 60;
  w.DEFAULT_CONTEXT_THRESHOLD = g.DEFAULT_CONTEXT_THRESHOLD;

  g.DEFAULT_MAX_TURNS = 120;
  w.DEFAULT_MAX_TURNS = g.DEFAULT_MAX_TURNS;

  // These are referenced but not critical for lazy-load testing
  g.loadExecutorRulesSection = null;
  w.loadExecutorRulesSection = null;
  g.loadTunnelPluginsSection = null;
  w.loadTunnelPluginsSection = null;
}

function createSettingsBody(projectId: string): HTMLElement {
  const body = document.createElement("div");
  body.id = `settings-body-${projectId}`;
  document.body.appendChild(body);
  return body;
}

beforeEach(() => {
  // Clear the module-level cache before each test
  _settingsTabDataCache.clear();

  // Clean up any lingering DOM nodes
  document.body.innerHTML = "";
});

afterEach(() => {
  _settingsTabDataCache.clear();
  document.body.innerHTML = "";
  const g = globalThis as any;
  ["api", "loadProjectSettings", "getActiveWorkspaceRole", "state"].forEach(
    (k) => {
      delete g[k];
      delete (window as any)[k];
    }
  );
});

// ---------------------------------------------------------------------------
// (1) Initial mount: only project-tab data fetched, NOT account endpoints
// ---------------------------------------------------------------------------
describe("loadSettingsTab initial mount", () => {
  it("does NOT call /settings/notifications on initial mount", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    createSettingsBody(PID);

    await loadSettingsTab(PID);

    expect(notifCalls).toHaveLength(0);
  });

  it("does NOT call /projects/:id/ntfy on initial mount", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    createSettingsBody(PID);

    await loadSettingsTab(PID);

    expect(ntfyCalls).toHaveLength(0);
  });

  it("DOES call /settings/mcp-config on initial mount (project-tab data)", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    createSettingsBody(PID);

    await loadSettingsTab(PID);

    expect(mcpCalls.length).toBeGreaterThanOrEqual(1);
  });

  it("renders lazy-load placeholder elements for the account-tab sections", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    createSettingsBody(PID);

    await loadSettingsTab(PID);

    // The ntfy placeholder should exist in the DOM
    expect(document.getElementById(`settings-ntfy-lazy-${PID}`)).not.toBeNull();
    // The prefs placeholder should also exist
    expect(document.getElementById(`settings-prefs-lazy-${PID}`)).not.toBeNull();
  });

  it("account tab data cache starts empty after initial mount", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    createSettingsBody(PID);

    await loadSettingsTab(PID);

    expect(_settingsTabDataCache.has(`${PID}:account`)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// (2) Clicking Account tab triggers the account-specific fetch
// ---------------------------------------------------------------------------
describe("_loadSettingsAccountPane — first load", () => {
  it("calls /settings/notifications when account pane is loaded", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);

    // Create minimal placeholder DOM that _loadSettingsAccountPane expects
    const ntfyEl = document.createElement("div");
    ntfyEl.id = `settings-ntfy-lazy-${PID}`;
    document.body.appendChild(ntfyEl);
    const prefsEl = document.createElement("div");
    prefsEl.id = `settings-prefs-lazy-${PID}`;
    document.body.appendChild(prefsEl);

    await _loadSettingsAccountPane(PID);

    expect(notifCalls).toHaveLength(1);
    expect(notifCalls[0]).toBe("/settings/notifications");
  });

  it("calls /projects/:id/ntfy when account pane is loaded", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);

    const ntfyEl = document.createElement("div");
    ntfyEl.id = `settings-ntfy-lazy-${PID}`;
    document.body.appendChild(ntfyEl);
    const prefsEl = document.createElement("div");
    prefsEl.id = `settings-prefs-lazy-${PID}`;
    document.body.appendChild(prefsEl);

    await _loadSettingsAccountPane(PID);

    expect(ntfyCalls).toHaveLength(1);
    expect(ntfyCalls[0]).toContain(`/projects/${PID}/ntfy`);
  });

  it("stores the fetched data in the cache after first account pane load", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);

    const ntfyEl = document.createElement("div");
    ntfyEl.id = `settings-ntfy-lazy-${PID}`;
    document.body.appendChild(ntfyEl);
    const prefsEl = document.createElement("div");
    prefsEl.id = `settings-prefs-lazy-${PID}`;
    document.body.appendChild(prefsEl);

    expect(_settingsTabDataCache.has(`${PID}:account`)).toBe(false);
    await _loadSettingsAccountPane(PID);
    expect(_settingsTabDataCache.has(`${PID}:account`)).toBe(true);
  });

  it("shows a loading indicator in the ntfy placeholder while fetching", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];

    // Make api take a tick so we can observe the loading state
    let resolveApi!: () => void;
    const apiHold = new Promise<void>((r) => (resolveApi = r));
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    (globalThis as any).api = vi.fn(async (url: string) => {
      await apiHold;
      if (url === "/settings/notifications")
        return { prefs: { hitl: false, sprint: false } };
      if (url.includes("/ntfy")) return { notify_url: "", notify_email: "" };
      if (url === "/settings/mcp-config") return { base_url: "x" };
      return {};
    });
    (window as any).api = (globalThis as any).api;

    const ntfyEl = document.createElement("div");
    ntfyEl.id = `settings-ntfy-lazy-${PID}`;
    ntfyEl.textContent = "Notifications load when you open the Account tab.";
    document.body.appendChild(ntfyEl);
    const prefsEl = document.createElement("div");
    prefsEl.id = `settings-prefs-lazy-${PID}`;
    document.body.appendChild(prefsEl);

    const loadPromise = _loadSettingsAccountPane(PID);

    // While the API is held, the placeholder should show "loading…"
    const loadingEl = document.getElementById(`settings-ntfy-lazy-${PID}`);
    expect(loadingEl?.innerHTML).toContain("loading");

    resolveApi();
    await loadPromise;
  });
});

// ---------------------------------------------------------------------------
// (3) Re-clicking Account tab does NOT re-fetch (cache hit)
// ---------------------------------------------------------------------------
describe("_loadSettingsAccountPane — cache hit on re-click", () => {
  it("does not call api again when account pane is loaded a second time", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);

    // First load
    const ntfyEl = document.createElement("div");
    ntfyEl.id = `settings-ntfy-lazy-${PID}`;
    document.body.appendChild(ntfyEl);
    const prefsEl = document.createElement("div");
    prefsEl.id = `settings-prefs-lazy-${PID}`;
    document.body.appendChild(prefsEl);

    await _loadSettingsAccountPane(PID);
    const callsAfterFirst = notifCalls.length;

    // Second load (simulates re-clicking the Account tab)
    await _loadSettingsAccountPane(PID);
    const callsAfterSecond = notifCalls.length;

    // No additional api calls after the first load
    expect(callsAfterSecond).toBe(callsAfterFirst);
  });

  it("cache key is project-scoped: different projects have separate caches", async () => {
    const PID2 = "test-proj-lazy-002";
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);

    // Load account pane for PID
    const ntfyEl = document.createElement("div");
    ntfyEl.id = `settings-ntfy-lazy-${PID}`;
    document.body.appendChild(ntfyEl);
    const prefsEl = document.createElement("div");
    prefsEl.id = `settings-prefs-lazy-${PID}`;
    document.body.appendChild(prefsEl);
    await _loadSettingsAccountPane(PID);

    expect(_settingsTabDataCache.has(`${PID}:account`)).toBe(true);
    // Different project not yet loaded
    expect(_settingsTabDataCache.has(`${PID2}:account`)).toBe(false);
  });

  it("cache is cleared on page reload / _settingsTabDataCache.clear()", () => {
    // Manually seed the cache
    _settingsTabDataCache.set(`${PID}:account`, { data: "mock" });
    expect(_settingsTabDataCache.has(`${PID}:account`)).toBe(true);

    _settingsTabDataCache.clear();

    expect(_settingsTabDataCache.has(`${PID}:account`)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Integration: loadSettingsTab followed by account pane load
// ---------------------------------------------------------------------------
describe("full flow: initial load then account tab activation", () => {
  it("account-tab api calls happen only after account pane load, not during initial mount", async () => {
    const notifCalls: string[] = [];
    const ntfyCalls: string[] = [];
    const mcpCalls: string[] = [];
    setupGlobals(notifCalls, ntfyCalls, mcpCalls);
    createSettingsBody(PID);

    // Initial mount
    await loadSettingsTab(PID);
    expect(notifCalls).toHaveLength(0);
    expect(ntfyCalls).toHaveLength(0);

    // Account tab click
    await _loadSettingsAccountPane(PID);
    expect(notifCalls).toHaveLength(1);
    expect(ntfyCalls).toHaveLength(1);
  });
});
