// dashboard-sprint-move.ts — what the sprint arrow button DOES (0c30b989).
//
// dashboard-versions.ts owns the popover and the next-version rule. This file
// is the glue around them: which endpoint a choice calls, what is sent, what the
// toast claims, and which views repaint afterwards. That is the part that
// delivers the owner's request ("move to the next version, say which number,
// verifiably, without touching the title"), and it used to live inline in
// dashboard.ts -- a ~13,800-line script that no unit test can import -- so it was
// only ever checked by hand in a browser. It now sits behind injected
// dependencies (the real api/toast/loaders are wired in dashboard.ts) so
// dashboard-sprint-move.test.ts can pin every one of those decisions.

import {
  closeVersionMovePopover,
  collectBoardVersions,
  flashMovedItem,
  openVersionMovePopover,
  openVersionMovePopoverItemId,
} from "./dashboard-versions";

export interface SprintMoveDeps {
  api: (path: string, init?: RequestInit) => Promise<any>;
  toast: (message: string, isError?: boolean) => void;
  /** Repaints the Live tab's board (grouped by version). */
  refreshLiveTab: (projectId: string) => Promise<unknown>;
  /** Repaints the Queue tab; only called while that tab's body exists. */
  loadQueue: (projectId: string) => Promise<unknown>;
  /** Repaints the Goal tab's sprint board, when that board has been built. */
  reloadSprintBoard: (projectId: string) => Promise<unknown> | void;
}

/** The element the Queue tab renders into (dashboard.ts buildTabBody). */
export const queueBodyId = (projectId: string) => `queue-body-${projectId}`;

export function createSprintMoveActions(deps: SprintMoveDeps) {
  /** Repaint every surface that lists sprint items after a move or defer: the
   *  Live board (grouped by version), the Queue tab and the Goal tab's sprint
   *  board. Previously only the Live tab refreshed, and the Queue only if a
   *  websocket event happened to arrive while it was open. One surface failing
   *  never stops the others. */
  async function repaintSprintViews(projectId: string): Promise<void> {
    const run = async (job: () => Promise<unknown> | void) => {
      await job();
    };
    const queueOpen = !!document.getElementById(queueBodyId(projectId));
    await Promise.allSettled([
      run(() => deps.refreshLiveTab(projectId)),
      queueOpen ? run(() => deps.loadQueue(projectId)) : Promise.resolve(),
      run(() => deps.reloadSprintBoard(projectId)),
    ]);
  }

  /** POST .../move, check what the server says it did, repaint. Throws on
   *  failure so the popover can show the reason inline. */
  async function sprintMoveItem(projectId: string, itemId: string, body: Record<string, unknown>) {
    const out = await deps.api(`/projects/${projectId}/sprint-items/${itemId}/move`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    // Trust the row the server re-read after writing, not our own request: the
    // toast must state where the item really is.
    if (!out || !out.item || out.item.version !== out.to_version) {
      throw new Error("The move could not be verified. Refresh and check the item.");
    }
    deps.toast(out.unchanged ? `Already in ${out.to_version}` : `Moved to ${out.to_version}`);
    // Not awaited: the move is done and verified, so let the popover close now;
    // the repaint refetches several lists and can take a second or two.
    void repaintSprintViews(projectId).then(() => flashMovedItem(itemId));
    return out;
  }

  /** The legacy push: defer to the backburner, recording the target version in
   *  pushed_to. Now an explicit third choice rather than the only one. */
  async function sprintDeferItem(projectId: string, itemId: string, targetVersion: string) {
    await deps.api(`/projects/${projectId}/sprint-items/${itemId}/push`, {
      method: "POST",
      body: JSON.stringify({ to_version: targetVersion }),
    });
    deps.toast(`Deferred to backburner (${targetVersion})`);
    void repaintSprintViews(projectId);
  }

  /** The arrow button on a pending item. Opens a popover offering "Move to
   *  <next version>", a specific version (datalist of the versions already on
   *  the board) and "Defer to backburner", instead of window.prompt deferring
   *  the item to whatever was typed. Clicking the arrow again closes it. The
   *  name is kept: inline onclick handlers and old bookmarks use it. */
  async function sprintPushPrompt(projectId: string, itemId: string, anchor?: unknown) {
    if (openVersionMovePopoverItemId() === itemId) {
      closeVersionMovePopover();
      return;
    }
    closeVersionMovePopover();

    let list: any[] = [];
    try {
      const payload = await deps.api(`/projects/${projectId}/sprint-items`);
      list = Array.isArray(payload) ? payload : (payload && payload.items) || [];
    } catch (e: any) {
      deps.toast(`Could not load the item: ${e.message}`, true);
      return;
    }
    const item = list.find((it: any) => it.id === itemId);
    if (!item) {
      deps.toast("That sprint item no longer exists.", true);
      await repaintSprintViews(projectId);
      return;
    }

    // The board may have repainted while the list loaded, replacing the button
    // that was clicked; anchor to whichever arrow button is on screen now.
    const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(itemId) : itemId;
    const clicked = anchor instanceof HTMLElement && anchor.isConnected ? anchor : null;
    const live = clicked
      || document.querySelector(`[data-act="move-version"][data-item-id="${esc}"]`);

    const currentVersion: string = item.version || "";
    openVersionMovePopover({
      itemId,
      itemTitle: item.title || "",
      currentVersion,
      boardVersions: collectBoardVersions(list, currentVersion),
      anchor: live instanceof HTMLElement ? live : null,
      // expected_version makes a retried request safe: if the item moved in the
      // meantime the server answers 409 instead of advancing it a second time.
      onMoveNext: () =>
        sprintMoveItem(projectId, itemId, { next: true, expected_version: currentVersion }),
      onMoveSpecific: (version: string) =>
        sprintMoveItem(projectId, itemId, { to_version: version, expected_version: currentVersion }),
      onDefer: (target: string) => sprintDeferItem(projectId, itemId, target),
      onError: (message: string) => deps.toast(`Move failed: ${message}`, true),
    });
  }

  return { repaintSprintViews, sprintMoveItem, sprintDeferItem, sprintPushPrompt };
}
