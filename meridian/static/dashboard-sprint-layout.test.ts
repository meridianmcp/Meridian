// Layout contract for the Live tab's "Sprint progress" rows.
//
// The bug: for a long title, .sprint-item-title (an INLINE <span> inside a plain
// <div>, so its flex / overflow / text-overflow rules did nothing while
// white-space:nowrap stopped it wrapping) ran straight over .sprint-item-ver and
// .sprint-item-actions. The fix makes the title a block-level, wrapping,
// shrinkable box inside its own column, lets the row wrap so the buttons can
// drop UNDER the text, and baseline-aligns the icon / version / buttons with the
// title's first line.
//
// jsdom has no layout engine, so these tests render the REAL renderSprintProgress
// and inject the REAL dashboard.css, then assert (through getComputedStyle and the
// parsed CSSOM) the properties that GUARANTEE no overlap. Every test below includes
// at least one assertion that the pre-fix code violates.
//
// Limits, and how they are covered: jsdom resolves getComputedStyle by SOURCE ORDER
// only (no specificity) and ignores @media, so the property checks alone cannot see a
// rule that wins on specificity or applies only on a phone. The last describe block
// therefore scans every rule in the stylesheet (all @media, any specificity) for a
// declaration that could undo the contract, and the PIXEL layout itself is measured in
// a real browser by tests/test_demo_ux.py (test_sprint_rows_never_overlap_in_a_real_browser).
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
// renderSprintProgress calls the ambient global escapeHtml (bundled app-wide by
// esbuild at runtime). Importing dashboard-utils for its side effect registers it
// on window exactly like the real bundle does.
import "./dashboard-utils";
import { renderSprintProgress } from "./dashboard-sprint";

const PID = "P";
const LONG =
  "SECURITY (reproduced, urgent): cross-tenant artifact export and purge by a caller-supplied project_id (RT-TI-001, RT-TI-002)";
const TOKEN = "x".repeat(300); // a 300-character unbreakable token
const LONG_RESOURCE = "file:" + "very/long/path/".repeat(12) + "x.py";
const LONG_VERSION = "v1.2.3-" + "x".repeat(40);
// (tests/test_demo_ux.py renders the same fixture in a real browser: keep the two in sync)

const mk = (over: Record<string, unknown>) => ({ version: "v1", status: "pending", ...over });
const ITEMS = [
  // one board row per status, long / unbreakable titles
  mk({
    id: "pending", title: LONG, status: "pending",
    notes: "Notes with an unbreakable token " + "N".repeat(200) + " and some ordinary words.",
    touches_resources: JSON.stringify(["file:meridian/static/dashboard.css", "note:" + "n".repeat(120), "decision:abcdef12", LONG_RESOURCE]),
  }),
  mk({ id: "todo", title: TOKEN, status: "todo" }),
  mk({ id: "in_progress", title: LONG, status: "in_progress", claimed_at: "2026-01-01T00:00:00Z", stall_count: 2 }),
  mk({ id: "retried", title: LONG, status: "pending", claimed_at: "2026-01-01T00:00:00Z" }),
  mk({ id: "done", title: TOKEN, status: "done" }),
  mk({ id: "failed", title: LONG, status: "failed" }),
  mk({ id: "skipped", title: LONG, status: "skipped" }),
  mk({ id: "pushed_board", title: LONG, status: "pushed", pushed_to: "v2" }),
  mk({ id: "short", title: "Short title", status: "pending" }),
  // children (nested rows) + a parent that shows the [done/total] badge
  mk({ id: "parent", title: LONG, status: "in_progress" }),
  mk({
    id: "kid1", parent_id: "parent", title: TOKEN, status: "pending",
    notes: "kid notes " + "K".repeat(150), touches_resources: JSON.stringify([LONG_RESOURCE]),
  }),
  mk({ id: "kid2", parent_id: "parent", title: "short child", status: "done" }),
  // indeterminate (board row with the amber badge + the "Needs attention" row)
  mk({ id: "ind", title: LONG, status: "indeterminate" }),
  // human-assigned ("Your tasks" row)
  mk({ id: "human", title: LONG, status: "pending", milestone_type: "human" }),
  // backburner-only (no active peers in its version)
  mk({ id: "bb", title: TOKEN, version: "v9", status: "pushed", pushed_to: "v10" }),
  // a very long version label must wrap inside the row, not push the buttons out
  mk({ id: "longver", title: LONG, version: LONG_VERSION }),
  mk({ id: "longver_attn", title: TOKEN, version: LONG_VERSION, status: "indeterminate" }),
];

