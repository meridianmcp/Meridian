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
// rule that wins on specificity or applies only on a phone. The scan describe block
// therefore reads every rule of the raw stylesheet (nesting, @layer, @container, :is()
// groups and all @media resolved; var() values judged as harmful where the property can
// harm) for a declaration that could undo the contract, and the PIXEL layout itself is
// measured in a real browser by tests/test_demo_ux.py
// (test_sprint_rows_never_overlap_in_a_real_browser, which also pins that SHORT rows keep
// their buttons beside the title: test_sprint_short_rows_keep_their_buttons_beside_the_title...).
// jsdom rejects a sheet with CSS nesting or @layer outright, so such a sheet is flattened
// before jsdom sees it (toJsdomCss); a flat sheet is fed through unchanged.
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

// ---------------------------------------------------------------------------
// A small CSS reader, shared by the cascade scan below and by the jsdom loader.
//
// jsdom's CSSOM is not a safe thing to read declarations from: it rejects the WHOLE sheet
// ("Could not parse CSS stylesheet") when it meets CSS nesting or @layer, and its style
// declaration silently drops properties it does not know (text-wrap, ...). So the scan reads
// the raw text itself (rules, nesting, @media / @supports / @container / @layer gates,
// `;` inside url() and strings), and asks the DOM only which elements a selector could match.
// ---------------------------------------------------------------------------
type Rule = { gates: string[]; selectors: string[]; decls: Array<[string, string]> };
type Item = { kind: "stmt"; text: string } | { kind: "block"; prelude: string; body: string };

/** Split `text` on `sep` at nesting depth 0 (outside (), [], {} and quoted strings). */
function splitTop(text: string, sep: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let quote = "";
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = "";
    } else if (c === '"' || c === "'") quote = c;
    else if ("([{".includes(c)) depth++;
    else if (")]}".includes(c)) depth = Math.max(0, depth - 1);
    else if (c === sep && depth === 0) {
      parts.push(text.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(text.slice(start));
  return parts;
}

/** The top-level items of a CSS block body: `;`-terminated statements and `prelude { body }` blocks. */
function cssItems(text: string): Item[] {
  const items: Item[] = [];
  let depth = 0;
  let quote = "";
  let start = 0;
  let brace = 0;
  let bodyStart = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = "";
    } else if (c === '"' || c === "'") quote = c;
    else if (brace > 0) {
      if (c === "{") brace++;
      else if (c === "}" && --brace === 0) {
        items.push({ kind: "block", prelude: text.slice(start, bodyStart).trim(), body: text.slice(bodyStart + 1, i) });
        start = i + 1;
      }
    } else if (c === "(" || c === "[") depth++;
    else if (c === ")" || c === "]") depth = Math.max(0, depth - 1);
    else if (depth === 0 && c === ";") {
      const stmt = text.slice(start, i).trim();
      if (stmt) items.push({ kind: "stmt", text: stmt });
      start = i + 1;
    } else if (depth === 0 && c === "{") {
      bodyStart = i;
      brace = 1;
    }
  }
  if (brace === 0 && text.slice(start).trim()) items.push({ kind: "stmt", text: text.slice(start).trim() });
  return items;
}

const GROUP_AT_RULE = /^@(media|supports|container|layer|scope|document|starting-style)\b/i;
const squash = (s: string) => s.split(/\s+/).filter(Boolean).join(" ");

/**
 * Every style rule of `css`, in source order, with nesting resolved (`&` or an implicit
 * descendant) and the preludes of the conditional / layer groups it sits in. Statement
 * at-rules (@import, `@layer a, b;`) are skipped without swallowing the rule after them, and
 * rule-less at-rules (@keyframes, @font-face) are skipped whole.
 */
