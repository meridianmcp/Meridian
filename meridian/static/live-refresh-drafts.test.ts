// 8a665a03 -- a repaint must never throw away what the user typed.
//
// Owner design intent, found as a bug class in the live-refresh lane: a list view that is
// rebuilt from a fresh fetch (innerHTML = ...) replaces every node in it, including the one
// the user is typing into. The Goal tab was fixed first (live-refresh-goal-edits.test.ts);
// these tests pin the same rule on every other surface that repaints on its own:
//
//   * the Live tab's sprint board -- the add-item box, and an open inline editor (title /
//     version, notes, touches_resources) -- repainted ~10 s after any sprint_item_added /
//     sprint_item_updated event, on every reconnect and after every mutation;
//   * the HITL drawer (input#hitl-ans-<id> in the per-project tab) -- repainted by every
//     hitl_filed event and by Refresh;
//   * the global HITL panel (input.hitl-answer-input) -- repainted by every hitl_filed event;
//   * the Status tab's pending-HITL reply boxes (input[data-input]) -- repainted by every
//     task event.
//
// The code under test is the REAL shipped code: renderSprintProgress and the helper are
// imported; the dashboard.ts functions are lifted out of the source by source-harness.ts.
// File name has no `dashboard-` prefix on purpose: tests/dashboard_src.py concatenates every
// dashboard-*.ts into the text the source-scanning pytest checks.
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { loadDashboardFunctions } from "./source-harness";
import { paintKeepingDrafts, hasUnsentDrafts, fieldHoldsDraft, escapeHtml, type DraftUnit } from "./dashboard-utils";
import { renderSprintProgress } from "./dashboard-sprint";

const DASH = "meridian/static/dashboard.ts";
const PID = "proj-drafts-1";

type Fns = Record<string, (...a: any[]) => any>;
const load = (names: string[], scope: Record<string, unknown>) => loadDashboardFunctions(DASH, names, scope) as Fns;
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

/** What a user does: click into a field, type, leave the caret somewhere. */
function typeInto(el: HTMLInputElement | HTMLTextAreaElement, text: string, caret?: [number, number]) {
  el.focus();
  el.value = text;
  const [a, b] = caret ?? [text.length, text.length];
  el.setSelectionRange(a, b);
}
const caretOf = (el: HTMLInputElement | HTMLTextAreaElement) => [el.selectionStart, el.selectionEnd];

beforeAll(() => {
  // jsdom has no CSS.escape; sprintItemEdit and its siblings locate their row with it.
  if (!(globalThis as any).CSS?.escape) {
    vi.stubGlobal("CSS", { escape: (s: string) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c) });
  }
});

