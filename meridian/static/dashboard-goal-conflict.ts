// dashboard-goal-conflict.ts — fc779141: keep an in-progress edit of a goal field
// safe from live updates and from a stale save.
//
// The Goal tab has three editable fields (north star, version goal, current
// focus).  Before this module refreshGoal() rewrote all three on every
// goal_updated push, so an agent calling set_goal while a person was typing
// silently destroyed the draft, and the save that followed was last-write-wins
// on the server.  Each field is now a GoalField:
//
//   - it remembers the server text + per-field stamp the edit is based on;
//   - applyServer() (the only way server data reaches the editor) updates an
//     untouched field live, but never overwrites unsaved text: it keeps the
//     person's draft, remembers the incoming value and shows a compact
//     "Changed elsewhere" bar with Keep mine / Take theirs / See both;
//   - saveNow() sends the stamp as expected_updated_at and treats a 409 like a
//     remote change (same bar) instead of a lost write;
//   - Esc offers to discard an unsaved edit, and leaving the Goal tab / the page
//     with unsaved edits asks first.
//
// Everything that decides something is a pure function or a method on GoalField
// that takes its DOM through a config object, so the conflict matrix is unit
// tested in jsdom (dashboard-goal-conflict.test.ts) without the 13k-line
// dashboard.ts.  Only formatRelativeTime is imported (same module graph as every
// other strict-typed dashboard module).

import { formatRelativeTime } from './dashboard-utils';

export type GoalFieldKey = 'version_goal' | 'north_star' | 'sprint';

/** Who/when for the most recent remote change to a field (from a goal_updated event). */
export interface RemoteMeta {
  kind?: string | null;   // 'human' | 'agent' | 'unknown'
  source?: string | null; // 'dashboard' | 'mcp' | 'goal_md' | 'api'
  id?: string | null;
  stamp?: string | null;  // the field stamp this change produced; used to match an event to a refresh
}

export type SaveOutcome = 'saved' | 'skipped' | 'blocked' | 'conflict' | 'error';
export type ApplyOutcome = 'unchanged' | 'converged' | 'applied' | 'conflict';

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/**
 * Canonical form used to compare an editor value with a server value.  A
 * textarea reports "\n" for every line break and trailing whitespace is not
 * something a person means to change, so neither may make a field look dirty or
 * a server value look different.  North star and current focus are saved
 * trimmed (as they always were), the version goal only loses trailing space.
 */
export function normalizeGoalText(key: GoalFieldKey, text: unknown): string {
  const t = String(text ?? '').replace(/\r\n?/g, '\n');
  return key === 'version_goal' ? t.replace(/\s+$/, '') : t.trim();
}

export interface GoalParts {
  /** First line when it looks like a version label ("v2.3 — auth sprint"), else ''. */
  titleLine: string;
  /** Read-only SHIPPED block between the title and CURRENT FOCUS. */
  shipped: string;
  /** The zone a person edits: CURRENT FOCUS / KEY FILES onwards. */
  editable: string;
  /** Text after the AUTO BLOCKS marker, or null when there is none. */
  autoBlocks: string | null;
}

export const GOAL_AUTO_SPLIT = '--- AUTO BLOCKS BELOW ---';

/**
 * Split stored version-goal text into its read-only zones and the editable zone.
 * This is refreshGoal()'s original splitting, extracted unchanged so the editor
 * and a 409 response resolve the same text to the same editable string.
 */
export function splitGoalText(text: string): GoalParts {
  const splitIdx = text.indexOf(GOAL_AUTO_SPLIT);
  const mainText = splitIdx !== -1 ? text.slice(0, splitIdx).trimEnd() : text;
  const allLines = mainText.split('\n');
  // Only use the first line as the version title when it looks like a version
  // label (e.g. "v1.0.0", "v2.3 — auth sprint"); otherwise everything is editable.
  const firstLine = allLines[0] || '';
  const isVersionLabel = /^v\d+\.\d+/.test(firstLine.trim()) || firstLine.trim().length === 0;
  const titleLine = isVersionLabel ? firstLine : '';
  const body = (isVersionLabel ? allLines.slice(1) : allLines).join('\n').replace(/^\n/, '');
  const editStart = body.search(/^(CURRENT FOCUS|KEY FILES)/m);
  return {
    titleLine,
    shipped: editStart > 0 ? body.slice(0, editStart).trimEnd() : '',
    editable: editStart > 0 ? body.slice(editStart) : body,
    autoBlocks: splitIdx !== -1 ? text.slice(splitIdx + GOAL_AUTO_SPLIT.length).trimStart() : null,
  };
}

