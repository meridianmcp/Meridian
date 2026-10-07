// Unit tests for the goal-field conflict handling (fc779141).
//
// The bug: refreshGoal() rewrote the north star / version goal / current focus
// textareas on every goal_updated push, so an agent's set_goal destroyed a
// person's unsaved draft, and the save that followed was last-write-wins.
// These tests drive the real GoalField controller (the same one dashboard.ts
// wires to the three fields) in jsdom and cover the conflict matrix:
// not editing / editing + remote arrives / keep mine / take theirs / save with
// a stale base (409) / Esc discard / unchanged text never prompts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  GoalField,
  _resetGoalFieldRegistry,
  describeRemoteChange,
  diffLines,
  getGoalField,
  guardGoalLeave,
  hasUnsavedGoalEdits,
  installGoalUnloadGuard,
  noteGoalEvent,
  normalizeGoalText,
  parseGoalConflict,
  registerGoalFields,
  splitGoalText,
  type GoalFieldConfig,
  type GoalFieldKey,
} from "./dashboard-goal-conflict";

// ---------------------------------------------------------------------------
// Rig
// ---------------------------------------------------------------------------

interface Rig {
  field: GoalField;
  ta: HTMLTextAreaElement;
  root: HTMLElement;
  persist: ReturnType<typeof vi.fn>;
  notify: ReturnType<typeof vi.fn>;
  confirm: ReturnType<typeof vi.fn>;
}

function rig(over: Partial<GoalFieldConfig> = {}): Rig {
  const root = document.createElement("div");
  const ta = document.createElement("textarea");
  root.append(ta);
  document.body.append(root);
  const persist = vi.fn(async (_text: string, _stamp: string | null) => ({ stamp: "S-saved" }));
  const notify = vi.fn();
  const confirm = vi.fn(() => true);
  const field = new GoalField({
    key: "north_star" as GoalFieldKey,
    label: "north star",
    el: ta,
    anchor: () => ta,
    getValue: () => ta.value,
    setValue: (t: string) => { ta.value = t; },
    persist,
    notify,
    confirm,
    ...over,
  });
  field.wire({ blur: [ta], input: [ta], keys: [ta] });
  return { field, ta, root, persist, notify, confirm };
}

/** What a person does: put text in the field and fire the input event. */
function type(r: Rig, text: string): void {
  r.ta.value = text;
  r.ta.dispatchEvent(new Event("input", { bubbles: true }));
}

const barOf = (r: Rig) => r.root.querySelector(".goal-conflict-bar") as HTMLElement | null;
const visibleBar = (r: Rig) => {
  const b = barOf(r);
  return b && !b.hidden ? b : null;
};
const click = (r: Rig, act: string) =>
  (barOf(r)!.querySelector(`button[data-act="${act}"]`) as HTMLButtonElement).click();
const flush = () => new Promise((res) => setTimeout(res, 0));

function conflict409(value: unknown, stamp: string) {
  return Object.assign(new Error("409"), {
    status: 409,
    responseText: JSON.stringify({
      detail: { error: "goal_conflict", field: "north_star", current: { value, updated_at: stamp } },
    }),
  });
}

/** A rig whose field was loaded with `base` at stamp T0, like refreshGoal's first call. */
function loaded(base = "base text", over: Partial<GoalFieldConfig> = {}): Rig {
  const r = rig(over);
  r.field.applyServer(base, "T0");
  return r;
}

beforeEach(() => {
  document.body.innerHTML = "";
  _resetGoalFieldRegistry();
});
afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The conflict matrix
// ---------------------------------------------------------------------------