// ---------------------------------------------------------------------------
// The shared helper
// ---------------------------------------------------------------------------
describe("paintKeepingDrafts", () => {
  const units: DraftUnit[] = [{ selector: "input.box", key: (el) => el.id }];
  const root = () => document.getElementById("root") as HTMLElement;
  const box = (id: string) => document.getElementById(id) as HTMLInputElement;
  const html = (...ids: string[]) =>
    ids.map((id) => `<div class="card" data-v="new"><input class="box" id="${id}" placeholder="fresh"></div>`).join("");

  beforeEach(() => {
    document.body.innerHTML = `<div id="root"></div><input id="elsewhere">`;
    root().innerHTML = html("a", "b");
  });

  it("carries a typed input across the repaint: same node, same text, same focus and caret", () => {
    const a = box("a");
    typeInto(a, "half a sentence", [2, 6]);
    const carried = paintKeepingDrafts(root(), html("a", "b", "c"), units);
    expect(carried).toBe(1);
    expect(box("a")).toBe(a);
    expect(a.value).toBe("half a sentence");
    expect(document.activeElement).toBe(a);
    expect(caretOf(a)).toEqual([2, 6]);
    expect(root().querySelectorAll(".card")).toHaveLength(3); // the rest of the list did repaint
  });

  it("an untouched, unfocused input is repainted like everything else", () => {
    const b = box("b");
    expect(paintKeepingDrafts(root(), html("a", "b"), units)).toBe(0);
    expect(box("b")).not.toBe(b);
    expect(box("b").value).toBe("");
  });

  it("a focused but still empty input is kept (the user has clicked in)", () => {
    const a = box("a");
    a.focus();
    paintKeepingDrafts(root(), html("a", "b"), units);
    expect(box("a")).toBe(a);
    expect(document.activeElement).toBe(a);
  });

  it("text typed into an unfocused field is kept, and the repaint does not steal the focus", () => {
    const a = box("a");
    a.value = "typed, then clicked away";
    (document.getElementById("elsewhere") as HTMLInputElement).focus();
    paintKeepingDrafts(root(), html("a", "b"), units);
    expect(box("a")).toBe(a);
    expect(a.value).toBe("typed, then clicked away");
    expect(document.activeElement).toBe(document.getElementById("elsewhere"));
  });

  it("drops a draft whose repainted twin is gone, without throwing, and does not leave a stray node", () => {
    const a = box("a");
    typeInto(a, "reply to a request that was answered elsewhere");
    expect(() => paintKeepingDrafts(root(), html("b"), units)).not.toThrow();
    expect(root().contains(a)).toBe(false);
    expect(document.getElementById("a")).toBeNull();
    expect(document.activeElement).not.toBe(a);
  });

  it("keeps several drafts at once, each in its own card", () => {
    typeInto(box("b"), "second");
    box("a").value = "first";
    paintKeepingDrafts(root(), html("b", "a"), units); // the cards were re-ordered
    expect(box("a").value).toBe("first");
    expect(box("b").value).toBe("second");
    expect(Array.from(root().querySelectorAll(".box")).map((e) => e.id)).toEqual(["b", "a"]);
  });

  it("matches the Nth node of a repeated key to the Nth twin (a row drawn twice)", () => {
    const twice: DraftUnit[] = [{ selector: ".row", key: (el) => el.dataset.k, keep: (el) => el.classList.contains("open") }];
    root().innerHTML = `<div class="row" data-k="x">1</div><div class="row open" data-k="x">2</div>`;
    const second = root().querySelectorAll(".row")[1] as HTMLElement;
    paintKeepingDrafts(root(), `<div class="row" data-k="x">new 1</div><div class="row" data-k="x">new 2</div>`, twice);
    const rows = Array.from(root().querySelectorAll(".row")) as HTMLElement[];
    expect(rows[0]!.textContent).toBe("new 1");
    expect(rows[1]).toBe(second);
  });

  it("a unit with no key is never kept", () => {
    const noKey: DraftUnit[] = [{ selector: "input.box", key: () => "" }];
    typeInto(box("a"), "x");
    expect(paintKeepingDrafts(root(), html("a"), noKey)).toBe(0);
  });

  it("hasUnsentDrafts / fieldHoldsDraft report what the repaint would keep", () => {
    expect(hasUnsentDrafts(root(), units)).toBe(false);
    expect(hasUnsentDrafts(null, units)).toBe(false);
    box("a").value = "x";
    expect(hasUnsentDrafts(root(), units)).toBe(true);
    expect(fieldHoldsDraft(box("a"))).toBe(true);
    expect(fieldHoldsDraft(box("b"))).toBe(false);
    box("a").value = "";
    box("b").focus();
    expect(fieldHoldsDraft(box("b"))).toBe(true);
    // a wrapper counts when a field inside it holds a draft; a checkbox is not a text field
    root().insertAdjacentHTML("beforeend", `<div id="w"><input type="checkbox" checked></div>`);
    expect(fieldHoldsDraft(document.getElementById("w")!)).toBe(false);
    expect(fieldHoldsDraft(box("b").parentElement!)).toBe(true);
  });

  it("works for a textarea too", () => {
    root().innerHTML = `<div class="card"><textarea class="box" id="t"></textarea></div>`;
    const t = document.getElementById("t") as HTMLTextAreaElement;
    typeInto(t, "line one\nline two", [4, 4]);
    paintKeepingDrafts(root(), `<div class="card"><textarea class="box" id="t"></textarea></div><p>more</p>`, [
      { selector: "textarea.box", key: (el) => el.id },
    ]);
    expect(document.getElementById("t")).toBe(t);
    expect(t.value).toBe("line one\nline two");
    expect(document.activeElement).toBe(t);
    expect(caretOf(t)).toEqual([4, 4]);
  });
});

