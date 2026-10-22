// Behavioural test of how dashboard.ts wires the Goal tab to the GoalField controllers (fc779141).
//
// dashboard-goal-conflict.test.ts covers the controller in isolation.  What it cannot see is
// the glue in dashboard.ts: refreshGoal() feeding each of the three fields, initGoalFields()
// building their save requests, the Save buttons, the tab-close / vtab-switch leave guards, the
// beforeunload guard and the goal_updated event routing.  A source scan for those names passes
// even when a call sits in a dead branch, so this file loads the REAL dashboard.ts (and the real
// dashboard.html markup) into jsdom, answers its requests from a small in-memory server that
// implements the same stamp / 409 contract as POST /goal, /goal/north-star and /goal/sprint, and
// drives the Goal tab the way a person and an agent do.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

type Field = "version_goal" | "north_star" | "sprint";

// ---------------------------------------------------------------------------
// In-memory server: the goal endpoints with the real per-field stamp + 409 contract
// ---------------------------------------------------------------------------

const PROJECT = { id: "p1", name: "Proj One" };

function content(editable: string, shipped = "SHIPPED\n- login page", auto = "auto block one", title = "v2.3 — auth sprint"): string {
  return `${title}\n\n${shipped}\n\n${editable}\n--- AUTO BLOCKS BELOW ---\n${auto}`;
}

class FakeServer {
  clock = 0;
  version = 1;
  goal: unknown = content("CURRENT FOCUS\nship the login flow");
  northStar = "be the memory layer";
  sprint = "fix login";
  stamps: Record<Field, string> = { version_goal: "", north_star: "", sprint: "" };
  posts: Array<{ path: string; body: any; status: number }> = [];
  gets = 0;
  failNextPost: { status: number; text: string } | null = null;
  networkDownOnPost = false;
  failGets = false;
  /** When set, GET /goal waits for it: a slow refresh, as on a hosted server. */
  gate: Promise<void> | null = null;
  sessions: Array<{ name: string; status: string }> = [];

  constructor() {
    this.reset();
  }

  /** Stamps look like the server's "YYYY-MM-DD HH:MM:SS"; every write gets a new one. */
  next(): string {
    this.clock += 1;
    return `2026-10-07 10:${String(Math.floor(this.clock / 60)).padStart(2, "0")}:${String(this.clock % 60).padStart(2, "0")}`;
  }

  reset(): void {
    this.clock = 0;
    this.version = 1;
    this.goal = content("CURRENT FOCUS\nship the login flow");
    this.northStar = "be the memory layer";
    this.sprint = "fix login";
    // Three DIFFERENT stamps, so a save that reads or sends another field's stamp is visible.
    this.stamps = { version_goal: this.next(), north_star: this.next(), sprint: this.next() };
    this.posts = [];
    this.gets = 0;
    this.failNextPost = null;
    this.networkDownOnPost = false;
    this.failGets = false;
    this.gate = null;
    this.sessions = [];
  }

  payload(): Record<string, unknown> {
    return {
      content: this.goal,
      version: this.version,
      north_star: this.northStar,
      sprint: this.sprint,
      updated_at: this.stamps.version_goal,
      field_updated_at: { ...this.stamps },
      decisions: "",
    };
  }

  /** A write by someone else (an agent): changes the field, moves its stamp, bumps the version. */
  write(field: Field, value: unknown): void {
    if (field === "version_goal") this.goal = value;
    else if (field === "north_star") this.northStar = String(value);
    else this.sprint = String(value);
    this.stamps[field] = this.next();
    this.version += 1;
  }

  conflict(field: Field, expected: string): { status: number; text: string } {
    const value = field === "version_goal" ? this.goal : field === "north_star" ? this.northStar : this.sprint;
    return {
      status: 409,
      text: JSON.stringify({
        detail: {
          error: "goal_conflict",
          message: "changed by someone else",
          field,
          expected_updated_at: expected,
          current: { value, updated_at: this.stamps[field], version: this.version },
          field_updated_at: { ...this.stamps },
        },
      }),
    };
  }

  handle(url: string, init: RequestInit | undefined): { status: number; text: string } {
    const method = (init?.method || "GET").toUpperCase();
    const path = url.split("?")[0];
    const base = `/projects/${PROJECT.id}`;
    if (method === "GET") {
      if (path === "/projects") return ok([PROJECT]);
      if (path === `${base}/goal`) {
        this.gets += 1;
        return this.failGets ? { status: 500, text: "boom" } : ok(this.payload());
      }
      if (path === `${base}/sessions`) return ok(this.sessions);
      if (/\/(sprint-items|tasks|notes|decisions-pinned|hitl|files|documents|insights)$/.test(path) || path === "/hitl") return ok([]);
      return ok({});
    }
    if (method === "POST" && path.startsWith(`${base}/goal`)) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      let status = 200;
      let text = "";
      if (this.networkDownOnPost) throw new TypeError("Failed to fetch");
      if (this.failNextPost) {
        ({ status, text } = this.failNextPost);
        this.failNextPost = null;
      } else {
        const field: Field = path === `${base}/goal` ? "version_goal" : path.endsWith("/north-star") ? "north_star" : "sprint";
        const expected = body.expected_updated_at;
        if (expected !== undefined && expected !== this.stamps[field]) {
          ({ status, text } = this.conflict(field, expected));
        } else {
          this.write(field, field === "version_goal" ? body.content : field === "north_star" ? body.north_star : body.sprint);
          text = JSON.stringify(this.payload());
        }
      }
      this.posts.push({ path: path.slice(base.length), body, status });
      return { status, text };
    }
    return ok({});
  }
}

