// Tests for dashboard-workspace.ts: the pure grouping, labelling and rendering functions
// (workspace-overview-spec-r2, section 9.2). DOM wiring and the dashboard.ts entry points are in
// dashboard-workspace-view.test.ts. Repo fixtures are plain objects; the 200-repo partition check
// uses a fixed PRNG so every run sees the same input. The live-session parity vectors are read
// from the shared fixture that the Python tests load too (section 9.4).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  classifyRepo,
  countProjectsWithGuard,
  formatAgeSeconds,
  groupRepos,
  isWorkspaceOverview,
  REPO_GROUP_ORDER,
  renderWorkspaceFetchError,
  renderWorkspaceOverview,
  unknownText,
  type ActivityFacts,
  type ClassifyContext,
  type GapFacts,
  type GuardFacts,
  type IndexFacts,
  type ProjectFacts,
  type RepoFacts,
  type WorkspaceOverview,
} from "./dashboard-workspace";
import { isLiveSession, sessionAgeMs } from "./dashboard-utils";
import liveVectors from "../../tests/fixtures/workspace_live_vectors.json";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ENFORCE: ClassifyContext = { serverGuardMode: "enforce", snapshotState: "ok" };

function guard(over: Partial<GuardFacts> = {}): GuardFacts {
  return {
    settings_state: "present",
    registered: true,
    components: ["pre", "post", "brief"],
    complete: true,
    installed_mode: "enforce",
    scope: "project",
    runtime_ok: true,
    problems: [],
    record: null,
    record_mode_matches: true,
    ...over,
  };
}

function index(over: Partial<IndexFacts> = {}): IndexFacts {
  return {
    mode: "own",
    why: "own index",
    pinned: false,
    project: "alpha",
    index_root: "C:/repos/alpha",
    shadowed: [],
    partial: false,
    present: true,
    fresh: true,
    age_days: 2.5,
    last_index_write: "2026-10-25T12:00:00Z",
    nodes: 1200,
    files: 40,
    db_bytes: 2097152,
    covers_repo_root: true,
    slug_match: true,
    snapshot_state: "ok",
    ...over,
  };
}

// An index row with no index behind it (mode none): every measured field is null.
function noIndex(): Partial<IndexFacts> {
  return {
    mode: "none",
    partial: null,
    present: null,
    fresh: null,
    age_days: null,
    nodes: null,
    db_bytes: null,
    covers_repo_root: null,
  };
}

// An index that was never read: the scan budget stopped first, so the backend sends mode null
// and every probe null, with why carrying the visible reason (backend _index_unknown, spec 7.5).
const UNREAD_REASON = "not read: scan budget reached";
function unreadIndex(over: Partial<IndexFacts> = {}): Partial<IndexFacts> {
  return {
    mode: null,
    why: UNREAD_REASON,
    pinned: null,
    project: null,
    index_root: null,
    shadowed: [],
    partial: null,
    present: null,
    fresh: null,
    age_days: null,
    last_index_write: null,
    nodes: null,
    files: null,
    db_bytes: null,
    covers_repo_root: null,
    slug_match: null,
    snapshot_state: "ok",
    ...over,
  };
}

// A guard row for a repo with a settings.json that has no Meridian guard entries.
function noGuard(): Partial<GuardFacts> {
  return {
    settings_state: "present",
    registered: false,
    components: [],
    complete: false,
    installed_mode: null,
    scope: null,
    runtime_ok: null,
    problems: [],
    record: null,
    record_mode_matches: null,
  };
}

// A guard row for a repo with no .claude/settings.json at all (the default no-guard state).
// The backend reports this as settings_state "absent" with registered false (spec 2.2).
function absentGuard(): Partial<GuardFacts> {
  return { ...noGuard(), settings_state: "absent" };
}

interface RepoOver {
  key?: string;
  name?: string;
  guard?: Partial<GuardFacts>;
  index?: Partial<IndexFacts>;
  managed?: boolean | null;
}

function repo(over: RepoOver = {}): RepoFacts {
  return {
    key: over.key ?? "c:/repos/alpha",
    path: "C:/repos/alpha",
    name: over.name ?? "alpha",
    guard: guard(over.guard),
    codebase_memory: {
      managed_entry: over.managed === undefined ? true : over.managed,
      index: index(over.index),
    },
  };
}

function activity(over: Partial<ActivityFacts> = {}): ActivityFacts {
  return {
    last_guard_event_at: null,
    last_guard_event_reason: "no guard event recorded for this repo in audit.log",
    last_prospect_receipt_at: null,
    last_session_seen_at: null,
    receipt_window_days: 7,
    ...over,
  };
}

function project(over: Partial<ProjectFacts> = {}): ProjectFacts {
  return {
    id: "p1",
    name: "Alpha",
    status: "active",
    priority: 1,
    repo_keys: [],
    activity: "none",
    ...over,
  };
}