type Kind = "board" | "attention" | "human" | "backburner";
const KINDS: Kind[] = ["board", "attention", "human", "backburner"];

function kindOf(row: HTMLElement): Kind {
  const det = row.closest("details");
  if (det) {
    const summary = Array.from(det.children).find((c) => c.tagName === "SUMMARY");
    if (summary && (summary.textContent || "").includes("Backburner")) return "backburner";
  }
  const box = row.parentElement as HTMLElement;
  if (box.id.startsWith("live-sprint-progress")) return "board";
  const first = box.firstElementChild as HTMLElement | null;
  if (first && !first.classList.contains("sprint-item-row")) {
    const head = first.textContent || "";
    if (head.includes("Needs attention")) return "attention";
    if (head.includes("Your tasks")) return "human";
  }
  return "board"; // nested subtask rows
}

const allRows = (): HTMLElement[] => Array.from(document.querySelectorAll<HTMLElement>(".sprint-item-row"));
const rowsOf = (kind: Kind) => allRows().filter((r) => kindOf(r) === kind);
/** Computed value of one CSS property (kebab-case, as written in the stylesheet). */
const v = (el: Element, prop: string) => getComputedStyle(el).getPropertyValue(prop);
/** The element that is the row's text column: the wrapper div in board rows, the title itself otherwise. */
const columnOf = (row: HTMLElement) => {
  const title = row.querySelector(".sprint-item-title") as HTMLElement;
  return title.parentElement === row ? title : (title.parentElement as HTMLElement);
};

beforeAll(() => {
  // Same cascade the dashboard gets: the REAL stylesheet text.
  const css = readFileSync(resolve(process.cwd(), "meridian/static/dashboard.css"), "utf8");
  const style = document.createElement("style");
  style.id = "real-dashboard-css";
  style.textContent = css;
  document.head.appendChild(style);
});

// The board's real ancestor chain (dashboard.ts buildTabBody: .app > main > .tab-bodies >
// .tab-body > .vtab-drawer > .drawer-panel > .live-body > .live-section > the board root).
// Without it a selector such as `.live-body .sprint-item-title` or `.tab-body span` could not
// match anything, so a rule that wins on specificity through an ancestor would go unnoticed.
const BOARD_OPEN =
  `<div class="app"><main class="main"><div id="tab-bodies" class="tab-bodies"><div id="tab-body-${PID}" class="tab-body active">` +
  `<div id="drawer-${PID}" class="vtab-drawer open"><div id="drawer-live-${PID}" class="drawer-panel active">` +
  `<div id="live-body-${PID}" class="live-body"><div class="live-section">`;
const BOARD_CLOSE = `</div></div></div></div></div></div></main></div>`;

beforeEach(() => {
  // renderSprintProgress wires the add-input through these ambient globals.
  (globalThis as any).wireSprintAddEnter = () => {};
  (globalThis as any).addSprintItemFromInput = () => {};
  document.body.innerHTML = `${BOARD_OPEN}<div id="live-sprint-progress-${PID}" class="live-sprint-progress"></div>${BOARD_CLOSE}`;
  renderSprintProgress(PID, JSON.parse(JSON.stringify(ITEMS)));
});

describe("sprint row layout contract: title box", () => {
  it.each(KINDS)("%s rows: the title is a block-level, wrapping, shrinkable box that never truncates", (kind) => {
    const rows = rowsOf(kind);
    expect(rows.length, `fixture must render at least one ${kind} row`).toBeGreaterThan(0);
    for (const row of rows) {
      const title = row.querySelector(".sprint-item-title") as HTMLElement;
      const id = row.dataset.item;
      // block-level (an inline span ignores flex/overflow/width: the original bug)
      expect(["block", "flex", "grid", "inline-block", "flow-root", "list-item"], `${kind}/${id} title display`)
        .toContain(v(title, "display") || "inline");
      // allowed to wrap
      expect(["normal", "pre-wrap", "pre-line", "break-spaces"], `${kind}/${id} title white-space`)
        .toContain(v(title, "white-space") || "normal");
      // a 300-char unbreakable token must still break inside the column
      expect(["anywhere", "break-word"], `${kind}/${id} title overflow-wrap`).toContain(v(title, "overflow-wrap"));
      // can shrink below its content width
      expect(["0", "0px"], `${kind}/${id} title min-width`).toContain(v(title, "min-width"));
      // wraps instead of hiding text behind an ellipsis
      expect(v(title, "text-overflow"), `${kind}/${id} title text-overflow`).not.toBe("ellipsis");
      expect(v(title, "overflow"), `${kind}/${id} title overflow`).not.toBe("hidden");
    }
  });
});

