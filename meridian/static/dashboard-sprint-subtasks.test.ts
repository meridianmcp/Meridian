// Live sprint board: a SUBTASK moved to another version on its own (0c30b989).
//
// The server already did the right thing -- /move changes only the child's
// version. The bug was on the board: renderSprintProgress folded EVERY child of a
// displayed parent into the parent's collapsed <details> block, under the
// PARENT's version header, whatever the child's own version was. So a subtask sent
// to v2.2 stayed hidden under v2.1 with a [v2.2] badge, the v2.2 group did not list
// it, and flashMovedItem highlighted a row nobody could see.
//
// These tests render parents and re-versioned children through the real
// renderSprintProgress and read the DOM the way a human does: which header a row
// sits under, and whether a closed <details> hides it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// Registers escapeHtml / getPanelState / formatRelativeTime / ... on window,
// which the sprint renderers use as bare globals.
import "./dashboard-utils";
import { renderSprintProgress } from "./dashboard-sprint";
import { flashMovedItem } from "./dashboard-versions";

const PID = "proj-1";

type Item = Record<string, any>;
const mk = (over: Item): Item => ({
  id: "x",
  title: "x",
  version: "v2.1",
  status: "pending",
  pushed_to: null,
  parent_id: null,
  ...over,
});

/** The scenario from the report: Parent alpha (v2.1) with two v2.1 subtasks, a
 *  standalone item in v2.1 and an item already living in v2.2. */
const scenario = (over: { c1?: Item; c2?: Item; p1?: Item } = {}): Item[] => [
  mk({ id: "p1", title: "Parent alpha", ...over.p1 }),
  mk({ id: "c1", title: "Child one of alpha", parent_id: "p1", ...over.c1 }),
  mk({ id: "c2", title: "Child two of alpha", parent_id: "p1", ...over.c2 }),
  mk({ id: "s1", title: "Standalone beta" }),
  mk({ id: "e1", title: "Existing in v2.2", version: "v2.2" }),
];

const root = () => document.getElementById(`live-sprint-progress-${PID}`)!;
const row = (id: string) => root().querySelector<HTMLElement>(`.sprint-item-row[data-item="${id}"]`);
const rows = (id: string) => root().querySelectorAll<HTMLElement>(`.sprint-item-row[data-item="${id}"]`);
const titleOf = (el: Element) => el.getAttribute("data-title") || "";

interface Group { header: string; rows: string[]; subtasks: Record<string, string[]> }

/** The board as a human reads it: each version header with the rows listed
 *  directly under it, and, per row, the titles folded inside its subtasks block. */
function layout(): Group[] {
  const groups: Group[] = [];
  let current: Group | null = null;
  let lastRow = "";
  for (const el of Array.from(root().children)) {
    if (el.classList.contains("sprint-group-header")) {
      current = { header: el.textContent || "", rows: [], subtasks: {} };
      groups.push(current);
    } else if (el.classList.contains("sprint-item-row")) {
      if (!current) groups.push((current = { header: "", rows: [], subtasks: {} }));
      lastRow = titleOf(el);
      current.rows.push(lastRow);
    } else if (el.tagName === "DETAILS" && current && lastRow && el.querySelector(".sprint-item-row")) {
      current.subtasks[lastRow] = Array.from(el.querySelectorAll(".sprint-item-row")).map(titleOf);
    }
  }
  return groups;
}

/** The version header a row sits under, or null when it is not a direct child of
 *  the board (i.e. it is nested inside a parent's subtasks block). */
function headerOf(el: HTMLElement): string | null {
  if (el.parentElement !== root()) return null;
  let sib = el.previousElementSibling;
  while (sib && !sib.classList.contains("sprint-group-header")) sib = sib.previousElementSibling;
  return sib ? sib.textContent : "";
}

/** jsdom has no layout, so "visible" means: no collapsed <details> ancestor. */
const hiddenByCollapsedDetails = (el: HTMLElement) => {
  for (let n = el.parentElement; n; n = n.parentElement) {
    if (n.tagName === "DETAILS" && !(n as HTMLDetailsElement).open) return true;
  }
  return false;
};

beforeEach(() => {
  document.body.innerHTML = `<div id="live-sprint-progress-${PID}"></div>`;
  // Two helpers renderSprintProgress calls that dashboard.ts (not importable
  // here) defines; they wire the "+ Add" row, which these tests do not use.
  Object.assign(window, {
    wireSprintAddEnter: () => {},
    addSprintItemFromInput: () => {},
    state: { panels: {} },
  });
});
afterEach(() => vi.useRealTimers());

