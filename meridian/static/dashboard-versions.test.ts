// Unit tests for dashboard-versions.ts (0c30b989): the next-version rule shared
// with meridian/versioning.py, and the "move to a version" popover that replaced
// window.prompt on the sprint arrow button.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import cases from "../../tests/fixtures/next_version_cases.json";
import {
  MAX_VERSION_LENGTH,
  closeVersionMovePopover,
  collectBoardVersions,
  compareVersionLabels,
  computePopoverPosition,
  describeMoveError,
  flashMovedItem,
  isNextUnavailableError,
  nextVersion,
  openVersionMovePopover,
  openVersionMovePopoverItemId,
  validateVersionLabel,
  type VersionMovePopoverOptions,
} from "./dashboard-versions";

// ---------------------------------------------------------------------------
// The shared rule. tests/test_versioning.py runs the very same rows against
// meridian/versioning.py, so the two implementations cannot drift apart.
// ---------------------------------------------------------------------------

describe("nextVersion (shared fixture with meridian/versioning.py)", () => {
  for (const c of cases.next_version) {
    it(`${JSON.stringify(c.input)} -> ${JSON.stringify(c.expected)}`, () => {
      expect(nextVersion(c.input)).toBe(c.expected);
    });
  }

  it("covers the owner's own examples", () => {
    expect(nextVersion("v2.1")).toBe("v2.2");
    expect(nextVersion("v2.2")).toBe("v2.3");
  });

  it("never returns something validateVersionLabel would refuse", () => {
    for (const c of cases.next_version) {
      const out = nextVersion(c.input);
      if (out !== null) expect(validateVersionLabel(out)).toEqual({ ok: true, label: out });
    }
  });

  it("is None for non-strings", () => {
    expect(nextVersion(undefined)).toBeNull();
    expect(nextVersion(null)).toBeNull();
    expect(nextVersion(2 as unknown as string)).toBeNull();
  });
});

describe("validateVersionLabel (shared fixture with meridian/versioning.py)", () => {
  for (const c of cases.validate_version_label) {
    it(`${JSON.stringify(c.input).slice(0, 40)} -> ${c.label === null ? "rejected" : "accepted"}`, () => {
      const out = validateVersionLabel(c.input);
      if (c.label === null) {
        expect(out.ok).toBe(false);
      } else {
        expect(out).toEqual({ ok: true, label: c.label });
      }
    });
  }

  it("counts code points like Python's len(), not UTF-16 units", () => {
    // 40 astral characters = 80 UTF-16 units but 40 code points: accepted.
    expect(validateVersionLabel("\u{1F680}".repeat(40)).ok).toBe(true);
    expect(validateVersionLabel("\u{1F680}".repeat(MAX_VERSION_LENGTH + 1)).ok).toBe(false);
  });

  it("rejects non-strings", () => {
    expect(validateVersionLabel(null).ok).toBe(false);
    expect(validateVersionLabel(2).ok).toBe(false);
  });
});

describe("compareVersionLabels / collectBoardVersions", () => {
  it("orders numerically, then free text", () => {
    const sorted = ["v2.10", "v2.9", "v10", "v2", "current sprint v0.2", "0.2", "beta"].sort(
      compareVersionLabels,
    );
    expect(sorted).toEqual(["0.2", "v2", "v2.9", "v2.10", "v10", "beta", "current sprint v0.2"]);
  });

  it("collects the distinct board versions minus the item's own", () => {
    const items = [
      { version: "v2.1" },
      { version: "v2.0" },
      { version: "v2.1" },
      { version: " v2.10 " },
      { version: "" },
      { version: null },
      {},
    ];
    expect(collectBoardVersions(items, "v2.1")).toEqual(["v2.0", "v2.10"]);
    expect(collectBoardVersions(items)).toEqual(["v2.0", "v2.1", "v2.10"]);
    expect(collectBoardVersions(null)).toEqual([]);
  });
});