describe("sprint row layout contract: the row reflows around the title", () => {
  it.each(KINDS)("%s rows: wrap + baseline-align, version and actions never shrink or escape", (kind) => {
    const rows = rowsOf(kind);
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      const id = `${kind}/${row.dataset.item}`;
      // the row wraps so the buttons can drop under the text
      expect(v(row, "flex-wrap"), `${id} row flex-wrap`).toBe("wrap");
      // icon, version and buttons stay on the title's FIRST line
      expect(v(row, "align-items"), `${id} row align-items`).toBe("baseline");
      for (const child of Array.from(row.children)) {
        const sf = v(child, "align-self");
        expect(["", "auto", "baseline"], `${id} child ${child.className} align-self`).toContain(sf);
      }
      // the text column takes the free space and may shrink to nothing
      const col = columnOf(row);
      expect(v(col, "flex-grow"), `${id} column flex-grow`).toBe("1");
      expect(["0", "0px"], `${id} column min-width`).toContain(v(col, "min-width"));
      // version label: never squeezed, never wider than the row
      const ver = row.querySelector(":scope > .sprint-item-ver") as HTMLElement | null;
      expect(ver, `${id} has a version label`).not.toBeNull();
      expect(v(ver!, "flex-shrink"), `${id} ver flex-shrink`).toBe("0");
      expect(v(ver!, "max-width"), `${id} ver max-width`).toBe("100%");
      // actions: never squeezed, wrap internally rather than escape the row
      // (backburner rows have no .sprint-item-actions wrapper: their only control is a bare edit button)
      const actions = row.querySelector(":scope > .sprint-item-actions") as HTMLElement | null;
      if (kind === "backburner") {
        expect(actions, `${id} backburner rows keep their bare edit button`).toBeNull();
        expect(row.querySelector(":scope > button.sprint-btn"), `${id} edit button`).not.toBeNull();
        const badge = Array.from(row.children).find((c) => (c.textContent || "").startsWith("v") && !c.classList.contains("sprint-item-ver") && c.tagName === "SPAN" && !c.className) as HTMLElement | undefined;
        if (row.dataset.item === "pushed_board" || row.dataset.item === "bb") {
          expect(badge, `${id} pushed-to badge`).toBeDefined();
          expect(v(badge!, "max-width"), `${id} pushed-to badge max-width`).toBe("100%");
          expect(v(badge!, "overflow-wrap"), `${id} pushed-to badge overflow-wrap`).toBe("anywhere");
        }
      } else {
        expect(actions, `${id} has an actions container`).not.toBeNull();
      }
      if (actions && actions.childElementCount > 0) {
        const a = actions;
        expect(v(a, "flex-shrink"), `${id} actions flex-shrink`).toBe("0");
        expect(v(a, "flex-wrap"), `${id} actions flex-wrap`).toBe("wrap");
        expect(v(a, "max-width"), `${id} actions max-width`).toBe("100%");
        expect(v(a, "margin-left"), `${id} actions margin-left`).toBe("auto");
      }
    }
  });

  it("covers every status icon on the board and keeps the hooks other code queries", () => {
    const icons = new Set(rowsOf("board").map((r) => (r.querySelector(".sprint-item-icon") as HTMLElement).textContent));
    for (const glyph of ["○", "◑", "●", "✕", "—", "→", "⚠"]) expect(icons, `status glyph ${glyph}`).toContain(glyph);
    // preservation: class names, data- attributes and inline handlers are unchanged
    for (const row of rowsOf("board")) {
      for (const attr of ["data-item", "data-title", "data-version", "data-notes"]) expect(row.hasAttribute(attr)).toBe(true);
      expect(row.querySelector(".sprint-item-icon")).not.toBeNull();
      expect(row.querySelector(".sprint-item-title")).not.toBeNull();
      expect(row.querySelector(".sprint-item-ver")).not.toBeNull();
      expect(row.querySelector(".sprint-item-actions")).not.toBeNull();
    }
    for (const row of rowsOf("backburner")) {
      for (const attr of ["data-item", "data-title", "data-version"]) expect(row.hasAttribute(attr)).toBe(true);
    }
    const pending = allRows().find((r) => r.dataset.item === "pending" && kindOf(r) === "board")!;
    const onclicks = Array.from(pending.querySelectorAll("button")).map((b) => b.getAttribute("onclick") || "");
    expect(onclicks.some((o) => o.includes(`sprintAction('${PID}','pending','complete')`))).toBe(true);
    expect(onclicks.some((o) => o.includes(`sprintItemEdit('${PID}','pending')`))).toBe(true);
    // sprintItemNotesEdit / sprintItemResourcesEdit insert next to the title inside its wrapper
    const title = pending.querySelector(".sprint-item-title") as HTMLElement;
    expect(title.parentElement).not.toBe(pending);
    expect(title.parentElement!.contains(pending.querySelector(".sprint-item-notes"))).toBe(true);
    expect(title.parentElement!.contains(pending.querySelector(".sprint-item-resources"))).toBe(true);
    // the wrapper is styled by the stylesheet, not by an inline flex:1 that would out-rank the media queries
    expect((title.parentElement as HTMLElement).getAttribute("style")).toBeNull();
  });

  it("an empty actions placeholder (done rows with no meta chip) takes no space or wrapped line", () => {
    const done = allRows().find((r) => r.dataset.item === "done" && kindOf(r) === "board")!;
    const actions = done.querySelector(":scope > .sprint-item-actions") as HTMLElement;
    expect(actions.childElementCount).toBe(0);
    expect(v(actions, "display")).toBe("none");
  });
});

