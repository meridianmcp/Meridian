// Tests for dashboard-sprint-move.ts (0c30b989): the glue behind the sprint arrow
// button. dashboard-versions.test.ts covers the popover through callbacks; this
// file covers what the callbacks DO -- which endpoint is posted, what body is
// sent, what the toast claims, which views repaint, and where the landing
// highlight finds the row -- driven from the REAL arrow-button markup that
// renderSprintProgress (Live board) and renderQueue (Queue tab) emit, so a
// change to either side of that contract fails here instead of shipping silently.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// Registers escapeHtml / getPanelState / formatRelativeTime / ... on window,
// which the sprint renderers use as bare globals.
import "./dashboard-utils";
import { renderQueue, renderSprintProgress } from "./dashboard-sprint";
import { createSprintMoveActions, queueBodyId } from "./dashboard-sprint-move";
import { closeVersionMovePopover, nextVersion, openVersionMovePopoverItemId } from "./dashboard-versions";

const PID = "proj-1";

/** Inline onclick="..." attributes run in jsdom's own global, which vitest only
 *  mirrors onto `window` for names that existed at start-up. Set the name on
 *  both, the way dashboard.ts's Object.assign(window, {...}) does in a browser. */
function exposeToInlineHandlers(name: string, fn: unknown) {
  (window as any)[name] = fn;
  const dom = (globalThis as any).jsdom;
  if (dom && dom.window) dom.window[name] = fn;
}
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

type Item = Record<string, any>;
const mk = (over: Item): Item => ({
  id: "a1",
  title: "Add rate limiting",
  version: "v2.1",
  status: "pending",
  pushed_to: null,
  ...over,
});

interface Call { path: string; method: string; body: any }

function apiError(status: number, body: unknown) {
  const err: any = new Error(`${status}: ${JSON.stringify(body)}`);
  err.status = status;
  err.endpoint = "x";
  err.responseText = JSON.stringify(body);
  return err;
}

/** A fake of the server and of dashboard.ts's wiring: records every request the
 *  glue makes, answers like routes/sprint.py does, and counts the repaints. */
function harness(initial: Item[], opts: { queueOpen?: boolean } = {}) {
  const items = initial.map((i) => ({ ...i }));
  const calls: Call[] = [];
  const state = {
    // Hook to bend the server's answer to /move (unverifiable, unchanged, 409...).
    moveAnswer: null as null | ((item: Item, body: any) => any),
    listError: null as null | Error,
  };
  const api = vi.fn(async (path: string, init: RequestInit = {}) => {
    const method = init.method || "GET";
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ path, method, body });
    if (method === "GET" && path === `/projects/${PID}/sprint-items`) {
      if (state.listError) throw state.listError;
      return items.map((i) => ({ ...i }));
    }
    const move = path.match(/^\/projects\/proj-1\/sprint-items\/([^/]+)\/move$/);
    if (method === "POST" && move) {
      const item = items.find((i) => i.id === move[1])!;
      if (state.moveAnswer) return state.moveAnswer(item, body);
      const target = body.next ? nextVersion(item.version) : body.to_version;
      const from = item.version;
      item.version = target;
      return { item: { ...item }, from_version: from, to_version: target, via: body.next ? "next" : "specific", unchanged: false };
    }
    const push = path.match(/^\/projects\/proj-1\/sprint-items\/([^/]+)\/push$/);
    if (method === "POST" && push) {
      const item = items.find((i) => i.id === push[1])!;
      item.status = "pushed";
      item.pushed_to = body.to_version;
      return { ...item };
    }
    throw new Error(`unexpected request ${method} ${path}`);
  });
  const toast = vi.fn();
  const refreshLiveTab = vi.fn(async (_pid: string) => {});
  const loadQueue = vi.fn(async (_pid: string) => {});
  const reloadSprintBoard = vi.fn((_pid: string): Promise<unknown> | void => {});
  const actions = createSprintMoveActions({ api, toast, refreshLiveTab, loadQueue, reloadSprintBoard });

  // The DOM the dashboard has on screen: the Live board and (optionally) the
  // Queue tab, both painted by the real renderers from the same items.
  const paint = () => {
    renderSprintProgress(PID, items);
    const qb = document.getElementById(queueBodyId(PID));
    if (qb) qb.innerHTML = renderQueue(PID, items);
  };
  document.body.innerHTML =
    `<div id="live-sprint-progress-${PID}"></div>` +
    (opts.queueOpen === false ? "" : `<div id="${queueBodyId(PID)}"></div>`);
  paint();

  // renderSprintProgress / renderQueue emit onclick="sprintPushPrompt(pid, id,
  // this)"; dashboard.ts exposes that name on window. Same here.
  exposeToInlineHandlers("sprintPushPrompt", actions.sprintPushPrompt);
  return { items, calls, state, api, toast, refreshLiveTab, loadQueue, reloadSprintBoard, actions, paint };
}

