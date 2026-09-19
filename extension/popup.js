const ENGINE_URL = "http://127.0.0.1:8471";

// Belt-and-suspenders release window (write-back-spec.md section 4, point
// 4): chrome.runtime doesn't reliably fire an event on popup close, so
// besides the pagehide/sendBeacon attempt below, a freshly-opened popup also
// checks whether IT already has live claims older than this window -- which
// a legitimately-just-opened popup could never have caused -- and releases
// them. Two minutes is a generous upper bound on "how long could a popup
// realistically have been open" for this v1's single-tab-at-a-time usage.
const STALE_CLAIM_MS = 2 * 60 * 1000;

// Populated once per popup open, before anything claim-related can happen.
let currentProjectId = null;
let currentHolderToken = null;

function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = cls || "";
}

async function getActiveOverleafTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.includes("overleaf.com/project/")) return null;
  return tab;
}

/** Overleaf project id from the tab URL (overleaf.com/project/<id>) -- no
 * new permission needed, `tabs` already covers reading the URL (see
 * write-back-spec.md section 4, point 1). */
function projectIdFromUrl(url) {
  const match = url.match(/overleaf\.com\/project\/([a-zA-Z0-9]+)/);
  return match ? match[1] : null;
}

/** One opaque, client-generated token per browser session (not a Meridian
 * session_id -- see write-back-spec.md section 3). Created once with
 * crypto.randomUUID() and kept in chrome.storage.session so every popup open
 * in the same browser session reuses the same holder identity. */
async function getHolderToken() {
  const stored = await chrome.storage.session.get("holderToken");
  if (stored.holderToken) return stored.holderToken;
  const token = crypto.randomUUID();
  await chrome.storage.session.set({ holderToken: token });
  return token;
}

/** Belt-and-suspenders half of release: if THIS holder already has live
 * claims older than STALE_CLAIM_MS on this project, a freshly-opened popup
 * could not have created them just now -- they must have survived a close
 * that didn't fire the pagehide release below. Release them before doing
 * anything else. Best-effort only: any failure here (engine not running
 * yet, network hiccup) must never block the popup from opening normally. */
async function releaseStaleClaimsOnOpen(projectId, holderToken) {
  try {
    const res = await fetch(`${ENGINE_URL}/claims?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return;
    const { claims } = await res.json();
    const now = Date.now();
    const hasStale = (claims || []).some(
      (c) => c.holder_token === holderToken && now - Date.parse(c.claimed_at) > STALE_CLAIM_MS
    );
    if (hasStale) {
      await fetch(`${ENGINE_URL}/release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, holder_token: holderToken }),
      });
    }
  } catch {
    // Engine not reachable yet, or a transient network error -- the normal
    // /claim//release flow will surface a clearer error if the engine is
    // really down; this best-effort pass just shouldn't block popup open.
  }
}

async function check() {
  setStatus("Checking for an Overleaf editor on this tab…");
  const tab = await getActiveOverleafTab();
  if (!tab) {
    setStatus("Not on an Overleaf project page.", "missing");
    return;
  }
  chrome.tabs.sendMessage(tab.id, { type: "MERIDIAN_LATEX_GET_STATUS" }, (resp) => {
    if (chrome.runtime.lastError || !resp) {
      setStatus("No response from content script — try reloading the Overleaf tab.", "missing");
      return;
    }
    if (!resp.editorFound) {
      setStatus("Overleaf page found, but no .cm-content editor detected — selector may need updating.", "missing");
      return;
    }
    setStatus(`Editor detected: ${resp.lineCount} lines, ${resp.charCount} chars.`, "ok");
  });
}

function setOutline(text, cls) {
  const el = document.getElementById("outline");
  el.hidden = false;
  el.textContent = text;
  el.className = cls || "";
}

/**
 * Reads the FULL live document text via CM6's own `state.doc` model
 * (MERIDIAN_LATEX_GET_FULL_TEXT -> content_script.js's
 * getFullTextViaMainWorld -> injected.js's getFullText()) -- NOT the older
 * MERIDIAN_LATEX_GET_TEXT/readEditorText() DOM scrape, which silently
 * returns only whatever lines Overleaf's CM6 currently has rendered.
 * Real bug found live, 2026-09-18: CM6 virtualizes `.cm-content`'s
 * `.cm-line` children -- a real 405-line/69756-char manuscript had only 17
 * lines actually in the DOM at the time, so every outline extraction this
 * function feeds (getOutline(), saveNodeEdit()'s re-fetch) was silently
 * scoped to whatever happened to be scrolled into view, with no error or
 * truncation signal. This is the one and only place `getOutline()`/
 * `saveNodeEdit()` get "the document text" from, so fixing it here fixes
 * outline extraction for every node kind, not just the ones this pass
 * adds editing for.
 */
function getEditorText() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
      if (!tab || !tab.url || !tab.url.includes("overleaf.com/project/")) {
        reject(new Error("Not on an Overleaf project page."));
        return;
      }
      chrome.tabs.sendMessage(tab.id, { type: "MERIDIAN_LATEX_GET_FULL_TEXT" }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.found || resp.text == null) {
          reject(
            new Error(
              `Could not read the full editor text: ${
                (resp && resp.reason) || "no response from content script — try reloading the Overleaf tab."
              }`,
            ),
          );
          return;
        }
        resolve(resp.text);
      });
    });
  });
}

function nodeLabel(node) {
  if (node.kind === "heading") return `${node.level}: ${node.title}`;
  if (node.kind === "citation") return `cite: ${node.key}`;
  if (node.kind === "table" || node.kind === "figure") {
    return node.caption || node.label || "(untitled)";
  }
  return `line ${node.line ?? "?"}`;
}

/** `{node_id: claimEntry}` for a project's current live claims, from GET
 * /claims -- used only to pre-render "claimed by someone else" hints; the
 * actual source of truth for whether a claim SUCCEEDS is always the /claim
 * response itself (see claimNodeById below), never this snapshot. */
