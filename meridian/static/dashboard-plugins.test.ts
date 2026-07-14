// Unit tests for dashboard-plugins helpers.
//
// f5e1ed49 — the reference-manager (Zotero) status row: _renderZoteroStatusRow
// maps GET /tunnel/status (zotero_active + optional slot_status.zotero) to the
// same three-state status badge the other slot rows use.
// 78114fa6 — the stale-override "Use new default" handler: _applyStaleOverrideDefault
// must POPULATE the slot command input with the new built-in default (e.g.
// `uvx docx-mcp` for the word slot), not CLEAR it.
//
// dashboard-plugins.ts is a legacy *script* module (symbols land on `window`,
// not ES exports), so we import it for side effects and read the functions off
// the global. A stand-in escapeHtml is installed first because the helpers call
// the bare global at render time.
import { describe, it, expect, beforeAll } from "vitest";

const _escapeHtml = (s: unknown) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
  );
(globalThis as any).escapeHtml = _escapeHtml;
(globalThis as any).window = (globalThis as any).window || globalThis;
(globalThis as any).window.escapeHtml = _escapeHtml;

import "./dashboard-plugins";

beforeAll(() => {
  (globalThis as any).escapeHtml = _escapeHtml;
});

const _renderZoteroStatusRow: (status: any) => string =
  (globalThis as any)._renderZoteroStatusRow ||
  (globalThis as any).window._renderZoteroStatusRow;

const _applyStaleOverrideDefault = (window as any)._applyStaleOverrideDefault as (
  btn: any,
) => void;

// Build the minimal row DOM the stale-override handler walks: a [data-lifecycle]
// ancestor holding a .tp-command input and the "Use new default" button.
function buildRow(opts: { command?: string; newer: string }) {
  const row = document.createElement("div");
  row.setAttribute("data-lifecycle", "installed_inactive");
  const input = document.createElement("input");
  input.className = "tp-command";
  input.type = "text";
  input.value = opts.command ?? "uvx docx-mcp-server";
  const btn = document.createElement("button");
  btn.className = "tp-reset-default";
  btn.setAttribute("data-newer", opts.newer);
  row.appendChild(input);
  row.appendChild(btn);
  return { row, input, btn };
}

describe("_renderZoteroStatusRow", () => {
  it("is exposed on the global after the module loads", () => {
    expect(typeof _renderZoteroStatusRow).toBe("function");
  });

  it("renders the reference-manager label and the /zotero slot tag", () => {
    const html = _renderZoteroStatusRow({ zotero_active: true });
    expect(html).toContain("Reference manager");
    expect(html).toContain("/zotero");
    expect(html).toContain("zotero-mcp");
  });

  it("maps zotero_active=true (no health issue) to the active state", () => {
    const html = _renderZoteroStatusRow({ zotero_active: true });
    expect(html).toContain('data-zotero-status="active"');
    expect(html).toContain(">active<");
    expect(html).toContain("var(--success");
  });

  it("maps zotero_active=false to installed_inactive (bundled but not connected)", () => {
    const html = _renderZoteroStatusRow({ zotero_active: false });
    expect(html).toContain('data-zotero-status="installed_inactive"');
    expect(html).toContain(">inactive<");
    expect(html).toContain("#f59e0b");
    expect(html).toContain("start tunnel to activate");
  });

  it("treats a connected-but-unhealthy slot as unhealthy, with the reason badge", () => {
    const html = _renderZoteroStatusRow({
      zotero_active: true,
      slot_status: {
        zotero: { reason: "preflight_failed", detail: "Zotero not running on 127.0.0.1:23119" },
      },
    });
    expect(html).toContain('data-zotero-status="unhealthy"');
    expect(html).toContain(">unhealthy<");
    expect(html).toContain('data-slot-warning="zotero"');
    expect(html).toContain("preflight failed");
    expect(html).toContain("Zotero not running on 127.0.0.1:23119");
  });

  it("ignores an unrelated slot's health entry (only reads slot_status.zotero)", () => {
    const html = _renderZoteroStatusRow({
      zotero_active: true,
      slot_status: { word: { reason: "access_denied" } },
    });
    expect(html).toContain('data-zotero-status="active"');
    expect(html).not.toContain("data-slot-warning");
  });

  it("defaults to not-detected when given an empty / missing payload", () => {
    expect(_renderZoteroStatusRow(undefined)).toContain('data-zotero-status="installed_inactive"');
    expect(_renderZoteroStatusRow({})).toContain('data-zotero-status="installed_inactive"');
  });
});