export interface DiffOp {
  /** 'same' = in both texts, 'mine' = only in the person's text, 'theirs' = only in the server's. */
  type: 'same' | 'mine' | 'theirs';
  text: string;
}

const MAX_DIFF_LINES = 1000;
const RECENT_SAVE_MS = 2000;

/** Line diff (longest common subsequence) between the person's text and the server's. */
export function diffLines(mine: string, theirs: string): DiffOp[] {
  const a = mine.split('\n');
  const b = theirs.split('\n');
  // The O(n*m) table is bounded: the server caps a goal field at 10k characters,
  // so this is only a guard against a pathological paste.
  if (a.length > MAX_DIFF_LINES || b.length > MAX_DIFF_LINES) {
    return [
      ...a.map((text): DiffOp => ({ type: 'mine', text })),
      ...b.map((text): DiffOp => ({ type: 'theirs', text })),
    ];
  }
  const w = b.length + 1;
  const lcs = new Int32Array((a.length + 1) * w);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i * w + j] = a[i] === b[j]
        ? lcs[(i + 1) * w + j + 1] + 1
        : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      ops.push({ type: 'same', text: a[i] });
      i++;
      j++;
    } else if (lcs[(i + 1) * w + j] >= lcs[i * w + j + 1]) {
      ops.push({ type: 'mine', text: a[i++] });
    } else {
      ops.push({ type: 'theirs', text: b[j++] });
    }
  }
  while (i < a.length) ops.push({ type: 'mine', text: a[i++] });
  while (j < b.length) ops.push({ type: 'theirs', text: b[j++] });
  return ops;
}

/** "by an agent, 2m ago" / "2m ago" / "by a person" / '' — what the bar says about the remote change. */
export function describeRemoteChange(meta: RemoteMeta | null | undefined, stamp: string | null | undefined): string {
  const who = meta?.kind === 'agent' ? 'by an agent' : meta?.kind === 'human' ? 'by a person' : '';
  const when = stamp ? formatRelativeTime(stamp) : '';
  return [who, when].filter(Boolean).join(', ');
}

interface ParsedConflict {
  value: unknown;
  stamp: string;
}

/** The api() error for a 409 goal_conflict -> the current value and stamp, else null. */
export function parseGoalConflict(err: unknown): ParsedConflict | null {
  const e = err as { status?: number; responseText?: string } | null;
  if (!e || e.status !== 409 || typeof e.responseText !== 'string') return null;
  try {
    const detail = JSON.parse(e.responseText)?.detail;
    if (!detail || detail.error !== 'goal_conflict' || !detail.current) return null;
    return { value: detail.current.value, stamp: String(detail.current.updated_at ?? '') };
  } catch (_) {
    return null;
  }
}

function errorMessage(err: unknown): string {
  const e = err as { responseText?: string; message?: string } | null;
  if (e && typeof e.responseText === 'string') {
    try {
      const d = JSON.parse(e.responseText)?.detail;
      if (typeof d === 'string') return d;
      if (d && typeof d.message === 'string') return d.message;
    } catch (_) { /* fall through to the raw message */ }
  }
  const msg = (e && e.message) || 'request failed';
  // dashboard-core's api() throws this bare marker for a write in the read-only demo.
  return msg === 'demo_readonly' ? 'this demo is read-only' : msg;
}

// ---------------------------------------------------------------------------
// GoalField
// ---------------------------------------------------------------------------

export interface GoalFieldConfig {
  key: GoalFieldKey;
  /** Human wording for prompts: "north star", "version goal", "current focus". */
  label: string;
  /** The editor element: focus is read from it, it gets the dirty class, and it must be attached for the field to count. */
  el: HTMLElement;
  /** The bar is inserted before this node. */
  anchor: () => HTMLElement | null;
  getValue: () => string;
  setValue: (text: string) => void;
  /**
   * Send the edit with the stamp it is based on (null = unknown, e.g. an older
   * server: send none and keep last-write-wins).  Resolve with the field's new
   * stamp, or throw the api() error (status / responseText) so a 409 can be told
   * from a network failure.
   */
  persist: (text: string, expectedStamp: string | null) => Promise<{ stamp?: string } | undefined>;
  /** Map the server's value for this field (409 body) to editor text. Default: string. */
  fromServer?: (value: unknown) => string;
  /** Saving an empty value is allowed (version goal) or silently skipped (north star, current focus). */
  allowEmpty?: boolean;
  /** Last chance to cancel a save (the north star's "intended to be stable" prompt). */
  confirmSave?: (text: string, base: string) => boolean;
  confirm?: (message: string) => boolean;
  notify?: (message: string, isError?: boolean) => void;
  onSaved?: () => void;
}

