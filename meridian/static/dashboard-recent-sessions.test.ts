// Behavioural test of the Recent Sessions Resume button in dashboard.ts (loadRecentSessions).
//
// The Resume button copies a start_session(...) line that a person pastes into an agent. Both the
// session name and the project id are written into that line, and both are copied text, so both
// must go through escapeQuotedLiteral: a value holding a quote or a backslash must not end the
// literal early. This file loads the REAL dashboard.ts (with the real dashboard.html markup,
// which its module-level wiring needs), calls the window-exposed loadRecentSessions, and reads
// exactly what the Resume button writes to the clipboard.
import { beforeAll, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const w = window as any;

function installPage(): void {
  const html = readFileSync(resolve(__dirname, "../templates/dashboard.html"), "utf8")
    .replace(/\{\{[\s\S]*?\}\}/g, "")
    .replace(/\{%[\s\S]*?%\}/g, "");
  // Drop <script> elements with the DOM parser, not a regex (see dashboard-goal-wiring.test.ts).
  const parsed = new DOMParser().parseFromString(html, "text/html");
  parsed.querySelectorAll("script").forEach((s) => s.remove());
  document.body.innerHTML = parsed.body.innerHTML;
  // Module-level wiring fetches a few lists on load. Answer every request with an empty payload.
  (globalThis as any).fetch = async (url: unknown) => {
    const path = String(url).split("?")[0];
    const body = /\/(projects|sessions|tasks|notes|hitl|files|documents|insights)$/.test(path) ? [] : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  };
  (globalThis as any).WebSocket = class {
    close() {}
    addEventListener() {}
    send() {}
  };
}

describe("Recent Sessions Resume button (dashboard.ts loadRecentSessions)", () => {
  beforeAll(async () => {
    installPage();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(() => Promise.resolve()) },
      configurable: true,
    });
    await import("./dashboard");
  }, 60000);

  it("copies a start_session line whose project id is escaped like the session name", async () => {
    // A project id holding a quote and a backslash. A real id is a UUID, but the copied literal
    // must stay closed for any value the page was built with.
    const projectId = 'p"1\\x';
    const host = document.createElement("div");
    host.id = `recent-sessions-${projectId}`;
    document.body.appendChild(host);

    await w.loadRecentSessions(projectId, [
      { id: "s1", name: "resume-me", human_id: "adam", status: "closed", last_seen: "2026-10-01 10:00:00" },
    ]);
    const button = host.querySelector<HTMLButtonElement>(".resume-session-btn");
    expect(button).not.toBeNull();
    button!.click();

    const clipboard = navigator.clipboard.writeText as unknown as ReturnType<typeof vi.fn>;
    await vi.waitFor(() => expect(clipboard).toHaveBeenCalledTimes(1), { timeout: 3000 });
    expect(clipboard.mock.calls[0]![0]).toBe(
      'start_session(project_id="p\\"1\\\\x", session_name="resume-me", human_id="adam")',
    );
  }, 60000);
});