function makeModel(over: Partial<WorkspaceOverview> = {}): WorkspaceOverview {
  return {
    schema: "meridian-workspace-overview/1",
    generated_at: "2026-10-27T12:00:00Z",
    runtime: {
      version: "0.1.9",
      hostname: "host",
      location: "local",
      deployment: "self_hosted",
      database: { backend: "sqlite", connection_name: null },
      guard: {
        mode: "enforce",
        mode_scope: "server_process",
        disabled_rules: [],
        user_scope_installed: null,
        guard_dir: null,
      },
      tunnel: {
        client_process: "unknown",
        client_process_reason: "tunnel process not detected",
        live_slot_claims: 0,
        hosted_connection: "unknown",
        hosted_connection_reason: "The local server cannot see the hosted tunnel connection.",
      },
    },
    repos: {
      snapshot: {
        path: "C:/cache/snapshot.json",
        present: true,
        error: null,
        schema: "meridian-guard-snapshot/1",
        built_at: 1,
        built_age_s: 60,
        stats: { read: 3, reused: 2, dropped_missing_root: 0, kept_last_good: 1 },
        rows_total: 3,
        stale_after_days: 7,
      },
      items: [],
      total: 0,
      truncated: false,
      limit: 300,
      scan: { complete: true, bytes_read: 0, budget_bytes: 1, elapsed_s: 0.1, stopped_reason: null },
    },
    sessions: {
      live_window_s: 600,
      items: [],
      total: 0,
      truncated: false,
      totals: { total: 0, live: 0, status_active: 0, status_idle: 0 },
    },
    projects: { items: [], recent_window_s: 86400 },
    gaps: [],
    errors: [],
    ...over,
  };
}

// A model with one repo row, for the rendering tests that inspect a single row.
function modelWithRepo(r: RepoFacts, gaps: GapFacts[] = []): WorkspaceOverview {
  return makeModel({
    repos: { snapshot: makeModel().repos.snapshot, items: [r], truncated: false },
    gaps,
  });
}

// Seeded PRNG (mulberry32): the same sequence on every run and every machine.
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------
// classifyRepo: rules 1 to 5 and every rule 2 reason
// ---------------------------------------------------------------------------

describe("classifyRepo rule 1: enforced_fresh", () => {
  it("groups an enforcing, fresh, covered repo and puts the age in the reason", () => {
    expect(classifyRepo(repo(), ENFORCE)).toEqual({
      group: "enforced_fresh",
      reason: "Guard enforcing; index fresh (age 2.5 days)",
    });
  });

  it("accepts a pinned index as well as an own index", () => {
    expect(classifyRepo(repo({ index: { mode: "pin" } }), ENFORCE).group).toBe("enforced_fresh");
  });

  it("is never enforced_fresh when the pre hook is absent", () => {
    const result = classifyRepo(repo({ guard: { components: ["post", "brief"] } }), ENFORCE);
    expect(result.group).toBe("guard_not_enforcing");
    expect(result.reason).toBe("Guard hooks incomplete: missing pre");
  });

  it("is never enforced_fresh when the index is partial, even with every other field matching", () => {
    const result = classifyRepo(repo({ index: { partial: true } }), ENFORCE);
    expect(result.group).not.toBe("enforced_fresh");
    expect(result.reason).toBe("Index is partial (unfinished or broken build)");
  });

  it("is not enforced when the server guard mode is off, and never reads as enforcing", () => {
    const result = classifyRepo(repo(), { serverGuardMode: "off", snapshotState: "ok" });
    expect(result.group).toBe("guard_not_enforcing");
    expect(result.reason).toBe("Server guard mode is off (server process); not enforcing");
    expect(result.reason).not.toContain("Guard enforcing");
  });

  it("is not enforced when the snapshot is not ok", () => {
    const result = classifyRepo(repo(), { serverGuardMode: "enforce", snapshotState: "corrupt" });
    expect(result.group).toBe("guard_not_enforcing");
  });
});