describe("sprint row layout contract: notes, chips, badges and nested rows", () => {
  it("resource chips wrap inside the column; notes already break long tokens", () => {
    const rowsWithChips = rowsOf("board").filter((r) => r.querySelector(".resource-chip"));
    expect(rowsWithChips.length).toBeGreaterThan(0);
    for (const row of rowsWithChips) {
      for (const chip of Array.from(row.querySelectorAll(".resource-chip"))) {
        expect(v(chip, "max-width"), "chip max-width").toBe("100%");
        expect(["anywhere", "break-word"], "chip overflow-wrap").toContain(v(chip, "overflow-wrap"));
      }
    }
    const withNotes = rowsOf("board").filter((r) => r.querySelector(".sprint-item-notes"));
    expect(withNotes.length).toBeGreaterThan(0);
    for (const row of withNotes) {
      const notes = row.querySelector(".sprint-item-notes") as HTMLElement;
      expect(v(notes, "word-break") === "break-word" || ["anywhere", "break-word"].includes(v(notes, "overflow-wrap"))).toBe(true);
      // notes + chips live in the text column, so they can never reach the version / actions
      expect(columnOf(row).contains(notes)).toBe(true);
    }
  });

  it("badges stay inside the title (so they wrap with it) and nested rows keep their indent", () => {
    const parent = allRows().find((r) => r.dataset.item === "parent" && kindOf(r) === "board")!;
    const parentTitle = parent.querySelector(".sprint-item-title") as HTMLElement;
    expect(parentTitle.textContent).toContain("[1/2]"); // child-count badge
    const inProgress = allRows().find((r) => r.dataset.item === "in_progress" && kindOf(r) === "board")!;
    const t = inProgress.querySelector(".sprint-item-title") as HTMLElement;
    expect(t.querySelector(".sprint-stall-badge")).not.toBeNull();
    expect(t.querySelector(".sprint-live-dot")).not.toBeNull();
    const retried = allRows().find((r) => r.dataset.item === "retried" && kindOf(r) === "board")!;
    expect((retried.querySelector(".sprint-item-title") as HTMLElement).querySelector(".sprint-retried-badge")).not.toBeNull();
    const ind = allRows().find((r) => r.dataset.item === "ind" && kindOf(r) === "board")!;
    expect((ind.querySelector(".sprint-item-title") as HTMLElement).textContent).toContain("⚠"); // indeterminate badge
    // every badge sits in a title that is itself a wrapping block (not an inline run)
    for (const badgeTitle of [parentTitle, t]) {
      expect(["block", "flex", "grid", "inline-block", "flow-root"]).toContain(v(badgeTitle, "display") || "inline");
    }
    // nested child rows keep their 16px indent and still use the same wrapping row
    const kid = allRows().find((r) => r.dataset.item === "kid1")!;
    expect(kid.style.marginLeft).toBe("16px");
    expect(v(kid, "flex-wrap")).toBe("wrap");
    expect(["0", "0px"]).toContain(v(columnOf(kid), "min-width"));
  });
});