describe("not editing", () => {
  it("takes the first server value into the editor", () => {
    const r = rig();
    expect(r.field.applyServer("hello", "T0")).toBe("applied");
    expect(r.ta.value).toBe("hello");
    expect(r.field.stamp).toBe("T0");
  });

  it("updates live when the server value changes, without a bar", () => {
    const r = loaded();
    expect(r.field.applyServer("server v2", "T1")).toBe("applied");
    expect(r.ta.value).toBe("server v2");
    expect(r.field.stamp).toBe("T1");
    expect(visibleBar(r)).toBeNull();
    expect(r.ta.classList.contains("dirty")).toBe(false);
  });

  it("keeps the caret where it was when a focused, untouched field updates", () => {
    const r = loaded("abcdef");
    r.ta.focus();
    r.ta.setSelectionRange(2, 2);
    r.field.applyServer("abcdefGHI", "T1");
    expect(r.ta.value).toBe("abcdefGHI");
    expect(r.ta.selectionStart).toBe(2);
  });

  it("does not treat a programmatic value change as an edit", () => {
    // The current-focus field's select/input syncer sets values without input events.
    const r = loaded();
    r.ta.value = "set by a syncer";
    expect(r.field.isDirty()).toBe(false);
    expect(r.field.applyServer("server v2", "T1")).toBe("applied");
  });
});

describe("editing and a remote change arrives", () => {
  it("keeps the draft and shows the Changed elsewhere bar", () => {
    const r = loaded();
    type(r, "my draft");
    expect(r.field.applyServer("server v2", "T1")).toBe("conflict");

    expect(r.ta.value).toBe("my draft"); // never overwritten
    const bar = visibleBar(r)!;
    expect(bar).not.toBeNull();
    expect(bar.textContent).toContain("Changed elsewhere");
    for (const act of ["keep", "take", "both"]) {
      expect((bar.querySelector(`button[data-act="${act}"]`) as HTMLButtonElement).hidden).toBe(false);
    }
    expect((bar.querySelector('button[data-act="retry"]') as HTMLButtonElement).hidden).toBe(true);
    expect(r.ta.classList.contains("dirty")).toBe(true);
    // The base the edit is compared with is still the OLD server text.
    expect(r.field.base).toBe("base text");
  });

  it("protects the draft through repeated remote updates and shows the latest", () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    r.field.applyServer("server v3", "T2");
    expect(r.ta.value).toBe("my draft");
    expect(r.field.pending?.text).toBe("server v3");
    expect(r.field.pending?.stamp).toBe("T2");
  });

  it("says who changed it and how long ago when the event told us", () => {
    const r = loaded();
    const twoMinAgo = new Date(Date.now() - 120_000).toISOString().slice(0, 19).replace("T", " ");
    r.field.noteRemoteMeta({ kind: "agent", source: "mcp", stamp: twoMinAgo });
    type(r, "my draft");
    r.field.applyServer("server v2", twoMinAgo);
    expect(visibleBar(r)!.textContent).toContain("by an agent, 2m ago");
  });

  it("does not attribute a change to an event that produced a different stamp", () => {
    const r = loaded();
    r.field.noteRemoteMeta({ kind: "agent", stamp: "some-other-stamp" });
    type(r, "my draft");
    r.field.applyServer("server v2", "2020-01-01 00:00:00");
    expect(visibleBar(r)!.textContent).not.toContain("agent");
  });

  it("stays quiet when the person's draft already equals the new server text", () => {
    const r = loaded();
    type(r, "same words");
    expect(r.field.applyServer("same words", "T1")).toBe("converged");
    expect(visibleBar(r)).toBeNull();
    expect(r.field.isDirty()).toBe(false);
    expect(r.field.stamp).toBe("T1");
  });

  it("adopts the new stamp when only something else moved (no false conflict next save)", async () => {
    const r = loaded();
    type(r, "my draft");
    // e.g. the AUTO BLOCKS zone changed: same editable text, newer stamp.
    expect(r.field.applyServer("base text", "T5")).toBe("unchanged");
    expect(visibleBar(r)).toBeNull();
    expect(r.ta.value).toBe("my draft");
    await r.field.saveNow();
    expect(r.persist).toHaveBeenCalledWith("my draft", "T5");
  });

  it("clears the bar when the server goes back to what the edit is based on", () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    expect(visibleBar(r)).not.toBeNull();
    r.field.applyServer("base text", "T2");
    expect(visibleBar(r)).toBeNull();
    expect(r.field.pending).toBeNull();
  });

  it("shows what the server has once the person edits their way back to the base", () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    type(r, "base text"); // nothing of theirs is left to protect
    expect(r.ta.value).toBe("server v2");
    expect(visibleBar(r)).toBeNull();
    expect(r.field.stamp).toBe("T1");
  });

  it("does not overwrite while a save is still in flight", async () => {
    const r = loaded();
    let finish!: (v: { stamp: string }) => void;
    r.persist.mockImplementationOnce(() => new Promise((res) => { finish = res; }));
    type(r, "my draft");
    const saving = r.field.saveNow();
    expect(r.field.applyServer("someone else", "T9")).toBe("conflict");
    expect(r.ta.value).toBe("my draft");
    finish({ stamp: "S1" });
    expect(await saving).toBe("saved");
  });
});

