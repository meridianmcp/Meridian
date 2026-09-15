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

document.getElementById("refresh").addEventListener("click", check);
check();
