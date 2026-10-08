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
//     (possibly collapsed) group before landing on one of its tabs.
//   * revealGroupInStrip — the same reveal as a plain function over a strip, so
//     the waffle launcher (dashboard-waffle.ts, 90952bad) can reveal a group
//     without holding the closure wireVtabGroups returns.
//
// Why groups can START COLLAPSED (90952bad): the rail used to render every
// group expanded because external code navigates by clicking
// `.vtab-btn[data-vtab="X"]` directly (demo tour, deep-links, HITL/timeline
// jumps, the waffle launcher, and the Playwright UX tests). Every one of those
// programmatic paths goes through `btn.click()`, and a click on a button inside
// a `display:none` container still runs its onclick, which calls
// revealGroupForTab BEFORE anything measures or scrolls to the button. The
// demo tour measures the button only after that click, and the Playwright tests
// reveal the group before they wait for the button to be visible. So the default
// is now: every group collapsed except the one holding the active tab, and
// navigating to a tab in another group swaps the open group (accordion) unless
// the user opened that group themselves. Flip
// VTAB_GROUPS_COLLAPSED_BY_DEFAULT to false to restore the old all-expanded rail.

/**
 * Rail groups start collapsed except the active tab's group (90952bad). One
 * constant so the owner can revert the default without touching the wiring.
 */
export const VTAB_GROUPS_COLLAPSED_BY_DEFAULT = true;

/** Set on a strip when it uses the collapsed-by-default accordion behaviour. */
const ACCORDION_ATTR = "data-vaccordion";
/** Set on a group the user opened by hand: the accordion never auto-collapses it. */
const USER_OPEN_ATTR = "data-vuser-open";

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
  // 'experiments' was added to the rail after the grouping shipped and was never
  // listed here, so groupForTab() returned null for it and revealGroupForTab()
  // could not expand its group. With groups collapsed by default that would
  // leave an active Experiments tab invisible in the rail (90952bad).
  { id: "work", label: "Work", tabs: ["queue", "experiments", "hitl", "team", "sessions"] },
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

function setGroupExpanded(groupEl: Element, expanded: boolean): void {
  groupEl.classList.toggle("collapsed", !expanded);
  const header = groupEl.querySelector(".vtab-group-header");
  if (header) header.setAttribute("aria-expanded", String(expanded));
  // Drive collapse via inline display so no CSS-file rule is required.
  const tabs = groupEl.querySelector<HTMLElement>(".vtab-group-tabs");
  if (tabs) tabs.style.display = expanded ? "flex" : "none";
}

/**
 * Expand the group owning `tab` inside `stripEl` so the tab's button is laid
 * out. On a collapsed-by-default strip it also collapses every other group that
 * is only open because of an earlier navigation (not one the user opened by
 * hand), so the rail shows the active group rather than slowly re-expanding.
 * Safe to call with an unknown tab or a strip without that group.
 */
export function revealGroupInStrip(
  stripEl: ParentNode,
  tab: string | null | undefined,
): void {
  const groupId = groupForTab(tab);
  if (!groupId) return;
  const groupEl = stripEl.querySelector(`.vtab-group[data-vgroup="${groupId}"]`);
  if (!groupEl) return;
  setGroupExpanded(groupEl, true);
  const strip = stripEl as Element;
  if (typeof strip.hasAttribute === "function" && strip.hasAttribute(ACCORDION_ATTR)) {
    stripEl.querySelectorAll(".vtab-group").forEach((other) => {
      if (other === groupEl || other.hasAttribute(USER_OPEN_ATTR)) return;
      if (!other.classList.contains("collapsed")) setGroupExpanded(other, false);
    });
  }
}

/**
 * Wire the group headers so clicking one collapses/expands that group's tabs,
 * and (by default) start every group collapsed except the one holding the
 * active tab. Returns a `revealGroupForTab` helper: call it BEFORE
 * programmatically navigating to a tab so its group is expanded and the target
 * button is visible/measurable. Safe to call with an unknown tab.
 */
export function wireVtabGroups(
  stripEl: HTMLElement,
  opts: { collapseByDefault?: boolean } = {},
): { revealGroupForTab: (tab: string | null | undefined) => void } {
  const collapseByDefault = opts.collapseByDefault ?? VTAB_GROUPS_COLLAPSED_BY_DEFAULT;

  stripEl.querySelectorAll<HTMLElement>(".vtab-group-header").forEach((header) => {
    header.onclick = () => {
      const groupEl = header.closest(".vtab-group");
      if (!groupEl) return;
      // toggle: collapsed -> expanded, expanded -> collapsed
      const willExpand = groupEl.classList.contains("collapsed");
      setGroupExpanded(groupEl, willExpand);
      // A group the user opened by hand is exempt from the accordion collapse.
      if (willExpand) groupEl.setAttribute(USER_OPEN_ATTR, "1");
      else groupEl.removeAttribute(USER_OPEN_ATTR);
    };
  });

  if (collapseByDefault) {
    stripEl.setAttribute(ACCORDION_ATTR, "1");
    const activeTab = stripEl.querySelector<HTMLElement>(".vtab-btn.active")?.dataset.vtab;
    const activeGroup = groupForTab(activeTab);
    stripEl.querySelectorAll(".vtab-group").forEach((groupEl) => {
      setGroupExpanded(groupEl, groupEl.getAttribute("data-vgroup") === activeGroup);
    });
  }

  const revealGroupForTab = (tab: string | null | undefined) => revealGroupInStrip(stripEl, tab);

  return { revealGroupForTab };
}