describe("classifyRepo rule 2: guard_not_enforcing reasons (first match wins)", () => {
  it("(a) advisory install", () => {
    const result = classifyRepo(repo({ guard: { installed_mode: "advisory" } }), ENFORCE);
    expect(result).toEqual({
      group: "guard_not_enforcing",
      reason: "Installed in advisory mode; denies are advisory only",
    });
  });

  it("(b) runtime missing, with the problems joined", () => {
    const result = classifyRepo(
      repo({ guard: { installed_mode: "enforce", runtime_ok: false, problems: ["pre: script missing", "post: python missing"] } }),
      ENFORCE,
    );
    expect(result.reason).toBe("Guard runtime missing: pre: script missing; post: python missing");
  });

  it("(b) runtime failed with no problems listed", () => {
    const result = classifyRepo(repo({ guard: { runtime_ok: false, problems: [] } }), ENFORCE);
    expect(result.reason).toBe("Guard runtime check failed");
  });

  it("(c) hooks incomplete, listing the missing components", () => {
    // Rule 1 requires only pre, so a missing post is reached here only when another rule-1
    // condition fails first. Advisory server mode is that condition.
    const result = classifyRepo(
      repo({ guard: { components: ["pre", "brief"] } }),
      { serverGuardMode: "advisory", snapshotState: "ok" },
    );
    expect(result.reason).toBe("Guard hooks incomplete: missing post");
  });

  it("(d) server guard mode is not enforce", () => {
    const result = classifyRepo(repo(), { serverGuardMode: "advisory", snapshotState: "ok" });
    expect(result.reason).toBe("Server guard mode is advisory (server process); not enforcing");
  });

  it("(e) settings.json unreadable", () => {
    const result = classifyRepo(
      repo({ guard: { settings_state: "unreadable", registered: null, installed_mode: null, runtime_ok: null, components: [] } }),
      ENFORCE,
    );
    expect(result).toEqual({
      group: "guard_not_enforcing",
      reason: "Guard state unknown: settings.json unreadable",
    });
  });

  it("(f) index partial", () => {
    expect(classifyRepo(repo({ index: { partial: true } }), ENFORCE).reason)
      .toBe("Index is partial (unfinished or broken build)");
  });

  it("(g) index database missing", () => {
    expect(classifyRepo(repo({ index: { present: false, fresh: true } }), ENFORCE).reason)
      .toBe("Index database missing; snapshot row is stale");
  });

  it("(h) canonical and ancestor inheritance", () => {
    expect(classifyRepo(repo({ index: { mode: "canonical" } }), ENFORCE).reason)
      .toBe("Index inherited from the canonical checkout");
    expect(classifyRepo(repo({ index: { mode: "ancestor" } }), ENFORCE).reason)
      .toBe("Index inherited from a parent directory");
  });

  it("(i) coverage unknown", () => {
    expect(classifyRepo(repo({ index: { covers_repo_root: null } }), ENFORCE).reason)
      .toBe("Index coverage unknown (index read failed)");
  });

  it("(j) index does not cover the repo root", () => {
    expect(classifyRepo(repo({ index: { covers_repo_root: false } }), ENFORCE).reason)
      .toBe("Index does not cover the repo root");
  });

  it("(k) index stale, with the age and the limit", () => {
    expect(classifyRepo(repo({ index: { fresh: false, age_days: 12.4 } }), ENFORCE).reason)
      .toBe("index stale, last write 12.4 days ago (limit 7)");
  });

  it("(l) catch-all is never empty", () => {
    const result = classifyRepo(repo(), { serverGuardMode: "enforce", snapshotState: "corrupt" });
    expect(result.reason).toBe("Guard not enforcing; no single cause recorded");
    expect(result.reason.length).toBeGreaterThan(0);
  });
});

describe("classifyRepo rules 3 to 5", () => {
  it("rule 3: registered guard with no index", () => {
    expect(classifyRepo(repo({ index: noIndex() }), ENFORCE)).toEqual({
      group: "installed_no_index",
      reason: "Installed, no index covers this repo",
    });
  });

  it("rule 3: managed .mcp.json entry with no index and no guard", () => {
    const result = classifyRepo(repo({ guard: noGuard(), index: noIndex(), managed: true }), ENFORCE);
    expect(result).toEqual({
      group: "installed_no_index",
      reason: "Configured in .mcp.json; no index covers this repo",
    });
  });

  it("rule 3: snapshot missing gives an unknown index state", () => {
    const result = classifyRepo(repo({ index: noIndex() }), { serverGuardMode: "enforce", snapshotState: "missing" });
    expect(result.group).toBe("installed_no_index");
    expect(result.reason).toBe("Index state unknown: snapshot missing");
  });

  it("rule 4: index only, no guard, one reason per mode", () => {
    const cases: Array<[IndexFacts["mode"], string]> = [
      ["own", "Index present (own); no guard registered in this repo"],
      ["pin", "Index pinned to this repo (own); no guard registered in this repo"],
      ["canonical", "Index inherited from the canonical checkout; no guard registered in this repo"],
      ["ancestor", "Index inherited from a parent directory; no guard registered in this repo"],
    ];
    for (const [mode, reason] of cases) {
      const result = classifyRepo(repo({ guard: noGuard(), index: { mode } }), ENFORCE);
      expect(result).toEqual({ group: "index_only", reason });
    }
  });

  it("rule 5: nothing registered", () => {
    expect(classifyRepo(repo({ guard: noGuard(), index: noIndex(), managed: false }), ENFORCE)).toEqual({
      group: "not_installed",
      reason: "Nothing registered for this repo",
    });
  });

  it("rule 5: snapshot missing ends with the index state suffix", () => {
    const result = classifyRepo(
      repo({ guard: noGuard(), index: noIndex(), managed: false }),
      { serverGuardMode: "enforce", snapshotState: "missing" },
    );
    expect(result.group).toBe("not_installed");
    expect(result.reason).toBe("Nothing registered for this repo; index state unknown (snapshot missing)");
  });

  it("rule 5: unreadable settings, no index, and an unreadable .mcp.json get both suffixes", () => {
    const result = classifyRepo(
      repo({
        guard: { settings_state: "unreadable", registered: null, installed_mode: null, runtime_ok: null, components: [] },
        index: noIndex(),
        managed: null,
      }),
      { serverGuardMode: "enforce", snapshotState: "missing" },
    );
    expect(result).toEqual({
      group: "not_installed",
      reason: "Nothing registered for this repo; index state unknown (snapshot missing); guard state unknown (settings.json unreadable); .mcp.json unreadable",
    });
  });
});