interface PendingRemote {
  text: string;
  stamp: string | null;
  meta: RemoteMeta | null;
}

export class GoalField {
  readonly cfg: GoalFieldConfig;
  /** Server text the editor content is compared against (what it was last populated with / saved as). */
  base = '';
  /** Server stamp of `base`; sent as expected_updated_at. null = not known. */
  stamp: string | null = null;
  pending: PendingRemote | null = null;
  error: string | null = null;
  private touched = false;
  private lastSavedAt = 0;
  private inflight: Promise<SaveOutcome> | null = null;
  private recent: RemoteMeta | null = null;
  private bar: HTMLElement | null = null;
  private diffOpen = false;

  constructor(cfg: GoalFieldConfig) {
    this.cfg = cfg;
  }

  // -- state ----------------------------------------------------------------

  private norm(text: unknown): string {
    return normalizeGoalText(this.cfg.key, text);
  }

  current(): string {
    return this.norm(this.cfg.getValue());
  }

  /** The editor holds text that differs from `base` and the person put it there (or asked to save it). */
  isDirty(explicit = false): boolean {
    return (this.touched || explicit) && this.current() !== this.norm(this.base);
  }

  isSaving(): boolean {
    return this.inflight !== null;
  }

  /** Unsaved edits, or a save still on its way: a live update must not replace the text. */
  isEditing(): boolean {
    return this.isDirty() || this.isSaving();
  }

  isAttached(): boolean {
    return this.cfg.el.isConnected;
  }

  private isFocused(): boolean {
    return this.cfg.el.ownerDocument.activeElement === this.cfg.el;
  }

  /** Resolves once an in-flight save (if any) has finished, whatever its outcome. */
  async settle(): Promise<void> {
    if (this.inflight) {
      try { await this.inflight; } catch (_) { /* the outcome is already reflected in state */ }
    }
  }

  /** Remember who made the latest remote change; matched to a refresh by its stamp. */
  noteRemoteMeta(meta: RemoteMeta): void {
    this.recent = meta;
  }

  private metaFor(stamp: string | null): RemoteMeta | null {
    return this.recent && this.recent.stamp && this.recent.stamp === stamp ? this.recent : null;
  }

  private paintDirty(): void {
    this.cfg.el.classList.toggle('dirty', this.isDirty());
    this.cfg.el.classList.toggle('save-failed', this.error !== null);
  }

  /** Put text in the editor, keeping the caret where it was when the person is in the field. */
  private writeEditor(text: string): void {
    const el = this.cfg.el as HTMLInputElement | HTMLTextAreaElement;
    const keep = this.isFocused() && typeof el.selectionStart === 'number';
    const start = keep ? el.selectionStart : null;
    const end = keep ? el.selectionEnd : null;
    this.cfg.setValue(text);
    if (keep && start !== null && end !== null) {
      try { el.setSelectionRange(Math.min(start, text.length), Math.min(end, text.length)); } catch (_) { /* type=number etc. */ }
    }
  }

  private adopt(text: string, stamp: string | null): void {
    this.base = text;
    this.stamp = stamp;
    this.touched = false;
    this.pending = null;
    this.error = null;
    this.diffOpen = false;
  }

  // -- server data ----------------------------------------------------------