function parseCss(css: string): { rules: Rule[]; nested: boolean; layered: boolean } {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Array<Rule | null> = [];
  let nested = false;
  let layered = false;
  const walk = (body: string, selectors: string[] | null, gates: string[]): Array<[string, string]> => {
    const decls: Array<[string, string]> = [];
    for (const item of cssItems(body)) {
      if (item.kind === "stmt") {
        const colon = item.text.indexOf(":");
        if (!item.text.startsWith("@") && colon > 0) {
          decls.push([item.text.slice(0, colon).trim().toLowerCase(), squash(item.text.slice(colon + 1))]);
        }
        continue;
      }
      const prelude = squash(item.prelude);
      if (prelude.startsWith("@")) {
        if (GROUP_AT_RULE.test(prelude)) {
          if (/^@layer\b/i.test(prelude)) layered = true;
          const group = [...gates, prelude];
          const inner = walk(item.body, selectors, group);
          if (inner.length && selectors) {
            nested = true; // `.a { @media (..) { color: red } }`: the declarations style the parent
            out.push({ gates: group, selectors: [...selectors], decls: inner });
          }
        }
        continue;
      }
      const own = splitTop(prelude, ",").map((s) => s.trim()).filter(Boolean);
      const resolved = selectors
        ? selectors.flatMap((parent) => own.map((child) => (child.includes("&") ? child.split("&").join(parent) : `${parent} ${child}`)))
        : own;
      if (selectors) nested = true;
      const slot = out.length;
      out.push(null);
      out[slot] = { gates, selectors: resolved, decls: walk(item.body, resolved, gates) };
    }
    return decls;
  };
  walk(text, null, []);
  return { rules: out.filter((r): r is Rule => r !== null), nested, layered };
}

/**
 * The text to hand jsdom. A flat sheet goes in exactly as written. A sheet jsdom cannot parse
 * (CSS nesting, @layer: it would drop ALL of the dashboard's CSS and fail every computed-style
 * test) is flattened first: nesting resolved, @layer blocks unwrapped and hoisted ahead of the
 * unlayered rules (a layered rule loses to an unlayered one in every browser, and jsdom resolves
 * the cascade by source order), @container / @scope rules dropped (jsdom cannot evaluate them).
 * @keyframes / @font-face / @import are dropped too: no computed-style test reads them.
 */
function toJsdomCss(css: string): string {
  const { rules, nested, layered } = parseCss(css);
  if (!nested && !layered) return css;
  const isLayer = (r: Rule) => r.gates.some((g) => /^@layer\b/i.test(g));
  const emit = (r: Rule): string => {
    if (r.gates.some((g) => /^@(container|scope|document|starting-style)\b/i.test(g))) return "";
    const body = `${r.selectors.join(", ")} { ${r.decls.map(([p, v]) => `${p}: ${v};`).join(" ")} }`;
    return r.gates.filter((g) => /^@(media|supports)\b/i.test(g)).reduceRight((inner, gate) => `${gate} { ${inner} }`, body);
  };
  return [...rules.filter(isLayer), ...rules.filter((r) => !isLayer(r))].map(emit).filter(Boolean).join("\n");
}

const REAL_CSS = readFileSync(resolve(process.cwd(), "meridian/static/dashboard.css"), "utf8");