async function fetchLiveClaimsMap(projectId) {
  try {
    const res = await fetch(`${ENGINE_URL}/claims?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return {};
    const { claims } = await res.json();
    const map = {};
    for (const c of claims || []) map[c.node_id] = c;
    return map;
  } catch {
    return {};
  }
}

function showClaimResult(row, text, cls) {
  let el = row.querySelector(".claim-result");
  if (!el) {
    el = document.createElement("div");
    el.className = "claim-result";
    row.appendChild(el);
  }
  el.textContent = text;
  el.className = `claim-result ${cls || ""}`;
}

function addReleaseButton(row, nodeId) {
  if (row.querySelector(".release-btn")) return;
  const claimBtn = row.querySelector(".claim-btn");
  if (claimBtn) claimBtn.hidden = true;

  const btn = document.createElement("button");
  btn.className = "release-btn";
  btn.textContent = "Release";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const res = await fetch(`${ENGINE_URL}/release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: currentProjectId, holder_token: currentHolderToken, node_id: nodeId }),
      });
      const data = await res.json();
      showClaimResult(row, `Released (${data.released} claim${data.released === 1 ? "" : "s"}).`, "ok");
      btn.remove();
      if (claimBtn) claimBtn.hidden = false;
      // A released claim can no longer be safely edited -- drop the edit
      // section rather than leave a stale input sitting under a row that no
      // longer holds the claim it would need to write back against.
      const editSection = row.querySelector(".edit-section");
      if (editSection) editSection.remove();
    } catch (err) {
      showClaimResult(row, `Release request failed: ${err.message}`, "error");
      btn.disabled = false;
    }
  });
  row.appendChild(btn);
}

/**
 * The editable field(s) for each supported node kind, and how to read a
 * field's CURRENT value off a node object. A kind maps to an ARRAY of field
 * descriptors -- `heading`/`citation` each have exactly one, but
 * `table`/`figure` can have BOTH a caption and a label independently
 * editable (a node is claimed once; either or both of its fields can then be
 * edited under that one claim). `present(node)` gates whether the field
 * actually exists to edit right now -- per this pass's own scope (see
 * saveNodeEdit()'s module-level comment), INSERTING a caption/label that
 * doesn't exist yet is a fundamentally different, riskier operation than
 * replacing one that's already there, so a node with no caption/label simply
 * shows no edit box for that field, same as a kind with no entry here at
 * all keeps the claim-only UI (claim/release, no edit box).
 */
const EDITABLE_FIELDS = {
  // outline.js's title extraction includes a leading "*" for a starred
  // heading (e.g. \section*{Introduction} -> title "*Introduction") --
  // confirmed live, 2026-09-17: EVERY heading in the real dnabert manuscript
  // is starred, so this is the common case, not an edge case. The star is
  // NOT part of the actual argument text findMacroBraceArgs locates (it sits
  // outside the braces, between the macro name and "{"), so stripping it
  // here is purely a display/seed-value fix -- it does not change what
  // computeFieldRange() targets. Left un-stripped, a user who resubmits the
  // seeded value with only a light edit would insert a literal "*" character
  // into the paper's actual heading text, which is wrong output, not a
  // safety issue, but real and worth preventing at the source rather than
  // documenting as a known gotcha.
  heading: [
    {
      field: "title",
      label: "Title",
      get: (node) => (node.title && node.title.startsWith("*") ? node.title.slice(1) : node.title),
      present: () => true,
    },
  ],
  citation: [{ field: "key", label: "Key", get: (node) => node.key, present: () => true }],
  // table/figure: caption and label, each present only when the outline
  // already extracted one (see outline.js's captionLine/labelLine -- both
  // null when the field itself doesn't exist on this node).
  table: [
    { field: "caption", label: "Caption", get: (node) => node.caption, present: (node) => node.captionLine != null },
    { field: "label", label: "Label", get: (node) => node.label, present: (node) => node.labelLine != null },
  ],
  figure: [
    { field: "caption", label: "Caption", get: (node) => node.caption, present: (node) => node.captionLine != null },
    { field: "label", label: "Label", get: (node) => node.label, present: (node) => node.labelLine != null },
  ],
  // A bare inline/display-math "mathenv" equation has no label field at all
  // in the outline data (see outline.js) -- only the environment-shaped
  // kinds (\begin{equation}, \begin{align}, ...) ever have one, and
  // `present` already gates on labelLine existing, so a labelless equation
  // node simply shows no edit box, same as any other kind with nothing to
  // edit yet.
  equation: [{ field: "label", label: "Label", get: (node) => node.label, present: (node) => node.labelLine != null }],
};

/**
 * Debounced (400ms) citation-key check against the engine's /zotero-lookup
 * (see zotero.js/server.js). Debounced because this fires on every
 * keystroke via the input's own "input" listener, not just once -- without
 * debouncing, typing a key would fire one HTTP request per character.
 * `statusEl._zoteroTimer` is a plain ad hoc property on the element (no
 * framework/state layer here), matching this file's existing vanilla-DOM
 * style throughout.
 */
function scheduleZoteroCheck(statusEl, key, delayMs = 400) {
  clearTimeout(statusEl._zoteroTimer);
  const trimmed = (key || "").trim();
  if (!trimmed) {
    statusEl.textContent = "";
    statusEl.className = "zotero-status";
    return;
  }
  statusEl.textContent = "Checking Zotero…";
  statusEl.className = "zotero-status checking";
  statusEl._zoteroTimer = setTimeout(async () => {
    try {
      const res = await fetch(`${ENGINE_URL}/zotero-lookup?key=${encodeURIComponent(trimmed)}`);
      const result = await res.json();
      renderZoteroStatus(statusEl, result);
    } catch {
      renderZoteroStatus(statusEl, { resolved: null });
    }
  }, delayMs);
}

/** Three-way render, matching lookupCitationKey's own three-way result
 * (zotero.js): found / not found / couldn't check at all -- these are
 * genuinely different situations and must not be collapsed into a single
 * ok/error binary (see zotero.js's own header comment). */
function renderZoteroStatus(statusEl, result) {
  if (result.resolved === true) {
    statusEl.textContent = `✓ Zotero: ${result.title || result.tag}`;
    statusEl.className = "zotero-status ok";
  } else if (result.resolved === false) {
    statusEl.textContent = "⚠ Not found in Zotero";
    statusEl.className = "zotero-status warn";
  } else {
    statusEl.textContent = "Zotero unavailable";
    statusEl.className = "zotero-status unknown";
  }
}

/**
 * Renders the "edit this node" UI for a node whose claim just succeeded (or
 * was already held by this session on popup reopen) -- one text input per
 * PRESENT editable field (see EDITABLE_FIELDS), each seeded with that
 * field's current value plus its own "Queue edit" button (see
 * toggleQueueNodeEdit/applyBatch below), since a claim covers the whole
 * node but caption/label are independently queueable/saveable.
 * Idempotent (checked via `.edit-section`) so it's safe to call from both
 * the fresh-claim path and the already-claimed-on-reopen path. No-ops for a
 * `node.kind` not in EDITABLE_FIELDS, or one whose fields are all absent --
 * that node's row just keeps showing claim state with no edit control.
 */