describe("Keep mine", () => {
  it("saves the draft over the remote change, based on the remote stamp", async () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    click(r, "keep");
    await flush();
    expect(r.persist).toHaveBeenCalledTimes(1);
    expect(r.persist).toHaveBeenCalledWith("my draft", "T1");
    expect(r.ta.value).toBe("my draft");
    expect(visibleBar(r)).toBeNull();
    expect(r.field.stamp).toBe("S-saved");
    expect(r.field.isDirty()).toBe(false);
  });

  it("leaves everything as it was when the north-star confirmation is declined", async () => {
    const r = loaded("base text", { confirmSave: () => false });
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    click(r, "keep");
    await flush();
    expect(r.persist).not.toHaveBeenCalled();
    expect(r.ta.value).toBe("my draft");
    expect(visibleBar(r)).not.toBeNull(); // still waiting for a decision
    expect(r.field.pending?.text).toBe("server v2");
  });

  it("shows the bar again when a third change lands before the save", async () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    r.persist.mockRejectedValueOnce(conflict409("server v3", "T2"));
    click(r, "keep");
    await flush();
    expect(r.persist).toHaveBeenCalledTimes(1);
    expect(r.ta.value).toBe("my draft");
    expect(r.field.pending?.text).toBe("server v3");
    expect(visibleBar(r)).not.toBeNull();
  });
});

describe("Take theirs", () => {
  it("replaces the draft with the server text and does not save", () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    click(r, "take");
    expect(r.ta.value).toBe("server v2");
    expect(visibleBar(r)).toBeNull();
    expect(r.field.isDirty()).toBe(false);
    expect(r.field.stamp).toBe("T1");
    expect(r.persist).not.toHaveBeenCalled();
  });
});

describe("See both", () => {
  it("shows a read-only line diff of the two texts and toggles", () => {
    const r = loaded("line one\nline two");
    type(r, "line one\nmy line");
    r.field.applyServer("line one\ntheir line", "T1");
    click(r, "both");
    const diff = barOf(r)!.querySelector(".goal-conflict-diff") as HTMLElement;
    expect(diff.hidden).toBe(false);
    expect(diff.querySelector(".gcd-same .gcd-text")!.textContent).toBe("line one");
    expect(diff.querySelector(".gcd-mine .gcd-text")!.textContent).toBe("my line");
    expect(diff.querySelector(".gcd-theirs .gcd-text")!.textContent).toBe("their line");
    expect((barOf(r)!.querySelector('button[data-act="both"]') as HTMLElement).getAttribute("aria-expanded")).toBe("true");
    click(r, "both");
    expect(diff.hidden).toBe(true);
    // Looking at the diff changes nothing about the draft.
    expect(r.ta.value).toBe("line one\nmy line");
  });

  it("renders text as text, never as markup", () => {
    const r = loaded();
    type(r, "<img src=x onerror=alert(1)>");
    r.field.applyServer("<b>theirs</b>", "T1");
    click(r, "both");
    expect(barOf(r)!.querySelector("img")).toBeNull();
    expect(barOf(r)!.querySelector("b")).toBeNull();
    expect(barOf(r)!.textContent).toContain("<b>theirs</b>");
  });
});