// ---------------------------------------------------------------------------
// Live tab: the sprint board
// ---------------------------------------------------------------------------
describe("Live tab sprint board: a repaint keeps the add-item box and any open inline editor", () => {
  const item = (id: string, title: string, over: Record<string, any> = {}) => ({
    id, title, version: "v1", status: "pending", ...over,
  });
  const board = () => [item("it-1", "first item"), item("it-2", "second item"), item("it-3", "third item", { status: "done" })];
  /** What the next refresh brings: an agent renamed one item and added another. */
  const agentChanged = () => [
    item("it-1", "first item"),
    item("it-2", "second item (renamed by an agent)"),
    item("it-3", "third item", { status: "done" }),
    item("it-4", "fourth item"),
  ];

  const root = () => document.getElementById(`live-sprint-progress-${PID}`) as HTMLElement;
  const addBox = () => document.getElementById(`sprint-add-input-${PID}`) as HTMLInputElement;
  const row = (id: string) => document.querySelector(`.sprint-item-row[data-item="${id}"]`) as HTMLElement | null;
  const editInputs = (id: string) => Array.from(row(id)!.querySelectorAll(".sprint-edit-input")) as HTMLInputElement[];
  const paint = (items: any[]) => renderSprintProgress(PID, items);

  let api: ReturnType<typeof vi.fn>;
  let refreshSprintSurfaces: ReturnType<typeof vi.fn>;
  let addItem: ReturnType<typeof vi.fn>;
  let fns: Fns;

  beforeEach(() => {
    document.body.innerHTML = `<div id="live-sprint-progress-${PID}"></div><input id="elsewhere">`;
    api = vi.fn(async () => ({}));
    refreshSprintSurfaces = vi.fn(async () => {});
    addItem = vi.fn();
    fns = load(["wireSprintAddEnter", "sprintItemEdit", "sprintItemNotesEdit", "sprintItemResourcesEdit"], {
      api, toast: vi.fn(), refreshSprintSurfaces, addSprintItemFromInput: addItem,
    });
    // renderSprintProgress calls these two as page globals.
    (window as any).wireSprintAddEnter = fns.wireSprintAddEnter;
    (window as any).addSprintItemFromInput = addItem;
  });

  describe("the add-item box", () => {
    it("keeps its text, its caret and its focus when an agent's change repaints the board", () => {
      paint(board());
      const box = addBox();
      typeInto(box, "v2:half a tit", [3, 7]);
      paint(agentChanged());
      expect(addBox()).toBe(box);
      expect(box.value).toBe("v2:half a tit");
      expect(document.activeElement).toBe(box);
      expect(caretOf(box)).toEqual([3, 7]);
      // ...and the board itself did repaint
      expect(root().textContent).toContain("(renamed by an agent)");
      expect(root().textContent).toContain("fourth item");
    });

    it("survives repeated repaints (a reconnect, then a mutation, then another event)", () => {
      paint(board());
      typeInto(addBox(), "v2:still typing");
      for (let i = 0; i < 5; i++) paint(i % 2 ? agentChanged() : board());
      expect(addBox().value).toBe("v2:still typing");
      expect(document.activeElement).toBe(addBox());
    });

    it("Enter and the + Add button still submit what was typed after the repaint", () => {
      paint(board());
      typeInto(addBox(), "v2:a new item");
      paint(agentChanged());
      addBox().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
      expect(addItem).toHaveBeenCalledWith(PID);
      (root().querySelector(".sprint-add-btn") as HTMLElement).click();
      expect(addItem).toHaveBeenCalledTimes(2);
    });

    it("does not steal the focus when the user has moved on to another field", () => {
      paint(board());
      addBox().value = "v2:typed, then clicked away";
      (document.getElementById("elsewhere") as HTMLInputElement).focus();
      paint(agentChanged());
      expect(addBox().value).toBe("v2:typed, then clicked away");
      expect(document.activeElement).toBe(document.getElementById("elsewhere"));
    });

    it("an untouched box is repainted like the rest of the board (nothing is frozen)", () => {
      paint(board());
      const before = addBox();
      paint([]);
      expect(addBox()).not.toBe(before);
      expect(addBox().placeholder).toContain("e.g. v1.0:My item");
    });

    it.each([
      ["an empty board", [] as any[]],
      ["a finished sprint", [item("d1", "done one", { status: "done" })]],
    ])("is kept when the repaint is the '%s' variant of the board", (_name, variant) => {
      paint(board());
      typeInto(addBox(), "v2:typed on the busy board");
      paint(variant);
      expect(addBox().value).toBe("v2:typed on the busy board");
      expect(document.activeElement).toBe(addBox());
      // and back again
      paint(board());
      expect(addBox().value).toBe("v2:typed on the busy board");
    });
  });

  describe("the inline title / version editor", () => {
    it("stays open with its text, caret and focus when the board repaints, and Enter still saves it", async () => {
      paint(board());
      await fns.sprintItemEdit(PID, "it-1");
      const [title, version] = editInputs("it-1");
      expect(title).toBeTruthy();
      title!.value = "my better title";
      title!.setSelectionRange(3, 9);

      paint(agentChanged());

      expect(editInputs("it-1")).toEqual([title, version]); // the very same nodes
      expect(title!.value).toBe("my better title");
      expect(document.activeElement).toBe(title);
      expect(caretOf(title!)).toEqual([3, 9]);
      expect(root().textContent).toContain("(renamed by an agent)"); // the other rows repainted

      title!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
      await flush();
      expect(api).toHaveBeenCalledWith(
        `/projects/${PID}/sprint-items/it-1`,
        expect.objectContaining({ method: "PATCH", body: JSON.stringify({ title: "my better title", version: "v1" }) }),
      );
      expect(refreshSprintSurfaces).toHaveBeenCalledWith(PID);
    });

    it("a saved edit does not outlive its save: the repaint after it shows the saved row, not the editor", async () => {
      paint(board());
      await fns.sprintItemEdit(PID, "it-1");
      editInputs("it-1")[0]!.value = "my better title";
      editInputs("it-1")[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
      await flush();
      // refreshSprintSurfaces (mocked) is what repaints in the shipped flow:
      paint([item("it-1", "my better title"), ...board().slice(1)]);
      expect(editInputs("it-1")).toHaveLength(0);
      expect(row("it-1")!.textContent).toContain("my better title");
    });

    it("Escape closes the editor, restores the row and catches it up with what changed meanwhile", async () => {
      paint(board());
      await fns.sprintItemEdit(PID, "it-1");
      editInputs("it-1")[0]!.value = "abandoned edit";
      paint(agentChanged());
      editInputs("it-1")[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      expect(editInputs("it-1")).toHaveLength(0);
      expect(row("it-1")!.querySelector(".sprint-item-title")!.textContent).toContain("first item");
      expect(refreshSprintSurfaces).toHaveBeenCalledWith(PID);
      paint(agentChanged()); // the catch-up repaint
      expect(row("it-1")!.querySelectorAll(".sprint-edit-input")).toHaveLength(0);
    });

    it("an editor that was opened but not yet typed into is kept too", async () => {
      paint(board());
      await fns.sprintItemEdit(PID, "it-2");
      const [title] = editInputs("it-2");
      paint(agentChanged());
      expect(editInputs("it-2")[0]).toBe(title);
    });

    it("an editor on a row that left the board is dropped without throwing (nothing to attach it to)", async () => {
      paint(board());
      await fns.sprintItemEdit(PID, "it-1");
      expect(() => paint([item("it-2", "second item"), item("it-3", "third item", { status: "done" })])).not.toThrow();
      expect(row("it-1")).toBeNull();
      expect(row("it-2")).not.toBeNull();
    });

    it("control: with no editor open, a repaint rebuilds the rows", () => {
      paint(board());
      const before = row("it-2");
      paint(agentChanged());
      expect(row("it-2")).not.toBe(before);
    });
  });

  describe("the notes and touches_resources editors", () => {
    it("an open notes editor keeps its text and focus across a repaint", async () => {
      paint(board());
      await fns.sprintItemNotesEdit(PID, "it-1");
      const ta = row("it-1")!.querySelector(".sprint-notes-textarea") as HTMLTextAreaElement;
      typeInto(ta, "context I am still writing", [5, 10]);
      paint(agentChanged());
      expect(row("it-1")!.querySelector(".sprint-notes-textarea")).toBe(ta);
      expect(ta.value).toBe("context I am still writing");
      expect(document.activeElement).toBe(ta);
      expect(caretOf(ta)).toEqual([5, 10]);
    });

    it("a saved notes edit is not carried across the repaint that follows it", async () => {
      paint(board());
      await fns.sprintItemNotesEdit(PID, "it-1");
      const ta = row("it-1")!.querySelector(".sprint-notes-textarea") as HTMLTextAreaElement;
      ta.value = "saved notes";
      ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true }));
      await flush();
      expect(api).toHaveBeenCalledWith(
        `/projects/${PID}/sprint-items/it-1`,
        expect.objectContaining({ body: JSON.stringify({ notes: "saved notes" }) }),
      );
      paint([item("it-1", "first item", { notes: "saved notes" }), ...board().slice(1)]);
      expect(row("it-1")!.querySelector(".sprint-notes-textarea")).toBeNull();
      expect(row("it-1")!.textContent).toContain("saved notes");
    });

    it("a saved touches_resources edit is not carried across the repaint that follows it", async () => {
      paint(board());
      await fns.sprintItemResourcesEdit(PID, "it-1", null);
      const ta = row("it-1")!.querySelector(".sprint-resources-textarea") as HTMLTextAreaElement;
      ta.value = "file:meridian/server.py";
      ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true }));
      await flush();
      expect(api).toHaveBeenCalledWith(
        `/projects/${PID}/sprint-items/it-1`,
        expect.objectContaining({ body: JSON.stringify({ touches_resources: ["file:meridian/server.py"] }) }),
      );
      paint([item("it-1", "first item", { touches_resources: JSON.stringify(["file:meridian/server.py"]) }), ...board().slice(1)]);
      expect(row("it-1")!.querySelector(".sprint-resources-textarea")).toBeNull();
      expect(row("it-1")!.textContent).toContain("file:meridian/server.py");
    });

    it("an open touches_resources editor keeps its text and focus across a repaint", async () => {
      paint(board());
      await fns.sprintItemResourcesEdit(PID, "it-1", null);
      const ta = row("it-1")!.querySelector(".sprint-resources-textarea") as HTMLTextAreaElement;
      typeInto(ta, "file:meridian/server.py\nnote:");
      paint(agentChanged());
      expect(row("it-1")!.querySelector(".sprint-resources-textarea")).toBe(ta);
      expect(ta.value).toBe("file:meridian/server.py\nnote:");
      expect(document.activeElement).toBe(ta);
    });
  });
});