function addEditSection(row, node) {
  const descriptors = (EDITABLE_FIELDS[node.kind] || []).filter((d) => d.present(node));
  if (descriptors.length === 0) return;
  if (row.querySelector(".edit-section")) return;

  const section = document.createElement("div");
  section.className = "edit-section";

  for (const descriptor of descriptors) {
    const fieldRow = document.createElement("div");
    fieldRow.className = "edit-field-row";

    if (descriptors.length > 1) {
      const fieldLabel = document.createElement("span");
      fieldLabel.className = "edit-field-label";
      fieldLabel.textContent = `${descriptor.label}: `;
      fieldRow.appendChild(fieldLabel);
    }

    const input = document.createElement("input");
    input.type = "text";
    input.className = "edit-input";
    input.value = descriptor.get(node);
    // Captured separately from input.value because by the time this field
    // gets queued, input.value IS the new value the user typed -- this is
    // the one place the pre-edit value is still available, for provenance
    // recording (see toggleQueueNodeEdit/applyBatch).
    input.dataset.originalValue = descriptor.get(node);
    fieldRow.appendChild(input);

    // Citation-key validation against the local Zotero library (item
    // 6160d667 piece 2) -- purely informational, never blocks queueing or
    // applying an edit: Zotero not running, or a key legitimately not in
    // the library yet (e.g. a brand-new reference not added yet), are both
    // real, non-error situations a user should be free to proceed through.
    if (node.kind === "citation" && descriptor.field === "key") {
      const zoteroStatus = document.createElement("span");
      zoteroStatus.className = "zotero-status";
      fieldRow.appendChild(zoteroStatus);
      scheduleZoteroCheck(zoteroStatus, input.value);
      input.addEventListener("input", () => scheduleZoteroCheck(zoteroStatus, input.value));
    }

    const saveBtn = document.createElement("button");
    saveBtn.className = "save-edit-btn";
    saveBtn.textContent = "Queue edit";
    saveBtn.addEventListener("click", () => toggleQueueNodeEdit(node, row, input, saveBtn, descriptor.field));
    fieldRow.appendChild(saveBtn);

    section.appendChild(fieldRow);
  }

  row.appendChild(section);
}

async function claimNodeById(node, row) {
  try {
    const res = await fetch(`${ENGINE_URL}/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: currentProjectId, node_id: node.id, holder_token: currentHolderToken }),
    });
    const data = await res.json();
    if (!data.claimed) {
      showClaimResult(row, `Not claimed: ${data.reason || "unknown reason"}`, "error");
      return;
    }
    // Point 3 / point 6: identity_confidence: "low" renders a visible inline
    // warning, never a blocked action -- the claim above already succeeded,
    // and (for the kinds that support editing) it's shown before the edit
    // box below it, not instead of it.
    if (data.identity_confidence === "low") {
      showClaimResult(row, `Claimed. Warning: low identity confidence — ${data.identity_confidence_reason}`, "warning");
    } else {
      showClaimResult(row, "Claimed.", "ok");
    }
    addReleaseButton(row, node.id);
    addEditSection(row, node);
  } catch (err) {
    showClaimResult(row, `Claim request failed: ${err.message}`, "error");
  }
}

/**
 * Renders one row per structural node with a "Claim to edit" button.
 *
 * A claimed node whose kind is in EDITABLE_FIELDS (heading, citation,
 * table, figure, equation) also gets a real edit box per present field --
 * see addEditSection()/saveNodeEdit(). A node whose kind has no entry, or
 * whose fields are all absent (e.g. a table with neither caption nor
 * label), still just shows claim state and a Release button with no edit
 * control.
 */
function renderNodes(nodes, existingClaims) {
  const container = document.getElementById("nodes");
  container.innerHTML = "";
  container.hidden = nodes.length === 0;

  for (const node of nodes) {
    const row = document.createElement("div");
    row.className = "node-row";

    const label = document.createElement("span");
    label.className = "node-label";
    label.textContent = `[${node.kind}] ${nodeLabel(node)}`;
    row.appendChild(label);

    // A node kind CAN appear in the outline with zero present editable
    // fields today -- most commonly a `tabular` nested inside a `table`
    // float with no caption/label of its own (the outer float has those;
    // the inner tabular is still its own distinct, addressable AST node,
    // kept for a future cell/column-edit operation to target -- see
    // outline.test.js's "documented behavior change" test). Offering
    // "Claim to edit" for it today is a dead end: addEditSection renders
    // nothing once claimed, since EDITABLE_FIELDS has nothing present to
    // show. Rather than let a user claim it and then wonder why nothing
    // happened, say so up front and skip the claim button entirely --
    // this changes only what the UI offers, never the outline data itself
    // or the engine's own claim capability (still claimable via a direct
    // API call, if a future feature needs to).
    const hasEditableField = (EDITABLE_FIELDS[node.kind] || []).some((d) => d.present(node));
    if (hasEditableField) {
      const claimBtn = document.createElement("button");
      claimBtn.className = "claim-btn";
      claimBtn.textContent = "Claim to edit";
      claimBtn.addEventListener("click", () => claimNodeById(node, row));
      row.appendChild(claimBtn);
    } else {
      const note = document.createElement("span");
      note.className = "node-no-fields";
      note.textContent = "(no editable fields yet)";
      row.appendChild(note);
    }

    container.appendChild(row);

    const existing = existingClaims[node.id];
    if (existing && existing.holder_token === currentHolderToken) {
      // Reopening the popup on a project where this same holder already
      // holds a still-live claim (within CLAIM_TTL_MINUTES) -- reflect that
      // instead of showing "Claim to edit" as if nothing were claimed.
      showClaimResult(row, "Already claimed by this session.", "ok");
      addReleaseButton(row, node.id);
      addEditSection(row, node);
    } else if (existing) {
      showClaimResult(row, "Currently claimed by another session.", "warning");
    }
  }
}

async function getOutline() {
  // A non-empty pendingBatch here means either (a) applyBatch() already
  // cleared it before calling getOutline() itself (the normal post-apply
  // refresh -- this is a no-op in that case), or (b) something else
  // triggered a re-render (manual "Get structural outline" click, a future
  // auto-refresh) while edits were still queued. renderNodes() below is
  // about to replace every row's DOM wholesale, which would orphan
  // pendingBatch's row/input/queueBtn references -- discard rather than
  // leave a stale, visually-inconsistent "queued" state pointing at
  // detached elements.
  if (pendingBatch.size > 0) {
    pendingBatch.clear();
    updateBatchBar();
  }
  setOutline("Reading editor…");
  try {
    const tab = await getActiveOverleafTab();
    if (!tab) throw new Error("Not on an Overleaf project page.");
    currentProjectId = projectIdFromUrl(tab.url);
    currentHolderToken = await getHolderToken();

    if (currentProjectId) {
      await releaseStaleClaimsOnOpen(currentProjectId, currentHolderToken);
    }

    const text = await getEditorText();
    setOutline(`Sending ${text.length} chars to the local engine (${ENGINE_URL})…`);
    const res = await fetch(`${ENGINE_URL}/outline`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, project_id: currentProjectId || undefined }),
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Engine returned ${res.status}: ${body}`);
    }
    const { nodes, matched, added, removed } = await res.json();
    const counts = {};
    for (const n of nodes) counts[n.kind] = (counts[n.kind] || 0) + 1;
    const summary = Object.entries(counts)
      .map(([kind, n]) => `${kind}: ${n}`)
      .join("\n");
    const diffLine =
      matched !== undefined
        ? `\n(${matched.length} matched, ${added.length} added, ${removed.length} removed vs. last check)`
        : "";
    setOutline(`${nodes.length} nodes\n${summary}${diffLine}`, "ok");

    if (currentProjectId) {
      const existingClaims = await fetchLiveClaimsMap(currentProjectId);
      renderNodes(nodes, existingClaims);
    } else {
      // No project_id derivable (shouldn't happen given the Overleaf-tab
      // check above, but stay defensive) -- claim/release needs one, so
      // don't render claim buttons that would just fail every click.
      document.getElementById("nodes").hidden = true;
    }
  } catch (err) {
    setOutline(
      `${err.message}\n\n(Is the local engine server running? cd meridian-latex/engine && node src/server.js)`,
      "error",
    );
  }
}

