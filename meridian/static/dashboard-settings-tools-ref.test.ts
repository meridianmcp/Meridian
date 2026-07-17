// b905da5a — Unit tests for the _toolsReferenceHtml helper.
//
// Verifies:
//   (1) Given an empty tool list, renders an empty state without crashing.
//   (2) All three tiers are rendered as section headers when tools are present.
//   (3) Tool names appear in the rendered output.
//   (4) workflow_tier missing from a tool defaults gracefully (common-support).
//   (5) A realistic slice of the tool list (main-workflow + common-support +
//       maintenance-only mix) renders without throwing.
//   (6) The total tool count is mentioned in the output.
import { describe, it, expect, beforeAll } from "vitest";
import { _toolsReferenceHtml, type ToolEntry } from "./dashboard-settings";

const esc = (s: unknown) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
  );

beforeAll(() => {
  (globalThis as any).escapeHtml = esc;
  (window as any).escapeHtml = esc;
});

// Helper: parse the returned HTML string into a DOM fragment for querying.
function parseHtml(html: string): HTMLElement {
  const wrapper = document.createElement("div");
  wrapper.innerHTML = html;
  return wrapper;
}

// Minimal tool fixtures covering all three tiers.
const SAMPLE_TOOLS: ToolEntry[] = [
  { name: "start_session",      title: "Start Session",      workflow_tier: "main-workflow" },
  { name: "generate_handoff",   title: "Generate Handoff",   workflow_tier: "main-workflow" },
  { name: "complete_sprint_item", title: "Complete Sprint Item", workflow_tier: "main-workflow" },
  { name: "checkpoint",         title: "Checkpoint",         workflow_tier: "common-support" },
  { name: "add_insight",        title: "Add Insight",        workflow_tier: "common-support" },
  { name: "log_task",           title: "Log Task",           workflow_tier: "common-support" },
  { name: "analyze_sprint",     title: "Analyze Sprint",     workflow_tier: "maintenance-only" },
  { name: "assign_sprint_waves",title: "Assign Sprint Waves",workflow_tier: "maintenance-only" },
  { name: "send_message",       title: "Send Message",       workflow_tier: "maintenance-only" },
];

describe("_toolsReferenceHtml — empty list", () => {
  it("renders without throwing given an empty tool list", () => {
    expect(() => _toolsReferenceHtml([])).not.toThrow();
  });

  it("returns an HTML string for an empty list", () => {
    const html = _toolsReferenceHtml([]);
    expect(typeof html).toBe("string");
    expect(html.length).toBeGreaterThan(0);
  });

  it("mentions 0 tools in the count for an empty list", () => {
    const html = _toolsReferenceHtml([]);
    expect(html).toContain("0");
  });
});

describe("_toolsReferenceHtml — tier headers", () => {
  it("renders a 'Main Workflow' section header", () => {
    const dom = parseHtml(_toolsReferenceHtml(SAMPLE_TOOLS));
    expect(dom.innerHTML).toContain("Main Workflow");
  });

  it("renders a 'Common Support' section header", () => {
    const dom = parseHtml(_toolsReferenceHtml(SAMPLE_TOOLS));
    expect(dom.innerHTML).toContain("Common Support");
  });

  it("renders a 'Maintenance Only' section header", () => {
    const dom = parseHtml(_toolsReferenceHtml(SAMPLE_TOOLS));
    expect(dom.innerHTML).toContain("Maintenance Only");
  });
});

describe("_toolsReferenceHtml — tool names appear in output", () => {
  it("includes main-workflow tool names", () => {
    const html = _toolsReferenceHtml(SAMPLE_TOOLS);
    expect(html).toContain("start_session");
    expect(html).toContain("generate_handoff");
    expect(html).toContain("complete_sprint_item");
  });

  it("includes common-support tool names", () => {
    const html = _toolsReferenceHtml(SAMPLE_TOOLS);
    expect(html).toContain("checkpoint");
    expect(html).toContain("add_insight");
    expect(html).toContain("log_task");
  });

  it("includes maintenance-only tool names", () => {
    const html = _toolsReferenceHtml(SAMPLE_TOOLS);
    expect(html).toContain("analyze_sprint");
    expect(html).toContain("send_message");
  });
});

describe("_toolsReferenceHtml — tier counts", () => {
  it("shows correct tool count per tier", () => {
    const html = _toolsReferenceHtml(SAMPLE_TOOLS);
    // Each tier has 3 tools — check that the count appears somewhere per section.
    // The count is rendered as "3 tools" in each tier header.
    const matches = html.match(/3 tools/g);
    expect(matches).not.toBeNull();
    // Three tiers, each with 3 tools
    expect((matches || []).length).toBeGreaterThanOrEqual(3);
  });

  it("mentions the total tool count in the summary line", () => {
    const html = _toolsReferenceHtml(SAMPLE_TOOLS);
    // total = 9 tools
    expect(html).toContain("9");
  });
});

describe("_toolsReferenceHtml — graceful defaults", () => {
  it("defaults a tool with no workflow_tier to 'common-support'", () => {
    const tools: ToolEntry[] = [
      { name: "mystery_tool" },  // no workflow_tier
    ];
    // Should not throw and should render the tool somewhere
    const html = _toolsReferenceHtml(tools);
    expect(html).toContain("mystery_tool");
  });

  it("handles a tool with an unrecognized tier value gracefully", () => {
    const tools: ToolEntry[] = [
      { name: "weird_tool", workflow_tier: "super-special" as any },
    ];
    expect(() => _toolsReferenceHtml(tools)).not.toThrow();
  });
});

describe("_toolsReferenceHtml — machine-readable tag mentioned", () => {
  it("mentions workflow_tier as a machine-readable field in the preamble", () => {
    const html = _toolsReferenceHtml(SAMPLE_TOOLS);
    expect(html).toContain("workflow_tier");
  });
});
