// Unit tests for the strict-typed dashboard-mcp module (cb7d55ae).
import { describe, it, expect, beforeAll } from "vitest";
import { _renderToolEntry } from "./dashboard-mcp";

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