// ---------------------------------------------------------------------------
// The path the owner hit: a sprint event, ~10 s later, the Live refresh
// ---------------------------------------------------------------------------
describe("the throttled Live refresh after a sprint_item_added / sprint_item_updated event", () => {
  const item = (id: string, title: string, over: Record<string, any> = {}) => ({
    id, title, version: "v1", status: "pending", ...over,
  });
  let serverItems: any[];
  let projectApi: ReturnType<typeof vi.fn>;
  let fns: Fns;

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = `<div id="live-sprint-progress-${PID}"></div>`;
    serverItems = [item("it-1", "first item"), item("it-2", "second item")];
    projectApi = vi.fn(async (_pid: string, path: string) => (path.includes("/sprint-items") ? serverItems.slice() : []));
    const state = { panels: { [PID]: { activeVtab: "live", taskCache: [] } }, tabs: [], projects: [] };
    const addItem = vi.fn();
    const stubs = load(["wireSprintAddEnter"], { addSprintItemFromInput: addItem });
    (window as any).wireSprintAddEnter = stubs.wireSprintAddEnter;
    (window as any).addSprintItemFromInput = addItem;
    fns = load(["handleWsEvent", "scheduleLiveRefresh", "refreshLiveTab", "sprintItemEdit"], {
      state,
      api: vi.fn(async () => ({})),
      toast: vi.fn(),
      refreshSprintSurfaces: vi.fn(async () => {}),
      liveRefreshState: { [PID]: { enabled: true } },
      LIVE_REFRESH_MS: 30000,
      LIVE_THROTTLE_MS: 10000,
      projectApi,
      renderSprintProgress, // the real board renderer
      renderWaveProgress: vi.fn(),
      renderInProgressBySession: vi.fn(),
      renderLiveSessions: vi.fn(),
      cacheMostRecentSession: vi.fn(),
      loadSprintNotesPanel: vi.fn(async () => {}),
      renderLiveQueue: vi.fn(),
      renderProjectLoadError: vi.fn(() => ""),
      wireProjectLoadRetry: vi.fn(),
      repaintVisibleSprintViews: vi.fn(),
      refreshProjectCountBadges: vi.fn(),
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const addBox = () => document.getElementById(`sprint-add-input-${PID}`) as HTMLInputElement;

  it.each(["sprint_item_updated", "sprint_item_added"])(
    "%s: the repaint ~10 s later keeps a half-typed new item and an open title editor",
    async (type) => {
      renderSprintProgress(PID, serverItems);
      typeInto(addBox(), "v2:a half-typed new item", [4, 9]);
      await fns.sprintItemEdit(PID, "it-1");
      const titleInput = document.querySelector(`.sprint-item-row[data-item="it-1"] .sprint-edit-input`) as HTMLInputElement;
      titleInput.value = "a title I am rewriting";

      serverItems.push(item("it-3", "added by an agent"));
      fns.handleWsEvent(PID, { type, project_id: PID });

      await vi.advanceTimersByTimeAsync(9_999);
      expect(projectApi).not.toHaveBeenCalled(); // the 10 s floor: nothing has been fetched yet
      await vi.advanceTimersByTimeAsync(2);
      expect(projectApi).toHaveBeenCalledWith(PID, `/projects/${PID}/sprint-items`);
      expect(document.getElementById(`live-sprint-progress-${PID}`)!.textContent).toContain("added by an agent");

      expect(addBox().value).toBe("v2:a half-typed new item");
      expect(caretOf(addBox())).toEqual([4, 9]);
      const kept = document.querySelector(`.sprint-item-row[data-item="it-1"] .sprint-edit-input`) as HTMLInputElement;
      expect(kept).toBe(titleInput);
      expect(kept.value).toBe("a title I am rewriting");
    },
  );
});

// ---------------------------------------------------------------------------
// HITL drawer: the per-project tab
// ---------------------------------------------------------------------------
describe("HITL drawer: a repaint keeps an answer being typed", () => {
  const card = (id: string, question: string, over: Record<string, any> = {}) => ({
    id, status: "pending", question, urgency: "normal", created_at: "2026-10-07T10:00:00", ...over,
  });
  let rows: any[];
  let api: ReturnType<typeof vi.fn>;
  let fns: Fns;
  const body = () => document.getElementById(`hitl-body-${PID}`) as HTMLElement;
  const ans = (id: string) => document.getElementById(`hitl-ans-${id}`) as HTMLInputElement | null;

  beforeEach(() => {
    document.body.innerHTML =
      `<select id="hitl-status-filter-${PID}"><option value="pending">pending</option></select>` +
      `<button id="hitl-refresh-${PID}"></button><div id="hitl-body-${PID}"></div>`;
    rows = [card("r1", "Which database?"), card("r2", "Ship it?")];
    api = vi.fn(async () => rows.slice());
    fns = load(["loadHitlTab"], {
      api, escapeHtml, toast: vi.fn(), confirm: vi.fn(() => true), _wireTabSearch: vi.fn(),
      _removeHitlCard: vi.fn(), paintKeepingDrafts, hasUnsentDrafts,
    });
  });

  async function open() {
    await fns.loadHitlTab(PID);
    await flush();
  }

  it("control: the first open paints a card per pending request", async () => {
    await open();
    expect(ans("r1")).not.toBeNull();
    expect(ans("r2")).not.toBeNull();
  });

  it("a hitl_filed repaint (loadHitlTab again) keeps the text, the caret and the focus", async () => {
    await open();
    const first = ans("r1")!;
    typeInto(first, "Postgres, but", [2, 5]);
    rows.push(card("r3", "A brand-new question?"));

    await fns.loadHitlTab(PID); // what the hitl_filed handler does
    await flush();

    expect(ans("r1")).toBe(first);
    expect(first.value).toBe("Postgres, but");
    expect(document.activeElement).toBe(first);
    expect(caretOf(first)).toEqual([2, 5]);
    expect(ans("r3")).not.toBeNull(); // the list itself did repaint
    expect(body().querySelectorAll(".hitl-row")).toHaveLength(3);
  });

  it("does not blank the list with a 'loading…' placeholder while a draft is on screen", async () => {
    await open();
    typeInto(ans("r1")!, "typing");
    fns.loadHitlTab(PID); // synchronous part only: the request has not returned yet
    expect(body().textContent).not.toContain("loading");
    expect(ans("r1")!.value).toBe("typing");
    await flush();
  });

  it("control: with nothing typed the placeholder is still shown while the request is out", async () => {
    await open();
    fns.loadHitlTab(PID);
    expect(body().textContent).toContain("loading");
    await flush();
  });

  it("text typed while the request was in flight is kept too (judged after the fetch)", async () => {
    await open();
    const first = ans("r1")!;
    first.focus(); // clicked in, nothing typed yet
    let release: (v: any) => void = () => {};
    api.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    fns.loadHitlTab(PID);
    first.value = "typed while loading";
    first.setSelectionRange(1, 3);
    release(rows.slice());
    await flush();
    expect(ans("r1")).toBe(first);
    expect(first.value).toBe("typed while loading");
    expect(document.activeElement).toBe(first);
  });

  it("the Refresh button and a status-filter change keep it as well", async () => {
    await open();
    const first = ans("r1")!;
    typeInto(first, "half");
    (document.getElementById(`hitl-refresh-${PID}`) as HTMLElement).click();
    await flush();
    expect(ans("r1")).toBe(first);
    const select = document.getElementById(`hitl-status-filter-${PID}`) as HTMLSelectElement;
    await select.onchange!(new Event("change"));
    expect(ans("r1")).toBe(first);
    expect(first.value).toBe("half");
  });

  it("each card keeps its own draft, and a request answered elsewhere loses only its own", async () => {
    await open();
    typeInto(ans("r2")!, "second draft");
    ans("r1")!.value = "first draft";
    rows = [rows[1]!, card("r4", "Another?")]; // r1 was answered by a teammate
    await fns.loadHitlTab(PID);
    await flush();
    expect(ans("r1")).toBeNull();
    expect(ans("r2")!.value).toBe("second draft");
    expect(document.activeElement).toBe(ans("r2"));
    expect(ans("r4")!.value).toBe("");
  });

  it("a transient fetch failure leaves the cards and the draft alone", async () => {
    await open();
    const first = ans("r1")!;
    typeInto(first, "do not lose me");
    api.mockRejectedValueOnce(new Error("server restarting"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await fns.loadHitlTab(PID);
    await flush();
    expect(ans("r1")).toBe(first);
    expect(first.value).toBe("do not lose me");
    expect(body().textContent).not.toContain("failed to load");
    warn.mockRestore();
  });

  it("control: with no draft a failed fetch still reports the failure", async () => {
    await open();
    api.mockRejectedValueOnce(new Error("server restarting"));
    await fns.loadHitlTab(PID);
    await flush();
    expect(body().textContent).toContain("failed to load HITL queue");
  });

  it("the Answer button sends the text that survived the repaint", async () => {
    await open();
    typeInto(ans("r1")!, "Postgres");
    rows.push(card("r3", "More?"));
    await fns.loadHitlTab(PID);
    await flush();
    (body().querySelector('.hitl-answer-btn[data-hitl-id="r1"]') as HTMLElement).click();
    await flush();
    expect(api).toHaveBeenCalledWith(
      "/hitl/r1",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ action: "answer", answer: "Postgres" }) }),
    );
  });
});