describe("error helpers", () => {
  const apiError = (status: number, body: unknown) => {
    const err: any = new Error(`${status}: ${JSON.stringify(body)}`);
    err.status = status;
    err.responseText = JSON.stringify(body);
    return err;
  };

  it("pulls the reason out of FastAPI's detail", () => {
    expect(describeMoveError(apiError(409, { detail: "moved by someone else" }))).toBe(
      "moved by someone else",
    );
    expect(
      describeMoveError(apiError(422, { detail: { code: "x", message: "no next version" } })),
    ).toBe("no next version");
    expect(describeMoveError(apiError(422, { detail: [{ msg: "bad field" }] }))).toBe("bad field");
  });

  it("words the demo's read-only marker for a human", () => {
    expect(describeMoveError(new Error("demo_readonly"))).toBe("The demo is read-only.");
  });

  it("falls back to the Error message, then a generic line", () => {
    expect(describeMoveError(new Error("offline"))).toBe("offline");
    expect(describeMoveError({ responseText: "<html>", message: "502" })).toBe("502");
    expect(describeMoveError(null)).toBe("The request failed.");
  });

  // The server's global 404 handler answers EVERY 404 (even the move route's own
  // "sprint item not found") with an HTML error page, and api() builds its message
  // as "<status>: <body>". Without this the popover would print the page's CSS.
  const htmlErrorPage = (status: number) => {
    const body =
      `<!doctype html><html lang='en'><head><meta charset='utf-8'><title>${status}</title>` +
      "<style>body{background:#0b0c0e;color:#fff}</style></head><body><div class='card'>" +
      `<h1>${status}</h1><p>not found</p></div></body></html>`;
    const err: any = new Error(`${status}: ${body}`);
    err.status = status;
    err.responseText = body;
    return err;
  };

  it("says the item is gone for a 404 instead of printing the server's HTML page", () => {
    const text = describeMoveError(htmlErrorPage(404));
    expect(text).toBe("That sprint item no longer exists. Refresh the board.");
    expect(text).not.toMatch(/<|doctype|style/i);
  });

  it("never lets an HTML error page reach the popover for other statuses either", () => {
    expect(describeMoveError(htmlErrorPage(502))).toBe("The request failed (502).");
    expect(describeMoveError(htmlErrorPage(500))).toBe("The request failed (500).");
  });

  it("recognises the server's next-version-unavailable answer", () => {
    expect(
      isNextUnavailableError(
        apiError(422, { detail: { code: "next_version_unavailable", message: "m" } }),
      ),
    ).toBe(true);
    expect(isNextUnavailableError(apiError(422, { detail: "other" }))).toBe(false);
    expect(isNextUnavailableError(apiError(409, { detail: { code: "next_version_unavailable" } }))).toBe(false);
    expect(isNextUnavailableError(new Error("x"))).toBe(false);
  });
});

describe("computePopoverPosition", () => {
  const size = { width: 300, height: 200 };
  const viewport = { width: 1000, height: 700 };

  it("right-aligns under the anchor", () => {
    const pos = computePopoverPosition({ left: 800, top: 100, right: 830, bottom: 120 }, size, viewport);
    expect(pos).toEqual({ left: 530, top: 126 });
  });

  it("flips above when there is no room below", () => {
    const pos = computePopoverPosition({ left: 800, top: 600, right: 830, bottom: 620 }, size, viewport);
    expect(pos.top).toBe(600 - 6 - 200);
  });

  it("stays inside a narrow viewport", () => {
    const pos = computePopoverPosition(
      { left: 10, top: 100, right: 40, bottom: 120 },
      { width: 344, height: 200 },
      { width: 360, height: 640 },
    );
    expect(pos.left).toBe(8);
    expect(pos.left + 344).toBeLessThanOrEqual(360 - 8);
  });

  it("centres when there is no anchor", () => {
    expect(computePopoverPosition(null, size, viewport)).toEqual({ left: 350, top: 250 });
  });
});

// ---------------------------------------------------------------------------
// Popover
// ---------------------------------------------------------------------------

const flush = () => new Promise<void>((r) => setTimeout(r, 0));

function key(target: EventTarget, k: string, init: KeyboardEventInit = {}) {
  const ev = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(ev);
  return ev;
}