beforeAll(() => {
  // Same cascade the dashboard gets: the REAL stylesheet text.
  const style = document.createElement("style");
  style.id = "real-dashboard-css";
  style.textContent = toJsdomCss(REAL_CSS);
  document.head.appendChild(style);
  // jsdom must really have loaded it (a sheet it cannot parse is silently dropped, and every
  // computed-style assertion below would then be reading nothing)
  expect(style.sheet, "jsdom could not parse dashboard.css").not.toBeNull();
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
    // reading order of a board row: status icon, text column, version label, THEN the buttons (the
    // stylesheet's margin-left:auto pushes the buttons right; a row that lists them before the version
    // label would put the label on the wrong side of them)
    for (const row of rowsOf("board")) {
      const order = Array.from(row.children).map((c) => Array.from(c.classList).find((k) => k.startsWith("sprint-item-")));
      expect(order, `board row ${row.dataset.item}`).toEqual(["sprint-item-icon", "sprint-item-main", "sprint-item-ver", "sprint-item-actions"]);
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

  it("the text column is sized by its content, capped at the room beside the icon (a fixed minimum basis wrapped the buttons of every short row)", () => {
    const base = parseCss(REAL_CSS).rules.filter((r) => r.gates.length === 0);
    for (const selector of [".sprint-item-main", ".sprint-item-row > .sprint-item-title"]) {
      const decls = new Map(base.filter((r) => r.selectors.includes(selector)).flatMap((r) => r.decls));
      expect(decls.get("flex"), `${selector} flex`).toBe("1 1 auto");
      expect(decls.get("min-width"), `${selector} min-width`).toBe("0");
      expect(decls.get("max-width"), `${selector} max-width`).toBe("calc(100% - 20px)");
    }
  });

  it("the wrapping / alignment rules live in the base stylesheet, so they hold at every width", () => {
    const base = Array.from((sheet().sheet as unknown as { cssRules: AnyRule[] }).cssRules).filter((r) => !r.media);
    // the LAST matching rule: jsdom resolves the cascade by source order, and a layered rule (which loses
    // to every unlayered one in a browser) is hoisted ahead of the unlayered rules when the sheet is flattened
    const rule = (sel: string) => base.filter((r) => (r.selectorText || "").trim() === sel).pop();
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
// reads EVERY style rule of the raw stylesheet text (nesting resolved, every @media /
// @supports / @container / @layer gate, hover/focus states stripped), asks the DOM which
// board elements the rule could match (so ancestors, types, ids, :is()/:where() groups and
// attribute selectors all count), and rejects any declaration that could undo the contract,
// whatever the rule's specificity, position, layer or viewport gate. A value it cannot
// resolve (var(), env(), attr()) counts as harmful wherever the property could do harm.
// The pixel layout itself is measured in a real browser by tests/test_demo_ux.py.
// ---------------------------------------------------------------------------
describe("sprint row layout contract: nothing in the stylesheet or markup can undo it", () => {
  type Flat = { selector: string; gate: string; decls: Array<[string, string]> };
  const flatten = (css: string): Flat[] =>
    parseCss(css).rules.flatMap((r) => r.selectors.map((selector) => ({ selector, gate: r.gates.join(" "), decls: r.decls })));

  // The "→ v2" pill and the buttons carry short fixed labels and are truncated / kept on
  // one line on purpose.
  const DELIBERATE = ".sprint-item-meta, .sprint-btn";
  const TEXT_BOXES = ".sprint-item-title, .sprint-item-main, .sprint-item-ver, .resource-chip";
  const COLUMN = ".sprint-item-title, .sprint-item-main";
  // Boxes holding wrapping text: pinning one to a fixed height, squashing its line box or
  // shifting it out of the flow paints its content over the NEXT row. The icon, the live dot,
  // the badges and the buttons are fixed-size on purpose.
  const FLOW =
    ".sprint-item-row, .sprint-item-main, .sprint-item-title, .sprint-item-ver, .sprint-item-actions, .sprint-item-notes, .sprint-item-resources, .resource-chip";
  const ROW_BOXES = ".sprint-item-row, .sprint-item-actions";
  const UNRESOLVED = /\b(?:var|env|attr)\(/;
  const WATCHED = new Set([
    "white-space", "text-wrap", "text-wrap-mode", "text-overflow", "overflow", "overflow-x", "overflow-y",
    "overflow-block", "overflow-inline", "flex-wrap", "flex-flow", "overflow-wrap", "word-wrap", "word-break",
    "display", "min-width", "position", "height", "block-size", "max-height", "max-block-size", "line-height",
    "font", "top", "bottom", "inset", "inset-block", "inset-block-start", "inset-block-end", "transform",
    "translate", "margin", "margin-top", "margin-bottom", "margin-block", "margin-block-start",
    "margin-block-end", "all",
  ]);
  /** True when a line-height is small enough to paint wrapped lines over each other. */
  const squashes = (lineHeight: string): boolean => {
    const m = /^([0-9.]+)(px|pt|em|rem|%)?$/.exec(lineHeight);
    if (!m) return false;
    const floor = ({ "": 1, px: 9, pt: 7, em: 0.75, rem: 0.75, "%": 75 } as Record<string, number>)[m[2] ?? ""];
    return parseFloat(m[1]) < floor;
  };

  /** Why `prop: raw` is harmful on `el`, or null when it is harmless. */
  const harm = (el: Element, prop: string, raw: string): string | null => {
    const value = squash(raw.replace(/!important/i, "").trim().toLowerCase());
    const unresolved = UNRESOLVED.test(value);
    const is = (selector: string) => el.matches(selector);
    const truncating = !is(DELIBERATE);
    const verdict = (applies: boolean, harmful: boolean, message: string): string | null =>
      !applies ? null : unresolved ? `${message} (its value is built from a custom property, which cannot be resolved here)` : harmful ? message : null;
    switch (prop) {
      case "white-space":
        return verdict(truncating, /^(nowrap|pre)$/.test(value), "stops the text wrapping");
      case "text-wrap":
      case "text-wrap-mode":
        return verdict(truncating, value.split(" ").includes("nowrap"), "stops the text wrapping");
      case "text-overflow":
        return verdict(truncating, !/^(clip|initial|unset|inherit)$/.test(value), "ellipsizes (hides) text");
      case "overflow":
      case "overflow-x":
      case "overflow-y":
      case "overflow-block":
      case "overflow-inline":
        return verdict(truncating, /\b(hidden|clip|scroll|auto)\b/.test(value), "clips its content");
      case "flex-wrap":
        return verdict(is(ROW_BOXES), value === "nowrap", "stops the row wrapping");
      case "flex-flow":
        return verdict(is(ROW_BOXES), value.split(" ").includes("nowrap"), "stops the row wrapping");
      case "overflow-wrap":
      case "word-wrap":
        return verdict(is(TEXT_BOXES), value === "normal", "stops a long token breaking");
      case "word-break":
        return verdict(true, value === "keep-all", "stops a long token breaking");
      case "display":
        return verdict(is(COLUMN), !/^(block|flex|grid|flow-root|inline-block|list-item)$/.test(value),
          "makes the text column inline (ignores width / overflow) or hides it");
      case "min-width":
        return verdict(is(COLUMN), !/^0(px)?$/.test(value), "stops the text column shrinking");
      case "position":
        return verdict(true, /^(absolute|fixed)$/.test(value), "takes a row element out of flow (it can paint over its neighbours)");
      case "height":
      case "block-size":
        return verdict(is(FLOW), !/^(auto|fit-content|min-content|max-content|initial|unset|revert|revert-layer)$/.test(value),
          "pins a text box to a fixed height, so wrapped content paints over the next row");
      case "max-height":
      case "max-block-size":
        return verdict(is(FLOW), !/^(none|initial|unset|revert|revert-layer)$/.test(value),
          "caps a text box's height, so wrapped content paints over the next row");
      case "line-height":
        return verdict(is(FLOW), squashes(value), "squashes wrapped lines onto each other");
      case "font": {
        const slash = /\/\s*([^\s,/]+)/.exec(value);
        return verdict(is(FLOW), !!slash && squashes(slash[1]), "squashes wrapped lines onto each other");
      }
      case "top":
      case "bottom":
      case "inset":
      case "inset-block":
      case "inset-block-start":
      case "inset-block-end":
        return verdict(is(FLOW), !/^(auto|0(px|%|em|rem)?|initial|unset|revert)$/.test(value),
          "offsets a text box from its place in the flow (it can paint over the previous row)");
      case "transform":
      case "translate":
        return verdict(is(FLOW), !/^(none|initial|unset|revert)$/.test(value), "moves a text box out of its place in the flow");
      case "margin":
      case "margin-top":
      case "margin-bottom":
      case "margin-block":
      case "margin-block-start":
      case "margin-block-end":
        return verdict(is(FLOW), /(^|\s)-[0-9.]/.test(value), "pulls a text box over its neighbours with a negative margin");
      case "all":
        return verdict(truncating, true, "resets every property, including the wrapping contract");
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

  /** Every harmful declaration, in `css` or in the markup's own inline styles, that can reach a board element. */
  const scan = (css: string | null): string[] => {
    const els = boardElements();
    const found = new Set<string>();
    if (css !== null) {
      for (const rule of flatten(css)) {
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
    }
    for (const el of els) {
      // the style attribute is read as text: jsdom's style declaration drops properties it does not know
      for (const part of splitTop(el.getAttribute("style") || "", ";")) {
        const colon = part.indexOf(":");
        if (colon < 1) continue;
        const prop = part.slice(0, colon).trim().toLowerCase();
        const raw = squash(part.slice(colon + 1));
        const why = WATCHED.has(prop) ? harm(el, prop, raw) : null;
        if (why) found.add(`inline style on <${label(el)}> { ${prop}: ${raw} } ${why}`);
      }
    }
    return Array.from(found).sort();
  };

  it("no rule in dashboard.css, whatever its specificity, order, layer or @media gate, and no inline style can undo the wrapping contract", () => {
    expect(scan(REAL_CSS)).toEqual([]);
  });

  it("the scan is not vacuous: it reaches the contract rules and tolerates the deliberately truncated pill", () => {
    const els = boardElements();
    expect(els.length).toBeGreaterThan(60);
    // the exception is real (a pushed row's "→ v2" pill) and is allowed to truncate
    expect(els.some((e) => e.matches(".sprint-item-meta"))).toBe(true);
    expect(scan(".sprint-item-meta { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }")).toEqual([]);
    // the real sheet really does reach the board: the base contract rules ...
    const reaching = flatten(REAL_CSS).filter((r) => els.some((e) => couldMatch(e, r.selector)));
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
      ".sprint-item-row .sprint-item-title { white-space: nowrap !important; }", /\.sprint-item-row \.sprint-item-title \{ white-space: nowrap !important \} stops the text wrapping/],
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
      ".sprint-item-title { position: absolute; }", /position: absolute \} takes a row element out of flow/],
    // --- syntax the scan used to be blind to: every one of these re-breaks a real browser ---
    ["CSS nesting (implicit descendant)",
      ".live-body { .sprint-item-title { white-space: nowrap; } }", /\.live-body \.sprint-item-title \{ white-space: nowrap \} stops the text wrapping/],
    ["CSS nesting with &",
      ".sprint-item-row { & .sprint-item-title { white-space: nowrap; } }", /\.sprint-item-row \.sprint-item-title \{ white-space: nowrap \}/],
    ["a nested @media that styles its parent",
      ".sprint-item-title { @media (max-width: 768px) { white-space: nowrap; } }", /@media \(max-width: 768px\) \.sprint-item-title \{ white-space: nowrap \}/],
    ["a value taken from a custom property",
      ".sprint-item-title { white-space: var(--ws); }", /white-space: var\(--ws\) \} stops the text wrapping \(its value is built from a custom property/],
    ["a custom-property flex-wrap",
      ".sprint-item-row { flex-wrap: var(--wrap); }", /flex-wrap: var\(--wrap\) \} stops the row wrapping \(its value is built/],
    [":is() groups on both sides",
      ":is(.live-body, .tab-body) :is(.sprint-item-title) { white-space: nowrap; }", /:is\(\.live-body, \.tab-body\) :is\(\.sprint-item-title\) \{ white-space: nowrap \}/],
    [":where() group of row elements",
      ".sprint-item-row :where(.sprint-item-title, .sprint-item-ver) { white-space: nowrap; }", /:where\(.*\) \{ white-space: nowrap \}/],
    ["a rule inside @layer (important layered rules beat unlayered ones)",
      "@layer base { .sprint-item-title { white-space: nowrap !important; } }", /@layer base \.sprint-item-title \{ white-space: nowrap !important \}/],
    ["a rule inside @container",
      "@container (min-width: 1px) { .sprint-item-title { white-space: nowrap; } }", /@container \(min-width: 1px\) \.sprint-item-title/],
    ["a rule right after a statement at-rule",
      "@layer base, theme;\n@import url('x.css');\n.sprint-item-title { white-space: nowrap; }", /\.sprint-item-title \{ white-space: nowrap \}/],
    ["a row pinned to one line (height)",
      ".sprint-item-row { height: 28px; }", /\.sprint-item-row \{ height: 28px \} pins a text box to a fixed height/],
    ["a row pinned to one line (max-height)",
      ".sprint-item-row { max-height: 28px; }", /max-height: 28px \} caps a text box's height/],
    ["a zero-height buttons box",
      ".sprint-item-actions { height: 0; }", /\.sprint-item-actions \{ height: 0 \} pins a text box/],
    ["squashed lines (line-height)",
      ".sprint-item-title { line-height: 0.5; }", /line-height: 0\.5 \} squashes wrapped lines/],
    ["squashed lines (font shorthand)",
      ".sprint-item-title { font: 12px/0.5 sans-serif; }", /font: 12px\/0\.5 sans-serif \} squashes wrapped lines/],
    ["a title lifted onto the previous row",
      ".sprint-item-title { position: relative; top: -22px; }", /top: -22px \} offsets a text box/],
    ["a transformed title",
      ".sprint-item-title { transform: translateY(-20px); }", /transform: translateY\(-20px\) \} moves a text box/],
    ["a negative margin",
      ".sprint-item-row { margin: 0 0 -10px; }", /margin: 0 0 -10px \} pulls a text box over its neighbours/],
    ["flex-flow nowrap",
      ".sprint-item-row { flex-flow: row nowrap; }", /flex-flow: row nowrap \} stops the row wrapping/],
    ["text-wrap nowrap (a property jsdom's style declaration drops)",
      ".sprint-item-title { text-wrap: nowrap; }", /text-wrap: nowrap \} stops the text wrapping/],
    ["all: unset",
      ".sprint-item-title { all: unset; }", /all: unset \} resets every property/],
  ])("catches %s", (_name, css, expected) => {
    expect(scan(css).join("\n")).toMatch(expected);
  });

  it("does not cry wolf at rules that cannot reach the board, or at values that are fine", () => {
    expect(scan(".tabs span { white-space: nowrap; } .sidebar button { overflow: hidden; } .sprint-item-row { gap: 10px; color: red; }")).toEqual([]);
    expect(scan("@media (max-width: 768px) { .vtab-strip .vtab-btn { white-space: nowrap; overflow: hidden; } }")).toEqual([]);
    // fixed-size parts of a row are not text boxes
    expect(scan(".sprint-btn { height: 20px; line-height: 1; } .sprint-live-dot { width: 7px; height: 7px; }")).toEqual([]);
    expect(scan("@keyframes sprintPulse { from { height: 0; top: -4px; } to { height: 7px; top: 0; } }")).toEqual([]);
    // values that are fine on a text box
    expect(scan(".sprint-item-row { min-height: 28px; line-height: 1.4; margin: 0 0 2px; height: auto; max-height: none; padding: var(--pad); }")).toEqual([]);
    expect(scan(".sprint-item-title { line-height: normal; transform: none; top: auto; margin-top: 2px; font: 12px/1.4 sans-serif; }")).toEqual([]);
    // nesting, groups, layers and custom properties that do not reach a row
    expect(scan(".sidebar { .tab { white-space: nowrap; } } .tabs { @media (max-width: 768px) { white-space: nowrap; } }")).toEqual([]);
    expect(scan(":is(.tabs, .sidebar) span { white-space: nowrap; } @layer base { .tabs span { white-space: nowrap; } }")).toEqual([]);
    expect(scan(".tabs span { white-space: var(--ws); } @import url('x.css'); @font-face { font-family: X; src: url(data:font/woff2;base64,AAAA); }")).toEqual([]);
  });

  it("catches a harmful inline style written into the markup", () => {
    const title = document.querySelector(".sprint-item-row .sprint-item-title") as HTMLElement;
    title.style.whiteSpace = "nowrap";
    expect(scan(null).join("\n")).toMatch(/inline style on <span\.sprint-item-title> \{ white-space: nowrap \}/);
    title.removeAttribute("style");
    expect(scan(null)).toEqual([]);
    // a longhand jsdom's style declaration would drop is still read from the attribute text
    title.setAttribute("style", "text-wrap: nowrap; height: 14px");
    const found = scan(null).join("\n");
    expect(found).toMatch(/inline style on <span\.sprint-item-title> \{ text-wrap: nowrap \}/);
    expect(found).toMatch(/\{ height: 14px \} pins a text box/);
    title.removeAttribute("style");
  });
});