// --- Real node editing (claim -> edit -> write -> release) -------------
//
// Builds on three already-proven pieces: the outline+matching engine
// (POST /outline, matchOutlines), the claim coordination layer (POST
// /claim, /release), and the write-dispatch primitive
// (MERIDIAN_LATEX_APPLY_EDITS -> injected.js's applyEdits). This is the
// integration write-back-spec.md section 4 point 2 flagged as real,
// separate follow-on work once the coordination layer existed and was
// trusted -- it now exists (claim/release are live above) so this wires the
// actual text round-trip on top of it.
//
// `heading` (title), `citation` (key), `table`/`figure` (caption and/or
// label), and equation-like environments (label) are all wired up for real
// editing -- see EDITABLE_FIELDS above and computeFieldRange()'s per-kind
// dispatch below.
//
// The caption/label case needed one real fix first, not just new wiring:
// a caption/label can sit anywhere inside a node's `line`..`end_line` span
// (which can cover many lines for a table/figure), and naively scanning
// that whole span risks matching a NESTED environment's own caption/label
// (e.g. a `tabular` float's own caption inside an outer `table` -- see
// engine/README.md's note on why those are now separate nodes) instead of
// the intended one. The fix lives engine-side: outline.js's
// `findFirstMacroInOwnScope` stops at a nested structural boundary instead
// of recursing through it, and each node now carries its OWN field's exact
// source line (`captionLine`/`labelLine`) computed from that scoped search
// -- so this file only ever reads and edits the ONE line the engine already
// resolved unambiguously, the same safe single-line pattern heading/
// citation editing already used.
//
// A bare inline/display-math `equation` node (CM6 "mathenv", no
// environment) still has no `label` field in the outline data at all --
// EDITABLE_FIELDS' `present` check gates on `labelLine != null`, so that
// node kind simply shows no edit box, same as any other absent field.
// INSERTING a caption/label that doesn't exist yet is a fundamentally
// different (and riskier) operation than replacing one that's already
// there, and stays out of scope for this pass.

function showEditResult(row, text, cls) {
  let el = row.querySelector(".edit-result");
  if (!el) {
    el = document.createElement("div");
    el.className = "edit-result";
    row.appendChild(el);
  }
  el.textContent = text;
  el.className = `edit-result ${cls || ""}`;
}

/** Promisified wrapper around the MERIDIAN_LATEX_GET_LINE_INFO relay (see
 * content_script.js/injected.js's getLineInfo()). Resolves to
 * `{found, from, to, text}` or `{found: false, reason}` -- never rejects,
 * matching the rest of this file's message-passing helpers. */
function getLineInfo(tab, lineNumber) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tab.id, { type: "MERIDIAN_LATEX_GET_LINE_INFO", lineNumber }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        resolve({ found: false, reason: "No response from content script — try reloading the Overleaf tab." });
        return;
      }
      resolve(resp);
    });
  });
}

/** Promisified wrapper around MERIDIAN_LATEX_APPLY_EDITS, for the real edit
 * flow below. Separate from the raw diagnostic button's own inline callback
 * (applyTestEdit) -- kept independent deliberately so nothing here touches
 * that still-useful low-level debugging path. */
function applyEditsOnTab(tab, edits) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tab.id, { type: "MERIDIAN_LATEX_APPLY_EDITS", edits }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        resolve({ applied: false, reason: "No response from content script — try reloading the Overleaf tab." });
        return;
      }
      resolve(resp);
    });
  });
}

/** Every node in `nodes` (a fresh outline array) that shares `target`'s
 * `kind` and `line` -- and, for a heading, its `level` too (so a
 * `\section{}` on the same line as an unrelated `\subsection{}` never gets
 * confused for a sibling). Order matches `nodes`' own array order, which is
 * document order (see outline.js's visit()) -- the same left-to-right order
 * the locate* functions below scan the raw line text in, so the Nth sibling
 * here should always line up with the Nth occurrence found in the text.
 * Used to disambiguate WHICH occurrence on a line is this specific node,
 * for the (rare, but real) case of more than one same-kind node on one
 * physical line. */
function siblingsOnLine(nodes, target) {
  return nodes.filter(
    (n) =>
      n.kind === target.kind &&
      n.line === target.line &&
      (target.kind !== "heading" || n.level === target.level),
  );
}

/** Index of the first `}` that closes the `{` at `text[openIdx]`, tracking
 * nested-brace depth (so `\section{A \textbf{B}}` still resolves to the
 * OUTER closing brace, not the first `}` encountered). Returns -1 if the
 * text never balances back to depth 0 (an unclosed brace on this line --
 * e.g. a macro argument that wraps onto the next line, which this v1 does
 * not follow across lines). `text[openIdx]` must be `{`. */