describe("version move popover", () => {
  let anchor: HTMLButtonElement;
  let onMoveNext: ReturnType<typeof vi.fn>;
  let onMoveSpecific: ReturnType<typeof vi.fn>;
  let onDefer: ReturnType<typeof vi.fn>;
  let onError: ReturnType<typeof vi.fn>;

  const open = (over: Partial<VersionMovePopoverOptions> = {}) =>
    openVersionMovePopover({
      itemId: "item-1",
      itemTitle: "Add rate limiting",
      currentVersion: "v2.1",
      boardVersions: ["v2.0", "v2.5"],
      anchor,
      onMoveNext: onMoveNext as any,
      onMoveSpecific: onMoveSpecific as any,
      onDefer: onDefer as any,
      onError: onError as any,
      ...over,
    });
  const q = <T extends HTMLElement>(sel: string) =>
    document.querySelector<T>(`.version-move-popover ${sel}`)!;

  beforeEach(() => {
    document.body.innerHTML = "";
    anchor = document.createElement("button");
    anchor.textContent = "→";
    document.body.append(anchor);
    onMoveNext = vi.fn().mockResolvedValue(undefined);
    onMoveSpecific = vi.fn().mockResolvedValue(undefined);
    onDefer = vi.fn().mockResolvedValue(undefined);
    onError = vi.fn();
  });
  afterEach(() => {
    closeVersionMovePopover();
    vi.useRealTimers();
  });

  it("states the computed next version on the primary button and focuses it", () => {
    open();
    const next = q<HTMLButtonElement>(".vmp-next");
    expect(next.textContent).toBe("Move to v2.2 (next)");
    expect(next.disabled).toBe(false);
    expect(document.activeElement).toBe(next);
    const pop = document.querySelector(".version-move-popover")!;
    expect(pop.getAttribute("role")).toBe("dialog");
    expect(pop.getAttribute("aria-label")).toBeTruthy();
    // The item's title is shown as context and is never editable here.
    expect(q(".vmp-title").textContent).toBe("Add rate limiting");
    expect(document.querySelector(".version-move-popover input[type=text]")!.getAttribute("list")).toBeTruthy();
  });

  it("offers the board's versions through a datalist", () => {
    open();
    const input = q<HTMLInputElement>(".vmp-input");
    const list = document.getElementById(input.getAttribute("list")!)!;
    expect(Array.from(list.querySelectorAll("option")).map((o) => o.value)).toEqual(["v2.0", "v2.5"]);
  });

  it("renders titles and versions as text, not HTML", () => {
    open({ itemTitle: "<img src=x onerror=alert(1)>", currentVersion: "<b>v2.1</b>" });
    expect(document.querySelector(".version-move-popover img")).toBeNull();
    expect(document.querySelector(".version-move-popover b")).toBeNull();
    expect(q(".vmp-title").textContent).toBe("<img src=x onerror=alert(1)>");
  });

  it("'Move to next' calls onMoveNext once and closes on success", async () => {
    open();
    q<HTMLButtonElement>(".vmp-next").click();
    q<HTMLButtonElement>(".vmp-next").click(); // a double click must not send twice
    await flush();
    expect(onMoveNext).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".version-move-popover")).toBeNull();
    expect(openVersionMovePopoverItemId()).toBeNull();
  });

  it("freezes the controls while the request runs", async () => {
    let finish!: () => void;
    onMoveNext.mockReturnValue(new Promise<void>((r) => (finish = r)));
    open();
    q<HTMLButtonElement>(".vmp-next").click();
    await flush();
    expect(q<HTMLButtonElement>(".vmp-next").disabled).toBe(true);
    expect(q<HTMLButtonElement>(".vmp-next").textContent).toBe("Working…");
    expect(q<HTMLInputElement>(".vmp-input").disabled).toBe(true);
    expect(document.querySelector(".version-move-popover")!.getAttribute("aria-busy")).toBe("true");
    finish();
    await flush();
    expect(document.querySelector(".version-move-popover")).toBeNull();
  });

  it("keeps the popover open and shows the reason when the request fails", async () => {
    const err: any = new Error("409: ...");
    err.responseText = JSON.stringify({ detail: "item was moved by someone else" });
    onMoveNext.mockRejectedValue(err);
    open();
    q<HTMLButtonElement>(".vmp-next").click();
    await flush();
    const msg = q(".vmp-error");
    expect(msg.hidden).toBe(false);
    expect(msg.textContent).toBe("item was moved by someone else");
    expect(msg.getAttribute("role")).toBe("alert");
    // Usable again, with its label restored.
    expect(q<HTMLButtonElement>(".vmp-next").disabled).toBe(false);
    expect(q<HTMLButtonElement>(".vmp-next").textContent).toBe("Move to v2.2 (next)");
    expect(document.activeElement).toBe(q(".vmp-next"));
  });

  it("steers to the explicit-version field when the server cannot derive a next version", async () => {
    const err: any = new Error("422");
    err.status = 422;
    err.responseText = JSON.stringify({ detail: { code: "next_version_unavailable", message: "cannot derive" } });
    onMoveNext.mockRejectedValue(err);
    open();
    q<HTMLButtonElement>(".vmp-next").click();
    await flush();
    expect(q(".vmp-error").textContent).toBe("cannot derive");
    expect(document.activeElement).toBe(q(".vmp-input"));
  });

  it("when no next version can be worked out, disables that button and asks instead of guessing", () => {
    open({ currentVersion: "current sprint v0.2" });
    const next = q<HTMLButtonElement>(".vmp-next");
    expect(next.disabled).toBe(true);
    expect(next.textContent).toBe("Move to next version");
    expect(q(".vmp-hint").textContent).toContain("No next version can be worked out");
    expect(document.activeElement).toBe(q(".vmp-input"));
    next.click();
    expect(onMoveNext).not.toHaveBeenCalled();
  });

  it("an item with no version at all is handled the same way", () => {
    open({ currentVersion: "" });
    expect(q<HTMLButtonElement>(".vmp-next").disabled).toBe(true);
    expect(q(".vmp-current").textContent).toContain("(no version)");
  });

  it("Enter in the field moves to the typed (trimmed) version", async () => {
    open();
    const input = q<HTMLInputElement>(".vmp-input");
    input.value = "  v3.0 ";
    const ev = key(input, "Enter");
    expect(ev.defaultPrevented).toBe(true);
    await flush();
    expect(onMoveSpecific).toHaveBeenCalledWith("v3.0");
    expect(onMoveNext).not.toHaveBeenCalled();
    expect(document.querySelector(".version-move-popover")).toBeNull();
  });

  it("the Move button does the same as Enter", async () => {
    open();
    q<HTMLInputElement>(".vmp-input").value = "v9";
    q<HTMLButtonElement>(".vmp-go").click();
    await flush();
    expect(onMoveSpecific).toHaveBeenCalledWith("v9");
  });

  it("refuses an empty, invalid or unchanged specific version without calling out", async () => {
    open();
    const input = q<HTMLInputElement>(".vmp-input");
    key(input, "Enter");
    expect(q(".vmp-error").textContent).toBe("Enter a version.");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    input.value = "v2​.5";
    key(input, "Enter");
    expect(q(".vmp-error").textContent).toContain("control or invisible");
    input.value = "v2.1";
    key(input, "Enter");
    expect(q(".vmp-error").textContent).toBe("It is already in v2.1.");
    await flush();
    expect(onMoveSpecific).not.toHaveBeenCalled();
    // Typing clears the message.
    input.value = "v2.";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(q(".vmp-error").hidden).toBe(true);
  });

  it("defer defaults to the next version and says so; a typed version wins", async () => {
    open();
    expect(q(".vmp-defer + .vmp-hint").textContent).toContain("pushed to v2.2");
    const input = q<HTMLInputElement>(".vmp-input");
    input.value = "v4.0";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(q(".vmp-defer + .vmp-hint").textContent).toContain("pushed to v4.0");
    q<HTMLButtonElement>(".vmp-defer").click();
    await flush();
    expect(onDefer).toHaveBeenCalledWith("v4.0");
    expect(onMoveNext).not.toHaveBeenCalled();
    expect(onMoveSpecific).not.toHaveBeenCalled();
  });

  it("defer without a derivable next version asks for one", async () => {
    open({ currentVersion: "current sprint v0.2" });
    q<HTMLButtonElement>(".vmp-defer").click();
    expect(q(".vmp-error").textContent).toBe("Type the version it is deferred to, above.");
    expect(onDefer).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(q(".vmp-input"));
  });

  it("Escape closes it and returns focus to the arrow button", () => {
    open();
    const ev = key(document.activeElement!, "Escape");
    expect(ev.defaultPrevented).toBe(true);
    expect(document.querySelector(".version-move-popover")).toBeNull();
    expect(document.activeElement).toBe(anchor);
  });

  it("returns focus to the item's arrow button even after a repaint replaced the clicked one", () => {
    anchor.dataset.act = "move-version";
    anchor.dataset.itemId = "item-1";
    open();
    // The board repaints: the clicked button is replaced by an identical new one.
    const replacement = document.createElement("button");
    replacement.dataset.act = "move-version";
    replacement.dataset.itemId = "item-1";
    anchor.replaceWith(replacement);
    key(document.activeElement!, "Escape");
    expect(document.activeElement).toBe(replacement);
  });

  it("the close button closes it", () => {
    open();
    q<HTMLButtonElement>(".vmp-close").click();
    expect(document.querySelector(".version-move-popover")).toBeNull();
  });

  it("clicking elsewhere closes it, but the anchor's own mousedown does not", () => {
    open();
    anchor.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(document.querySelector(".version-move-popover")).not.toBeNull();
    q(".vmp-title").dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(document.querySelector(".version-move-popover")).not.toBeNull();
    document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(document.querySelector(".version-move-popover")).toBeNull();
  });

  it("keeps Tab inside the popover (focus trap)", () => {
    open();
    const close = q<HTMLButtonElement>(".vmp-close");
    const defer = q<HTMLButtonElement>(".vmp-defer");
    defer.focus();
    const fwd = key(defer, "Tab");
    expect(fwd.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(close);
    const back = key(close, "Tab", { shiftKey: true });
    expect(back.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(defer);
    // In the middle Tab is left to the browser.
    q<HTMLButtonElement>(".vmp-next").focus();
    expect(key(q(".vmp-next"), "Tab").defaultPrevented).toBe(false);
  });

  it("only one popover exists at a time and the item id is exposed for toggling", () => {
    open();
    expect(openVersionMovePopoverItemId()).toBe("item-1");
    open({ itemId: "item-2" });
    expect(document.querySelectorAll(".version-move-popover")).toHaveLength(1);
    expect(openVersionMovePopoverItemId()).toBe("item-2");
    expect(closeVersionMovePopover()).toBe(true);
    expect(closeVersionMovePopover()).toBe(false);
  });

  it("reports a failure through onError when it was closed while still working", async () => {
    let fail!: (e: Error) => void;
    onMoveNext.mockReturnValue(new Promise((_r, rej) => (fail = rej)));
    open();
    q<HTMLButtonElement>(".vmp-next").click();
    await flush();
    q<HTMLButtonElement>(".vmp-close").click(); // still live while busy
    expect(document.querySelector(".version-move-popover")).toBeNull();
    fail(new Error("server went away"));
    await flush();
    expect(onError).toHaveBeenCalledWith("server went away");
  });

  it("positions itself inside the viewport", () => {
    open();
    const pop = document.querySelector<HTMLElement>(".version-move-popover")!;
    expect(pop.style.position).toBe("fixed");
    expect(parseInt(pop.style.left, 10)).toBeGreaterThanOrEqual(8);
    expect(parseInt(pop.style.top, 10)).toBeGreaterThanOrEqual(8);
  });
});

describe("flashMovedItem", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div class="sprint-item-row" data-item="a1"></div>
      <div class="queue-item" data-item-id="a1"></div>
      <div class="queue-item" data-item-id="b2"></div>`;
  });
  afterEach(() => vi.useRealTimers());

  it("highlights the item on the Live board and the Queue, then clears it", () => {
    vi.useFakeTimers();
    flashMovedItem("a1");
    const flashed = document.querySelectorAll(".sprint-row-moved");
    expect(flashed).toHaveLength(2);
    expect(document.querySelector('[data-item-id="b2"]')!.classList.contains("sprint-row-moved")).toBe(false);
    vi.advanceTimersByTime(2500);
    expect(document.querySelectorAll(".sprint-row-moved")).toHaveLength(0);
  });

  it("is a quiet no-op when the item is not on screen", () => {
    expect(() => flashMovedItem("nope")).not.toThrow();
    expect(document.querySelectorAll(".sprint-row-moved")).toHaveLength(0);
  });
});