// ---------------------------------------------------------------------------
// The CSS reader itself (the scan above is only as good as what it reads) and the jsdom
// loader: jsdom drops the WHOLE sheet when it meets nesting or @layer, which used to fail
// every computed-style test above for a rule that is harmless in a browser.
// ---------------------------------------------------------------------------
describe("sprint row layout contract: the CSS reader and the jsdom loader", () => {
  it("resolves nesting, keeps every gate, splits selector lists only on top-level commas and survives ; in url()/strings", () => {
    const { rules, nested, layered } = parseCss(
      "@import url('x.css');\n@layer base, theme;\n" +
        ".a, :is(.b, .c) > .d { color: red; background: url(data:image/png;base64,AAA=); content: 'a;b'; }\n" +
        ".live-body { .sprint-item-title { white-space: nowrap } &:hover { color: blue } > .x { top: 1px }\n" +
        "  @media (max-width: 768px) { gap: 2px } }\n" +
        "@media (max-width: 768px) { @supports (display: grid) { .g { display: grid } } }\n" +
        "@keyframes k { from { height: 0 } to { height: 5px } }\n@font-face { font-family: F; src: url(f.woff2) }\n" +
        "@layer base { .layered { white-space: pre } }\n.last { color: green }",
    );
    const bySelector = new Map(rules.map((r) => [r.selectors.join(" | "), r]));
    expect(bySelector.get(".a | :is(.b, .c) > .d")!.decls).toEqual([
      ["color", "red"], ["background", "url(data:image/png;base64,AAA=)"], ["content", "'a;b'"],
    ]);
    expect(bySelector.get(".live-body .sprint-item-title")!.decls).toEqual([["white-space", "nowrap"]]);
    expect(bySelector.get(".live-body:hover")!.decls).toEqual([["color", "blue"]]);
    expect(bySelector.get(".live-body > .x")!.decls).toEqual([["top", "1px"]]);
    expect(bySelector.get(".live-body")).toMatchObject({ gates: ["@media (max-width: 768px)"], decls: [["gap", "2px"]] });
    expect(bySelector.get(".g")!.gates).toEqual(["@media (max-width: 768px)", "@supports (display: grid)"]);
    expect(bySelector.get(".layered")!.gates).toEqual(["@layer base"]);
    expect(bySelector.get(".last")).toMatchObject({ gates: [], decls: [["color", "green"]] });
    expect(rules.some((r) => r.selectors.includes("from") || r.decls.some(([p]) => p === "font-family"))).toBe(false);
    expect(nested && layered).toBe(true);
  });

  it("feeds a flat sheet to jsdom exactly as written (keyframes, font-face and all), and flattens only what it must", () => {
    const flat = "@import url('x.css');\n.a { color: red; }\n@media (max-width: 1px) { .b { color: blue; } }\n@keyframes k { from { top: 0 } }\n";
    expect(parseCss(flat)).toMatchObject({ nested: false, layered: false });
    expect(toJsdomCss(flat)).toBe(flat);
    expect(parseCss(".a { .b { color: red } }").nested).toBe(true);
    expect(parseCss("@layer x { .a { color: red } }").layered).toBe(true);
  });

  const load = (css: string) => {
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
    return style;
  };
  const whiteSpace = (el: Element) => getComputedStyle(el).getPropertyValue("white-space");

  it("flattens a sheet jsdom cannot parse so a harmless layered or nested rule does not drop the whole cascade", () => {
    const title = document.querySelector(".sprint-item-row .sprint-item-title") as HTMLElement;
    expect(whiteSpace(title)).toBe("normal");
    // a layered nowrap loses to the unlayered `white-space: normal` of the real sheet in every browser
    const layered = load(toJsdomCss(REAL_CSS + "\n@layer base { .sprint-item-title { white-space: nowrap; } }"));
    try {
      expect(layered.sheet, "the flattened sheet must be parseable").not.toBeNull();
      expect(whiteSpace(title)).toBe("normal");
    } finally {
      layered.remove();
    }
    // nesting is resolved (the nested rule is the LAST rule, so it wins in source order, as in a browser)
    const nestedStyle = load(toJsdomCss(REAL_CSS + "\n.live-body { .sprint-item-title { white-space: nowrap; } }"));
    try {
      expect(nestedStyle.sheet).not.toBeNull();
      expect(whiteSpace(title)).toBe("nowrap");
    } finally {
      nestedStyle.remove();
    }
    expect(whiteSpace(title)).toBe("normal");
  });

  it("keeps @media blocks in the flattened sheet (the parsed-stylesheet tests read them) and drops what jsdom cannot evaluate", () => {
    const css = toJsdomCss(".a { color: red; } @layer x { .b { color: blue; } } .c { @media (max-width: 1px) { color: green } } @container (min-width: 1px) { .d { color: pink } }");
    expect(css).toContain("@media (max-width: 1px) { .c { color: green; } }");
    expect(css.indexOf(".b {")).toBeLessThan(css.indexOf(".a {"));
    expect(css).not.toContain("@layer");
    expect(css).not.toContain(".d");
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

  it("clicking it calls the shared sprintResetPending helper for this item (it PATCHes, then repaints Queue, Live and the Goal board; see live-refresh.test.ts)", () => {
    const row = allRows().find((r) => r.dataset.item === "ind" && kindOf(r) === "attention")!;
    const code = row.querySelector<HTMLButtonElement>('button[title="Back to pending"]')!.getAttribute("onclick")!;
    const helper = vi.fn();
    // an inline handler runs in global scope: give it only the one global it may use
    new Function("sprintResetPending", code)(helper);
    expect(helper).toHaveBeenCalledTimes(1);
    expect(helper).toHaveBeenCalledWith(PID, "ind");
  });
});