function matchBraceIndex(text, openIdx) {
  let depth = 0;
  for (let i = openIdx; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** Every `{...}` argument immediately following each occurrence of
 * `macroPrefix` (a literal string ending in `{`, e.g. `"\\section{"`) on
 * `text`, left to right. Each result is `{start, end}` -- the argument's
 * content span, exclusive of the braces themselves. A macro occurrence with
 * no balanced closing brace on this line is skipped (not reported) rather
 * than guessed at. */
function findAllBraceArgs(text, macroPrefix) {
  const results = [];
  let searchFrom = 0;
  while (true) {
    const idx = text.indexOf(macroPrefix, searchFrom);
    if (idx === -1) break;
    const braceOpen = idx + macroPrefix.length - 1;
    const closeIdx = matchBraceIndex(text, braceOpen);
    if (closeIdx === -1) {
      searchFrom = idx + macroPrefix.length;
      continue;
    }
    results.push({ start: braceOpen + 1, end: closeIdx });
    searchFrom = closeIdx + 1;
  }
  return results;
}

/** Every `{...}` argument immediately following each occurrence of a macro
 * named `macroName` on `text`, left to right -- tolerant of an optional `*`
 * between the macro name and its argument brace (`\section{...}` AND
 * `\section*{...}` both match a search for macroName="section"). This is a
 * REAL, common case, not a hypothetical: found live, 2026-09-17, testing
 * against the actual dnabert manuscript -- every single heading in that real
 * document is starred (`\section*{}`, `\subsection*{}`, `\paragraph*{}`,
 * PLOS's own unnumbered-heading convention), so the earlier exact-prefix
 * version of this search (`findAllBraceArgs(text, "\\" + level + "{")`)
 * found ZERO occurrences for every real heading in that paper and always hit
 * the occurrence-count safety abort -- safe (never a wrong edit) but useless
 * (never a successful one either) for the overwhelmingly common real case.
 * Each result is `{start, end}` -- the argument's content span, exclusive of
 * the braces themselves. A macro occurrence with no balanced closing brace on
 * this line, or whose character immediately after the (optional) `*` isn't
 * `{`, is skipped (not reported) rather than guessed at. */
function findMacroBraceArgs(text, macroName) {
  const results = [];
  const anchor = `\\${macroName}`;
  let searchFrom = 0;
  while (true) {
    const idx = text.indexOf(anchor, searchFrom);
    if (idx === -1) break;
    let braceOpen = idx + anchor.length;
    if (text[braceOpen] === "*") braceOpen += 1;
    if (text[braceOpen] !== "{") {
      // Not a real match at all (e.g. this "\section" is actually the start
      // of "\subsectionfoo" or some other longer macro name, or a bare macro
      // with no argument on this line) -- move past just the anchor, not the
      // whole remaining line, so a genuine later occurrence is still found.
      searchFrom = idx + anchor.length;
      continue;
    }
    const closeIdx = matchBraceIndex(text, braceOpen);
    if (closeIdx === -1) {
      searchFrom = braceOpen + 1;
      continue;
    }
    results.push({ start: braceOpen + 1, end: closeIdx });
    searchFrom = closeIdx + 1;
  }
  return results;
}

/** Every comma-separated key segment inside every `\cite{...}` occurrence on
 * `text`, left to right, flattened into one ordered list -- mirroring
 * outline.js's own `raw.split(",").map(k => k.trim())` extraction exactly,
 * but keeping each segment's untrimmed `{start, end}` offsets (trimmed down
 * to the key's own span, so the returned range excludes surrounding
 * whitespace) alongside its trimmed `text`. A `\cite{}` with no balanced
 * closing brace on this line is skipped, same as findAllBraceArgs. */
function findCiteSegments(text) {
  const segments = [];
  const prefix = "\\cite{";
  let searchFrom = 0;
  while (true) {
    const idx = text.indexOf(prefix, searchFrom);
    if (idx === -1) break;
    const braceOpen = idx + prefix.length - 1;
    const closeIdx = matchBraceIndex(text, braceOpen);
    if (closeIdx === -1) {
      searchFrom = idx + prefix.length;
      continue;
    }
    const argStart = braceOpen + 1;
    const argText = text.slice(argStart, closeIdx);
    let partOffset = 0;
    for (const part of argText.split(",")) {
      const rawStart = argStart + partOffset;
      const leadWs = part.match(/^\s*/)[0].length;
      const trailWs = part.match(/\s*$/)[0].length;
      segments.push({
        start: rawStart + leadWs,
        end: rawStart + part.length - trailWs,
        text: part.trim(),
      });
      partOffset += part.length + 1; // +1 for the comma consumed by split()
    }
    searchFrom = closeIdx + 1;
  }
  return segments;
}

/** Computes `{from, to}` (line-relative, added to `lineInfo.from` by the
 * caller) for a heading node's title argument. Does NOT hard-abort on a
 * text mismatch between the outline's `title` and the raw source at that
 * span -- a title's rendered text can legitimately differ from raw source
 * when it contains nested macros (e.g. `\section{A \textit{B}}` renders as
 * title "A B"), so an exact-text check would false-positive on real,
 * unremarkable documents. The occurrence-COUNT check (siblings vs. brace
 * occurrences found) is the real safety net here. */
function locateHeadingRange(lineInfo, target, siblings) {
  const idx = siblings.findIndex((n) => n.id === target.id);
  if (idx === -1) {
    return { ok: false, reason: "Internal error: node not found among its own line siblings." };
  }
  const occurrences = findMacroBraceArgs(lineInfo.text, target.level);
  if (occurrences.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `Heading count mismatch on line ${target.line}: the outline reports ${siblings.length} ` +
        `"${target.level}" heading(s) there, but ${occurrences.length} "\\${target.level}" (optionally ` +
        `starred) occurrence(s) were found scanning the live line text. Aborting edit for safety.`,
    };
  }
  const occ = occurrences[idx];
  return { ok: true, from: lineInfo.from + occ.start, to: lineInfo.from + occ.end };
}

/** Computes `{from, to}` (line-relative) for a citation node's key. Unlike
 * the heading case above, THIS one hard-aborts on a text mismatch: a
 * citation key inside `\cite{...}` has no legitimate reason to render
 * differently from its raw source (no nested macros expected there), so a
 * mismatch means this function's reconstruction of the line has diverged
 * from what outline.js actually saw -- safer to abort than guess. */
function locateCitationRange(lineInfo, target, siblings) {
  const idx = siblings.findIndex((n) => n.id === target.id);
  if (idx === -1) {
    return { ok: false, reason: "Internal error: node not found among its own line siblings." };
  }
  const segments = findCiteSegments(lineInfo.text);
  if (segments.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `Citation count mismatch on line ${target.line}: the outline reports ${siblings.length} ` +
        `citation(s) there, but ${segments.length} were found scanning the live line text. ` +
        `Aborting edit for safety.`,
    };
  }
  const seg = segments[idx];
  if (seg.text !== target.key) {
    return {
      ok: false,
      reason:
        `Citation key mismatch at position ${idx} on line ${target.line}: expected "${target.key}", ` +
        `found "${seg.text}" in the live document. Aborting edit for safety.`,
    };
  }
  return { ok: true, from: lineInfo.from + seg.start, to: lineInfo.from + seg.end };
}