function ok(body: unknown): { status: number; text: string } {
  return { status: 200, text: JSON.stringify(body) };
}

const server = new FakeServer();

// ---------------------------------------------------------------------------
// Page + dashboard.ts
// ---------------------------------------------------------------------------

let confirmMock: ReturnType<typeof vi.fn>;
const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms));

function installPage(): void {
  const html = readFileSync(resolve(__dirname, "../templates/dashboard.html"), "utf8")
    .replace(/<script[\s\S]*?<\/script>/g, "")
    .replace(/\{\{[\s\S]*?\}\}/g, "")
    .replace(/\{%[\s\S]*?%\}/g, "");
  const body = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
  document.body.innerHTML = body ? body[1] : html;
  (globalThis as any).fetch = async (url: unknown, init?: RequestInit) => {
    const isGoalGet = String(url).split("?")[0] === `/projects/${PROJECT.id}/goal` && (init?.method || "GET") === "GET";
    if (isGoalGet && server.gate) await server.gate;
    const res = server.handle(String(url), init);
    return new Response(res.status === 204 ? null : res.text, { status: res.status, headers: { "content-type": "application/json" } });
  };
  (globalThis as any).WebSocket = class {
    close() {}
    addEventListener() {}
    send() {}
  };
}

const w = window as any;

const ID: Record<Field, string> = { version_goal: "goal-p1", north_star: "goal-north-star-p1", sprint: "goal-sprint-p1" };
const el = (k: Field) => document.getElementById(ID[k]) as HTMLTextAreaElement;
const bar = (k: Field) => {
  const b = document.querySelector(`.goal-conflict-bar[data-goal-field="${k}"]`) as HTMLElement | null;
  return b && !b.hidden ? b : null;
};
const act = (k: Field, a: string) =>
  (document.querySelector(`.goal-conflict-bar[data-goal-field="${k}"] button[data-act="${a}"]`) as HTMLButtonElement).click();
const dialog = () => document.querySelector(".goal-leave-dialog") as HTMLElement | null;
const choose = (c: string) =>
  (document.querySelector(`.goal-leave-dialog button[data-choice="${c}"]`) as HTMLButtonElement).click();

/** What a person does: focus the field, type, (optionally) leave it so the blur-save runs. */
function type(k: Field, text: string): void {
  const e = el(k);
  e.focus();
  e.value = text;
  e.dispatchEvent(new Event("input", { bubbles: true }));
}
function leave(k: Field): void {
  const e = el(k);
  if (document.activeElement === e) e.blur();
  else e.dispatchEvent(new Event("blur"));
}
const esc = (k: Field) => {
  const ev = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  el(k).dispatchEvent(ev);
  return ev;
};

async function settle(): Promise<void> {
  await sleep(25);
}

/** The page's own refresh, awaited (what the WebSocket handler fires without waiting). */
async function refresh(): Promise<void> {
  await w.refreshGoal(PROJECT.id);
  await settle();
}

/** An agent writes `field`, the server pushes goal_updated, the dashboard re-fetches. */
async function remote(field: Field, value: unknown, by: unknown = { kind: "agent", source: "mcp", id: null }): Promise<void> {
  server.write(field, value);
  const before = server.gets;
  w.handleWsEvent(PROJECT.id, {
    type: "goal_updated",
    version: server.version,
    changed_fields: [field],
    changed_by: by,
    updated_at: server.stamps.version_goal,
    field_updated_at: { ...server.stamps },
  });
  await vi.waitFor(() => expect(server.gets).toBeGreaterThan(before), { timeout: 3000 });
  await settle();
}

async function openPanel(): Promise<void> {
  w.closeTab?.(PROJECT.id);
  w.openTab(PROJECT);
  await vi.waitFor(() => expect(el("version_goal")?.value).toContain("ship the login flow"), { timeout: 3000 });
  await vi.waitFor(() => expect(el("sprint").value).toBe("fix login"), { timeout: 3000 });
  // The current-focus select / input pair is wired 200ms after the panel opens (it gets its
  // "Custom..." option in the same step as the syncer that setValue calls).
  await vi.waitFor(() => expect(document.getElementById("goal-sprint-select-p1")?.innerHTML).toContain("__custom__"), { timeout: 3000 });
}

beforeAll(async () => {
  installPage();
  await import("./dashboard");
  await vi.waitFor(() => expect(document.getElementById("goal-p1")).not.toBeNull(), { timeout: 8000 });
}, 60000);

beforeEach(async () => {
  try { localStorage.clear(); } catch (_) { /* no storage */ }
  server.reset();
  confirmMock = vi.fn(() => true);
  w.confirm = confirmMock;
  (globalThis as any).confirm = confirmMock;
  await openPanel();
});

afterEach(() => {
  document.querySelectorAll(".goal-leave-overlay").forEach((n) => n.remove());
  vi.restoreAllMocks();
});

const NEW_FOCUS = "CURRENT FOCUS\nan agent re-planned the sprint";

// ---------------------------------------------------------------------------
// 1. Not editing: live updates reach all four displays
// ---------------------------------------------------------------------------