describe("_applyStaleOverrideDefault", () => {
  it("POPULATES the command input with the new default (does not clear it)", () => {
    const { input, btn } = buildRow({ command: "uvx docx-mcp-server", newer: "uvx docx-mcp" });
    _applyStaleOverrideDefault(btn);
    expect(input.value).toBe("uvx docx-mcp");
    expect(input.value).not.toBe("");
  });

  it("overwrites a stale command value with the new default", () => {
    const { input, btn } = buildRow({ command: "uvx word-mcp-live", newer: "uvx docx-mcp" });
    _applyStaleOverrideDefault(btn);
    expect(input.value).toBe("uvx docx-mcp");
  });

  it("fires an 'input' event so collectConfig picks up the change", () => {
    const { input, btn } = buildRow({ newer: "uvx docx-mcp" });
    let fired = 0;
    input.addEventListener("input", () => {
      fired += 1;
    });
    _applyStaleOverrideDefault(btn);
    expect(fired).toBe(1);
  });

  it("leaves the field untouched when there is no newer default (empty data-newer)", () => {
    const { input, btn } = buildRow({ command: "uvx custom-thing", newer: "" });
    _applyStaleOverrideDefault(btn);
    expect(input.value).toBe("uvx custom-thing");
  });

  it("is null-safe for a detached button with no [data-lifecycle] ancestor", () => {
    const orphan = document.createElement("button");
    orphan.setAttribute("data-newer", "uvx docx-mcp");
    expect(() => _applyStaleOverrideDefault(orphan)).not.toThrow();
  });

  it("is null-safe for undefined / non-element input", () => {
    expect(() => _applyStaleOverrideDefault(undefined)).not.toThrow();
    expect(() => _applyStaleOverrideDefault({} as any)).not.toThrow();
  });
});

describe("_renderStaleOverrideWarning wires the populate handler", () => {
  it("renders a button carrying the joined new default on data-newer (word slot)", () => {
    const render = (window as any)._renderStaleOverrideWarning as (p: any) => string;
    const html = render({
      slot: "word",
      stale_override: true,
      newer_default_command: ["uvx", "docx-mcp"],
      newer_default_label: "Word / DOCX authoring (docx-mcp)",
    });
    expect(html).toContain('data-newer="uvx docx-mcp"');
    expect(html).toContain("_applyStaleOverrideDefault");
    expect(html).not.toContain("c.value=''");
    expect(html).toContain("Use new default");
  });

  it("wires render -> click end-to-end: clicking populates the command field", () => {
    const render = (window as any)._renderStaleOverrideWarning as (p: any) => string;
    const host = document.createElement("div");
    host.setAttribute("data-lifecycle", "installed_inactive");
    const input = document.createElement("input");
    input.className = "tp-command";
    input.value = "uvx docx-mcp-server";
    host.appendChild(input);
    host.insertAdjacentHTML(
      "beforeend",
      render({
        slot: "word",
        stale_override: true,
        newer_default_command: ["uvx", "docx-mcp"],
      }),
    );
    const btn = host.querySelector(".tp-reset-default") as HTMLElement;
    _applyStaleOverrideDefault(btn);
    expect(input.value).toBe("uvx docx-mcp");
  });
});

// 678ec121 — enabled (stored config) and active (this session's live tunnel
// connection) are independently tracked and can genuinely disagree (confirmed
// live: word/desktop-commander showed enabled:false while active:true from one
// client). _pluginLifecycleState/_renderLifecycleBadge must surface that
// mismatch explicitly rather than silently rendering "active" next to an
// unchecked toggle with no explanation.
describe("_pluginLifecycleState / _renderLifecycleBadge — enabled/active mismatch", () => {
  const _pluginLifecycleState = (window as any)._pluginLifecycleState as (
    plugin: any,
    active: any,
    slotStatus?: any,
  ) => string;
  const _renderLifecycleBadge = (window as any)._renderLifecycleBadge as (
    plugin: any,
    lifecycleState: any,
    installCmd: any,
  ) => string;

  it("connected + enabled is plain active (unchanged baseline)", () => {
    const state = _pluginLifecycleState({ slot: "word", enabled: true }, { word: true });
    expect(state).toBe("active");
  });

  it("connected + explicitly disabled is a distinct active_disabled state, not plain active", () => {
    const state = _pluginLifecycleState({ slot: "word", enabled: false }, { word: true });
    expect(state).toBe("active_disabled");
  });

  it("connected + enabled unset (undefined, not explicitly false) stays plain active", () => {
    // Core/legacy rows may omit `enabled` entirely; only an explicit false
    // means "the user turned this off" — must not misfire on omission.
    const state = _pluginLifecycleState({ slot: "word" }, { word: true });
    expect(state).toBe("active");
  });

  it("unhealthy still takes priority over the enabled/active mismatch", () => {
    const state = _pluginLifecycleState(
      { slot: "word", enabled: false },
      { word: true },
      { word: { reason: "preflight_failed" } },
    );
    expect(state).toBe("unhealthy");
  });

  it("not connected + disabled is not_installed (unchanged baseline)", () => {
    const state = _pluginLifecycleState({ slot: "word", enabled: false }, { word: false });
    expect(state).toBe("not_installed");
  });

  it("renders a distinct label + explanatory hint for active_disabled, not the plain active badge", () => {
    const html = _renderLifecycleBadge({ slot: "word" }, "active_disabled", "");
    expect(html).toContain("active (disabled)");
    expect(html).toContain("disabled — still connected");
    expect(html).not.toContain(">active<"); // must not read as the plain "active" label
  });

  it("active_disabled keeps the same green connected dot as plain active", () => {
    const activeHtml = _renderLifecycleBadge({ slot: "word" }, "active", "");
    const mismatchHtml = _renderLifecycleBadge({ slot: "word" }, "active_disabled", "");
    const dotColor = "var(--success, #3fb950)";
    expect(activeHtml).toContain(dotColor);
    expect(mismatchHtml).toContain(dotColor);
  });
});
