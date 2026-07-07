// Unit tests for dashboard-settings markup + helpers.
//
// 2ff2ff1f — the Handoff Format textarea (id="ws-handoff-template") must default
// to a tall height (rows="16") and stay vertically resizable.
// f2157803 — the executor-config context_threshold / max_turns controls are
// number inputs (exact value always visible + directly editable), not sliders.
// ca8c0d56 — the Claude Code (rc-watcher) and Codex CLI setup blurbs are terse
// single-liners leading with the copy-paste config.
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  _execTurnsNumberInputHtml,
  claudeRcWatcherBlurb,
  codexSetupBlurb,
} from "./dashboard-settings";

// Vitest runs with cwd = repo root, so resolve the source module from there.
const source = readFileSync(
  resolve(process.cwd(), "meridian/static/dashboard-settings.ts"),
  "utf8",
);

// Isolate the exact <textarea id="ws-handoff-template" ...> opening tag.
const textareaTag =
  source.match(/<textarea id="ws-handoff-template"[^>]*>/)?.[0] ?? "";

// Minimal stand-in for the ambient escapeHtml used at runtime.
const esc = (s: unknown) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
  );

beforeAll(() => {
  (window as any).escapeHtml = esc;
});

function parse(html: string): HTMLInputElement {
  const el = document.createElement("div");
  el.innerHTML = html;
  const input = el.querySelector("input");
  if (!input) throw new Error("no input rendered");
  return input as HTMLInputElement;
}

// Strip inline tags so length/word checks measure the visible copy.
const visible = (html: string) => html.replace(/<[^>]+>/g, "");

describe("ws-handoff-template textarea", () => {
  it("exists in the settings source", () => {
    expect(textareaTag).not.toBe("");
  });

  it("defaults to a tall height (rows=16), not the old cramped rows=6", () => {
    expect(textareaTag).toContain('rows="16"');
    expect(textareaTag).not.toContain('rows="6"');
  });

  it("stays vertically resizable", () => {
    expect(textareaTag).toContain("resize:vertical");
  });
});

describe("_execTurnsNumberInputHtml", () => {
  it("renders a number input (not a range slider) so the value is directly editable", () => {
    const input = parse(_execTurnsNumberInputHtml("context_threshold", "p1", 60, 10, 200, 5));
    expect(input.getAttribute("type")).toBe("number");
    expect(input.getAttribute("type")).not.toBe("range");
  });

  it("preserves the exact setting key in the element id (state binding unchanged)", () => {
    const ctx = parse(_execTurnsNumberInputHtml("context_threshold", "proj-42", 60, 10, 200, 5));
    const mt = parse(_execTurnsNumberInputHtml("max_turns", "proj-42", 120, 40, 500, 20));
    expect(ctx.id).toBe("exec-context_threshold-proj-42");
    expect(mt.id).toBe("exec-max_turns-proj-42");
  });

  it("shows the exact current value in the input so it is always visible", () => {
    const input = parse(_execTurnsNumberInputHtml("context_threshold", "p1", 85, 10, 200, 5));
    expect(input.getAttribute("value")).toBe("85");
    expect(input.value).toBe("85");
  });

  it("carries sensible min/max/step bounds matching the clamp on save", () => {
    const ctx = parse(_execTurnsNumberInputHtml("context_threshold", "p1", 60, 10, 200, 5));
    expect(ctx.getAttribute("min")).toBe("10");
    expect(ctx.getAttribute("max")).toBe("200");
    expect(ctx.getAttribute("step")).toBe("5");

    const mt = parse(_execTurnsNumberInputHtml("max_turns", "p1", 120, 40, 500, 20));
    expect(mt.getAttribute("min")).toBe("40");
    expect(mt.getAttribute("max")).toBe("500");
    expect(mt.getAttribute("step")).toBe("20");
  });

  it("advertises a numeric mobile keyboard via inputmode", () => {
    const input = parse(_execTurnsNumberInputHtml("max_turns", "p1", 120, 40, 500, 20));
    expect(input.getAttribute("inputmode")).toBe("numeric");
  });

  it("HTML-escapes the rendered value to avoid attribute injection", () => {
    const html = _execTurnsNumberInputHtml("context_threshold", "p1", '"><img>', 10, 200, 5);
    expect(html).not.toContain('"><img>');
    expect(html).toContain("&quot;&gt;&lt;img&gt;");
  });

  it("is registered on window for the settings template to call", () => {
    expect(typeof (window as any)._execTurnsNumberInputHtml).toBe("function");
  });
});

describe("claudeRcWatcherBlurb", () => {
  const html = claudeRcWatcherBlurb();

  it("still explains the claude --rc / headless hook gap", () => {
    expect(html).toContain("claude --rc");
    expect(html.toLowerCase()).toContain("hook");
  });

  it("is a single terse line, not a multi-sentence paragraph", () => {
    expect(html).not.toContain("\n");
    expect(html).not.toContain("<p");
    expect(visible(html).length).toBeLessThan(180);
    expect((visible(html).match(/\.(\s|$)/g) || []).length).toBeLessThanOrEqual(1);
  });
});

describe("codexSetupBlurb", () => {
  const url = "https://usemeridian.us/mcp";
  const html = codexSetupBlurb(url, esc);

  it("keeps the essential config-file + one-command instruction intact", () => {
    expect(html).toContain("~/.codex/config.toml");
    expect(html).toContain("codex mcp add meridian");
    expect(html).toContain(url);
  });

  it("routes the url through the provided escaper (no unescaped injection)", () => {
    const out = codexSetupBlurb('x"><script>', esc);
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("is a single terse line, not a multi-sentence paragraph", () => {
    expect(html).not.toContain("\n");
    expect(visible(html).length).toBeLessThan(120);
    expect((visible(html).match(/\.(\s|$)/g) || []).length).toBeLessThanOrEqual(1);
  });
});