describe("saving with a stale base (409)", () => {
  it("keeps the draft and shows the bar with the current server value", async () => {
    const r = loaded();
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(conflict409("their newer text", "T2"));
    expect(await r.field.saveNow()).toBe("conflict");
    expect(r.persist).toHaveBeenCalledWith("my draft", "T0");
    expect(r.ta.value).toBe("my draft");
    expect(visibleBar(r)!.textContent).toContain("Changed elsewhere");
    expect(r.field.pending?.text).toBe("their newer text");
    expect(r.field.pending?.stamp).toBe("T2");
    expect(r.notify).not.toHaveBeenCalledWith(expect.stringContaining("save failed"), true);
  });

  it("then Take theirs shows the 409's value and Keep mine saves on its stamp", async () => {
    const r = loaded();
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(conflict409("their newer text", "T2"));
    await r.field.saveNow();
    click(r, "keep");
    await flush();
    expect(r.persist).toHaveBeenLastCalledWith("my draft", "T2");

    const r2 = loaded();
    type(r2, "my draft");
    r2.persist.mockRejectedValueOnce(conflict409("their newer text", "T2"));
    await r2.field.saveNow();
    click(r2, "take");
    expect(r2.ta.value).toBe("their newer text");
  });

  it("re-saves silently when the 409 only reflects a read-only zone moving", async () => {
    const r = loaded();
    type(r, "my draft");
    // The server's stored value for the editable zone is what the edit is based on.
    r.persist.mockRejectedValueOnce(conflict409("base text", "T3"));
    expect(await r.field.saveNow()).toBe("saved");
    expect(r.persist).toHaveBeenCalledTimes(2);
    expect(r.persist).toHaveBeenLastCalledWith("my draft", "T3");
    expect(visibleBar(r)).toBeNull();
  });

  it("counts a 409 whose value equals the draft as already saved", async () => {
    const r = loaded();
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(conflict409("my draft", "T4"));
    expect(await r.field.saveNow()).toBe("saved");
    expect(r.persist).toHaveBeenCalledTimes(1);
    expect(r.field.isDirty()).toBe(false);
  });

  it("maps the 409 value through fromServer (version goal zones)", async () => {
    const r = loaded("CURRENT FOCUS\nold", {
      key: "version_goal",
      fromServer: (v) => splitGoalText(String(v)).editable,
    });
    type(r, "CURRENT FOCUS\nmine");
    r.persist.mockRejectedValueOnce(conflict409("v2 — sprint\nCURRENT FOCUS\ntheirs\n--- AUTO BLOCKS BELOW ---\nlog", "T2"));
    expect(await r.field.saveNow()).toBe("conflict");
    expect(r.field.pending?.text).toBe("CURRENT FOCUS\ntheirs");
  });
});

describe("a failed save (network error, 5xx)", () => {
  it("keeps the text, shows the error on the field and can retry", async () => {
    const r = loaded();
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(new Error("Failed to fetch"));
    expect(await r.field.saveNow()).toBe("error");

    expect(r.ta.value).toBe("my draft");
    expect(r.ta.classList.contains("dirty")).toBe(true);
    expect(r.ta.classList.contains("save-failed")).toBe(true);
    const bar = visibleBar(r)!;
    expect(bar.classList.contains("is-error")).toBe(true);
    expect(bar.textContent).toContain("Save failed");
    expect(bar.textContent).toContain("Failed to fetch");
    expect(bar.textContent).toContain("Your text is kept");
    expect(r.notify).toHaveBeenCalledWith("save failed: Failed to fetch", true);
    expect((bar.querySelector('button[data-act="keep"]') as HTMLButtonElement).hidden).toBe(true);

    click(r, "retry");
    await flush();
    expect(r.persist).toHaveBeenCalledTimes(2);
    expect(visibleBar(r)).toBeNull();
    expect(r.ta.classList.contains("save-failed")).toBe(false);
  });

  it("words the demo's read-only refusal for a person", async () => {
    const r = loaded();
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(new Error("demo_readonly"));
    await r.field.saveNow();
    expect(visibleBar(r)!.textContent).toContain("this demo is read-only");
  });

  it("prefers the server's own message from the response body", async () => {
    const r = loaded();
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(Object.assign(new Error("500: x"), {
      status: 500,
      responseText: JSON.stringify({ detail: "input too large" }),
    }));
    await r.field.saveNow();
    expect(visibleBar(r)!.textContent).toContain("input too large");
  });
});