/** Every node in `nodes` whose OWN `lineField` (e.g. "captionLine") equals
 * `target`'s -- the caption/label analogue of `siblingsOnLine` above, used
 * to disambiguate which occurrence on that line is this specific node's
 * field when more than one node's same-named field happens to land on the
 * identical physical source line (rare, but the same paranoia the heading/
 * citation paths already apply). `null`-valued fields never match (both
 * `siblingsOnLine`'s heading/citation `line` and this one exclude a node
 * with no line for the field in question -- see EDITABLE_FIELDS' `present`
 * gate, which already ensures `target[lineField]` itself is non-null here).
 */
function siblingsOnFieldLine(nodes, target, lineField) {
  return nodes.filter((n) => n[lineField] != null && n[lineField] === target[lineField]);
}

/** Computes `{from, to}` (line-relative) for a table/figure node's caption,
 * or an equation-like node's label -- shared by locateCaptionRange and
 * locateLabelRange below, parameterized by which macro/line-field/node
 * property to use. Same occurrence-COUNT safety net as
 * locateHeadingRange/locateCitationRange: if the number of `\macroName{`
 * occurrences found scanning the live line doesn't match the number of
 * outline nodes that claim that exact line for this field, abort rather
 * than guess. Unlike the heading case (which tolerates a rendered/raw text
 * mismatch for legitimate nested-macro reasons) this hard-aborts on a text
 * mismatch too, same as citation -- a caption/label field found via a
 * SCOPED search (findFirstMacroInOwnScope, engine-side) has no legitimate
 * reason to render differently from its raw source at the specific
 * occurrence this function locates. */
function locateCaptionOrLabelRange(lineInfo, target, nodes, { lineField, nodeProp, macroName }) {
  const siblings = siblingsOnFieldLine(nodes, target, lineField);
  const idx = siblings.findIndex((n) => n.id === target.id);
  if (idx === -1) {
    return { ok: false, reason: "Internal error: node not found among its own field-line siblings." };
  }
  const occurrences = findAllBraceArgs(lineInfo.text, `\\${macroName}{`);
  if (occurrences.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `${macroName} count mismatch on line ${target[lineField]}: the outline reports ${siblings.length} ` +
        `node(s) with a ${macroName} there, but ${occurrences.length} "\\${macroName}{" occurrence(s) were ` +
        `found scanning the live line text. Aborting edit for safety.`,
    };
  }
  const occ = occurrences[idx];
  const found = lineInfo.text.slice(occ.start, occ.end);
  if (found !== target[nodeProp]) {
    return {
      ok: false,
      reason:
        `${macroName} text mismatch at position ${idx} on line ${target[lineField]}: expected ` +
        `"${target[nodeProp]}", found "${found}" in the live document. Aborting edit for safety.`,
    };
  }
  return { ok: true, from: lineInfo.from + occ.start, to: lineInfo.from + occ.end };
}

function locateCaptionRange(lineInfo, target, nodes) {
  return locateCaptionOrLabelRange(lineInfo, target, nodes, {
    lineField: "captionLine",
    nodeProp: "caption",
    macroName: "caption",
  });
}

function locateLabelRange(lineInfo, target, nodes) {
  return locateCaptionOrLabelRange(lineInfo, target, nodes, {
    lineField: "labelLine",
    nodeProp: "label",
    macroName: "label",
  });
}

/**
 * Resolves `target` (a freshly re-matched node -- see saveNodeEdit) down to
 * an absolute `{from, to}` document offset for ONE of its editable fields
 * (`fieldName`, e.g. "title"/"key"/"caption"/"label"), by reading the
 * CURRENT live line the field itself sits on (its own captionLine/labelLine
 * for table/figure/equation, or the node's own `line` for heading/citation)
 * via getLineInfo() and locating the field within it. `nodes` is the full
 * fresh outline array (needed to compute `target`'s siblings for occurrence
 * disambiguation). Returns `{ok: true, from, to}` or `{ok: false, reason}`.
 */
async function computeFieldRange(tab, target, nodes, fieldName) {
  if (target.kind === "heading" || target.kind === "citation") {
    if (target.line == null) {
      return { ok: false, reason: "This node has no line number in the current outline data." };
    }
    const lineInfo = await getLineInfo(tab, target.line);
    if (!lineInfo.found) {
      return {
        ok: false,
        reason: `Could not read line ${target.line} from the live document: ${lineInfo.reason || "unknown reason"}`,
      };
    }
    const siblings = siblingsOnLine(nodes, target);
    if (target.kind === "heading") return locateHeadingRange(lineInfo, target, siblings);
    return locateCitationRange(lineInfo, target, siblings);
  }

  if (fieldName === "caption" || fieldName === "label") {
    const lineField = fieldName === "caption" ? "captionLine" : "labelLine";
    const fieldLine = target[lineField];
    if (fieldLine == null) {
      return { ok: false, reason: `This node has no ${fieldName} in the current outline data.` };
    }
    const lineInfo = await getLineInfo(tab, fieldLine);
    if (!lineInfo.found) {
      return {
        ok: false,
        reason: `Could not read line ${fieldLine} from the live document: ${lineInfo.reason || "unknown reason"}`,
      };
    }
    return fieldName === "caption"
      ? locateCaptionRange(lineInfo, target, nodes)
      : locateLabelRange(lineInfo, target, nodes);
  }

  return { ok: false, reason: `Editing field "${fieldName}" on kind "${target.kind}" is not supported.` };
}

// --- Batched multi-node edit (README "what's next": batched transaction,
// not permanent per-write confirmation; decision 28ebe1f0 design 1) -------
//
// Per-field "Queue edit" replaces the old immediate-dispatch "Save edit":
// queuing several fields (across one or many claimed nodes) accumulates
// them in `pendingBatch`, and ONE "Apply queued edits" click dispatches
// every queued edit as a SINGLE CM6 transaction (view.dispatch already
// accepts an array of ChangeSpecs, applied atomically -- injected.js's
// applyEdits() has supported this since it was written; this is purely a
// popup.js orchestration gap being closed, not a new engine/injected.js
// capability). One confirmation for N edits, matching the intended UX the
// README's "what's next" section described rather than the
// validation-phase per-write dialog.
//
// `pendingBatch` is keyed by `${node.id}:${field}` so re-clicking "Queue
// edit" on the same field updates its queued value in place instead of
// creating a duplicate entry.
const pendingBatch = new Map();

function batchKey(nodeId, field) {
  return `${nodeId}:${field}`;
}

function getBatchBarEl() {
  return document.getElementById("batch-bar");
}

