// Unit tests for the workspace Blog editor builders (e553fa7a).
// Covers the BUG fix: a saved draft can be re-opened in the editor form and
// re-saved in place. Tests the pure HTML builders + the DOM populate/reset
// helpers that back the per-post "Edit" affordance.
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import {
  blogEditorFormHtml,
  blogPostCardHtml,
  populateBlogEditor,
  resetBlogEditor,
} from "./dashboard-blog";

beforeAll(() => {
  // The builders call the global escapeHtml (dashboard-utils at runtime).
  (globalThis as any).escapeHtml = (s: unknown) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
    );
});

describe("blogEditorFormHtml", () => {
  it("renders an empty 'new post' form when no post is given", () => {
    const html = blogEditorFormHtml("proj1", null);
    expect(html).toContain("NEW POST");
    expect(html).toContain('id="blog-editor-title-input-proj1"');
    expect(html).toContain('value=""'); // hidden id + title empty
    expect(html).toContain("Save post");
    // reset button hidden in create mode
    expect(html).toContain("display:none");
  });

  it("pre-populates the form from an existing post (edit mode)", () => {
    const html = blogEditorFormHtml("proj1", {
      id: "p-42",
      title: "My Draft",
      body_md: "hello **world**",
      status: "draft",
    });
    expect(html).toContain("EDIT POST");
    expect(html).toContain('id="blog-editor-id-proj1"');
    expect(html).toContain('value="p-42"'); // hidden id carries the post id
    expect(html).toContain('value="My Draft"'); // title input value
    expect(html).toContain("hello **world**"); // body in the textarea
    expect(html).toContain("Update post"); // save button label flips
  });

  it("escapes user content in the populated form", () => {
    const html = blogEditorFormHtml("proj1", {
      id: "p1",
      title: `<script>"x"</script>`,
      body_md: "<b>b</b>",
    });
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("&lt;b&gt;b&lt;/b&gt;");
  });

  it("selects the post's status in the dropdown", () => {
    const html = blogEditorFormHtml("proj1", { id: "p1", title: "T", status: "published" });
    expect(html).toContain('<option value="published" selected>published</option>');
  });
});

describe("blogPostCardHtml", () => {
  it("renders an Edit button carrying the post id", () => {
    const html = blogPostCardHtml({ id: "p-7", title: "Post 7", slug: "post-7" });
    expect(html).toContain('class="blog-edit-btn secondary"');
    expect(html).toContain('data-blog-id="p-7"');
    expect(html).toContain(">Edit</button>");
    expect(html).toContain("Post 7");
    expect(html).toContain("/blog/post-7");
  });

  it("falls back to 'Untitled' and omits the link when there is no slug/url", () => {
    const html = blogPostCardHtml({ id: "p-8" });
    expect(html).toContain("Untitled");
    expect(html).not.toContain("<a ");
  });
});

describe("populateBlogEditor / resetBlogEditor (DOM)", () => {
  const PID = "projX";
  beforeEach(() => {
    document.body.innerHTML = blogEditorFormHtml(PID, null);
  });

  it("repopulates the editor form fields from a draft (the bug fix)", () => {
    const ok = populateBlogEditor(PID, {
      id: "d-1",
      title: "Editable Draft",
      body_md: "body text here",
      status: "archived",
    });
    expect(ok).toBe(true);

    const idEl = document.getElementById(`blog-editor-id-${PID}`) as HTMLInputElement;
    const titleEl = document.getElementById(`blog-editor-title-input-${PID}`) as HTMLInputElement;
    const bodyEl = document.getElementById(`blog-editor-body-${PID}`) as HTMLTextAreaElement;
    const statusEl = document.getElementById(`blog-editor-status-${PID}`) as HTMLSelectElement;
    const labelEl = document.getElementById(`blog-editor-title-${PID}`)!;
    const saveEl = document.getElementById(`blog-editor-save-${PID}`)!;

    expect(idEl.value).toBe("d-1");
    expect(titleEl.value).toBe("Editable Draft");
    expect(bodyEl.value).toBe("body text here");
    expect(statusEl.value).toBe("archived");
    expect(labelEl.textContent).toBe("EDIT POST");
    expect(saveEl.textContent).toBe("Update post");
  });

  it("returns false when the editor form is not in the DOM", () => {
    document.body.innerHTML = "";
    expect(populateBlogEditor(PID, { id: "x", title: "t" })).toBe(false);
  });

  it("resets the form back to a clean 'new post' state", () => {
    populateBlogEditor(PID, { id: "d-1", title: "T", body_md: "B", status: "published" });
    resetBlogEditor(PID);

    const idEl = document.getElementById(`blog-editor-id-${PID}`) as HTMLInputElement;
    const titleEl = document.getElementById(`blog-editor-title-input-${PID}`) as HTMLInputElement;
    const bodyEl = document.getElementById(`blog-editor-body-${PID}`) as HTMLTextAreaElement;
    const statusEl = document.getElementById(`blog-editor-status-${PID}`) as HTMLSelectElement;
    const saveEl = document.getElementById(`blog-editor-save-${PID}`)!;

    expect(idEl.value).toBe("");
    expect(titleEl.value).toBe("");
    expect(bodyEl.value).toBe("");
    expect(statusEl.value).toBe("draft");
    expect(saveEl.textContent).toBe("Save post");
  });
});
