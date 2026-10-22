// The Project tab adds a member to this project with a check first: it looks the
// email up among workspace members, reports an existing workspace or project
// membership, and offers to elevate a project-scoped member to the whole workspace.
// The Account invite defaults to the current project. Source-level checks, matching
// dashboard-settings.test.ts.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(
  resolve(process.cwd(), "meridian/static/dashboard-settings.ts"),
  "utf8",
);

describe("project-scoped member add in the Project tab", () => {
  it("renders a check control and an actions area with per-project ids", () => {
    expect(source).toContain('id="project-invite-section-${projectId}"');
    expect(source).toContain('id="project-invite-email-${projectId}"');
    expect(source).toContain('id="project-invite-check-${projectId}"');
    expect(source).toContain('id="project-invite-actions-${projectId}"');
  });

  it("looks the email up among workspace members before acting", () => {
    expect(source).toContain("api('/workspace/members')");
    expect(source).toContain("already a workspace member");
    expect(source).toContain("already a member of this project");
  });

  it("invites with project_id pinned to the current project", () => {
    expect(source).toMatch(/\/workspace\/invite', \{ method: 'POST', body: JSON\.stringify\(\{ email, role: roleEl!\.value \|\| 'member', project_id: projectId \}\)/);
  });

  it("elevates a project-scoped member by clearing project_id, not by re-inviting", () => {
    expect(source).toContain("api(`/workspace/members/${memberId}`, { method: 'PATCH', body: JSON.stringify({ project_id: null }) })");
    expect(source).toContain("Make workspace member");
  });

  it("is hidden from guests the same way the Account invite is", () => {
    expect(source).toContain("settings-project-invite-form-${projectId}");
    expect(source).toMatch(/projectInviteForm\.style\.display = 'none'/);
  });

  it("refreshes the Account members list after a change", () => {
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