describe("a field nobody is editing follows the server live", () => {
  it("north star", async () => {
    await remote("north_star", "an agent's new north star");
    expect(el("north_star").value).toBe("an agent's new north star");
    expect(bar("north_star")).toBeNull();
    expect(el("north_star").classList.contains("dirty")).toBe(false);
  });

  it("current focus (the field refreshGoal used to write directly)", async () => {
    await remote("sprint", "ship the billing page");
    expect(el("sprint").value).toBe("ship the billing page");
    expect(bar("sprint")).toBeNull();
  });

  it("version goal, its read-only zones and the version label", async () => {
    await remote("version_goal", content(NEW_FOCUS, "SHIPPED\n- billing", "auto block two", "v2.4 — billing"));
    expect(el("version_goal").value).toBe(NEW_FOCUS);
    expect(document.getElementById("goal-title-p1")!.textContent).toBe("v2.4 — billing");
    expect(document.getElementById("goal-shipped-p1")!.textContent).toBe("SHIPPED\n- billing");
    expect(document.getElementById("goal-autoblocks-p1")!.textContent).toBe("auto block two");
    expect(document.getElementById("goal-version-p1")!.textContent).toBe(`v${server.version}`);
    expect(bar("version_goal")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Editing + a remote change: the draft survives, the bar offers the choice
// ---------------------------------------------------------------------------

const EDITS: Array<{ key: Field; mine: string; theirs: unknown; theirsText: string; label: string }> = [
  { key: "north_star", mine: "my own north star", theirs: "their north star", theirsText: "their north star", label: "north star" },
  { key: "sprint", mine: "my own focus", theirs: "their focus", theirsText: "their focus", label: "current focus" },
  {
    key: "version_goal",
    mine: "CURRENT FOCUS\nmy plan",
    theirs: content("CURRENT FOCUS\ntheir plan"),
    theirsText: "CURRENT FOCUS\ntheir plan",
    label: "version goal",
  },
];

describe.each(EDITS)("editing the $label while a remote change arrives", ({ key, mine, theirs, theirsText, label }) => {
  it("keeps the draft and shows the Changed elsewhere bar, saying it was an agent", async () => {
    type(key, mine);
    await remote(key, theirs);
    expect(el(key).value).toBe(mine);
    const b = bar(key);
    expect(b).not.toBeNull();
    expect(b!.textContent).toContain("Changed elsewhere");
    expect(b!.textContent).toContain("by an agent");
    expect(b!.textContent).toContain(label);
    expect(el(key).classList.contains("dirty")).toBe(true);
    expect(server.posts).toHaveLength(0);
  });

  it("says by a person when the event came from a human", async () => {
    type(key, mine);
    await remote(key, theirs, { kind: "human", source: "dashboard", id: "adam" });
    expect(bar(key)!.textContent).toContain("by a person");
  });

  it("does not hold the other fields back: they still update live", async () => {
    type(key, mine);
    const other: Field = key === "north_star" ? "sprint" : "north_star";
    await remote(other, "someone else moved this one");
    expect(el(key).value).toBe(mine);
    expect(bar(key)).toBeNull();
    expect(el(other).value).toBe("someone else moved this one");
  });

  it("Take theirs shows the remote text and saves nothing", async () => {
    type(key, mine);
    await remote(key, theirs);
    act(key, "take");
    await settle();
    expect(el(key).value).toBe(theirsText);
    expect(bar(key)).toBeNull();
    expect(el(key).classList.contains("dirty")).toBe(false);
    expect(server.posts).toHaveLength(0);
  });

  it("Keep mine saves the draft on top of the remote change, based on the remote stamp", async () => {
    type(key, mine);
    await remote(key, theirs);
    const remoteStamp = server.stamps[key];
    act(key, "keep");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    await settle();
    expect(server.posts[0].status).toBe(200);
    expect(server.posts[0].body.expected_updated_at).toBe(remoteStamp);
    expect(el(key).value).toBe(mine);
    expect(bar(key)).toBeNull();
    const stored = key === "version_goal" ? String(server.goal) : key === "north_star" ? server.northStar : server.sprint;
    expect(stored).toContain(mine);
  });

  it("See both shows the two texts side by side as read-only lines", async () => {
    type(key, mine);
    await remote(key, theirs);
    act(key, "both");
    const diff = bar(key)!.querySelector(".goal-conflict-diff") as HTMLElement;
    expect(diff.hidden).toBe(false);
    const mineLines = Array.from(diff.querySelectorAll(".gcd-mine .gcd-text")).map((n) => n.textContent);
    const theirLines = Array.from(diff.querySelectorAll(".gcd-theirs .gcd-text")).map((n) => n.textContent);
    expect(mineLines.join("\n")).toContain(mine.split("\n").pop()!);
    expect(theirLines.join("\n")).toContain(theirsText.split("\n").pop()!);
  });
});

describe("a failed or absent event still cannot lose the draft", () => {
  it("a refresh with no event naming the fields still shows the bar (without who)", async () => {
    type("north_star", "my own north star");
    server.write("north_star", "their north star");
    await refresh();
    expect(el("north_star").value).toBe("my own north star");
    const b = bar("north_star");
    expect(b).not.toBeNull();
    expect(b!.textContent).not.toContain("by an agent");
  });

  it("a refresh that fails to load does not blank a field being edited", async () => {
    type("version_goal", "CURRENT FOCUS\nmy plan");
    server.failGets = true;
    await refresh();
    expect(el("version_goal").value).toBe("CURRENT FOCUS\nmy plan");
    expect(document.getElementById("goal-version-p1")!.textContent).toBe("(load failed)");
  });

  it("a refresh that fails to load still shows the unavailable state when nobody is editing", async () => {
    server.failGets = true;
    await refresh();
    expect(el("version_goal").value).toBe("");
    expect(el("version_goal").placeholder).toBe("Goal state failed to load.");
  });
});

// ---------------------------------------------------------------------------
// 3. Saving: blur-save, the Save buttons, stamps
// ---------------------------------------------------------------------------

describe("saving sends the stamp the edit is based on", () => {
  it("version goal: blur saves, keeping the read-only zones, labelled as the dashboard", async () => {
    const stamp = server.stamps.version_goal;
    type("version_goal", "CURRENT FOCUS\nmy plan");
    leave("version_goal");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    const post = server.posts[0];
    expect(post.path).toBe("/goal");
    expect(post.body.expected_updated_at).toBe(stamp);
    expect(post.body.source).toBe("dashboard");
    expect(post.body.content).toContain("v2.3 — auth sprint");
    expect(post.body.content).toContain("- login page");
    expect(post.body.content).toContain("CURRENT FOCUS\nmy plan");
    expect(post.body.content).toContain("--- AUTO BLOCKS BELOW ---\nauto block one");
    await settle();
    expect(document.getElementById("goal-version-p1")!.textContent).toBe(`v${server.version}`);
  });

  it("north star: sends the north star stamp, the owner id and the dashboard label", async () => {
    const stamp = server.stamps.north_star;
    type("north_star", "a changed north star");
    leave("north_star");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].path).toBe("/goal/north-star");
    expect(server.posts[0].body).toMatchObject({
      north_star: "a changed north star",
      human_id: "owner",
      expected_updated_at: stamp,
      source: "dashboard",
    });
  });

  it("current focus: sends the current focus stamp", async () => {
    const stamp = server.stamps.sprint;
    type("sprint", "a new focus");
    leave("sprint");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].path).toBe("/goal/sprint");
    expect(server.posts[0].body).toMatchObject({ sprint: "a new focus", expected_updated_at: stamp, source: "dashboard" });
  });

  it.each(["version_goal", "north_star", "sprint"] as Field[])(
    "%s: a second save is based on the stamp the FIRST response gave for that field, not another field's",
    async (key) => {
      const text = (n: number) => (key === "version_goal" ? `CURRENT FOCUS\nedit ${n}` : `edit ${n}`);
      type(key, text(1));
      leave(key);
      await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
      await settle();
      // A save re-pulls the goal, so the page shows the version the save produced.
      expect(document.getElementById("goal-version-p1")!.textContent).toBe(`v${server.version}`);
      const afterFirst = server.stamps[key];
      type(key, text(2));
      leave(key);
      await vi.waitFor(() => expect(server.posts).toHaveLength(2), { timeout: 3000 });
      expect(server.posts[1].body.expected_updated_at).toBe(afterFirst);
      expect(server.posts[1].status).toBe(200);
      await settle();
      expect(bar(key)).toBeNull();
    },
  );

  it.each(["version_goal", "north_star", "sprint"] as Field[])(
    "%s: a second save made before the re-pull lands is based on the stamp the save response gave",
    async (key) => {
      // On a slow connection the person can edit again before the refresh that follows a save
      // has answered; the only fresh stamp the page has then is the one in the save response.
      let release!: () => void;
      server.gate = new Promise<void>((res) => { release = res; });
      const text = (n: number) => (key === "version_goal" ? `CURRENT FOCUS
edit ${n}` : `edit ${n}`);
      try {
        type(key, text(1));
        leave(key);
        await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
        await settle();
        const fromResponse = server.stamps[key];
        type(key, text(2));
        leave(key);
        await vi.waitFor(() => expect(server.posts).toHaveLength(2), { timeout: 3000 });
        expect(server.posts[1].body.expected_updated_at).toBe(fromResponse);
        expect(server.posts[1].status).toBe(200);
      } finally {
        release();
      }
      await settle();
      expect(bar(key)).toBeNull();
    },
  );

  it.each(["version_goal", "north_star", "sprint"] as Field[])(
    "%s: after a remote change to ANOTHER field the next save is not a false conflict",
    async (key) => {
      const other: Field = key === "sprint" ? "north_star" : "sprint";
      await remote(other, "moved by an agent");
      type(key, key === "version_goal" ? "CURRENT FOCUS\nmine" : "mine");
      leave(key);
      await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
      expect(server.posts[0].status).toBe(200);
      expect(bar(key)).toBeNull();
    },
  );

  it.each(["version_goal", "north_star", "sprint"] as Field[])(
    "%s: after a remote change to the SAME field (not editing) the next save is based on the new stamp",
    async (key) => {
      await remote(key, key === "version_goal" ? content("CURRENT FOCUS\nagent plan") : "agent value");
      const stamp = server.stamps[key];
      type(key, key === "version_goal" ? "CURRENT FOCUS\nmine" : "mine");
      leave(key);
      await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
      expect(server.posts[0].body.expected_updated_at).toBe(stamp);
      expect(server.posts[0].status).toBe(200);
    },
  );

  it("the Save buttons are the same controllers: an explicit save posts once", async () => {
    type("version_goal", "CURRENT FOCUS\nvia button");
    (document.getElementById("save-goal-p1") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    type("sprint", "sprint via button");
    (document.getElementById("save-sprint-p1") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(server.posts).toHaveLength(2), { timeout: 3000 });
    // north star asks first; the confirm mock says yes
    type("north_star", "north star via button");
    (document.getElementById("save-north-star-p1") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(server.posts).toHaveLength(3), { timeout: 3000 });
    expect(server.posts.map((p) => p.path)).toEqual(["/goal", "/goal/sprint", "/goal/north-star"]);
    expect(server.posts.every((p) => p.status === 200)).toBe(true);
  });

  it("a JSON goal is edited as JSON and saved back as an object", async () => {
    server.goal = { title: "structured", items: [1, 2] };
    server.version += 1;
    server.stamps.version_goal = server.next();
    await refresh();
    expect(JSON.parse(el("version_goal").value)).toEqual({ title: "structured", items: [1, 2] });
    type("version_goal", JSON.stringify({ title: "structured", items: [1, 2, 3] }, null, 2));
    leave("version_goal");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].body.content).toEqual({ title: "structured", items: [1, 2, 3] });
  });
});