// ---------------------------------------------------------------------------
// The global HITL panel
// ---------------------------------------------------------------------------
describe("global HITL panel: a repaint keeps an answer being typed", () => {
  const req = (id: string, question: string) => ({
    id, project_id: "p1", urgency: "normal", question, created_at: "2026-10-07 10:00:00",
  });
  let pending: any[];
  let hitlAnswer: ReturnType<typeof vi.fn>;
  let fns: Fns;
  const list = () => document.getElementById("hitl-list") as HTMLElement;
  const input = (id: string) => list().querySelector(`.hitl-answer-input[data-hitl-id="${id}"]`) as HTMLInputElement | null;

  beforeEach(() => {
    document.body.innerHTML =
      `<div id="hitl-bar" style="display:none"><span id="hitl-count"></span></div>` +
      `<div id="hitl-panel"></div><div id="hitl-list"></div>`;
    pending = [req("h1", "Deploy now?"), req("h2", "Which region?")];
    hitlAnswer = vi.fn();
    fns = load(["refreshHitl"], {
      api: vi.fn(async (path: string) => (path.includes("status=pending") ? pending.slice() : [])),
      setVtabCountBadge: vi.fn(),
      formatRelativeTime: () => "just now",
      escapeHtml,
      _HITL_URGENCY_COLOR: { normal: "#888", high: "#fa0", blocking: "#f00" },
      _renderAutoAnsweredHitls: vi.fn(() => ""),
      _hitlAnswer: hitlAnswer,
      _hitlDismiss: vi.fn(),
      paintKeepingDrafts,
    });
  });

  it("control: the first refresh paints a card per pending request", async () => {
    await fns.refreshHitl();
    expect(input("h1")).not.toBeNull();
    expect(document.getElementById("hitl-count")!.textContent).toBe("2");
  });

  it("a second refresh (every hitl_filed event, every reconnect) keeps the text, caret and focus", async () => {
    await fns.refreshHitl();
    const first = input("h1")!;
    typeInto(first, "yes, after the", [4, 9]);
    pending.push(req("h3", "A third question?"));

    await fns.refreshHitl();

    expect(input("h1")).toBe(first);
    expect(first.value).toBe("yes, after the");
    expect(document.activeElement).toBe(first);
    expect(caretOf(first)).toEqual([4, 9]);
    expect(input("h3")).not.toBeNull(); // the panel itself did repaint
  });

  it("Enter in the kept box still answers that request, and Answer too", async () => {
    await fns.refreshHitl();
    typeInto(input("h1")!, "yes");
    await fns.refreshHitl();
    input("h1")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(hitlAnswer).toHaveBeenCalledWith("h1");
    (list().querySelector('.hitl-answer-btn[data-hitl-id="h1"]') as HTMLElement).click();
    expect(hitlAnswer).toHaveBeenCalledTimes(2);
  });

  it("an untouched box is repainted (control), and a request resolved elsewhere takes its draft with it", async () => {
    await fns.refreshHitl();
    const untouched = input("h2")!;
    typeInto(input("h1")!, "answer for a request someone else answered");
    pending = [pending[1]!];
    await fns.refreshHitl();
    expect(input("h1")).toBeNull();
    expect(input("h2")).not.toBe(untouched);
  });

  it("the panel hides itself when nothing is pending, drafts or not", async () => {
    await fns.refreshHitl();
    typeInto(input("h1")!, "x");
    pending = [];
    await fns.refreshHitl();
    expect((document.getElementById("hitl-bar") as HTMLElement).style.display).toBe("none");
  });
});

