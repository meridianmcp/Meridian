// The Project tab has its own member add form pinned to the current project, and
// the Account invite defaults to the current project instead of all projects.
// Source-level checks, matching dashboard-settings.test.ts.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(
  resolve(process.cwd(), "meridian/static/dashboard-settings.ts"),
  "utf8",
);

describe("project-scoped member add in the Project tab", () => {
  it("renders an add-to-project form with per-project ids", () => {
    expect(source).toContain('id="project-invite-section-${projectId}"');
    expect(source).toContain('id="project-invite-email-${projectId}"');
    expect(source).toContain('id="project-invite-btn-${projectId}"');
  });

  it("posts the invite with project_id pinned to the current project", () => {
    expect(source).toMatch(/project-invite-btn-\$\{projectId\}[\s\S]{0,1400}project_id: projectId/);
  });

  it("is hidden from guests the same way the Account invite is", () => {
    expect(source).toContain("settings-project-invite-form-${projectId}");
    expect(source).toMatch(/projectInviteForm\.style\.display = 'none'/);
  });

  it("refreshes the Account members list after an invite", () => {
    expect(source).toContain("(window as any)._membersRefresh[projectId] = () => renderMembers();");
  });

  it("is not routed to the Account tab (the classifier keys on members-section)", () => {
    expect("project-invite-section-p1".includes("members-section")).toBe(false);
  });
});

describe("Account invite defaults to the current project", () => {
  it("selects the current project in the scope dropdown", () => {
    expect(source).toContain("${p.id === projectId ? ' selected' : ''}");
  });

  it("labels the blank option as the whole workspace, not a silent default", () => {
    expect(source).toContain('<option value="">whole workspace (all projects)</option>');
    expect(source).not.toContain('<option value="">all projects</option>');
  });
});