describe("sprint row layout contract: media queries and the parsed stylesheet", () => {
  type AnyRule = { selectorText?: string; style?: CSSStyleDeclaration; cssRules?: AnyRule[]; media?: { mediaText: string } };
  const sheet = () => document.getElementById("real-dashboard-css") as HTMLStyleElement;
  const mediaRules = (query: string): AnyRule[] => {
    const rules = Array.from((sheet().sheet as unknown as { cssRules: AnyRule[] }).cssRules);
    return rules
      .filter((r) => r.media && r.media.mediaText.replace(/\s+/g, " ").includes(query))
      .flatMap((r) => Array.from(r.cssRules || []));
  };
  const sprintRules = (rules: AnyRule[]) => rules.filter((r) => /sprint-item/.test(r.selectorText || ""));

  it("the 768px and 480px passes only tune the sprint rows: they never force nowrap / ellipsis / a full-width title", () => {
    const phone = sprintRules(mediaRules("max-width: 768px"));
    const narrow = sprintRules(mediaRules("max-width: 480px"));
    expect(phone.length).toBeGreaterThan(0);
    expect(narrow.length).toBeGreaterThan(0);
    for (const rule of [...phone, ...narrow]) {
      const s = rule.style as CSSStyleDeclaration;
      const where = rule.selectorText;
      expect(s.getPropertyValue("white-space"), `${where} white-space`).not.toBe("nowrap");
      expect(s.getPropertyValue("flex-wrap"), `${where} flex-wrap`).not.toBe("nowrap");
      expect(s.getPropertyValue("text-overflow"), `${where} text-overflow`).not.toBe("ellipsis");
      expect(s.getPropertyValue("overflow"), `${where} overflow`).not.toBe("hidden");
    }
    // the old rule gave the title flex-basis:100%, which on the direct-child rows left the
    // status icon alone on a line above the text; the title column must not do that any more
    const titleRules = phone.filter((r) => /sprint-item-title/.test(r.selectorText || ""));
    for (const rule of titleRules) {
      expect(rule.style!.getPropertyValue("flex-basis"), `${rule.selectorText} flex-basis`).not.toBe("100%");
    }
    // on a phone the action buttons take their own line under the text
    const actions = phone.find((r) => (r.selectorText || "").trim() === ".sprint-item-actions");
    expect(actions, "768px block must reconcile .sprint-item-actions").toBeDefined();
    expect(actions!.style!.getPropertyValue("flex-basis")).toBe("100%");
  });

  it("the wrapping / alignment rules live in the base stylesheet, so they hold at every width", () => {
    const base = Array.from((sheet().sheet as unknown as { cssRules: AnyRule[] }).cssRules).filter((r) => !r.media);
    const rule = (sel: string) => base.find((r) => (r.selectorText || "").trim() === sel);
    const row = rule(".sprint-item-row");
    expect(row, ".sprint-item-row base rule").toBeDefined();
    expect(row!.style!.getPropertyValue("flex-wrap")).toBe("wrap");
    expect(row!.style!.getPropertyValue("align-items")).toBe("baseline");
    const title = rule(".sprint-item-title");
    expect(title, ".sprint-item-title base rule").toBeDefined();
    expect(title!.style!.getPropertyValue("white-space")).toBe("normal");
    expect(title!.style!.getPropertyValue("overflow-wrap")).toBe("anywhere");
  });
});