const arrow = (where: "live" | "queue", id = "a1") =>
  document.querySelector<HTMLButtonElement>(
    (where === "live" ? `#live-sprint-progress-${PID}` : `#${queueBodyId(PID)}`) +
      ` [data-act="move-version"][data-item-id="${id}"]`,
  )!;
const pop = () => document.querySelector<HTMLElement>(".version-move-popover");
const q = <T extends HTMLElement>(sel: string) => document.querySelector<T>(`.version-move-popover ${sel}`)!;
const movePosts = (calls: Call[]) => calls.filter((c) => c.method === "POST");

/** Click the arrow and wait for the item list to load and the popover to open. */
async function openFrom(where: "live" | "queue", id = "a1") {
  arrow(where, id).click();
  await vi.waitFor(() => expect(pop()).not.toBeNull());
}

beforeEach(() => {
  document.body.innerHTML = "";
  // Two helpers renderSprintProgress calls that dashboard.ts (not importable
  // here) defines; they wire the "+ Add" row, which these tests do not use.
  Object.assign(window, {
    wireSprintAddEnter: () => {},
    addSprintItemFromInput: () => {},
    state: { panels: {} },
  });
});
afterEach(() => {
  closeVersionMovePopover();
  exposeToInlineHandlers("sprintPushPrompt", undefined);
  vi.useRealTimers();
});

describe("the arrow button markup the glue is driven from", () => {
  it("exists on pending rows of the Live board and of the Queue, never on finished ones", () => {
    harness([mk({}), mk({ id: "d1", title: "finished", status: "done" })]);
    expect(arrow("live")).not.toBeNull();
    expect(arrow("queue")).not.toBeNull();
    expect(arrow("live", "d1")).toBeNull();
    expect(arrow("queue", "d1")).toBeNull();
  });

  it("the inline onclick hands the project id, the item id and the button itself to sprintPushPrompt", () => {
    const h = harness([mk({})]);
    const spy = vi.fn(h.actions.sprintPushPrompt);
    exposeToInlineHandlers("sprintPushPrompt", spy);
    arrow("queue").click();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0]).toBe(PID);
    expect(spy.mock.calls[0][1]).toBe("a1");
    expect(spy.mock.calls[0][2]).toBe(arrow("queue"));
  });

  it("the Queue card carries data-item-id (the landing highlight looks it up there)", () => {
    harness([mk({})]);
    expect(document.querySelector(`#${queueBodyId(PID)} .queue-item[data-item-id="a1"]`)).not.toBeNull();
    expect(document.querySelector(`#live-sprint-progress-${PID} .sprint-item-row[data-item="a1"]`)).not.toBeNull();
  });
});