describe("Esc discards an unsaved edit", () => {
  const esc = (r: Rig) => {
    const ev = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    r.ta.dispatchEvent(ev);
    return ev;
  };

  it("asks, and reverts to the last saved value when confirmed", () => {
    const r = loaded();
    type(r, "my draft");
    const ev = esc(r);
    expect(r.confirm).toHaveBeenCalledTimes(1);
    expect(r.confirm.mock.calls[0][0]).toContain("north star");
    expect(r.ta.value).toBe("base text");
    expect(r.field.isDirty()).toBe(false);
    expect(ev.defaultPrevented).toBe(true);
  });

  it("keeps the draft when the person declines", () => {
    const r = loaded();
    type(r, "my draft");
    r.confirm.mockReturnValueOnce(false);
    esc(r);
    expect(r.ta.value).toBe("my draft");
    expect(r.field.isDirty()).toBe(true);
  });

  it("never prompts when the text did not actually change", () => {
    const r = loaded();
    const ev = esc(r);
    expect(r.confirm).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(false);

    // Typing and then typing it back is not a change either.
    type(r, "base text plus");
    type(r, "base text");
    esc(r);
    expect(r.confirm).not.toHaveBeenCalled();
  });

  it("with a remote change waiting, discarding shows the current server text", () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    esc(r);
    expect(r.ta.value).toBe("server v2");
    expect(visibleBar(r)).toBeNull();
    expect(r.field.stamp).toBe("T1");
  });
});

describe("saving", () => {
  it("never saves text that did not change", async () => {
    const r = loaded();
    r.ta.dispatchEvent(new Event("blur"));
    await flush();
    expect(r.persist).not.toHaveBeenCalled();
    type(r, "base text");
    r.ta.dispatchEvent(new Event("blur"));
    await flush();
    expect(r.persist).not.toHaveBeenCalled();
  });

  it("saves an edit on blur with the stamp it is based on", async () => {
    const r = loaded();
    type(r, "my draft");
    r.ta.dispatchEvent(new Event("blur"));
    await flush();
    expect(r.persist).toHaveBeenCalledWith("my draft", "T0");
    expect(r.field.base).toBe("my draft");
    expect(r.field.stamp).toBe("S-saved");
    expect(r.field.isDirty()).toBe(false);
  });

  it("does not write over an unresolved Changed elsewhere prompt", async () => {
    const r = loaded();
    type(r, "my draft");
    r.field.applyServer("server v2", "T1");
    r.ta.dispatchEvent(new Event("blur"));
    await flush();
    expect(r.persist).not.toHaveBeenCalled();
    expect(r.notify).not.toHaveBeenCalled(); // a blur is silent
    expect(await r.field.saveNow({ explicit: true })).toBe("blocked");
    expect(r.notify).toHaveBeenCalledWith(expect.stringContaining("Resolve"), true);
    expect(r.persist).not.toHaveBeenCalled();
  });

  it("an explicit save with nothing to save says so instead of posting", async () => {
    const r = loaded();
    expect(await r.field.saveNow({ explicit: true })).toBe("skipped");
    expect(r.notify).toHaveBeenCalledWith("No changes to save");
    expect(r.persist).not.toHaveBeenCalled();
  });

  it("stays silent when the Save button click follows the blur-save that just ran", async () => {
    const r = loaded();
    type(r, "my draft");
    r.ta.dispatchEvent(new Event("blur")); // clicking Save blurs the field first
    await flush();
    expect(r.persist).toHaveBeenCalledTimes(1);
    expect(await r.field.saveNow({ explicit: true })).toBe("skipped");
    expect(r.notify).not.toHaveBeenCalled(); // would replace the "saved" toast
    expect(r.persist).toHaveBeenCalledTimes(1);
  });

  it("an explicit save sends a value picked without an input event", async () => {
    const r = loaded();
    r.ta.value = "picked in the select"; // no input event, like choosing a session
    expect(await r.field.saveNow({ explicit: true })).toBe("saved");
    expect(r.persist).toHaveBeenCalledWith("picked in the select", "T0");
  });

  it("skips an empty value unless the field allows it", async () => {
    const r = loaded();
    type(r, "   ");
    expect(await r.field.saveNow({ explicit: true })).toBe("skipped");
    const v = loaded("something", { key: "version_goal", allowEmpty: true });
    type(v, "");
    expect(await v.field.saveNow()).toBe("saved");
  });

  it("reverts to the saved text when the north-star confirmation is declined", async () => {
    const r = loaded("the vision", { confirmSave: () => false });
    type(r, "a different vision");
    expect(await r.field.saveNow()).toBe("skipped");
    expect(r.ta.value).toBe("the vision");
    expect(r.persist).not.toHaveBeenCalled();
  });

  it("shares one in-flight save between callers", async () => {
    const r = loaded();
    let finish!: (v: { stamp: string }) => void;
    r.persist.mockImplementationOnce(() => new Promise((res) => { finish = res; }));
    type(r, "my draft");
    const a = r.field.saveNow();
    const b = r.field.saveNow();
    finish({ stamp: "S1" });
    expect(await a).toBe("saved");
    expect(await b).toBe("saved");
    expect(r.persist).toHaveBeenCalledTimes(1);
  });

  it("sends no stamp when the server never gave one (older server: last-write-wins)", async () => {
    const r = rig();
    r.field.applyServer("base text", null);
    type(r, "my draft");
    await r.field.saveNow();
    expect(r.persist).toHaveBeenCalledWith("my draft", null);
  });
});