// ---------------------------------------------------------------------------
// Cascade-independent scan. A rule that is MORE SPECIFIC than the contract rules (an
// ancestor-qualified `.live-body .sprint-item-title { white-space: nowrap }`) or that only
// applies at a phone width (`@media (max-width: 768px) { .live-body span { white-space:
// nowrap } }`) re-breaks the layout in a browser, yet passes every getComputedStyle
// assertion above (jsdom: source order only, no @media) and every source-text check that
// asks for one exact selector. This scan therefore does not resolve the cascade at all: it
// takes EVERY style rule (nested @media / @supports included, hover/focus states stripped),
// asks the DOM which board elements the rule could match (so ancestors, types, ids and
// attribute selectors all count), and rejects any declaration that could undo the contract,
// whatever the rule's specificity, position or viewport gate.
// ---------------------------------------------------------------------------
describe("sprint row layout contract: nothing in the stylesheet or markup can undo it", () => {
  type Flat = { selector: string; gate: string; decls: Array<[string, string]> };
  const flatten = (rules: any[], gate = ""): Flat[] =>
    rules.flatMap((r: any): Flat[] => {
      if (r.cssRules && (r.media || r.conditionText !== undefined)) {
        const g = r.media ? `@media ${r.media.mediaText}` : `@supports ${r.conditionText}`;
        return flatten(Array.from(r.cssRules), gate ? `${gate} ${g}` : g);
      }
      if (!r.selectorText) return [];
      const decls: Array<[string, string]> = [];
      for (let i = 0; i < r.style.length; i++) decls.push([r.style[i], r.style.getPropertyValue(r.style[i])]);
      return [{ selector: r.selectorText, gate, decls }];
    });

  // The "→ v2" pill and the buttons carry short fixed labels and are truncated / kept on
  // one line on purpose.
  const DELIBERATE = ".sprint-item-meta, .sprint-btn";
  const TEXT_BOXES = ".sprint-item-title, .sprint-item-main, .sprint-item-ver, .resource-chip";
  const COLUMN = ".sprint-item-title, .sprint-item-main";
  const WATCHED = new Set([
    "white-space", "text-overflow", "overflow", "overflow-x", "flex-wrap", "overflow-wrap",
    "word-wrap", "word-break", "display", "min-width", "position",
  ]);

  /** Why `prop: raw` is harmful on `el`, or null when it is harmless. */
  const harm = (el: Element, prop: string, raw: string): string | null => {
    const value = raw.replace(/!important/i, "").trim().toLowerCase();
    const deliberate = () => el.matches(DELIBERATE);
    switch (prop) {
      case "white-space":
        return /^(nowrap|pre)$/.test(value) && !deliberate() ? "stops the text wrapping" : null;
      case "text-overflow":
        return !/^(clip|initial|unset|inherit)$/.test(value) && !deliberate() ? "ellipsizes (hides) text" : null;
      case "overflow":
      case "overflow-x":
        return /\b(hidden|clip|scroll|auto)\b/.test(value) && !deliberate() ? "clips its content" : null;
      case "flex-wrap":
        return value === "nowrap" && el.matches(".sprint-item-row, .sprint-item-actions") ? "stops the row wrapping" : null;
      case "overflow-wrap":
      case "word-wrap":
        return value === "normal" && el.matches(TEXT_BOXES) ? "stops a long token breaking" : null;
      case "word-break":
        return value === "keep-all" ? "stops a long token breaking" : null;
      case "display":
        return !/^(block|flex|grid|flow-root|inline-block|list-item)$/.test(value) && el.matches(COLUMN)
          ? "makes the text column inline (ignores width / overflow) or hides it" : null;
      case "min-width":
        return !/^0(px)?$/.test(value) && el.matches(COLUMN) ? "stops the text column shrinking" : null;
      case "position":
        return /^(absolute|fixed)$/.test(value) ? "takes a board element out of flow (it can paint over its neighbours)" : null;
      default:
        return null;
    }
  };

  const STATE = /:(hover|focus|focus-visible|focus-within|active|visited|target)\b/g;
  /** Could `selector` apply to `el` at some point (any state)? Unparseable here => assume yes. */
  const couldMatch = (el: Element, selector: string): boolean => {
    try {
      return el.matches(selector.replace(STATE, "").trim() || "*");
    } catch {
      return true;
    }
  };
  const label = (el: Element) => el.tagName.toLowerCase() + (el.className ? "." + String(el.className).trim().split(/\s+/)[0] : "");

  const boardElements = () =>
    Array.from(document.getElementById(`live-sprint-progress-${PID}`)!.querySelectorAll(".sprint-item-row, .sprint-item-row *"));
  const realSheetRules = (): Flat[] =>
    flatten(Array.from(((document.getElementById("real-dashboard-css") as HTMLStyleElement).sheet as any).cssRules));

  /** Every harmful declaration, in `css` or in the markup's own inline styles, that can reach a board element. */
  const scan = (css: string | null): string[] => {
    const els = boardElements();
    const found = new Set<string>();
    if (css !== null) {
      const style = document.createElement("style");
      style.textContent = css;
      document.head.appendChild(style);
      try {
        for (const rule of flatten(Array.from((style.sheet as any).cssRules))) {
          for (const [prop, raw] of rule.decls) {
            if (!WATCHED.has(prop)) continue;
            for (const el of els) {
              const why = harm(el, prop, raw);
              if (why && couldMatch(el, rule.selector)) {
                found.add(`${rule.gate ? rule.gate + " " : ""}${rule.selector} { ${prop}: ${raw} } ${why} (<${label(el)}>)`);
              }
            }
          }
        }
      } finally {
        style.remove();
      }
    }
    for (const el of els) {
      const s = (el as HTMLElement).style;
      for (let i = 0; i < s.length; i++) {
        const why = WATCHED.has(s[i]) ? harm(el, s[i], s.getPropertyValue(s[i])) : null;
        if (why) found.add(`inline style on <${label(el)}> { ${s[i]}: ${s.getPropertyValue(s[i])} } ${why}`);
      }
    }
    return Array.from(found).sort();
  };
  const realCss = () => (document.getElementById("real-dashboard-css") as HTMLStyleElement).textContent || "";

  it("no rule in dashboard.css, whatever its specificity, order or @media gate, and no inline style can undo the wrapping contract", () => {
    expect(scan(realCss())).toEqual([]);
  });

  it("the scan is not vacuous: it reaches the contract rules and tolerates the deliberately truncated pill", () => {
    const els = boardElements();
    expect(els.length).toBeGreaterThan(60);
    // the exception is real (a pushed row's "→ v2" pill) and is allowed to truncate
    expect(els.some((e) => e.matches(".sprint-item-meta"))).toBe(true);
    expect(scan(".sprint-item-meta { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }")).toEqual([]);
    // the real sheet really does reach the board: the base contract rules ...
    const reaching = realSheetRules().filter((r) => els.some((e) => couldMatch(e, r.selector)));
    expect(reaching.filter((r) => !r.gate && r.decls.some(([p]) => WATCHED.has(p))).length).toBeGreaterThanOrEqual(6);
    // ... and the @media-gated ones, which getComputedStyle in jsdom never applies
    expect(reaching.filter((r) => r.gate.includes("max-width: 768px")).length).toBeGreaterThanOrEqual(3);
    expect(reaching.filter((r) => r.gate.includes("max-width: 480px")).length).toBeGreaterThanOrEqual(1);
  });

  it.each<[string, string, RegExp]>([
    ["an earlier, more specific rule (ancestor-qualified nowrap on the title)",
      ".live-body .sprint-item-title { white-space: nowrap; }", /\.live-body \.sprint-item-title \{ white-space: nowrap \} stops the text wrapping/],
    ["a non-sprint selector inside the phone @media block",
      "@media (max-width: 768px) { .live-body span { white-space: nowrap; } }", /@media \(max-width: 768px\) \.live-body span \{ white-space: nowrap \}/],
    ["a bare element selector inside the narrow-phone @media block",
      "@media (max-width: 480px) { span { white-space: pre; } }", /@media \(max-width: 480px\) span \{ white-space: pre \}/],
    ["a rule nested in @supports",
      "@supports (display: grid) { .tab-body .sprint-item-row span { white-space: nowrap } }", /@supports/],
    ["an ellipsis + clip on the title",
      ".sprint-item-title { overflow: hidden; text-overflow: ellipsis; }", /text-overflow: ellipsis/],
    ["overflow-x clip through a descendant combinator",
      ".sprint-item-row * { overflow-x: clip; }", /overflow-x: clip/],
    ["!important",
      ".sprint-item-row .sprint-item-title { white-space: nowrap !important; }", /\.sprint-item-row \.sprint-item-title \{ white-space: nowrap \} stops the text wrapping/],
    ["a hover state",
      ".sprint-item-title:hover { white-space: nowrap; }", /\.sprint-item-title:hover \{ white-space: nowrap \}/],
    ["a row that stops wrapping on a phone",
      "@media (max-width: 480px) { .sprint-item-row { flex-wrap: nowrap; } }", /flex-wrap: nowrap \} stops the row wrapping/],
    ["action buttons that stop wrapping",
      ".sprint-item-actions { flex-wrap: nowrap; }", /\.sprint-item-actions \{ flex-wrap: nowrap \}/],
    ["an inline title again",
      "@media (max-width: 768px) { .sprint-item-title { display: inline; } }", /display: inline \} makes the text column inline/],
    ["a text column that cannot shrink",
      ".sprint-item-main { min-width: auto; }", /min-width: auto \} stops the text column shrinking/],
    ["a long token that cannot break",
      ".sprint-item-ver { overflow-wrap: normal; }", /overflow-wrap: normal \} stops a long token breaking/],
    ["a resource chip that cannot wrap",
      ".sprint-item-resources .resource-chip { white-space: nowrap; }", /\.resource-chip \{ white-space: nowrap \}/],
    ["a title taken out of flow",
      ".sprint-item-title { position: absolute; }", /position: absolute \} takes a board element out of flow/],
  ])("catches %s", (_name, css, expected) => {
    expect(scan(css).join("\n")).toMatch(expected);
  });

  it("does not cry wolf at rules that cannot reach the board", () => {
    expect(scan(".tabs span { white-space: nowrap; } .sidebar button { overflow: hidden; } .sprint-item-row { gap: 10px; color: red; }")).toEqual([]);
    expect(scan("@media (max-width: 768px) { .vtab-strip .vtab-btn { white-space: nowrap; overflow: hidden; } }")).toEqual([]);
  });

  it("catches a harmful inline style written into the markup", () => {
    const title = document.querySelector(".sprint-item-row .sprint-item-title") as HTMLElement;
    title.style.whiteSpace = "nowrap";
    expect(scan(null).join("\n")).toMatch(/inline style on <span\.sprint-item-title> \{ white-space: nowrap \}/);
    title.removeAttribute("style");
    expect(scan(null)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The Needs-attention row's "Pending" button used to render the tail of its own
// handler as its label: `${JSON.stringify(projectId)}` put raw double quotes inside the
// double-quoted onclick attribute, so the attribute ended early and the rest of the
// handler (`x.id===it.id?{...x,status:'pending'}:x)))">`) became a 255px-wide unbreakable
// "label" that pushed the row's buttons out past the board at narrow widths. (The
// handler also referenced `items` / `it`, which do not exist where an inline handler runs.)
// ---------------------------------------------------------------------------
describe("sprint board buttons: labels and the Needs-attention 'Pending' handler", () => {
  afterEach(() => vi.restoreAllMocks());

  it("no board button label leaks markup or handler code from a mis-quoted attribute", () => {
    const buttons = Array.from(document.querySelectorAll<HTMLButtonElement>(`#live-sprint-progress-${PID} button.sprint-btn`));
    expect(buttons.length).toBeGreaterThan(20);
    for (const b of buttons) {
      const text = (b.textContent || "").trim();
      expect(text, `button ${b.title}`).toMatch(/^[^"'<>=(){}\\]{1,24}$/u);
    }
  });

  it("the Needs-attention 'Pending' button reads '↩ Pending' and carries only its own attributes", () => {
    const row = allRows().find((r) => r.dataset.item === "ind" && kindOf(r) === "attention")!;
    const btn = row.querySelector<HTMLButtonElement>('button[title="Back to pending"]')!;
    expect((btn.textContent || "").trim()).toBe("↩ Pending");
    expect(Array.from(btn.attributes).map((a) => a.name).sort()).toEqual(["class", "onclick", "title"]);
  });

  it("clicking it PATCHes the item back to pending, then refreshes the Live tab", async () => {
    const row = allRows().find((r) => r.dataset.item === "ind" && kindOf(r) === "attention")!;
    const code = row.querySelector<HTMLButtonElement>('button[title="Back to pending"]')!.getAttribute("onclick")!;
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true });
    const refreshSpy = vi.fn();
    // an inline handler runs in global scope: give it only the two globals it may use
    const handler = new Function("fetch", "refreshLiveTab", code);
    handler(fetchSpy, refreshSpy);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`/projects/${PID}/sprint-items/ind`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ status: "pending" });
    await Promise.resolve();
    await Promise.resolve();
    expect(refreshSpy).toHaveBeenCalledWith(PID);
  });
});