describe("classifyRepo rule 4 with no .claude/settings.json (settings_state absent, decision F2)", () => {
  it("an index with no guard file is index_only, with the mode reason", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: { mode: "own" } }), ENFORCE)).toEqual({
      group: "index_only",
      reason: "Index present (own); no guard registered in this repo",
    });
  });

  it("a pinned or inherited index keeps its mode reason", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: { mode: "pin" } }), ENFORCE).reason)
      .toBe("Index pinned to this repo (own); no guard registered in this repo");
    expect(classifyRepo(repo({ guard: absentGuard(), index: { mode: "canonical" } }), ENFORCE).reason)
      .toBe("Index inherited from the canonical checkout; no guard registered in this repo");
  });

  it("the server guard mode adds no suffix, because nothing is registered", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: { mode: "own" } }), { serverGuardMode: "off", snapshotState: "ok" }))
      .toEqual({ group: "index_only", reason: "Index present (own); no guard registered in this repo" });
  });

  it("no index and a managed .mcp.json entry is installed_no_index", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: noIndex(), managed: true }), ENFORCE)).toEqual({
      group: "installed_no_index",
      reason: "Configured in .mcp.json; no index covers this repo",
    });
  });

  it("no index and no .mcp.json entry is not_installed, with no guard-state suffix", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: noIndex(), managed: false }), ENFORCE)).toEqual({
      group: "not_installed",
      reason: "Nothing registered for this repo",
    });
  });

  it("an unreadable .mcp.json adds only its own suffix", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: noIndex(), managed: null }), ENFORCE)).toEqual({
      group: "not_installed",
      reason: "Nothing registered for this repo; .mcp.json unreadable",
    });
  });
});

// A null index mode means the index was never read. It is unknown, not "none" and not a known
// mode, so it is never index_only and never enforced_fresh (spec 2.2, 3.3, 7.5).
describe("classifyRepo with a null index mode (index not read, scan budget reached)", () => {
  it("settings present, not registered, index null is not index_only", () => {
    expect(classifyRepo(repo({ guard: noGuard(), index: unreadIndex() }), ENFORCE)).toEqual({
      group: "not_installed",
      reason: `Nothing registered for this repo; index unknown (${UNREAD_REASON})`,
    });
  });

  it("settings absent, not registered, index null is not index_only either", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: unreadIndex(), managed: false }), ENFORCE)).toEqual({
      group: "not_installed",
      reason: `Nothing registered for this repo; index unknown (${UNREAD_REASON})`,
    });
  });

  it("a registered guard with a null index is guard_not_enforcing, with the index reason", () => {
    expect(classifyRepo(repo({ index: unreadIndex() }), ENFORCE)).toEqual({
      group: "guard_not_enforcing",
      reason: `Index unknown (${UNREAD_REASON})`,
    });
  });

  it("enforcing-looking fields with a null index are never enforced_fresh", () => {
    // Every other rule 1 field passes. The null mode alone must keep the repo out of enforced_fresh.
    const result = classifyRepo(
      repo({ index: unreadIndex({ present: true, fresh: true, covers_repo_root: true, partial: false, age_days: 2.5 }) }),
      ENFORCE,
    );
    expect(result.group).toBe("guard_not_enforcing");
    expect(result.group).not.toBe("enforced_fresh");
    expect(result.reason).toBe(`Index unknown (${UNREAD_REASON})`);
  });

  it("an unscanned guard reason comes before the index reason", () => {
    const result = classifyRepo(
      repo({
        guard: { settings_state: "not_scanned", registered: null, installed_mode: null, runtime_ok: null, components: [] },
        index: unreadIndex(),
        managed: null,
      }),
      ENFORCE,
    );
    expect(result).toEqual({ group: "guard_not_enforcing", reason: "Guard state unknown: settings.json not_scanned" });
  });

  it("a null index with an empty reason still shows a non-blank reason", () => {
    expect(classifyRepo(repo({ index: unreadIndex({ why: "" }) }), ENFORCE).reason).toBe("Index unknown (not recorded)");
  });

  it("a null index with the snapshot missing keeps the snapshot suffix before the index suffix", () => {
    expect(classifyRepo(repo({ guard: absentGuard(), index: unreadIndex(), managed: true }), { serverGuardMode: "enforce", snapshotState: "missing" })).toEqual({
      group: "not_installed",
      reason: `Nothing registered for this repo; index state unknown (snapshot missing); index unknown (${UNREAD_REASON})`,
    });
  });
});

// ---------------------------------------------------------------------------
// groupRepos: partition and counts
// ---------------------------------------------------------------------------