// ---------------------------------------------------------------------------
// The Status tab's pending-HITL reply boxes (rebuilt by every task event)
// ---------------------------------------------------------------------------
describe("pending-HITL reply boxes: a task event keeps a reply being typed", () => {
  const task = (id: string, description: string) => ({ id, description, status: "pending-hitl" });
  let tasks: any[];
  let state: any;
  let hitlReply: ReturnType<typeof vi.fn>;
  let fns: Fns;
  const reply = (id: string) => document.querySelector(`#hitl-queue-${PID} input[data-input="${id}"]`) as HTMLInputElement | null;

  beforeEach(() => {
    document.body.innerHTML =
      `<div id="hitl-banner-${PID}"></div><div id="hitl-queue-${PID}"></div><div id="tasks-${PID}"></div>`;
    tasks = [task("t1", "[ASK]: Which branch?"), task("t2", "[ASK]: Which tag?")];
    state = { panels: { [PID]: { taskCache: tasks } } };
    hitlReply = vi.fn();
    fns = load(["renderTasks", "renderHitlRow", "wireHitlRow"], {
      state, escapeHtml, renderTaskRow: () => "", _wireTabSearch: vi.fn(), toast: vi.fn(),
      hitlReply, hitlExecute: vi.fn(), _loadMoreTasks: vi.fn(), paintKeepingDrafts,
    });
  });

  it("keeps the text, the caret and the focus when the list is rebuilt, and Reply still sends it", () => {
    fns.renderTasks(PID);
    const first = reply("t1")!;
    typeInto(first, "main, not", [1, 4]);
    tasks.push(task("t3", "[ASK]: A new one?"));

    fns.renderTasks(PID); // task_created / task_updated

    expect(reply("t1")).toBe(first);
    expect(first.value).toBe("main, not");
    expect(document.activeElement).toBe(first);
    expect(caretOf(first)).toEqual([1, 4]);
    expect(reply("t3")).not.toBeNull();
    (document.querySelector(`#hitl-queue-${PID} [data-task="t1"] button[data-action="reply"]`) as HTMLElement).click();
    expect(hitlReply).toHaveBeenCalledWith(PID, "t1", "main, not");
  });

  it("control: an untouched reply box is simply repainted", () => {
    fns.renderTasks(PID);
    const untouched = reply("t2")!;
    fns.renderTasks(PID);
    expect(reply("t2")).not.toBe(untouched);
  });
});