  /**
   * The only way server data reaches the editor.  `text` is the server's value
   * for this field in editor form, `stamp` its per-field updated_at (null when
   * the server did not say).
   *
   *  unchanged  the server text is what the edit is based on (another field, or
   *             only the read-only zones, moved): adopt the stamp so the next
   *             save is not a false conflict, touch nothing else.
   *  converged  the person's draft already equals the server text: nothing to ask.
   *  applied    nothing to protect: the editor takes the new text live.
   *  conflict   unsaved edits: keep the draft, remember `text`, show the bar.
   */
  applyServer(text: string, stamp: string | null, meta?: RemoteMeta | null): ApplyOutcome {
    const incoming = this.norm(text);
    const cur = this.current();
    if (incoming === this.norm(this.base)) {
      this.stamp = stamp;
      if (this.pending) {
        // The server went back to what the edit is based on: nothing to resolve.
        this.pending = null;
        this.diffOpen = false;
      }
      this.renderBar();
      this.paintDirty();
      return 'unchanged';
    }
    if (cur === incoming && this.isEditing()) {
      this.adopt(text, stamp);
      this.renderBar();
      this.paintDirty();
      return 'converged';
    }
    if (!this.isEditing()) {
      this.writeEditor(text);
      this.adopt(text, stamp);
      this.recent = meta ?? this.metaFor(stamp);
      this.renderBar();
      this.paintDirty();
      return 'applied';
    }
    this.pending = { text, stamp, meta: meta ?? this.metaFor(stamp) };
    this.renderBar();
    this.paintDirty();
    return 'conflict';
  }

  // -- user actions ---------------------------------------------------------

  /** An input/change event from the person (not a programmatic setValue). */
  onUserInput(): void {
    this.touched = true;
    if (this.pending && !this.isDirty()) {
      // They edited their way back to the base text: nothing of theirs is left
      // to protect, so show what the server has.
      const p = this.pending;
      this.writeEditor(p.text);
      this.adopt(p.text, p.stamp);
    } else if (this.error !== null) {
      this.error = null;
    }
    this.renderBar();
    this.paintDirty();
  }

  /** Blur / explicit save.  Never writes over an unresolved "Changed elsewhere" prompt. */
  async saveNow(opts: { explicit?: boolean; skipConfirm?: boolean } = {}): Promise<SaveOutcome> {
    if (this.inflight) return this.inflight;
    const explicit = !!opts.explicit;
    if (this.pending) {
      if (explicit) this.cfg.notify?.('Resolve the change from elsewhere first: Keep mine or Take theirs.', true);
      return 'blocked';
    }
    if (!this.isDirty(explicit)) {
      // A click on a Save button blurs the field first, so the blur-save has usually
      // just finished: say nothing then, rather than replace its "saved" toast.
      if (explicit && Date.now() - this.lastSavedAt > RECENT_SAVE_MS) this.cfg.notify?.('No changes to save');
      return 'skipped';
    }
    const text = this.current();
    if (!text && !this.cfg.allowEmpty) return 'skipped';
    if (!opts.skipConfirm && this.cfg.confirmSave && !this.cfg.confirmSave(text, this.norm(this.base))) {
      this.writeEditor(this.base);
      this.touched = false;
      this.paintDirty();
      return 'skipped';
    }
    const run = this.runSave(text, false);
    this.inflight = run;
    try {
      return await run;
    } finally {
      this.inflight = null;
      this.renderBar();
      this.paintDirty();
    }
  }

  private async runSave(text: string, retried: boolean): Promise<SaveOutcome> {
    this.error = null;
    this.renderBar();
    try {
      const res = await this.cfg.persist(text, this.stamp);
      this.base = text;
      this.touched = false;
      this.error = null;
      if (res && res.stamp) this.stamp = res.stamp;
      this.lastSavedAt = Date.now();
      if (this.pending && this.norm(this.pending.text) === text) this.pending = null;
      this.cfg.onSaved?.();
      return 'saved';
    } catch (err) {
      const conflict = parseGoalConflict(err);
      if (conflict) {
        const incoming = this.cfg.fromServer ? this.cfg.fromServer(conflict.value) : String(conflict.value ?? '');
        const outcome = this.applyServer(incoming, conflict.stamp, this.metaFor(conflict.stamp));
        if (outcome === 'converged') return 'saved';
        // Only a read-only zone moved: the edit itself does not clash, so save it again on the new stamp.
        if (outcome === 'unchanged' && !retried) return this.runSave(text, true);
        return 'conflict';
      }
      this.error = errorMessage(err);
      this.cfg.notify?.('save failed: ' + this.error, true);
      return 'error';
    }
  }

