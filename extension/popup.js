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

function getEditorText() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
      if (!tab || !tab.url || !tab.url.includes("overleaf.com/project/")) {
        reject(new Error("Not on an Overleaf project page."));
        return;
      }
      chrome.tabs.sendMessage(tab.id, { type: "MERIDIAN_LATEX_GET_TEXT" }, (resp) => {
        if (chrome.runtime.lastError || !resp || resp.text == null) {
          reject(new Error("Could not read editor text — try reloading the Overleaf tab."));
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
 * The editable field for each supported node kind, and how to read its
 * CURRENT value off a node object. Only `heading` (title) and `citation`
 * (key) are wired up for real editing -- see saveNodeEdit()'s module-level
 * comment for why `table`/`figure` captions and `equation` labels are not.
 * A kind with no entry here just keeps the claim-only UI (claim/release,
 * no edit box) -- see addEditSection() below.
 */
const EDITABLE_FIELDS = {
  heading: (node) => node.title,
  citation: (node) => node.key,
};

/**
 * Renders the "edit this node" UI for a node whose claim just succeeded (or
 * was already held by this session on popup reopen) -- a text input seeded
 * with the field's current value plus a "Save edit" button. Idempotent
 * (checked via `.edit-section`) so it's safe to call from both the
 * fresh-claim path and the already-claimed-on-reopen path. No-ops for a
 * `node.kind` not in EDITABLE_FIELDS -- that node's row just keeps showing
 * claim state with no edit control, per the scope decision above.
 */
function addEditSection(row, node) {
  const getField = EDITABLE_FIELDS[node.kind];
  if (!getField) return;
  if (row.querySelector(".edit-section")) return;

  const section = document.createElement("div");
  section.className = "edit-section";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "edit-input";
  input.value = getField(node);
  section.appendChild(input);

  const saveBtn = document.createElement("button");
  saveBtn.className = "save-edit-btn";
  saveBtn.textContent = "Save edit";
  saveBtn.addEventListener("click", () => saveNodeEdit(node, row, input, saveBtn));
  section.appendChild(saveBtn);

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
 * A claimed node whose kind is in EDITABLE_FIELDS (heading, citation) also
 * gets a real edit box (see addEditSection()/saveNodeEdit()) -- the
 * paragraph/section text round-trip write-back-spec.md's section 4/5
 * originally scoped out as separate follow-on work. Other kinds
 * (table/figure/equation) still just show claim state and a Release button,
 * same as before -- see saveNodeEdit()'s comment for why those were left out
 * of this pass.
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

    const claimBtn = document.createElement("button");
    claimBtn.className = "claim-btn";
    claimBtn.textContent = "Claim to edit";
    claimBtn.addEventListener("click", () => claimNodeById(node, row));
    row.appendChild(claimBtn);

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
// Only `heading` (title) and `citation` (key) are wired up for real
// editing -- see EDITABLE_FIELDS above and computeFieldRange()'s per-kind
// comments below for why `table`/`figure` captions and `equation` labels
// were left out of this pass rather than handled unreliably:
//  - A caption/label can appear anywhere inside a node's `line`..`end_line`
//    span (which can cover many lines for a table/figure), so locating "the"
//    \caption{}/\label{} reliably needs a multi-line scan that risks
//    matching a NESTED environment's own caption/label (e.g. a `tabular`
//    inside a `table` float -- see engine/README.md's note on why those are
//    now separate nodes) instead of the intended one.
//  - A bare inline/display-math `equation` node (CM6 "mathenv", no
//    environment) has no `label` field in the outline data at all -- adding
//    one would mean INSERTING new `\label{...}` syntax, a fundamentally
//    different (and riskier) operation than replacing an existing field's
//    text, which is genuinely out of scope for this pass per the task's own
//    framing ("editing the addressable field(s) already in the node's own
//    outline data").
// Both `heading` and `citation` fields, by contrast, are guaranteed to
// already exist as plain text inside a single known line whenever the node
// itself exists (a heading's `title` comes straight from its own
// `\level{...}` argument; a citation's `key` from its own `\cite{...}`
// argument) -- the two cases this pass handles well rather than everything
// unreliably.

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
  const macroPrefix = `\\${target.level}{`;
  const occurrences = findAllBraceArgs(lineInfo.text, macroPrefix);
  if (occurrences.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `Heading count mismatch on line ${target.line}: the outline reports ${siblings.length} ` +
        `"${target.level}" heading(s) there, but ${occurrences.length} "${macroPrefix}" occurrence(s) ` +
        `were found scanning the live line text. Aborting edit for safety.`,
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

/**
 * Resolves `target` (a freshly re-matched node -- see saveNodeEdit) down to
 * an absolute `{from, to}` document offset for its editable field, by
 * reading the CURRENT live line via getLineInfo() and locating the field
 * within it. `nodes` is the full fresh outline array (needed to compute
 * `target`'s siblings-on-this-line for occurrence disambiguation). Returns
 * `{ok: true, from, to}` or `{ok: false, reason}`.
 */
async function computeFieldRange(tab, target, nodes) {
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
  if (target.kind === "citation") return locateCitationRange(lineInfo, target, siblings);
  return { ok: false, reason: `Editing kind "${target.kind}" is not supported.` };
}

/**
 * The real claim -> edit -> write -> release flow for one node (spec
 * points 3-5). `node` is the node object as it stood when the row's edit
 * box was rendered (i.e. at claim time or popup-reopen time) -- it is used
 * only as the LOOKUP KEY (`node.id`) into a fresh re-fetch below; every
 * actual offset/text computation uses the re-fetched, re-matched current
 * node, never anything read earlier. This mirrors the exact same
 * never-trust-a-stale-read discipline injected.js's applyEdits() already
 * applies at the CM6 layer -- this function is the layer above it that
 * re-verifies at the OUTLINE level before ever calling down into that.
 */
async function saveNodeEdit(node, row, input, saveBtn) {
  const newValue = input.value;
  if (!newValue.trim()) {
    showEditResult(row, "Type a non-empty value first.", "error");
    return;
  }

  saveBtn.disabled = true;
  input.disabled = true;
  showEditResult(row, "Re-fetching the live outline before writing…");

  try {
    const tab = await getActiveOverleafTab();
    if (!tab) throw new Error("Not on an Overleaf project page.");
    if (!currentProjectId || !currentHolderToken) {
      throw new Error("No active claim session for this project — re-open the popup and re-claim.");
    }

    // Step 1: re-fetch the live outline fresh -- never the stale one from
    // whenever the node was claimed (spec point 3).
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

    // Step 2: re-locate the SAME logical node via matchOutlines (already run
    // server-side by POST /outline above). If its fingerprint id changed,
    // that's a real signal the node's content moved/changed since it was
    // claimed -- abort rather than blindly write to whatever now sits at
    // the old id.
    const entry = (matched || []).find((m) => m.oldId === node.id);
    if (!entry) {
      throw new Error(
        "This node is no longer present in the live document (it may have been deleted, or the match " +
          "was lost). Aborting the edit — release this claim and re-check the document.",
      );
    }
    if (entry.newId !== entry.oldId) {
      throw new Error(
        `This node's content changed since it was claimed (fingerprint ${entry.oldId} -> ${entry.newId}), ` +
          "meaning something else edited it in the meantime. Aborting to avoid overwriting content that " +
          "isn't what you saw when you claimed it — release and re-claim to edit its current state.",
      );
    }
    const freshNode = entry.node;

    // Step 3: compute {from, to} for the field, against the CURRENT live
    // line (never anything read earlier this session).
    showEditResult(row, "Locating field text on the live line…");
    const range = await computeFieldRange(tab, freshNode, nodes);
    if (!range.ok) throw new Error(range.reason);

    // Step 4: dispatch via the existing write-dispatch primitive -- a
    // single edit; batching multiple simultaneous node edits is deferred,
    // per the spec, to later work.
    showEditResult(row, "Dispatching edit…");
    const applyResp = await applyEditsOnTab(tab, [{ from: range.from, to: range.to, insert: newValue }]);
    if (!applyResp.applied) {
      throw new Error(`Write failed: ${applyResp.reason || "unknown reason"}`);
    }
    const verified = Array.isArray(applyResp.verified) ? applyResp.verified : [];
    if (!(verified.length > 0 && verified.every(Boolean))) {
      throw new Error(
        "The edit was dispatched but readback verification failed — the live document may not reflect " +
          "the requested change. Check Overleaf directly before retrying.",
      );
    }

    // Step 5: release the claim and refresh the displayed outline so the
    // row reflects the newly-committed state.
    await fetch(`${ENGINE_URL}/release`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: currentProjectId, holder_token: currentHolderToken, node_id: node.id }),
    });

    showEditResult(row, "Edit applied and verified. Refreshing outline…", "ok");
    await getOutline();
  } catch (err) {
    showEditResult(row, err.message, "error");
    saveBtn.disabled = false;
    input.disabled = false;
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
check();
