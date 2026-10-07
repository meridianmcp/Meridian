// dashboard-versions.ts — "move a sprint item to another version" (0c30b989).
//
// The arrow button on a pending sprint item used to open window.prompt asking
// for a version and then DEFER the item (status 'pushed', hidden in the
// Backburner). It now opens a small popover with three explicit choices:
//
//   1. "Move to v2.2 (next)"  - the number is computed here and shown on the
//      button; the server computes it again and returns the one it used.
//   2. "Move to a specific version" - typed, with the versions already on the
//      board offered through a <datalist>.
//   3. "Defer to backburner"  - the old behaviour, kept as an explicit choice.
//
// This file holds the pure rules (nextVersion / validateVersionLabel mirror
// meridian/versioning.py and are tested against the same JSON fixture) and the
// popover DOM. Network calls, toasts and repainting stay in dashboard.ts: the
// popover only calls the callbacks it is given, so it unit-tests in jsdom.

// ---------------------------------------------------------------------------
// Pure rules (mirror meridian/versioning.py)
// ---------------------------------------------------------------------------

export const MAX_VERSION_LENGTH = 64;
// A component with more digits than this is refused: JavaScript numbers lose
// integer precision above 2**53 and Python must give the same answer.
const MAX_COMPONENT_DIGITS = 9;
// Optional v/V prefix, dot-separated ASCII numbers, optional trailing .x
// wildcard. [0-9] (not \d) keeps the rule identical to the Python side.
const VERSION_RE = /^([vV]?)([0-9]+(?:\.[0-9]+)*)(\.[xX])?$/;
// Only ASCII whitespace is trimmed, so this and Python's strip(" \t\r\n")
// cannot disagree (String.prototype.trim also removes U+FEFF and friends).
const ASCII_WS_ENDS = /^[ \t\r\n]+|[ \t\r\n]+$/g;

/** The version that follows `version`, or null when that is not unambiguous.
 *
 * The last numeric component is incremented and everything else is kept:
 * v2.1 -> v2.2, v2.9 -> v2.10, 2.1 -> 2.2, v2 -> v3, v0.2.x -> v0.3.x,
 * 1.0.0 -> 1.0.1, 2026.09 -> 2026.10. Anything unparseable returns null and
 * the caller asks the human; it never guesses. */
export function nextVersion(version: string | null | undefined): string | null {
  if (typeof version !== "string") return null;
  const text = version.replace(ASCII_WS_ENDS, "");
  if (!text || text.length > MAX_VERSION_LENGTH) return null;
  const m = VERSION_RE.exec(text);
  if (!m) return null;
  const parts = m[2].split(".");
  if (parts.some((p) => p.length > MAX_COMPONENT_DIGITS)) return null;
  const last = parts[parts.length - 1];
  let bumped = String(parseInt(last, 10) + 1);
  // Keep calendar-style padding: 2026.09 -> 2026.10.
  if (last.length > 1 && last[0] === "0") bumped = bumped.padStart(last.length, "0");
  parts[parts.length - 1] = bumped;
  const result = m[1] + parts.join(".") + (m[3] || "");
  return result.length <= MAX_VERSION_LENGTH ? result : null;
}

export type VersionLabelCheck = { ok: true; label: string } | { ok: false; error: string };

// Control (Cc), invisible format (Cf), line/paragraph separator (Zl, Zp) and
// any space separator (Zs) other than a plain ASCII space.
const FORBIDDEN_CHARS = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Zs}]/u;

/** Mirror of validate_version_label: trimmed, non-empty, <= 64 characters, no
 * control / invisible characters. The server re-validates; this only saves a
 * round trip and words the error. */
export function validateVersionLabel(value: unknown): VersionLabelCheck {
  if (typeof value !== "string") return { ok: false, error: "Enter a version." };
  const label = value.replace(ASCII_WS_ENDS, "");
  if (!label) return { ok: false, error: "Enter a version." };
  // Code points, not UTF-16 units, to count like Python's len().
  if (Array.from(label).length > MAX_VERSION_LENGTH) {
    return { ok: false, error: `A version can be at most ${MAX_VERSION_LENGTH} characters.` };
  }
  if (FORBIDDEN_CHARS.test(label.replace(/ /g, ""))) {
    return { ok: false, error: "A version cannot contain control or invisible characters." };
  }
  return { ok: true, label };
}

function versionKey(label: string): number[] | null {
  const m = VERSION_RE.exec(label.replace(ASCII_WS_ENDS, ""));
  return m ? m[2].split(".").map((p) => parseInt(p, 10)) : null;
}

/** Natural ordering for version labels: numeric where both parse (v2.9 before
 * v2.10), parseable before free text, free text alphabetical. */