  /** "Keep mine": the person's text stays and is saved over the remote change. */
  async keepMine(): Promise<SaveOutcome> {
    const p = this.pending;
    if (!p) return 'skipped';
    // Ask before touching any state: declining must leave the prompt (and the
    // person's draft) exactly as it was, not fall back to the remote text.
    if (this.cfg.confirmSave && !this.cfg.confirmSave(this.current(), this.norm(p.text))) return 'skipped';
    // Rebase onto the remote version: the next save is based on what is now there.
    this.base = p.text;
    this.stamp = p.stamp;
    this.pending = null;
    this.diffOpen = false;
    this.touched = true;
    this.renderBar();
    this.paintDirty();
    return this.saveNow({ explicit: true, skipConfirm: true });
  }

  /** "Take theirs": the editor shows the server's text; the draft is dropped. */
  takeTheirs(): void {
    const p = this.pending;
    if (!p) return;
    this.writeEditor(p.text);
    this.adopt(p.text, p.stamp);
    this.renderBar();
    this.paintDirty();
  }

  /** Revert the editor to the last saved (or, with a pending remote change, the current server) text. */
  discard(): void {
    const target = this.pending ? { text: this.pending.text, stamp: this.pending.stamp } : { text: this.base, stamp: this.stamp };
    this.writeEditor(target.text);
    this.adopt(target.text, target.stamp);
    this.renderBar();
    this.paintDirty();
  }

  /** Esc: offer to discard, but only when there is something to lose. */
  handleEscape(): 'noop' | 'cancelled' | 'discarded' {
    if (!this.isDirty()) return 'noop';
    const ask = this.cfg.confirm ?? ((m: string) => window.confirm(m));
    if (!ask(`Discard your unsaved changes to the ${this.cfg.label}?`)) return 'cancelled';
    this.discard();
    return 'discarded';
  }

  /** Attach blur-save, dirty tracking and Esc-to-discard to the editor element(s). */
  wire(opts: { blur: HTMLElement[]; input: HTMLElement[]; keys: HTMLElement[] }): void {
    for (const el of opts.blur) el.addEventListener('blur', () => { void this.saveNow(); });
    for (const el of opts.input) {
      el.addEventListener('input', () => this.onUserInput());
      el.addEventListener('change', () => this.onUserInput());
    }
    for (const el of opts.keys) {
      el.addEventListener('keydown', (ev: Event) => {
        const e = ev as KeyboardEvent;
        if (e.key === 'Escape' && this.handleEscape() !== 'noop') e.preventDefault();
      });
    }
  }

  destroy(): void {
    this.bar?.remove();
    this.bar = null;
  }

  // -- the bar --------------------------------------------------------------

  private buildBar(): HTMLElement | null {
    const anchor = this.cfg.anchor();
    if (!anchor || !anchor.parentNode) return null;
    const doc = anchor.ownerDocument;
    const bar = doc.createElement('div');
    bar.className = 'goal-conflict-bar';
    bar.setAttribute('role', 'alert');
    bar.dataset.goalField = this.cfg.key;
    bar.hidden = true;

    const msg = doc.createElement('div');
    msg.className = 'goal-conflict-msg';
    const lead = doc.createElement('strong');
    lead.className = 'goal-conflict-lead';
    const detail = doc.createElement('span');
    detail.className = 'goal-conflict-detail';
    msg.append(lead, detail);

    const actions = doc.createElement('div');
    actions.className = 'goal-conflict-actions';
    const button = (act: string, label: string, onClick: () => void): HTMLButtonElement => {
      const b = doc.createElement('button');
      b.type = 'button';
      b.className = 'secondary';
      b.dataset.act = act;
      b.textContent = label;
      b.addEventListener('click', onClick);
      return b;
    };
    actions.append(
      button('keep', 'Keep mine', () => { void this.keepMine(); }),
      button('take', 'Take theirs', () => this.takeTheirs()),
      button('both', 'See both', () => { this.diffOpen = !this.diffOpen; this.renderBar(); }),
      button('retry', 'Retry save', () => { void this.saveNow({ explicit: true }); }),
    );

    const diff = doc.createElement('div');
    diff.className = 'goal-conflict-diff';
    diff.hidden = true;

    bar.append(msg, actions, diff);
    anchor.parentNode.insertBefore(bar, anchor);
    return bar;
  }

