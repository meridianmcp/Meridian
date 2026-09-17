function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = cls || "";
}

async function check() {
  setStatus("Checking for an Overleaf editor on this tab…");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.includes("overleaf.com/project/")) {
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

const ENGINE_URL = "http://127.0.0.1:8471";

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

async function getOutline() {
  setOutline("Reading editor…");
  try {
    const text = await getEditorText();
    setOutline(`Sending ${text.length} chars to the local engine (${ENGINE_URL})…`);
    const res = await fetch(`${ENGINE_URL}/outline`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Engine returned ${res.status}: ${body}`);
    }
    const { nodes } = await res.json();
    const counts = {};
    for (const n of nodes) counts[n.kind] = (counts[n.kind] || 0) + 1;
    const summary = Object.entries(counts)
      .map(([kind, n]) => `${kind}: ${n}`)
      .join("\n");
    setOutline(`${nodes.length} nodes\n${summary}`, "ok");
  } catch (err) {
    setOutline(
      `${err.message}\n\n(Is the local engine server running? cd meridian-latex/engine && node src/server.js)`,
      "error",
    );
  }
}

document.getElementById("refresh").addEventListener("click", check);
document.getElementById("get-outline").addEventListener("click", getOutline);
check();
