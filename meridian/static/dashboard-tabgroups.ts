// dashboard-tabgroups.ts (2d3b8424)
// ---------------------------------------------------------------------------
// IA / navigation grouping for the left vtab rail.
//
// The dashboard historically rendered ~18 FLAT top-level vtab buttons in one
// vertical rail — too chaotic to scan. This module GROUPS those tabs into a few
// logical groups (Overview / Planning / Work / Content / History), reusing the
// SAME nested-subtab idea the Goal tab already uses for its data-gtab subtabs: a
// small header per group, with the group's member tabs nested under it. It is
// PURE IA — every original tab still renders (nothing dropped), every data-vtab
// is unchanged, and each tab's panel/content is untouched.
//
// Where the markup lives: the grouped rail markup is emitted INLINE in
// buildTabBody (dashboard.ts) as literal <button data-vtab="…"> elements wrapped
// in `.vtab-group` containers. It is kept literal (not generated here) so the
// source-scanning UI tests that assert `data-vtab="X"` exists in dashboard.ts
// keep matching. This module owns:
//   * VTAB_GROUPS — the single source of truth for group membership (asserted by
//     the vitest suite so the inline DOM and this model can't silently drift).
//   * groupForTab — map a data-vtab id to its group.
//   * wireVtabGroups — collapse/expand the group headers, and expose
//     revealGroupForTab so programmatic navigation always re-expands a
//     (possibly user-collapsed) group before landing on one of its tabs.
//
// Why groups render EXPANDED by default (never hidden on first paint): external
// code navigates by clicking `.vtab-btn[data-vtab="X"]` directly (demo tour,
// deep-links, HITL/timeline jumps, and the Playwright UX tests). Those clicks
// must keep working, and the demo tour measures the button's bounding box, so
// every button must stay laid out on load. Collapsing is a user action;
// revealGroupForTab undoes it just-in-time before a navigation.

/** A group of tabs, shown as one collapsible header in the rail. */
export interface VtabGroup {
  /** Stable group id (data-vgroup). */
  id: string;
  /** Short human label shown on the group header. */
  label: string;
  /** data-vtab ids in this group, in display order. */
  tabs: readonly string[];
}

/**
 * The canonical grouping. Every original flat tab appears in exactly one group.
 * The FIRST group's FIRST tab (status) stays the default-active tab, matching
 * the previous rail order (status was the first flat button, marked active).
 *
 * Keep this in lock-step with the inline `.vtab-group` markup in buildTabBody —
 * the vitest suite asserts membership + full coverage of the original flat set.
 */
export const VTAB_GROUPS: readonly VtabGroup[] = [
  { id: "overview", label: "Overview", tabs: ["status", "live"] },
  { id: "planning", label: "Planning", tabs: ["goal", "insights", "blog"] },
  { id: "work", label: "Work", tabs: ["queue", "hitl", "team", "sessions"] },
  { id: "content", label: "Content", tabs: ["files", "notes", "devlog", "documents", "docs", "codeintel"] },
  { id: "history", label: "History", tabs: ["timeline", "rewind", "settings"] },
] as const;

/** The full, ordered list of grouped tab ids (union across all groups). */
export const ALL_GROUPED_TABS: readonly string[] = VTAB_GROUPS.flatMap((g) => g.tabs);

/** Map a data-vtab id to its owning group id, or null if ungrouped/unknown. */
export function groupForTab(tab: string | null | undefined): string | null {
  if (!tab) return null;
  for (const g of VTAB_GROUPS) {
    if (g.tabs.includes(tab)) return g.id;
  }
  return null;
}

/**
 * Wire the group headers so clicking one collapses/expands that group's tabs.
 * Returns a `revealGroupForTab` helper: call it BEFORE programmatically
 * navigating to a tab so its (possibly user-collapsed) group is re-expanded and
 * the target button is visible/measurable. Safe to call with an unknown tab.
 */
export function wireVtabGroups(
  stripEl: HTMLElement,
): { revealGroupForTab: (tab: string | null | undefined) => void } {
  const setExpanded = (groupEl: Element, expanded: boolean) => {
    groupEl.classList.toggle("collapsed", !expanded);
    const header = groupEl.querySelector(".vtab-group-header");
    if (header) header.setAttribute("aria-expanded", String(expanded));
    // Drive collapse via inline display so no CSS-file rule is required.
    const tabs = groupEl.querySelector<HTMLElement>(".vtab-group-tabs");
    if (tabs) tabs.style.display = expanded ? "flex" : "none";
  };

  stripEl.querySelectorAll<HTMLElement>(".vtab-group-header").forEach((header) => {
    header.onclick = () => {
      const groupEl = header.closest(".vtab-group");
      if (!groupEl) return;
      // toggle: collapsed -> expanded, expanded -> collapsed
      setExpanded(groupEl, groupEl.classList.contains("collapsed"));
    };
  });

  const revealGroupForTab = (tab: string | null | undefined) => {
    const groupId = groupForTab(tab);
    if (!groupId) return;
    const groupEl = stripEl.querySelector(`.vtab-group[data-vgroup="${groupId}"]`);
    if (groupEl) setExpanded(groupEl, true);
  };

  return { revealGroupForTab };
}
