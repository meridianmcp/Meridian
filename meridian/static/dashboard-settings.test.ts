// Unit tests for dashboard-settings markup (2ff2ff1f).
//
// The Handoff Format textarea (id="ws-handoff-template") is authored inline in
// the large, side-effectful loadSettingsTab() render function. Rather than drive
// that whole DOM/fetch path, we assert directly on the module source for the
// one element this item owns: it must default to a tall height (rows="16", up
// from the old too-small rows="6") and stay vertically resizable so a full
// 7-placeholder custom template is usable without scrolling.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Vitest runs with cwd = repo root (config include path is repo-relative), so
// resolve the source module from there rather than import.meta.url — the latter
// is not guaranteed to be a file: URL under the jsdom transform.
const source = readFileSync(
  resolve(process.cwd(), "meridian/static/dashboard-settings.ts"),
  "utf8",
);

// Isolate the exact <textarea id="ws-handoff-template" ...> opening tag.
const textareaTag =
  source.match(/<textarea id="ws-handoff-template"[^>]*>/)?.[0] ?? "";

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