describe("groupRepos", () => {
  it("gives all zeros for empty input", () => {
    const { groups, counts } = groupRepos([], ENFORCE);
    for (const group of REPO_GROUP_ORDER) {
      expect(counts[group]).toBe(0);
      expect(groups[group]).toEqual([]);
    }
  });

  it("partitions 200 seeded repos: counts sum to the input length and each input lands once", () => {
    const rand = mulberry32(20261027);
    const pick = <T>(options: readonly T[]): T => options[Math.floor(rand() * options.length)] as T;
    const repos: RepoFacts[] = [];
    for (let i = 0; i < 200; i++) {
      const settings = pick(["present", "present", "absent", "unreadable", "not_scanned"] as const);
      repos.push(
        repo({
          key: `c:/repos/r${i}`,
          name: `r${i}`,
          guard: {
            settings_state: settings,
            registered: pick([true, false, null]),
            components: ["pre", "post", "brief"].filter(() => rand() < 0.7),
            installed_mode: pick(["enforce", "advisory", null] as const),
            runtime_ok: pick([true, false, null]),
          },
          index: {
            mode: pick(["own", "pin", "canonical", "ancestor", "none", null] as const),
            present: pick([true, false, null]),
            fresh: pick([true, false, null]),
            partial: pick([true, false, null]),
            covers_repo_root: pick([true, false, null]),
            age_days: pick([1.5, 9.2, null]),
          },
          managed: pick([true, false, null]),
        }),
      );
    }
    // The seeded set must include null index modes, or the null checks below prove nothing.
    expect(repos.filter((r) => r.codebase_memory.index.mode === null).length).toBeGreaterThan(0);
    const contexts: ClassifyContext[] = [
      ENFORCE,
      { serverGuardMode: "off", snapshotState: "ok" },
      { serverGuardMode: "enforce", snapshotState: "missing" },
    ];
    for (const ctx of contexts) {
      const { groups, counts } = groupRepos(repos, ctx);
      const total = REPO_GROUP_ORDER.reduce((sum, group) => sum + counts[group], 0);
      expect(total).toBe(repos.length);

      const seen: string[] = [];
      for (const group of REPO_GROUP_ORDER) {
        expect(groups[group].length).toBe(counts[group]);
        for (const item of groups[group]) {
          seen.push(item.key);
          expect(item.reason.length).toBeGreaterThan(0);
          // A null index mode is unknown: never index_only, enforced_fresh or installed_no_index.
          if (item.codebase_memory.index.mode === null) {
            expect(["guard_not_enforcing", "not_installed"]).toContain(group);
          }
        }
      }
      expect([...seen].sort()).toEqual(repos.map((r) => r.key).sort());
    }
  });
});

// ---------------------------------------------------------------------------
// Labels, ages and project counts
// ---------------------------------------------------------------------------

describe("formatAgeSeconds and unknownText", () => {
  it("formats the boundary values from the spec", () => {
    expect(formatAgeSeconds(null)).toBe("unknown");
    expect(formatAgeSeconds(undefined)).toBe("unknown");
    expect(formatAgeSeconds(0.4)).toBe("0s");
    expect(formatAgeSeconds(42)).toBe("42s");
    expect(formatAgeSeconds(300)).toBe("5m");
    expect(formatAgeSeconds(7200)).toBe("2h");
    expect(formatAgeSeconds(172800)).toBe("2d");
  });

  it("keeps the sign for a session last seen ahead of the server clock", () => {
    expect(formatAgeSeconds(-300)).toBe("-5m");
  });

  it("unknownText prefixes the reason", () => {
    expect(unknownText("x")).toBe("unknown: x");
  });
});

describe("countProjectsWithGuard", () => {
  it("counts a project once when any of its repo keys is a registered repo", () => {
    const repos = [
      repo({ key: "c:/a", guard: { registered: true } }),
      repo({ key: "c:/b", guard: { registered: true } }),
      repo({ key: "c:/c", guard: { registered: false } }),
    ];
    const projects = [
      project({ id: "p1", repo_keys: ["c:/c", "c:/a", "c:/b"] }),
      project({ id: "p2", repo_keys: ["c:/c"] }),
      project({ id: "p3", repo_keys: [] }),
    ];
    expect(countProjectsWithGuard(projects, repos)).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Live sessions: isLiveSession boundary and the shared parity vectors (spec section 9.4)
// ---------------------------------------------------------------------------

describe("isLiveSession boundary", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-10-27T12:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("600000 ms is live", () => {
    expect(isLiveSession({ status: "active", last_seen: "2026-10-27T11:50:00Z" })).toBe(true);
  });

  it("600001 ms is not live", () => {
    expect(isLiveSession({ status: "active", last_seen: "2026-10-27T11:49:59.999Z" })).toBe(false);
  });

  it("a negative age is not live", () => {
    expect(isLiveSession({ status: "active", last_seen: "2026-10-27T12:00:05Z" })).toBe(false);
  });

  it("an idle session is not live", () => {
    expect(isLiveSession({ status: "idle", last_seen: "2026-10-27T11:59:00Z" })).toBe(false);
  });
});

describe("shared parity vectors (tests/fixtures/workspace_live_vectors.json)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("the fixture declares the 600 s live window", () => {
    expect(liveVectors.live_window_s).toBe(600);
    expect(liveVectors.vectors.length).toBeGreaterThan(0);
  });

  it.each(liveVectors.vectors)("$name agrees with the backend expectation", (vector) => {
    vi.setSystemTime(Date.parse(liveVectors.now));
    const session = { status: vector.status, last_seen: vector.last_seen };
    expect(isLiveSession(session)).toBe(vector.expected_live);
    const ageMs = sessionAgeMs(session);
    const ageS = Number.isFinite(ageMs) ? Math.round(ageMs / 100) / 10 : null;
    expect(ageS).toBe(vector.expected_age_s);
  });
});