describe("a subtask moved to another version on its own", () => {
  it("is listed under the TARGET version header, not inside the parent's subtasks block", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" } }));

    expect(layout()).toEqual([
      {
        header: "v2.1",
        rows: ["Parent alpha", "Standalone beta"],
        // Only the sibling that did NOT move is still folded under the parent.
        subtasks: { "Parent alpha": ["Child two of alpha"] },
      },
      {
        header: "v2.2",
        rows: ["Child one of alpha", "Existing in v2.2"],
        subtasks: {},
      },
    ]);

    const moved = row("c1")!;
    expect(headerOf(moved)).toBe("v2.2");
    expect(moved.closest("details")).toBeNull();
    // ...and it is not left behind in the parent's collapsed block as well.
    expect(rows("c1")).toHaveLength(1);
    const parentBlock = row("p1")!.nextElementSibling!;
    expect(parentBlock.tagName).toBe("DETAILS");
    expect(parentBlock.querySelector('[data-item="c1"]')).toBeNull();
  });

  it("is visibly marked as a subtask of its parent, and only that row is", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" } }));

    const tags = root().querySelectorAll(".sprint-subtask-tag");
    expect(tags).toHaveLength(1);
    expect(tags[0].textContent).toBe("subtask of Parent alpha");
    expect(tags[0].getAttribute("data-subtask-of")).toBe("p1");
    expect(row("c1")!.contains(tags[0])).toBe(true);
    // The parent, the sibling still folded under it and the unrelated rows carry none.
    for (const id of ["p1", "c2", "s1", "e1"]) {
      expect(row(id)!.querySelector(".sprint-subtask-tag")).toBeNull();
    }
    // It keeps its own title, version label and arrow: it is a full row, not a stub.
    expect(row("c1")!.getAttribute("data-title")).toBe("Child one of alpha");
    expect(row("c1")!.querySelector(".sprint-item-ver")!.textContent).toBe("v2.2");
    expect(row("c1")!.querySelector('[data-act="move-version"]')).not.toBeNull();
  });

  it("escapes the parent's title in the tag", () => {
    renderSprintProgress(
      PID,
      scenario({ p1: { title: 'Parent <img src=x onerror="alert(1)">' }, c1: { version: "v2.2" } }),
    );
    const tag = row("c1")!.querySelector(".sprint-subtask-tag")!;
    expect(tag.querySelector("img")).toBeNull();
    expect(tag.textContent).toBe('subtask of Parent <img src=x onerror="alert(1)">');
  });

  it("keeps a child with the SAME version as its parent in the collapsed subtasks block", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" } }));

    const sibling = row("c2")!;
    expect(headerOf(sibling)).toBeNull(); // nested, not a board-level row
    expect(sibling.closest("details")).not.toBeNull();
    expect(hiddenByCollapsedDetails(sibling)).toBe(true); // collapsed by default, as before
    expect(row("p1")!.nextElementSibling!.contains(sibling)).toBe(true);
    expect(sibling.querySelector(".sprint-subtask-tag")).toBeNull();
  });

  it("with no child moved the board is exactly as before: both subtasks folded under the parent", () => {
    renderSprintProgress(PID, scenario());

    expect(layout()).toEqual([
      {
        header: "v2.1",
        rows: ["Parent alpha", "Standalone beta"],
        subtasks: { "Parent alpha": ["Child one of alpha", "Child two of alpha"] },
      },
      { header: "v2.2", rows: ["Existing in v2.2"], subtasks: {} },
    ]);
    expect(root().querySelector(".sprint-subtask-tag")).toBeNull();
    const summary = row("p1")!.nextElementSibling!.querySelector("summary")!.textContent!;
    expect(summary).toContain("2 subtasks");
    expect(summary).toContain("0/2 done");
  });

  it("still counts in the parent's [done/total] badge", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" } }));
    const badge = (id: string) => row(id)!.querySelector(".sprint-item-title")!.textContent!;
    // Two subtasks in all, none done; the one that left is still one of them.
    expect(badge("p1")).toContain("[0/2]");
    // Neither the moved child nor its sibling grow a badge of their own.
    expect(badge("c1")).not.toMatch(/\[\d+\/\d+\]/);
    expect(badge("c2")).not.toMatch(/\[\d+\/\d+\]/);
  });

  it("the badge counts the moved child's own progress too", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2", status: "done" } }));
    // c1 is done in v2.2 (which has an active peer, so it is on the board).
    expect(row("p1")!.querySelector(".sprint-item-title")!.textContent).toContain("[1/2]");
    expect(headerOf(row("c1")!)).toBe("v2.2");
    // The parent's block header still tells the truth about what is folded in it.
    const summary = row("p1")!.nextElementSibling!.querySelector("summary")!.textContent!;
    expect(summary).toContain("1 subtask");
    expect(summary).toContain("1/2 done");
  });

  it("drops the parent's subtasks block altogether once every subtask has left", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" }, c2: { version: "v2.2" } }));
    expect(layout()[0]).toEqual({ header: "v2.1", rows: ["Parent alpha", "Standalone beta"], subtasks: {} });
    expect(row("p1")!.nextElementSibling!.tagName).not.toBe("DETAILS");
    expect(row("p1")!.querySelector(".sprint-item-title")!.textContent).toContain("[0/2]");
    expect(layout()[1].rows).toEqual(["Child one of alpha", "Child two of alpha", "Existing in v2.2"]);
  });

  it("works when the child was moved to a version nobody else is in yet", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v3.0" } }));
    // (Groups appear in the order the list first mentions a version.)
    expect(layout().map((g) => g.header).sort()).toEqual(["v2.1", "v2.2", "v3.0"]);
    expect(layout().find((g) => g.header === "v3.0")!.rows).toEqual(["Child one of alpha"]);
    expect(headerOf(row("c1")!)).toBe("v3.0");
    expect(row("c1")!.querySelector(".sprint-subtask-tag")!.textContent).toBe("subtask of Parent alpha");
  });

  it("a subtask with no version at all stands on its own row too, not inside the parent", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "" } }));
    const moved = row("c1")!;
    expect(moved.closest("details")).toBeNull();
    expect(moved.parentElement).toBe(root());
    expect(moved.querySelector(".sprint-subtask-tag")).not.toBeNull();
    expect(row("p1")!.nextElementSibling!.querySelector('[data-item="c1"]')).toBeNull();
  });

  it("a grandchild keeps folding under the moved subtask when they share a version", () => {
    renderSprintProgress(PID, [
      mk({ id: "p1", title: "Parent alpha" }),
      mk({ id: "c1", title: "Child one of alpha", parent_id: "p1", version: "v2.2" }),
      mk({ id: "g1", title: "Grandchild of c1", parent_id: "c1", version: "v2.2" }),
      mk({ id: "e1", title: "Existing in v2.2", version: "v2.2" }),
    ]);
    expect(layout()).toEqual([
      { header: "v2.1", rows: ["Parent alpha"], subtasks: {} },
      {
        header: "v2.2",
        rows: ["Child one of alpha", "Existing in v2.2"],
        subtasks: { "Child one of alpha": ["Grandchild of c1"] },
      },
    ]);
    // c1 is tagged against p1; g1's parent moved with it, so g1 needs no tag.
    expect(row("c1")!.querySelector(".sprint-subtask-tag")!.textContent).toBe("subtask of Parent alpha");
    expect(row("g1")!.querySelector(".sprint-subtask-tag")).toBeNull();
  });

  it("a child whose parent is not on the board is a plain row, as before (no tag)", () => {
    renderSprintProgress(PID, [
      mk({ id: "p0", title: "Finished parent", version: "v2.0", status: "done" }),
      mk({ id: "c9", title: "Orphaned child", parent_id: "p0", version: "v2.1" }),
      mk({ id: "s1", title: "Standalone beta" }),
    ]);
    expect(layout()).toEqual([{ header: "v2.1", rows: ["Orphaned child", "Standalone beta"], subtasks: {} }]);
    expect(root().querySelector(".sprint-subtask-tag")).toBeNull();
  });
});