  /** Paint the bar from current state: conflict, failed save, or hidden. */
  renderBar(): void {
    const conflict = this.pending !== null;
    const failed = !conflict && this.error !== null;
    if (!this.bar || !this.bar.isConnected) {
      if (!conflict && !failed) return;
      this.bar = this.buildBar();
      if (!this.bar) return;
    }
    const bar = this.bar;
    bar.hidden = !conflict && !failed;
    bar.classList.toggle('is-error', failed);
    if (bar.hidden) return;

    const lead = bar.querySelector('.goal-conflict-lead') as HTMLElement;
    const detail = bar.querySelector('.goal-conflict-detail') as HTMLElement;
    const show = (act: string, on: boolean) => {
      const b = bar.querySelector(`button[data-act="${act}"]`) as HTMLButtonElement | null;
      if (b) b.hidden = !on;
    };
    show('keep', conflict);
    show('take', conflict);
    show('both', conflict);
    show('retry', failed);

    const diff = bar.querySelector('.goal-conflict-diff') as HTMLElement;
    if (failed) {
      lead.textContent = 'Save failed';
      detail.textContent = ` ${this.error}. Your text is kept.`;
      diff.hidden = true;
      return;
    }
    const p = this.pending as PendingRemote;
    const who = describeRemoteChange(p.meta, p.stamp);
    lead.textContent = 'Changed elsewhere';
    detail.textContent = `${who ? ` ${who}.` : '.'} Your unsaved ${this.cfg.label} is kept.`;
    const both = bar.querySelector('button[data-act="both"]') as HTMLButtonElement;
    both.textContent = this.diffOpen ? 'Hide diff' : 'See both';
    both.setAttribute('aria-expanded', String(this.diffOpen));
    diff.hidden = !this.diffOpen;
    if (this.diffOpen) this.paintDiff(diff, p);
  }

  private paintDiff(diff: HTMLElement, p: PendingRemote): void {
    const doc = diff.ownerDocument;
    diff.replaceChildren();
    const legend = doc.createElement('div');
    legend.className = 'gcd-legend';
    legend.textContent = '- only in your text    + only in the current version';
    diff.append(legend);
    for (const op of diffLines(this.current(), this.norm(p.text))) {
      const line = doc.createElement('div');
      line.className = `gcd-line gcd-${op.type}`;
      const mark = doc.createElement('span');
      mark.className = 'gcd-mark';
      mark.textContent = op.type === 'mine' ? '-' : op.type === 'theirs' ? '+' : ' ';
      const body = doc.createElement('span');
      body.className = 'gcd-text';
      body.textContent = op.text;
      line.append(mark, body);
      diff.append(line);
    }
  }
}

// ---------------------------------------------------------------------------
// Per-project registry, leave guards
// ---------------------------------------------------------------------------

type FieldSet = Partial<Record<GoalFieldKey, GoalField>>;
const registry = new Map<string, FieldSet>();

/** Register a project's fields; replaces (and cleans up) the set from a previous render of its tab. */
export function registerGoalFields(projectId: string, fields: FieldSet): void {
  const old = registry.get(projectId);
  if (old) for (const f of Object.values(old)) if (f && !(Object.values(fields) as unknown[]).includes(f)) f.destroy();
  registry.set(projectId, fields);
}

export function getGoalField(projectId: string, key: GoalFieldKey): GoalField | null {
  return registry.get(projectId)?.[key] ?? null;
}

function attachedFields(projectId?: string): GoalField[] {
  // A closed project tab leaves its (detached) fields behind; drop them here
  // instead of holding them for the life of the page.
  for (const [id, set] of registry) {
    if (!(Object.values(set) as GoalField[]).some((f) => f && f.isAttached())) registry.delete(id);
  }
  const sets = projectId === undefined ? [...registry.values()] : [registry.get(projectId)].filter(Boolean) as FieldSet[];
  return sets.flatMap((s) => Object.values(s) as GoalField[]).filter((f) => f && f.isAttached());
}

export function hasUnsavedGoalEdits(projectId?: string): boolean {
  return attachedFields(projectId).some((f) => f.isDirty() || f.isSaving());
}

/** Route a goal_updated WebSocket event's "who changed what" to the fields it names. */
export function noteGoalEvent(projectId: string, event: any): void {
  const changed: unknown = event && event.changed_fields;
  if (!Array.isArray(changed)) return;
  const by = event.changed_by || {};
  for (const key of changed as GoalFieldKey[]) {
    const f = getGoalField(projectId, key);
    if (!f) continue;
    f.noteRemoteMeta({
      kind: by.kind ?? null,
      source: by.source ?? null,
      id: by.id ?? null,
      stamp: event.field_updated_at ? event.field_updated_at[key] ?? null : null,
    });
  }
}