describe("comparison ignores what a person did not mean to change", () => {
  it("treats CRLF and trailing whitespace as the same text", () => {
    const r = loaded("line one\r\nline two  \n", { key: "version_goal" });
    r.ta.value = "line one\nline two"; // what a textarea reports
    expect(r.field.isDirty(true)).toBe(false);
    expect(r.field.applyServer("line one\nline two", "T1")).toBe("unchanged");
  });

  it("north star and current focus compare trimmed", () => {
    const r = loaded("  vision ");
    type(r, "vision");
    expect(r.field.isDirty()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Leaving the tab / page with unsaved edits
// ---------------------------------------------------------------------------

describe("unsaved edits when leaving", () => {
  function registered(...rigs: Array<[GoalFieldKey, Rig]>) {
    registerGoalFields("p1", Object.fromEntries(rigs.map(([k, r]) => [k, r.field])));
  }

  it("beforeunload is cancelled only while something is unsaved", () => {
    installGoalUnloadGuard();
    const r = loaded();
    registered(["north_star", r]);

    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);

    type(r, "my draft");
    expect(hasUnsavedGoalEdits("p1")).toBe(true);
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });

  it("a field whose tab was closed no longer holds the page hostage", () => {
    installGoalUnloadGuard();
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    r.root.remove(); // tab torn down
    expect(hasUnsavedGoalEdits()).toBe(false);
    const ev = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
  });

  it("forgets the fields of a closed project tab", () => {
    const gone = loaded();
    registered(["north_star", gone]);
    gone.root.remove();
    registerGoalFields("p2", { north_star: loaded().field });
    expect(getGoalField("p1", "north_star")).toBe(gone.field); // lookups are unaffected until a walk prunes
    expect(hasUnsavedGoalEdits()).toBe(false); // any registry walk prunes
    expect(getGoalField("p1", "north_star")).toBeNull();
    expect(getGoalField("p2", "north_star")).not.toBeNull();
  });

  it("lets a clean tab switch straight through", () => {
    const r = loaded();
    registered(["north_star", r]);
    const proceed = vi.fn();
    expect(guardGoalLeave("p1", proceed)).toBe(false);
    expect(proceed).not.toHaveBeenCalled();
  });

  const dialog = () => document.querySelector(".goal-leave-dialog") as HTMLElement | null;
  const choose = (c: string) => (document.querySelector(`.goal-leave-dialog button[data-choice="${c}"]`) as HTMLButtonElement).click();

  it("asks Save / Discard / Stay and Discard reverts the field and proceeds", async () => {
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    const proceed = vi.fn();
    expect(guardGoalLeave("p1", proceed)).toBe(true);
    await flush();
    expect(dialog()).not.toBeNull();
    expect(dialog()!.textContent).toContain("north star");
    for (const c of ["save", "discard", "stay"]) expect(document.querySelector(`button[data-choice="${c}"]`)).not.toBeNull();
    choose("discard");
    await flush();
    expect(r.ta.value).toBe("base text");
    expect(proceed).toHaveBeenCalledTimes(1);
    expect(dialog()).toBeNull();
  });

  it("Stay here keeps the page and the draft", async () => {
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    const proceed = vi.fn();
    guardGoalLeave("p1", proceed);
    await flush();
    choose("stay");
    await flush();
    expect(proceed).not.toHaveBeenCalled();
    expect(r.ta.value).toBe("my draft");
  });

  it("Save saves, then proceeds", async () => {
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    const proceed = vi.fn();
    guardGoalLeave("p1", proceed);
    await flush();
    choose("save");
    await flush();
    expect(r.persist).toHaveBeenCalledWith("my draft", "T0");
    expect(proceed).toHaveBeenCalledTimes(1);
  });

  it("Save that fails does not proceed, and the error stays on the field", async () => {
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    r.persist.mockRejectedValueOnce(new Error("offline"));
    const proceed = vi.fn();
    guardGoalLeave("p1", proceed);
    await flush();
    choose("save");
    await flush();
    expect(proceed).not.toHaveBeenCalled();
    expect(visibleBar(r)!.textContent).toContain("offline");
  });

  it("waits for the blur-save that is already running, and does not prompt if it succeeds", async () => {
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    let finish!: (v: { stamp: string }) => void;
    r.persist.mockImplementationOnce(() => new Promise((res) => { finish = res; }));
    void r.field.saveNow(); // what the blur of the clicked tab button started
    const proceed = vi.fn();
    expect(guardGoalLeave("p1", proceed)).toBe(true);
    await flush();
    expect(proceed).not.toHaveBeenCalled();
    finish({ stamp: "S1" });
    await flush();
    expect(dialog()).toBeNull();
    expect(proceed).toHaveBeenCalledTimes(1);
  });

  it("Esc in the prompt means stay", async () => {
    const r = loaded();
    registered(["north_star", r]);
    type(r, "my draft");
    const proceed = vi.fn();
    guardGoalLeave("p1", proceed);
    await flush();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await flush();
    expect(dialog()).toBeNull();
    expect(proceed).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// goal_updated events
// ---------------------------------------------------------------------------

describe("noteGoalEvent", () => {
  it("hands who-changed-what to the fields the event names", () => {
    const ns = loaded();
    const sp = loaded("focus", { key: "sprint" });
    registerGoalFields("p1", { north_star: ns.field, sprint: sp.field });
    noteGoalEvent("p1", {
      type: "goal_updated",
      changed_fields: ["north_star"],
      changed_by: { kind: "agent", source: "mcp", id: null },
      field_updated_at: { north_star: "2020-01-01 00:00:00", sprint: "x" },
    });
    type(ns, "my draft");
    ns.field.applyServer("theirs", "2020-01-01 00:00:00");
    expect(visibleBar(ns)!.textContent).toContain("by an agent");

    type(sp, "my focus");
    sp.field.applyServer("their focus", "x");
    expect(visibleBar(sp)!.textContent).not.toContain("agent"); // sprint was not in changed_fields
  });

  it("ignores an event from an older server that names no fields", () => {
    const ns = loaded();
    registerGoalFields("p1", { north_star: ns.field });
    expect(() => noteGoalEvent("p1", { type: "goal_updated", version: 3 })).not.toThrow();
    expect(getGoalField("p1", "north_star")).toBe(ns.field);
  });
});

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("normalizeGoalText", () => {
  it("normalises line breaks; the version goal keeps leading space, loses trailing", () => {
    expect(normalizeGoalText("version_goal", "  a\r\nb \n\n")).toBe("  a\nb");
    expect(normalizeGoalText("north_star", "  a\r\nb \n\n")).toBe("a\nb");
    expect(normalizeGoalText("sprint", null)).toBe("");
  });
});

describe("splitGoalText", () => {
  it("splits title, SHIPPED, the editable zone and AUTO BLOCKS", () => {
    const parts = splitGoalText(
      "v2.3 — auth sprint\nSHIPPED\n- login\nCURRENT FOCUS\nbilling\n\n--- AUTO BLOCKS BELOW ---\nsession log",
    );
    expect(parts.titleLine).toBe("v2.3 — auth sprint");
    expect(parts.shipped).toBe("SHIPPED\n- login");
    expect(parts.editable).toBe("CURRENT FOCUS\nbilling");
    expect(parts.autoBlocks).toBe("session log");
  });

  it("puts everything in the editable zone when there is no version label", () => {
    const parts = splitGoalText("Just a free-form goal\nsecond line");
    expect(parts.titleLine).toBe("");
    expect(parts.shipped).toBe("");
    expect(parts.editable).toBe("Just a free-form goal\nsecond line");
    expect(parts.autoBlocks).toBeNull();
  });

  it("an empty goal is an editable empty string", () => {
    expect(splitGoalText("")).toEqual({ titleLine: "", shipped: "", editable: "", autoBlocks: null });
  });

  it("starting at CURRENT FOCUS leaves no SHIPPED zone", () => {
    const parts = splitGoalText("v1.0.0\nCURRENT FOCUS\nship it");
    expect(parts.shipped).toBe("");
    expect(parts.editable).toBe("CURRENT FOCUS\nship it");
  });
});

describe("diffLines", () => {
  it("marks common, own-only and server-only lines in order", () => {
    expect(diffLines("a\nb\nc", "a\nx\nc")).toEqual([
      { type: "same", text: "a" },
      { type: "mine", text: "b" },
      { type: "theirs", text: "x" },
      { type: "same", text: "c" },
    ]);
  });

  it("handles pure additions, pure removals and identical text", () => {
    expect(diffLines("a", "a\nb").map((o) => o.type)).toEqual(["same", "theirs"]);
    expect(diffLines("a\nb", "a").map((o) => o.type)).toEqual(["same", "mine"]);
    expect(diffLines("a", "a").map((o) => o.type)).toEqual(["same"]);
  });

  it("falls back to a whole-block comparison for absurdly long texts", () => {
    const big = Array.from({ length: 1200 }, (_, i) => `l${i}`).join("\n");
    const ops = diffLines(big, "short");
    expect(ops.filter((o) => o.type === "mine")).toHaveLength(1200);
    expect(ops.filter((o) => o.type === "theirs")).toHaveLength(1);
  });
});

describe("describeRemoteChange", () => {
  it("combines who and when, either alone, or nothing", () => {
    const ago = new Date(Date.now() - 5 * 60_000).toISOString().slice(0, 19).replace("T", " ");
    expect(describeRemoteChange({ kind: "agent" }, ago)).toBe("by an agent, 5m ago");
    expect(describeRemoteChange({ kind: "human" }, ago)).toBe("by a person, 5m ago");
    expect(describeRemoteChange({ kind: "unknown" }, ago)).toBe("5m ago");
    expect(describeRemoteChange({ kind: "agent" }, null)).toBe("by an agent");
    expect(describeRemoteChange(null, null)).toBe("");
  });
});

describe("parseGoalConflict", () => {
  it("reads the current value and stamp from a 409 goal_conflict body", () => {
    expect(parseGoalConflict(conflict409("v", "T9"))).toEqual({ value: "v", stamp: "T9" });
  });

  it("returns null for anything else", () => {
    expect(parseGoalConflict(new Error("x"))).toBeNull();
    expect(parseGoalConflict({ status: 500, responseText: "{}" })).toBeNull();
    expect(parseGoalConflict({ status: 409, responseText: "not json" })).toBeNull();
    expect(parseGoalConflict({ status: 409, responseText: JSON.stringify({ detail: "other conflict" }) })).toBeNull();
    expect(parseGoalConflict(null)).toBeNull();
  });
});