/** Re-renders the batch bar's summary/item list from `pendingBatch`'s
 * current contents, showing/hiding the whole bar based on whether it's
 * empty. Called after every queue/unqueue/apply/cancel. */
function updateBatchBar() {
  const bar = getBatchBarEl();
  const summary = document.getElementById("batch-bar-summary");
  const items = document.getElementById("batch-bar-items");

  if (pendingBatch.size === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  summary.textContent = `${pendingBatch.size} edit${pendingBatch.size === 1 ? "" : "s"} queued.`;
  items.innerHTML = "";
  for (const [key, entry] of pendingBatch) {
    const itemRow = document.createElement("div");
    itemRow.className = "batch-item-row";
    const label = document.createElement("span");
    label.textContent = `[${entry.node.kind}] ${entry.field}: "${entry.newValue}"`;
    itemRow.appendChild(label);
    const removeBtn = document.createElement("button");
    removeBtn.className = "batch-item-remove";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => unqueueByKey(key));
    itemRow.appendChild(removeBtn);
    items.appendChild(itemRow);
  }
}

/** Reverts one queued field's row UI back to its pre-queue state (button
 * re-labeled "Queue edit", input re-enabled) -- shared by the explicit
 * per-item "Remove" button and by toggling an already-queued field's own
 * button back off. */
function resetQueueUi(entry) {
  entry.input.disabled = false;
  entry.queueBtn.disabled = false;
  entry.queueBtn.textContent = "Queue edit";
  entry.queueBtn.classList.remove("queued");
}

function unqueueByKey(key) {
  const entry = pendingBatch.get(key);
  if (!entry) return;
  pendingBatch.delete(key);
  resetQueueUi(entry);
  updateBatchBar();
}

/**
 * Per-field "Queue edit" button handler: queues the field's current input
 * value (or, if already queued, un-queues it -- a toggle, same button).
 * Does NOT touch the engine or the live document at all -- purely local
 * state + UI until "Apply queued edits" runs. Validates non-empty input
 * the same way the old immediate-save flow did.
 */
function toggleQueueNodeEdit(node, row, input, queueBtn, field) {
  const key = batchKey(node.id, field);
  if (pendingBatch.has(key)) {
    unqueueByKey(key);
    return;
  }
  const newValue = input.value;
  if (!newValue.trim()) {
    showEditResult(row, "Type a non-empty value first.", "error");
    return;
  }
  pendingBatch.set(key, { node, field, newValue, oldValue: input.dataset.originalValue, row, input, queueBtn });
  input.disabled = true;
  queueBtn.textContent = "Queued (click to unqueue)";
  queueBtn.classList.add("queued");
  showEditResult(row, "", "");
  updateBatchBar();
}

function showBatchResult(text, cls) {
  const el = document.getElementById("batch-result");
  el.hidden = false;
  el.textContent = text;
  el.className = cls || "";
}

function cancelBatch() {
  for (const entry of pendingBatch.values()) resetQueueUi(entry);
  pendingBatch.clear();
  updateBatchBar();
}

/**
 * The real claim -> edit -> write -> release flow, now for the WHOLE
 * queued batch as one atomic operation (spec points 3-5, generalized from
 * one node to N). Every queued entry's `node` is used only as a LOOKUP KEY
 * into a SINGLE fresh re-fetch below; every actual offset/text computation
 * uses that one fresh, re-matched outline, never anything read earlier --
 * the same never-trust-a-stale-read discipline the old single-edit flow
 * used, now applied once for the whole batch rather than once per edit.
 *
 * All-or-nothing by construction: if ANY queued entry fails to re-match or
 * fails to locate its field on the live document, the WHOLE batch aborts
 * before anything is dispatched -- no partial application. This mirrors
 * injected.js's own applyEdits() contract (reject the whole array on any
 * single bad edit) one layer up, at the outline-matching level.
 */
