// Unit tests for the strict-typed dashboard-mcp module (cb7d55ae).
import { describe, it, expect, beforeAll } from "vitest";
import {
  _renderToolEntry,
  _groupToolsByCategory,
  _renderToolSections,
} from "./dashboard-mcp";

beforeAll(() => {
  // _renderToolEntry calls the global escapeHtml (defined in dashboard-utils at
  // runtime via window). Provide a minimal stand-in for the unit test.
  (globalThis as any).escapeHtml = (s: unknown) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
    );
});

describe("_renderToolEntry", () => {
  it("renders the tool name, description, and a typed param table", () => {
    const html = _renderToolEntry({
      name: "do_thing",
      description: "Does a <thing>",
      inputSchema: {
        properties: { x: { type: "string", description: "the x" }, y: { type: "number" } },
        required: ["x"],
      },
    });
    expect(html).toContain("do_thing");
    expect(html).toContain("Does a &lt;thing&gt;"); // escaped
    expect(html).toContain("the x");
    expect(html).toContain("required"); // x is required
    expect(html).toContain("optional"); // y is optional
    expect(html).toContain("<table");
    // Signature marks optional params with a trailing '?'.
    expect(html).toContain("y?");
  });

  it("renders without a param table when the tool has no properties", () => {
    const html = _renderToolEntry({ name: "ping", description: "", inputSchema: {} });
    expect(html).toContain("ping");
    expect(html).not.toContain("<table");
  });

  it("defaults a missing param type to 'any'", () => {
    const html = _renderToolEntry({
      name: "t",
      inputSchema: { properties: { z: { description: "no type" } } },
    });
    expect(html).toContain("any");
  });
});

// 70ac52e4 — grouping/section helpers that turn the flat tool list into
// collapsible <details> sections per category.
describe("_groupToolsByCategory", () => {
  const cats = {
    goal: ["set_goal", "get_goal"],
    notes: ["add_note", "get_notes"],
  };

  it("buckets tools into their categories, in category order", () => {
    const tools = [
      { name: "get_notes" },
      { name: "set_goal" },
      { name: "add_note" },
      { name: "get_goal" },
    ];
    const groups = _groupToolsByCategory(tools, cats);
    expect(groups.map((g) => g.key)).toEqual(["goal", "notes"]);
    // Within a category, tools follow the category's declared name order.
    expect(groups[0].tools.map((t: any) => t.name)).toEqual(["set_goal", "get_goal"]);
    expect(groups[1].tools.map((t: any) => t.name)).toEqual(["add_note", "get_notes"]);
  });

  it("drops categories that have no matching tools", () => {
    const groups = _groupToolsByCategory([{ name: "set_goal" }], cats);
    expect(groups.map((g) => g.key)).toEqual(["goal"]);
  });

  it("routes uncategorized tools into a trailing 'other' bucket (nothing dropped)", () => {
    const tools = [{ name: "set_goal" }, { name: "mystery_tool" }];
    const groups = _groupToolsByCategory(tools, cats);
    expect(groups.map((g) => g.key)).toEqual(["goal", "other"]);
    const total = groups.reduce((n, g) => n + g.tools.length, 0);
    expect(total).toBe(tools.length);
    expect(groups[groups.length - 1].tools.map((t: any) => t.name)).toEqual(["mystery_tool"]);
  });

  it("tolerates empty/absent input without throwing", () => {
    expect(_groupToolsByCategory([], cats)).toEqual([]);
    expect(_groupToolsByCategory(undefined as any, cats)).toEqual([]);
  });
});

describe("_renderToolSections", () => {
  const cats = { goal: ["set_goal"], notes: ["add_note"] };
  const labels = { goal: "Goal Tools", notes: "Notes Tools", other: "Other" };

  it("renders one collapsible <details> section per non-empty category", () => {
    const tools = [{ name: "set_goal" }, { name: "add_note" }];
    const html = _renderToolSections(tools, cats, labels);
    const detailsCount = (html.match(/<details/g) || []).length;
    expect(detailsCount).toBe(2);
    // Sections default open so the tab is fully visible and search still matches.
    expect(html).toContain("<details open");
    expect(html).toContain("<summary");
    // Category labels + per-category counts are shown in the summary.
    expect(html).toContain("Goal Tools");
    expect(html).toContain("Notes Tools");
    expect(html).toContain("(1)");
    // Each section wraps the underlying tool-entry markup.
    expect(html).toContain('class="tool-entry"');
    expect(html).toContain("set_goal");
  });

  it("labels the catch-all bucket 'Other' and includes uncategorized tools", () => {
    const html = _renderToolSections([{ name: "loose_tool" }], cats, labels);
    expect(html).toContain('data-category="other"');
    expect(html).toContain("Other");
    expect(html).toContain("loose_tool");
  });

  it("returns an empty string when there are no tools", () => {
    expect(_renderToolSections([], cats, labels)).toBe("");
  });
});