describe("move to the next version", () => {
  it("POSTs /move with next and the version the human saw, states the number, repaints, closes", async () => {
    const h = harness([mk({})]);
    await openFrom("live");
    expect(q(".vmp-next").textContent).toBe("Move to v2.2 (next)");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(pop()).toBeNull());

    // Exactly one write, to the MOVE endpoint (not /push), not touching the title.
    expect(movePosts(h.calls)).toEqual([
      { path: `/projects/${PID}/sprint-items/a1/move`, method: "POST", body: { next: true, expected_version: "v2.1" } },
    ]);
    expect(h.toast).toHaveBeenCalledTimes(1);
    expect(h.toast).toHaveBeenCalledWith("Moved to v2.2");
    expect(h.items[0].title).toBe("Add rate limiting");
    expect(h.items[0].status).toBe("pending");
  });

  it("repaints the Live board, the Queue and the Goal sprint board, each for this project", async () => {
    const h = harness([mk({})]);
    await openFrom("queue");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(h.reloadSprintBoard).toHaveBeenCalledWith(PID));
    expect(h.refreshLiveTab).toHaveBeenCalledWith(PID);
    expect(h.loadQueue).toHaveBeenCalledWith(PID);
  });

  it("does not load the Queue when that tab was never opened, but still repaints the others", async () => {
    const h = harness([mk({})], { queueOpen: false });
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(h.reloadSprintBoard).toHaveBeenCalledWith(PID));
    expect(h.refreshLiveTab).toHaveBeenCalledWith(PID);
    expect(h.loadQueue).not.toHaveBeenCalled();
  });

  it("highlights the row where it landed, on the Live board AND in the Queue, after the repaint", async () => {
    const h = harness([mk({})]);
    // The real loaders re-render the boards from the server's new state.
    h.refreshLiveTab.mockImplementation(async () => h.paint());
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() =>
      expect(document.querySelectorAll(".sprint-row-moved").length).toBe(2),
    );
    expect(document.querySelector(`#live-sprint-progress-${PID} .sprint-row-moved`)!.getAttribute("data-item")).toBe("a1");
    expect(document.querySelector(`#${queueBodyId(PID)} .sprint-row-moved`)!.getAttribute("data-item-id")).toBe("a1");
    // ...and the Live board now lists it under the new version heading.
    expect(document.querySelector(`#live-sprint-progress-${PID} .sprint-group-header`)!.textContent).toBe("v2.2");
  });

  it("says so when the item was already there instead of claiming a move", async () => {
    const h = harness([mk({})]);
    h.state.moveAnswer = (item) => ({ item: { ...item }, from_version: "v2.1", to_version: "v2.1", unchanged: true });
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(h.toast).toHaveBeenCalled());
    expect(h.toast).toHaveBeenCalledWith("Already in v2.1");
  });

  it("refuses to claim a move the server's own row does not confirm", async () => {
    const h = harness([mk({})]);
    // The server says it used v2.2 but the row it read back is still v2.1.
    h.state.moveAnswer = (item) => ({ item: { ...item, version: "v2.1" }, from_version: "v2.1", to_version: "v2.2", unchanged: false });
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(q(".vmp-error").hidden).toBe(false));
    expect(q(".vmp-error").textContent).toContain("could not be verified");
    expect(pop()).not.toBeNull(); // still open, usable
    expect(h.toast).not.toHaveBeenCalled();
    expect(h.refreshLiveTab).not.toHaveBeenCalled();
    expect(h.loadQueue).not.toHaveBeenCalled();
    expect(h.reloadSprintBoard).not.toHaveBeenCalled();
  });

  it("treats an empty or item-less answer as unverified too", async () => {
    const h = harness([mk({})]);
    h.state.moveAnswer = () => ({ to_version: "v2.2" });
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(q(".vmp-error").hidden).toBe(false));
    expect(h.toast).not.toHaveBeenCalled();
  });

  it("shows the server's reason inline on a 409 and neither toasts a move nor repaints", async () => {
    const h = harness([mk({})]);
    h.state.moveAnswer = () => {
      throw apiError(409, { detail: "sprint item a1 is now in version 'v2.2'" });
    };
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(q(".vmp-error").hidden).toBe(false));
    expect(q(".vmp-error").textContent).toContain("is now in version");
    expect(h.toast).not.toHaveBeenCalled();
    expect(h.refreshLiveTab).not.toHaveBeenCalled();
  });

  it("the popover closes as soon as the move is verified, even while a repaint hangs", async () => {
    const h = harness([mk({})]);
    h.refreshLiveTab.mockImplementation(() => new Promise(() => {}));
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(pop()).toBeNull());
    expect(h.toast).toHaveBeenCalledWith("Moved to v2.2");
  });

  it("one failing repaint does not stop the other two", async () => {
    const h = harness([mk({})]);
    h.loadQueue.mockRejectedValue(new Error("queue down"));
    h.refreshLiveTab.mockImplementation(async () => h.paint());
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await vi.waitFor(() => expect(h.reloadSprintBoard).toHaveBeenCalledWith(PID));
    await vi.waitFor(() => expect(document.querySelector(".sprint-row-moved")).not.toBeNull());
  });

  it("an item whose label has no next version cannot be moved 'next': the button is disabled and nothing is posted", async () => {
    const h = harness([mk({ version: "current sprint v0.2" })]);
    await openFrom("live");
    const next = q<HTMLButtonElement>(".vmp-next");
    expect(next.disabled).toBe(true);
    next.click();
    await flush();
    expect(movePosts(h.calls)).toEqual([]);
  });
});