async function applyBatch() {
  if (pendingBatch.size === 0) return;
  const entries = [...pendingBatch.values()];
  const applyBtn = document.getElementById("batch-apply-btn");
  const cancelBtn = document.getElementById("batch-cancel-btn");
  applyBtn.disabled = true;
  cancelBtn.disabled = true;
  showBatchResult(`Re-fetching the live outline before writing ${entries.length} edit(s)…`);

  try {
    const tab = await getActiveOverleafTab();
    if (!tab) throw new Error("Not on an Overleaf project page.");
    if (!currentProjectId || !currentHolderToken) {
      throw new Error("No active claim session for this project — re-open the popup and re-claim.");
    }

    // Step 1: ONE fresh outline re-fetch for the entire batch.
    const text = await getEditorText();
    const res = await fetch(`${ENGINE_URL}/outline`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, project_id: currentProjectId }),
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Engine returned ${res.status} re-fetching outline: ${body}`);
    }
    const { nodes, matched } = await res.json();

    // Step 2 + 3: re-locate + compute a range for EVERY queued entry
    // against that one fresh outline. Any single failure aborts the whole
    // batch (all-or-nothing) -- named precisely so the user knows which
    // queued edit to fix or remove, rather than a vague "batch failed".
    const resolved = [];
    for (const entry of entries) {
      const matchEntry = (matched || []).find((m) => m.oldId === entry.node.id);
      if (!matchEntry) {
        throw new Error(
          `[${entry.node.kind}] ${entry.field}: this node is no longer present in the live document ` +
            "(deleted, or the match was lost). Aborting the whole batch — remove this entry and retry.",
        );
      }
      if (matchEntry.newId !== matchEntry.oldId) {
        throw new Error(
          `[${entry.node.kind}] ${entry.field}: this node's content changed since it was claimed ` +
            `(fingerprint ${matchEntry.oldId} -> ${matchEntry.newId}) — something else edited it. ` +
            "Aborting the whole batch — remove this entry, release, and re-claim to edit its current state.",
        );
      }
      const range = await computeFieldRange(tab, matchEntry.node, nodes, entry.field);
      if (!range.ok) {
        throw new Error(`[${entry.node.kind}] ${entry.field}: ${range.reason}`);
      }
      resolved.push({ entry, from: range.from, to: range.to, insert: entry.newValue });
    }

    // Step 4: ONE dispatch, all edits as a single CM6 transaction. Sorted
    // by `from` purely so a human reading applyEdits' own overlap-rejection
    // error (if the ranges genuinely do overlap) sees them in document
    // order -- applyEdits' own validation re-sorts internally regardless,
    // so this has no effect on correctness, only on error readability.
    resolved.sort((a, b) => a.from - b.from);
    showBatchResult(`Dispatching ${resolved.length} edit(s) as one transaction…`);
    const applyResp = await applyEditsOnTab(
      tab,
      resolved.map((r) => ({ from: r.from, to: r.to, insert: r.insert })),
    );
    if (!applyResp.applied) {
      throw new Error(`Write failed: ${applyResp.reason || "unknown reason"}`);
    }
    const verified = Array.isArray(applyResp.verified) ? applyResp.verified : [];
    if (!(verified.length === resolved.length && verified.every(Boolean))) {
      throw new Error(
        "The batch was dispatched but readback verification failed for at least one edit — the live " +
          "document may not fully reflect the requested changes. Check Overleaf directly before retrying.",
      );
    }

    // Step 5: record provenance for every queued edit, then release each
    // node's claim, and refresh the outline. Provenance recording is
    // best-effort and never fatal here -- the actual write to Overleaf
    // already succeeded and was readback-verified above; a local audit-log
    // failure must not make that look like a failed edit, and must not block
    // releasing the claim (see provenance.js's header comment for why this
    // is a local ledger, not a live meridian-outputs call).
    for (const { entry } of resolved) {
      try {
        await fetch(`${ENGINE_URL}/provenance`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_id: currentProjectId,
            node_id: entry.node.id,
            kind: entry.node.kind,
            field: entry.field,
            old_value: entry.oldValue,
            new_value: entry.newValue,
            holder_token: currentHolderToken,
          }),
        });
      } catch (err) {
        console.warn("Provenance recording failed (edit itself already succeeded):", err);
      }
      await fetch(`${ENGINE_URL}/release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: currentProjectId,
          holder_token: currentHolderToken,
          node_id: entry.node.id,
        }),
      });
    }

    showBatchResult(`${resolved.length} edit(s) applied and verified. Refreshing outline…`, "ok");
    pendingBatch.clear();
    await getOutline();
  } catch (err) {
    showBatchResult(err.message, "error");
    applyBtn.disabled = false;
    cancelBtn.disabled = false;
  }
}

/**
 * "Check CM6 access" -- diagnostic only, proving the MAIN-world
 * findFromDOM mechanism works. Deliberately separate from "Get structural
 * outline": this button never touches the local engine server, never reads
 * the full document, and has no claim/release involvement. Read-only sanity
 * check (doc length, line count, first ~200 chars) -- see the pinned
 * Meridian decision on the write-back safe path (2026-09-17, project
 * meridian-build) for why this is scaffolded as read-only-first.
 */
function setCm6Info(text, cls) {
  const el = document.getElementById("cm6-info");
  el.hidden = false;
  el.textContent = text;
  el.className = cls || "";
}

async function checkCm6Access() {
  setCm6Info("Checking CM6 access…");
  const tab = await getActiveOverleafTab();
  if (!tab) {
    setCm6Info("Not on an Overleaf project page.", "error");
    return;
  }
  chrome.tabs.sendMessage(tab.id, { type: "MERIDIAN_LATEX_GET_CM6_INFO" }, (resp) => {
    if (chrome.runtime.lastError || !resp) {
      setCm6Info("No response from content script — try reloading the Overleaf tab.", "error");
      return;
    }
    if (!resp.found) {
      setCm6Info(`Not found: ${resp.reason || "unknown reason"}`, "error");
      return;
    }
    setCm6Info(
      `EditorView found via findFromDOM.\nlength: ${resp.length}\nlines: ${resp.lines}\npreview: ${JSON.stringify(resp.preview)}`,
      "ok",
    );
  });
}

/**
 * "Insert test edit at document start" -- diagnostic-only exercise of the
 * new write-dispatch primitive (MERIDIAN_LATEX_APPLY_EDITS ->
 * content_script.js's applyEditsViaMainWorld -> injected.js's applyEdits ->
 * view.dispatch). Deliberately minimal: a single `{from: 0, to: 0, insert}`
 * edit, not a claim-integrated editing flow -- that's separate, later work
 * once this primitive is proven (write-back-spec.md section 4, point 2).
 * Shows the full structured result (applied/verified/reason) rather than
 * collapsing it to a yes/no, so a failed validation or readback mismatch is
 * visible, not silently swallowed.
 */
function setApplyEditResult(text, cls) {
  const el = document.getElementById("apply-edit-result");
  el.hidden = false;
  el.textContent = text;
  el.className = cls || "";
}

async function applyTestEdit() {
  const input = document.getElementById("test-edit-text");
  const text = input.value;
  if (!text) {
    setApplyEditResult("Type some text first.", "error");
    return;
  }

  setApplyEditResult("Applying test edit…");
  const tab = await getActiveOverleafTab();
  if (!tab) {
    setApplyEditResult("Not on an Overleaf project page.", "error");
    return;
  }

  chrome.tabs.sendMessage(
    tab.id,
    { type: "MERIDIAN_LATEX_APPLY_EDITS", edits: [{ from: 0, to: 0, insert: text }] },
    (resp) => {
      if (chrome.runtime.lastError || !resp) {
        setApplyEditResult("No response from content script — try reloading the Overleaf tab.", "error");
        return;
      }
      if (!resp.applied) {
        setApplyEditResult(`Not applied: ${resp.reason || "unknown reason"}`, "error");
        return;
      }
      const verified = Array.isArray(resp.verified) ? resp.verified : [];
      const allVerified = verified.length > 0 && verified.every(Boolean);
      setApplyEditResult(
        `Applied.\nnewLength: ${resp.newLength}\nverified: ${JSON.stringify(verified)}`,
        allVerified ? "ok" : "warning",
      );
    },
  );
}

// Best-effort release-all-for-this-holder on popup unload. chrome.runtime
// doesn't reliably fire an event when a popup closes, so this is paired
// with releaseStaleClaimsOnOpen() above as the other half of "belt and
// suspenders" (write-back-spec.md section 4, point 4) rather than relied on
// alone. sendBeacon (not fetch) because a popup's page is already tearing
// down by the time pagehide fires -- an ordinary fetch can be cancelled
// mid-flight, sendBeacon is the browser-level primitive built for exactly
// this "fire this request even though the page is going away" case.
window.addEventListener("pagehide", () => {
  if (!currentProjectId || !currentHolderToken) return;
  const body = JSON.stringify({ project_id: currentProjectId, holder_token: currentHolderToken });
  const blob = new Blob([body], { type: "application/json" });
  navigator.sendBeacon(`${ENGINE_URL}/release`, blob);
});

document.getElementById("refresh").addEventListener("click", check);
document.getElementById("get-outline").addEventListener("click", getOutline);
document.getElementById("check-cm6").addEventListener("click", checkCm6Access);
document.getElementById("apply-test-edit").addEventListener("click", applyTestEdit);
document.getElementById("batch-apply-btn").addEventListener("click", applyBatch);
document.getElementById("batch-cancel-btn").addEventListener("click", cancelBatch);
check();