export function compareVersionLabels(a: string, b: string): number {
  const ka = versionKey(a);
  const kb = versionKey(b);
  if (ka && kb) {
    const n = Math.max(ka.length, kb.length);
    for (let i = 0; i < n; i++) {
      const d = (ka[i] ?? 0) - (kb[i] ?? 0);
      if (d !== 0) return d;
    }
    return a.localeCompare(b);
  }
  if (ka) return -1;
  if (kb) return 1;
  return a.localeCompare(b);
}

/** The distinct versions present on the board, naturally sorted, minus the
 * item's own (offered through the "specific version" datalist). */
export function collectBoardVersions(
  items: ReadonlyArray<{ version?: string | null }> | null | undefined,
  excludeVersion?: string | null,
): string[] {
  const seen = new Set<string>();
  for (const it of items || []) {
    const v = typeof it?.version === "string" ? it.version.trim() : "";
    if (v && v !== (excludeVersion || "").trim()) seen.add(v);
  }
  return Array.from(seen).sort(compareVersionLabels);
}

/** A human sentence for a failed move/defer request. `api()` throws an Error
 * whose responseText is the raw JSON body; FastAPI puts the reason in
 * `detail` (a string, a {message} object, or a list of validation errors). */
export function describeMoveError(err: any): string {
  // api() throws this marker (after showing its own toast) for a write in the
  // read-only public demo.
  if (err && err.message === "demo_readonly") return "The demo is read-only.";
  const raw = err && typeof err.responseText === "string" ? err.responseText : "";
  if (raw) {
    try {
      const detail = JSON.parse(raw)?.detail;
      if (typeof detail === "string" && detail) return detail;
      if (detail && typeof detail.message === "string") return detail.message;
      if (Array.isArray(detail) && typeof detail[0]?.msg === "string") return detail[0].msg;
    } catch {
      /* not JSON: fall through to the Error's own message */
    }
  }
  return err && err.message ? String(err.message) : "The request failed.";
}

/** True for the server's "this label has no next version" answer, so the UI
 * can fall back to asking for an explicit version. */
export function isNextUnavailableError(err: any): boolean {
  if (!err || err.status !== 422 || typeof err.responseText !== "string") return false;
  try {
    return JSON.parse(err.responseText)?.detail?.code === "next_version_unavailable";
  } catch {
    return false;
  }
}

export interface PopoverRect { left: number; top: number; right: number; bottom: number }

/** Where to put the fixed-position popover: right-aligned under the anchor,
 * flipped above it when there is no room below, always inside the viewport. */
export function computePopoverPosition(
  anchor: PopoverRect | null,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  gap = 6,
  margin = 8,
): { left: number; top: number } {
  const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, Math.max(lo, hi)));
  if (!anchor) {
    return {
      left: clamp((viewport.width - size.width) / 2, margin, viewport.width - size.width - margin),
      top: clamp((viewport.height - size.height) / 2, margin, viewport.height - size.height - margin),
    };
  }
  const left = clamp(anchor.right - size.width, margin, viewport.width - size.width - margin);
  let top = anchor.bottom + gap;
  if (top + size.height > viewport.height - margin) {
    const above = anchor.top - gap - size.height;
    if (above >= margin) top = above;
  }
  return { left, top: clamp(top, margin, viewport.height - size.height - margin) };
}

// ---------------------------------------------------------------------------
// Popover
// ---------------------------------------------------------------------------

export interface VersionMovePopoverOptions {
  itemId: string;
  itemTitle: string;
  currentVersion: string;
  /** Versions already on the board (datalist suggestions). */
  boardVersions: string[];
  /** The arrow button: the popover anchors under it and returns focus to it. */
  anchor?: HTMLElement | null;
  onMoveNext: () => Promise<unknown>;
  onMoveSpecific: (version: string) => Promise<unknown>;
  /** `targetVersion` is what the legacy push records in pushed_to. */
  onDefer: (targetVersion: string) => Promise<unknown>;
  /** A request failed after the human had already closed the popover (while it
   * was still working): there is no popover left to show the reason in. */
  onError?: (message: string) => void;
}

export interface VersionMovePopover {
  el: HTMLElement;
  close: () => void;
}

export const VERSION_POPOVER_CLASS = "version-move-popover";

let openPopover: { itemId: string; close: () => void } | null = null;

/** Close the open popover, if any. Returns whether one was open. */
export function closeVersionMovePopover(): boolean {
  if (!openPopover) return false;
  openPopover.close();
  return true;
}