// ---------------------------------------------------------------------------
// renderWorkspaceOverview: escaping, empty states, truncation, errors, guard off
// ---------------------------------------------------------------------------

describe("renderWorkspaceOverview", () => {
  it("escapes repo, path, session, project and gap text", () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const model = makeModel({
      repos: {
        snapshot: makeModel().repos.snapshot,
        items: [repo({ name: hostile, key: "c:/x" })],
        truncated: false,
      },
      sessions: {
        items: [{
          id: "s1",
          name: "<script>alert(2)</script>",
          project_name: hostile,
          status: "active",
          age_s: 5,
          live: true,
          worktree: null,
        }],
        truncated: false,
      },
      gaps: [{ code: "x", message: hostile }],
    });
    const html = renderWorkspaceOverview(model);
    expect(html).not.toContain("<img src=x onerror=alert(1)>");
    expect(html).not.toContain("<script>alert(2)</script>");
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).toContain("&lt;script&gt;alert(2)&lt;/script&gt;");
  });

  it("escapes a session's worktree branch (S5)", () => {
    const hostile = `"><img src=x onerror=alert(3)>`;
    const model = makeModel({
      sessions: {
        items: [{
          id: "s-wt",
          name: "branch-session",
          status: "idle",
          age_s: 60,
          live: false,
          worktree: { path: "C:/wt/one", branch: hostile, repo_key: "c:/x" },
        }],
        truncated: false,
      },
    });
    const html = renderWorkspaceOverview(model);
    expect(html).not.toContain(hostile);
    expect(html).not.toContain("<img src=x onerror=alert(3)>");
    expect(html).toContain("&quot;&gt;&lt;img src=x onerror=alert(3)&gt;");
  });

  it("escapes a project name in the Projects section (S5)", () => {
    const hostile = `"><img src=x onerror=alert(4)>`;
    const html = renderWorkspaceOverview(makeModel({ projects: { items: [project({ name: hostile })] } }));
    expect(html).not.toContain(hostile);
    expect(html).not.toContain("<img src=x onerror=alert(4)>");
    expect(html).toContain("&quot;&gt;&lt;img src=x onerror=alert(4)&gt;");
  });

  it("escapes the repo path and the snapshot path (S5)", () => {
    const hostile = `"><img src=x onerror=alert(5)>`;
    const model = makeModel({
      repos: {
        snapshot: { ...makeModel().repos.snapshot, path: hostile },
        items: [{ ...repo({ key: "c:/p" }), path: hostile }],
        truncated: false,
      },
    });
    const html = renderWorkspaceOverview(model);
    expect(html).not.toContain(hostile);
    expect(html).not.toContain("<img src=x onerror=alert(5)>");
    expect(html).toContain("&quot;&gt;&lt;img src=x onerror=alert(5)&gt;");
  });

  it("shows each empty-state text when its array is empty", () => {
    const empty = makeModel({
      repos: { snapshot: { ...makeModel().repos.snapshot, error: "missing" }, items: [], truncated: false },
    });
    const html = renderWorkspaceOverview(empty);
    expect(html).toContain("No codebase-memory index snapshot yet. It is written when a Claude Code session starts in a repo with the guard installed.");
    expect(html).toContain("No repos known on this machine yet. Repos appear once a project reports a working directory, a worktree is registered, or a guard is installed.");
    expect(html).toContain("No sessions recorded.");
    expect(html).toContain("No projects.");
  });

  it("says when no repo has a registered guard", () => {
    const model = makeModel({
      repos: { snapshot: makeModel().repos.snapshot, items: [repo({ guard: noGuard(), index: noIndex(), managed: false })], truncated: false },
    });
    expect(renderWorkspaceOverview(model)).toContain("No Meridian guard registered in any known repo.");
  });

  it("an absent settings.json with an index shows under Index only, no guard", () => {
    const html = renderWorkspaceOverview(modelWithRepo(repo({ guard: absentGuard(), index: { mode: "own" } })));
    expect(html).toContain('Index only, no guard <span class="ws-count">1</span>');
    expect(html).toContain("guard: none");
  });

  it("shows the truncation notices with the totals", () => {
    const repos = Array.from({ length: 300 }, (_, i) => repo({ key: `c:/r${i}`, name: `r${i}` }));
    const sessions = Array.from({ length: 200 }, (_, i) => ({
      id: `s${i}`, name: `s${i}`, status: "idle", age_s: 60, worktree: null,
    }));
    const model = makeModel({
      repos: { snapshot: makeModel().repos.snapshot, items: repos, total: 301, truncated: true, limit: 300 },
      sessions: { items: sessions, total: 201, truncated: true },
    });
    const html = renderWorkspaceOverview(model);
    expect(html).toContain("Showing 300 of 301 repos, sorted by path.");
    expect(html).toContain("Showing 200 of 201 sessions, newest first.");
  });

  it("labels truncation on each repo chip and in the guard-projects chip, not in the heading (F9, F12)", () => {
    const repos = Array.from({ length: 300 }, (_, i) => repo({ key: `c:/r${i}`, name: `r${i}` }));
    const model = makeModel({
      repos: { snapshot: makeModel().repos.snapshot, items: repos, total: 301, truncated: true, limit: 300 },
      projects: { items: [project({ repo_keys: ["c:/r0"] })] },
    });
    const html = renderWorkspaceOverview(model);
    expect(html.split("of the 300 shown").length - 1).toBe(5);
    expect(html).not.toContain("Repo groups (of the");
    expect(html).toContain("projects with a registered guard: 1 (counted over the 300 repos shown)");
  });

  it("shows the scan-budget notice exactly once, in Gaps (F10)", () => {
    const model = makeModel({
      repos: {
        snapshot: makeModel().repos.snapshot,
        items: [],
        truncated: false,
        scan: { complete: false, stopped_reason: "bytes" },
      },
    });
    const html = renderWorkspaceOverview(model);
    expect(html.split("The filesystem scan stopped").length - 1).toBe(1);
    expect(html.indexOf("The filesystem scan stopped")).toBeGreaterThan(html.indexOf('<h3 class="ws-heading">Gaps</h3>'));
  });

  it("names an errored section with its code and never echoes a raw error", () => {
    const html = renderWorkspaceOverview(makeModel({ errors: [{ section: "sessions", code: "db_error" }] }));
    expect(html).toContain("sessions: This section could not be read (db_error).");
  });

  it("with guard mode off, no row reads enforcing and the header names the mode", () => {
    const model = makeModel({
      runtime: { ...makeModel().runtime, guard: { ...makeModel().runtime.guard, mode: "off" } },
      repos: { snapshot: makeModel().repos.snapshot, items: [repo()], truncated: false },
    });
    const html = renderWorkspaceOverview(model);
    expect(html).toContain("Server guard mode is off");
    expect(html).not.toContain("Guard enforcing");
  });

  it("renders an unknown tunnel as not detected, never as not running", () => {
    const html = renderWorkspaceOverview(makeModel());
    expect(html).toContain("tunnel process not detected");
    expect(html).not.toContain("not running");
  });
});