describe("move to a specific version", () => {
  it("POSTs /move with to_version and the version the human saw, and offers the board's versions", async () => {
    const h = harness([mk({}), mk({ id: "b2", title: "other", version: "v2.5" })]);
    await openFrom("queue");
    const list = document.getElementById(q<HTMLInputElement>(".vmp-input").getAttribute("list")!)!;
    expect(Array.from(list.querySelectorAll("option")).map((o) => (o as HTMLOptionElement).value)).toEqual(["v2.5"]);
    q<HTMLInputElement>(".vmp-input").value = " v3.0 ";
    q<HTMLButtonElement>(".vmp-go").click();
    await vi.waitFor(() => expect(pop()).toBeNull());
    expect(movePosts(h.calls)).toEqual([
      { path: `/projects/${PID}/sprint-items/a1/move`, method: "POST", body: { to_version: "v3.0", expected_version: "v2.1" } },
    ]);
    expect(h.toast).toHaveBeenCalledWith("Moved to v3.0");
    expect(h.items.find((i) => i.id === "a1")!.title).toBe("Add rate limiting");
  });

  it("an item with no version can still be moved to a typed one", async () => {
    const h = harness([mk({ version: "" })]);
    await openFrom("live");
    q<HTMLInputElement>(".vmp-input").value = "v1.0";
    q<HTMLButtonElement>(".vmp-go").click();
    await vi.waitFor(() => expect(pop()).toBeNull());
    expect(movePosts(h.calls)[0].body).toEqual({ to_version: "v1.0", expected_version: "" });
  });
});

describe("defer to backburner (the legacy push, now the third choice)", () => {
  it("POSTs /push with {to_version} (never /move), says deferred, repaints", async () => {
    const h = harness([mk({})]);
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-defer").click();
    await vi.waitFor(() => expect(pop()).toBeNull());
    expect(movePosts(h.calls)).toEqual([
      { path: `/projects/${PID}/sprint-items/a1/push`, method: "POST", body: { to_version: "v2.2" } },
    ]);
    expect(h.toast).toHaveBeenCalledWith("Deferred to backburner (v2.2)");
    await vi.waitFor(() => expect(h.reloadSprintBoard).toHaveBeenCalledWith(PID));
    expect(h.refreshLiveTab).toHaveBeenCalledWith(PID);
    expect(h.loadQueue).toHaveBeenCalledWith(PID);
    // The deferred item is NOT moved: its own version is untouched.
    expect(h.items[0].version).toBe("v2.1");
    expect(h.items[0].status).toBe("pushed");
  });

  it("defers to the version typed in the field when there is one", async () => {
    const h = harness([mk({})]);
    await openFrom("queue");
    q<HTMLInputElement>(".vmp-input").value = "v9.9";
    q<HTMLInputElement>(".vmp-input").dispatchEvent(new Event("input", { bubbles: true }));
    q<HTMLButtonElement>(".vmp-defer").click();
    await vi.waitFor(() => expect(pop()).toBeNull());
    expect(movePosts(h.calls)[0]).toEqual({
      path: `/projects/${PID}/sprint-items/a1/push`, method: "POST", body: { to_version: "v9.9" },
    });
    expect(h.toast).toHaveBeenCalledWith("Deferred to backburner (v9.9)");
  });
});