/** The item the open popover belongs to (so the arrow button can toggle it). */
export function openVersionMovePopoverItemId(): string | null {
  return openPopover ? openPopover.itemId : null;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function openVersionMovePopover(opts: VersionMovePopoverOptions): VersionMovePopover {
  closeVersionMovePopover();
  const { itemId, itemTitle, currentVersion, boardVersions, anchor } = opts;
  const next = nextVersion(currentVersion);
  const uid = `vmp-${Math.random().toString(36).slice(2, 8)}`;

  const pop = el("div", VERSION_POPOVER_CLASS);
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-modal", "false");
  pop.setAttribute("aria-label", "Move sprint item to a version");
  pop.dataset.itemId = itemId;

  const head = el("div", "vmp-head");
  const title = el("span", "vmp-title", itemTitle || "Sprint item");
  title.title = itemTitle || "";
  const closeBtn = el("button", "vmp-close", "✕");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", "Close");
  head.append(title, closeBtn);

  const cur = el("div", "vmp-current");
  cur.append("Now in ");
  cur.append(el("code", "", currentVersion || "(no version)"));

  const nextBtn = el("button", "vmp-next");
  nextBtn.type = "button";
  nextBtn.dataset.action = "next";
  const nextHint = el("div", "vmp-hint");
  nextHint.id = `${uid}-next-hint`;
  if (next) {
    nextBtn.textContent = `Move to ${next} (next)`;
    nextHint.textContent = "Stays pending; only its version changes.";
  } else {
    nextBtn.textContent = "Move to next version";
    nextBtn.disabled = true;
    nextHint.textContent = currentVersion
      ? `No next version can be worked out from "${currentVersion}". Type one below.`
      : "This item has no version. Type one below.";
  }
  nextBtn.setAttribute("aria-describedby", nextHint.id);

  const or = el("div", "vmp-or", "or a specific version");
  const row = el("div", "vmp-row");
  const input = el("input", "vmp-input");
  input.type = "text";
  input.placeholder = "e.g. v2.5";
  input.maxLength = MAX_VERSION_LENGTH;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Specific version");
  const listId = `${uid}-versions`;
  input.setAttribute("list", listId);
  const datalist = el("datalist");
  datalist.id = listId;
  for (const v of boardVersions) {
    const opt = el("option");
    opt.value = v;
    datalist.append(opt);
  }
  const goBtn = el("button", "vmp-go", "Move");
  goBtn.type = "button";
  goBtn.dataset.action = "specific";
  row.append(input, goBtn, datalist);

  const sep = el("div", "vmp-sep");
  const deferBtn = el("button", "vmp-defer", "Defer to backburner");
  deferBtn.type = "button";
  deferBtn.dataset.action = "defer";
  const deferHint = el("div", "vmp-hint");
  deferHint.id = `${uid}-defer-hint`;
  deferBtn.setAttribute("aria-describedby", deferHint.id);

  const error = el("div", "vmp-error");
  error.setAttribute("role", "alert");
  error.hidden = true;

  pop.append(head, cur, nextBtn, nextHint, or, row, sep, deferBtn, deferHint, error);

  // What "Defer to backburner" would record as pushed_to: the typed version if
  // there is one, else the next version when it can be worked out.
  const deferTarget = (): string | null => {
    const typed = input.value.replace(ASCII_WS_ENDS, "");
    if (typed) {
      const c = validateVersionLabel(typed);
      return c.ok ? c.label : null;
    }
    return next;
  };
  const refreshDeferHint = () => {
    const t = deferTarget();
    deferHint.textContent = t
      ? `Hides it in the Backburner, marked as pushed to ${t}.`
      : "Type the version it is deferred to, above.";
  };
  refreshDeferHint();

  // Tab order for the focus trap, and the subset frozen while a request runs
  // (the close button stays live so a hung request can still be dismissed).
  const controls = [closeBtn, nextBtn, input, goBtn, deferBtn];
  const frozenWhileBusy = [nextBtn, input, goBtn, deferBtn];
  const showError = (msg: string, invalidInput = false) => {
    error.textContent = msg;
    error.hidden = false;
    input.setAttribute("aria-invalid", invalidInput ? "true" : "false");
  };
  const clearError = () => {
    error.textContent = "";
    error.hidden = true;
    input.setAttribute("aria-invalid", "false");
  };

  let closed = false;
  let busy = false;

  const onDocMouseDown = (ev: MouseEvent) => {
    const t = ev.target as Node | null;
    if (!t || pop.contains(t)) return;
    // The anchor's own click toggles the popover; closing on its mousedown
    // would make that click reopen it.
    if (anchor && anchor.contains(t)) return;
    close();
  };
  const onResize = () => close();
  const onKeyDown = (ev: KeyboardEvent) => {
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      close();
    } else if (ev.key === "Tab") {
      const live = controls.filter((c) => !c.disabled);
      if (!live.length) return;
      const first = live[0];
      const last = live[live.length - 1];
      const active = document.activeElement;
      if (ev.shiftKey && (active === first || !pop.contains(active))) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && (active === last || !pop.contains(active))) {
        ev.preventDefault();
        first.focus();
      }
    }
  };

  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("mousedown", onDocMouseDown, true);
    document.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("resize", onResize);
    const hadFocus = pop.contains(document.activeElement);
    pop.remove();
    if (openPopover && openPopover.close === close) openPopover = null;
    if (hadFocus) {
      // Give focus back to the arrow button. The boards repaint on a timer and
      // on every websocket event, which replaces the very element that was
      // clicked, so fall back to the item's current arrow button.
      const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(itemId) : itemId;
      const target =
        anchor && anchor.isConnected
          ? anchor
          : document.querySelector<HTMLElement>(
              `[data-act="move-version"][data-item-id="${esc}"]`,
            );
      if (target) target.focus();
    }
  }

  const setBusy = (on: boolean, working?: HTMLButtonElement) => {
    busy = on;
    pop.setAttribute("aria-busy", on ? "true" : "false");
    for (const c of frozenWhileBusy) c.disabled = on || (c === nextBtn && !next);
    if (on && working) {
      working.dataset.label = working.textContent || "";
      working.textContent = "Working…";
    }
    if (!on) {
      for (const c of [nextBtn, goBtn, deferBtn]) {
        if (c.dataset.label) {
          c.textContent = c.dataset.label;
          delete c.dataset.label;
        }
      }
    }
  };

  const run = async (working: HTMLButtonElement, job: () => Promise<unknown>) => {
    if (busy) return;
    clearError();
    setBusy(true, working);
    try {
      await job();
      close();
    } catch (err) {
      if (closed) {
        if (opts.onError) opts.onError(describeMoveError(err));
        return;
      }
      setBusy(false);
      showError(describeMoveError(err));
      // The server may know better than the browser (e.g. a label this build
      // cannot parse): steer the human to the explicit-version field.
      (isNextUnavailableError(err) ? input : working).focus();
    }
  };

  nextBtn.addEventListener("click", () => {
    if (!next) return;
    void run(nextBtn, () => opts.onMoveNext());
  });
  const submitSpecific = () => {
    if (busy) return;
    const check = validateVersionLabel(input.value);
    if (!check.ok) {
      showError(check.error, true);
      input.focus();
      return;
    }
    if (check.label === currentVersion) {
      showError(`It is already in ${check.label}.`, true);
      input.focus();
      return;
    }
    void run(goBtn, () => opts.onMoveSpecific(check.label));
  };
  goBtn.addEventListener("click", submitSpecific);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      submitSpecific();
    }
  });
  input.addEventListener("input", () => {
    if (!error.hidden) clearError();
    refreshDeferHint();
  });
  deferBtn.addEventListener("click", () => {
    if (busy) return;
    const target = deferTarget();
    if (!target) {
      const typed = input.value.replace(ASCII_WS_ENDS, "");
      const check = typed ? validateVersionLabel(typed) : null;
      showError(check && !check.ok ? check.error : "Type the version it is deferred to, above.", true);
      input.focus();
      return;
    }
    void run(deferBtn, () => opts.onDefer(target));
  });
  closeBtn.addEventListener("click", () => close());
  pop.addEventListener("keydown", (ev) => {
    // Escape / Tab are handled on the document (capture) so they also work
    // when focus has drifted outside; this keeps stray keys from reaching the
    // board's own shortcuts.
    ev.stopPropagation();
  });

  document.body.appendChild(pop);
  document.addEventListener("mousedown", onDocMouseDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("resize", onResize);

  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  pop.style.position = "fixed";
  pop.style.visibility = "hidden";
  const rect = pop.getBoundingClientRect();
  const size = { width: rect.width || 300, height: rect.height || 220 };
  const anchorRect = anchor ? anchor.getBoundingClientRect() : null;
  const pos = computePopoverPosition(anchorRect, size, { width: vw, height: vh });
  pop.style.left = `${Math.round(pos.left)}px`;
  pop.style.top = `${Math.round(pos.top)}px`;
  pop.style.visibility = "";

  (next ? nextBtn : input).focus();
  openPopover = { itemId, close };
  return { el: pop, close };
}

/** After a move, briefly highlight the item wherever it is now painted (the
 * Live board row and the Queue card) and scroll it into view, so the human sees
 * it land under the new version. Best-effort. */
export function flashMovedItem(itemId: string, root: ParentNode = document): void {
  try {
    const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(itemId) : itemId;
    const rows = root.querySelectorAll<HTMLElement>(
      `.sprint-item-row[data-item="${esc}"], .queue-item[data-item-id="${esc}"]`,
    );
    rows.forEach((row, i) => {
      row.classList.add("sprint-row-moved");
      if (i === 0 && typeof row.scrollIntoView === "function") {
        row.scrollIntoView({ block: "nearest" });
      }
      setTimeout(() => row.classList.remove("sprint-row-moved"), 2400);
    });
  } catch {
    /* cosmetic only */
  }
}