/** Closing the page cannot offer Save / Discard (the browser only shows its own prompt), so it warns. */
let unloadGuardInstalled = false;
export function installGoalUnloadGuard(win: Window = window): void {
  if (unloadGuardInstalled) return;
  unloadGuardInstalled = true;
  win.addEventListener('beforeunload', (e: BeforeUnloadEvent) => {
    if (!hasUnsavedGoalEdits()) return;
    e.preventDefault();
    e.returnValue = '';
  });
}

export type LeaveChoice = 'save' | 'discard' | 'stay';

/** Small modal: Save / Discard / Stay here.  Esc or a click outside means stay. */
export function promptGoalLeave(fields: GoalField[], doc: Document = document): Promise<LeaveChoice> {
  return new Promise((resolve) => {
    const overlay = doc.createElement('div');
    overlay.className = 'goal-leave-overlay';
    const dlg = doc.createElement('div');
    dlg.className = 'goal-leave-dialog';
    dlg.setAttribute('role', 'dialog');
    dlg.setAttribute('aria-modal', 'true');
    dlg.setAttribute('aria-label', 'Unsaved goal changes');
    const h = doc.createElement('div');
    h.className = 'goal-leave-title';
    h.textContent = 'Unsaved changes';
    const p = doc.createElement('div');
    p.className = 'goal-leave-body';
    p.textContent = `You have unsaved changes to the ${fields.map((f) => f.cfg.label).join(' and the ')}.`;
    const row = doc.createElement('div');
    row.className = 'goal-leave-actions';
    const finish = (choice: LeaveChoice) => {
      doc.removeEventListener('keydown', onKey, true);
      overlay.remove();
      resolve(choice);
    };
    const mk = (cls: string, label: string, choice: LeaveChoice): HTMLButtonElement => {
      const b = doc.createElement('button');
      b.type = 'button';
      b.className = cls;
      b.textContent = label;
      b.dataset.choice = choice;
      b.addEventListener('click', () => finish(choice));
      return b;
    };
    const save = mk('primary', 'Save', 'save');
    row.append(save, mk('secondary', 'Discard', 'discard'), mk('secondary', 'Stay here', 'stay'));
    dlg.append(h, p, row);
    overlay.append(dlg);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) finish('stay'); });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish('stay'); }
    };
    doc.addEventListener('keydown', onKey, true);
    doc.body.append(overlay);
    save.focus();
  });
}

/**
 * Leaving the Goal tab (or closing the project tab) with unsaved goal edits.
 * Returns true when it took over (the caller must stop and let `proceed` run
 * later), false when there is nothing unsaved and the caller can continue now.
 * Normally a blur-save has already put the edit on the server by the time a tab
 * button is clicked; what is left unsaved here is a failed save or an unresolved
 * "Changed elsewhere", which is exactly what must not be dropped silently.
 */
export function guardGoalLeave(projectId: string, proceed: () => void): boolean {
  const fields = attachedFields(projectId);
  if (!fields.some((f) => f.isDirty() || f.isSaving())) return false;
  void (async () => {
    await Promise.all(fields.map((f) => f.settle()));
    const dirty = fields.filter((f) => f.isDirty());
    if (!dirty.length) { proceed(); return; }
    const choice = await promptGoalLeave(dirty);
    if (choice === 'stay') return;
    if (choice === 'discard') {
      dirty.forEach((f) => f.discard());
      proceed();
      return;
    }
    const outcomes = await Promise.all(dirty.map((f) => f.saveNow({ explicit: true })));
    if (outcomes.every((o) => o === 'saved' || o === 'skipped')) proceed();
  })();
  return true;
}

/** Test hook: forget every registered project (module state outlives a test otherwise). */
export function _resetGoalFieldRegistry(): void {
  registry.clear();
}

// Re-expose on window like every dashboard module, so inline handlers and
// dashboard.ts's bare references keep resolving after IIFE bundling.
try {
  Object.assign(window, {
    GoalField, registerGoalFields, getGoalField, hasUnsavedGoalEdits, noteGoalEvent,
    installGoalUnloadGuard, guardGoalLeave, splitGoalText, normalizeGoalText,
  });
} catch (e) { /* window unavailable (non-browser) */ }