describe("opening, toggling and failing to open", () => {
  it("fetches the item list, anchors the popover to the clicked arrow, and a second click closes it", async () => {
    const h = harness([mk({})]);
    await openFrom("live");
    expect(h.calls.filter((c) => c.method === "GET" && c.path === `/projects/${PID}/sprint-items`)).toHaveLength(1);
    expect(openVersionMovePopoverItemId()).toBe("a1");
    expect(pop()!.dataset.itemId).toBe("a1");
    // Clicking the same arrow again toggles the popover shut and fetches nothing.
    arrow("live").click();
    await flush();
    expect(pop()).toBeNull();
    expect(openVersionMovePopoverItemId()).toBeNull();
    expect(h.calls.filter((c) => c.method === "GET")).toHaveLength(1);
  });

  it("places the popover under the arrow that was clicked, not in the middle of the screen", async () => {
    harness([mk({})]);
    const btn = arrow("live");
    btn.getBoundingClientRect = () =>
      ({ left: 800, top: 100, right: 830, bottom: 120, width: 30, height: 20, x: 800, y: 100, toJSON() {} }) as DOMRect;
    await openFrom("live");
    // Right-aligned under the anchor: 830 - 300 wide, 120 + 6 gap (see computePopoverPosition).
    expect(pop()!.style.left).toBe("530px");
    expect(pop()!.style.top).toBe("126px");
  });

  it("another item's arrow replaces the open popover instead of stacking a second", async () => {
    harness([mk({}), mk({ id: "b2", title: "second", version: "v3.0" })]);
    await openFrom("live", "a1");
    arrow("live", "b2").click();
    await vi.waitFor(() => expect(pop()!.dataset.itemId).toBe("b2"));
    expect(document.querySelectorAll(".version-move-popover")).toHaveLength(1);
    expect(q(".vmp-next").textContent).toBe("Move to v3.1 (next)");
  });

  it("returns focus to the arrow when Escape closes it", async () => {
    harness([mk({})]);
    await openFrom("live");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(pop()).toBeNull();
    expect(document.activeElement).toBe(arrow("live"));
  });

  it("toasts and opens nothing when the item list cannot be loaded", async () => {
    const h = harness([mk({})]);
    h.state.listError = new Error("offline");
    arrow("live").click();
    await vi.waitFor(() => expect(h.toast).toHaveBeenCalled());
    expect(h.toast).toHaveBeenCalledWith("Could not load the item: offline", true);
    expect(pop()).toBeNull();
  });

  it("toasts, repaints and opens nothing when the item no longer exists", async () => {
    const h = harness([mk({})]);
    h.items.length = 0; // deleted behind the board's back
    arrow("live").click();
    await vi.waitFor(() => expect(h.toast).toHaveBeenCalled());
    expect(h.toast).toHaveBeenCalledWith("That sprint item no longer exists.", true);
    expect(pop()).toBeNull();
    expect(h.refreshLiveTab).toHaveBeenCalledWith(PID);
  });

  it("accepts the {items: [...]} list shape as well as a bare array", async () => {
    const h = harness([mk({})]);
    const original = h.api.getMockImplementation()!;
    h.api.mockImplementation(async (path: string, init?: RequestInit) => {
      const out = await original(path, init);
      return Array.isArray(out) ? { items: out } : out;
    });
    await openFrom("live");
    expect(q(".vmp-next").textContent).toBe("Move to v2.2 (next)");
  });

  it("reports a failure that lands after the popover was already dismissed as a toast", async () => {
    const h = harness([mk({})]);
    let reject!: (e: Error) => void;
    h.state.moveAnswer = () => new Promise((_res, rej) => (reject = rej));
    await openFrom("live");
    q<HTMLButtonElement>(".vmp-next").click();
    await flush();
    q<HTMLButtonElement>(".vmp-close").click(); // human gives up waiting
    expect(pop()).toBeNull();
    reject(apiError(500, { detail: "database is locked" }));
    await vi.waitFor(() => expect(h.toast).toHaveBeenCalled());
    expect(h.toast).toHaveBeenCalledWith("Move failed: database is locked", true);
  });
});