describe("renderWorkspaceOverview: session counts without backend totals (S4)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-10-27T12:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("counts live sessions with the shared isLiveSession rule when sessions.totals is absent", () => {
    // No live flags and no totals: the counts and the row chips must both come from isLiveSession.
    const model = makeModel({
      sessions: {
        items: [
          { id: "a", name: "fresh-active", status: "active", last_seen: "2026-10-27T11:59:00Z", age_s: 60, worktree: null },
          { id: "b", name: "stale-active", status: "active", last_seen: "2026-10-27T11:00:00Z", age_s: 3600, worktree: null },
          { id: "c", name: "fresh-idle", status: "idle", last_seen: "2026-10-27T11:59:30Z", age_s: 30, worktree: null },
        ],
        truncated: false,
      },
    });
    const html = renderWorkspaceOverview(model);
    expect(html).toContain('<span class="ws-chip">live: 1</span>');
    expect(html).toContain('<span class="ws-chip">status active: 2</span>');
    expect(html).toContain('<span class="ws-chip">status idle: 1</span>');
    expect(html).toContain('<span class="ws-chip">live</span>');
    expect(html).toContain('<span class="ws-chip">active, not live</span>');
  });
});

describe("renderWorkspaceOverview: unknown values carry their reason as visible text (F8)", () => {
  it("index age, nodes and size render unknown with a reason, not a bare unknown", () => {
    const html = renderWorkspaceOverview(
      modelWithRepo(repo({ index: { age_days: null, nodes: null, db_bytes: null } })),
    );
    expect(html).toContain("unknown: no last-write time recorded");
    expect(html).toContain("unknown: node count not recorded");
    expect(html).toContain("unknown: database size not read");
    expect(html).not.toContain("nodes unknown");
    expect(html).not.toContain("size unknown");
  });

  it("an unreadable .mcp.json reads unknown with its reason as visible text", () => {
    const html = renderWorkspaceOverview(modelWithRepo(repo({ managed: null })));
    expect(html).toContain("unknown: .mcp.json unreadable");
  });

  it("a null last guard event with no reason reads unknown with a reason", () => {
    const html = renderWorkspaceOverview(
      modelWithRepo({ ...repo(), activity: activity({ last_guard_event_at: null, last_guard_event_reason: null }) }),
    );
    expect(html).toContain("last guard event: unknown: no guard event reason reported");
  });

  it("a null hostname and version render unknown with a reason", () => {
    const html = renderWorkspaceOverview(makeModel({
      runtime: { ...makeModel().runtime, hostname: null, version: null },
    }));
    expect(html).toContain("unknown: hostname not reported");
    expect(html).toContain("unknown: version not reported");
  });
});

