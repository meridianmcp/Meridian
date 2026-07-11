// Unit tests for the notes "Load More" button state (7fa07e10).
//
// Regression guard for the bug where the count label showed the UNFILTERED
// library total (e.g. "100 of 150") even when the active tab/kind/tag/search
// filter matched ZERO loaded notes. The count/label + button visibility must be
// derived from the SAME filtered set as the rendered list, not the server's
// separate unfiltered COUNT.
import { describe, expect, it } from "vitest";
import { notesLoadMoreState, type NotesLoadMoreInput } from "./dashboard-notes";

const base: NotesLoadMoreInput = {
  visibleCount: 0,
  loadedCount: 0,
  hasMore: false,
  totalCount: 0,
  remaining: 0,
  filterActive: false,
  pageSize: 100,
};

describe("notesLoadMoreState — no client filter (server totals valid)", () => {
  it("shows the real (loaded of total) label with the real remaining count", () => {
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 100,
      loadedCount: 100,
      hasMore: true,
      totalCount: 150,
      remaining: 50,
      filterActive: false,
    });
    expect(st.show).toBe(true);
    expect(st.label).toContain("Load 50 more");
    expect(st.label).toContain("(100 of 150)");
  });

  it("clamps 'Load N more' to the page size when remaining exceeds it", () => {
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 100,
      loadedCount: 100,
      hasMore: true,
      totalCount: 500,
      remaining: 400,
      filterActive: false,
    });
    expect(st.label).toContain("Load 100 more"); // pageSize=100, not 400
    expect(st.label).toContain("(100 of 500)");
  });

  it("hides the button when the server reports no more pages", () => {
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 30,
      loadedCount: 30,
      hasMore: false,
      totalCount: 30,
      remaining: 0,
      filterActive: false,
    });
    expect(st.show).toBe(false);
    expect(st.label).toBe("");
  });
});

describe("notesLoadMoreState — client filter active (the 7fa07e10 bug)", () => {
  it("ZERO matches: never shows the unfiltered library total (no '0 of 150')", () => {
    // The reported case: search/tab filter matches zero loaded notes, but the
    // server still has more unfiltered pages.
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 0,
      loadedCount: 100,
      hasMore: true,
      totalCount: 150,
      remaining: 50,
      filterActive: true,
    });
    // Must NOT present the library total as if it were the filtered total.
    expect(st.label).not.toContain("of 150");
    expect(st.label).not.toContain("100 of");
    expect(st.label).not.toMatch(/\d+ of \d+/);
    // May still offer to search the unloaded pages, but reports only what matched.
    expect(st.label).toContain("0 matches");
  });

  it("ZERO matches + nothing more to load: button is hidden entirely", () => {
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 0,
      loadedCount: 100,
      hasMore: false,
      totalCount: 150,
      remaining: 50,
      filterActive: true,
    });
    expect(st.show).toBe(false);
    expect(st.label).toBe("");
  });

  it("some matches: label reflects the FILTERED count, not the library total", () => {
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 3,
      loadedCount: 100,
      hasMore: true,
      totalCount: 150,
      remaining: 50,
      filterActive: true,
    });
    expect(st.label).not.toMatch(/\d+ of \d+/); // no "100 of 150"
    expect(st.label).toContain("3 matches");
  });

  it("exactly one match: uses singular '1 match'", () => {
    const st = notesLoadMoreState({
      ...base,
      visibleCount: 1,
      loadedCount: 100,
      hasMore: true,
      totalCount: 150,
      remaining: 50,
      filterActive: true,
    });
    expect(st.label).toContain("1 match");
    expect(st.label).not.toContain("1 matches");
  });
});
