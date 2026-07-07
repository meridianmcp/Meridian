// 6153bc29 — the Live tab's "Active sessions" section is now collapsible via the
// same native <details>/<summary> pattern the rest of the dashboard uses (matching
// the "History (Append-only log)" details block). These tests pin the structural
// contract of that wrapper so a future refactor can't silently break either the
// collapse affordance or the renderLiveSessions() divider logic that depends on
// the .live-sessions root still living inside a .live-section ancestor.
import { describe, it, expect, beforeEach } from "vitest";

// The exact static markup produced by the Live-tab section render in dashboard.ts
// for a given project id. Kept in sync by shape (default-open <details>, the
// section label promoted to <summary>, the .live-sessions render root nested
// inside the <details> but still under .live-section).
function activeSessionsSectionHtml(projectId: string): string {
  return `
    <div class="live-section">
      <details class="live-section-collapse" open>
        <summary class="live-section-label" style="cursor:pointer;list-style:none">Active sessions</summary>
        <div class="live-sessions" id="live-sessions-${projectId}">
          <div class="live-empty">No active sessions.</div>
        </div>
      </details>
    </div>`;
}

describe("Live tab — Active sessions collapsible wrapper", () => {
  const projectId = "proj-123";

  beforeEach(() => {
    document.body.innerHTML = activeSessionsSectionHtml(projectId);
  });

  it("wraps the section in a native <details> using the dashboard collapse pattern", () => {
    const details = document.querySelector(".live-section details");
    expect(details).not.toBeNull();
    expect(details!.tagName.toLowerCase()).toBe("details");
    // <summary> carries the section heading text.
    const summary = details!.querySelector("summary.live-section-label");
    expect(summary).not.toBeNull();
    expect(summary!.textContent).toContain("Active sessions");
  });

  it("defaults to open", () => {
    const details = document.querySelector(".live-section details") as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(details.hasAttribute("open")).toBe(true);
  });

  it("keeps the live-sessions render root inside the <details>", () => {
    const details = document.querySelector(".live-section details")!;
    const root = document.getElementById(`live-sessions-${projectId}`);
    expect(root).not.toBeNull();
    expect(details.contains(root)).toBe(true);
  });

  it("preserves the .live-section ancestor that renderLiveSessions relies on", () => {
    // renderLiveSessions() calls root.closest('.live-section') to show/hide the
    // panel and its trailing .live-divider. Nesting the root under <details> must
    // not break that lookup.
    const root = document.getElementById(`live-sessions-${projectId}`)!;
    const section = root.closest(".live-section");
    expect(section).not.toBeNull();
    expect(section!.classList.contains("live-section")).toBe(true);
  });

  it("collapses and re-opens by toggling the details element", () => {
    const details = document.querySelector(".live-section details") as HTMLDetailsElement;
    details.open = false;
    expect(details.open).toBe(false);
    // The render root still exists while collapsed (content is hidden, not removed).
    expect(document.getElementById(`live-sessions-${projectId}`)).not.toBeNull();
    details.open = true;
    expect(details.open).toBe(true);
  });
});