describe("the north star keeps its 'intended to be stable' confirmation", () => {
  it("asks before changing an existing north star, and declining reverts the field without saving", async () => {
    confirmMock.mockReturnValue(false);
    type("north_star", "a casual rewrite");
    leave("north_star");
    await settle();
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(String(confirmMock.mock.calls[0][0])).toContain("intended to be stable");
    expect(server.posts).toHaveLength(0);
    expect(el("north_star").value).toBe("be the memory layer");
  });

  it("saves when the person confirms", async () => {
    type("north_star", "a deliberate rewrite");
    leave("north_star");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(server.northStar).toBe("a deliberate rewrite");
  });

  it("does not ask for a first north star", async () => {
    server.northStar = "";
    server.stamps.north_star = server.next();
    await refresh();
    type("north_star", "the very first one");
    leave("north_star");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(confirmMock).not.toHaveBeenCalled();
  });

  it("Keep mine asks as well, and declining leaves the prompt and the draft alone", async () => {
    type("north_star", "my rewrite");
    await remote("north_star", "their rewrite");
    confirmMock.mockReturnValue(false);
    act("north_star", "keep");
    await settle();
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(server.posts).toHaveLength(0);
    expect(el("north_star").value).toBe("my rewrite");
    expect(bar("north_star")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 4. A stale save (409) and the read-only zones
// ---------------------------------------------------------------------------

describe("saving over a change the page has not seen yet (409)", () => {
  it.each(["version_goal", "north_star", "sprint"] as Field[])(
    "%s: nothing is saved, the draft stays and the bar offers the choice",
    async (key) => {
      // An agent wrote while the person was typing, and the push has not reached this page.
      server.write(key, key === "version_goal" ? content("CURRENT FOCUS\nagent plan") : "agent value");
      type(key, key === "version_goal" ? "CURRENT FOCUS\nmy plan" : "my value");
      leave(key);
      await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
      await settle();
      expect(server.posts[0].status).toBe(409);
      expect(el(key).value).toBe(key === "version_goal" ? "CURRENT FOCUS\nmy plan" : "my value");
      const b = bar(key);
      expect(b).not.toBeNull();
      expect(b!.textContent).toContain("Changed elsewhere");
      const stored = key === "version_goal" ? String(server.goal) : key === "north_star" ? server.northStar : server.sprint;
      expect(stored).toContain("agent");
      // Take theirs then shows what the 409 carried.
      act(key, "take");
      await settle();
      expect(el(key).value).toBe(key === "version_goal" ? "CURRENT FOCUS\nagent plan" : "agent value");
    },
  );

  it("a JSON goal's 409 shows the current object as JSON text", async () => {
    server.goal = { title: "structured", items: [1, 2] };
    server.version += 1;
    server.stamps.version_goal = server.next();
    await refresh();
    server.write("version_goal", { title: "structured", items: [1, 2, 3] });
    type("version_goal", JSON.stringify({ title: "mine", items: [] }, null, 2));
    leave("version_goal");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    await settle();
    expect(server.posts[0].status).toBe(409);
    expect(bar("version_goal")).not.toBeNull();
    act("version_goal", "take");
    await settle();
    expect(JSON.parse(el("version_goal").value)).toEqual({ title: "structured", items: [1, 2, 3] });
  });

  it("Keep mine after a 409 saves on the stamp the 409 carried", async () => {
    server.write("sprint", "agent value");
    const remoteStamp = server.stamps.sprint;
    type("sprint", "my value");
    leave("sprint");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    await settle();
    act("sprint", "keep");
    await vi.waitFor(() => expect(server.posts).toHaveLength(2), { timeout: 3000 });
    expect(server.posts[1].status).toBe(200);
    expect(server.posts[1].body.expected_updated_at).toBe(remoteStamp);
    expect(server.sprint).toBe("my value");
  });

  it("when only a read-only zone moved, the edit is saved again silently on the new stamp, on top of the NEW zones", async () => {
    // An agent appended an AUTO BLOCK and shipped something: the editable text did not change.
    server.write("version_goal", content("CURRENT FOCUS\nship the login flow", "SHIPPED\n- login page\n- billing", "auto block two"));
    type("version_goal", "CURRENT FOCUS\nship the login flow, carefully");
    leave("version_goal");
    await vi.waitFor(() => expect(server.posts).toHaveLength(2), { timeout: 3000 });
    await settle();
    expect(server.posts.map((p) => p.status)).toEqual([409, 200]);
    const resaved = String(server.posts[1].body.content);
    expect(resaved).toContain("CURRENT FOCUS\nship the login flow, carefully");
    // The re-save must not drop what the agent added to the read-only zones.
    expect(resaved).toContain("- billing");
    expect(resaved).toContain("--- AUTO BLOCKS BELOW ---\nauto block two");
    expect(resaved).not.toContain("auto block one");
    expect(document.getElementById("goal-shipped-p1")!.textContent).toContain("- billing");
    expect(document.getElementById("goal-autoblocks-p1")!.textContent).toBe("auto block two");
    expect(bar("version_goal")).toBeNull();
  });

  it("the sprint save never reads or is fed the north star's stamp", async () => {
    // The server answers every write with all three stamps and they differ, so mixing them
    // up is a 409 on the next save (and a bar) instead of a quiet pass.
    type("north_star", "ns edit");
    leave("north_star");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    await settle();
    type("sprint", "sprint edit");
    leave("sprint");
    await vi.waitFor(() => expect(server.posts).toHaveLength(2), { timeout: 3000 });
    expect(server.posts[1].status).toBe(200);
    await settle();
    const afterSprintSave = server.stamps.sprint;
    expect(afterSprintSave).not.toBe(server.stamps.north_star);
    type("sprint", "sprint edit two");
    leave("sprint");
    await vi.waitFor(() => expect(server.posts).toHaveLength(3), { timeout: 3000 });
    expect(server.posts[2].status).toBe(200);
    expect(server.posts[2].body.expected_updated_at).toBe(afterSprintSave);
  });
});

// ---------------------------------------------------------------------------
// 5. A failed save keeps the text and says so on the field
// ---------------------------------------------------------------------------

describe("a failed save", () => {
  it.each(["version_goal", "north_star", "sprint"] as Field[])("%s: a server error keeps the text and shows it on the field", async (key) => {
    server.failNextPost = { status: 500, text: JSON.stringify({ detail: "database is busy" }) };
    const mine = key === "version_goal" ? "CURRENT FOCUS\nmy plan" : "my value";
    type(key, mine);
    leave(key);
    await vi.waitFor(() => expect(bar(key)).not.toBeNull(), { timeout: 3000 });
    expect(el(key).value).toBe(mine);
    expect(bar(key)!.textContent).toContain("Save failed");
    expect(bar(key)!.textContent).toContain("database is busy");
    expect(el(key).classList.contains("save-failed")).toBe(true);
    expect(document.getElementById("toast")!.textContent).toBe("save failed: database is busy");
    // Retry from the bar saves it.
    act(key, "retry");
    await vi.waitFor(() => expect(server.posts.filter((p) => p.status === 200)).toHaveLength(1), { timeout: 3000 });
    await settle();
    expect(bar(key)).toBeNull();
  });

  it("a dropped connection is handled the same way", async () => {
    server.networkDownOnPost = true;
    type("sprint", "offline edit");
    leave("sprint");
    await vi.waitFor(() => expect(bar("sprint")).not.toBeNull(), { timeout: 3000 });
    expect(el("sprint").value).toBe("offline edit");
    expect(bar("sprint")!.textContent).toContain("Save failed");
  });
});

// ---------------------------------------------------------------------------
// 6. Esc
// ---------------------------------------------------------------------------

describe("Esc inside a field", () => {
  it.each(["version_goal", "north_star", "sprint"] as Field[])("%s: offers Discard and reverts to the saved value when confirmed", (key) => {
    const saved = el(key).value;
    type(key, key === "version_goal" ? "CURRENT FOCUS\nscribble" : "scribble");
    const ev = esc(key);
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(String(confirmMock.mock.calls[0][0])).toContain("Discard your unsaved changes");
    expect(ev.defaultPrevented).toBe(true);
    expect(el(key).value).toBe(saved);
    expect(el(key).classList.contains("dirty")).toBe(false);
    expect(server.posts).toHaveLength(0);
  });

  it("keeps the draft when the person declines", () => {
    confirmMock.mockReturnValue(false);
    type("north_star", "scribble");
    esc("north_star");
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(el("north_star").value).toBe("scribble");
  });

  it.each(["version_goal", "north_star", "sprint"] as Field[])("%s: never prompts when the text did not actually change", (key) => {
    const saved = el(key).value;
    type(key, "something else");
    type(key, saved);
    const ev = esc(key);
    expect(confirmMock).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 7. Leaving: vtab switch, closing the project tab, closing the page
// ---------------------------------------------------------------------------

const vtabBtn = (v: string) => document.querySelector(`#vtab-strip-p1 .vtab-btn[data-vtab="${v}"]`) as HTMLButtonElement;
const activeVtab = () => w.state.panels[PROJECT.id].activeVtab;

/** Open the Goal vtab and leave an unsaved edit behind (a save that failed). */
async function unsavedGoalEdit(): Promise<void> {
  vtabBtn("goal").click();
  expect(activeVtab()).toBe("goal");
  server.failNextPost = { status: 500, text: "boom" };
  type("sprint", "unsaved focus");
  leave("sprint");
  await vi.waitFor(() => expect(bar("sprint")).not.toBeNull(), { timeout: 3000 });
}

describe("leaving the Goal tab with unsaved edits", () => {
  it("a clean tab switch goes straight through", () => {
    vtabBtn("goal").click();
    vtabBtn("status").click();
    expect(dialog()).toBeNull();
    expect(activeVtab()).toBe("status");
  });

  it("asks Save / Discard / Stay, and Stay keeps the Goal tab and the text", async () => {
    await unsavedGoalEdit();
    vtabBtn("status").click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("stay");
    await settle();
    expect(activeVtab()).toBe("goal");
    expect(el("sprint").value).toBe("unsaved focus");
  });

  it("Discard reverts the field and then switches", async () => {
    await unsavedGoalEdit();
    vtabBtn("status").click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("discard");
    await vi.waitFor(() => expect(activeVtab()).toBe("status"), { timeout: 3000 });
    expect(el("sprint").value).toBe("fix login");
    expect(dialog()).toBeNull();
  });

  it("Save saves and then switches", async () => {
    await unsavedGoalEdit();
    vtabBtn("status").click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("save");
    await vi.waitFor(() => expect(activeVtab()).toBe("status"), { timeout: 3000 });
    expect(server.sprint).toBe("unsaved focus");
  });

  it("only the Goal tab is guarded: switching away from another tab never prompts", async () => {
    await unsavedGoalEdit();
    vtabBtn("live").click(); // leaves the goal vtab: prompt (cancelled below)
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("stay");
    await settle();
    expect(activeVtab()).toBe("goal");
  });
});

describe("closing the project tab with unsaved edits", () => {
  const closeBtn = () => document.querySelector('.tab[data-tab-id="p1"] .close') as HTMLButtonElement;

  it("asks first; Stay keeps the tab open", async () => {
    await unsavedGoalEdit();
    closeBtn().click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("stay");
    await settle();
    expect(w.state.tabs.some((t: any) => t.id === "p1")).toBe(true);
  });

  it("Discard closes it", async () => {
    await unsavedGoalEdit();
    closeBtn().click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("discard");
    await vi.waitFor(() => expect(w.state.tabs.some((t: any) => t.id === "p1")).toBe(false), { timeout: 3000 });
  });

  it("a clean tab closes at once", () => {
    closeBtn().click();
    expect(dialog()).toBeNull();
    expect(w.state.tabs.some((t: any) => t.id === "p1")).toBe(false);
  });
});

describe("closing the page", () => {
  const fire = () => {
    const ev = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev);
    return ev;
  };

  it("is warned about only while something is unsaved", async () => {
    expect(fire().defaultPrevented).toBe(false);
    server.failNextPost = { status: 500, text: "boom" };
    type("north_star", "unsaved north star");
    leave("north_star");
    await vi.waitFor(() => expect(bar("north_star")).not.toBeNull(), { timeout: 3000 });
    expect(fire().defaultPrevented).toBe(true);
    esc("north_star");
    expect(fire().defaultPrevented).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 8. The current-focus select
// ---------------------------------------------------------------------------

describe("the current-focus session picker", () => {
  it("a session picked in the select is an edit that Save sends, with the focus stamp", async () => {
    server.sessions = [{ name: "s-alpha", status: "active" }];
    await openPanel();
    const sel = document.getElementById("goal-sprint-select-p1") as HTMLSelectElement;
    expect(Array.from(sel.options).map((o) => o.value)).toContain("s-alpha");
    const stamp = server.stamps.sprint;
    sel.value = "s-alpha";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    expect(el("sprint").value).toBe("s-alpha");
    (document.getElementById("save-sprint-p1") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].body).toMatchObject({ sprint: "s-alpha", expected_updated_at: stamp });
  });

  it("picking a session is an edit: it is kept when an agent changes the focus meanwhile", async () => {
    server.sessions = [{ name: "s-alpha", status: "active" }, { name: "s-beta", status: "active" }];
    await openPanel();
    const sel = document.getElementById("goal-sprint-select-p1") as HTMLSelectElement;
    sel.value = "s-alpha";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    expect(el("sprint").classList.contains("dirty")).toBe(true);
    await remote("sprint", "s-beta");
    expect(sel.value).toBe("s-alpha");
    expect(el("sprint").value).toBe("s-alpha");
    expect(bar("sprint")).not.toBeNull();
  });

  it("picking a session while a remote change waits does not let the remote change replace the pick", async () => {
    server.sessions = [{ name: "s-alpha", status: "active" }, { name: "s-beta", status: "active" }];
    await openPanel();
    const sel = document.getElementById("goal-sprint-select-p1") as HTMLSelectElement;
    type("sprint", "my own focus");
    await remote("sprint", "their focus");
    expect(bar("sprint")).not.toBeNull();
    sel.value = "s-alpha";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    expect(el("sprint").value).toBe("s-alpha");
    expect(sel.value).toBe("s-alpha");
    expect(bar("sprint")).not.toBeNull(); // still a conflict the person has to resolve
    act("sprint", "keep");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].body.sprint).toBe("s-alpha");
  });

  it("a live update of the focus moves the picker with it", async () => {
    server.sessions = [{ name: "s-alpha", status: "active" }];
    await openPanel();
    await remote("sprint", "s-alpha");
    const sel = document.getElementById("goal-sprint-select-p1") as HTMLSelectElement;
    expect(sel.value).toBe("s-alpha");
    expect(el("sprint").value).toBe("s-alpha");
  });
});

// ---------------------------------------------------------------------------
// 9. Smaller guarantees of the same glue
// ---------------------------------------------------------------------------

describe("what each field may save", () => {
  it("the version goal may be emptied and saved", async () => {
    type("version_goal", "");
    leave("version_goal");
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].status).toBe(200);
    expect(String(server.posts[0].body.content)).toContain("v2.3 — auth sprint");
  });

  it.each(["north_star", "sprint"] as Field[])("%s: an emptied field is not saved (it is a mistake, not an edit)", async (key) => {
    type(key, "   ");
    leave(key);
    await settle();
    expect(server.posts).toHaveLength(0);
    expect(confirmMock).not.toHaveBeenCalled();
  });

  it.each([
    ["version_goal", "save-goal-p1", "CURRENT FOCUS\nset by a script"],
    ["north_star", "save-north-star-p1", "set by a script"],
    ["sprint", "save-sprint-p1", "set by a script"],
  ] as Array<[Field, string, string]>)("%s: the Save button sends a value that changed without an input event", async (key, btn, text) => {
    el(key).value = text; // e.g. a picker or a paste handler: no input event
    (document.getElementById(btn) as HTMLButtonElement).click();
    await vi.waitFor(() => expect(server.posts).toHaveLength(1), { timeout: 3000 });
    expect(server.posts[0].status).toBe(200);
  });

  it("the Save button with nothing changed says so instead of posting", async () => {
    (document.getElementById("save-north-star-p1") as HTMLButtonElement).click();
    await settle();
    expect(server.posts).toHaveLength(0);
    expect(document.getElementById("toast")!.textContent).toBe("No changes to save");
  });
});

describe("the read-only zones of the version goal", () => {
  it("are hidden when the goal has no title, SHIPPED block or AUTO BLOCKS", async () => {
    await remote("version_goal", "CURRENT FOCUS\nonly the editable part");
    expect(el("version_goal").value).toBe("CURRENT FOCUS\nonly the editable part");
    expect(document.getElementById("goal-title-p1")!.style.display).toBe("none");
    expect(document.getElementById("goal-shipped-p1")!.style.display).toBe("none");
    expect(document.getElementById("goal-autoblocks-p1")!.style.display).toBe("none");
    expect(document.getElementById("goal-autoblocks-wrapper-p1")!.style.display).toBe("none");
    expect(el("version_goal").style.borderRadius).toBe("4px");
  });

  it("are shown when it has all three", async () => {
    await remote("version_goal", "CURRENT FOCUS\nbare");
    await remote("version_goal", content("CURRENT FOCUS\nfull"));
    expect(document.getElementById("goal-title-p1")!.style.display).toBe("block");
    expect(document.getElementById("goal-shipped-p1")!.style.display).toBe("block");
    expect(document.getElementById("goal-autoblocks-p1")!.style.display).toBe("block");
    expect(document.getElementById("goal-autoblocks-wrapper-p1")!.style.display).toBe("block");
    expect(el("version_goal").style.borderRadius).toBe("0 0 4px 4px");
  });

  it("follow the server even while the editable text is protected", async () => {
    type("version_goal", "CURRENT FOCUS\nmy plan");
    await remote("version_goal", content("CURRENT FOCUS\ntheir plan", "SHIPPED\n- billing", "auto block two", "v2.4 — billing"));
    expect(el("version_goal").value).toBe("CURRENT FOCUS\nmy plan");
    expect(document.getElementById("goal-title-p1")!.textContent).toBe("v2.4 — billing");
    expect(document.getElementById("goal-shipped-p1")!.textContent).toBe("SHIPPED\n- billing");
    expect(document.getElementById("goal-autoblocks-p1")!.textContent).toBe("auto block two");
  });
});

describe("other events that re-pull the goal are protected too", () => {
  const task = { id: "t1", description: "answered a HITL", status: "done", session_id: "s1", created_at: "2026-10-07 10:00:00" };

  it("a task event updates an untouched field", async () => {
    server.write("sprint", "moved by a HITL reply");
    const before = server.gets;
    w.handleWsEvent(PROJECT.id, { type: "task_created", task });
    await vi.waitFor(() => expect(server.gets).toBeGreaterThan(before), { timeout: 3000 });
    await settle();
    expect(el("sprint").value).toBe("moved by a HITL reply");
  });

  it("a task event never overwrites a field being edited", async () => {
    type("north_star", "my draft");
    server.write("north_star", "someone else's");
    const before = server.gets;
    w.handleWsEvent(PROJECT.id, { type: "task_updated", task });
    await vi.waitFor(() => expect(server.gets).toBeGreaterThan(before), { timeout: 3000 });
    await settle();
    expect(el("north_star").value).toBe("my draft");
    expect(bar("north_star")).not.toBeNull();
  });
});

describe("the vtab guard resets itself", () => {
  it("prompts again the next time, not only the first time", async () => {
    await unsavedGoalEdit();
    vtabBtn("status").click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("discard");
    await vi.waitFor(() => expect(activeVtab()).toBe("status"), { timeout: 3000 });
    // Back to the Goal tab with a new unsaved edit; leaving through the SAME button must ask again.
    await unsavedGoalEdit();
    vtabBtn("status").click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull(), { timeout: 3000 });
    choose("stay");
    await settle();
    expect(activeVtab()).toBe("goal");
  });

  it("only leaving the Goal tab is guarded", async () => {
    await unsavedGoalEdit();
    // The unsaved text is still there, but the person is not on the Goal tab (e.g. restored
    // there some other way): moving between other tabs must not ask about goal text.
    w.state.panels[PROJECT.id].activeVtab = "live";
    vtabBtn("status").click();
    await settle();
    expect(dialog()).toBeNull();
    expect(activeVtab()).toBe("status");
  });

  it("clicking the Goal tab while on it never prompts", async () => {
    await unsavedGoalEdit();
    vtabBtn("goal").click();
    await settle();
    expect(dialog()).toBeNull();
    expect(activeVtab()).toBe("goal");
  });
});
