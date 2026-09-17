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
    } catch (err) {
      showClaimResult(row, `Release request failed: ${err.message}`, "error");
      btn.disabled = false;
    }
  });
  row.appendChild(btn);
}

async function claimNodeById(nodeId, row) {
  try {
    const res = await fetch(`${ENGINE_URL}/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: currentProjectId, node_id: nodeId, holder_token: currentHolderToken }),
    });
    const data = await res.json();
    if (!data.claimed) {
      showClaimResult(row, `Not claimed: ${data.reason || "unknown reason"}`, "error");
      return;
    }
    // Point 3: identity_confidence: "low" renders a visible inline warning,
    // never a blocked action -- the claim above already succeeded.
    if (data.identity_confidence === "low") {
      showClaimResult(row, `Claimed. Warning: low identity confidence — ${data.identity_confidence_reason}`, "warning");
    } else {
      showClaimResult(row, "Claimed.", "ok");
    }
    addReleaseButton(row, nodeId);
  } catch (err) {
    showClaimResult(row, `Claim request failed: ${err.message}`, "error");
  }
}

/**
 * Renders one row per structural node with a "Claim to edit" button.
 *
 * Deliberately NOT rendered here: any text-editing surface for a claimed
 * node. write-back-spec.md section 4 covers identity + coordination only --
 * the actual paragraph/section text round-trip (read a node's source text,
 * let the user edit it, write it back into Overleaf's live CodeMirror editor)
 * is real, separate follow-on work, out of scope for this pass (see the
 * spec's section 5). A claimed node here just shows its claim state and a
 * Release button.
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
    claimBtn.addEventListener("click", () => claimNodeById(node.id, row));
    row.appendChild(claimBtn);

    container.appendChild(row);

    const existing = existingClaims[node.id];
    if (existing && existing.holder_token === currentHolderToken) {
      // Reopening the popup on a project where this same holder already
      // holds a still-live claim (within CLAIM_TTL_MINUTES) -- reflect that
      // instead of showing "Claim to edit" as if nothing were claimed.
      showClaimResult(row, "Already claimed by this session.", "ok");
      addReleaseButton(row, node.id);
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
check();