describe("renderWorkspaceOverview: a null index mode is unknown with its reason (spec 7.5)", () => {
  it("settings present, not registered, index null: the chip shows the reason and the row is not Index only", () => {
    const html = renderWorkspaceOverview(modelWithRepo(repo({ guard: noGuard(), index: unreadIndex() })));
    expect(html).toContain(`<span class="ws-chip ws-chip--unknown">index: unknown (${UNREAD_REASON})</span>`);
    expect(html).toContain('Nothing registered <span class="ws-count">1</span>');
    expect(html).toContain('Index only, no guard <span class="ws-count">0</span>');
  });

  it("never prints a bare unknown, null or undefined index chip for a null mode", () => {
    const html = renderWorkspaceOverview(modelWithRepo(repo({ index: unreadIndex() })));
    expect(html).not.toContain("index: null");
    expect(html).not.toContain("index: undefined");
    expect(html).not.toContain("index: unknown</span>");
    expect(html).toContain(`index: unknown (${UNREAD_REASON})`);
  });

  it("a registered guard with a null index shows the index reason on the row", () => {
    const html = renderWorkspaceOverview(modelWithRepo(repo({ index: unreadIndex() })));
    expect(html).toContain('Guard not enforcing <span class="ws-count">1</span>');
    expect(html).toContain(`<div class="ws-reason">Index unknown (${UNREAD_REASON})</div>`);
  });

  it("settings absent with a null index sits under Nothing registered, with the reason on the chip and the row", () => {
    const html = renderWorkspaceOverview(modelWithRepo(repo({ guard: absentGuard(), index: unreadIndex(), managed: false })));
    expect(html).toContain('Nothing registered <span class="ws-count">1</span>');
    expect(html).toContain(`<span class="ws-chip ws-chip--unknown">index: unknown (${UNREAD_REASON})</span>`);
    expect(html).toContain(`<div class="ws-reason">Nothing registered for this repo; index unknown (${UNREAD_REASON})</div>`);
  });
});

describe("renderWorkspaceOverview: last prospect receipt (F4)", () => {
  const defaultRootGap: GapFacts = {
    code: "receipts_default_root_excluded",
    message: "Receipts whose repo root came from the server's default checkout are not attributed to any repo.",
  };
  const truncatedGap: GapFacts = {
    code: "receipts_window_truncated",
    message: "The receipt query hit its row limit, so the last-receipt time can be older than the true value.",
  };

  it("a null receipt time with the default-root gap is unknown with its reason, never 'none attributed'", () => {
    const html = renderWorkspaceOverview(modelWithRepo({ ...repo(), activity: activity() }, [defaultRootGap]));
    expect(html).toContain("last prospect receipt: unknown: Receipts whose repo root came from the server&#39;s default checkout are not attributed to any repo.");
    expect(html).not.toContain("none attributed in the last");
  });

  it("a null receipt time with the truncation gap gives the truncation reason", () => {
    const html = renderWorkspaceOverview(modelWithRepo({ ...repo(), activity: activity() }, [truncatedGap]));
    expect(html).toContain("last prospect receipt: unknown: The receipt query hit its row limit, so the last-receipt time can be older than the true value.");
  });

  it("with neither gap, a null receipt time keeps the affirmative text", () => {
    const html = renderWorkspaceOverview(modelWithRepo({ ...repo(), activity: activity() }, []));
    expect(html).toContain("last prospect receipt: none attributed in the last 7 days");
  });

  it("a recorded receipt time is shown even when a gap is present", () => {
    const html = renderWorkspaceOverview(
      modelWithRepo({ ...repo(), activity: activity({ last_prospect_receipt_at: "2026-10-26T10:00:00Z" }) }, [defaultRootGap]),
    );
    expect(html).toContain("last prospect receipt: 2026-10-26T10:00:00Z");
    expect(html).not.toContain("unknown: Receipts whose repo root");
  });
});

// ---------------------------------------------------------------------------
// Response shape and fetch-error text
// ---------------------------------------------------------------------------

describe("isWorkspaceOverview and renderWorkspaceFetchError", () => {
  it("accepts a complete model and rejects a non-object body", () => {
    expect(isWorkspaceOverview(makeModel())).toBe(true);
    expect(isWorkspaceOverview(null)).toBe(false);
    expect(isWorkspaceOverview("<html>")).toBe(false);
    expect(isWorkspaceOverview({ repos: {} })).toBe(false);
  });

  it("shows the HTTP status, or no response when there is none", () => {
    expect(renderWorkspaceFetchError(500)).toContain("Could not read the workspace overview (HTTP 500).");
    expect(renderWorkspaceFetchError(null)).toContain("Could not read the workspace overview (no response).");
    expect(renderWorkspaceFetchError(500)).toContain('data-ws-action="retry"');
  });
});