describe("the landing highlight after a subtask move", () => {
  it("flashMovedItem finds exactly one row for the moved subtask and it is not hidden in a collapsed block", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" } }));

    expect(rows("c1")).toHaveLength(1);
    expect(hiddenByCollapsedDetails(row("c1")!)).toBe(false);

    vi.useFakeTimers();
    const scrolled = vi.fn();
    row("c1")!.scrollIntoView = scrolled;
    flashMovedItem("c1");

    const flashed = Array.from(root().querySelectorAll(".sprint-row-moved"));
    expect(flashed).toHaveLength(1);
    expect(flashed[0]).toBe(row("c1"));
    expect(hiddenByCollapsedDetails(flashed[0] as HTMLElement)).toBe(false);
    // The row it scrolls to is the one under the v2.2 header.
    expect(scrolled).toHaveBeenCalledTimes(1);
    expect(headerOf(flashed[0] as HTMLElement)).toBe("v2.2");

    vi.advanceTimersByTime(2500);
    expect(root().querySelectorAll(".sprint-row-moved")).toHaveLength(0);
  });

  it("contrast: a same-version subtask is still hidden in the collapsed block, which is why a moved one cannot stay there", () => {
    renderSprintProgress(PID, scenario({ c1: { version: "v2.2" } }));
    expect(hiddenByCollapsedDetails(row("c2")!)).toBe(true);
    expect(hiddenByCollapsedDetails(row("c1")!)).toBe(false);
  });
});