// ---------------------------------------------------------------------------
// One event, both HITL surfaces
// ---------------------------------------------------------------------------
describe("a hitl_filed event repaints both HITL surfaces and loses neither answer", () => {
  it("through the real handleWsEvent", async () => {
    document.body.innerHTML =
      `<div id="hitl-bar"><span id="hitl-count"></span></div><div id="hitl-panel"></div><div id="hitl-list"></div>` +
      `<select id="hitl-status-filter-${PID}"><option value="pending">pending</option></select>` +
      `<div id="hitl-body-${PID}"></div>`;
    const pending = [{ id: "h1", project_id: PID, status: "pending", urgency: "normal", question: "Deploy?", created_at: "2026-10-07 10:00:00" }];
    const api = vi.fn(async (path: string) => (path.includes("status=answered") ? [] : pending.slice()));
    const state = { panels: { [PID]: { activeVtab: "hitl", taskCache: [] } }, tabs: [], projects: [] };
    const fns = load(["handleWsEvent", "refreshHitl", "loadHitlTab"], {
      state, api, escapeHtml, toast: vi.fn(), confirm: vi.fn(() => true), _wireTabSearch: vi.fn(), _removeHitlCard: vi.fn(),
      paintKeepingDrafts, hasUnsentDrafts, setVtabCountBadge: vi.fn(), formatRelativeTime: () => "now",
      _HITL_URGENCY_COLOR: { normal: "#888" }, _renderAutoAnsweredHitls: vi.fn(() => ""), _hitlAnswer: vi.fn(),
      _hitlDismiss: vi.fn(), refreshProjectCountBadges: vi.fn(),
    });
    await fns.refreshHitl();
    await fns.loadHitlTab(PID);
    await flush();
    const panelBox = document.querySelector("#hitl-list .hitl-answer-input") as HTMLInputElement;
    const drawerBox = document.getElementById("hitl-ans-h1") as HTMLInputElement;
    panelBox.value = "typed in the panel";
    drawerBox.value = "typed in the drawer";

    fns.handleWsEvent(PID, { type: "hitl_filed", project_id: PID });
    await flush();

    expect((document.querySelector("#hitl-list .hitl-answer-input") as HTMLInputElement)).toBe(panelBox);
    expect(panelBox.value).toBe("typed in the panel");
    expect(document.getElementById("hitl-ans-h1")).toBe(drawerBox);
    expect(drawerBox.value).toBe("typed in the drawer");
  });
});
