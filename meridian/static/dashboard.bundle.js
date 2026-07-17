"use strict";
(() => {
  // meridian/static/dashboard-core.ts
  var _HEALTH_STALE_MS = 45e3;
  var _lastApiOkAt = 0;
  var _lastApiFailed = false;
  function _setHealthDot(color, title) {
    const dot = document.getElementById("connection-health-dot");
    if (!dot) return;
    dot.style.background = color;
    dot.title = title;
  }
  function _refreshHealthDot() {
    if (_lastApiFailed) {
      _setHealthDot("#ef4444", "Disconnected \u2014 last request failed");
      return;
    }
    if (_lastApiOkAt === 0) {
      _setHealthDot("#6b7280", "Connecting\u2026");
      return;
    }
    const age = Date.now() - _lastApiOkAt;
    if (age > _HEALTH_STALE_MS) {
      _setHealthDot("#f59e0b", "Idle / stale \u2014 no recent server response");
    } else {
      _setHealthDot("#22c55e", "Connected");
    }
  }
  window._refreshHealthDot = _refreshHealthDot;
  function _markApiOk() {
    _lastApiOkAt = Date.now();
    _lastApiFailed = false;
    _refreshHealthDot();
  }
  function _markApiFail() {
    _lastApiFailed = true;
    _refreshHealthDot();
  }
  if (typeof window !== "undefined" && !window._healthDotTimer) {
    window._healthDotTimer = setInterval(_refreshHealthDot, 5e3);
  }
  async function api2(path, opts = {}) {
    const state2 = window.state || {};
    const headers = { "Content-Type": "application/json" };
    if (state2.activeWorkspaceTenantId) {
      headers["X-Workspace-Tenant-Id"] = state2.activeWorkspaceTenantId;
    }
    let r3;
    try {
      r3 = await fetch(path, { headers, ...opts });
    } catch (netErr) {
      _markApiFail();
      throw netErr;
    }
    if (!r3.ok) {
      if (r3.status === 403 && (typeof isDemoMode === "function" ? isDemoMode() : false)) {
        if (typeof showDemoReadonlyToast === "function") showDemoReadonlyToast();
        throw new Error("demo_readonly");
      }
      if (r3.status >= 500) _markApiFail();
      else _markApiOk();
      const text = await r3.text();
      const err = new Error(`${r3.status}: ${text}`);
      err.status = r3.status;
      err.endpoint = path;
      err.responseText = text;
      throw err;
    }
    _markApiOk();
    return r3.status === 204 ? null : r3.json();
  }
  window.api = api2;
  var _staleProjectsHandled = /* @__PURE__ */ new Set();
  window._staleProjectsHandled = _staleProjectsHandled;
  async function projectApi2(projectId, path, opts = {}) {
    const state2 = window.state || {};
    try {
      const data = await api2(path, opts);
      if (typeof clearProjectLoadError === "function") clearProjectLoadError(projectId, path);
      return data;
    } catch (e3) {
      if (e3 && e3.status === 404 && /project not found/i.test(e3.responseText || "") && !(state2.projects || []).some((p3) => p3.id === projectId) && !_staleProjectsHandled.has(projectId)) {
        _staleProjectsHandled.add(projectId);
        try {
          if (typeof closeTab === "function") closeTab(projectId);
        } catch (_2) {
        }
        try {
          if (typeof _checkAccountSwitch === "function") _checkAccountSwitch();
        } catch (_2) {
        }
        throw e3;
      }
      if (typeof recordProjectLoadError === "function") recordProjectLoadError(projectId, path, e3);
      throw e3;
    }
  }
  window.projectApi = projectApi2;
  try {
    Object.assign(window, { api: api2, projectApi: projectApi2, _staleProjectsHandled });
  } catch (e3) {
  }

  // meridian/static/dashboard-utils.ts
  var _PLAN_LABELS2 = {
    solo: "Standard",
    free: "Free Trial",
    standard: "Standard",
    pro: "Pro",
    trial: "Trial",
    admin: "Admin"
  };
  var QUEUE_DONE_PAGE_SIZE2 = 10;
  var SESSION_LIVE_WINDOW_MS = 10 * 60 * 1e3;
  var DEFAULT_MAX_PINNED_DECISIONS2 = 20;
  var DEFAULT_CONTEXT_THRESHOLD2 = 40;
  var DEFAULT_MAX_TURNS2 = 200;
  function sessionRecencyKey(session) {
    if (!session) return "";
    return String(session.last_seen || session.created_at || "");
  }
  function sortSessionsMostRecentFirst2(sessions) {
    return (sessions ? sessions.slice() : []).sort(
      (a3, b2) => sessionRecencyKey(b2).localeCompare(sessionRecencyKey(a3))
    );
  }
  function getPanelState2(projectId) {
    window.state.panels[projectId] = window.state.panels[projectId] || {};
    return window.state.panels[projectId];
  }
  var _toastTimer;
  function toast2(msg, isError = false) {
    const el2 = document.getElementById("toast");
    if (!el2) return;
    el2.textContent = msg;
    el2.classList.toggle("error", isError);
    el2.classList.add("show");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el2.classList.remove("show"), 2600);
  }
  function escapeHtml2(s3) {
    const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return String(s3).replace(/[&<>"']/g, (c3) => map[c3] ?? c3);
  }
  function formatRelativeTime2(ts) {
    if (!ts) return "";
    const iso = ts.includes("T") ? ts : ts.replace(" ", "T") + "Z";
    const then = new Date(iso);
    const seconds = Math.max(0, Math.floor((Date.now() - then.getTime()) / 1e3));
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }
  function sessionAgeMs2(session) {
    const raw = session && session.last_seen ? String(session.last_seen) : "";
    if (!raw) return Number.POSITIVE_INFINITY;
    const iso = raw.includes("T") ? raw : raw.replace(" ", "T") + "Z";
    const parsed = new Date(iso).getTime();
    return Number.isFinite(parsed) ? Date.now() - parsed : Number.POSITIVE_INFINITY;
  }
  function isLiveSession2(session, ageMs) {
    const age = ageMs == null ? sessionAgeMs2(session) : ageMs;
    return !!session && session.status === "active" && age >= 0 && age <= SESSION_LIVE_WINDOW_MS;
  }
  var _HUMAN_COLORS = ["#6c8fff", "#a78bfa", "#22d3ee", "#4ade80", "#fbbf24", "#f87171", "#fb923c", "#e879f9"];
  function _colorForHuman2(humanId) {
    let h3 = 0;
    const id = humanId || "";
    for (let i3 = 0; i3 < id.length; i3++) h3 = (h3 << 5) - h3 + id.charCodeAt(i3) | 0;
    return _HUMAN_COLORS[Math.abs(h3) % _HUMAN_COLORS.length];
  }
  function suggestedFsRoots2(execCfg, currentRoots) {
    const have = new Set(
      (Array.isArray(currentRoots) ? currentRoots : []).map((r3) => String(r3 ?? "").trim()).filter(Boolean)
    );
    const out = [];
    const seen = /* @__PURE__ */ new Set();
    const add = (raw) => {
      const v3 = String(raw ?? "").trim();
      if (!v3 || have.has(v3) || seen.has(v3)) return;
      seen.add(v3);
      out.push(v3);
    };
    const cfg = execCfg && typeof execCfg === "object" ? execCfg : {};
    if (Array.isArray(cfg.repo_paths)) {
      for (const p3 of cfg.repo_paths) add(p3 && typeof p3 === "object" ? p3.cwd : p3);
    }
    add(cfg.repo_path);
    return out;
  }
  try {
    Object.assign(window, {
      getPanelState: getPanelState2,
      toast: toast2,
      escapeHtml: escapeHtml2,
      formatRelativeTime: formatRelativeTime2,
      sessionAgeMs: sessionAgeMs2,
      isLiveSession: isLiveSession2,
      sessionRecencyKey,
      sortSessionsMostRecentFirst: sortSessionsMostRecentFirst2,
      _colorForHuman: _colorForHuman2,
      _PLAN_LABELS: _PLAN_LABELS2,
      QUEUE_DONE_PAGE_SIZE: QUEUE_DONE_PAGE_SIZE2,
      SESSION_LIVE_WINDOW_MS,
      _HUMAN_COLORS,
      DEFAULT_MAX_PINNED_DECISIONS: DEFAULT_MAX_PINNED_DECISIONS2,
      DEFAULT_CONTEXT_THRESHOLD: DEFAULT_CONTEXT_THRESHOLD2,
      DEFAULT_MAX_TURNS: DEFAULT_MAX_TURNS2,
      suggestedFsRoots: suggestedFsRoots2
    });
  } catch (e3) {
  }

  // meridian/static/dashboard-demo.ts
  function isDemoMode2() {
    return !!window.state?.serverConfig?.demo_mode || window.location.pathname.startsWith("/demo");
  }
  function isHostedMode2() {
    return !!window.MERIDIAN_HOSTED;
  }
  function isHostedAdmin2() {
    return isHostedMode2() && !!window.MERIDIAN_IS_ADMIN;
  }
  function ensureTourButton2() {
    const footer = document.querySelector(".sidebar-footer");
    if (!footer || document.getElementById("tour-launch-btn")) return;
    const btn = document.createElement("button");
    btn.id = "tour-launch-btn";
    btn.type = "button";
    btn.textContent = "\u{1F9ED} Take the tour";
    btn.title = "Replay the guided dashboard walkthrough";
    btn.style = "display:block;width:100%;margin-top:8px;padding:6px 10px;font-size:11px;color:var(--text);font-family:var(--font-mono);text-align:center;background:var(--surface-1);border:1px solid var(--border);border-radius:5px;cursor:pointer";
    btn.onmouseenter = () => {
      btn.style.borderColor = "var(--accent)";
      btn.style.color = "var(--accent)";
    };
    btn.onmouseleave = () => {
      btn.style.borderColor = "var(--border)";
      btn.style.color = "var(--text)";
    };
    btn.onclick = () => {
      try {
        startDemoTour(0);
      } catch (e3) {
      }
    };
    footer.appendChild(btn);
  }
  function ensureFeedbackButton2() {
    if (!isHostedMode2()) return;
    const footer = document.querySelector(".sidebar-footer");
    if (!footer || document.getElementById("feedback-launch-btn")) return;
    const btn = document.createElement("button");
    btn.id = "feedback-launch-btn";
    btn.type = "button";
    btn.setAttribute("data-demo-hide", "");
    btn.textContent = "\u{1F4AC} Send feedback";
    btn.title = "Report a bug or request a feature";
    btn.style = "display:block;width:100%;margin-top:6px;padding:5px 10px;font-size:10px;color:var(--muted);font-family:var(--font-mono);text-align:center;background:transparent;border:1px solid var(--border);border-radius:5px;cursor:pointer";
    btn.onmouseenter = () => {
      btn.style.borderColor = "var(--accent)";
      btn.style.color = "var(--accent)";
    };
    btn.onmouseleave = () => {
      btn.style.borderColor = "var(--border)";
      btn.style.color = "var(--muted)";
    };
    btn.onclick = () => {
      try {
        showFeedbackModal();
      } catch (e3) {
      }
    };
    footer.appendChild(btn);
  }
  function showFeedbackModal() {
    if (document.getElementById("feedback-modal")) return;
    const overlay = document.createElement("div");
    overlay.id = "feedback-modal";
    overlay.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center";
    const box = document.createElement("div");
    box.style.cssText = "background:var(--surface-0);border:1px solid var(--border);border-radius:8px;padding:24px 28px;width:440px;max-width:94vw;display:flex;flex-direction:column;gap:12px";
    box.innerHTML = `

    <div style="font-weight:700;font-size:14px">Send feedback</div>

    <label style="font-size:11px;color:var(--muted)">Type

      <select id="feedback-type" style="display:block;width:100%;margin-top:4px;font-size:12px;font-family:var(--font-mono);background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:6px 8px">

        <option value="bug">Bug</option>

        <option value="feature">Feature request</option>

        <option value="other">Other</option>

      </select>

    </label>

    <label style="font-size:11px;color:var(--muted)">Message

      <textarea id="feedback-message" rows="4" placeholder="What's on your mind?" style="display:block;width:100%;margin-top:4px;box-sizing:border-box;font-size:12px;font-family:inherit;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:7px 10px;resize:vertical"></textarea>

    </label>

    <label style="font-size:11px;color:var(--muted)">Email

      <input id="feedback-email" type="email" placeholder="you@example.com" style="display:block;width:100%;margin-top:4px;box-sizing:border-box;font-size:12px;font-family:var(--font-mono);background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:6px 10px">

    </label>

    <div id="feedback-status" style="font-size:11px;min-height:16px;color:var(--muted)"></div>

    <div style="display:flex;gap:8px;justify-content:flex-end">

      <button id="feedback-cancel" class="secondary" style="font-size:12px">Cancel</button>

      <button id="feedback-send" style="font-size:12px">Send</button>

    </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const statusEl = box.querySelector("#feedback-status");
    box.querySelector("#feedback-cancel").onclick = () => overlay.remove();
    overlay.onclick = (e3) => {
      if (e3.target === overlay) overlay.remove();
    };
    box.querySelector("#feedback-send").onclick = async () => {
      const type = box.querySelector("#feedback-type").value;
      const message = box.querySelector("#feedback-message").value.trim();
      const email = box.querySelector("#feedback-email").value.trim();
      if (!message) {
        statusEl.textContent = "Enter a message.";
        statusEl.style.color = "var(--danger,#dc2626)";
        return;
      }
      if (!email || !email.includes("@")) {
        statusEl.textContent = "Enter a valid email.";
        statusEl.style.color = "var(--danger,#dc2626)";
        return;
      }
      statusEl.textContent = "Sending\u2026";
      statusEl.style.color = "var(--muted)";
      try {
        await api("/feedback", { method: "POST", body: JSON.stringify({ type, message, email }) });
        statusEl.textContent = "Thanks! Feedback sent.";
        statusEl.style.color = "#059669";
        setTimeout(() => overlay.remove(), 900);
      } catch (e3) {
        statusEl.textContent = e3.message || "Could not send \u2014 please try again.";
        statusEl.style.color = "var(--danger,#dc2626)";
      }
    };
    box.querySelector("#feedback-message").focus();
  }
  function hideDemoAdminControls2() {
    const selectors = [
      "#restart-server-btn",
      "#stop-server-btn",
      "#banner-restart-btn",
      "#git-check-btn",
      "#update-banner",
      "#delete-account-section",
      "[data-demo-hide]",
      // Settings: hide write controls entirely in demo
      '[id^="ntfy-url-"]',
      '[id^="ntfy-save-"]',
      '[id^="ntfy-test-"]',
      '[id^="ntfy-status-"]',
      '[id^="mcp-gen-token-"]',
      '[id^="invite-email-"]',
      '[id^="invite-role-"]',
      '[id^="invite-btn-"]',
      '[id^="github-repo-"]',
      '[id^="github-branch-"]',
      '[id^="github-connect-btn-"]',
      '[id^="github-save-btn-"]',
      '[id^="github-disconnect-btn-"]',
      '[id^="github-test-btn-"]',
      // Files tab: hide Edit subtab, show Preview only
      '[id^="file-mode-edit-"]',
      // Workspace settings: hide write controls entirely in demo (v3.4)
      "#ws-settings-save",
      "#ws-dec-title",
      "#ws-dec-body",
      "#ws-dec-add",
      "#ws-note-title",
      "#ws-note-body",
      "#ws-note-add",
      // Workspace DB connect UI — write action, hide in demo
      "#connect-db-link",
      "#connect-db-save",
      // Easy-setup + goal template write controls (settings tab)
      '[id^="exec-ez-save-"]',
      '[id^="codex-save-goal-"]',
      '[id^="codex-regen-goal-"]'
    ];
    selectors.forEach((sel) => {
      document.querySelectorAll(sel).forEach((el2) => {
        el2.style.display = "none";
      });
    });
    const writeBtnSelectors = [
      "button[data-write]",
      ".btn-write",
      "#add-sprint-item-btn",
      '[id^="save-goal-"]',
      '[id^="save-sprint-"]',
      '[id^="delete-project-"]',
      '[id^="rename-project-"]',
      '[id^="add-sprint-"]',
      '[id^="mark-done-"]',
      '[id^="claim-task-"]',
      // HITL markdown section-update approval (writes + commits a doc) — demo-gated
      ".hitl-approve-btn",
      ".hitl-reject-btn"
    ];
    writeBtnSelectors.forEach((sel) => {
      document.querySelectorAll(sel).forEach((btn) => {
        if (btn.dataset.demoHintApplied) return;
        btn.dataset.demoHintApplied = "1";
        btn.title = "Sign in to use";
        btn.style.opacity = String(parseFloat(btn.style.opacity || "1") * 0.55);
        btn.style.cursor = "not-allowed";
        const orig = btn.onclick;
        btn.onclick = (e3) => {
          e3.preventDefault();
          e3.stopPropagation();
          showDemoReadonlyToast2();
        };
      });
    });
  }
  function showDemoReadonlyToast2() {
    const el2 = document.getElementById("toast");
    if (!el2) return;
    el2.innerHTML = 'Read-only demo \u2014 <a href="/auth/login" style="color:#fff;font-weight:600;text-decoration:underline">sign in for full access \u2192</a>';
    el2.classList.add("error", "show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el2.classList.remove("show"), 3200);
  }
  function showDemoOnboardingOverlay2() {
    if (document.getElementById("demo-onboarding-overlay")) return;
    if (_demoTourDone()) return;
    const resuming = _demoTourSavedStep() > 0;
    const ctaLabel = resuming ? "Resume tour \u2192" : "Got it \u2014 show me around \u2192";
    const overlay = document.createElement("div");
    overlay.id = "demo-onboarding-overlay";
    overlay.style = "position:fixed;inset:0;z-index:20000;background:rgba(0,0,0,0.72);display:flex;align-items:center;justify-content:center;padding:16px";
    overlay.innerHTML = `<div style="background:#1e2029;border:1px solid #7c3aed66;border-radius:14px;padding:32px 36px;max-width:480px;width:100%;box-shadow:0 12px 48px rgba(0,0,0,0.7);position:relative;font-family:inherit">

  <button onclick="document.getElementById('demo-onboarding-overlay').remove()" style="position:absolute;top:12px;right:14px;background:none;border:none;color:#8b8fa8;font-size:22px;cursor:pointer;line-height:1;padding:4px" title="Dismiss">\xD7</button>

  <h3 style="color:#e8eaf0;margin:0 0 18px;font-size:1.35rem;font-weight:700">Welcome to the Meridian demo</h3>

  <ol style="color:#c4c6d4;font-size:1.02rem;line-height:1.85;padding-left:1.3em;margin:0 0 24px">

    <li>This is a live demo coordinating a real multi-session build. It's read-only.</li>

    <li>Click any session on the left to explore.</li>

    <li>Write actions are disabled \u2014 <a href="/auth/login" style="color:#6c8fff;text-decoration:underline">sign in to create your own project</a>.</li>

  </ol>

  <div style="display:flex;gap:8px">

    <button onclick="document.getElementById('demo-onboarding-overlay').remove()" style="background:#2a2d35;border:none;border-radius:7px;color:#8b8fa8;padding:10px 18px;cursor:pointer;font-size:.98rem;font-family:inherit;flex:0 0 auto">Skip</button>

    <button onclick="document.getElementById('demo-onboarding-overlay').remove();resumeDemoTour()" style="background:#7c3aed;border:none;border-radius:7px;color:#fff;padding:10px 24px;cursor:pointer;font-size:1.02rem;font-family:inherit;flex:1">${ctaLabel}</button>

  </div>

</div>`;
    overlay.addEventListener("click", (e3) => {
      if (e3.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
  }
  try {
    Object.assign(window, { isDemoMode: isDemoMode2, isHostedMode: isHostedMode2, isHostedAdmin: isHostedAdmin2, ensureTourButton: ensureTourButton2, ensureFeedbackButton: ensureFeedbackButton2, showFeedbackModal, hideDemoAdminControls: hideDemoAdminControls2, showDemoReadonlyToast: showDemoReadonlyToast2, showDemoOnboardingOverlay: showDemoOnboardingOverlay2 });
  } catch (e3) {
  }

  // meridian/static/dashboard-timeline.ts
  function renderTimeline2(projectId, data) {
    const wrap = document.getElementById(`timeline-wrap-${projectId}`);
    if (!wrap) return;
    const p3 = window.state?.panels[projectId];
    const sessionFilter = p3 && p3.timelineSessionFilter;
    const rawTasks = data && data.tasks || [];
    const tasks = sessionFilter ? rawTasks.filter((t3) => t3.session_id === sessionFilter) : rawTasks;
    const goal_events = sessionFilter ? [] : data && data.goal_events || [];
    data = { ...data || {}, tasks, goal_events };
    if (!tasks.length && !goal_events.length) {
      wrap.innerHTML = `<div class="timeline-empty">no activity yet \u2014 log a task to see it here</div>`;
      return;
    }
    if (p3 && p3._echart) {
      try {
        p3._echart.dispose();
      } catch (_2) {
      }
      p3._echart = null;
    }
    if (p3 && p3._heatchart) {
      try {
        p3._heatchart.dispose();
      } catch (_2) {
      }
      p3._heatchart = null;
    }
    if (typeof echarts === "undefined") {
      _renderTimelineLog(projectId, data);
      return;
    }
    const savedTlView = (() => {
      try {
        return localStorage.getItem("meridian_tl_view_" + projectId) || "heatmap";
      } catch (_2) {
        return "heatmap";
      }
    })();
    wrap.innerHTML = `

    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">

      <div class="tl-subtabs" style="margin-bottom:0">

        <button class="tl-subtab${savedTlView === "heatmap" ? " active" : ""}" data-sub="heatmap">Heatmap</button>

        <button class="tl-subtab${savedTlView === "detail" ? " active" : ""}" data-sub="detail">Detail</button>

      </div>

      <select id="tl-view-select-${projectId}" style="padding:3px 6px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:10px;font-family:var(--font-mono);cursor:pointer" title="Timeline grouping mode">

        <option value="heatmap"${savedTlView === "heatmap" ? " selected" : ""}>By Heatmap</option>

        <option value="detail"${savedTlView === "detail" ? " selected" : ""}>By Session</option>

        <option value="tasks"${savedTlView === "tasks" ? " selected" : ""}>Tasks</option>

        <option value="sprints"${savedTlView === "sprints" ? " selected" : ""}>Sprints only</option>

        <option value="by-sprint"${savedTlView === "by-sprint" ? " selected" : ""}>By Sprint</option>

      </select>
      ${sessionFilter ? `<span style="font-size:10px;color:var(--accent);border:1px solid var(--accent)55;border-radius:3px;padding:2px 6px">session ${escapeHtml(sessionFilter.slice(0, 8))}</span><button class="secondary" id="tl-clear-session-${projectId}" style="padding:2px 8px;font-size:10px">Clear</button>` : ""}

    </div>

    <div class="tl-pane${savedTlView === "heatmap" || savedTlView === "detail" ? " active" : ""}" id="tl-pane-heatmap-${projectId}"${savedTlView !== "heatmap" ? ' style="display:none"' : ""}></div>

    <div class="tl-pane" id="tl-pane-detail-${projectId}" style="display:${savedTlView === "detail" ? "" : "none"}"></div>

    <div id="tl-pane-tasks-${projectId}" style="display:${savedTlView === "tasks" ? "" : "none"}"></div>

    <div id="tl-pane-sprints-${projectId}" style="display:${savedTlView === "sprints" || savedTlView === "by-sprint" ? "" : "none"}"></div>`;
    const heatPane = document.getElementById(`tl-pane-heatmap-${projectId}`);
    const detailPane = document.getElementById(`tl-pane-detail-${projectId}`);
    const tasksPane = document.getElementById(`tl-pane-tasks-${projectId}`);
    const sprintsPane = document.getElementById(`tl-pane-sprints-${projectId}`);
    const _renderTasksFlat = () => {
      const { tasks: tasks2 = [] } = data || {};
      if (!tasks2.length) {
        tasksPane.innerHTML = `<div class="timeline-empty">no tasks logged yet</div>`;
        return;
      }
      tasksPane.innerHTML = tasks2.map((t3) => {
        const ts = (t3.created_at || "").slice(0, 16).replace("T", " ");
        const who = t3.human_id || t3.session_name || "";
        const status = (t3.status || "").toUpperCase();
        return `<div style="padding:5px 8px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:baseline">

        <span style="font-size:9px;color:var(--muted);white-space:nowrap;min-width:100px">${escapeHtml(ts)}</span>

        ${who ? `<span style="font-size:9px;color:var(--accent);white-space:nowrap">${escapeHtml(who)}</span>` : ""}

        <span style="font-size:10px;color:var(--muted);white-space:nowrap">[${escapeHtml(status)}]</span>

        <span style="font-size:11px;color:var(--text);word-break:break-word">${escapeHtml(t3.description || "")}</span>

      </div>`;
      }).join("");
    };
    const _renderSprintsView = (groupBySprint) => {
      api(`/projects/${projectId}/sprint-items?status=done`).then((items) => {
        const done = (items || []).filter((it) => it.status === "done").sort((a3, b2) => String(b2.completed_at || b2.added_at || "").localeCompare(String(a3.completed_at || a3.added_at || "")));
        if (!done.length) {
          sprintsPane.innerHTML = `<div class="timeline-empty">no completed sprint items</div>`;
          return;
        }
        if (groupBySprint) {
          const groups = {};
          done.forEach((it) => {
            const key = it.version || it.item_group || "(unversioned)";
            (groups[key] = groups[key] || []).push(it);
          });
          sprintsPane.innerHTML = Object.entries(groups).map(
            ([grp, items2]) => `<div style="margin-bottom:12px">

            <div style="font-size:10px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;padding:4px 0;border-bottom:1px solid var(--border);margin-bottom:4px">${escapeHtml(grp)} (${items2.length})</div>

            ${items2.map((it) => `<div style="padding:3px 0;font-size:11px;color:var(--text);display:flex;gap:8px"><span style="font-size:9px;color:var(--muted);white-space:nowrap;min-width:70px">${escapeHtml((it.completed_at || it.added_at || "").slice(0, 10))}</span><span>${escapeHtml(it.title || "")}</span></div>`).join("")}

          </div>`
          ).join("");
        } else {
          sprintsPane.innerHTML = done.map(
            (it) => `<div style="padding:4px 0;font-size:11px;color:var(--text);display:flex;gap:8px;border-bottom:1px solid var(--border)33">

            <span style="font-size:9px;color:var(--muted);white-space:nowrap;min-width:70px">${escapeHtml((it.completed_at || it.added_at || "").slice(0, 10))}</span>

            ${it.version ? `<span style="font-size:9px;color:var(--accent)">${escapeHtml(it.version)}</span>` : ""}

            <span>${escapeHtml(it.title || "")}</span>

          </div>`
          ).join("");
        }
      }).catch((e3) => {
        sprintsPane.innerHTML = `<div class="timeline-empty">failed: ${escapeHtml(e3.message)}</div>`;
      });
    };
    _renderTimelineHeatmap(projectId, data, heatPane);
    _renderTimelineGantt(projectId, data, detailPane);
    if (savedTlView === "tasks") _renderTasksFlat();
    if (savedTlView === "sprints") _renderSprintsView(false);
    if (savedTlView === "by-sprint") _renderSprintsView(true);
    const viewSelect = document.getElementById(`tl-view-select-${projectId}`);
    const clearSessionBtn = document.getElementById(`tl-clear-session-${projectId}`);
    if (clearSessionBtn) clearSessionBtn.onclick = () => {
      if (p3) p3.timelineSessionFilter = null;
      loadTimeline(projectId);
    };
    if (viewSelect) {
      viewSelect.onchange = () => {
        const view = viewSelect.value;
        try {
          localStorage.setItem("meridian_tl_view_" + projectId, view);
        } catch (_2) {
        }
        wrap.querySelectorAll(".tl-subtab").forEach((b2) => b2.classList.toggle("active", b2.dataset.sub === view));
        heatPane.style.display = view === "heatmap" ? "" : "none";
        detailPane.style.display = view === "detail" ? "" : "none";
        tasksPane.style.display = view === "tasks" ? "" : "none";
        sprintsPane.style.display = view === "sprints" || view === "by-sprint" ? "" : "none";
        if (view === "heatmap" && p3 && p3._heatchart) {
          try {
            p3._heatchart.resize();
          } catch (_2) {
          }
        }
        if (view === "detail" && p3 && p3._echart) {
          try {
            p3._echart.resize();
          } catch (_2) {
          }
        }
        if (view === "tasks") _renderTasksFlat();
        if (view === "sprints") _renderSprintsView(false);
        if (view === "by-sprint") _renderSprintsView(true);
      };
    }
    wrap.querySelectorAll(".tl-subtab").forEach((btn) => {
      btn.onclick = () => {
        const sub = btn.dataset.sub;
        if (viewSelect) viewSelect.value = sub;
        try {
          localStorage.setItem("meridian_tl_view_" + projectId, sub);
        } catch (_2) {
        }
        wrap.querySelectorAll(".tl-subtab").forEach((b2) => b2.classList.toggle("active", b2 === btn));
        heatPane.style.display = sub === "heatmap" ? "" : "none";
        detailPane.style.display = sub === "detail" ? "" : "none";
        tasksPane.style.display = "none";
        sprintsPane.style.display = "none";
        if (sub === "heatmap" && p3 && p3._heatchart) {
          try {
            p3._heatchart.resize();
          } catch (_2) {
          }
        }
        if (sub === "detail" && p3 && p3._echart) {
          try {
            p3._echart.resize();
          } catch (_2) {
          }
        }
      };
    });
  }
  function _heatmapPieces(maxScale) {
    const colors = ["#bbf7d0", "#4ade80", "#16a34a", "#ca8a04", "#ea580c", "#dc2626"];
    const n2 = colors.length;
    const pieces = [];
    let lo = 1;
    for (let i3 = 0; i3 < n2; i3++) {
      if (i3 === n2 - 1) {
        pieces.push({ min: lo, color: colors[i3], label: `${lo}+` });
        break;
      }
      const hi = Math.max(lo, Math.round(maxScale * (i3 + 1) / n2));
      pieces.push({ min: lo, max: hi, color: colors[i3], label: lo === hi ? `${lo}` : `${lo}\u2013${hi}` });
      lo = hi + 1;
    }
    return pieces;
  }
  function _heatmapMaxFor(projectId) {
    const raw = parseInt(localStorage.getItem(`meridian_heatmap_max_${projectId}`) || "", 10);
    if (!Number.isFinite(raw)) return 25;
    return Math.min(100, Math.max(10, raw));
  }
  function _renderTimelineHeatmap(projectId, data, paneEl) {
    if (!paneEl) return;
    const daily = data && data.daily_counts || [];
    if (!daily.length) {
      paneEl.innerHTML = `<div class="timeline-empty">no activity yet \u2014 log a task to see it here</div>`;
      return;
    }
    const cssVar = (name, fallback) => {
      const v3 = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v3 || fallback;
    };
    const emptyColor = cssVar("--surface-2", "#1a2740");
    const borderCol = cssVar("--border", "#232830");
    const textPrimary = cssVar("--text", "#d8dde6");
    const textMuted = cssVar("--muted", "#9ba5b5");
    let allPeople = data.people && data.people.slice() || [];
    let allClients = data.clients && data.clients.slice() || [];
    if (!allPeople.length || !allClients.length) {
      const ps = /* @__PURE__ */ new Set(), cs = /* @__PURE__ */ new Set();
      daily.forEach((d3) => (d3.sessions || []).forEach((s3) => {
        ps.add(s3.person || s3.human || "(unknown)");
        cs.add(s3.client || "(none)");
      }));
      if (!allPeople.length) allPeople = [...ps].sort();
      if (!allClients.length) allClients = [...cs].sort();
    }
    const dates = daily.map((d3) => d3.date).sort();
    const rangeStart = dates[0];
    const rangeEnd = dates[dates.length - 1];
    const selKey = (k3) => `meridian_tl_${k3}_${projectId}`;
    const loadSel = (k3, all) => {
      try {
        const raw = JSON.parse(localStorage.getItem(selKey(k3)) || "null");
        if (Array.isArray(raw)) {
          const keep = raw.filter((x2) => all.includes(x2));
          if (keep.length) return new Set(keep);
        }
      } catch (_2) {
      }
      return new Set(all);
    };
    let selPeople = loadSel("people", allPeople);
    let selClients = loadSel("clients", allClients);
    const clientOK = (s3) => selClients.size === allClients.length || selClients.has(s3.client || "(none)");
    const CELL = 16;
    const CAL_TOP = 28;
    const CAL_H = CELL * 7 + 34;
    const ROW_GAP = 18;
    const rowH = CAL_H + ROW_GAP;
    let detailByPersonDay = {};
    function computeView() {
      let rows = allPeople.filter((p3) => selPeople.has(p3));
      if (!rows.length) rows = allPeople.slice();
      const multi = rows.length > 1;
      const rowKeys = multi ? rows : ["__all__"];
      detailByPersonDay = {};
      const countByKeyDay = {};
      daily.forEach((d3) => {
        (d3.sessions || []).forEach((s3) => {
          if (!clientOK(s3)) return;
          const person = s3.person || s3.human || "(unknown)";
          if (!selPeople.has(person)) return;
          const key = multi ? person : "__all__";
          countByKeyDay[`${key}|${d3.date}`] = (countByKeyDay[`${key}|${d3.date}`] || 0) + s3.count;
          (detailByPersonDay[`${key}|${d3.date}`] = detailByPersonDay[`${key}|${d3.date}`] || []).push(s3);
        });
      });
      const calendars2 = [], series2 = [], titles2 = [];
      rowKeys.forEach((rk, i3) => {
        const top = CAL_TOP + i3 * rowH;
        calendars2.push({
          top,
          left: multi ? 120 : 40,
          right: 12,
          cellSize: [CELL, CELL],
          range: rangeStart === rangeEnd ? rangeStart : [rangeStart, rangeEnd],
          splitLine: { show: true, lineStyle: { color: borderCol, type: "dashed", width: 1 } },
          itemStyle: { color: emptyColor, borderColor: "#0d1b2e", borderWidth: 1 },
          yearLabel: { show: false },
          monthLabel: { color: textPrimary, fontFamily: "IBM Plex Mono", fontSize: 13, fontWeight: "bold" },
          dayLabel: { color: textMuted, fontFamily: "IBM Plex Mono", fontSize: 10, firstDay: 1 }
        });
        if (multi) {
          titles2.push({
            text: rk.length > 16 ? rk.slice(0, 15) + "\u2026" : rk,
            left: 6,
            top: top + CAL_H / 2 - 6,
            textStyle: { color: _colorForHuman(rk === "(unknown)" ? "" : rk), fontFamily: "IBM Plex Mono", fontSize: 10, fontWeight: "bold" }
          });
        }
        const pts = daily.map((d3) => {
          const count = countByKeyDay[`${rk}|${d3.date}`] || 0;
          const dayDetail = detailByPersonDay[`${rk}|${d3.date}`] || [];
          const scount = new Set(dayDetail.map((s3) => s3.session_id)).size;
          return { value: [d3.date, count], scount, person: rk };
        }).filter((pt) => pt.value[1] > 0);
        series2.push({
          type: "heatmap",
          coordinateSystem: "calendar",
          calendarIndex: i3,
          data: pts,
          // Stamp the day-of-month (dark) onto cells that had activity, so the
          // calendar reads as a calendar — empty days carry no label since pts
          // only includes count > 0.
          label: {
            show: true,
            color: "#0b1220",
            fontFamily: "IBM Plex Mono",
            fontSize: 10,
            fontWeight: "bold",
            formatter: (p3) => {
              const v3 = p3.value && p3.value[0];
              if (!v3) return "";
              const dd = parseInt(String(v3).slice(8, 10), 10);
              return Number.isFinite(dd) ? String(dd) : "";
            }
          }
        });
      });
      const totalH2 = CAL_TOP + rowKeys.length * rowH + 28;
      return { calendars: calendars2, series: series2, titles: titles2, totalH: totalH2 };
    }
    let { calendars, series, titles, totalH } = computeView();
    paneEl.innerHTML = "";
    let scaleMax = _heatmapMaxFor(projectId);
    let applyFilters = () => {
    };
    if (allPeople.length > 1 || allClients.length > 1) {
      const bar = document.createElement("div");
      bar.className = "tl-filter-bar";
      bar.style.cssText = "display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:0 4px 8px;font-size:10px;font-family:IBM Plex Mono,monospace;color:var(--muted)";
      paneEl.appendChild(bar);
      const toggle = (sel, all, key, x2) => {
        if (x2 === "__all__") sel = new Set(all);
        else {
          sel.has(x2) ? sel.delete(x2) : sel.add(x2);
          if (!sel.size) sel = new Set(all);
        }
        localStorage.setItem(selKey(key), JSON.stringify([...sel]));
        return sel;
      };
      const closeAllPanels = () => bar.querySelectorAll("[data-tl-panel]").forEach((p3) => {
        p3.style.display = "none";
      });
      const mkDropdown = (labelText, all, getSel, setSel, key) => {
        const wrap = document.createElement("div");
        wrap.style.cssText = "position:relative;display:inline-block";
        const btn = document.createElement("button");
        btn.style.cssText = "display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:6px;cursor:pointer;font-size:10px;font-family:inherit;border:1px solid var(--border);background:var(--surface-2);color:var(--text)";
        const panel = document.createElement("div");
        panel.setAttribute("data-tl-panel", "1");
        panel.style.cssText = "position:absolute;z-index:30;top:calc(100% + 4px);left:0;min-width:170px;max-height:240px;overflow-y:auto;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:4px;box-shadow:0 6px 20px rgba(0,0,0,0.4);display:none";
        const sync = () => {
          const sel = getSel();
          const total = all.length;
          const allSel = sel.size === total;
          btn.textContent = "";
          const cap = document.createElement("span");
          cap.textContent = `${labelText}: ${allSel ? "All" : sel.size + "/" + total}`;
          const caret = document.createElement("span");
          caret.textContent = "\u25BE";
          caret.style.cssText = "opacity:0.6";
          btn.appendChild(cap);
          btn.appendChild(caret);
          panel.innerHTML = "";
          const mkRow = (text, value, active) => {
            const row = document.createElement("div");
            row.textContent = text;
            row.title = text;
            row.style.cssText = `padding:4px 8px;border-radius:4px;cursor:pointer;white-space:nowrap;font-size:10px;margin-bottom:1px;background:${active ? "#2563eb" : "transparent"};color:${active ? "#fff" : "var(--text)"}`;
            row.onmouseenter = () => {
              if (!active) row.style.background = "var(--surface-2)";
            };
            row.onmouseleave = () => {
              if (!active) row.style.background = "transparent";
            };
            row.onclick = (e3) => {
              e3.stopPropagation();
              setSel(toggle(getSel(), all, key, value));
              sync();
              applyFilters();
            };
            return row;
          };
          panel.appendChild(mkRow("All", "__all__", allSel));
          all.forEach((x2) => panel.appendChild(mkRow(x2.length > 30 ? x2.slice(0, 29) + "\u2026" : x2, x2, sel.has(x2))));
        };
        btn.onclick = (e3) => {
          e3.stopPropagation();
          const willOpen = panel.style.display === "none";
          closeAllPanels();
          panel.style.display = willOpen ? "block" : "none";
        };
        sync();
        wrap.appendChild(btn);
        wrap.appendChild(panel);
        return wrap;
      };
      if (allPeople.length > 1) {
        bar.appendChild(mkDropdown("People", allPeople, () => selPeople, (s3) => {
          selPeople = s3;
        }, "people"));
      }
      if (allClients.length > 1) {
        bar.appendChild(mkDropdown("Client", allClients, () => selClients, (s3) => {
          selClients = s3;
        }, "clients"));
      }
      paneEl.addEventListener("click", closeAllPanels);
    }
    const ctrl = document.createElement("div");
    ctrl.style.cssText = "display:flex;align-items:center;gap:8px;justify-content:flex-end;padding:0 4px 6px;font-size:11px;color:var(--muted);font-family:IBM Plex Mono,monospace";
    const ctrlLabel = document.createElement("label");
    ctrlLabel.textContent = "Scale max";
    ctrlLabel.style.cssText = "cursor:default";
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "10";
    slider.max = "100";
    slider.step = "5";
    slider.value = String(scaleMax);
    slider.style.cssText = "width:120px;accent-color:#16a34a;cursor:pointer";
    const valOut = document.createElement("span");
    valOut.textContent = String(scaleMax);
    valOut.style.cssText = "min-width:24px;text-align:right;color:var(--text)";
    ctrlLabel.setAttribute("for", `heatscale-${projectId}`);
    slider.id = `heatscale-${projectId}`;
    ctrl.appendChild(ctrlLabel);
    ctrl.appendChild(slider);
    ctrl.appendChild(valOut);
    paneEl.appendChild(ctrl);
    const container = document.createElement("div");
    container.style.cssText = `width:100%;height:${totalH}px;min-height:${totalH}px`;
    paneEl.appendChild(container);
    const detailBox = document.createElement("div");
    detailBox.className = "tl-heat-detail";
    detailBox.style.cssText = "padding:8px 4px 4px;font-size:11px;color:var(--muted)";
    detailBox.textContent = "Click a day to see the sessions that contributed.";
    paneEl.appendChild(detailBox);
    const chart = echarts.init(container, null, { renderer: "canvas" });
    chart.setOption({
      backgroundColor: "transparent",
      animation: false,
      title: titles,
      tooltip: {
        trigger: "item",
        backgroundColor: "#0d1b2e",
        borderColor: "#1e3a5f",
        textStyle: { color: "#c7d5ef", fontSize: 11, fontFamily: "IBM Plex Mono" },
        formatter: (params) => {
          const d3 = params.data;
          if (!d3 || !d3.value) return "";
          const date = d3.value[0], count = d3.value[1];
          return `<b>${escapeHtml(date)}</b> \u2014 ${count} task${count === 1 ? "" : "s"} across ${d3.scount} session${d3.scount === 1 ? "" : "s"}`;
        }
      },
      visualMap: {
        type: "piecewise",
        show: true,
        orient: "horizontal",
        left: "center",
        bottom: 0,
        itemWidth: 11,
        itemHeight: 11,
        textStyle: { color: "#8b9cba", fontSize: 9, fontFamily: "IBM Plex Mono" },
        pieces: _heatmapPieces(scaleMax)
      },
      calendar: calendars,
      series
    });
    const renderDetail2 = (person, date) => {
      const list = detailByPersonDay[`${person}|${date}`] || [];
      if (!list.length) {
        detailBox.innerHTML = `<span style="color:var(--muted)">${escapeHtml(date)} \u2014 no sessions</span>`;
        return;
      }
      const total = list.reduce((a3, s3) => a3 + s3.count, 0);
      const rows = list.map((s3) => {
        const cli = s3.client && s3.client !== "(none)" ? `<span class="tl-heat-sess-client">${escapeHtml(s3.client)}</span>` : "";
        return `<div class="tl-heat-sess"><span class="tl-heat-sess-name">${escapeHtml(s3.name || "(unknown)")}</span>` + cli + `<span class="tl-heat-sess-count">${s3.count} task${s3.count === 1 ? "" : "s"}</span></div>`;
      }).join("");
      detailBox.innerHTML = `<div class="tl-heat-detail-head">${escapeHtml(person)} \xB7 ${escapeHtml(date)} \xB7 ${total} task${total === 1 ? "" : "s"} \xB7 ${list.length} session${list.length === 1 ? "" : "s"}</div>${rows}`;
    };
    chart.on("click", (params) => {
      if (params.componentType !== "series" || !params.data || !params.data.value) return;
      renderDetail2(params.data.person, params.data.value[0]);
    });
    slider.addEventListener("input", () => {
      scaleMax = Math.min(100, Math.max(10, parseInt(slider.value, 10) || 25));
      valOut.textContent = String(scaleMax);
      localStorage.setItem(`meridian_heatmap_max_${projectId}`, String(scaleMax));
      chart.setOption({ visualMap: { pieces: _heatmapPieces(scaleMax) } });
    });
    const pnl = window.state?.panels[projectId];
    if (pnl) pnl._heatchart = chart;
    applyFilters = () => {
      ({ calendars, series, titles, totalH } = computeView());
      container.style.height = `${totalH}px`;
      container.style.minHeight = `${totalH}px`;
      chart.setOption(
        { title: titles, calendar: calendars, series },
        { replaceMerge: ["calendar", "series", "title"] }
      );
      try {
        chart.resize();
      } catch (_2) {
      }
    };
    try {
      new ResizeObserver(() => {
        try {
          chart.resize();
        } catch (_2) {
        }
      }).observe(container);
    } catch (_2) {
    }
  }
  function _renderTimelineGantt(projectId, data, paneEl) {
    if (!paneEl) return;
    const p3 = window.state?.panels[projectId];
    const { tasks = [], goal_events = [] } = data || {};
    const parseTs = (ts) => {
      if (!ts) return null;
      try {
        return new Date(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
      } catch (_2) {
        return null;
      }
    };
    const sessionNames = [...new Set(tasks.map((t3) => t3.session_name || "(unknown)"))];
    const yCategories = [...sessionNames, "goal"];
    const STATUS_COLOR = { done: "#34d399", failed: "#f87171", in_progress: "#6c8fff", pending: "#9ca3af" };
    const byStatus = {};
    tasks.forEach((t3) => {
      const d3 = parseTs(t3.created_at);
      if (!d3) return;
      const st = t3.status || "pending";
      if (!byStatus[st]) byStatus[st] = [];
      byStatus[st].push({
        value: [d3.getTime(), t3.session_name || "(unknown)"],
        desc: t3.description || "",
        sess: t3.session_name || "(unknown)",
        ts: t3.created_at,
        status: st
      });
    });
    const series = Object.entries(byStatus).map(([st, pts]) => ({
      name: st,
      type: "scatter",
      symbol: "rect",
      symbolSize: [36, 10],
      itemStyle: { color: STATUS_COLOR[st] || "#6b7280", opacity: 0.85 },
      emphasis: { scale: 1.4, itemStyle: { opacity: 1 } },
      data: pts
    }));
    const goalByKey = /* @__PURE__ */ new Map();
    goal_events.forEach((g2) => {
      if (g2.field === "version_goal") {
        const s3 = g2.new_summary || "";
        if (s3.startsWith("[AUTO SUMMARY") || s3.startsWith("- [DONE]") || s3.startsWith("- [PENDING]")) return;
      }
      const key = g2.field + (g2.updated_at || "").slice(0, 13);
      if (!goalByKey.has(key) || g2.version > (goalByKey.get(key).version || 0)) goalByKey.set(key, g2);
    });
    const GOAL_COLOR = { sprint_updated_at: "#6c8fff", ns_updated_at: "#fbbf24", content_updated_at: "#a78bfa" };
    const goalPts = [];
    const markLineData = [];
    goalByKey.forEach((g2) => {
      const d3 = parseTs(g2.updated_at);
      if (!d3) return;
      const color = GOAL_COLOR[g2.field] || "#a78bfa";
      const lbl = g2.field.replace("_updated_at", "").replace("_", " ");
      const ms = d3.getTime();
      goalPts.push({ value: [ms, "goal"], field: lbl, version: g2.version, ts: g2.updated_at, itemStyle: { color } });
      markLineData.push({ xAxis: ms, lineStyle: { color, type: "dashed", width: 1, opacity: 0.5 }, label: { show: false } });
    });
    if (goalPts.length) {
      series.push({
        name: "goal",
        type: "scatter",
        symbol: "diamond",
        symbolSize: 9,
        data: goalPts,
        markLine: { silent: true, symbol: "none", data: markLineData }
      });
    }
    paneEl.innerHTML = "";
    const container = document.createElement("div");
    container.style.cssText = "width:100%;height:100%;min-height:300px";
    paneEl.appendChild(container);
    const chart = echarts.init(container, null, { renderer: "canvas" });
    chart.setOption({
      backgroundColor: "transparent",
      animation: false,
      tooltip: {
        trigger: "item",
        backgroundColor: "#0d1b2e",
        borderColor: "#1e3a5f",
        textStyle: { color: "#c7d5ef", fontSize: 11, fontFamily: "IBM Plex Mono" },
        confine: true,
        className: "timeline-tooltip",
        extraCssText: "max-width:340px;white-space:normal;",
        position: (point, params, dom, rect, size) => {
          const x2 = point[0], y3 = point[1];
          const containerWidth = size && size.viewSize && size.viewSize[0] || 0;
          return x2 > containerWidth * 0.6 ? [x2 - 300, y3] : [x2 + 20, y3];
        },
        formatter: (params) => {
          const d3 = params.data;
          if (d3.field) return `<b>${escapeHtml(d3.field)}</b> v${d3.version}<br><span style="color:#8b9cba;font-size:9px">${escapeHtml(d3.ts || "")}</span>`;
          return `<b>${escapeHtml(d3.sess)}</b><br><span style="color:${STATUS_COLOR[d3.status] || "#9ca3af"}">${escapeHtml(d3.status)}</span> \xB7 <span style="color:#8b9cba;font-size:9px">${escapeHtml(d3.ts || "")}</span><br><span class="timeline-tooltip-desc" style="color:#c7d5ef">${escapeHtml(d3.desc)}</span>`;
        }
      },
      legend: {
        top: 0,
        right: 0,
        textStyle: { color: "#8b9cba", fontSize: 10, fontFamily: "IBM Plex Mono" },
        itemWidth: 10,
        itemHeight: 8
      },
      grid: { top: 26, right: 12, bottom: 26, left: 8, containLabel: true },
      xAxis: {
        type: "time",
        axisLabel: { color: "#8b9cba", fontFamily: "IBM Plex Mono", fontSize: 9, hideOverlap: true },
        splitLine: { lineStyle: { color: "#1e2d4a" } },
        axisLine: { lineStyle: { color: "#1e2d4a" } }
      },
      yAxis: {
        type: "category",
        data: yCategories,
        inverse: true,
        axisLabel: {
          color: "#8b9cba",
          fontFamily: "IBM Plex Mono",
          fontSize: 9,
          formatter: (v3) => v3.length > 22 ? v3.slice(0, 21) + "\u2026" : v3,
          width: 148,
          overflow: "truncate"
        },
        splitLine: { lineStyle: { color: "#1e2d4a55" } },
        axisLine: { lineStyle: { color: "#1e2d4a" } }
      },
      series,
      dataZoom: [{ type: "inside", xAxisIndex: 0 }]
    });
    if (p3) p3._echart = chart;
    const tlRangeKey = `meridian_tl_range_${projectId}`;
    const fromInput = document.getElementById(`timeline-from-${projectId}`);
    const toInput = document.getElementById(`timeline-to-${projectId}`);
    const errEl = document.getElementById(`timeline-range-err-${projectId}`);
    const setZoom = (from, to) => {
      if (from || to) {
        try {
          chart.dispatchAction({ type: "dataZoom", dataZoomIndex: 0, startValue: from ? from.getTime() : void 0, endValue: to ? to.getTime() : void 0 });
        } catch (_2) {
        }
      } else {
        try {
          chart.dispatchAction({ type: "dataZoom", dataZoomIndex: 0, start: 0, end: 100 });
        } catch (_2) {
        }
      }
    };
    const applyRange = () => {
      const fv = fromInput ? fromInput.value : "";
      const tv = toInput ? toInput.value : "";
      const from = fv ? new Date(fv) : null;
      const to = tv ? /* @__PURE__ */ new Date(tv + "T23:59:59Z") : null;
      if (from && to && from >= to) {
        if (errEl) {
          errEl.textContent = "From must be before To";
          errEl.style.display = "";
        }
        return;
      }
      if (errEl) errEl.style.display = "none";
      try {
        if (fv || tv) {
          localStorage.setItem(tlRangeKey, JSON.stringify({ from: fv, to: tv }));
        } else {
          localStorage.removeItem(tlRangeKey);
        }
      } catch (_2) {
      }
      setZoom(from, to);
    };
    const savedRange = (() => {
      try {
        return JSON.parse(localStorage.getItem(tlRangeKey) || "null");
      } catch (_2) {
        return null;
      }
    })();
    if (savedRange && fromInput && toInput) {
      fromInput.value = savedRange.from || "";
      toInput.value = savedRange.to || "";
      if (savedRange.from && savedRange.to) setZoom(new Date(savedRange.from), /* @__PURE__ */ new Date(savedRange.to + "T23:59:59Z"));
    }
    if (fromInput) fromInput.addEventListener("change", applyRange);
    if (toInput) toInput.addEventListener("change", applyRange);
    const nowD = /* @__PURE__ */ new Date();
    const todayStr = nowD.toISOString().slice(0, 10);
    const r7Btn = document.getElementById(`timeline-r7d-${projectId}`);
    const r30Btn = document.getElementById(`timeline-r30d-${projectId}`);
    const rAllBtn = document.getElementById(`timeline-rall-${projectId}`);
    if (r7Btn) r7Btn.onclick = () => {
      if (fromInput) fromInput.value = new Date(nowD.getTime() - 7 * 864e5).toISOString().slice(0, 10);
      if (toInput) toInput.value = todayStr;
      applyRange();
    };
    if (r30Btn) r30Btn.onclick = () => {
      if (fromInput) fromInput.value = new Date(nowD.getTime() - 30 * 864e5).toISOString().slice(0, 10);
      if (toInput) toInput.value = todayStr;
      applyRange();
    };
    if (rAllBtn) rAllBtn.onclick = () => {
      if (fromInput) fromInput.value = "";
      if (toInput) toInput.value = "";
      if (errEl) errEl.style.display = "none";
      try {
        localStorage.removeItem(tlRangeKey);
      } catch (_2) {
      }
      setZoom(null, null);
    };
    try {
      new ResizeObserver(() => {
        try {
          chart.resize();
        } catch (_2) {
        }
      }).observe(container);
    } catch (_2) {
    }
  }
  try {
    Object.assign(window, { renderTimeline: renderTimeline2, _heatmapPieces, _heatmapMaxFor, _renderTimelineHeatmap, _renderTimelineGantt });
  } catch (e3) {
  }

  // meridian/static/dashboard-mcp.ts
  function _renderToolEntry(tool) {
    const props = tool.inputSchema && tool.inputSchema.properties ? tool.inputSchema.properties : {};
    const required = new Set(tool.inputSchema && tool.inputSchema.required || []);
    const params = Object.entries(props).map(([name, schema]) => {
      const req = required.has(name) ? "required" : "optional";
      const type = schema.type || "any";
      const desc = schema.description ? escapeHtml(schema.description) : "";
      return `<tr><td style="color:var(--text);padding:2px 10px 2px 0">${escapeHtml(name)}</td><td style="color:var(--muted);padding:2px 10px 2px 0">${type}</td><td style="color:var(--muted);padding:2px 10px 2px 0;font-style:italic">${req}</td><td style="color:var(--muted);padding:2px 0">${desc}</td></tr>`;
    }).join("");
    const signature = Object.keys(props).map((n2) => required.has(n2) ? n2 : `${n2}?`).join(", ");
    return `<div class="tool-entry" data-search="${escapeHtml((tool.name || "") + " " + (tool.description || ""))}" style="margin-bottom:14px"><div style="color:var(--text);font-weight:600;font-size:13px">${escapeHtml(tool.name)}(<span style="color:var(--muted);font-weight:400">${escapeHtml(signature)}</span>)</div><div style="color:var(--muted);margin:3px 0 5px 0;font-size:12px;line-height:1.45">${escapeHtml(tool.description || "")}</div>${params ? `<table style="font-size:11px;border-collapse:collapse;width:100%">${params}</table>` : ""}</div>`;
  }
  function _groupToolsByCategory(tools, categories) {
    const byName = {};
    (tools || []).forEach((t3) => {
      if (t3 && t3.name) byName[t3.name] = t3;
    });
    const groups = [];
    const claimed = /* @__PURE__ */ new Set();
    for (const [key, names] of Object.entries(categories || {})) {
      const catTools = (names || []).map((n2) => byName[n2]).filter(Boolean);
      if (!catTools.length) continue;
      catTools.forEach((t3) => claimed.add(t3.name));
      groups.push({ key, tools: catTools });
    }
    const rest = (tools || []).filter((t3) => t3 && t3.name && !claimed.has(t3.name));
    if (rest.length) groups.push({ key: "other", tools: rest });
    return groups;
  }
  function _renderToolSections2(tools, categories, labels) {
    const groups = _groupToolsByCategory(tools, categories);
    return groups.map(({ key, tools: catTools }) => {
      const label = labels && labels[key] || "Other";
      const summary = `<summary style="cursor:pointer;list-style:none;color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border);user-select:none">${escapeHtml(label)} <span style="color:var(--muted)">(${catTools.length})</span></summary>`;
      const body = catTools.map(_renderToolEntry).join("");
      return `<details open class="tool-category" data-category="${escapeHtml(key)}" style="margin-bottom:18px">${summary}${body}</details>`;
    }).join("");
  }
  try {
    Object.assign(window, { _renderToolEntry, _groupToolsByCategory, _renderToolSections: _renderToolSections2 });
  } catch (e3) {
  }

  // meridian/static/components/sprintGraph.ts
  var SPRINT_STATUS_COLORS = {
    pending: "#64748b",
    todo: "#64748b",
    in_progress: "#38bdf8",
    provisional_complete: "#a78bfa",
    done: "#22c55e",
    failed: "#ef4444",
    skipped: "#475569",
    pushed: "#14b8a6",
    indeterminate: "#f59e0b"
  };
  function sprintStatusColor(status) {
    return SPRINT_STATUS_COLORS[String(status || "pending")] || "#64748b";
  }
  function parseResources(tr) {
    if (!tr) return [];
    if (Array.isArray(tr)) return tr.map(String);
    const s3 = String(tr).trim();
    if (!s3) return [];
    if (s3.startsWith("[")) {
      try {
        const arr = JSON.parse(s3);
        return Array.isArray(arr) ? arr.map(String) : [];
      } catch {
      }
    }
    return s3.split(",").map((x2) => x2.trim()).filter(Boolean);
  }
  function criticalPath(items) {
    const byId = new Map(items.map((i3) => [i3.id, i3]));
    const memo = /* @__PURE__ */ new Map();
    function chainTo(id, seen) {
      const cached = memo.get(id);
      if (cached) return cached;
      if (seen.has(id)) return [id];
      seen.add(id);
      const it = byId.get(id);
      const dep = it && it.depends_on && byId.has(it.depends_on) ? it.depends_on : null;
      const path = dep ? [...chainTo(dep, seen), id] : [id];
      seen.delete(id);
      memo.set(id, path);
      return path;
    }
    let best = [];
    for (const it of items) {
      const p3 = chainTo(it.id, /* @__PURE__ */ new Set());
      if (p3.length > best.length) best = p3;
    }
    return best;
  }
  function buildSprintDagElements(items) {
    const els = [];
    const ids = new Set(items.map((i3) => i3.id));
    const critical = new Set(criticalPath(items));
    for (const it of items) {
      els.push({
        group: "nodes",
        data: {
          id: `item:${it.id}`,
          label: (it.title || it.id).slice(0, 40),
          status: it.status || "pending",
          color: sprintStatusColor(it.status),
          critical: critical.has(it.id) ? 1 : 0
        }
      });
    }
    for (const it of items) {
      if (it.depends_on && ids.has(it.depends_on)) {
        els.push({
          group: "edges",
          data: {
            id: `dep:${it.depends_on}->${it.id}`,
            source: `item:${it.depends_on}`,
            target: `item:${it.id}`,
            etype: "depends",
            color: "#94a3b8",
            critical: critical.has(it.id) && critical.has(it.depends_on) ? 1 : 0
          }
        });
      }
    }
    const byResource = /* @__PURE__ */ new Map();
    for (const it of items) {
      for (const r3 of parseResources(it.touches_resources)) {
        const arr = byResource.get(r3) || [];
        arr.push(it.id);
        byResource.set(r3, arr);
      }
    }
    const seenPair = /* @__PURE__ */ new Set();
    for (const [resource, memberIds] of byResource) {
      if (memberIds.length < 2) continue;
      for (let i3 = 0; i3 < memberIds.length; i3++) {
        for (let j3 = i3 + 1; j3 < memberIds.length; j3++) {
          const pair = [memberIds[i3], memberIds[j3]].sort();
          const key = pair.join("|");
          if (seenPair.has(key)) continue;
          seenPair.add(key);
          els.push({
            group: "edges",
            data: {
              id: `conflict:${key}`,
              source: `item:${pair[0]}`,
              target: `item:${pair[1]}`,
              etype: "conflict",
              color: "#f59e0b",
              resource
            }
          });
        }
      }
    }
    return els;
  }
  function toMs(ts) {
    if (!ts) return null;
    const t3 = Date.parse(String(ts).replace(" ", "T"));
    return Number.isNaN(t3) ? null : t3;
  }
  function buildGanttModel(items) {
    const rows = [];
    for (const it of items) {
      const start = toMs(it.claimed_at) ?? toMs(it.added_at);
      if (start == null) continue;
      const end = toMs(it.completed_at);
      rows.push({
        id: it.id,
        title: (it.title || it.id).slice(0, 60),
        status: String(it.status || "pending"),
        startMs: start,
        endMs: end == null ? start : Math.max(end, start)
      });
    }
    rows.sort((a3, b2) => a3.startMs - b2.startMs || a3.id.localeCompare(b2.id));
    const minMs = rows.length ? Math.min(...rows.map((r3) => r3.startMs)) : 0;
    const maxMs = rows.length ? Math.max(...rows.map((r3) => r3.endMs)) : 0;
    return { rows, minMs, maxMs };
  }
  function ganttBars(items, minWidthPct = 1.5) {
    const { rows, minMs, maxMs } = buildGanttModel(items);
    const span = Math.max(1, maxMs - minMs);
    return rows.map((r3) => {
      const left = Math.min((r3.startMs - minMs) / span * 100, 100 - minWidthPct);
      const rawWidth = (r3.endMs - r3.startMs) / span * 100;
      const width = Math.min(100 - left, Math.max(minWidthPct, rawWidth));
      return {
        id: r3.id,
        title: r3.title,
        status: r3.status,
        leftPct: +left.toFixed(2),
        widthPct: +width.toFixed(2)
      };
    });
  }
  function mountSprintDag(container, elements) {
    const w3 = typeof window !== "undefined" ? window : void 0;
    const cy = w3 && w3.cytoscape;
    if (!cy || !container) return null;
    try {
      const fcose = w3.cytoscapeFcose;
      if (fcose && !cy.__meridianFcose) {
        cy.use(fcose);
        cy.__meridianFcose = true;
      }
    } catch {
    }
    try {
      return cy({
        container,
        elements: elements.map((e3) => ({ group: e3.group, data: e3.data })),
        style: [
          { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: "#e2e8f0", "font-size": 7, "text-wrap": "wrap", "text-max-width": 80, "text-valign": "center" } },
          { selector: "node[critical = 1]", style: { "border-width": 2, "border-color": "#fde047" } },
          { selector: "edge", style: { width: 1, "line-color": "data(color)", "target-arrow-color": "data(color)", "target-arrow-shape": "triangle", "curve-style": "bezier" } },
          { selector: "edge[etype = 'conflict']", style: { "line-style": "dashed", "target-arrow-shape": "none" } }
        ],
        layout: { name: w3.cytoscapeFcose ? "fcose" : "breadthfirst", directed: true, animate: false }
      });
    } catch {
      return null;
    }
  }

  // meridian/static/dashboard-sprint.ts
  function _ensureSprintPulseStyle() {
    if (typeof document === "undefined") return;
    if (document.getElementById("sprint-pulse-style")) return;
    const st = document.createElement("style");
    st.id = "sprint-pulse-style";
    st.textContent = "@keyframes sprintPulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,0.6)}70%{box-shadow:0 0 0 5px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}";
    document.head.appendChild(st);
  }
  function _sprintHistoryBadges(it) {
    if (!it) return "";
    _ensureSprintPulseStyle();
    let html = "";
    const stall = Number(it.stall_count || 0);
    if (stall > 0) {
      html += `<span class="sprint-stall-badge" title="Stalled and re-queued ${stall} time(s)" style="margin-left:6px;font-size:9px;font-weight:700;color:#f59e0b;background:#f59e0b22;border-radius:3px;padding:1px 5px">\u21BB${stall}</span>`;
    }
    const isPending = it.status === "pending" || it.status === "todo";
    if (isPending && it.claimed_at) {
      html += `<span class="sprint-retried-badge" title="Was claimed by a session before \u2014 now back to pending" style="margin-left:6px;font-size:9px;color:var(--muted);border:1px solid var(--border);border-radius:3px;padding:1px 5px">retried</span>`;
    }
    if (it.status === "in_progress" && it.claimed_at) {
      html += `<span class="sprint-live-dot" title="Claimed by an active session" style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#22c55e;margin-left:6px;animation:sprintPulse 1.6s infinite;vertical-align:middle"></span>`;
    }
    return html;
  }
  if (typeof window !== "undefined") window._sprintHistoryBadges = _sprintHistoryBadges;
  function _renderPlanBadge2(me) {
    const planColors = { free: "#3b82f6", trial: "#059669", standard: "#3b82f6", pro: "#7c3aed", admin: "#9ca3af" };
    const planLabels = _PLAN_LABELS;
    const plan = me.is_internal || me.is_admin ? "admin" : me.plan || "free";
    const verEl = document.getElementById("server-version");
    if (verEl && !document.getElementById("plan-badge")) {
      const badge = document.createElement("span");
      badge.id = "plan-badge";
      const badgeColor = planColors[plan] || "#9ca3af";
      const badgeLabel = plan === "free" && me.days_remaining != null ? `Free \xB7 ${me.days_remaining}d left` : planLabels[plan] || plan;
      badge.title = `${planLabels[plan] || plan} plan`;
      badge.style = `margin-left:6px;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:0.04em;background:${badgeColor}22;color:${badgeColor};border:1px solid ${badgeColor}44;vertical-align:middle`;
      badge.textContent = badgeLabel;
      verEl.parentNode.insertBefore(badge, verEl.nextSibling);
    }
    const noUpgrade = plan === "admin" || !!me.is_internal;
    const planBadge = document.getElementById("plan-badge");
    if (planBadge && !document.getElementById("billing-link")) {
      const hasStripe = !!me.has_stripe_customer;
      if (hasStripe || !noUpgrade) {
        const link = document.createElement("a");
        link.id = "billing-link";
        link.href = hasStripe ? "/billing/portal" : "/pricing";
        link.textContent = hasStripe ? "Manage" : "Upgrade";
        link.title = hasStripe ? "Open Stripe billing portal" : "See plans and upgrade";
        link.style = "margin-left:6px;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:600;letter-spacing:0.03em;background:transparent;color:var(--accent);border:1px solid var(--accent)55;vertical-align:middle;text-decoration:none;cursor:pointer";
        planBadge.parentNode.insertBefore(link, planBadge.nextSibling);
      }
    }
    if (plan === "free" && !me.is_internal && !me.expired && !isDemoMode() && !document.getElementById("trial-banner") && !sessionStorage.getItem("trial-banner-dismissed")) {
      const daysLeft = me.days_remaining != null ? me.days_remaining : 30;
      const elapsed = Math.max(0, 30 - daysLeft);
      const bannerBg = elapsed >= 28 ? "#dc2626" : elapsed >= 25 ? "#d97706" : "#ca8a04";
      const upgradeUrl = window.state?.serverConfig?.stripe_payment_link || "/pricing";
      const daysStr = me.days_remaining != null ? `${me.days_remaining} day${me.days_remaining !== 1 ? "s" : ""}` : "limited time";
      const b2 = document.createElement("div");
      b2.id = "trial-banner";
      b2.style = `position:fixed;top:0;left:0;right:0;z-index:9997;background:${bannerBg};color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em;display:flex;align-items:center;justify-content:center;gap:10px`;
      b2.innerHTML = `<span>Free trial \xB7 <strong>${daysStr} remaining</strong></span><a href="${escapeHtml(upgradeUrl)}" style="color:#fff;text-decoration:underline;font-weight:600;white-space:nowrap">Upgrade to Standard \u2192</a><button onclick="sessionStorage.setItem('trial-banner-dismissed','1');this.closest('#trial-banner').remove();document.body.style.paddingTop=Math.max(0,parseInt(document.body.style.paddingTop||'0',10)-28)+'px'" style="background:none;border:none;color:rgba(255,255,255,0.7);font-size:16px;cursor:pointer;padding:0 0 0 6px;line-height:1" title="Dismiss for this session">\xD7</button>`;
      document.body.prepend(b2);
      document.body.style.paddingTop = parseInt(document.body.style.paddingTop || "0", 10) + 28 + "px";
    }
    if (isHostedMode() && !isHostedAdmin()) {
      const hostedLabel = document.querySelector(".hosted-label");
      if (hostedLabel && !hostedLabel.dataset.planUpdated) {
        hostedLabel.dataset.planUpdated = "1";
        hostedLabel.textContent = "Hosted (shared pool)";
        if (plan === "free" && !me.is_internal && !document.getElementById("db-upgrade-link")) {
          const upgradeLink = document.createElement("a");
          upgradeLink.id = "db-upgrade-link";
          upgradeLink.href = "/pricing";
          upgradeLink.textContent = "Upgrade for dedicated DB \u2192";
          upgradeLink.style.cssText = "display:block;margin-top:3px;font-size:9px;color:var(--accent);text-decoration:none;opacity:.85;font-family:var(--font-mono);letter-spacing:.02em";
          hostedLabel.insertAdjacentElement("afterend", upgradeLink);
        }
      }
    }
    if (me.expired && !document.getElementById("expired-banner")) {
      const b2 = document.createElement("div");
      b2.id = "expired-banner";
      const expLabel = (_PLAN_LABELS[plan] || plan) + " expired";
      b2.style = "position:fixed;top:0;left:0;right:0;z-index:9998;background:#dc2626;color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em";
      b2.innerHTML = `${expLabel}. <a href="/pricing" style="color:#fff;text-decoration:underline">Upgrade to continue \u2192</a>`;
      document.body.prepend(b2);
      document.body.style.paddingTop = parseInt(document.body.style.paddingTop || "0", 10) + 28 + "px";
    }
    if (me.github_connected === false && !isDemoMode() && !document.getElementById("github-onboarding-banner") && !sessionStorage.getItem("github-banner-dismissed")) {
      const b2 = document.createElement("div");
      b2.id = "github-onboarding-banner";
      b2.style = "position:fixed;top:0;left:0;right:0;z-index:9997;background:#7c3aed;color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:10px";
      b2.innerHTML = `<span>Connect your GitHub repo \u2014 give your AI sessions live code access, no extra installs needed.</span><a href="#settings" onclick="document.querySelector('.vtab-btn[data-vtab=settings]')?.click()" style="color:#fff;text-decoration:underline;white-space:nowrap">Connect now \u2192</a><button onclick="sessionStorage.setItem('github-banner-dismissed','1');this.closest('#github-onboarding-banner').remove();document.body.style.paddingTop=Math.max(0,parseInt(document.body.style.paddingTop||'0',10)-28)+'px'" style="background:none;border:none;color:rgba(255,255,255,0.7);font-size:16px;cursor:pointer;padding:0 0 0 6px;line-height:1" title="Dismiss">\xD7</button>`;
      document.body.prepend(b2);
      document.body.style.paddingTop = parseInt(document.body.style.paddingTop || "0", 10) + 28 + "px";
    }
    ensureSignOutLink(me.email);
    ensureWorkspaceSwitcher();
  }
  function renderSprintProgress2(projectId, items) {
    const root = document.getElementById(`live-sprint-progress-${projectId}`);
    if (!root) return;
    const statusIcon = (s3) => ({
      pending: "\u25CB",
      todo: "\u25CB",
      in_progress: "\u25D1",
      done: "\u25CF",
      failed: "\u2715",
      skipped: "\u2014",
      pushed: "\u2192",
      indeterminate: "\u26A0"
    })[s3] || "?";
    const statusColor = (s3) => ({
      pending: "var(--muted)",
      todo: "var(--muted)",
      in_progress: "var(--accent)",
      done: "var(--accent-green)",
      failed: "#e05",
      skipped: "var(--muted)",
      pushed: "var(--accent)",
      indeterminate: "#fbbf24"
    })[s3] || "var(--muted)";
    const activeSet = /* @__PURE__ */ new Set(["pending", "todo", "in_progress"]);
    if (items.length === 0) {
      root.innerHTML = `

      <div class="live-empty">No sprint items. Add one below.</div>

      <div class="sprint-add-row" style="margin-top:6px">

        <input class="live-add-input" id="sprint-add-input-${projectId}"

               placeholder="version:title (e.g. v1.0:My item)">

        <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"

                style="margin-left:4px">+ Add</button>

      </div>`;
      root.querySelector(".sprint-add-btn").onclick = () => addSprintItemFromInput(projectId);
      wireSprintAddEnter(projectId, root);
      return;
    }
    const activeStatuses = /* @__PURE__ */ new Set(["pending", "todo", "in_progress"]);
    const activeVersions = new Set(items.filter((it) => activeStatuses.has(it.status)).map((it) => it.version));
    let displayItems = items.filter(
      (it) => activeStatuses.has(it.status) || it.version && activeVersions.has(it.version)
    );
    if (displayItems.length === 0) displayItems = items.filter((it) => activeStatuses.has(it.status));
    if (displayItems.length === 0) {
      root.innerHTML = `

      <div class="live-empty" style="color:var(--accent-green)">\u{1F389} Sprint complete! All items done.</div>

      <div class="sprint-add-row" style="margin-top:6px">

        <input class="live-add-input" id="sprint-add-input-${projectId}"

               placeholder="version:title  or  just title" style="flex:1">

        <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"

                style="margin-left:4px">+ Add</button>

      </div>`;
      root.querySelector(".sprint-add-btn").onclick = () => addSprintItemFromInput(projectId);
      wireSprintAddEnter(projectId, root);
      return;
    }
    const indeterminateItems = items.filter((it) => it.status === "indeterminate");
    let html = "";
    if (indeterminateItems.length > 0) {
      html += `<div style="background:#422b00;border:1px solid #fbbf24;border-radius:6px;padding:8px 10px;margin-bottom:10px">

      <div style="color:#fbbf24;font-weight:600;margin-bottom:6px;font-size:12px">\u26A0 Needs attention (${indeterminateItems.length})</div>`;
      html += indeterminateItems.map((it) => `

      <div class="sprint-item-row" data-item="${escapeHtml(it.id)}" style="background:transparent;border-bottom:1px solid #5a3b00;padding:4px 0">

        <span class="sprint-item-icon" style="color:#fbbf24">\u26A0</span>

        <span class="sprint-item-title">${escapeHtml(it.title)}</span>

        <span class="sprint-item-ver">${escapeHtml(it.version)}</span>

        <span class="sprint-item-actions">

          <button class="sprint-btn" title="Mark done"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">\u2713 Done</button>

          <button class="sprint-btn" title="Back to pending"

            onclick="fetch('/projects/${escapeHtml(projectId)}/sprint-items/${escapeHtml(it.id)}',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'pending'})}).then(()=>renderSprintProgress(${JSON.stringify(projectId)},items.map(x=>x.id===it.id?{...x,status:'pending'}:x)))">\u21A9 Pending</button>

          <button class="sprint-btn sprint-btn-fail" title="Mark failed"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">\u2715 Fail</button>

          <button class="sprint-btn" title="Backburner (skip)" style="color:var(--muted)"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">\u2014 Backburner</button>

        </span>

      </div>`).join("");
      html += `</div>`;
    }
    const humanItems = items.filter((it) => it.milestone_type === "human" && activeSet.has(it.status));
    if (humanItems.length > 0) {
      html += `<div style="background:rgba(59,130,246,0.08);border:1px solid rgba(59,130,246,0.35);border-radius:6px;padding:8px 10px;margin-bottom:10px">

      <div style="color:var(--accent);font-weight:600;margin-bottom:6px;font-size:12px">\u{1F464} Your tasks (${humanItems.length})</div>`;
      html += humanItems.map((it) => `

      <div class="sprint-item-row" data-item="${escapeHtml(it.id)}" style="background:transparent;border-bottom:1px solid rgba(59,130,246,0.2);padding:4px 0">

        <span class="sprint-item-icon" style="color:var(--accent)">\u{1F464}</span>

        <span class="sprint-item-title">${escapeHtml(it.title)}</span>

        <span class="sprint-item-ver">${escapeHtml(it.version)}</span>

        <span class="sprint-item-actions">

          <button class="sprint-btn" title="Mark done"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">\u2713 Done</button>

        </span>

      </div>`).join("");
      html += `</div>`;
    }
    const allChildrenOf = /* @__PURE__ */ new Map();
    items.forEach((it) => {
      if (it.parent_id) {
        if (!allChildrenOf.has(it.parent_id)) allChildrenOf.set(it.parent_id, []);
        allChildrenOf.get(it.parent_id).push(it);
      }
    });
    const displayedParentIds = new Set(
      displayItems.map((it) => it.id).filter((id) => allChildrenOf.has(id))
    );
    const displayChildrenOf = /* @__PURE__ */ new Map();
    displayItems.forEach((it) => {
      if (it.parent_id && displayedParentIds.has(it.parent_id)) {
        if (!displayChildrenOf.has(it.parent_id)) displayChildrenOf.set(it.parent_id, []);
        displayChildrenOf.get(it.parent_id).push(it);
      }
    });
    const renderItem = (it, isChild) => {
      const icon = statusIcon(it.status);
      const color = statusColor(it.status);
      const isActive = activeSet.has(it.status);
      const meta = it.pushed_to ? `<span class="sprint-item-meta">\u2192 ${escapeHtml(it.pushed_to)}</span>` : "";
      const notesHtml = it.notes && !it.pushed_to ? `<div class="sprint-item-notes" style="font-size:10px;color:var(--muted);margin-top:2px;line-height:1.4;white-space:pre-wrap;word-break:break-word">${escapeHtml(it.notes.length > 180 ? it.notes.slice(0, 180) + "\u2026" : it.notes)}</div>` : "";
      const _resources = (() => {
        try {
          return JSON.parse(it.touches_resources || "[]");
        } catch {
          return [];
        }
      })();
      const resourcesHtml = _resources.length > 0 ? `<div class="sprint-item-resources" style="display:flex;flex-wrap:wrap;gap:3px;margin-top:3px">${_resources.map((r3) => {
        const chipColor = r3.startsWith("note:") ? "var(--accent-blue,#3b82f6)" : r3.startsWith("decision:") ? "#a78bfa" : "var(--muted)";
        return `<span class="resource-chip" onclick="resourceChipClick('${escapeHtml(projectId)}','${escapeHtml(r3)}')" style="font-size:9px;padding:1px 5px;border-radius:3px;cursor:pointer;background:var(--surface-2);border:1px solid var(--border);color:${chipColor};font-family:var(--font-mono)">${escapeHtml(r3)}</span>`;
      }).join("")}</div>` : "";
      const editBtn = `<button class="sprint-btn" title="Edit title/version"

             onclick="sprintItemEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">\u270F</button>`;
      const notesBtn = `<button class="sprint-btn" title="Add/edit notes"

             onclick="sprintItemNotesEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">\u{1F4DD}</button>`;
      const resourcesBtn = `<button class="sprint-btn" title="Edit touches_resources"
             onclick="sprintItemResourcesEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}',${JSON.stringify(it.touches_resources || null)})">\u{1F517}</button>`;
      const feedbackHtml = "";
      const canEdit = it.status === "pending" || it.status === "todo";
      const actions = isActive ? `<span class="sprint-item-actions">

           <button class="sprint-btn" title="Done"

             onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">\u2713</button>

           <button class="sprint-btn" title="Skip"

             onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">\u2014</button>

           <button class="sprint-btn sprint-btn-fail" title="Fail"

             onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">\u2715</button>

           <button class="sprint-btn sprint-btn-push" title="Push to next version"

             onclick="sprintPushPrompt('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">\u2192</button>

           ${canEdit ? editBtn : ""}

           ${notesBtn}

           ${resourcesBtn}

         </span>` : `<span class="sprint-item-actions">${meta}${feedbackHtml}</span>`;
      const allKids = allChildrenOf.get(it.id) || [];
      const kidDone = allKids.filter((c3) => c3.status === "done").length;
      const childBadge = allKids.length > 0 ? `<span style="font-size:10px;color:var(--muted);margin-left:4px">[${kidDone}/${allKids.length}]</span>` : "";
      const indBadge = it.status === "indeterminate" ? `<span style="color:#fbbf24;margin-left:4px;font-size:11px">\u26A0</span>` : "";
      const indentStyle = isChild ? "margin-left:16px;border-left:2px solid var(--border);padding-left:8px;" : "";
      const rowHtml = `<div class="sprint-item-row" data-item="${escapeHtml(it.id)}"

      data-title="${escapeHtml(it.title)}" data-version="${escapeHtml(it.version || "")}"

      data-notes="${escapeHtml(it.notes || "")}"

      style="${indentStyle}">

      <span class="sprint-item-icon" style="color:${color}">${icon}</span>

      <div style="flex:1;min-width:0">

        <span class="sprint-item-title">${escapeHtml(it.title)}${indBadge}${childBadge}${_sprintHistoryBadges(it)}</span>

        ${notesHtml}

        ${resourcesHtml}

      </div>

      <span class="sprint-item-ver">${escapeHtml(it.version || "")}</span>

      ${actions}

    </div>`;
      const dispKids = displayChildrenOf.get(it.id) || [];
      const childrenBlock = dispKids.length > 0 ? `<details style="margin-left:16px;border-left:2px solid var(--border);margin-bottom:2px">

           <summary style="cursor:pointer;padding:2px 6px;font-family:var(--font-mono);font-size:10px;color:var(--muted);list-style:none;display:flex;align-items:center;gap:4px;user-select:none">

             <span>\u25B8</span><span>${dispKids.length} subtask${dispKids.length !== 1 ? "s" : ""} \xB7 ${kidDone}/${allKids.length} done</span>

           </summary>

           <div style="padding:2px 0">

             ${dispKids.map((c3) => renderItem(c3, true)).join("")}

           </div>

         </details>` : "";
      return rowHtml + childrenBlock;
    };
    const versionOrder = [...new Set(displayItems.map((it) => it.version || ""))];
    const groups = /* @__PURE__ */ new Map();
    displayItems.forEach((it) => {
      const g2 = it.version || "";
      if (!groups.has(g2)) groups.set(g2, []);
      groups.get(g2).push(it);
    });
    for (const [groupName, groupItems] of groups) {
      if (groupName) {
        html += `<div class="sprint-group-header">${escapeHtml(groupName)}</div>`;
      }
      const topLevel = groupItems.filter((it) => !it.parent_id || !displayedParentIds.has(it.parent_id));
      html += topLevel.map((it) => renderItem(it, false)).join("");
    }
    const total = displayItems.length;
    const done = displayItems.filter((i3) => i3.status === "done").length;
    const pct = total > 0 ? Math.round(done / total * 100) : 0;
    const pctColor = done === 0 ? "var(--muted)" : done === total ? "var(--accent-green)" : "#fbbf24";
    html += `<div class="sprint-footer">

    <span class="sprint-pct" style="color:${pctColor};font-weight:600">${done}/${total} \xB7 ${pct}%</span>

    <div class="sprint-add-row">

      <input class="live-add-input" id="sprint-add-input-${projectId}"

             placeholder="version:title  or  just title" style="flex:1">

      <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"

              style="margin-left:4px">+ Add</button>

    </div>

  </div>`;
    const pushedItems = items.filter((it) => it.status === "pushed");
    if (pushedItems.length > 0) {
      html += `<details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

      <summary style="cursor:pointer;padding:6px 10px;font-family:var(--font-mono);font-size:10px;color:var(--muted);letter-spacing:.05em;user-select:none;list-style:none;display:flex;align-items:center;gap:6px">

        <span>\u23F8</span><span>Backburner (${pushedItems.length} pushed)</span>

      </summary>

      <div style="padding:4px 10px 8px">

        ${pushedItems.map((it) => `<div class="sprint-item-row" data-item="${escapeHtml(it.id)}" data-title="${escapeHtml(it.title)}" data-version="${escapeHtml(it.version || "")}" style="display:flex;align-items:center;gap:6px;padding:3px 0;border-top:1px solid var(--border)">

          <span style="color:var(--muted);font-size:10px;flex-shrink:0">\u2192</span>

          <span class="sprint-item-title" style="font-family:var(--font-mono);font-size:10px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</span>

          ${it.pushed_to ? `<span style="font-size:9px;color:var(--accent);background:var(--accent)1a;border:1px solid var(--accent)33;border-radius:3px;padding:0 5px;flex-shrink:0;font-family:var(--font-mono)">${escapeHtml(it.pushed_to)}</span>` : ""}

          <span class="sprint-item-ver" style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(it.version || "")}</span>

          <button class="sprint-btn" title="Edit title/version" onclick="sprintItemEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">\u270F</button>

        </div>`).join("")}

      </div>

    </details>`;
    }
    root.innerHTML = html;
    root.querySelector(".sprint-add-btn").onclick = () => addSprintItemFromInput(projectId);
    wireSprintAddEnter(projectId, root);
    try {
      if (Array.isArray(items) && items.length) {
        const dag = document.createElement("details");
        dag.className = "sprint-dag-wrap";
        dag.style.marginTop = "8px";
        dag.innerHTML = `<summary style="cursor:pointer;font-size:10px;color:var(--muted)">Dependency graph</summary><div id="sprint-dag-${escapeHtml(projectId)}" class="sprint-dag" style="width:100%;height:320px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;margin-top:4px"></div>`;
        root.appendChild(dag);
        dag.addEventListener("toggle", () => {
          if (!dag.open) return;
          const host = dag.querySelector(".sprint-dag");
          if (host && !host.dataset.mounted) {
            const cy = mountSprintDag(host, buildSprintDagElements(items));
            if (cy) host.dataset.mounted = "1";
          }
        });
      }
    } catch (e3) {
    }
  }
  function renderQueue2(projectId, sprintItems = []) {
    const panel = getPanelState(projectId);
    const sectionState = panel.queueSectionState || (panel.queueSectionState = {
      backburner: true,
      pending: false,
      in_progress: false,
      done: true,
      failed: true
    });
    const doneLimit = panel.queueDoneLimit || QUEUE_DONE_PAGE_SIZE;
    const totalDoneCount = panel.queueTotalDoneCount != null ? panel.queueTotalDoneCount : (sprintItems || []).filter((it) => it.status === "done").length;
    const items = (sprintItems || []).slice();
    const sortByNewest = (a3, b2) => String(b2.completed_at || b2.added_at || "").localeCompare(String(a3.completed_at || a3.added_at || ""));
    const backburner = items.filter((it) => ["pushed", "skipped"].includes(it.status)).sort(sortByNewest);
    const pending = items.filter((it) => it.status === "pending" || it.status === "todo").sort(sortByNewest);
    const inProgress = items.filter((it) => it.status === "in_progress").sort(sortByNewest);
    const failed = items.filter((it) => it.status === "failed").sort(sortByNewest);
    const doneAll = items.filter((it) => it.status === "done").sort(sortByNewest);
    const done = doneAll.slice(0, doneLimit);
    const renderItem = (it) => {
      const version = it.version ? `<span style="font-size:9px;color:var(--accent);background:var(--accent)1a;border:1px solid var(--accent)33;border-radius:999px;padding:1px 6px;font-family:var(--font-mono)">${escapeHtml(it.version)}</span>` : "";
      const pushedTo = it.pushed_to ? `<span style="font-size:9px;color:var(--muted)">\u2192 ${escapeHtml(it.pushed_to)}</span>` : "";
      const tsSource = it.completed_at || it.added_at || "";
      const meta = [
        it.item_group ? `group: ${it.item_group}` : "",
        it.human_id ? `human: ${it.human_id}` : "",
        it.depends_on ? `depends_on: ${it.depends_on}` : "",
        tsSource ? formatRelativeTime(tsSource) : ""
      ].filter(Boolean).join(" \xB7 ");
      const canAct = ["pending", "todo", "in_progress"].includes(it.status);
      const actions = canAct ? `

      <div style="display:flex;gap:4px;align-items:center;flex-shrink:0">

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Mark done"

          onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">\u2713</button>

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Skip"

          onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">\u2014</button>

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Fail"

          onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">\u2715</button>

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Push to next version"

          onclick="sprintPushPrompt('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">\u2192</button>

      </div>` : "";
      const isBackburner = ["pushed", "skipped"].includes(it.status);
      const archiveBtn = isBackburner ? `

      <div style="flex-shrink:0">

        <button class="secondary" data-demo-hide style="padding:1px 6px;font-size:9px" title="Delete permanently"

          onclick="sprintArchive('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">\u{1F5D1}</button>

      </div>` : "";
      return `<div class="queue-item" data-bb-title="${escapeHtml((it.title || "").toLowerCase())}" data-bb-group="${escapeHtml((it.item_group || "").toLowerCase())}">

      <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">

        <div style="min-width:0;flex:1">

          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

            ${version}

            ${it.slug ? `<span style="font-size:9px;font-family:var(--font-mono);color:var(--muted);background:var(--surface-1);border:1px solid var(--border);border-radius:3px;padding:0 4px;white-space:nowrap" title="${escapeHtml(it.id || "")}">${escapeHtml(it.slug)}</span>` : ""}

            <span style="color:var(--text);font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis">${escapeHtml(it.title || "")}</span>

            ${pushedTo}

          </div>

          ${meta ? `<div class="queue-item-ts" style="margin-left:0;margin-top:3px">${escapeHtml(meta)}</div>` : ""}

          ${it.notes ? `<div style="margin-top:4px;font-size:10px;color:var(--muted);white-space:pre-wrap;word-break:break-word">${escapeHtml(it.notes)}</div>` : ""}

        </div>

        ${actions}${archiveBtn}

      </div>

    </div>`;
    };
    const section = (icon, title, rows, emptyMsg, opts = {}) => {
      const key = opts.key || "";
      const collapsed = key ? sectionState[key] ?? !!opts.collapsed : !!opts.collapsed;
      const footer = opts.footer || "";
      return `<div class="queue-section" data-section="${escapeHtml(key)}" data-collapsed="${collapsed ? "true" : "false"}">

      <div class="queue-section-header" role="button" tabindex="0" aria-expanded="${collapsed ? "false" : "true"}" data-section-key="${escapeHtml(key)}">

        <span class="queue-section-header-label">${icon} ${title} <span class="queue-section-count">(${opts.count != null ? opts.count : rows.length})</span></span>

        <span class="queue-section-chevron" aria-hidden="true">\u25B6</span>

      </div>

      <div class="queue-section-body">

        <div class="queue-section-body-inner">

          ${rows.length ? rows.map(renderItem).join("") : `<div class="queue-empty">${emptyMsg}</div>`}

          ${footer}

        </div>

      </div>

    </div>`;
    };
    const backburnerSection = () => {
      if (!backburner.length) {
        return section("\u23F8", "Backburner", backburner, "no backburner items", { key: "backburner", collapsed: true });
      }
      const collapsed = sectionState.backburner ?? true;
      const groups = {};
      for (const it of backburner) {
        const g2 = it.item_group || "(ungrouped)";
        (groups[g2] = groups[g2] || []).push(it);
      }
      const groupNames = Object.keys(groups).sort((a3, b2) => a3.localeCompare(b2));
      const search = `<input type="text" id="backburner-search-${escapeHtml(projectId)}" placeholder="filter backburner\u2026"

      oninput="filterBackburner('${escapeHtml(projectId)}', this.value)"

      style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 8px;margin-bottom:8px;outline:none">`;
      const groupHtml = groupNames.map((g2) => `

      <div class="bb-group">

        <div style="font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.4px;margin:8px 0 4px">${escapeHtml(g2)} <span style="opacity:0.6">(${groups[g2].length})</span></div>

        ${groups[g2].map(renderItem).join("")}

      </div>`).join("");
      return `<div class="queue-section" data-section="backburner" data-collapsed="${collapsed ? "true" : "false"}">

      <div class="queue-section-header" role="button" tabindex="0" aria-expanded="${collapsed ? "false" : "true"}" data-section-key="backburner">

        <span class="queue-section-header-label">\u23F8 Backburner <span class="queue-section-count">(${backburner.length})</span></span>

        <span class="queue-section-chevron" aria-hidden="true">\u25B6</span>

      </div>

      <div class="queue-section-body">

        <div class="queue-section-body-inner">

          ${search}${groupHtml}

        </div>

      </div>

    </div>`;
    };
    const doneFooter = doneAll.length > done.length ? `<div style="padding-top:6px">

        <button class="secondary" id="queue-done-more-${projectId}" style="padding:3px 10px;font-size:10px">

          Load more (${done.length}/${doneAll.length})

        </button>

      </div>` : "";
    const doneTitle = totalDoneCount ? `${totalDoneCount} completed` : "Done";
    return [
      section("\u23F3", "Pending", pending, "no pending sprint items", { key: "pending" }),
      section("\u{1F504}", "In Progress", inProgress, "nothing in progress", { key: "in_progress" }),
      backburnerSection(),
      section("\u2705", doneTitle, done, "no completed sprint items", { key: "done", collapsed: true, footer: doneFooter, count: totalDoneCount }),
      section("\u2715", "Failed", failed, "no failed sprint items", { key: "failed", collapsed: true })
    ].join("");
  }
  try {
    Object.assign(window, { _renderPlanBadge: _renderPlanBadge2, renderSprintProgress: renderSprintProgress2, renderQueue: renderQueue2 });
  } catch (e3) {
  }

  // meridian/static/dashboard-waves.ts
  var _TERMINAL = /* @__PURE__ */ new Set(["done", "skipped", "failed", "pushed"]);
  var _CLAIMABLE = /* @__PURE__ */ new Set(["pending", "todo"]);
  var _PRIORITY_RANK = { urgent: 0, high: 1, normal: 2, low: 3 };
  var _PRIORITY_DEFAULT = _PRIORITY_RANK.normal;
  function parseTouchesResources(raw) {
    if (raw == null) return [];
    let values;
    if (Array.isArray(raw)) {
      values = raw;
    } else {
      const text = String(raw).trim();
      if (!text) return [];
      try {
        const decoded = JSON.parse(text);
        values = Array.isArray(decoded) ? decoded : [decoded];
      } catch {
        values = text.split(",");
      }
    }
    const out = [];
    const seen = /* @__PURE__ */ new Set();
    for (const v3 of values) {
      const candidate = String(v3 ?? "").trim();
      if (!candidate) continue;
      const norm = candidate.toLowerCase().startsWith("inferred:") ? candidate.slice("inferred:".length).trim() : candidate;
      if (!norm) continue;
      if (!seen.has(norm)) {
        seen.add(norm);
        out.push(norm);
      }
    }
    return out;
  }
  function _resourceFileOf(rid) {
    if (rid.startsWith("file:")) return rid.slice("file:".length);
    if (rid.startsWith("symbol:")) return rid.slice("symbol:".length).split("::")[0];
    return null;
  }
  function _twoResourcesConflict(r1, r22) {
    if (r1 === r22) return true;
    const f1 = _resourceFileOf(r1);
    const f22 = _resourceFileOf(r22);
    if (f1 !== null && f1 === f22) {
      const bothSymbols = r1.startsWith("symbol:") && r22.startsWith("symbol:");
      return !bothSymbols;
    }
    return false;
  }
  function _resourceSetsConflict(a3, b2) {
    for (const ra of a3) {
      for (const rb of b2) {
        if (_twoResourcesConflict(ra, rb)) return true;
      }
    }
    return false;
  }
  function computeWaveProgress(sprintItems) {
    const items = Array.isArray(sprintItems) ? sprintItems : [];
    const statusOf = (it) => String(it.status || "pending");
    const byId = {};
    for (const it of items) if (it && it.id) byId[String(it.id)] = it;
    const doneCount = items.filter((it) => statusOf(it) === "done").length;
    const blockingParent = (it) => {
      const seen = /* @__PURE__ */ new Set();
      let dep = it.depends_on ? String(it.depends_on) : "";
      while (dep && !seen.has(dep)) {
        seen.add(dep);
        const parent = byId[dep];
        if (!parent) return null;
        const ps = statusOf(parent);
        if (!_TERMINAL.has(ps)) return parent;
        if (ps === "failed" && (it.failure_mode || "continue") !== "continue") return parent;
        dep = parent.depends_on ? String(parent.depends_on) : "";
      }
      return null;
    };
    const eligible = [];
    let runningCount = 0;
    let blockedCount = 0;
    for (const it of items) {
      if (!it || !it.id) continue;
      if (it.blocker_kind === "manual") continue;
      const st = statusOf(it);
      const isRunning = st === "in_progress" || _CLAIMABLE.has(st) && !!it.claimed_at;
      if (isRunning) runningCount++;
      if (!_CLAIMABLE.has(st)) continue;
      if (it.claimed_at) continue;
      const parent = blockingParent(it);
      if (parent !== null) {
        const ps = statusOf(parent);
        if (ps === "failed" && (it.failure_mode || "continue") === "continue") {
        } else {
          blockedCount++;
          continue;
        }
      }
      eligible.push(it);
    }
    const sorted = eligible.slice().sort((a3, b2) => {
      const pa = _PRIORITY_RANK[String(a3.priority || "normal")] ?? _PRIORITY_DEFAULT;
      const pb = _PRIORITY_RANK[String(b2.priority || "normal")] ?? _PRIORITY_DEFAULT;
      if (pa !== pb) return pa - pb;
      const aa = String(a3.added_at || "");
      const ab = String(b2.added_at || "");
      if (aa !== ab) return aa < ab ? -1 : 1;
      return String(a3.id) < String(b2.id) ? -1 : 1;
    });
    const withRes = sorted.map((it) => ({ it, res: parseTouchesResources(it.touches_resources) }));
    const declared = withRes.filter((x2) => x2.res.length > 0);
    const undeclared = withRes.filter((x2) => x2.res.length === 0);
    const groups = [];
    const groupResources = [];
    for (const { it, res } of declared) {
      let placed = false;
      for (let gi = 0; gi < groupResources.length; gi++) {
        if (!_resourceSetsConflict(res, groupResources[gi])) {
          groups[gi].push(it);
          for (const r3 of res) groupResources[gi].add(r3);
          placed = true;
          break;
        }
      }
      if (!placed) {
        groups.push([it]);
        groupResources.push(new Set(res));
      }
    }
    for (const { it } of undeclared) groups.push([it]);
    const batches = groups.map((groupItems, index) => {
      let running = 0;
      let earliest = null;
      for (const it of groupItems) {
        const live = byId[String(it.id)] || it;
        const st = statusOf(live);
        const isRunning = st === "in_progress" || _CLAIMABLE.has(st) && !!live.claimed_at;
        if (isRunning) {
          running++;
          const c3 = live.claimed_at ? String(live.claimed_at) : null;
          if (c3 && (earliest === null || c3 < earliest)) earliest = c3;
        }
      }
      return {
        index,
        items: groupItems,
        running,
        pending: groupItems.length - running,
        total: groupItems.length,
        parallel: groupItems.length > 1,
        earliestClaim: earliest
      };
    });
    const maxFanOut = batches.reduce((m3, b2) => Math.max(m3, b2.total), 0);
    let activeBatchIndex = batches.findIndex((b2) => b2.running > 0);
    if (activeBatchIndex < 0) activeBatchIndex = batches.findIndex((b2) => b2.total > 0);
    return {
      batches,
      groupCount: groups.length,
      eligibleCount: eligible.length,
      runningCount,
      blockedCount,
      undeclaredCount: undeclared.length,
      doneCount,
      maxFanOut,
      activeBatchIndex,
      strategy: maxFanOut > 1 ? "parallel" : "sequential",
      hasTokenTelemetry: false
    };
  }
  function formatElapsedSince(claimedAt, nowMs) {
    if (!claimedAt) return "";
    const t3 = Date.parse(String(claimedAt).replace(" ", "T"));
    if (isNaN(t3)) return "";
    const now = nowMs == null ? Date.now() : nowMs;
    const mins = Math.max(0, Math.floor((now - t3) / 6e4));
    if (mins < 60) return `${mins}m`;
    return `${Math.floor(mins / 60)}h ${mins % 60}m`;
  }
  function _esc(s3) {
    try {
      return typeof escapeHtml === "function" ? escapeHtml(String(s3 ?? "")) : String(s3 ?? "");
    } catch {
      return String(s3 ?? "");
    }
  }
  function buildWaveProgressHtml(wp, nowMs) {
    if (!wp.batches.length) {
      if (wp.blockedCount > 0) {
        return `<div class="live-empty">No parallel batch ready \u2014 ${wp.blockedCount} item(s) blocked on dependencies.</div>`;
      }
      return `<div class="live-empty">No parallelizable work right now.</div>`;
    }
    const stratColor = wp.strategy === "parallel" ? "var(--accent-green)" : "var(--muted)";
    const header = `<div class="wave-progress-summary" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px;font-size:11px"><span style="font-weight:600;color:${stratColor}">${_esc(wp.strategy.toUpperCase())}</span><span style="color:var(--muted)">${wp.groupCount} wave${wp.groupCount !== 1 ? "s" : ""}</span><span style="color:var(--muted)">\xB7 ${wp.eligibleCount} eligible</span>` + (wp.runningCount ? `<span style="color:var(--accent-green)">\xB7 ${wp.runningCount} running</span>` : "") + (wp.blockedCount ? `<span style="color:#fbbf24">\xB7 ${wp.blockedCount} blocked</span>` : "") + (wp.maxFanOut > 1 ? `<span style="color:var(--accent)">\xB7 up to ${wp.maxFanOut}\xD7 parallel</span>` : "") + `</div>`;
    const rows = wp.batches.map((b2) => {
      const isActive = b2.index === wp.activeBatchIndex;
      const accent = b2.running > 0 ? "var(--accent-green)" : isActive ? "var(--accent)" : "var(--border)";
      const elapsed = formatElapsedSince(b2.earliestClaim, nowMs);
      const parallelTag = b2.parallel ? `<span title="These items touch no shared resources \u2014 safe to run at once" style="font-size:9px;color:var(--accent);border:1px solid var(--accent)55;border-radius:3px;padding:0 4px">\u2225 ${b2.total}\xD7</span>` : `<span title="Runs on its own \u2014 no proven parallel-safe peer" style="font-size:9px;color:var(--muted)">seq</span>`;
      const counts = (b2.running ? `<span style="color:var(--accent-green)">${b2.running} running</span> \xB7 ` : "") + `<span style="color:var(--muted)">${b2.pending} pending</span>`;
      const items = b2.items.map((it) => {
        const live = String(it.status || "pending");
        const dot = live === "in_progress" || it.claimed_at ? "var(--accent-green)" : "var(--muted)";
        return `<div style="display:flex;gap:6px;align-items:center;margin-top:3px">
            <span style="width:5px;height:5px;border-radius:50%;background:${dot};flex:none"></span>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="${_esc(it.title || it.id)}">${_esc(it.title || it.id)}</span>
          </div>`;
      }).join("");
      return `<div class="wave-batch" data-batch="${b2.index}" style="border-left:3px solid ${accent};padding:4px 0 6px 8px;margin-bottom:8px">
        <div style="display:flex;gap:6px;align-items:center;justify-content:space-between">
          <span style="font-size:11px;font-weight:600;color:var(--text)">Wave ${b2.index + 1} ${parallelTag}${isActive ? ' <span style="font-size:9px;color:var(--accent)">\u25CF active</span>' : ""}</span>
          <span style="font-size:9px;color:var(--muted);flex:none">${counts}${elapsed ? ` \xB7 ${_esc(elapsed)}` : ""}</span>
        </div>
        ${items}
      </div>`;
    }).join("");
    const gapNote = `<div class="wave-progress-note" style="margin-top:4px;font-size:9px;color:var(--muted);line-height:1.4">Cost shown is wall-clock elapsed since an item was claimed. Per-wave token/$ cost isn't tracked server-side yet, so it's omitted rather than estimated.</div>`;
    return header + rows + gapNote;
  }
  function renderWaveProgress2(projectId, sprintItems) {
    try {
      if (typeof document === "undefined") return;
      const root = document.getElementById(`live-wave-progress-${projectId}`);
      if (!root) return;
      const wp = computeWaveProgress(sprintItems);
      root.innerHTML = buildWaveProgressHtml(wp);
    } catch {
    }
  }
  try {
    Object.assign(window, {
      computeWaveProgress,
      buildWaveProgressHtml,
      renderWaveProgress: renderWaveProgress2,
      formatElapsedSince,
      parseTouchesResources
    });
  } catch {
  }

  // meridian/static/dashboard-settings.ts
  function suggestNtfyTopic2(projectId) {
    const proj = (window.state?.projects || []).find((p3) => p3.id === projectId);
    const slug = (proj?.name || "meridian").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24) || "meridian";
    return slug;
  }
  function _execTurnsNumberInputHtml(field, projectId, value, min, max, step) {
    const esc = window.escapeHtml || String;
    return `<input id="exec-${field}-${projectId}" type="number" inputmode="numeric" min="${min}" max="${max}" step="${step}" value="${esc(String(value))}" style="width:100px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:3px 6px;margin-top:4px;display:block">`;
  }
  window._execTurnsNumberInputHtml = _execTurnsNumberInputHtml;
  function claudeRcWatcherBlurb() {
    return "For <code>claude --rc</code> (headless) mode, where SessionStart hooks don't fire \u2014 installs a background watcher that fires the hook per session.";
  }
  function codexSetupBlurb(mcpHttpUrl, esc) {
    return `Add to <code>~/.codex/config.toml</code>, or run <code>codex mcp add meridian ${esc(mcpHttpUrl)}</code>.`;
  }
  function _applySettingsRoleVisibility(projectId, guest) {
    if (!guest) return;
    const body = document.getElementById(`settings-body-${projectId}`);
    if (!body) return;
    [
      `settings-account-card-${projectId}`,
      // account + plan + billing + delete
      `settings-account-danger-${projectId}`,
      // export my data + danger zone
      `settings-notifications-card-${projectId}`,
      // ntfy / webhook / email
      `workspace-section-${projectId}`
      // workspace defaults + decisions/notes
    ].forEach((id) => {
      const el2 = document.getElementById(id);
      if (el2) el2.style.display = "none";
    });
    const inviteForm = document.getElementById(`settings-invite-form-${projectId}`);
    if (inviteForm) inviteForm.style.display = "none";
    body.querySelectorAll("button").forEach((b2) => {
      if (/^save\b/i.test((b2.textContent || "").trim())) b2.style.display = "none";
    });
  }
  function _detectClientOS() {
    const ua = (navigator.userAgent || navigator.platform || "").toLowerCase();
    return ua.includes("win") ? "windows" : "unix";
  }
  var _MERIDIAN_CONNECT_ASSETS = [
    ["Windows x86_64", "meridian-connect-x86_64-windows.exe"],
    ["macOS Apple Silicon", "meridian-connect-aarch64-apple-darwin"],
    ["Linux x86_64", "meridian-connect-x86_64-unknown-linux"]
  ];
  function _directBinaryDownloadsHtml() {
    const base = "https://github.com/meridianmcp/Meridian/releases/latest/download";
    const rows = _MERIDIAN_CONNECT_ASSETS.map(
      ([label, asset]) => `<li style="margin:0 0 3px 0"><span style="display:inline-block;min-width:120px;color:var(--muted)">${label}</span> <a href="${base}/${asset}" download style="color:var(--accent);text-decoration:none;word-break:break-all">${asset}</a></li>`
    ).join("");
    return `<details style="margin-top:10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">
    <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">Direct binary download (if the install script fails)</summary>
    <div style="padding:0 10px 10px">
      <div style="font-size:10px;color:var(--muted);margin:6px 0 6px">Grab the <code>meridian-connect</code> tunnel binary for your platform straight from the latest GitHub release, then run it manually:</div>
      <ul style="list-style:none;margin:0;padding:0;font-size:10px;font-family:var(--font-mono)">${rows}</ul>
    </div>
  </details>`;
  }
  function _collapseConnectPlatforms(projectId) {
    const body = document.getElementById(`settings-body-${projectId}`);
    if (!body) return;
    const os = _detectClientOS();
    const grids = /* @__PURE__ */ new Set();
    body.querySelectorAll('pre[id^="hooks-install-"]').forEach((pre) => {
      const platform = pre.id.includes("-windows-") ? "windows" : "unix";
      const wrap = pre.parentElement;
      if (!wrap) return;
      wrap.dataset.connectPlatform = platform;
      wrap.style.display = platform === os ? "" : "none";
      if (wrap.parentElement) grids.add(wrap.parentElement);
    });
    grids.forEach((grid) => {
      if (grid.parentElement && grid.parentElement.querySelector(".connect-os-toggle")) return;
      const link = document.createElement("a");
      link.href = "#";
      link.className = "connect-os-toggle";
      link.textContent = "Show other platforms";
      link.style.cssText = "display:inline-block;margin:2px 0 6px;font-size:10px;color:var(--accent);text-decoration:none;cursor:pointer";
      link.onclick = (e3) => {
        e3.preventDefault();
        const expanded = link.dataset.expanded === "1";
        grid.querySelectorAll("[data-connect-platform]").forEach((el2) => {
          el2.style.display = expanded ? el2.dataset.connectPlatform === os ? "" : "none" : "";
        });
        link.dataset.expanded = expanded ? "" : "1";
        link.textContent = expanded ? "Show other platforms" : "Show detected platform only";
      };
      grid.insertAdjacentElement("afterend", link);
    });
  }
  function _classifySettingsSection(el2, projectId) {
    const id = el2 && el2.id || "";
    const has = (frag) => id.indexOf(frag) !== -1;
    if (has("workspace-section")) return "workspace";
    if (has("settings-account-card") || has("settings-account-danger") || has("settings-grp-aw") || has("settings-grp-blog") || has("members-section") || has("fs-mcp-section") || has("tunnel-plugins") || has("github-card")) return "account";
    return "project";
  }
  window._classifySettingsSection = _classifySettingsSection;
  function _applyActiveTabVisibility(projectId) {
    const body = document.getElementById(`settings-body-${projectId}`);
    if (!body) return;
    const key = body.dataset.activeStab || "project";
    body.querySelectorAll(":scope > .settings-tabpane").forEach((p3) => {
      p3.style.display = p3.dataset.stabPane === key ? "" : "none";
    });
    body.querySelectorAll(":scope > .settings-tabbar .settings-tab-btn").forEach((b2) => {
      const active = b2.dataset.stabBtn === key;
      b2.style.color = active ? "var(--text)" : "var(--muted)";
      b2.style.borderBottomColor = active ? "var(--accent)" : "transparent";
      b2.style.fontWeight = active ? "600" : "400";
    });
  }
  var _settingsTabDataCache = /* @__PURE__ */ new Map();
  window._settingsTabDataCache = _settingsTabDataCache;
  async function _loadSettingsAccountPane(projectId) {
    const cacheKey = `${projectId}:account`;
    if (_settingsTabDataCache.has(cacheKey)) {
      _applySettingsAccountPaneData(projectId, _settingsTabDataCache.get(cacheKey));
      return;
    }
    const PREFS = [
      { key: "hitl", label: "HITL \u2014 get notified when a session needs your input" },
      { key: "sprint", label: "Sprint done \u2014 all items completed" }
    ];
    const ntfyPlaceholder = document.getElementById(`settings-ntfy-lazy-${projectId}`);
    const prefsPlaceholder = document.getElementById(`settings-prefs-lazy-${projectId}`);
    if (ntfyPlaceholder) ntfyPlaceholder.innerHTML = '<div style="color:var(--muted);font-size:10px">loading\u2026</div>';
    if (prefsPlaceholder) prefsPlaceholder.innerHTML = '<div style="color:var(--muted);font-size:10px">loading\u2026</div>';
    try {
      const [notifResult, ntfyResult, mcpResult] = await Promise.allSettled([
        api("/settings/notifications"),
        api(`/projects/${projectId}/ntfy`),
        api("/settings/mcp-config")
      ]);
      const data = { notifResult, ntfyResult, mcpResult, PREFS };
      _settingsTabDataCache.set(cacheKey, data);
      _applySettingsAccountPaneData(projectId, data);
    } catch (e3) {
      if (ntfyPlaceholder) ntfyPlaceholder.innerHTML = '<div style="color:var(--muted);font-size:10px">Could not load notification settings.</div>';
      if (prefsPlaceholder) prefsPlaceholder.innerHTML = "";
    }
  }
  window._loadSettingsAccountPane = _loadSettingsAccountPane;
  function _applySettingsAccountPaneData(projectId, data) {
    const { notifResult, ntfyResult, mcpResult, PREFS } = data;
    const prefs = notifResult.status === "fulfilled" ? notifResult.value.prefs || {} : null;
    const mcpData = mcpResult.status === "fulfilled" ? mcpResult.value : null;
    const ntfyPlaceholder = document.getElementById(`settings-ntfy-lazy-${projectId}`);
    if (ntfyPlaceholder) ntfyPlaceholder.outerHTML = _settingsNotificationsCardHtml(projectId, ntfyResult);
    const prefsPlaceholder = document.getElementById(`settings-prefs-lazy-${projectId}`);
    if (prefsPlaceholder) prefsPlaceholder.outerHTML = _settingsNotificationPrefsHtml(projectId, prefs, PREFS, mcpData);
  }
  window._applySettingsAccountPaneData = _applySettingsAccountPaneData;
  function _activateSettingsTab(projectId, key) {
    const body = document.getElementById(`settings-body-${projectId}`);
    if (!body) return;
    body.dataset.activeStab = key;
    _applyActiveTabVisibility(projectId);
    if (key === "account") {
      const cacheKey = `${projectId}:account`;
      if (!_settingsTabDataCache.has(cacheKey)) {
        _loadSettingsAccountPane(projectId).catch(() => {
        });
      }
    }
  }
  window._activateSettingsTab = _activateSettingsTab;
  function _organizeSettingsIntoTabs(projectId) {
    const body = document.getElementById(`settings-body-${projectId}`);
    if (!body || body.dataset.tabbed === "1") return;
    body.dataset.tabbed = "1";
    const TABS = [["project", "Project"], ["workspace", "Workspace"], ["account", "Account"]];
    const panes = {};
    const bar = document.createElement("div");
    bar.className = "settings-tabbar";
    bar.style.cssText = "display:flex;gap:2px;margin-bottom:12px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface);z-index:2";
    TABS.forEach(([key, label]) => {
      const pane = document.createElement("div");
      pane.className = "settings-tabpane";
      pane.dataset.stabPane = key;
      panes[key] = pane;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "settings-tab-btn";
      btn.dataset.stabBtn = key;
      btn.textContent = label;
      btn.style.cssText = "background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);font-size:11px;font-family:var(--font-mono);padding:6px 12px;cursor:pointer";
      btn.onclick = () => _activateSettingsTab(projectId, key);
      bar.appendChild(btn);
    });
    Array.from(body.children).forEach((el2) => {
      panes[_classifySettingsSection(el2, projectId)].appendChild(el2);
    });
    body.appendChild(bar);
    TABS.forEach(([key]) => body.appendChild(panes[key]));
    const wsSec = document.getElementById(`workspace-section-${projectId}`);
    if (wsSec && wsSec.parentElement !== panes.workspace) panes.workspace.appendChild(wsSec);
    try {
      const obs = new MutationObserver((muts) => {
        muts.forEach((m3) => m3.addedNodes.forEach((node) => {
          if (node.nodeType !== 1) return;
          if (node.classList && (node.classList.contains("settings-tabpane") || node.classList.contains("settings-tabbar"))) return;
          const tab = (node.id || "").indexOf("tunnel-plugins") !== -1 ? "account" : _classifySettingsSection(node, projectId);
          panes[tab].appendChild(node);
          _applyActiveTabVisibility(projectId);
        }));
      });
      obs.observe(body, { childList: true });
      body._settingsObs = obs;
    } catch (e3) {
    }
    _activateSettingsTab(projectId, "project");
  }
  window._organizeSettingsIntoTabs = _organizeSettingsIntoTabs;
  function _settingsAccountCardHtml(projectId) {
    let out = "";
    if (window.state.tenantEmail) {
      const plan = window.state.tenantPlan || "free";
      const hasStripe = !!window.state.tenantHasStripe;
      const noUpgrade = plan === "admin" || !!window.state.tenantIsInternal;
      let billingBtn = "";
      if (hasStripe) {
        billingBtn = `<button id="billing-portal-btn-${escapeHtml(projectId)}" class="primary" style="padding:4px 10px;font-size:10px;background:var(--accent);color:#001020;border-radius:4px;font-weight:600;cursor:pointer;border:none">Manage billing \u2192</button>`;
      } else if (!noUpgrade) {
        const upgradeUrl = window.state.serverConfig?.stripe_payment_link || "/pricing";
        billingBtn = `<a href="${escapeHtml(upgradeUrl)}" class="primary" style="padding:4px 10px;font-size:10px;text-decoration:none;background:var(--accent);color:#001020;border-radius:4px;font-weight:600">Upgrade to Standard \u2192</a>`;
      }
      const days = window.state.tenantDaysRemaining;
      const expiresAt = window.state.tenantExpiresAt;
      const isTrialish = (plan === "free" || plan === "trial") && !window.state.tenantIsInternal;
      let expiryLine = "";
      let resubBtn = "";
      if (isTrialish && (expiresAt || days != null || window.state.tenantExpired)) {
        const dateStr = expiresAt ? String(expiresAt).slice(0, 10) : "";
        if (window.state.tenantExpired) {
          expiryLine = `<div style="color:#f87171">${_PLAN_LABELS[plan] || plan} expired${dateStr ? ` on ${escapeHtml(dateStr)}` : ""}.</div>`;
        } else {
          const dleft = days != null ? `${days} day${days === 1 ? "" : "s"} left` : "";
          expiryLine = `<div>${_PLAN_LABELS[plan] || plan} expires${dateStr ? ` on <span style="color:var(--text)">${escapeHtml(dateStr)}</span>` : ""}${dleft ? ` <span style="color:var(--muted)">(${dleft})</span>` : ""}.</div>`;
        }
        const payLink = window.state.serverConfig?.stripe_payment_link || "/pricing";
        resubBtn = `<a href="${escapeHtml(payLink)}" class="primary" style="padding:4px 10px;font-size:10px;text-decoration:none;background:var(--accent);color:#001020;border-radius:4px;font-weight:600">${window.state.tenantExpired ? "Resubscribe" : "Upgrade to Standard"}</a>`;
      }
      out += `<div data-demo-hide id="settings-account-card-${projectId}" style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

      <div style="font-weight:600;font-size:11px;color:var(--text);margin-bottom:6px">Account</div>

      <div style="font-size:10px;color:var(--muted);line-height:1.7">

        <div>Email: <span style="color:var(--text)">${escapeHtml(window.state.tenantEmail)}</span></div>

        <div>Plan: <span style="color:var(--text)">${escapeHtml(_PLAN_LABELS[plan] || plan)}</span></div>

        ${expiryLine}

      </div>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">

        ${resubBtn || billingBtn}

        <a href="/auth/logout" class="secondary" style="padding:4px 10px;font-size:10px;text-decoration:none;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:4px">Sign out</a>

        <button id="account-delete-${projectId}" class="secondary" style="padding:4px 10px;font-size:10px;background:var(--surface-1);color:#f87171;border:1px solid #f8717155;border-radius:4px;cursor:pointer">Delete account\u2026</button>

      </div>

    </div>`;
    }
    return out;
  }
  function _settingsBrowserConnectorCardHtml(projectId) {
    let out = "";
    const browserConnectorAccountNote = isHostedMode() ? `

    <div style="margin-top:6px;font-size:10px;color:var(--muted)">

      The browser connector uses whichever Meridian account is logged in at usemeridian.us in this

      browser tab. To use a different account, sign out and sign back in before reconnecting.

      <a href="/auth/logout?next=/auth/login" style="color:var(--accent);text-decoration:none;white-space:nowrap">Switch Meridian account \u2192</a>

    </div>` : "";
    out += `<div style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">

      <div>

        <div style="font-weight:600;font-size:11px;color:var(--text);margin-bottom:2px">Browser connector</div>

        <div style="font-size:10px;color:var(--muted)">Use Meridian directly in Claude or ChatGPT - hosted MCP, no extension required</div>

      </div>

      <a href="https://docs.usemeridian.us/browser-connector/" target="_blank" style="white-space:nowrap;padding:4px 10px;background:var(--accent);color:#fff;border-radius:4px;font-size:10px;font-weight:600;text-decoration:none">Setup guide \u2192</a>

    </div>

    ${browserConnectorAccountNote}

  </div>`;
    return out;
  }
  function _settingsNotificationsCardHtml(projectId, ntfyResult) {
    let out = "";
    const ntfyData = ntfyResult.status === "fulfilled" ? ntfyResult.value : null;
    const savedNotifyUrl = ntfyData ? ntfyData.notify_url || ntfyData.ntfy_url || "" : "";
    const savedNotifyEmail = ntfyData ? ntfyData.notify_email || "" : "";
    const defaultNotifyUrl = displayNotifyTarget(savedNotifyUrl);
    let ntfyWarnAcknowledged = false;
    try {
      ntfyWarnAcknowledged = localStorage.getItem(STORAGE_KEY("ntfy.warn.dismissed")) === "1";
    } catch (e3) {
    }
    const ntfyInputDisabled = ntfyWarnAcknowledged ? "" : "disabled";
    const ntfyWarnDisplay = ntfyWarnAcknowledged ? "display:none" : "";
    out += `<div data-demo-hide id="settings-notifications-card-${projectId}" style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <div style="font-weight:600;font-size:11px;color:var(--text);margin-bottom:4px">Notifications</div>

    <div style="font-size:10px;color:var(--muted);margin-bottom:8px">

      Save a push/webhook target and an email target independently.

      Alerts fire on HITL requests and sprint completions. No account needed for ntfy.

    </div>

    <div id="ntfy-warn-${projectId}" style="margin-bottom:8px;padding:8px 10px;border:1px solid #f59e0b88;border-radius:5px;background:#f59e0b11;font-size:10px;color:#f59e0b;line-height:1.5;${ntfyWarnDisplay}">

      <strong>\u26A0 Security notice:</strong> ntfy.sh topics are public \u2014 anyone who knows your topic name can subscribe and read your alerts. Use a long, random topic name (e.g. <code>my-project-a7f3k2</code>) or self-host ntfy for privacy. Slack/Discord webhooks and email are private alternatives.<br>

      <label style="display:flex;align-items:center;gap:6px;margin-top:6px;cursor:pointer;color:var(--text)">

        <input type="checkbox" id="ntfy-warn-ack-${projectId}" style="cursor:pointer;accent-color:#f59e0b">

        I understand my ntfy topic is public

      </label>

    </div>

    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

      <label style="font-size:10px;color:var(--muted);white-space:nowrap;min-width:100px">ntfy_url:</label>

      <input type="text" id="ntfy-url-${projectId}"

        value="${escapeHtml(defaultNotifyUrl)}"

        placeholder="${escapeHtml(suggestNtfyTopic2(projectId))}  \xB7  https://hooks.slack.com/\u2026"

        ${ntfyInputDisabled}

        style="flex:1;min-width:200px;padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;opacity:${ntfyWarnAcknowledged ? "1" : "0.4"}">

      <button class="secondary" id="ntfy-save-${projectId}" ${ntfyInputDisabled} style="padding:4px 10px;font-size:10px;opacity:${ntfyWarnAcknowledged ? "1" : "0.4"}">Save</button>

      <button class="secondary" id="ntfy-test-${projectId}" ${ntfyInputDisabled} style="padding:4px 10px;font-size:10px;opacity:${ntfyWarnAcknowledged ? "1" : "0.4"}" title="Send a test notification to verify your URL">Test</button>

      <span id="ntfy-status-${projectId}" style="font-size:10px;color:var(--muted);min-width:40px"></span>

    </div>

    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px">

      <label style="font-size:10px;color:var(--muted);white-space:nowrap;min-width:100px">notify_email:</label>

      <input type="email" id="notify-email-${projectId}"
        value="${escapeHtml(savedNotifyEmail || window.state.tenantEmail || "")}"
        placeholder="you@example.com"
        style="flex:1;min-width:180px;padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

      <button class="secondary" id="notify-email-save-${projectId}" style="padding:4px 10px;font-size:10px">Save</button>

      <span id="notify-email-status-${projectId}" style="font-size:10px;color:var(--muted);min-width:40px"></span>

    </div>

    <div style="font-size:9px;color:var(--muted);margin-top:4px;line-height:1.6">

      <strong>ntfy</strong> \u2014 install the ntfy app (iOS / Android / desktop), pick any topic name, and type it here. The <code>https://ntfy.sh/</code> prefix is added for you.<br>

      <strong>Email</strong> \u2014 save <code>notify_email</code> separately to get alerts by email (hosted only; fires independently from ntfy).<br>

      <strong>Webhook</strong> \u2014 paste any <code>https://</code> URL (Slack, Discord, or your own) to receive a JSON POST.

    </div>

  </div>`;
    return out;
  }
  function _settingsNotificationPrefsHtml(projectId, prefs, PREFS, mcpData) {
    let out = "";
    if (prefs !== null) {
      out += `<div style="margin-bottom:12px">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Email notifications</div>`;
      PREFS.forEach((p3) => {
        const checked = prefs[p3.key] ? "checked" : "";
        out += `<label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;cursor:pointer;font-family:var(--font-mono);font-size:11px;color:var(--text)">

        <input type="checkbox" data-pref="${p3.key}" ${checked} style="cursor:pointer">

        ${escapeHtml(p3.label)}

      </label>`;
      });
      out += `<div id="settings-save-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px;margin-top:6px"></div>`;
      out += "</div>";
    } else if (!mcpData) {
      out += '<div style="color:var(--muted);font-size:11px;padding:8px 0">Settings are only available in hosted mode (usemeridian.us).</div>';
    }
    return out;
  }
  window._settingsAccountCardHtml = _settingsAccountCardHtml;
  window._settingsBrowserConnectorCardHtml = _settingsBrowserConnectorCardHtml;
  window._settingsNotificationsCardHtml = _settingsNotificationsCardHtml;
  window._settingsNotificationPrefsHtml = _settingsNotificationPrefsHtml;
  function _toolsReferenceHtml(tools) {
    const TIERS = [
      ["main-workflow", "Main Workflow", "Called in virtually every session \u2014 the essential executor loop."],
      ["common-support", "Common Support", "Called frequently; regular hygiene most healthy sessions use."],
      ["maintenance-only", "Maintenance Only", "Genuinely occasional: periodic diagnostics, orchestrator-only primitives, one-time configuration."]
    ];
    const TIER_COLORS = {
      "main-workflow": "var(--accent)",
      "common-support": "#a78bfa",
      "maintenance-only": "var(--muted)"
    };
    const byTier = {
      "main-workflow": [],
      "common-support": [],
      "maintenance-only": []
    };
    for (const t3 of tools) {
      const tier = t3.workflow_tier || "common-support";
      if (!byTier[tier]) byTier[tier] = [];
      byTier[tier].push(t3);
    }
    let out = `<div style="font-size:10px;color:var(--muted);margin-bottom:12px">
    ${tools.length} built-in MCP tools across 3 tiers. The <code>workflow_tier</code> field is machine-readable in every <code>tools/list</code> response.
  </div>`;
    for (const [tierKey, tierLabel, tierDesc] of TIERS) {
      const tierTools = byTier[tierKey] || [];
      const color = TIER_COLORS[tierKey] || "var(--muted)";
      out += `<div style="margin-bottom:14px">
      <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px;border-bottom:1px solid var(--border);padding-bottom:4px">
        <span style="font-size:10px;font-weight:700;color:${color};text-transform:uppercase;letter-spacing:.06em">${tierLabel}</span>
        <span style="font-size:9px;color:var(--muted)">${tierDesc}</span>
        <span style="margin-left:auto;font-size:9px;color:var(--muted)">${tierTools.length} tools</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px">`;
      for (const tool of tierTools) {
        const displayName = tool.title || tool.name;
        const desc = (tool.description || "").replace(/\s+/g, " ").slice(0, 120);
        out += `<div style="padding:4px 6px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;display:flex;flex-direction:column;gap:2px" title="${window.escapeHtml ? window.escapeHtml(desc) : desc}">
          <code style="font-size:9px;font-family:var(--font-mono);color:var(--text);word-break:break-all">${tool.name}</code>
          <span style="font-size:9px;color:var(--muted);line-height:1.3;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">${displayName}</span>
        </div>`;
      }
      out += `</div></div>`;
    }
    return out;
  }
  window._toolsReferenceHtml = _toolsReferenceHtml;
  async function loadSettingsTab2(projectId, { force = false } = {}) {
    const body = document.getElementById(`settings-body-${projectId}`);
    if (!body) return;
    if (!force && body.dataset.tabbed === "1" && body.children.length > 0) {
      const age = Date.now() - parseInt(body.dataset.settingsTs || "0", 10);
      if (age < 3e4) return;
    }
    if (!("_loadTok" in body)) body._loadTok = 0;
    const _myTok = ++body._loadTok;
    if (body._settingsObs) {
      body._settingsObs.disconnect();
      body._settingsObs = null;
    }
    delete body.dataset.tabbed;
    body.innerHTML = '<div style="color:var(--muted);font-size:11px">loading\u2026</div>';
    const _activeRole = await getActiveWorkspaceRole();
    const _guest = _activeRole === "viewer" || _activeRole === "member";
    const _canInvite = _activeRole === "owner" || _activeRole === "admin";
    try {
      let buildHookCurlHeaders2 = function(token) {
        const headers = [];
        if (token) headers.push(`-H 'Authorization: Bearer ${token}'`);
        headers.push(`-H 'Content-Type: application/json'`);
        return headers.join(" ");
      }, buildHookCurlCommand2 = function(path, token) {
        const cmd = `curl -s -X POST ${buildHookCurlHeaders2(token)} -d '{"project_id":"${projectId}"}' ${hooksBaseUrl}/hooks/${path}`;
        if (path === "session-start") return `${cmd} | jq -r '.hookSpecificOutput.additionalContext // empty'`;
        return cmd;
      }, buildHookPowerShellCommand2 = function(path, token) {
        const headerClause = token ? ` -Headers @{ Authorization = 'Bearer ${token}' }` : "";
        const bodyJson = `{"project_id":"${projectId}"}`;
        if (path === "session-start") {
          return `powershell -NoProfile -NonInteractive -Command "try { $r = Invoke-WebRequest -Method POST -Uri '${hooksBaseUrl}/hooks/session-start'${headerClause} -ContentType 'application/json' -Body '${bodyJson}' -UseBasicParsing; $r.Content } catch { '{}' }"`;
        }
        return `powershell -NoProfile -NonInteractive -Command "try { Invoke-WebRequest -Method POST -Uri '${hooksBaseUrl}/hooks/stop'${headerClause} -ContentType 'application/json' -Body '${bodyJson}' -UseBasicParsing | Out-Null } catch { }"`;
      }, buildClaudeHookSnippet2 = function(platform, token) {
        const start = platform === "windows" ? buildHookPowerShellCommand2("session-start", token) : buildHookCurlCommand2("session-start", token);
        const stop = platform === "windows" ? buildHookPowerShellCommand2("stop", token) : buildHookCurlCommand2("stop", token);
        return JSON.stringify({
          hooks: {
            SessionStart: [{ matcher: "", hooks: [{ type: "command", command: start }] }],
            Stop: [{ matcher: "", hooks: [{ type: "command", command: stop }] }]
          }
        }, null, 2);
      }, buildCodexHookSnippet2 = function(platform, token) {
        const start = platform === "windows" ? buildHookPowerShellCommand2("session-start", token) : buildHookCurlCommand2("session-start", token);
        const stop = platform === "windows" ? buildHookPowerShellCommand2("stop", token) : buildHookCurlCommand2("stop", token);
        return `[mcp_servers.meridian]
type = "http"
url = "${hooksBaseUrl}/mcp"

[hooks]
session_start = ${JSON.stringify(start)}
stop = ${JSON.stringify(stop)}`;
      };
      var buildHookCurlHeaders = buildHookCurlHeaders2, buildHookCurlCommand = buildHookCurlCommand2, buildHookPowerShellCommand = buildHookPowerShellCommand2, buildClaudeHookSnippet = buildClaudeHookSnippet2, buildCodexHookSnippet = buildCodexHookSnippet2;
      const [mcpResult, settingsResult, ghResult] = await Promise.allSettled([
        api("/settings/mcp-config"),
        loadProjectSettings(projectId),
        api(`/projects/${projectId}/github/status`)
      ]);
      const mcpData = mcpResult.status === "fulfilled" ? mcpResult.value : null;
      const projectSettings = settingsResult.status === "fulfilled" ? settingsResult.value : { project_id: projectId, max_pinned_decisions: DEFAULT_MAX_PINNED_DECISIONS };
      const ghData = ghResult.status === "fulfilled" ? ghResult.value : null;
      const ghRepos = Array.isArray(ghData?.repos) ? ghData.repos : [];
      const ghRepoMap = Object.fromEntries(ghRepos.map((repo) => [repo.full_name, repo]));
      const ghSelectedRepo = ghData?.repo || ghRepos[0]?.full_name || "";
      const ghSelectedBranch = ghData?.branch || ghRepoMap[ghSelectedRepo]?.default_branch || "main";
      const ghUsername = ghData?.github_user || ghData?.login || "";
      const ghAvatarUrl = ghData?.avatar_url || "";
      const ghConnections = Array.isArray(ghData?.connections) ? ghData.connections : [];
      const ghRepoChoices = ghRepos.length ? ghRepos : ghSelectedRepo ? [{
        full_name: ghSelectedRepo,
        name: ghSelectedRepo.split("/").slice(-1)[0] || ghSelectedRepo,
        owner: ghSelectedRepo.includes("/") ? ghSelectedRepo.split("/")[0] : ""
      }] : [];
      const repoGroups = /* @__PURE__ */ new Map();
      ghRepoChoices.forEach((repo) => {
        const fullName = repo.full_name || "";
        if (!fullName) return;
        const owner = repo.owner || (fullName.includes("/") ? fullName.split("/")[0] : "other");
        if (!repoGroups.has(owner)) repoGroups.set(owner, []);
        repoGroups.get(owner).push(repo);
      });
      const ghRepoOptions = Array.from(repoGroups.keys()).sort((a3, b2) => {
        const aPersonal = !!ghUsername && a3.toLowerCase() === ghUsername.toLowerCase();
        const bPersonal = !!ghUsername && b2.toLowerCase() === ghUsername.toLowerCase();
        if (aPersonal !== bPersonal) return aPersonal ? -1 : 1;
        const aMeridian = a3.toLowerCase() === "meridianmcp";
        const bMeridian = b2.toLowerCase() === "meridianmcp";
        if (aMeridian !== bMeridian) return aMeridian ? -1 : 1;
        return a3.localeCompare(b2);
      }).map((owner) => {
        const label = !!ghUsername && owner.toLowerCase() === ghUsername.toLowerCase() ? `Personal (@${owner})` : `${owner} repos`;
        const options = (repoGroups.get(owner) || []).slice().sort((a3, b2) => String(a3.name || a3.full_name || "").localeCompare(String(b2.name || b2.full_name || ""))).map((repo) => {
          const fullName = repo.full_name || "";
          const repoName = repo.name || (fullName.includes("/") ? fullName.split("/").slice(-1)[0] : fullName);
          return `<option value="${escapeHtml(fullName)}" ${fullName === ghSelectedRepo ? "selected" : ""}>${escapeHtml(repoName)}</option>`;
        }).join("");
        return `<optgroup label="${escapeHtml(label)}">${options}</optgroup>`;
      }).join("");
      const hooksBaseUrl = (mcpData && mcpData.base_url || window.location.origin || window.state.serverConfig?.server_url || "http://localhost:7878").replace(/\/$/, "");
      let _secState = { connect: true, executor: false, config: false, account: false };
      try {
        Object.assign(_secState, JSON.parse(localStorage.getItem("meridian.settings.sections." + projectId) || "{}"));
      } catch (e3) {
      }
      const _secHtml = function(k3, title) {
        const openAttr = _secState[k3] ? "open" : "";
        const caretRot = _secState[k3] ? "transform:rotate(90deg)" : "";
        return '<details id="settings-sec-' + k3 + "-" + projectId + '" ' + openAttr + ' style="margin-bottom:12px;border:1px solid var(--border);border-radius:6px"><summary style="cursor:pointer;list-style:none;padding:10px 12px;display:flex;align-items:center;gap:8px"><span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;' + caretRot + '">\u25B6</span><span style="font-weight:600;font-size:11px;color:var(--text)">' + title + '</span></summary><div style="padding:0 0 4px">';
      };
      const _allRepoPaths = [];
      if (!isHostedMode()) {
        try {
          const _allProjSettings = await Promise.allSettled(
            (window.state.projects || []).map((p3) => loadProjectSettings(p3.id))
          );
          for (const _ps of _allProjSettings) {
            if (_ps.status === "fulfilled") {
              const _rps = Array.isArray(_ps.value?.executor_config?.repo_paths) ? _ps.value.executor_config.repo_paths : [];
              for (const _rp of _rps) {
                if (_rp && !_allRepoPaths.includes(_rp)) _allRepoPaths.push(_rp);
              }
            }
          }
        } catch (_2) {
        }
      }
      let html = "";
      html += _settingsAccountCardHtml(projectId);
      let _connectPanelHtml = "";
      if (isHostedMode()) {
        const _advKey = `meridian.settings.adv.${projectId}`;
        let _advOpen = false;
        try {
          _advOpen = localStorage.getItem(_advKey) === "1";
        } catch (e3) {
        }
        _connectPanelHtml += `<details class="meridian-disclosure" style="margin-bottom:16px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <summary style="cursor:pointer;list-style:none;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;gap:8px">

      <span style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">

        <span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;flex-shrink:0">\u25B6</span>

        <span style="font-weight:600;font-size:11px;color:var(--text)">Meridian Connect</span>

      </span>

      <span style="font-size:10px;color:var(--muted);flex-shrink:0">Claude Code + Codex</span>

    </summary>

    <div style="padding:0 12px 12px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Install once per machine. Hooks auto-start sessions and sync context with Meridian.</div>

      ${osExecutorHintBanner(projectId)}

      <div style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin-bottom:10px">

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">macOS / Linux / WSL</div>

          <pre id="hooks-install-unix-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-unix-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Windows PowerShell</div>

          <pre id="hooks-install-windows-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-windows-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </div>

      <div style="font-size:10px;color:var(--muted);margin-top:6px">Need manual config? See <a href="https://docs.usemeridian.us/configuration" target="_blank" style="color:var(--accent);text-decoration:none">docs.usemeridian.us/configuration</a></div>

      ${_directBinaryDownloadsHtml()}

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;margin-top:10px">

        ${mcpData ? `<button id="hooks-gen-token-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Generate API key</button>` : ""}

        ${mcpData ? `<button id="hooks-gen-readonly-token-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px" title="Read-only tokens can only call get_* tools \u2014 safe for ChatGPT connectors">Generate read-only key</button>` : ""}

        <span id="hooks-token-status-${projectId}" style="font-size:10px;color:var(--muted)">${mcpData ? "Generate an API key to replace the placeholder token in the hosted snippets below." : "Local mode - no Bearer token needed."}</span>

      </div>

      <div id="hooks-key-reveal-${projectId}" style="display:none;margin-bottom:8px;padding:8px 10px;border:1px solid var(--accent);border-radius:4px;background:var(--surface-1)">
        <div style="font-size:10px;color:var(--accent);font-weight:600;margin-bottom:6px">Your new API key \u2014 save it now, it won't be shown again:</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input id="hooks-key-reveal-input-${projectId}" type="text" readonly style="flex:1;min-width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px">
          <button id="hooks-key-copy-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Copy key</button>
          <button id="hooks-key-dismiss-${projectId}" class="secondary" style="font-size:10px;padding:4px 8px" title="Dismiss">\xD7</button>
        </div>
      </div>

      ${mcpData ? `<div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">

          <span style="font-size:10px;font-weight:600;color:var(--text)">Existing API keys</span>

          <button id="hooks-refresh-tokens-${projectId}" class="secondary" style="font-size:10px;padding:3px 8px">Refresh</button>

        </div>

        <div id="hooks-token-list-${projectId}" style="display:grid;gap:6px"></div>

      </div>` : ""}

      <details style="margin-bottom:10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">Windows manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-win-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-win-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

      <details style="border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">macOS / Linux manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-unix-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-unix-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

    </div>

  </details>`;
      }
      {
        let _psOpen = true;
        try {
          _psOpen = localStorage.getItem("meridian.settings.ps." + projectId) !== "0";
        } catch (e3) {
        }
        const _psRot = _psOpen ? "transform:rotate(90deg)" : "";
        html += `<details id="settings-grp-ps-${projectId}" ${_psOpen ? "open" : ""} style="margin-bottom:12px;border:2px solid var(--border);border-radius:8px"><summary style="cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:8px;background:var(--surface-2);border-radius:8px"><span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;${_psRot}">\u25B6</span><span style="font-weight:700;font-size:11px;color:var(--text);letter-spacing:.04em">PROJECT SETTINGS</span></summary><div style="padding:8px 8px 4px">`;
      }
      html += _secHtml("connect", "Connect Claude Code");
      html += _settingsBrowserConnectorCardHtml(projectId);
      if (mcpData) {
        html += `<div style="margin-bottom:14px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)" id="github-card-${projectId}">

    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;flex-wrap:wrap">

      <div style="display:flex;align-items:center;gap:8px;min-width:0">

        ${githubIconSvg(14, "var(--text)")}

        <div style="min-width:0">

          <div style="font-weight:600;font-size:11px;color:var(--text)">Connect GitHub repo</div>

          <div style="font-size:10px;color:var(--muted)">Connect your account and pick the repo your sessions should read from.</div>

        </div>

      </div>

      ${ghData?.connected ? `

        <div style="display:flex;align-items:center;gap:6px">

          <button class="secondary" id="github-test-btn-${projectId}" style="padding:3px 8px;font-size:10px">Test</button>

          <button class="secondary" id="github-disconnect-btn-${projectId}" style="padding:3px 8px;font-size:10px;color:var(--danger,#ef4444)">Disconnect</button>

          <span id="github-status-${projectId}" style="font-size:10px;color:var(--muted)"></span>

        </div>

      ` : ""}

    </div>

    ${ghConnections.length ? `

      <div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border);border-radius:5px;background:var(--surface-1)">

        <div style="font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px">Connected accounts</div>

        <div style="display:flex;flex-direction:column;gap:5px">

          ${ghConnections.map(function(c3) {
          var login = c3 && c3.account_login || "";
          var active = !!ghUsername && login.toLowerCase() === ghUsername.toLowerCase();
          return `<div style="display:flex;align-items:center;gap:8px"><img src="https://github.com/${encodeURIComponent(login)}.png?size=32" alt="" style="width:18px;height:18px;border-radius:50%;border:1px solid var(--border);background:var(--surface-2)" onerror="this.style.visibility='hidden'"><span style="font-size:11px;color:var(--text);font-weight:600">@${escapeHtml(login)}</span>` + (active ? `<span style="font-size:8px;font-weight:700;color:#22c55e;border:1px solid #22c55e55;border-radius:3px;padding:1px 5px">ACTIVE</span>` : `<button class="secondary gh-acct-use" data-login="${escapeHtml(login)}" style="font-size:9px;padding:1px 7px">Use for this project</button>`) + `<span style="flex:1"></span><button class="secondary gh-acct-remove" data-login="${escapeHtml(login)}" style="font-size:9px;padding:1px 7px;color:var(--danger,#ef4444)">Disconnect</button></div>`;
        }).join("")}

        </div>

        <button class="secondary" id="github-add-account-${projectId}" style="margin-top:8px;font-size:10px;padding:3px 10px">+ Connect another GitHub account</button>

      </div>

    ` : ""}

    ${ghData?.connected ? `

      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">

        <img src="${escapeHtml(ghAvatarUrl || "https://github.com/github.png?size=48")}" alt="" style="width:26px;height:26px;border-radius:50%;object-fit:cover;border:1px solid var(--border);background:var(--surface-1)">

        <div style="min-width:0;flex:1">

          <div style="font-size:11px;font-weight:700;color:var(--text)">${ghUsername ? "@" + escapeHtml(ghUsername) : "GitHub connected"}</div>

          <div style="font-size:9px;color:var(--muted)">${ghRepos.length ? `${ghRepos.length} accessible repos` : "Fetching repo access\u2026"}</div>

        </div>

      </div>

      <div style="display:grid;grid-template-columns:minmax(220px,1.4fr) minmax(110px,0.6fr) auto;gap:8px;align-items:end">

        <label style="display:flex;flex-direction:column;gap:3px;min-width:0">

          <span style="font-size:9px;color:var(--muted)">Repo</span>

          <select id="github-repo-${projectId}"

            style="padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

            ${ghRepoOptions}

          </select>

          <span style="font-size:9px;color:var(--muted)">Grouped by owner so personal and org repos stay separate.</span>

        </label>

        <label style="display:flex;flex-direction:column;gap:3px;min-width:0">

          <span style="font-size:9px;color:var(--muted)">Branch</span>

          <select id="github-branch-${projectId}"

            style="padding:5px 8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

            <option value="${escapeHtml(ghSelectedBranch)}" selected>${escapeHtml(ghSelectedBranch)}</option>

          </select>

        </label>

        <button class="primary" id="github-save-btn-${projectId}" style="padding:5px 12px;font-size:11px">Save repo</button>

      </div>

    ` : `

      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">

        <div style="font-size:10px;color:var(--muted)">Use GitHub OAuth to connect once. Meridian stores an encrypted token and pulls your repo list automatically.</div>

        <button class="primary" id="github-connect-btn-${projectId}" style="padding:5px 12px;font-size:11px">Connect with GitHub</button>

      </div>

    `}

  </div>`;
      }
      {
        const _klCfg = projectSettings && projectSettings.executor_config || {};
        const _klPaths = Array.isArray(_klCfg.repo_paths) ? _klCfg.repo_paths : [];
        const _klHosts = Array.isArray(_klCfg.hostnames) ? _klCfg.hostnames : [];
        html += `<div style="margin-bottom:12px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--surface)">
      <div style="font-weight:600;font-size:13px;color:var(--text);margin-bottom:4px">Known Locations</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px">First hook from a new machine auto-registers it here. All future sessions from that machine route to this project regardless of directory.</div>
      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Registered Machines</div>
      <div id="exec-ez-hosts-tbl-${projectId}" style="margin-bottom:8px;font-size:10px;font-family:var(--font-mono)">${_klHosts.length ? '<table style="width:100%;border-collapse:collapse">' + _klHosts.map((h3, i3) => `<tr>
              <td style="padding:2px 6px 2px 0;color:var(--text)">${escapeHtml(h3.hostname || "")}</td>
              <td style="padding:2px 6px 2px 0;color:var(--muted)"><label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:9px"><input type="checkbox" class="exec-ez-host-autocwd" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" ${h3.auto_add_cwds ? "checked" : ""} style="cursor:pointer"> Auto-add new cwds</label></td>
              <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-host" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">Remove</button></td>
            </tr>`).join("") + "</table>" : '<div style="color:var(--muted);font-style:italic;font-size:10px">No machines registered yet \u2014 first hook auto-registers.</div>'}</div>
      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Specific Paths (cwd overrides)</div>
      <div id="exec-ez-paths-tbl-${projectId}" style="margin-bottom:8px;font-size:10px;font-family:var(--font-mono)">${_klPaths.length ? '<table style="width:100%;border-collapse:collapse">' + _klPaths.map((p3, i3) => `<tr>
              <td style="padding:2px 6px 2px 0;color:var(--text)">${escapeHtml(p3.hostname || "")}</td>
              <td style="padding:2px 6px 2px 0;color:var(--muted)">${escapeHtml(p3.cwd || "")}</td>
              <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-row" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">Remove</button></td>
            </tr>`).join("") + "</table>" : '<div style="color:var(--muted);font-style:italic;font-size:10px">No path overrides \u2014 machine-level routing handles most cases.</div>'}</div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input id="exec-ez-add-cwd-${projectId}" type="text" placeholder="cwd e.g. C:\\Users\\you\\project" style="flex:2;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">
        <input id="exec-ez-add-host-${projectId}" type="text" placeholder="hostname" list="exec-ez-host-options-${projectId}" value="${escapeHtml(String(_klHosts[0] && _klHosts[0].hostname || ""))}" style="flex:1;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">
        <button id="exec-ez-add-btn-${projectId}" class="secondary" style="font-size:10px;padding:3px 10px">Add</button>
      </div>
      <datalist id="exec-ez-host-options-${projectId}">${_klHosts.map((h3) => `<option value="${escapeHtml(String(h3.hostname || ""))}"></option>`).join("")}</datalist>
      <div style="display:flex;gap:8px;align-items:center">
        <button id="exec-ez-save-${projectId}" class="primary" style="font-size:10px;padding:3px 10px">Save</button>
        <button id="exec-ez-clear-${projectId}" class="secondary" style="font-size:10px;padding:3px 10px">Clear all</button>
        <span id="exec-ez-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px"></span>
      </div>
    </div>`;
      }
      if (!isHostedMode()) _connectPanelHtml += `<details class="meridian-disclosure" style="margin-bottom:16px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)">

    <summary style="cursor:pointer;list-style:none;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;gap:8px">

      <span style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">

        <span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;flex-shrink:0">\u25B6</span>

        <span style="font-weight:600;font-size:11px;color:var(--text)">Meridian Connect</span>

      </span>

      <span style="font-size:10px;color:var(--muted);flex-shrink:0">Claude Code + Codex</span>

    </summary>

    <div style="padding:0 12px 12px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Install once per machine. Hooks auto-start sessions and sync context with Meridian.</div>

      ${osExecutorHintBanner(projectId)}

      <div style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin-bottom:10px">

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">macOS / Linux / WSL</div>

          <pre id="hooks-install-unix-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-unix-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

        <div>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Windows PowerShell</div>

          <pre id="hooks-install-windows-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-install-windows-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </div>

      <div style="font-size:10px;color:var(--muted);margin-top:6px">Need manual config? See <a href="https://docs.usemeridian.us/configuration" target="_blank" style="color:var(--accent);text-decoration:none">docs.usemeridian.us/configuration</a></div>

      ${_directBinaryDownloadsHtml()}

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;margin-top:10px">

        ${mcpData ? `<button id="hooks-gen-token-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Generate API key</button>` : ""}

        ${mcpData ? `<button id="hooks-gen-readonly-token-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px" title="Read-only tokens can only call get_* tools \u2014 safe for ChatGPT connectors">Generate read-only key</button>` : ""}

        <span id="hooks-token-status-${projectId}" style="font-size:10px;color:var(--muted)">${mcpData ? "Generate an API key to replace the placeholder token in the hosted snippets below." : "Local mode - no Bearer token needed."}</span>

      </div>

      <div id="hooks-key-reveal-${projectId}" style="display:none;margin-bottom:8px;padding:8px 10px;border:1px solid var(--accent);border-radius:4px;background:var(--surface-1)">
        <div style="font-size:10px;color:var(--accent);font-weight:600;margin-bottom:6px">Your new API key \u2014 save it now, it won't be shown again:</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input id="hooks-key-reveal-input-${projectId}" type="text" readonly style="flex:1;min-width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px">
          <button id="hooks-key-copy-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Copy key</button>
          <button id="hooks-key-dismiss-${projectId}" class="secondary" style="font-size:10px;padding:4px 8px" title="Dismiss">\xD7</button>
        </div>
      </div>

      ${mcpData ? `<div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">

          <span style="font-size:10px;font-weight:600;color:var(--text)">Existing API keys</span>

          <button id="hooks-refresh-tokens-${projectId}" class="secondary" style="font-size:10px;padding:3px 8px">Refresh</button>

        </div>

        <div id="hooks-token-list-${projectId}" style="display:grid;gap:6px"></div>

      </div>` : ""}

      <details style="margin-bottom:10px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">Windows manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-win-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-win-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-win-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

      <details style="border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

        <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text)">macOS / Linux manual config</summary>

        <div style="padding:0 10px 10px">

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:8px 0 4px">Claude Code - <code>~/.claude/settings.json</code></div>

          <pre id="hooks-unix-claude-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-claude-${projectId}" style="font-size:10px;padding:4px 10px;margin-bottom:10px">Copy</button>

          <div style="font-size:10px;font-weight:600;color:var(--text);margin:0 0 4px">Codex - <code>~/.codex/config.toml</code></div>

          <pre id="hooks-unix-codex-${projectId}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

          <button class="secondary" id="hooks-copy-unix-codex-${projectId}" style="font-size:10px;padding:4px 10px">Copy</button>

        </div>

      </details>

    </div>

  </details>`;
      if (false) {
        const projects = mcpData.projects || [];
        const baseUrl = mcpData.base_url || "https://usemeridian.us";
        const firstPid = projects[0]?.id || "";
        const clients = [
          { id: "claude-desktop", label: "Claude Desktop", file: "~/.config/Claude/claude_desktop_config.json" },
          { id: "claude-code", label: "Claude Code", file: ".mcp.json (project root)" },
          { id: "cursor", label: "Cursor", file: "~/.cursor/mcp.json" }
        ];
        const projectOpts = projects.map(
          (p3) => `<option value="${escapeHtml(p3.id)}">${escapeHtml(p3.name)}</option>`
        ).join("");
        html += `<div style="margin-bottom:16px">

      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">

        <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase">MCP client setup</div>

        <a href="https://docs.usemeridian.us/browser-connector/" target="_blank" style="font-size:10px;color:var(--muted);text-decoration:none" title="Full setup guide">setup guide \u2192</a>

      </div>

      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">

        <div style="display:flex;gap:0;border:1px solid var(--border);border-radius:3px;overflow:hidden" id="mcp-client-tabs-${projectId}">

          ${clients.map((c3, i3) => `<button data-client="${c3.id}" style="background:${i3 === 0 ? "var(--accent)" : "var(--surface-1)"};color:${i3 === 0 ? "#000" : "var(--text)"};border:none;padding:3px 10px;font-size:10px;font-family:var(--font-mono);cursor:pointer;white-space:nowrap">${c3.label}</button>`).join("")}

        </div>

        ${projects.length > 1 ? `<select id="mcp-project-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">${projectOpts}</select>` : ""}

      </div>

      <pre id="mcp-config-block-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all">Generate an API key to see your config</pre>

      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">

        <button id="mcp-gen-token-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Generate API key</button>

        <button id="mcp-copy-btn-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px" disabled>Copy config</button>

        <span id="mcp-copy-status-${projectId}" style="font-size:10px;color:var(--muted)"></span>

      </div>

      <div id="mcp-key-reveal-${projectId}" style="display:none;margin-top:8px;padding:8px 10px;border:1px solid var(--accent);border-radius:4px;background:var(--surface-1)">
        <div style="font-size:10px;color:var(--accent);font-weight:600;margin-bottom:6px">Your new API key \u2014 save it now, it won't be shown again:</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input id="mcp-key-reveal-input-${projectId}" type="text" readonly style="flex:1;min-width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px">
          <button id="mcp-key-copy-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Copy key</button>
          <button id="mcp-key-dismiss-${projectId}" class="secondary" style="font-size:10px;padding:4px 8px" title="Dismiss">\xD7</button>
        </div>
      </div>

      <div id="mcp-file-note-${projectId}" style="font-size:10px;color:var(--muted);margin-top:6px"></div>

    </div>`;
        setTimeout(() => {
          let activeClient = "claude-desktop";
          let currentPid = firstPid;
          const configBlock = document.getElementById(`mcp-config-block-${projectId}`);
          const copyBtn = document.getElementById(`mcp-copy-btn-${projectId}`);
          const copyStatus = document.getElementById(`mcp-copy-status-${projectId}`);
          const fileNote = document.getElementById(`mcp-file-note-${projectId}`);
          const genBtn = document.getElementById(`mcp-gen-token-${projectId}`);
          const projectSel = document.getElementById(`mcp-project-${projectId}`);
          const tabsEl = document.getElementById(`mcp-client-tabs-${projectId}`);
          function buildConfig() {
            if (!currentToken) return null;
            return JSON.stringify({
              mcpServers: {
                meridian: {
                  command: "npx",
                  args: ["-y", "mcp-remote", `${baseUrl}/mcp`],
                  env: { BEARER_TOKEN: currentToken }
                }
              }
            }, null, 2);
          }
          function renderConfig() {
            const cli = clients.find((c3) => c3.id === activeClient) || clients[0];
            const cfg = buildConfig();
            if (cfg) {
              configBlock.textContent = cfg;
              copyBtn.disabled = false;
              fileNote.textContent = `Save to: ${cli.file}`;
            } else if (window.state.serverConfig?.demo_mode) {
              const demoKey = "sk_meridian_demo_" + "x".repeat(24);
              const demoCfg = JSON.stringify({
                mcpServers: {
                  meridian: {
                    command: "npx",
                    args: ["-y", "mcp-remote", `${baseUrl}/mcp`],
                    env: { BEARER_TOKEN: demoKey }
                  }
                }
              }, null, 2);
              configBlock.textContent = demoCfg;
              copyBtn.disabled = false;
              fileNote.textContent = `Demo key \u2014 sign up at ${baseUrl} for a real one`;
            } else {
              const placeholderKey = "sk_meridian_" + "x".repeat(32);
              const placeholderCfg = JSON.stringify({
                mcpServers: {
                  meridian: {
                    command: "npx",
                    args: ["-y", "mcp-remote", `${baseUrl}/mcp`],
                    env: { BEARER_TOKEN: placeholderKey }
                  }
                }
              }, null, 2);
              configBlock.textContent = placeholderCfg;
              copyBtn.disabled = false;
              fileNote.textContent = `Save to: ${cli.file}`;
              if (copyStatus) copyStatus.textContent = 'Click "Generate API key" to replace the placeholder with your real key.';
            }
          }
          if (tabsEl) {
            tabsEl.querySelectorAll("button[data-client]").forEach((btn) => {
              btn.onclick = () => {
                activeClient = btn.dataset.client;
                tabsEl.querySelectorAll("button[data-client]").forEach((b2) => {
                  b2.style.background = b2 === btn ? "var(--accent)" : "var(--surface-1)";
                  b2.style.color = b2 === btn ? "#000" : "var(--text)";
                });
                renderConfig();
              };
            });
          }
          if (projectSel) {
            projectSel.onchange = () => {
              currentPid = projectSel.value;
              renderConfig();
            };
          }
          if (genBtn) {
            genBtn.onclick = async () => {
              genBtn.disabled = true;
              genBtn.textContent = "Generating\u2026";
              try {
                const tok = await api("/auth/tokens", { method: "POST", body: JSON.stringify({ label: "mcp-config" }) });
                currentToken = tok.token;
                renderConfig();
                const pid = copyBtn?.id?.replace("mcp-copy-config-", "") || "";
                const hostedEl = document.getElementById(`hosted-mcp-json-${pid}`);
                if (hostedEl && currentToken) {
                  const refreshedJson = JSON.stringify({ mcpServers: { meridian: { command: "npx", args: ["-y", "mcp-remote", "https://usemeridian.us/mcp"], env: { BEARER_TOKEN: currentToken } } } }, null, 2);
                  hostedEl.textContent = refreshedJson;
                  const copyHostedBtn = document.getElementById(`copy-hosted-mcp-json-${pid}`);
                  if (copyHostedBtn) copyHostedBtn.onclick = async () => {
                    try {
                      await navigator.clipboard.writeText(refreshedJson);
                      copyHostedBtn.textContent = "Copied!";
                      setTimeout(() => {
                        copyHostedBtn.textContent = "Copy .mcp.json";
                      }, 1800);
                    } catch (e3) {
                    }
                  };
                }
                copyBtn.disabled = false;
                const mcpRevealEl = document.getElementById(`mcp-key-reveal-${projectId}`);
                const mcpRevealInput = document.getElementById(`mcp-key-reveal-input-${projectId}`);
                const mcpKeyCopyBtn = document.getElementById(`mcp-key-copy-btn-${projectId}`);
                const mcpKeyDismiss = document.getElementById(`mcp-key-dismiss-${projectId}`);
                if (mcpRevealEl && mcpRevealInput) {
                  let _hideMcpReveal2 = function() {
                    mcpRevealEl.style.display = "none";
                    if (copyStatus) copyStatus.textContent = "Key saved: " + ("sk_meridian_\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" + tok.token.slice(-4)) + tok.token.slice(-4);
                  };
                  var _hideMcpReveal = _hideMcpReveal2;
                  mcpRevealInput.value = tok.token;
                  mcpRevealEl.style.display = "";
                  if (mcpKeyCopyBtn) mcpKeyCopyBtn.onclick = async () => {
                    try {
                      await navigator.clipboard.writeText(tok.token);
                      mcpKeyCopyBtn.textContent = "Copied!";
                      setTimeout(() => {
                        mcpKeyCopyBtn.textContent = "Copy key";
                      }, 1800);
                    } catch (e3) {
                    }
                  };
                  if (mcpKeyDismiss) mcpKeyDismiss.onclick = _hideMcpReveal2;
                  setTimeout(_hideMcpReveal2, 3e4);
                } else if (copyStatus) {
                  copyStatus.textContent = "Real key generated \u2014 save it, it won't be shown again.";
                }
              } catch (e3) {
                if (copyStatus) copyStatus.textContent = `error: ${escapeHtml(String(e3))}`;
              } finally {
                genBtn.disabled = false;
                genBtn.textContent = "Generate new key";
              }
            };
          }
          if (copyBtn) {
            copyBtn.onclick = async () => {
              const cfg = buildConfig();
              if (!cfg) return;
              try {
                await navigator.clipboard.writeText(cfg);
                copyBtn.textContent = "Copied!";
                setTimeout(() => {
                  copyBtn.textContent = "Copy config";
                }, 1800);
              } catch (e3) {
                copyStatus.textContent = "Copy failed \u2014 select and copy manually";
              }
            };
          }
        }, 0);
      }
      {
        const serverUrl = (window.state.serverConfig?.server_url || "http://localhost:7878").replace(/\/$/, "");
        const mcpHttpUrl = `${serverUrl}/mcp`;
        const isHosted = window.location.hostname === "usemeridian.us";
        const rawTomlPath = window.state.serverConfig?.toml_path || "";
        const cwd = rawTomlPath ? rawTomlPath.replace(/[/\\]meridian\.toml$/i, "").replace(/\\/g, "/") : isHosted ? "" : "/path/to/your/meridian";
        const isDemo2 = !!window.state.serverConfig?.demo_mode;
        const displayPid = isDemo2 ? "your-project-id" : projectId;
        const stdioText = `[mcp_servers.meridian]
type = "stdio"
command = "pixi"
args = ["run", "python", "-m", "meridian", "--mcp"]
cwd = "${cwd.replace(/"/g, '\\"')}"`;
        const httpText = `[mcp_servers.meridian]
type = "http"
url = "${mcpHttpUrl}"`;
        const _gCfg = projectSettings && projectSettings.executor_config || {};
        const _gAutoText = `/goal Complete pending sprint items in order. Done when all items
marked complete via complete_sprint_item(), ${_gCfg.test_cmd || "pixi run test"} passes${_gCfg.test_min != null ? "\n" + _gCfg.test_min + "+," : ","} generate_handoff() called. Stop after 40 turns or HITL.

project_id = "${displayPid}"`;
        const goalText = _gCfg.goal_template || _gAutoText;
        const _mcpUrl = (window.state.serverConfig?.base_url || "https://usemeridian.us") + "/mcp";
        const hostedMcpJson = JSON.stringify({
          mcpServers: {
            meridian: {
              type: "http",
              url: _mcpUrl,
              headers: { Authorization: "Bearer sk_meridian_YOUR_KEY_HERE" }
            }
          }
        }, null, 2);
        html += `<details style="margin-top:12px;border:1px solid var(--border);border-radius:6px;overflow:hidden">
      <summary style="cursor:pointer;padding:8px 10px;font-size:10px;font-weight:600;color:var(--text);background:var(--surface-2);list-style:none;display:flex;align-items:center;gap:6px;user-select:none">
        <span style="font-size:12px">\u26A1</span> Install rc watcher <span style="color:var(--muted);font-weight:400;margin-left:4px">(for <code>claude --rc</code> server mode)</span>
      </summary>
      <div style="padding:10px 12px;font-size:10px;color:var(--muted);line-height:1.8">
        <p style="margin:0 0 8px">${claudeRcWatcherBlurb()}</p>
        <div style="margin-bottom:6px;font-size:10px;color:var(--text);font-weight:600">Windows</div>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:10px">
          <code id="rc-watcher-win-cmd-${escapeHtml(projectId)}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;word-break:break-all">irm ${escapeHtml(hooksBaseUrl)}/install_watcher.ps1 | iex</code>
          <button onclick="navigator.clipboard.writeText(document.getElementById('rc-watcher-win-cmd-${escapeHtml(projectId)}').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)}).catch(()=>{})" style="padding:3px 8px;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;cursor:pointer;white-space:nowrap;color:var(--text)">Copy</button>
        </div>
        <div style="margin-bottom:6px;font-size:10px;color:var(--text);font-weight:600">macOS / Linux</div>
        <div style="display:flex;gap:6px;align-items:center">
          <code id="rc-watcher-unix-cmd-${escapeHtml(projectId)}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;word-break:break-all">curl -fsSL ${escapeHtml(hooksBaseUrl)}/install_watcher.sh | bash</code>
          <button onclick="navigator.clipboard.writeText(document.getElementById('rc-watcher-unix-cmd-${escapeHtml(projectId)}').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)}).catch(()=>{})" style="padding:3px 8px;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;cursor:pointer;white-space:nowrap;color:var(--text)">Copy</button>
        </div>
      </div>
    </details>`;
        html += "</div></details>";
        html += _secHtml("executor", "Executor Setup");
        html += `<div style="margin-bottom:16px">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Codex CLI setup</div>

      ${isHostedMode() ? "" : `
      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">${codexSetupBlurb(mcpHttpUrl, escapeHtml)}</div>

      ${!isHosted ? `<div style="margin-bottom:12px">
        <label style="font-size:10px;color:var(--muted)">Your Meridian path<br>
          <input type="text" id="meridian-path-${escapeHtml(projectId)}" placeholder="/path/to/Meridian" value="${escapeHtml(rawTomlPath ? cwd : "")}" style="width:100%;max-width:400px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 8px;margin-top:3px;box-sizing:border-box">
        </label>
        <div style="font-size:9px;color:var(--muted);margin-top:3px">Updates the STDIO cwd below in real time.</div>
      </div>` : ""}

      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Option A \u2014 STDIO (local, recommended)</div>

      <pre id="codex-stdio-${escapeHtml(projectId)}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

      <button class="secondary" id="codex-copy-stdio-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px;margin-bottom:12px">Copy</button>

      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px;margin-top:8px">Option B \u2014 HTTP (when Meridian server is running)</div>

      <pre id="codex-http-${escapeHtml(projectId)}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>

      <button class="secondary" id="codex-copy-http-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px;margin-bottom:12px">Copy</button>
      `}

      <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px;margin-top:8px">/goal template</div>

      <textarea id="codex-goal-${escapeHtml(projectId)}" rows="6" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);resize:vertical;margin:0 0 4px 0;white-space:pre;box-sizing:border-box"></textarea>

      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

        <button class="secondary" id="codex-copy-goal-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Copy</button>

        <button class="primary" id="codex-save-goal-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Save</button>

        <button class="secondary" id="codex-regen-goal-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Regenerate</button>

        <span id="codex-goal-status-${escapeHtml(projectId)}" style="font-size:10px;color:var(--muted)"></span>

      </div>

      ${isHostedMode() ? `<div style="margin-top:12px;font-size:10px;color:var(--muted)">Need manual config? See <a href="https://docs.usemeridian.us/configuration" target="_blank" style="color:var(--accent);text-decoration:none">docs.usemeridian.us/configuration</a></div>

      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;overflow:hidden">
        <summary style="cursor:pointer;list-style:none;padding:6px 10px;background:var(--surface-2);font-size:10px;color:var(--muted)">Advanced \u2014 HTTP config (Codex / custom)</summary>
        <div style="padding:10px 12px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px">${codexSetupBlurb(mcpHttpUrl, escapeHtml)}</div>
          <pre id="codex-http-${escapeHtml(projectId)}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;font-size:10px;font-family:var(--font-mono);color:var(--text);overflow-x:auto;margin:0 0 6px 0;white-space:pre-wrap;word-break:break-all"></pre>
          <button class="secondary" id="codex-copy-http-${escapeHtml(projectId)}" style="font-size:10px;padding:4px 10px">Copy</button>
        </div>
      </details>` : ""}

    </div>`;
        setTimeout(() => {
          const stdioEl = document.getElementById(`codex-stdio-${projectId}`);
          const httpEl = document.getElementById(`codex-http-${projectId}`);
          const goalEl = document.getElementById(`codex-goal-${projectId}`);
          if (stdioEl) stdioEl.textContent = stdioText;
          if (httpEl) httpEl.textContent = httpText;
          if (goalEl) goalEl.value = goalText;
          function _codexCopySetup(btnId, text) {
            const btn = document.getElementById(btnId);
            if (!btn) return;
            btn.onclick = async () => {
              try {
                await navigator.clipboard.writeText(text);
                btn.textContent = "Copied!";
                setTimeout(() => {
                  btn.textContent = "Copy";
                }, 1800);
              } catch (e3) {
                btn.textContent = "Select and copy manually";
              }
            };
          }
          _codexCopySetup(`codex-copy-stdio-${projectId}`, stdioText);
          _codexCopySetup(`codex-copy-http-${projectId}`, httpText);
          const copyGoalBtn = document.getElementById(`codex-copy-goal-${projectId}`);
          if (copyGoalBtn && goalEl) {
            copyGoalBtn.onclick = async () => {
              try {
                await navigator.clipboard.writeText(goalEl.value);
                copyGoalBtn.textContent = "Copied!";
                setTimeout(() => {
                  copyGoalBtn.textContent = "Copy";
                }, 1800);
              } catch (e3) {
                copyGoalBtn.textContent = "Select and copy manually";
              }
            };
          }
          const saveGoalBtn = document.getElementById(`codex-save-goal-${projectId}`);
          const goalStatusEl = document.getElementById(`codex-goal-status-${projectId}`);
          if (saveGoalBtn && goalEl) {
            saveGoalBtn.onclick = async () => {
              saveGoalBtn.disabled = true;
              try {
                const curCfg = projectSettings && projectSettings.executor_config || {};
                await saveProjectSettings(projectId, { executor_config: { ...curCfg, goal_template: goalEl.value } });
                if (goalStatusEl) {
                  goalStatusEl.textContent = "Saved.";
                  setTimeout(() => {
                    if (goalStatusEl) goalStatusEl.textContent = "";
                  }, 2e3);
                }
              } catch (e3) {
                if (goalStatusEl) goalStatusEl.textContent = `Failed: ${String(e3)}`;
              } finally {
                saveGoalBtn.disabled = false;
              }
            };
          }
          const regenGoalBtn = document.getElementById(`codex-regen-goal-${projectId}`);
          if (regenGoalBtn && goalEl) {
            regenGoalBtn.onclick = async () => {
              goalEl.value = _gAutoText;
              regenGoalBtn.disabled = true;
              try {
                const curCfg = projectSettings && projectSettings.executor_config || {};
                const { goal_template: _gt, ...restCfg } = curCfg;
                await saveProjectSettings(projectId, { executor_config: restCfg });
                if (goalStatusEl) {
                  goalStatusEl.textContent = "Regenerated.";
                  setTimeout(() => {
                    if (goalStatusEl) goalStatusEl.textContent = "";
                  }, 2e3);
                }
              } catch (e3) {
                if (goalStatusEl) goalStatusEl.textContent = `Failed: ${String(e3)}`;
              } finally {
                regenGoalBtn.disabled = false;
              }
            };
          }
          const pushBtn = document.getElementById(`push-mcp-template-${projectId}`);
          if (pushBtn) {
            pushBtn.onclick = async () => {
              pushBtn.disabled = true;
              pushBtn.textContent = "Pushing\u2026";
              try {
                await api(`/projects/${projectId}/github/push-mcp-template`, { method: "POST" });
                pushBtn.textContent = "\u2713 Pushed!";
                pushBtn.style.color = "#059669";
              } catch (e3) {
                const msg = String(e3);
                if (msg.includes("409")) {
                  pushBtn.textContent = "Already exists";
                } else {
                  pushBtn.textContent = "Failed: " + msg.slice(0, 40);
                }
                pushBtn.disabled = false;
              }
            };
          }
          const hostedMcpEl = document.getElementById(`hosted-mcp-json-${projectId}`);
          if (hostedMcpEl) hostedMcpEl.textContent = hostedMcpJson;
          _codexCopySetup(`copy-hosted-mcp-json-${projectId}`, hostedMcpJson);
          const pathInput = document.getElementById(`meridian-path-${projectId}`);
          if (pathInput) {
            pathInput.addEventListener("input", function() {
              const newCwd = pathInput.value.trim() || "/path/to/your/meridian";
              const newStdio = '[mcp_servers.meridian]\ntype = "stdio"\ncommand = "pixi"\nargs = ["run", "python", "-m", "meridian", "--mcp"]\ncwd = "' + newCwd.replace(/"/g, '\\"') + '"';
              if (stdioEl) stdioEl.textContent = newStdio;
              _codexCopySetup(`codex-copy-stdio-${projectId}`, newStdio);
            });
          }
        }, 0);
      }
      html += "</div></details>";
      html += _secHtml("config", "Project Config");
      html += `<div style="margin-bottom:16px">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Constitution</div>

    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">

      <label style="font-size:10px;color:var(--muted)">

        Max pinned decisions warning threshold<br>

        <input type="number" id="constitution-max-${projectId}" min="1" max="500" step="1" inputmode="numeric" style="margin-top:4px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;width:80px">

      </label>

      <span id="constitution-max-status-${projectId}" style="font-size:10px;color:var(--muted)">Warning updates the Decisions tab banner and archive suggestion.</span>

    </div>

  </div>`;
      setTimeout(() => {
        const sel = document.getElementById(`constitution-max-${projectId}`);
        const status = document.getElementById(`constitution-max-status-${projectId}`);
        if (!sel) return;
        sel.value = String(projectSettings.max_pinned_decisions || DEFAULT_MAX_PINNED_DECISIONS);
        const commit = async () => {
          const raw = parseInt(String(sel.value || ""), 10);
          const nextLimit = Number.isFinite(raw) ? Math.min(500, Math.max(1, raw)) : DEFAULT_MAX_PINNED_DECISIONS;
          sel.value = String(nextLimit);
          sel.disabled = true;
          try {
            const saved = await saveProjectSettings(projectId, { max_pinned_decisions: nextLimit });
            sel.value = String(saved.max_pinned_decisions || DEFAULT_MAX_PINNED_DECISIONS);
            if (status) status.textContent = `Warning threshold saved at ${saved.max_pinned_decisions}.`;
            renderConstitutionWarning(projectId);
          } catch (e3) {
            if (status) status.textContent = `Save failed: ${String(e3)}`;
          } finally {
            sel.disabled = false;
          }
        };
        sel.addEventListener("change", commit);
        sel.addEventListener("blur", commit);
        sel.addEventListener("keydown", (e3) => {
          if (e3.key === "Enter") {
            e3.preventDefault();
            sel.blur();
          }
        });
      }, 0);
      const hitlMode = Math.max(0, Math.min(2, parseInt(projectSettings && projectSettings.hitl_auto_answer || 0, 10) || 0));
      const _hitlDesc = {
        0: "Off \u2014 every request waits for a human.",
        1: "Safe \u2014 auto-answers routine executor questions only; corrections, file-diff approvals, location mismatches, and anything destructive still wait for you.",
        2: "Aggressive \u2014 auto-answers everything except corrections and security-sensitive requests."
      };
      html += `<div style="margin-bottom:16px">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Human-in-the-loop</div>

    <label style="display:block;font-size:11px;color:var(--text);margin-bottom:6px">Auto-answer HITL requests</label>

    <select id="hitl-auto-${projectId}" style="width:100%;padding:6px 8px;font-size:11px;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;cursor:pointer">

      <option value="0" ${hitlMode === 0 ? "selected" : ""}>Off</option>

      <option value="1" ${hitlMode === 1 ? "selected" : ""}>Safe</option>

      <option value="2" ${hitlMode === 2 ? "selected" : ""}>Aggressive</option>

    </select>

    <div id="hitl-auto-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:6px;min-height:13px">${_hitlDesc[hitlMode]}</div>

  </div>`;
      setTimeout(() => {
        const sel = document.getElementById(`hitl-auto-${projectId}`);
        const status = document.getElementById(`hitl-auto-status-${projectId}`);
        if (!sel) return;
        sel.onchange = async () => {
          const prev = sel.value;
          sel.disabled = true;
          try {
            const mode = Math.max(0, Math.min(2, parseInt(sel.value, 10) || 0));
            const saved = await saveProjectSettings(projectId, { hitl_auto_answer: mode });
            const m3 = Math.max(0, Math.min(2, parseInt(saved && saved.hitl_auto_answer || 0, 10) || 0));
            sel.value = String(m3);
            if (status) status.textContent = _hitlDesc[m3];
          } catch (e3) {
            sel.value = prev;
            if (status) status.textContent = `Save failed: ${String(e3)}`;
          } finally {
            sel.disabled = false;
          }
        };
      }, 0);
      const executionMode = projectSettings && projectSettings.execution_mode === "interactive" ? "interactive" : "autonomous";
      html += `<div style="margin-bottom:16px">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Execution Mode</div>

    <div style="font-size:10px;color:var(--muted);margin-bottom:10px">How a new session behaves when it starts. Injected into <code>start_session</code> at the protocol level.</div>

    <label style="display:block;font-size:11px;color:var(--text);margin-bottom:4px">Executor posture</label>
    <div style="font-size:10px;color:var(--muted);margin-bottom:6px"><strong>Autonomous</strong> claims and runs pending sprint items immediately without asking. <strong>Interactive</strong> reviews the items and asks which to start first.</div>
    <select id="execution-mode-${projectId}" style="width:100%;padding:6px 8px;font-size:11px;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;cursor:pointer">
      <option value="autonomous" ${executionMode === "autonomous" ? "selected" : ""}>Autonomous (claim &amp; run, do not defer)</option>
      <option value="interactive" ${executionMode === "interactive" ? "selected" : ""}>Interactive (ask for direction first)</option>
    </select>
    <div id="execution-mode-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:6px;min-height:13px"></div>

  </div>`;
      setTimeout(() => {
        const emSel = document.getElementById(`execution-mode-${projectId}`);
        const emStatus = document.getElementById(`execution-mode-status-${projectId}`);
        if (emSel) emSel.onchange = async () => {
          if (emStatus) emStatus.textContent = "Saving\u2026";
          try {
            await saveProjectSettings(projectId, { execution_mode: emSel.value });
            if (emStatus) emStatus.textContent = "Saved.";
            setTimeout(() => {
              if (emStatus) emStatus.textContent = "";
            }, 1500);
          } catch (e3) {
            if (emStatus) emStatus.textContent = `Save failed: ${String(e3)}`;
          }
        };
      }, 0);
      const autoWorktrees = parseInt((projectSettings && projectSettings.auto_worktrees) != null ? projectSettings.auto_worktrees : 1, 10);
      const requireMergeApproval = parseInt((projectSettings && projectSettings.require_merge_approval) != null ? projectSettings.require_merge_approval : 1, 10);
      html += `<div style="margin-bottom:16px">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Parallel Safety</div>

    <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Controls how executors coordinate when multiple sessions work in parallel.</div>

    <label style="display:block;font-size:11px;color:var(--text);margin-bottom:4px">Suggest git worktree on claim</label>
    <div style="font-size:10px;color:var(--muted);margin-bottom:6px">When ON, <code>claim_sprint_item</code> returns worktree setup commands so each session works in isolation.</div>
    <select id="auto-worktrees-${projectId}" style="width:100%;padding:6px 8px;font-size:11px;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;cursor:pointer;margin-bottom:12px">
      <option value="1" ${autoWorktrees === 1 ? "selected" : ""}>On (recommended)</option>
      <option value="0" ${autoWorktrees === 0 ? "selected" : ""}>Off</option>
    </select>

    <label style="display:block;font-size:11px;color:var(--text);margin-bottom:4px">Require merge approval</label>
    <div style="font-size:10px;color:var(--muted);margin-bottom:6px">When ON, completing a sprint item with an active worktree files a HITL correction reminding the executor to merge before removing the worktree.</div>
    <select id="require-merge-${projectId}" style="width:100%;padding:6px 8px;font-size:11px;background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:5px;cursor:pointer">
      <option value="1" ${requireMergeApproval === 1 ? "selected" : ""}>On (recommended)</option>
      <option value="0" ${requireMergeApproval === 0 ? "selected" : ""}>Off</option>
    </select>
    <div id="parallel-safety-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:6px;min-height:13px"></div>

  </div>`;
      setTimeout(() => {
        const awSel = document.getElementById(`auto-worktrees-${projectId}`);
        const rmSel = document.getElementById(`require-merge-${projectId}`);
        const psStatus = document.getElementById(`parallel-safety-status-${projectId}`);
        const saveSetting = async (patch) => {
          if (psStatus) psStatus.textContent = "Saving\u2026";
          try {
            await saveProjectSettings(projectId, patch);
            if (psStatus) psStatus.textContent = "Saved.";
            setTimeout(() => {
              if (psStatus) psStatus.textContent = "";
            }, 1500);
          } catch (e3) {
            if (psStatus) psStatus.textContent = `Save failed: ${String(e3)}`;
          }
        };
        if (awSel) awSel.onchange = () => saveSetting({ auto_worktrees: parseInt(awSel.value, 10) });
        if (rmSel) rmSel.onchange = () => saveSetting({ require_merge_approval: parseInt(rmSel.value, 10) });
      }, 0);
      const execCfg = projectSettings && projectSettings.executor_config || {};
      html += `<div style="margin-bottom:16px" id="executor-config-section-${projectId}">

    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Executor Config</div>

    <div style="font-size:10px;color:var(--muted);margin-bottom:8px">Per-project defaults injected into executor sessions via <code>start_session(role="executor")</code>. Set once; all executors inherit automatically.</div>

    <div style="margin-bottom:10px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:4px">repo_paths<br><span style="font-size:9px;color:var(--muted)">Auto-tracked by hooks. Delete rows to remove locations.</span></div>

      <div id="exec-repo-paths-tbl-${projectId}" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px">
        ${(() => {
        const rps = Array.isArray(execCfg.repo_paths) ? execCfg.repo_paths : [];
        if (!rps.length) return '<div style="color:var(--muted);font-style:italic">No locations tracked yet.</div>';
        return '<table style="width:100%;border-collapse:collapse">' + rps.map((p3, i3) => `<tr>
              <td style="padding:2px 6px 2px 0;color:var(--text)">${escapeHtml(p3.hostname || "")}</td>
              <td style="padding:2px 6px 2px 0;color:var(--muted)">${escapeHtml(p3.cwd || "")}</td>
              <td style="padding:2px 0;text-align:right"><button class="exec-del-rp-row" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">\u2715</button></td>
            </tr>`).join("") + "</table>";
      })()}
      </div>

      <button id="exec-clear-paths-${projectId}" class="secondary" style="font-size:9px;padding:2px 8px">Clear all</button>

    </div>

    <div style="margin-bottom:10px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:4px">Filesystem Roots<br><span style="font-size:9px;color:var(--muted)">Directories the tunnel filesystem connector can read. Default: your home directory.</span></div>

      <div id="exec-fs-roots-tbl-${projectId}" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"></div>

      <div style="display:flex;gap:6px">

        <input id="exec-fs-roots-input-${projectId}" type="text" placeholder="e.g. C:\\Users\\you\\Documents" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">

        <button id="exec-fs-roots-add-${projectId}" class="secondary" style="font-size:9px;padding:2px 10px">Add</button>

      </div>

      <div id="exec-fs-roots-suggest-${projectId}" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px"></div>

    </div>

    <!-- b970fe07 \u2014 Serena default repo path (code-extractor slot). Single value;
         used at tunnel start only when --repo is not passed on the CLI. -->
    <div style="margin-bottom:10px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:4px">Serena Repo Path<br><span style="font-size:9px;color:var(--muted)">Default <code>--project</code> for the tunnel's code-extractor (Serena). Used only when <code>--repo</code> is not passed at tunnel start.</span></div>

      <input id="exec-serena_repo_path-${projectId}" type="text" placeholder="e.g. C:\\Users\\you\\repo" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px" value="${escapeHtml(String(execCfg.serena_repo_path || ""))}">

    </div>

    <!-- 3248f35d \u2014 auto-prospecting (handoff code-pointer enrichment) toggle. ON
         by default (matches _code_pointers_enabled: absent/unset => True). -->
    <div style="margin-bottom:10px">

      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:10px;color:var(--muted)"><input type="checkbox" id="exec-enrich_prospect-${projectId}" ${execCfg.enrich_handoffs_with_code_pointers !== false ? "checked" : ""} style="cursor:pointer"> Auto-prospect code pointers on handoff <span style="font-size:9px">(attaches file/symbol pointers to sprint items; ON by default)</span></label>

    </div>

    <!-- b970fe07 \u2014 code-intel auto-index dirs (code slot). Add/remove list,
         mirroring Filesystem Roots. Used only when --code-dir is not passed. -->
    <div style="margin-bottom:10px">

      <div style="font-size:10px;color:var(--muted);margin-bottom:4px">Code-Intel Index Dirs<br><span style="font-size:9px;color:var(--muted)">Directories codebase-memory-mcp auto-indexes at tunnel start. Used only when <code>--code-dir</code> is not passed.</span></div>

      <div id="exec-code-dirs-tbl-${projectId}" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"></div>

      <div style="display:flex;gap:6px">

        <input id="exec-code-dirs-input-${projectId}" type="text" placeholder="e.g. C:\\Users\\you\\repo" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">

        <button id="exec-code-dirs-add-${projectId}" class="secondary" style="font-size:9px;padding:2px 10px">Add</button>

      </div>

    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 12px">

      <label style="font-size:10px;color:var(--muted)">env_file<br><input id="exec-env_file-${projectId}" type="text" placeholder=".env file path" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.env_file || ""))}"></label>

      <label style="font-size:10px;color:var(--muted)">test_cmd<br><input id="exec-test_cmd-${projectId}" type="text" placeholder="pixi run test" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.test_cmd || ""))}"></label>

      <label style="font-size:10px;color:var(--muted)">test_min<br><input id="exec-test_min-${projectId}" type="number" placeholder="Min passing tests" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.test_min != null ? execCfg.test_min : ""))}"></label>

      <label style="font-size:10px;color:var(--muted)">deploy_cmd<br><input id="exec-deploy_cmd-${projectId}" type="text" placeholder="git push / fly deploy" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.deploy_cmd || ""))}"></label>

      <label style="font-size:10px;color:var(--muted)">branch<br><input id="exec-branch-${projectId}" type="text" placeholder="dev" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px" value="${escapeHtml(String(execCfg.branch || ""))}"></label>

    </div>

    <label style="display:block;font-size:10px;color:var(--muted);margin-top:10px">

      Checkpoint after <span style="color:var(--text)">N</span> turns <span style="font-size:9px;color:var(--muted)">(10\u2013200)</span>

      ${_execTurnsNumberInputHtml("context_threshold", projectId, execCfg.context_threshold || DEFAULT_CONTEXT_THRESHOLD, 10, 200, 5)}

      <span style="font-size:9px;color:var(--muted)">When a session passes this many turns, <code>get_context_block</code> nudges it to checkpoint.</span>

    </label>

    <label style="display:block;font-size:10px;color:var(--muted);margin-top:10px">

      Stop after <span id="exec-max_turns-val-${projectId}" style="color:var(--text);font-family:var(--font-mono)">${escapeHtml(String(execCfg.max_turns || DEFAULT_MAX_TURNS))}</span> turns <span style="font-size:9px;color:var(--muted)">(40\u2013500)</span>

      ${_execTurnsNumberInputHtml("max_turns", projectId, execCfg.max_turns || DEFAULT_MAX_TURNS, 40, 500, 20)}

      <span id="exec-max_turns-warn-${projectId}" style="font-size:9px;color:var(--muted)"></span>

    </label>

    <label style="display:block;font-size:10px;color:var(--muted);margin-top:10px">

      Auto-continue (<code>/loop</code>)

      <select id="exec-loop_enabled-${projectId}" style="width:100%;max-width:320px;margin-top:4px;display:block;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px">

        <option value="workspace"${execCfg.loop_enabled !== true && execCfg.loop_enabled !== false ? " selected" : ""}>Use workspace default</option>

        <option value="true"${execCfg.loop_enabled === true ? " selected" : ""}>Always on</option>

        <option value="false"${execCfg.loop_enabled === false ? " selected" : ""}>Always off</option>

      </select>

      <span style="font-size:9px;color:var(--muted)">Prepends <code>/loop</code> to the session /goal so it auto-continues after each response.</span>

    </label>

    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">

      <button id="exec-save-${projectId}" class="primary" style="font-size:10px;padding:3px 10px">Save</button>

      <span id="exec-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px"></span>

    </div>

  </div>`;
      setTimeout(() => {
        const saveBtn = document.getElementById(`exec-save-${projectId}`);
        const statusEl = document.getElementById(`exec-status-${projectId}`);
        if (!saveBtn) return;
        const ctxSlider = document.getElementById(`exec-context_threshold-${projectId}`);
        const ctxVal = document.getElementById(`exec-context_threshold-val-${projectId}`);
        if (ctxSlider && ctxVal) {
          ctxSlider.addEventListener("input", () => {
            ctxVal.textContent = ctxSlider.value;
          });
        }
        const mtSlider = document.getElementById(`exec-max_turns-${projectId}`);
        const mtVal = document.getElementById(`exec-max_turns-val-${projectId}`);
        const mtWarn = document.getElementById(`exec-max_turns-warn-${projectId}`);
        const _renderMtWarn = (v3) => {
          const n2 = parseInt(v3 || "", 10);
          const band = n2 >= 350 ? "var(--danger, #e5534b)" : n2 >= 200 ? "var(--warning, #d29922)" : "var(--success, #3fb950)";
          if (mtVal) mtVal.style.color = band;
          if (!mtWarn) return;
          if (n2 >= 350) {
            mtWarn.textContent = "\u26A0 Very long sprint (350+ turns) \u2014 high context/token cost; checkpoint frequently and expect drift.";
            mtWarn.style.color = band;
          } else if (n2 >= 200) {
            mtWarn.textContent = "\u26A0 Long sprint (200+ turns) \u2014 set a checkpoint cadence so context does not overflow mid-run.";
            mtWarn.style.color = band;
          } else {
            mtWarn.textContent = "Turns before the /goal auto-stops. Raise for megasprints (max 500).";
            mtWarn.style.color = "var(--muted)";
          }
        };
        if (mtSlider && mtVal) {
          mtVal.textContent = mtSlider.value;
          _renderMtWarn(mtSlider.value);
          mtSlider.addEventListener("input", () => {
            mtVal.textContent = mtSlider.value;
            _renderMtWarn(mtSlider.value);
          });
        }
        let _execRepoPaths = Array.isArray(execCfg.repo_paths) ? [...execCfg.repo_paths] : [];
        const _rpTblEl = document.getElementById(`exec-repo-paths-tbl-${projectId}`);
        const _rerenderRpTbl = () => {
          if (!_rpTblEl) return;
          if (!_execRepoPaths.length) {
            _rpTblEl.innerHTML = '<div style="color:var(--muted);font-style:italic;font-size:10px">No locations tracked yet.</div>';
          } else {
            _rpTblEl.innerHTML = '<table style="width:100%;border-collapse:collapse">' + _execRepoPaths.map((p3, i3) => `<tr>
            <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p3.hostname || "")}</td>
            <td style="padding:2px 6px 2px 0;color:var(--muted);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p3.cwd || "")}</td>
            <td style="padding:2px 0;text-align:right"><button class="exec-del-rp-row" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">\u2715</button></td>
          </tr>`).join("") + "</table>";
            _rpTblEl.querySelectorAll(".exec-del-rp-row").forEach((b2) => {
              b2.onclick = () => {
                _execRepoPaths.splice(parseInt(b2.dataset.idx, 10), 1);
                _rerenderRpTbl();
              };
            });
          }
        };
        if (_rpTblEl) {
          _rpTblEl.querySelectorAll(".exec-del-rp-row").forEach((b2) => {
            b2.onclick = () => {
              _execRepoPaths.splice(parseInt(b2.dataset.idx, 10), 1);
              _rerenderRpTbl();
            };
          });
        }
        const _clearRpBtn = document.getElementById(`exec-clear-paths-${projectId}`);
        if (_clearRpBtn) {
          _clearRpBtn.onclick = () => {
            _execRepoPaths = [];
            _rerenderRpTbl();
          };
        }
        let _execFsRoots = Array.isArray(execCfg.filesystem_roots) ? execCfg.filesystem_roots.filter((r3) => typeof r3 === "string" && r3.trim()).map((r3) => r3.trim()) : [];
        const _fsTblEl = document.getElementById(`exec-fs-roots-tbl-${projectId}`);
        const _rerenderFsTbl = () => {
          if (!_fsTblEl) return;
          if (!_execFsRoots.length) {
            _fsTblEl.innerHTML = '<div style="color:var(--muted);font-style:italic;font-size:10px">None \u2014 defaults to your home directory.</div>';
            return;
          }
          _fsTblEl.innerHTML = '<table style="width:100%;border-collapse:collapse">' + _execFsRoots.map((p3, i3) => `<tr>
          <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p3)}</td>
          <td style="padding:2px 0;text-align:right"><button class="exec-del-fs-row" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">\u2715</button></td>
        </tr>`).join("") + "</table>";
          _fsTblEl.querySelectorAll(".exec-del-fs-row").forEach((b2) => {
            b2.onclick = () => {
              _execFsRoots.splice(parseInt(b2.dataset.idx, 10), 1);
              _rerenderFsTbl();
              _rerenderFsSuggest();
            };
          });
        };
        _rerenderFsTbl();
        const _fsSuggestEl = document.getElementById(`exec-fs-roots-suggest-${projectId}`);
        const _rerenderFsSuggest = () => {
          if (!_fsSuggestEl) return;
          const sugg = suggestedFsRoots(execCfg, _execFsRoots);
          if (!sugg.length) {
            _fsSuggestEl.innerHTML = "";
            return;
          }
          _fsSuggestEl.innerHTML = '<span style="font-size:9px;color:var(--muted);align-self:center">Add tracked repo path:</span>' + sugg.map((p3) => `<button class="exec-add-fs-suggest" data-root="${escapeHtml(p3)}" style="font-size:9px;padding:1px 8px;background:transparent;border:1px dashed var(--border);border-radius:3px;color:var(--accent);cursor:pointer;font-family:var(--font-mono)">+ ${escapeHtml(p3)}</button>`).join("");
          _fsSuggestEl.querySelectorAll(".exec-add-fs-suggest").forEach((b2) => {
            b2.onclick = () => {
              const root = b2.dataset.root || "";
              if (root && !_execFsRoots.includes(root)) _execFsRoots.push(root);
              _rerenderFsTbl();
              _rerenderFsSuggest();
            };
          });
        };
        _rerenderFsSuggest();
        const _fsInput = document.getElementById(`exec-fs-roots-input-${projectId}`);
        const _fsAddBtn = document.getElementById(`exec-fs-roots-add-${projectId}`);
        const _addFsRoot = () => {
          const v3 = (_fsInput?.value || "").trim();
          if (!v3) return;
          if (!_execFsRoots.includes(v3)) _execFsRoots.push(v3);
          if (_fsInput) _fsInput.value = "";
          _rerenderFsTbl();
          _rerenderFsSuggest();
        };
        if (_fsAddBtn) _fsAddBtn.onclick = _addFsRoot;
        if (_fsInput) _fsInput.addEventListener("keydown", (e3) => {
          if (e3.key === "Enter") {
            e3.preventDefault();
            _addFsRoot();
          }
        });
        let _execCodeDirs = Array.isArray(execCfg.codebase_code_dirs) ? execCfg.codebase_code_dirs.filter((r3) => typeof r3 === "string" && r3.trim()).map((r3) => r3.trim()) : [];
        const _cdTblEl = document.getElementById(`exec-code-dirs-tbl-${projectId}`);
        const _rerenderCdTbl = () => {
          if (!_cdTblEl) return;
          if (!_execCodeDirs.length) {
            _cdTblEl.innerHTML = '<div style="color:var(--muted);font-style:italic;font-size:10px">None \u2014 no auto-index unless --code-dir is passed.</div>';
            return;
          }
          _cdTblEl.innerHTML = '<table style="width:100%;border-collapse:collapse">' + _execCodeDirs.map((p3, i3) => `<tr>
          <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p3)}</td>
          <td style="padding:2px 0;text-align:right"><button class="exec-del-cd-row" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">\u2715</button></td>
        </tr>`).join("") + "</table>";
          _cdTblEl.querySelectorAll(".exec-del-cd-row").forEach((b2) => {
            b2.onclick = () => {
              _execCodeDirs.splice(parseInt(b2.dataset.idx, 10), 1);
              _rerenderCdTbl();
            };
          });
        };
        _rerenderCdTbl();
        const _cdInput = document.getElementById(`exec-code-dirs-input-${projectId}`);
        const _cdAddBtn = document.getElementById(`exec-code-dirs-add-${projectId}`);
        const _addCodeDir = () => {
          const v3 = (_cdInput?.value || "").trim();
          if (!v3) return;
          if (!_execCodeDirs.includes(v3)) _execCodeDirs.push(v3);
          if (_cdInput) _cdInput.value = "";
          _rerenderCdTbl();
        };
        if (_cdAddBtn) _cdAddBtn.onclick = _addCodeDir;
        if (_cdInput) _cdInput.addEventListener("keydown", (e3) => {
          if (e3.key === "Enter") {
            e3.preventDefault();
            _addCodeDir();
          }
        });
        saveBtn.onclick = async () => {
          saveBtn.disabled = true;
          const fields = ["env_file", "test_cmd", "deploy_cmd", "branch"];
          const cfg = {};
          cfg.repo_paths = _execRepoPaths;
          cfg.filesystem_roots = _execFsRoots;
          cfg.codebase_code_dirs = _execCodeDirs;
          const _serenaVal = (document.getElementById(`exec-serena_repo_path-${projectId}`)?.value || "").trim();
          if (_serenaVal) cfg.serena_repo_path = _serenaVal;
          if (Array.isArray(execCfg.hostnames)) cfg.hostnames = execCfg.hostnames;
          for (const f4 of fields) {
            const val = (document.getElementById(`exec-${f4}-${projectId}`)?.value || "").trim();
            if (val) cfg[f4] = val;
          }
          const minEl = document.getElementById(`exec-test_min-${projectId}`);
          const minVal = minEl ? parseInt(minEl.value || "", 10) : NaN;
          if (!isNaN(minVal) && minVal > 0) cfg.test_min = minVal;
          const ctxRaw = ctxSlider ? parseInt(ctxSlider.value || "", 10) : NaN;
          if (!isNaN(ctxRaw)) cfg.context_threshold = Math.min(200, Math.max(10, ctxRaw));
          const mtRaw = mtSlider ? parseInt(mtSlider.value || "", 10) : NaN;
          if (!isNaN(mtRaw)) cfg.max_turns = Math.min(500, Math.max(40, mtRaw));
          const loopSel = document.getElementById(`exec-loop_enabled-${projectId}`);
          if (loopSel) cfg.loop_enabled = loopSel.value === "true" ? true : loopSel.value === "false" ? false : "workspace";
          const enrichEl = document.getElementById(`exec-enrich_prospect-${projectId}`);
          if (enrichEl) cfg.enrich_handoffs_with_code_pointers = !!enrichEl.checked;
          try {
            await saveProjectSettings(projectId, { executor_config: cfg });
            if (statusEl) statusEl.textContent = "Saved.";
            setTimeout(() => {
              if (statusEl) statusEl.textContent = "";
            }, 2e3);
          } catch (e3) {
            if (statusEl) statusEl.textContent = `Save failed: ${String(e3)}`;
          } finally {
            saveBtn.disabled = false;
          }
        };
      }, 0);
      setTimeout(() => {
        const hitlCb = document.getElementById("ws-hitl-default");
        const sprintIn = document.getElementById("ws-sprint-default");
        const displayIn = document.getElementById("ws-display-name");
        const nudgeIn = document.getElementById("ws-nudge-threshold");
        const handoffTplIn = document.getElementById("ws-handoff-template");
        const execModeIn = document.getElementById("ws-exec-mode-default");
        const codeIntelCb = document.getElementById("ws-code-intel-default");
        const loopCb = document.getElementById("ws-loop-default");
        const REFRESH_TRIGGERS = ["add_insight", "pin_decision", "pin_workspace_decision", "set_north_star", "set_goal", "generate_handoff"];
        const autoRefreshCb = document.getElementById("ws-auto-refresh");
        const refreshIntervalIn = document.getElementById("ws-refresh-interval");
        const triggerCbs = {};
        for (const t3 of REFRESH_TRIGGERS) triggerCbs[t3] = document.getElementById(`ws-trigger-${t3}`);
        const saveBtn = document.getElementById("ws-settings-save");
        const saveStatus = document.getElementById("ws-settings-status");
        (async () => {
          try {
            const s3 = await api("/workspace/settings");
            if (hitlCb) hitlCb.checked = !!s3.hitl_auto_answer_default;
            if (sprintIn) sprintIn.value = s3.sprint_name_default || "";
            if (displayIn) displayIn.value = s3.display_name || "";
            if (nudgeIn) nudgeIn.value = s3.log_task_sprint_nudge_threshold != null ? s3.log_task_sprint_nudge_threshold : 5;
            if (handoffTplIn) handoffTplIn.value = s3.handoff_template || "";
            if (execModeIn) execModeIn.value = s3.execution_mode_default || "";
            if (codeIntelCb) codeIntelCb.checked = !!s3.code_intel_enabled_default;
            if (loopCb) loopCb.checked = s3.loop_enabled_default !== false;
            if (autoRefreshCb) autoRefreshCb.checked = !!s3.auto_refresh_enabled;
            if (refreshIntervalIn) refreshIntervalIn.value = s3.refresh_interval_turns != null ? s3.refresh_interval_turns : 10;
            const activeTriggers = Array.isArray(s3.refresh_triggers) ? s3.refresh_triggers : null;
            for (const t3 of REFRESH_TRIGGERS) {
              if (triggerCbs[t3]) triggerCbs[t3].checked = activeTriggers ? activeTriggers.includes(t3) : true;
            }
          } catch (e3) {
            if (saveStatus) saveStatus.textContent = "Could not load workspace defaults.";
          }
        })();
        if (saveBtn) saveBtn.onclick = async () => {
          saveBtn.disabled = true;
          try {
            const nudgeVal = nudgeIn ? parseInt(nudgeIn.value, 10) : 5;
            await api("/workspace/settings", {
              method: "PATCH",
              body: JSON.stringify({
                hitl_auto_answer_default: !!(hitlCb && hitlCb.checked),
                sprint_name_default: sprintIn && sprintIn.value.trim() || "",
                display_name: displayIn && displayIn.value.trim() || "",
                log_task_sprint_nudge_threshold: isNaN(nudgeVal) ? 5 : Math.max(0, nudgeVal),
                handoff_template: handoffTplIn && handoffTplIn.value.trim() || "",
                // 0bf67524 — "" clears the default; a value seeds new projects.
                execution_mode_default: execModeIn ? execModeIn.value : "",
                code_intel_enabled_default: codeIntelCb && codeIntelCb.checked ? 1 : 0,
                // 76cf8bda — workspace /loop auto-continue default.
                loop_enabled_default: !!(loopCb && loopCb.checked),
                // bf51b12e — planner context-refresh config.
                auto_refresh_enabled: !!(autoRefreshCb && autoRefreshCb.checked),
                refresh_interval_turns: (() => {
                  const raw = refreshIntervalIn ? parseInt(refreshIntervalIn.value, 10) : 10;
                  return isNaN(raw) ? 10 : Math.min(50, Math.max(1, raw));
                })(),
                refresh_triggers: REFRESH_TRIGGERS.filter((t3) => triggerCbs[t3] && triggerCbs[t3].checked)
              })
            });
            if (saveStatus) saveStatus.textContent = "Saved.";
            setTimeout(() => {
              if (saveStatus) saveStatus.textContent = "";
            }, 2e3);
            toast("Workspace defaults saved");
          } catch (e3) {
            const msg = e3 && e3.message ? e3.message : String(e3);
            if (saveStatus) saveStatus.textContent = `Save failed: ${msg}`;
            toast(`Save failed: ${msg}`, true);
          } finally {
            saveBtn.disabled = false;
          }
        };
        const decList = document.getElementById("ws-decisions-list");
        async function renderWsDecisions() {
          if (!decList) return;
          try {
            const items = await api("/workspace/decisions");
            decList.innerHTML = items && items.length ? items.map((d3) => `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">

              <span><span style="color:var(--accent)">${escapeHtml(d3.category || "TECHNICAL")}</span> ${escapeHtml(d3.title || "")}: <span style="color:var(--muted)">${escapeHtml(d3.body || "")}</span></span>

              <button class="secondary" data-did="${escapeHtml(d3.id)}" style="font-size:9px;padding:2px 7px">\xD7</button>

            </div>`).join("") : '<div style="color:var(--muted)">No workspace decisions yet.</div>';
            decList.querySelectorAll("button[data-did]").forEach((btn) => {
              btn.onclick = async () => {
                if (!confirm("Delete this workspace decision?")) return;
                try {
                  await api(`/workspace/decisions/${btn.dataset.did}`, { method: "DELETE" });
                  renderWsDecisions();
                } catch (e3) {
                  alert("Error: " + e3);
                }
              };
            });
          } catch (e3) {
            decList.innerHTML = '<div style="color:var(--muted)">Failed to load.</div>';
          }
        }
        renderWsDecisions();
        const decAdd = document.getElementById("ws-dec-add");
        if (decAdd) decAdd.onclick = async () => {
          const title = (document.getElementById("ws-dec-title")?.value || "").trim();
          const body2 = (document.getElementById("ws-dec-body")?.value || "").trim();
          if (!title || !body2) return;
          decAdd.disabled = true;
          try {
            await api("/workspace/decisions", { method: "POST", body: JSON.stringify({ title, body: body2 }) });
            document.getElementById("ws-dec-title").value = "";
            document.getElementById("ws-dec-body").value = "";
            renderWsDecisions();
          } catch (e3) {
            alert("Error: " + e3);
          } finally {
            decAdd.disabled = false;
          }
        };
        const noteList = document.getElementById("ws-notes-list");
        async function renderWsNotes() {
          if (!noteList) return;
          try {
            const items = await api("/workspace/notes");
            noteList.innerHTML = items && items.length ? items.map((n2) => `<div data-note-row="${escapeHtml(n2.id)}" style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">

              <span data-note-view="${escapeHtml(n2.id)}">${escapeHtml(n2.title || "")}: <span style="color:var(--muted)">${escapeHtml(n2.body || "")}</span>${n2.tags ? ` <span style="color:var(--accent);font-size:9px">${escapeHtml(n2.tags)}</span>` : ""}</span>

              <span style="display:flex;gap:4px;flex-shrink:0">
                <button class="secondary" data-nid-edit="${escapeHtml(n2.id)}" data-ntitle="${escapeHtml(n2.title || "")}" data-nbody="${escapeHtml(n2.body || "")}" style="font-size:9px;padding:2px 6px" title="Edit">\u270E</button>
                <button class="secondary" data-nid-move="${escapeHtml(n2.id)}" style="font-size:9px;padding:2px 6px" title="Move to project">\u2197</button>
                <button class="secondary" data-nid="${escapeHtml(n2.id)}" style="font-size:9px;padding:2px 7px">\xD7</button>
              </span>

            </div>`).join("") : '<div style="color:var(--muted)">No workspace notes yet.</div>';
            noteList.querySelectorAll("button[data-nid]").forEach((btn) => {
              btn.onclick = async () => {
                if (!confirm("Delete this workspace note?")) return;
                try {
                  await api(`/workspace/notes/${btn.dataset.nid}`, { method: "DELETE" });
                  renderWsNotes();
                } catch (e3) {
                  alert("Error: " + e3);
                }
              };
            });
            noteList.querySelectorAll("button[data-nid-edit]").forEach((btn) => {
              btn.onclick = () => {
                const nid = btn.dataset.nidEdit;
                const row = noteList.querySelector(`[data-note-row="${nid}"]`);
                const view = noteList.querySelector(`[data-note-view="${nid}"]`);
                if (!row || row.querySelector("textarea")) return;
                const titleVal = btn.dataset.ntitle;
                const bodyVal = btn.dataset.nbody;
                view.style.display = "none";
                const edit = document.createElement("div");
                edit.style.cssText = "flex:1;display:flex;flex-direction:column;gap:4px";
                edit.innerHTML = `
              <input type="text" value="${escapeHtml(titleVal)}" style="font-size:10px;padding:2px 6px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px;width:100%">
              <textarea rows="2" style="font-size:10px;padding:2px 6px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px;resize:vertical;width:100%">${escapeHtml(bodyVal)}</textarea>
              <span style="display:flex;gap:4px">
                <button class="primary" style="font-size:9px;padding:2px 8px">Save</button>
                <button class="secondary" style="font-size:9px;padding:2px 8px">Cancel</button>
              </span>`;
                row.insertBefore(edit, row.querySelector("[data-note-view]").nextSibling);
                edit.querySelector("button.secondary").onclick = () => {
                  edit.remove();
                  view.style.display = "";
                };
                edit.querySelector("button.primary").onclick = async () => {
                  const newTitle = edit.querySelector("input").value.trim();
                  const newBody = edit.querySelector("textarea").value.trim();
                  if (!newTitle || !newBody) return;
                  try {
                    await api(`/workspace/notes/${nid}`, { method: "PATCH", body: JSON.stringify({ title: newTitle, body: newBody }) });
                    renderWsNotes();
                  } catch (e3) {
                    alert("Error: " + e3);
                  }
                };
              };
            });
            noteList.querySelectorAll("button[data-nid-move]").forEach((btn) => {
              btn.onclick = () => {
                const nid = btn.dataset.nidMove;
                const row = noteList.querySelector(`[data-note-row="${nid}"]`);
                if (!row || row.querySelector("select[data-move-select]")) return;
                const projects = window.state.projects || [];
                if (!projects.length) {
                  alert("No projects to move to.");
                  return;
                }
                const picker = document.createElement("span");
                picker.style.cssText = "display:flex;gap:4px;align-items:center;flex-shrink:0";
                picker.innerHTML = `
              <select data-move-select style="font-size:9px;padding:2px 4px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px;max-width:120px">
                ${projects.map((p3) => `<option value="${escapeHtml(p3.id)}">${escapeHtml(p3.name || p3.id)}</option>`).join("")}
              </select>
              <button class="primary" data-move-go style="font-size:9px;padding:2px 8px">Move</button>
              <button class="secondary" data-move-cancel style="font-size:9px;padding:2px 6px">\xD7</button>`;
                const actions = btn.parentElement;
                actions.style.display = "none";
                row.appendChild(picker);
                picker.querySelector("[data-move-cancel]").onclick = () => {
                  picker.remove();
                  actions.style.display = "";
                };
                picker.querySelector("[data-move-go]").onclick = async () => {
                  const targetId = picker.querySelector("[data-move-select]").value;
                  if (!targetId) return;
                  try {
                    await api(`/workspace/notes/${nid}/move`, { method: "POST", body: JSON.stringify({ project_id: targetId }) });
                    renderWsNotes();
                    try {
                      if (typeof loadNotesTab === "function") await loadNotesTab(targetId);
                    } catch (_2) {
                    }
                  } catch (e3) {
                    alert("Error: " + e3);
                  }
                };
              };
            });
          } catch (e3) {
            noteList.innerHTML = '<div style="color:var(--muted)">Failed to load.</div>';
          }
        }
        renderWsNotes();
        const noteAdd = document.getElementById("ws-note-add");
        if (noteAdd) noteAdd.onclick = async () => {
          const title = (document.getElementById("ws-note-title")?.value || "").trim();
          const body2 = (document.getElementById("ws-note-body")?.value || "").trim();
          if (!title || !body2) return;
          noteAdd.disabled = true;
          try {
            await api("/workspace/notes", { method: "POST", body: JSON.stringify({ title, body: body2 }) });
            document.getElementById("ws-note-title").value = "";
            document.getElementById("ws-note-body").value = "";
            renderWsNotes();
          } catch (e3) {
            alert("Error: " + e3);
          } finally {
            noteAdd.disabled = false;
          }
        };
        const sprintList = document.getElementById("ws-sprint-list");
        async function renderWsSprint() {
          if (!sprintList) return;
          try {
            const items = await api("/workspace/sprint-items");
            if (!items || !items.length) {
              sprintList.innerHTML = '<div style="color:var(--muted)">No personal backlog items yet.</div>';
              return;
            }
            const groups = {};
            items.forEach((it) => {
              const g2 = it.item_group || "(ungrouped)";
              (groups[g2] = groups[g2] || []).push(it);
            });
            const STATUSES = ["todo", "pending", "in_progress", "done", "skipped", "failed"];
            sprintList.innerHTML = Object.keys(groups).map((g2) => {
              const rows = groups[g2].map((it) => {
                const done = it.status === "done" || it.status === "skipped";
                const titleStyle = done ? "text-decoration:line-through;color:var(--muted)" : "";
                const sel = `<select data-ws-status="${escapeHtml(it.id)}" style="font-size:9px;padding:1px 3px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:3px">` + STATUSES.map((s3) => `<option value="${s3}"${s3 === it.status ? " selected" : ""}>${s3}</option>`).join("") + `</select>`;
                return `<div data-ws-row="${escapeHtml(it.id)}" style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:3px 0;border-bottom:1px solid var(--border)"><span style="${titleStyle}">${escapeHtml(it.title || "")}${it.human_id ? ` <span style="color:var(--accent);font-size:9px">@${escapeHtml(it.human_id)}</span>` : ""}</span><span style="display:flex;gap:4px;flex-shrink:0;align-items:center">${sel}<button class="secondary" data-ws-done="${escapeHtml(it.id)}" style="font-size:9px;padding:2px 6px" title="Mark done">\u2713</button></span></div>`;
              }).join("");
              return `<div data-ws-group style="margin-bottom:8px"><div style="color:var(--accent);font-size:9px;letter-spacing:.05em;text-transform:uppercase;margin-bottom:2px">${escapeHtml(g2)}</div>${rows}</div>`;
            }).join("");
            sprintList.querySelectorAll("select[data-ws-status]").forEach((sel) => {
              sel.onchange = async () => {
                try {
                  await api(`/workspace/sprint-items/${sel.dataset.wsStatus}`, { method: "PATCH", body: JSON.stringify({ status: sel.value }) });
                  renderWsSprint();
                } catch (e3) {
                  alert("Error: " + e3);
                }
              };
            });
            sprintList.querySelectorAll("button[data-ws-done]").forEach((btn) => {
              btn.onclick = async () => {
                try {
                  await api(`/workspace/sprint-items/${btn.dataset.wsDone}/complete`, { method: "POST" });
                  renderWsSprint();
                } catch (e3) {
                  alert("Error: " + e3);
                }
              };
            });
          } catch (e3) {
            sprintList.innerHTML = '<div style="color:var(--muted)">Failed to load.</div>';
          }
        }
        renderWsSprint();
        const sprintAdd = document.getElementById("ws-sprint-add");
        if (sprintAdd) sprintAdd.onclick = async () => {
          const title = (document.getElementById("ws-sprint-title")?.value || "").trim();
          const group = (document.getElementById("ws-sprint-group")?.value || "").trim();
          if (!title) return;
          sprintAdd.disabled = true;
          try {
            await api("/workspace/sprint-items", { method: "POST", body: JSON.stringify({ title, group: group || void 0 }) });
            document.getElementById("ws-sprint-title").value = "";
            document.getElementById("ws-sprint-group").value = "";
            renderWsSprint();
          } catch (e3) {
            alert("Error: " + e3);
          } finally {
            sprintAdd.disabled = false;
          }
        };
      }, 0);
      html += "</div></details>";
      html += _secHtml("tools-ref", "MCP Tools Reference");
      html += `<div id="tools-ref-body-${projectId}">
    <div style="font-size:10px;color:var(--muted);margin-bottom:10px">All built-in Meridian MCP tools, grouped by how often they appear in a typical session. Expand to load.</div>
    <div id="tools-ref-content-${projectId}" style="display:none"></div>
  </div>`;
      html += "</div></details>";
      html += "</div></details>";
      {
        let _awOpen = false;
        try {
          _awOpen = localStorage.getItem("meridian.settings.aw." + projectId) === "1";
        } catch (e3) {
        }
        const _awRot = _awOpen ? "transform:rotate(90deg)" : "";
        html += `<details id="settings-grp-aw-${projectId}" ${_awOpen ? "open" : ""} style="margin-bottom:12px;border:2px solid var(--border);border-radius:8px"><summary style="cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:8px;background:var(--surface-2);border-radius:8px"><span class="meridian-caret" style="display:inline-block;font-size:10px;color:var(--muted);transition:transform 120ms ease;${_awRot}">\u25B6</span><span style="font-weight:700;font-size:11px;color:var(--text);letter-spacing:.04em">ACCOUNT &amp; WORKSPACE</span></summary><div style="padding:8px 8px 4px">`;
      }
      html += _connectPanelHtml;
      html += _secHtml("account", "Account");
      html += `<div style="margin-bottom:16px" id="workspace-section-${projectId}">
    <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Workspace</div>
    <div style="font-size:10px;color:var(--muted);margin-bottom:10px">Applies across <strong>all projects</strong> in this workspace. Notes and decisions here are injected at the top of every project's context block.</div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Default settings</div>
      <label style="display:flex;gap:8px;align-items:flex-start;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:6px">
        <input type="checkbox" id="ws-hitl-default" style="margin-top:2px">
        <span>Auto-answer HITL by default<br>
          <span style="font-size:9px;color:var(--muted)">Seeded onto new projects' HITL auto-answer toggle.</span>
        </span>
      </label>
      <label style="font-size:10px;color:var(--muted);display:block;margin-bottom:6px">Default execution mode for new projects<br>
        <select id="ws-exec-mode-default" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
          <option value="">(no default \u2014 use built-in)</option>
          <option value="autonomous">autonomous</option>
          <option value="interactive">interactive</option>
        </select>
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">New projects in this workspace start in this posture (0bf67524). Existing projects are unchanged.</span>
      </label>
      <label style="display:flex;gap:8px;align-items:flex-start;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:6px">
        <input type="checkbox" id="ws-code-intel-default" style="margin-top:2px">
        <span>Enable Code Intel on new projects by default<br>
          <span style="font-size:9px;color:var(--muted)">Seeds new projects' code-intel toggle on.</span>
        </span>
      </label>
      <label style="display:flex;gap:8px;align-items:flex-start;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:6px">
        <input type="checkbox" id="ws-loop-default" style="margin-top:2px">
        <span>Auto-continue (<code>/loop</code>) by default<br>
          <span style="font-size:9px;color:var(--muted)">Sessions prepend <code>/loop</code> to the /goal so they auto-continue after each response. Projects set to "Use workspace default" inherit this. Recommended for sprint sessions.</span>
        </span>
      </label>
      <div style="font-size:10px;color:var(--text);margin:12px 0 4px">Context Refresh</div>
      <div style="font-size:9px;color:var(--muted);margin-bottom:8px">Periodically re-inject fresh planning context into a session so it doesn't drift as its window fills. Fires on the trigger tools below and every N turns (bf51b12e).</div>
      <label style="display:flex;gap:8px;align-items:flex-start;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:6px">
        <input type="checkbox" id="ws-auto-refresh" style="margin-top:2px">
        <span>Auto-refresh context<br>
          <span style="font-size:9px;color:var(--muted)">Master switch. When off, no automatic refresh happens regardless of the settings below.</span>
        </span>
      </label>
      <label style="font-size:10px;color:var(--muted);display:block;margin-bottom:6px">Refresh every N turns<br>
        <input id="ws-refresh-interval" type="number" min="1" max="50" placeholder="10" style="width:80px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">Turns between interval-based refreshes (1\u201350). Default: 10.</span>
      </label>
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Refresh triggers</div>
      <div style="font-size:9px;color:var(--muted);margin-bottom:6px">Calling any checked tool also triggers a refresh.</div>
      <label style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:4px">
        <input type="checkbox" id="ws-trigger-add_insight"><span><code>add_insight</code></span>
      </label>
      <label style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:4px">
        <input type="checkbox" id="ws-trigger-pin_decision"><span><code>pin_decision</code></span>
      </label>
      <label style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:4px">
        <input type="checkbox" id="ws-trigger-pin_workspace_decision"><span><code>pin_workspace_decision</code></span>
      </label>
      <label style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:4px">
        <input type="checkbox" id="ws-trigger-set_north_star"><span><code>set_north_star</code></span>
      </label>
      <label style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:4px">
        <input type="checkbox" id="ws-trigger-set_goal"><span><code>set_goal</code></span>
      </label>
      <label style="display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text);cursor:pointer;margin-bottom:6px">
        <input type="checkbox" id="ws-trigger-generate_handoff"><span><code>generate_handoff</code></span>
      </label>
      <div style="display:flex;gap:6px;align-items:flex-start;margin-bottom:8px">
        <span id="ws-stophook-badge" style="font-size:9px;padding:2px 6px;border-radius:3px;white-space:nowrap;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">Stop hook: sprint_guard</span>
        <span style="font-size:9px;color:var(--muted)">Auto-written to <code>.claude/hooks/sprint_guard.sh</code>/<code>.ps1</code> by <code>generate_handoff</code> \u2014 blocks early-stop while sprint items are pending. (Regenerated on the machine running the tunnel/server; a hosted dashboard can't inspect your local files.)</span>
      </div>
      <label style="font-size:10px;color:var(--muted);display:block">Default sprint name<br>
        <input id="ws-sprint-default" type="text" placeholder="e.g. june-sprint" style="width:100%;max-width:240px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
      </label>
      <label style="font-size:10px;color:var(--muted);display:block;margin-top:6px">Your display name<br>
        <input id="ws-display-name" type="text" placeholder="e.g. Adam" style="width:100%;max-width:240px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">Used to attribute Claude/Codex hook sessions to you on the activity timeline when they don't set a name.</span>
      </label>
      <label style="font-size:10px;color:var(--muted);display:block;margin-top:6px">log_task nudge threshold (0 = off)<br>
        <input id="ws-nudge-threshold" type="number" min="0" max="100" placeholder="5" style="width:80px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;margin-top:2px">
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">After this many inline log_task calls with no sprint items, show a nudge to file sprint items. Default: 5.</span>
      </label>
      <div style="font-size:10px;color:var(--text);margin:12px 0 4px">Handoff Format</div>
      <label style="font-size:10px;color:var(--muted);display:block">Custom full-mode handoff template (leave blank for default)<br>
        <textarea id="ws-handoff-template" rows="16" placeholder="# Handoff&#10;Sprint: {{sprint}}&#10;&#10;## Recent Tasks&#10;{{recent_tasks}}&#10;&#10;## Pending&#10;{{pending_items}}" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:6px 8px;margin-top:2px;resize:vertical"></textarea>
        <span style="display:block;font-size:9px;color:var(--muted);margin-top:2px">Placeholders: {{sprint}}, {{recent_tasks}}, {{decisions}}, {{north_star}}, {{version_goal}}, {{pending_items}}, {{notes}}. Blank = default handoff.</span>
      </label>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
        <button id="ws-settings-save" class="primary" style="font-size:10px;padding:3px 10px">Save defaults</button>
        <span id="ws-settings-status" style="font-size:10px;color:var(--muted);min-height:14px"></span>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Workspace decisions</div>
      <div id="ws-decisions-list" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"><div style="color:var(--muted)">loading\u2026</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input id="ws-dec-title" type="text" placeholder="Title" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:120px">
        <input id="ws-dec-body" type="text" placeholder="Body" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:2;min-width:160px">
        <button id="ws-dec-add" class="primary" style="font-size:10px;padding:4px 10px">Pin</button>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Workspace notes</div>
      <div id="ws-notes-list" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"><div style="color:var(--muted)">loading\u2026</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input id="ws-note-title" type="text" placeholder="Title" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:120px">
        <input id="ws-note-body" type="text" placeholder="Body" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:2;min-width:160px">
        <button id="ws-note-add" class="primary" style="font-size:10px;padding:4px 10px">Add</button>
      </div>
    </div>
    <div id="ws-sprint-section">
      <div style="font-size:10px;color:var(--text);margin-bottom:4px">Personal backlog <span style="font-size:9px;color:var(--muted)">(cross-project \u2014 not tied to any one project)</span></div>
      <div id="ws-sprint-list" style="font-size:10px;font-family:var(--font-mono);margin-bottom:6px"><div style="color:var(--muted)">loading\u2026</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input id="ws-sprint-group" type="text" placeholder="Bucket (e.g. thesis)" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:100px">
        <input id="ws-sprint-title" type="text" placeholder="What needs doing" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:2;min-width:160px">
        <button id="ws-sprint-add" class="primary" style="font-size:10px;padding:4px 10px">Add</button>
      </div>
    </div>
  </div>`;
      if (!isHostedMode() && _allRepoPaths.length > 0) {
        const _fsPaths = _allRepoPaths.map((p3) => JSON.stringify(p3)).join(" ");
        const _fsNpx = `npx -y @modelcontextprotocol/server-filesystem ${_allRepoPaths.map((p3) => JSON.stringify(p3)).join(" ")}`;
        const _fsClaude = `claude mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem ${_allRepoPaths.map((p3) => JSON.stringify(p3)).join(" ")}`;
        html += `<div style="margin-bottom:16px" id="fs-mcp-section-${projectId}">
      <details>
        <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;padding-bottom:6px;border-bottom:1px solid var(--border);margin-bottom:8px">
          <span style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase">Local file reading for planning chat</span>
          <span style="font-size:9px;color:var(--muted);margin-left:auto">\u25BC</span>
        </summary>
        <div style="font-size:10px;color:var(--muted);margin-bottom:8px">Add a filesystem MCP server so Claude can read your repo files during planning conversations.</div>
        <div style="margin-bottom:8px">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px">npx command:</div>
          <div style="display:flex;gap:6px;align-items:flex-start">
            <code id="fs-mcp-npx-${projectId}" style="flex:1;display:block;padding:6px 8px;border:1px solid var(--border);border-radius:3px;background:var(--surface-1);color:var(--text);font-size:9px;font-family:var(--font-mono);white-space:pre-wrap;word-break:break-all">${escapeHtml(_fsNpx)}</code>
            <button class="secondary" style="font-size:9px;padding:3px 8px;flex-shrink:0" onclick="navigator.clipboard.writeText(${JSON.stringify(_fsNpx)}).then(()=>toast('Copied')).catch(()=>toast('Copy failed',true))">Copy</button>
          </div>
        </div>
        <div style="margin-bottom:8px">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px">claude mcp add (Claude Code):</div>
          <div style="display:flex;gap:6px;align-items:flex-start">
            <code style="flex:1;display:block;padding:6px 8px;border:1px solid var(--border);border-radius:3px;background:var(--surface-1);color:var(--text);font-size:9px;font-family:var(--font-mono);white-space:pre-wrap;word-break:break-all">${escapeHtml(_fsClaude)}</code>
            <button class="secondary" style="font-size:9px;padding:3px 8px;flex-shrink:0" onclick="navigator.clipboard.writeText(${JSON.stringify(_fsClaude)}).then(()=>toast('Copied')).catch(()=>toast('Copy failed',true))">Copy</button>
          </div>
        </div>
        <div style="font-size:9px;color:var(--muted);line-height:1.5">Requires Node.js. Add the generated URL as a second connector in claude.ai.<br>WSL users: localhost works directly. Remote/SSH: use <code style="font-size:8px">cloudflared tunnel --url http://localhost:PORT</code></div>
      </details>
    </div>`;
      }
      if (isHostedMode()) {
        html += `<div style="margin-bottom:16px" id="members-section-${projectId}">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Team members</div>

      <div id="members-list-${projectId}" style="margin-bottom:10px;font-size:11px;font-family:var(--font-mono)"><div style="color:var(--muted)">loading\u2026</div></div>

      <div id="settings-invite-form-${projectId}" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

        <input id="invite-email-${projectId}" type="email" placeholder="teammate@example.com" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 8px;flex:1;min-width:160px">

        <select id="invite-role-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 6px">

          <option value="admin">admin</option>

          <option value="member" selected>member</option>

          <option value="viewer">viewer</option>

        </select>

        <select id="invite-scope-${projectId}" title="Scope to project (blank = full workspace access)" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 6px">

          <option value="">all projects</option>

          ${(window.state.projects || []).map((p3) => `<option value="${escapeHtml(p3.id)}">${escapeHtml(p3.name || p3.id)}</option>`).join("")}

        </select>

        <button id="invite-btn-${projectId}" class="primary" style="font-size:10px;padding:4px 10px">Invite</button>

      </div>

      <div id="invite-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:5px;min-height:14px"></div>

    </div>`;
        setTimeout(async () => {
          const listEl = document.getElementById(`members-list-${projectId}`);
          const inviteBtn = document.getElementById(`invite-btn-${projectId}`);
          const inviteEmail = document.getElementById(`invite-email-${projectId}`);
          const inviteRole = document.getElementById(`invite-role-${projectId}`);
          const inviteScope = document.getElementById(`invite-scope-${projectId}`);
          const inviteStatus = document.getElementById(`invite-status-${projectId}`);
          async function renderMembers() {
            if (!listEl) return;
            try {
              const members = await api("/workspace/members");
              if (!members || members.length === 0) {
                listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">No team members yet.</div>';
                return;
              }
              const ROLE_CHOICES = ["admin", "member", "viewer"];
              const projectNameById = Object.fromEntries((window.state.projects || []).map((p3) => [p3.id, p3.name || p3.id]));
              listEl.innerHTML = members.map((m3) => {
                const opts = ROLE_CHOICES.map((r3) => `<option value="${r3}" ${m3.role === r3 ? "selected" : ""}>${r3}</option>`).join("");
                const _isCoAdmin = m3.role === "admin" && !!m3.project_id;
                const scopeBadge = m3.project_id ? ` <span style="color:${_isCoAdmin ? "var(--accent)" : "var(--muted)"};font-size:9px;font-weight:${_isCoAdmin ? "600" : "400"}" title="${_isCoAdmin ? "Co-admin \u2014 admin of" : "Scoped to"} project ${escapeHtml(m3.project_id)}">\xB7 ${_isCoAdmin ? "co-admin @ " : ""}${escapeHtml(projectNameById[m3.project_id] || m3.project_id)}</span>` : "";
                return `

            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">

              <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(m3.email)}${scopeBadge}${m3.pending ? ' <span style="color:var(--accent);font-size:9px;font-weight:600">Invited</span>' : ""}</span>

              ${m3.pending ? `<button class="resend-invite-btn secondary" data-mid="${escapeHtml(m3.id)}" title="Resend invite" style="font-size:9px;padding:2px 7px">Resend</button>` : `<select class="member-role-select" data-mid="${escapeHtml(m3.id)}" title="Change role" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 5px">${opts}</select>`}

              <button class="secondary" data-mid="${escapeHtml(m3.id)}" title="Remove member" style="font-size:9px;padding:2px 7px">\xD7</button>

            </div>`;
              }).join("");
              if (_guest) {
                listEl.querySelectorAll('.member-role-select, .resend-invite-btn, button[title="Remove member"]').forEach((el2) => el2.remove());
              }
              listEl.querySelectorAll("select.member-role-select").forEach((sel) => {
                sel.dataset.prev = sel.value;
                sel.onchange = async () => {
                  const newRole = sel.value;
                  sel.disabled = true;
                  try {
                    await api(`/workspace/members/${sel.dataset.mid}`, {
                      method: "PATCH",
                      body: JSON.stringify({ role: newRole })
                    });
                    sel.dataset.prev = newRole;
                    if (inviteStatus) {
                      inviteStatus.textContent = `Role updated to ${newRole}.`;
                      setTimeout(() => {
                        if (inviteStatus) inviteStatus.textContent = "";
                      }, 2500);
                    }
                  } catch (e3) {
                    sel.value = sel.dataset.prev;
                    if (inviteStatus) inviteStatus.textContent = `Error: ${escapeHtml(String(e3))}`;
                  } finally {
                    sel.disabled = false;
                  }
                };
              });
              listEl.querySelectorAll("button.resend-invite-btn").forEach((btn) => {
                btn.onclick = async () => {
                  btn.disabled = true;
                  btn.textContent = "\u2026";
                  try {
                    await api(`/workspace/invite/${btn.dataset.mid}/resend`, { method: "POST" });
                    btn.textContent = "Sent";
                    if (inviteStatus) {
                      inviteStatus.textContent = "Invite resent.";
                      setTimeout(() => {
                        if (inviteStatus) inviteStatus.textContent = "";
                      }, 2500);
                    }
                  } catch (e3) {
                    btn.textContent = "Resend";
                    if (inviteStatus) inviteStatus.textContent = `Error: ${escapeHtml(String(e3))}`;
                  } finally {
                    btn.disabled = false;
                  }
                };
              });
              listEl.querySelectorAll("button[data-mid]:not(.resend-invite-btn)").forEach((btn) => {
                btn.onclick = async () => {
                  if (!confirm("Remove this member?")) return;
                  try {
                    await api(`/workspace/members/${btn.dataset.mid}`, { method: "DELETE" });
                    renderMembers();
                  } catch (e3) {
                    alert("Error: " + e3);
                  }
                };
              });
            } catch (e3) {
              if (listEl) listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">Members only available in hosted mode.</div>';
            }
          }
          renderMembers();
          if (inviteBtn) {
            inviteBtn.onclick = async () => {
              const email = (inviteEmail?.value || "").trim();
              const role = inviteRole?.value || "member";
              const scopedProjectId = inviteScope?.value || null;
              if (!email) {
                if (inviteStatus) inviteStatus.textContent = "Enter an email address.";
                return;
              }
              inviteBtn.disabled = true;
              inviteBtn.textContent = "Sending\u2026";
              try {
                await api("/workspace/invite", { method: "POST", body: JSON.stringify({ email, role, project_id: scopedProjectId || void 0 }) });
                if (inviteEmail) inviteEmail.value = "";
                if (inviteStatus) {
                  inviteStatus.textContent = `Invite sent to ${email}.`;
                  setTimeout(() => {
                    if (inviteStatus) inviteStatus.textContent = "";
                  }, 3e3);
                }
                renderMembers();
              } catch (e3) {
                if (inviteStatus) inviteStatus.textContent = `Error: ${escapeHtml(String(e3))}`;
              } finally {
                inviteBtn.disabled = false;
                inviteBtn.textContent = "Invite";
              }
            };
          }
        }, 0);
      }
      const isDemo = !!window.state.serverConfig?.demo_mode;
      if (mcpData && !isDemo) {
        html += `<div style="margin-bottom:16px" id="settings-account-danger-${projectId}">

      <div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Account</div>

      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">

        <a href="/export/my-data" download style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 10px;text-decoration:none;cursor:pointer">Export my data</a>

        <span style="font-size:10px;color:var(--muted)">Download a JSON file of all your account data (GDPR).</span>

      </div>

      <div style="border:1px solid #7f1d1d;border-radius:4px;padding:10px;background:#1a0a0a">

        <div style="color:#f87171;font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px">Danger zone</div>

        <div style="font-size:10px;color:#9ca3af;margin-bottom:8px">Permanently delete your account, cancel your subscription, and erase all data. Cannot be undone.</div>

        <button id="delete-account-btn-${projectId}" style="background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b;border-radius:3px;padding:4px 12px;font-size:10px;font-family:var(--font-mono);cursor:pointer">Delete my account</button>

        <div id="delete-account-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:5px;min-height:14px"></div>

      </div>

    </div>`;
        setTimeout(() => {
          const deleteBtn2 = document.getElementById(`delete-account-btn-${projectId}`);
          const deleteStatus = document.getElementById(`delete-account-status-${projectId}`);
          if (!deleteBtn2) return;
          deleteBtn2.onclick = async () => {
            const typed = prompt("Export your data first. Then type DELETE to permanently delete your account:");
            if (typed !== "DELETE") return;
            if (!confirm("Final confirmation: your Stripe subscription will be cancelled and all data erased. Continue?")) return;
            deleteBtn2.disabled = true;
            deleteBtn2.textContent = "Deleting\u2026";
            try {
              await api("/account/delete", { method: "POST", body: JSON.stringify({ confirmation: "DELETE" }) });
              window.location.href = "/";
            } catch (e3) {
              if (deleteStatus) deleteStatus.textContent = `Error: ${escapeHtml(String(e3))}`;
              deleteBtn2.disabled = false;
              deleteBtn2.textContent = "Delete my account";
            }
          };
        }, 0);
      }
      if (mcpData) {
        html += `<div style="margin-bottom:16px" id="usage-section-${projectId}">

      <div id="usage-header-${projectId}" style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;padding-bottom:4px;border-bottom:1px solid var(--border)">Usage this month</div>

      <div id="usage-body-${projectId}" style="font-size:10px;color:var(--muted)">loading\u2026</div>

    </div>`;
        setTimeout(async () => {
          const usageEl = document.getElementById(`usage-body-${projectId}`);
          if (!usageEl) return;
          try {
            let pct2 = function(used, limit) {
              return Math.min(100, limit > 0 ? used / limit * 100 : 0);
            }, barColor2 = function(p3) {
              return p3 >= 100 ? "#ef4444" : p3 >= 80 ? "#f59e0b" : "var(--accent)";
            };
            var pct = pct2, barColor = barColor2;
            const u4 = await api("/settings/usage");
            const c3 = u4.compute || {};
            const s3 = u4.storage || {};
            const unlimited = !!u4.unlimited;
            const headerEl = document.getElementById(`usage-header-${projectId}`);
            if (headerEl) {
              if (u4.plan === "free") headerEl.textContent = "Trial usage";
              else if (unlimited) headerEl.textContent = "Usage";
              else headerEl.textContent = "Usage this month";
            }
            if (unlimited) {
              usageEl.innerHTML = `

            <div style="margin-bottom:10px;display:flex;justify-content:space-between">

              <span style="color:var(--text)">Compute</span>

              <span>${c3.used.toFixed(2)} CU-hrs <span style="color:var(--accent)">\xB7 Unlimited</span></span>

            </div>

            <div style="margin-bottom:4px;display:flex;justify-content:space-between">

              <span style="color:var(--text)">Storage</span>

              <span>${s3.used_gb.toFixed(3)} GB <span style="color:var(--accent)">\xB7 Unlimited</span></span>

            </div>`;
              return;
            }
            const cpct = pct2(c3.used, c3.grace);
            const spct = pct2(s3.used_gb, s3.limit_gb);
            usageEl.innerHTML = `

          <div style="margin-bottom:10px">

            <div style="display:flex;justify-content:space-between;margin-bottom:3px">

              <span style="color:var(--text)">Compute${c3.throttled ? ' <span style="color:#ef4444">(throttled)</span>' : ""}</span>

              <span>${c3.used.toFixed(2)} / ${c3.limit} CU-hrs <span style="color:var(--muted)">(${c3.grace} w/grace)</span></span>

            </div>

            <div style="background:var(--surface-1);border-radius:2px;height:5px;overflow:hidden">

              <div style="background:${barColor2(cpct)};width:${cpct}%;height:100%;transition:width .3s"></div>

            </div>

          </div>

          <div style="margin-bottom:12px">

            <div style="display:flex;justify-content:space-between;margin-bottom:3px">

              <span style="color:var(--text)">Storage</span>

              <span>${s3.used_gb.toFixed(3)} / ${s3.limit_gb} GB</span>

            </div>

            <div style="background:var(--surface-1);border-radius:2px;height:5px;overflow:hidden">

              <div style="background:${barColor2(spct)};width:${spct}%;height:100%;transition:width .3s"></div>

            </div>

          </div>

          <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">

            <label style="font-size:10px;color:var(--muted)">

              Compute overage budget ($USD/mo, 0 = throttle)<br>

              <input id="compute-cap-${projectId}" type="number" min="0" step="1" value="${c3.cap_usd}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;width:80px;margin-top:3px">

            </label>

            <label style="font-size:10px;color:var(--muted)">

              Storage overage budget ($USD/mo, 0 = block)<br>

              <input id="storage-cap-${projectId}" type="number" min="0" step="1" value="${s3.cap_usd}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;width:80px;margin-top:3px">

            </label>

            <button id="save-caps-${projectId}" class="secondary" style="font-size:10px;padding:4px 10px;align-self:flex-end">Save</button>

            <span id="caps-status-${projectId}" style="font-size:10px;color:var(--muted);min-height:14px;align-self:flex-end"></span>

          </div>`;
            const saveBtn = document.getElementById(`save-caps-${projectId}`);
            const capsStatus = document.getElementById(`caps-status-${projectId}`);
            if (saveBtn) {
              saveBtn.onclick = async () => {
                const cc = parseFloat(document.getElementById(`compute-cap-${projectId}`)?.value || "0");
                const sc = parseFloat(document.getElementById(`storage-cap-${projectId}`)?.value || "0");
                saveBtn.disabled = true;
                try {
                  await api("/settings/usage", { method: "PATCH", body: JSON.stringify({ compute_cap: cc, storage_cap: sc }) });
                  if (capsStatus) {
                    capsStatus.textContent = "saved";
                    setTimeout(() => {
                      capsStatus.textContent = "";
                    }, 2e3);
                  }
                } catch (e3) {
                  if (capsStatus) capsStatus.textContent = `error: ${escapeHtml(String(e3))}`;
                } finally {
                  saveBtn.disabled = false;
                }
              };
            }
          } catch (e3) {
            if (usageEl) usageEl.textContent = "Usage data unavailable.";
          }
        }, 0);
      }
      html += `<div id="settings-ntfy-lazy-${projectId}" style="color:var(--muted);font-size:10px">Notifications load when you open the Account tab.</div>`;
      html += `<div id="settings-prefs-lazy-${projectId}"></div>`;
      html += "</div></details>";
      html += "</div></details>";
      if ((window.state.tenantPlan || "") === "admin" || !isHostedMode()) {
        const _inp = "width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none";
        html += `<details id="settings-grp-blog-${projectId}" style="margin-bottom:12px;border:2px solid var(--border);border-radius:8px"><summary style="cursor:pointer;list-style:none;padding:10px 14px;display:flex;align-items:center;gap:8px;background:var(--surface-2);border-radius:8px"><span style="font-weight:700;font-size:11px;color:var(--text);letter-spacing:.04em">BLOG <span style="color:var(--muted)">(admin)</span></span></summary><div style="padding:10px"><div style="font-size:10px;color:var(--muted);margin-bottom:8px">Authoring is admin-only. Publish makes a post live at <code>/blog/&lt;slug&gt;</code> immediately.</div><input id="blog-edit-id" type="hidden"><input id="blog-title" type="text" placeholder="Post title" style="${_inp};margin-bottom:6px"><textarea id="blog-body" rows="8" placeholder="# Heading&#10;&#10;Markdown body. **bold**, \`code\`, fenced blocks." style="${_inp};resize:vertical"></textarea><div style="display:flex;gap:6px;margin-top:6px;align-items:center;flex-wrap:wrap"><button id="blog-save" class="primary" style="font-size:10px;padding:3px 10px">Save draft</button><button id="blog-gen" class="secondary" style="font-size:10px;padding:3px 10px">Generate from activity</button><button id="blog-new" class="secondary" style="font-size:10px;padding:3px 10px">New</button><span id="blog-status" style="font-size:10px;color:var(--muted)"></span></div><div id="blog-list" style="margin-top:12px;font-size:11px"><div style="color:var(--muted)">loading\u2026</div></div></div></details>`;
      }
      html += `<div style="margin-top:20px;padding-top:12px;border-top:1px solid var(--border);display:flex;gap:12px;font-size:9px;color:var(--muted)"><a href="/terms" target="_blank" rel="noopener" style="color:var(--muted);text-decoration:none" onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">Terms of Service</a><a href="/privacy" target="_blank" rel="noopener" style="color:var(--muted);text-decoration:none" onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">Privacy Policy</a><span style="margin-left:auto">\xA9 2026 Meridian</span></div>`;
      if (body._loadTok !== _myTok) return;
      try {
        body.innerHTML = html;
      } catch (renderErr) {
        console.error("Settings render failed:", renderErr);
        body.innerHTML = `<div style="color:var(--error);font-size:11px">Failed to render settings: ${escapeHtml(String(renderErr))}</div>`;
        return;
      }
      try {
        _organizeSettingsIntoTabs(projectId);
      } catch (e3) {
        console.error("Settings tabs failed:", e3);
      }
      body.dataset.settingsTs = String(Date.now());
      _applySettingsRoleVisibility(projectId, _guest);
      _collapseConnectPlatforms(projectId);
      {
        const _trDetails = document.getElementById(`settings-sec-tools-ref-${projectId}`);
        const _trContent = document.getElementById(`tools-ref-content-${projectId}`);
        const _trBody = document.getElementById(`tools-ref-body-${projectId}`);
        if (_trDetails && _trContent && _trBody) {
          let _trLoaded = false;
          const _loadToolsRef = async () => {
            if (_trLoaded) return;
            _trLoaded = true;
            _trContent.style.display = "";
            _trContent.innerHTML = '<div style="font-size:10px;color:var(--muted)">Loading\u2026</div>';
            try {
              const tools = await api("/tools");
              if (Array.isArray(tools)) {
                _trContent.innerHTML = _toolsReferenceHtml(tools);
              } else {
                _trContent.innerHTML = '<div style="font-size:10px;color:var(--muted)">Could not load tool list.</div>';
              }
            } catch (e3) {
              _trContent.innerHTML = `<div style="font-size:10px;color:var(--error)">Failed: ${escapeHtml(String(e3))}</div>`;
            }
          };
          if (_trDetails.open) {
            _loadToolsRef().catch(() => {
            });
          }
          _trDetails.addEventListener("toggle", () => {
            if (_trDetails.open) _loadToolsRef().catch(() => {
            });
          });
        }
      }
      try {
        window.loadExecutorRulesSection?.(projectId);
      } catch (e3) {
      }
      try {
        window.loadTunnelPluginsSection?.(projectId);
      } catch (e3) {
      }
      if (isDemoMode()) hideDemoAdminControls();
      setTimeout(() => {
        const titleIn = document.getElementById("blog-title");
        const bodyIn = document.getElementById("blog-body");
        const idIn = document.getElementById("blog-edit-id");
        const listEl = document.getElementById("blog-list");
        const statusEl = document.getElementById("blog-status");
        const saveBtn = document.getElementById("blog-save");
        const genBtn = document.getElementById("blog-gen");
        const newBtn = document.getElementById("blog-new");
        if (!listEl || !saveBtn) return;
        const setStatus = (m3) => {
          if (statusEl) {
            statusEl.textContent = m3;
            if (m3) setTimeout(() => {
              if (statusEl.textContent === m3) statusEl.textContent = "";
            }, 2500);
          }
        };
        const clearEditor = () => {
          if (idIn) idIn.value = "";
          if (titleIn) titleIn.value = "";
          if (bodyIn) bodyIn.value = "";
        };
        async function loadList() {
          listEl.innerHTML = '<div style="color:var(--muted)">Loading\u2026</div>';
          try {
            const posts = await api("/admin/blog/posts");
            if (!posts.length) {
              listEl.innerHTML = '<div style="color:var(--muted)">No posts yet.</div>';
              return;
            }
            listEl.innerHTML = posts.map((p3) => {
              const pub = p3.status === "published";
              return `<div style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid var(--border)">
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p3.title)}</span>
            <span style="font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;background:${pub ? "#22c55e22;color:#22c55e" : "var(--muted)22;color:var(--muted)"}">${pub ? "published" : "draft"}</span>
            <button class="secondary blog-edit" data-id="${escapeHtml(p3.id)}" style="font-size:9px;padding:2px 7px">Edit</button>
            <button class="secondary blog-toggle" data-id="${escapeHtml(p3.id)}" data-pub="${pub ? "1" : "0"}" style="font-size:9px;padding:2px 7px">${pub ? "Unpublish" : "Publish"}</button>
            <button class="secondary blog-del" data-id="${escapeHtml(p3.id)}" style="font-size:9px;padding:2px 7px">\u{1F5D1}</button>
          </div>`;
            }).join("");
            listEl.querySelectorAll(".blog-edit").forEach((b2) => b2.onclick = async () => {
              try {
                const p3 = await api(`/admin/blog/posts/${b2.dataset.id}`);
                if (idIn) idIn.value = p3.id;
                if (titleIn) titleIn.value = p3.title;
                if (bodyIn) bodyIn.value = p3.body_md || "";
                setStatus("Loaded for editing.");
              } catch (e3) {
                toast("load failed: " + e3.message, true);
              }
            });
            listEl.querySelectorAll(".blog-toggle").forEach((b2) => b2.onclick = async () => {
              const action = b2.dataset.pub === "1" ? "unpublish" : "publish";
              try {
                await api(`/admin/blog/posts/${b2.dataset.id}/${action}`, { method: "POST" });
                toast(action + "ed \u2713");
                loadList();
              } catch (e3) {
                toast("failed: " + e3.message, true);
              }
            });
            listEl.querySelectorAll(".blog-del").forEach((b2) => b2.onclick = async () => {
              if (!confirm("Delete this post?")) return;
              try {
                await api(`/admin/blog/posts/${b2.dataset.id}`, { method: "DELETE" });
                toast("deleted");
                if (idIn && idIn.value === b2.dataset.id) clearEditor();
                loadList();
              } catch (e3) {
                toast("failed: " + e3.message, true);
              }
            });
          } catch (e3) {
            listEl.innerHTML = `<div style="color:#f87171">Failed to load posts: ${escapeHtml(e3.message || String(e3))}</div>`;
          }
        }
        saveBtn.onclick = async () => {
          const title = (titleIn && titleIn.value || "").trim();
          if (!title) {
            setStatus("Title required.");
            return;
          }
          saveBtn.disabled = true;
          try {
            const payload = { title, body_md: bodyIn && bodyIn.value || "" };
            if (idIn && idIn.value) payload.id = idIn.value;
            const saved = await api("/admin/blog/posts", { method: "POST", body: JSON.stringify(payload) });
            if (idIn) idIn.value = saved.id;
            setStatus("Saved draft.");
            toast("saved \u2713");
            loadList();
          } catch (e3) {
            toast("save failed: " + e3.message, true);
          } finally {
            saveBtn.disabled = false;
          }
        };
        if (genBtn) genBtn.onclick = async () => {
          try {
            const d3 = await api("/admin/blog/generate-draft", { method: "POST" });
            if (idIn) idIn.value = "";
            if (titleIn) titleIn.value = d3.title || "";
            if (bodyIn) bodyIn.value = d3.body_md || "";
            setStatus("Draft generated \u2014 review and save.");
          } catch (e3) {
            toast("failed: " + e3.message, true);
          }
        };
        if (newBtn) newBtn.onclick = () => {
          clearEditor();
          setStatus("New post.");
        };
        loadList();
      }, 0);
      setTimeout(() => {
        ["connect", "executor", "config", "account"].forEach(function(k3) {
          const det = document.getElementById("settings-sec-" + k3 + "-" + projectId);
          if (!det) return;
          det.addEventListener("toggle", function() {
            try {
              const ss = JSON.parse(localStorage.getItem("meridian.settings.sections." + projectId) || "{}");
              ss[k3] = det.open;
              localStorage.setItem("meridian.settings.sections." + projectId, JSON.stringify(ss));
            } catch (e3) {
            }
            const caret = det.querySelector(":scope > summary .meridian-caret");
            if (caret) caret.style.transform = det.open ? "rotate(90deg)" : "";
          });
        });
        const psGrp = document.getElementById("settings-grp-ps-" + projectId);
        if (psGrp) {
          psGrp.addEventListener("toggle", function() {
            try {
              localStorage.setItem("meridian.settings.ps." + projectId, psGrp.open ? "1" : "0");
            } catch (e3) {
            }
            const c3 = psGrp.querySelector(":scope > summary .meridian-caret");
            if (c3) c3.style.transform = psGrp.open ? "rotate(90deg)" : "";
          });
        }
        const awGrp = document.getElementById("settings-grp-aw-" + projectId);
        if (awGrp) {
          awGrp.addEventListener("toggle", function() {
            try {
              localStorage.setItem("meridian.settings.aw." + projectId, awGrp.open ? "1" : "0");
            } catch (e3) {
            }
            const c3 = awGrp.querySelector(":scope > summary .meridian-caret");
            if (c3) c3.style.transform = awGrp.open ? "rotate(90deg)" : "";
          });
        }
      }, 0);
      setTimeout(() => {
        const ezSaveBtn = document.getElementById(`exec-ez-save-${projectId}`);
        const ezClearBtn = document.getElementById(`exec-ez-clear-${projectId}`);
        const ezStatus = document.getElementById(`exec-ez-status-${projectId}`);
        if (!ezSaveBtn) return;
        const _execCfgBase = projectSettings && projectSettings.executor_config || {};
        let _ezHosts = Array.isArray(_execCfgBase.hostnames) ? [..._execCfgBase.hostnames] : [];
        let _ezPaths = Array.isArray(_execCfgBase.repo_paths) ? [..._execCfgBase.repo_paths] : [];
        const _rerenderHostsTbl = () => {
          const tbl = document.getElementById(`exec-ez-hosts-tbl-${projectId}`);
          if (!tbl) return;
          tbl.innerHTML = _ezHosts.length ? '<table style="width:100%;border-collapse:collapse">' + _ezHosts.map((h3, i3) => `<tr>
            <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(h3.hostname || "")}</td>
            <td style="padding:2px 6px 2px 0;color:var(--muted)"><label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:9px"><input type="checkbox" class="exec-ez-host-autocwd" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" ${h3.auto_add_cwds ? "checked" : ""} style="cursor:pointer"> Auto-add new cwds</label></td>
            <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-host" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">\u2715</button></td>
          </tr>`).join("") + "</table>" : '<div style="color:var(--muted);font-style:italic;font-size:10px">No machines registered yet \u2014 first hook auto-registers.</div>';
          _wireHostBtns();
        };
        const _wireHostBtns = () => {
          document.querySelectorAll(`.exec-ez-del-host[data-pid="${projectId}"]`).forEach((btn) => {
            btn.onclick = () => {
              _ezHosts.splice(parseInt(btn.dataset.idx, 10), 1);
              _rerenderHostsTbl();
            };
          });
          document.querySelectorAll(`.exec-ez-host-autocwd[data-pid="${projectId}"]`).forEach((cb) => {
            cb.onchange = () => {
              const idx = parseInt(cb.dataset.idx, 10);
              if (_ezHosts[idx]) _ezHosts[idx] = { ..._ezHosts[idx], auto_add_cwds: cb.checked };
            };
          });
        };
        _wireHostBtns();
        const _rerenderPathsTbl = () => {
          const tbl = document.getElementById(`exec-ez-paths-tbl-${projectId}`);
          if (!tbl) return;
          tbl.innerHTML = _ezPaths.length ? '<table style="width:100%;border-collapse:collapse">' + _ezPaths.map((p3, i3) => `<tr>
            <td style="padding:2px 6px 2px 0;color:var(--text);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p3.hostname || "")}</td>
            <td style="padding:2px 6px 2px 0;color:var(--muted);font-size:10px;font-family:var(--font-mono)">${escapeHtml(p3.cwd || "")}</td>
            <td style="padding:2px 0;text-align:right"><button class="exec-ez-del-row" data-pid="${escapeHtml(projectId)}" data-idx="${i3}" style="font-size:9px;padding:1px 6px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer">\u2715</button></td>
          </tr>`).join("") + "</table>" : '<div style="color:var(--muted);font-style:italic;font-size:10px">No path overrides \u2014 machine-level routing handles most cases.</div>';
          _wirePathBtns();
        };
        const _wirePathBtns = () => {
          document.querySelectorAll(`.exec-ez-del-row[data-pid="${projectId}"]`).forEach((btn) => {
            btn.onclick = () => {
              _ezPaths.splice(parseInt(btn.dataset.idx, 10), 1);
              _rerenderPathsTbl();
            };
          });
        };
        _wirePathBtns();
        const _ezAddBtn = document.getElementById(`exec-ez-add-btn-${projectId}`);
        const _ezAddCwd = document.getElementById(`exec-ez-add-cwd-${projectId}`);
        const _ezAddHost = document.getElementById(`exec-ez-add-host-${projectId}`);
        const _doAddPath = async () => {
          const cwd = (_ezAddCwd?.value || "").trim();
          const hostname = (_ezAddHost?.value || "").trim();
          if (!cwd) {
            if (ezStatus) ezStatus.textContent = "Enter a cwd path.";
            return;
          }
          if (_ezAddBtn) _ezAddBtn.disabled = true;
          try {
            const settings = await api(`/projects/${projectId}/settings`);
            const cfg = settings && settings.executor_config || {};
            const paths = Array.isArray(cfg.repo_paths) ? cfg.repo_paths.slice() : [];
            const dup = paths.some((p3) => (p3.cwd || "").trim() === cwd && (p3.hostname || "").trim() === hostname);
            if (!dup) paths.push({ cwd, hostname });
            cfg.repo_paths = paths;
            delete cfg.repo_path;
            await saveProjectSettings(projectId, { executor_config: cfg });
            _ezPaths = paths;
            _rerenderPathsTbl();
            if (_ezAddCwd) _ezAddCwd.value = "";
            if (ezStatus) {
              ezStatus.textContent = dup ? "Already present." : "Added.";
              setTimeout(() => {
                if (ezStatus) ezStatus.textContent = "";
              }, 2e3);
            }
          } catch (e3) {
            if (ezStatus) ezStatus.textContent = `Add failed: ${String(e3)}`;
          } finally {
            if (_ezAddBtn) _ezAddBtn.disabled = false;
          }
        };
        if (_ezAddBtn) _ezAddBtn.onclick = _doAddPath;
        if (_ezAddCwd) _ezAddCwd.addEventListener("keydown", (e3) => {
          if (e3.key === "Enter") {
            e3.preventDefault();
            _doAddPath();
          }
        });
        if (ezClearBtn) {
          ezClearBtn.onclick = () => {
            _ezHosts = [];
            _ezPaths = [];
            _rerenderHostsTbl();
            _rerenderPathsTbl();
          };
        }
        ezSaveBtn.onclick = async () => {
          ezSaveBtn.disabled = true;
          const curCfg = projectSettings && projectSettings.executor_config || {};
          const cfg = { ...curCfg, hostnames: _ezHosts, repo_paths: _ezPaths };
          delete cfg.repo_path;
          try {
            await saveProjectSettings(projectId, { executor_config: cfg });
            if (ezStatus) {
              ezStatus.textContent = "Saved.";
              setTimeout(() => {
                if (ezStatus) ezStatus.textContent = "";
              }, 2e3);
            }
          } catch (e3) {
            if (ezStatus) ezStatus.textContent = `Save failed: ${String(e3)}`;
          } finally {
            ezSaveBtn.disabled = false;
          }
        };
      }, 0);
      const deleteBtn = document.getElementById(`account-delete-${projectId}`);
      if (deleteBtn) {
        deleteBtn.onclick = async () => {
          const typed = window.prompt(
            "This permanently deletes your account, projects, and data. Stripe subscription is canceled and the Neon DB is dropped.\n\nType DELETE to confirm."
          );
          if (typed !== "DELETE") {
            if (typed !== null) toast("Account NOT deleted (confirmation did not match).");
            return;
          }
          deleteBtn.disabled = true;
          try {
            await api("/account/delete", {
              method: "POST",
              body: JSON.stringify({ confirmation: "DELETE" })
            });
            toast("Account deleted. Signing out\u2026");
            setTimeout(() => {
              window.location.href = "/";
            }, 1200);
          } catch (e3) {
            toast("Delete failed: " + e3.message, true);
            deleteBtn.disabled = false;
          }
        };
      }
      const billingPortalBtn = document.getElementById(`billing-portal-btn-${projectId}`);
      if (billingPortalBtn) {
        billingPortalBtn.onclick = async () => {
          billingPortalBtn.disabled = true;
          billingPortalBtn.textContent = "Loading\u2026";
          try {
            const data = await api("/billing/portal", { method: "POST" });
            window.location.href = data.url;
          } catch (e3) {
            toast("Could not open billing portal: " + e3.message, true);
            billingPortalBtn.disabled = false;
            billingPortalBtn.textContent = "Manage billing \u2192";
          }
        };
      }
      var currentToken = null;
      setTimeout(() => {
        const hostedPlaceholderToken = mcpData ? "sk_meridian_" + "x".repeat(32) : "";
        let hooksToken = null;
        const renderHooks = () => {
          const activeToken = hooksToken || hostedPlaceholderToken;
          const installUnix = `curl -fsSL ${hooksBaseUrl}/install.sh | sh`;
          const installWindows = `irm ${hooksBaseUrl}/install.ps1 | iex`;
          const snippets = {
            [`hooks-install-unix-${projectId}`]: installUnix,
            [`hooks-install-windows-${projectId}`]: installWindows,
            [`hooks-win-claude-${projectId}`]: buildClaudeHookSnippet2("windows", activeToken),
            [`hooks-win-codex-${projectId}`]: buildCodexHookSnippet2("windows", activeToken),
            [`hooks-unix-claude-${projectId}`]: buildClaudeHookSnippet2("unix", activeToken),
            [`hooks-unix-codex-${projectId}`]: buildCodexHookSnippet2("unix", activeToken)
          };
          Object.entries(snippets).forEach(([id, text]) => {
            const el2 = document.getElementById(id);
            if (el2) el2.textContent = text;
          });
          const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
          if (statusEl) {
            if (hooksToken) {
              statusEl.textContent = "Real API key generated - hosted snippets are prefilled for this user and project.";
            } else if (mcpData) {
              statusEl.textContent = "Generate an API key to replace the placeholder token in the hosted snippets below.";
            } else {
              statusEl.textContent = "Local mode - no Bearer token needed.";
            }
          }
        };
        const tokenListEl = document.getElementById(`hooks-token-list-${projectId}`);
        const renderHooksTokenList = (tokens) => {
          if (!tokenListEl) return;
          if (!Array.isArray(tokens) || !tokens.length) {
            tokenListEl.innerHTML = `<div style="font-size:10px;color:var(--muted)">No API keys yet. Generate one above to prefill the hosted snippets.</div>`;
            return;
          }
          tokenListEl.innerHTML = tokens.map((token) => {
            const isReadOnly = (token.token_type || "readwrite") === "readonly";
            const typeBadge = `<span style="font-size:9px;padding:1px 5px;border-radius:3px;border:1px solid ${isReadOnly ? "#fbbf24" : "var(--accent)"};color:${isReadOnly ? "#fbbf24" : "var(--accent)"};margin-left:4px">${isReadOnly ? "read-only" : "read-write"}</span>`;
            return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2)">
          <div style="min-width:0;flex:1">
            <div style="font-size:10px;color:var(--text);font-family:var(--font-mono);word-break:break-all;display:flex;align-items:center;gap:4px">${escapeHtml(token.masked_token || "sk_meridian_...")}${typeBadge}</div>
            <div style="font-size:9px;color:var(--muted);margin-top:2px">${escapeHtml(token.label || "API key")} - ${escapeHtml(token.created_at || "")}</div>
          </div>
          <button class="secondary" data-token-id="${escapeHtml(token.id || "")}" data-token-label="${escapeHtml(token.label || "")}" style="font-size:10px;padding:3px 8px;color:var(--danger,#ef4444)">Revoke</button>
        </div>`;
          }).join("");
          tokenListEl.querySelectorAll("[data-token-id]").forEach((btn) => {
            btn.onclick = async () => {
              const tokenId = btn.getAttribute("data-token-id");
              if (!tokenId) return;
              const _revokeLabel = btn.getAttribute("data-token-label") || "";
              const _isHooksKey = _revokeLabel.includes("hooks") || _revokeLabel.includes("installer");
              const _revokeMsg = _isHooksKey ? "This key may be used in your Claude Code hooks.\n\nAfter revoking, re-run: irm https://usemeridian.us/install.ps1 | iex\n\nRevoke anyway?" : "Revoke this API key? Existing clients using it will stop working.";
              if (!confirm(_revokeMsg)) return;
              btn.disabled = true;
              try {
                await api(`/auth/tokens/${tokenId}`, { method: "DELETE" });
                const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
                if (statusEl) statusEl.textContent = "API key revoked.";
                await loadHooksTokens();
              } catch (e3) {
                btn.disabled = false;
                const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
                if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e3))}`;
              }
            };
          });
        };
        async function loadHooksTokens() {
          if (!tokenListEl) return;
          tokenListEl.innerHTML = `<div style="font-size:10px;color:var(--muted)">Loading API keys...</div>`;
          try {
            const tokens = await api("/auth/tokens");
            renderHooksTokenList(tokens);
          } catch (e3) {
            tokenListEl.innerHTML = `<div style="font-size:10px;color:var(--danger,#ef4444)">Could not load API keys.</div>`;
            const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
            if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e3))}`;
          }
        }
        const wireCopy = (buttonId, targetId) => {
          const btn = document.getElementById(buttonId);
          const target = document.getElementById(targetId);
          if (!btn || !target) return;
          btn.onclick = async () => {
            try {
              await navigator.clipboard.writeText(target.textContent || "");
              btn.textContent = "Copied!";
              setTimeout(() => {
                btn.textContent = "Copy";
              }, 1800);
            } catch (e3) {
              btn.textContent = "Select and copy manually";
            }
          };
        };
        [
          ["hooks-copy-install-unix-" + projectId, "hooks-install-unix-" + projectId],
          ["hooks-copy-install-windows-" + projectId, "hooks-install-windows-" + projectId],
          ["hooks-copy-win-claude-" + projectId, "hooks-win-claude-" + projectId],
          ["hooks-copy-win-codex-" + projectId, "hooks-win-codex-" + projectId],
          ["hooks-copy-unix-claude-" + projectId, "hooks-unix-claude-" + projectId],
          ["hooks-copy-unix-codex-" + projectId, "hooks-unix-codex-" + projectId]
        ].forEach(([buttonId, targetId]) => wireCopy(buttonId, targetId));
        function _showKeyReveal(rawToken, label) {
          const revealEl = document.getElementById(`hooks-key-reveal-${projectId}`);
          const revealInput = document.getElementById(`hooks-key-reveal-input-${projectId}`);
          const revealCopyBtn = document.getElementById(`hooks-key-copy-btn-${projectId}`);
          const revealDismiss = document.getElementById(`hooks-key-dismiss-${projectId}`);
          if (!revealEl || !revealInput) return;
          revealInput.value = rawToken;
          revealEl.style.display = "block";
          function _hideReveal() {
            revealEl.style.display = "none";
            const masked = "sk_meridian_\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" + rawToken.slice(-4);
            const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
            if (statusEl) statusEl.textContent = `${label || "Key"} saved: ${masked} \u2014 use "Generate new key" to rotate.`;
          }
          if (revealCopyBtn) {
            revealCopyBtn.onclick = async () => {
              try {
                await navigator.clipboard.writeText(rawToken);
                revealCopyBtn.textContent = "Copied!";
                setTimeout(() => {
                  revealCopyBtn.textContent = "Copy key";
                }, 1800);
              } catch (e3) {
              }
            };
          }
          if (revealDismiss) revealDismiss.onclick = _hideReveal;
          setTimeout(_hideReveal, 3e4);
        }
        const genBtn = document.getElementById(`hooks-gen-token-${projectId}`);
        if (genBtn) {
          genBtn.onclick = async () => {
            genBtn.disabled = true;
            genBtn.textContent = "Generating...";
            try {
              const tok = await api("/auth/tokens", { method: "POST", body: JSON.stringify({ label: "hooks-config" }) });
              hooksToken = tok.token;
              currentToken = tok.token;
              renderHooks();
              await loadHooksTokens();
              const hostedMcpEl2 = document.getElementById(`hosted-mcp-json-${projectId}`);
              if (hostedMcpEl2) {
                const j3 = JSON.stringify({ mcpServers: { meridian: { command: "npx", args: ["-y", "mcp-remote", "https://usemeridian.us/mcp"], env: { BEARER_TOKEN: tok.token } } } }, null, 2);
                hostedMcpEl2.textContent = j3;
              }
              _showKeyReveal(tok.token, "API key");
            } catch (e3) {
              const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
              if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e3))}`;
            } finally {
              genBtn.disabled = false;
              genBtn.textContent = hooksToken ? "Generate new key" : "Generate API key";
            }
          };
        }
        const genReadonlyBtn = document.getElementById(`hooks-gen-readonly-token-${projectId}`);
        if (genReadonlyBtn) {
          genReadonlyBtn.onclick = async () => {
            genReadonlyBtn.disabled = true;
            genReadonlyBtn.textContent = "Generating...";
            try {
              const tok = await api("/auth/tokens", { method: "POST", body: JSON.stringify({ label: "readonly", token_type: "readonly" }) });
              _showKeyReveal(tok.token, "Read-only key");
              await loadHooksTokens();
            } catch (e3) {
              const statusEl = document.getElementById(`hooks-token-status-${projectId}`);
              if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e3))}`;
            } finally {
              genReadonlyBtn.disabled = false;
              genReadonlyBtn.textContent = "Generate read-only key";
            }
          };
        }
        const refreshBtn = document.getElementById(`hooks-refresh-tokens-${projectId}`);
        if (refreshBtn) {
          refreshBtn.onclick = async () => {
            refreshBtn.disabled = true;
            try {
              await loadHooksTokens();
            } finally {
              refreshBtn.disabled = false;
            }
          };
        }
        renderHooks();
        if (tokenListEl) loadHooksTokens();
      }, 0);
      const ntfySaveBtn = document.getElementById(`ntfy-save-${projectId}`);
      if (ntfySaveBtn) {
        ntfySaveBtn.onclick = async () => {
          const inp = document.getElementById(`ntfy-url-${projectId}`);
          const statusEl = document.getElementById(`ntfy-status-${projectId}`);
          const raw = (inp ? inp.value : "").trim() || null;
          try {
            const saved = await api(`/projects/${projectId}/ntfy`, {
              method: "PATCH",
              body: JSON.stringify({ notify_url: raw, ntfy_url: raw })
            });
            const savedVal = saved && (saved.notify_url || saved.ntfy_url || "");
            const shownVal = displayNotifyTarget(savedVal || "");
            if (inp) inp.value = shownVal || "";
            if (statusEl) {
              statusEl.textContent = shownVal && raw && shownVal.toLowerCase() !== displayNotifyTarget(String(raw)).toLowerCase() ? `saved as ${shownVal}` : "saved";
              setTimeout(() => {
                statusEl.textContent = "";
              }, 2400);
            }
          } catch (e3) {
            if (statusEl) statusEl.textContent = "error";
          }
        };
      }
      const notifyEmailSaveBtn = document.getElementById(`notify-email-save-${projectId}`);
      if (notifyEmailSaveBtn) {
        notifyEmailSaveBtn.onclick = async () => {
          const inp = document.getElementById(`notify-email-${projectId}`);
          const statusEl = document.getElementById(`notify-email-status-${projectId}`);
          const raw = (inp ? inp.value : "").trim() || null;
          try {
            await api(`/projects/${projectId}/ntfy`, {
              method: "PATCH",
              body: JSON.stringify({ notify_email: raw })
            });
            if (statusEl) {
              statusEl.textContent = "saved";
              setTimeout(() => {
                statusEl.textContent = "";
              }, 2400);
            }
          } catch (e3) {
            if (statusEl) statusEl.textContent = "error";
          }
        };
      }
      const ntfyTestBtn = document.getElementById(`ntfy-test-${projectId}`);
      if (ntfyTestBtn) {
        ntfyTestBtn.onclick = async () => {
          const statusEl = document.getElementById(`ntfy-status-${projectId}`);
          ntfyTestBtn.disabled = true;
          try {
            await api(`/projects/${projectId}/notify/test`, { method: "POST", body: "{}" });
            if (statusEl) {
              statusEl.textContent = "sent!";
              setTimeout(() => {
                statusEl.textContent = "";
              }, 3e3);
            }
          } catch (e3) {
            if (statusEl) {
              const raw = String(e3?.message || e3 || "");
              let msg = raw.replace(/^\d+:\s*/, "");
              try {
                const parsed = JSON.parse(msg);
                msg = parsed.detail || parsed.message || parsed.error || msg;
              } catch (_err) {
              }
              statusEl.textContent = msg.includes("No notify URL configured") ? "save a URL first" : msg;
            }
          } finally {
            ntfyTestBtn.disabled = false;
          }
        };
      }
      const ntfyWarnAckCb = document.getElementById(`ntfy-warn-ack-${projectId}`);
      if (ntfyWarnAckCb) {
        ntfyWarnAckCb.onchange = () => {
          if (!ntfyWarnAckCb.checked) return;
          try {
            localStorage.setItem(STORAGE_KEY("ntfy.warn.dismissed"), "1");
          } catch (e3) {
          }
          const warnEl = document.getElementById(`ntfy-warn-${projectId}`);
          if (warnEl) warnEl.style.display = "none";
          const inp = document.getElementById(`ntfy-url-${projectId}`);
          const saveBtn = document.getElementById(`ntfy-save-${projectId}`);
          const testBtn = document.getElementById(`ntfy-test-${projectId}`);
          [inp, saveBtn, testBtn].forEach((el2) => {
            if (el2) {
              el2.disabled = false;
              el2.style.opacity = "1";
            }
          });
        };
      }
      body.querySelectorAll("input[data-pref]").forEach((cb) => {
        cb.onchange = async () => {
          const statusEl = document.getElementById(`settings-save-status-${projectId}`);
          const payload = {};
          body.querySelectorAll("input[data-pref]").forEach((c3) => {
            payload[c3.dataset.pref] = c3.checked;
          });
          try {
            await api("/settings/notifications", { method: "PATCH", body: JSON.stringify(payload) });
            if (statusEl) {
              statusEl.textContent = "saved";
              setTimeout(() => {
                statusEl.textContent = "";
              }, 1800);
            }
          } catch (e3) {
            if (statusEl) statusEl.textContent = `error: ${escapeHtml(String(e3))}`;
          }
        };
      });
      const ghConnectBtn = document.getElementById(`github-connect-btn-${projectId}`);
      if (ghConnectBtn) {
        ghConnectBtn.onclick = () => {
          window.location.href = `/auth/github/repo-connect?project_id=${encodeURIComponent(projectId)}`;
        };
      }
      const ghAddAccountBtn = document.getElementById(`github-add-account-${projectId}`);
      if (ghAddAccountBtn) {
        ghAddAccountBtn.onclick = () => {
          window.location.href = `/auth/github/repo-connect?project_id=${encodeURIComponent(projectId)}&select_account=1`;
        };
      }
      document.querySelectorAll(`#github-card-${projectId} .gh-acct-use`).forEach((btn) => {
        btn.onclick = async () => {
          const login = btn.dataset.login || "";
          if (!login) return;
          btn.disabled = true;
          try {
            await api(`/projects/${projectId}/github/account`, {
              method: "PATCH",
              body: JSON.stringify({ account_login: login })
            });
            loadSettingsTab2(projectId, { force: true });
          } catch (e3) {
            btn.disabled = false;
            toast("Could not switch account: " + (e3.message || e3), true);
          }
        };
      });
      document.querySelectorAll(`#github-card-${projectId} .gh-acct-remove`).forEach((btn) => {
        btn.onclick = async () => {
          const login = btn.dataset.login || "";
          if (!login) return;
          if (!confirm(`Disconnect GitHub account @${login}? Projects using it fall back to another connected account.`)) return;
          btn.disabled = true;
          try {
            await api(`/github/connections/${encodeURIComponent(login)}`, { method: "DELETE" });
            loadSettingsTab2(projectId, { force: true });
          } catch (e3) {
            btn.disabled = false;
            toast("Could not disconnect: " + (e3.message || e3), true);
          }
        };
      });
      const ghSaveBtn = document.getElementById(`github-save-btn-${projectId}`);
      if (ghSaveBtn) {
        ghSaveBtn.onclick = async () => {
          const statusEl = document.getElementById(`github-status-${projectId}`);
          const repo = (document.getElementById(`github-repo-${projectId}`)?.value || "").trim();
          const branch = (document.getElementById(`github-branch-${projectId}`)?.value || "main").trim();
          if (!repo) {
            if (statusEl) statusEl.textContent = "repo is required";
            return;
          }
          ghSaveBtn.disabled = true;
          ghSaveBtn.textContent = "Saving\u2026";
          if (statusEl) statusEl.textContent = "";
          try {
            await api(`/projects/${projectId}/github/connect`, {
              method: "POST",
              body: JSON.stringify({ repo, branch })
            });
            await loadSettingsTab2(projectId, { force: true });
          } catch (e3) {
            if (statusEl && statusEl.isConnected) statusEl.textContent = e3.message || "Save failed";
          } finally {
            if (ghSaveBtn.isConnected) {
              ghSaveBtn.disabled = false;
              ghSaveBtn.textContent = "Save repo";
            }
          }
        };
      }
      const ghDisconnectBtn = document.getElementById(`github-disconnect-btn-${projectId}`);
      if (ghDisconnectBtn) {
        ghDisconnectBtn.onclick = async () => {
          const statusEl = document.getElementById(`github-status-${projectId}`);
          ghDisconnectBtn.disabled = true;
          try {
            await api(`/projects/${projectId}/github/disconnect`, { method: "DELETE" });
            await loadSettingsTab2(projectId, { force: true });
          } catch (e3) {
            if (statusEl && statusEl.isConnected) statusEl.textContent = "error disconnecting";
            if (ghDisconnectBtn.isConnected) ghDisconnectBtn.disabled = false;
          }
        };
      }
      const ghRepoSelect = document.getElementById(`github-repo-${projectId}`);
      const ghBranchSelect = document.getElementById(`github-branch-${projectId}`);
      if (ghBranchSelect) {
        const fillBranches = async (repo, preferred) => {
          const fallback = preferred || ghSelectedBranch || "main";
          try {
            const res = await api(`/projects/${projectId}/github/branches?repo=${encodeURIComponent(repo || "")}`);
            let branches = Array.isArray(res && res.branches) ? res.branches.slice() : [];
            const want = preferred || res && res.default_branch || fallback;
            if (want && !branches.includes(want)) branches.unshift(want);
            if (!branches.length) branches = [fallback];
            ghBranchSelect.innerHTML = branches.map(
              (b2) => `<option value="${escapeHtml(b2)}" ${b2 === want ? "selected" : ""}>${escapeHtml(b2)}</option>`
            ).join("");
          } catch (e3) {
          }
        };
        fillBranches(ghSelectedRepo, ghSelectedBranch);
        if (ghRepoSelect) {
          ghRepoSelect.addEventListener("change", () => {
            const selectedRepo = ghRepoSelect.value;
            const nextDefault = ghRepoMap[selectedRepo] && ghRepoMap[selectedRepo].default_branch;
            fillBranches(selectedRepo, nextDefault);
          });
        }
      }
      const ghTestBtn = document.getElementById(`github-test-btn-${projectId}`);
      if (ghTestBtn) {
        ghTestBtn.onclick = async () => {
          const statusEl = document.getElementById(`github-status-${projectId}`);
          ghTestBtn.disabled = true;
          try {
            const st = await api(`/projects/${projectId}/github/status`);
            if (statusEl) {
              statusEl.textContent = st.connected ? st.github_user ? `@${st.github_user}` : "connected" : "not connected";
              setTimeout(() => {
                statusEl.textContent = "";
              }, 3e3);
            }
          } catch (e3) {
            if (statusEl) statusEl.textContent = "error";
          } finally {
            ghTestBtn.disabled = false;
          }
        };
      }
    } catch (e3) {
      body.innerHTML = `<div style="color:var(--error);font-size:11px">Failed to load settings: ${escapeHtml(String(e3))}</div>`;
    }
  }
  try {
    Object.assign(window, { suggestNtfyTopic: suggestNtfyTopic2, loadSettingsTab: loadSettingsTab2 });
  } catch (e3) {
  }

  // meridian/static/dashboard-plugins.ts
  var _TUNNEL_DEFAULT_PORTS = { fs: 8808, code: 8809, extract: 8810, ppt: 8811, word: 8812, dc: 8813 };
  var _OPTIN_SLOT_HINTS = {
    word: { pkg: "uvx docx-mcp", note: "Word / DOCX editing \u2014 needs uv (uvx)." },
    ppt: { pkg: "uvx powerpoint-mcp", note: "PowerPoint authoring \u2014 needs uv (uvx)." },
    dc: { pkg: "npx -y @wonderwhy-er/desktop-commander@latest", note: "Desktop Commander, local only \u2014 needs Node (npx)." }
  };
  var _CURATED_TUNNEL_PLUGINS = [
    { name: "Sequential Thinking", command: "npx -y @modelcontextprotocol/server-sequential-thinking", description: "Structured step-by-step reasoning", docs: "https://github.com/modelcontextprotocol/servers" },
    { name: "Fetch", command: "uvx mcp-server-fetch", description: "Fetch & convert web pages to markdown", docs: "https://github.com/modelcontextprotocol/servers" },
    { name: "Git", command: "uvx mcp-server-git", description: "Read/search/manipulate Git repos", docs: "https://github.com/modelcontextprotocol/servers" },
    { name: "Time", command: "uvx mcp-server-time", description: "Time & timezone conversion", docs: "https://github.com/modelcontextprotocol/servers" },
    { name: "Memory", command: "npx -y @modelcontextprotocol/server-memory", description: "Knowledge-graph persistent memory", docs: "https://github.com/modelcontextprotocol/servers" },
    { name: "arXiv", command: "uvx arxiv-mcp-server", description: "arXiv search, download papers as markdown, citation graph (Semantic Scholar), topic watches \u2014 research prospecting for planning sessions", docs: "https://github.com/blazickjp/arxiv-mcp-server" }
  ];
  window._CURATED_TUNNEL_PLUGINS = _CURATED_TUNNEL_PLUGINS;
  function _detectTunnelOs() {
    const ua = (navigator.userAgent || "") + " " + (navigator.platform || "");
    if (/win/i.test(ua)) return "windows";
    if (/mac|darwin|iphone|ipad/i.test(ua)) return "macos";
    return "linux";
  }
  window._detectTunnelOs = _detectTunnelOs;
  var _TUNNEL_INSTALL_CMDS = {
    windows: {
      label: "Windows",
      uv: "winget install --id=astral-sh.uv -e",
      node: "winget install OpenJS.NodeJS -e"
    },
    macos: {
      label: "macOS",
      uv: "brew install uv",
      node: "brew install node"
    },
    linux: {
      label: "Linux",
      uv: "curl -LsSf https://astral.sh/uv/install.sh | sh",
      node: "sudo apt-get install -y nodejs npm"
    }
  };
  window._TUNNEL_INSTALL_CMDS = _TUNNEL_INSTALL_CMDS;
  async function _tunnelCopyToClipboard(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_2) {
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (_2) {
      return false;
    }
  }
  window._tunnelCopyToClipboard = _tunnelCopyToClipboard;
  function _renderSlotHealthWarning(slot, slotStatus) {
    const st = slotStatus && slotStatus[slot];
    if (!st || !st.reason && !st.detail) return "";
    const label = st.reason === "access_denied" ? "access denied" : (st.reason || "unavailable").replace(/_/g, " ");
    const detail = st.detail ? `<div style="margin-top:3px;color:var(--muted);font-weight:400">${escapeHtml(st.detail)}</div>` : "";
    return `
        <div data-slot-warning="${escapeHtml(slot)}" style="margin-top:6px;padding:6px 8px;border:1px solid #c79a00;border-radius:4px;background:rgba(199,154,0,0.10);font-size:9px;line-height:1.6;color:#e0b400">
          <span style="font-weight:700">&#9888; ${escapeHtml(label)}</span>${detail}
        </div>`;
  }
  window._renderSlotHealthWarning = _renderSlotHealthWarning;
  function _renderStaleOverrideWarning(p3) {
    if (!p3 || !p3.stale_override) return "";
    const newer = Array.isArray(p3.newer_default_command) ? p3.newer_default_command.join(" ") : "";
    const label = p3.newer_default_label ? escapeHtml(p3.newer_default_label) : "a newer built-in default";
    return `
        <div data-slot-stale="${escapeHtml(p3.slot)}" style="margin-top:6px;padding:6px 8px;border:1px solid #3b82f6;border-radius:4px;background:rgba(59,130,246,0.10);font-size:9px;line-height:1.6;color:#7dd3fc">
          <span style="font-weight:700">&#9888; newer default available</span> \u2014 your saved command is an old built-in default. Current default: <b>${label}</b>${newer ? ` (<code style="font-family:var(--font-mono)">${escapeHtml(newer)}</code>)` : ""}.
          <button type="button" class="tp-reset-default" data-newer="${escapeHtml(newer)}" style="margin-left:6px;background:none;border:1px solid #3b82f6;border-radius:3px;color:#7dd3fc;font-size:8px;padding:1px 6px;cursor:pointer"
            onclick="window._applyStaleOverrideDefault && window._applyStaleOverrideDefault(this)">Use new default</button>
        </div>`;
  }
  window._renderStaleOverrideWarning = _renderStaleOverrideWarning;
  function _applyStaleOverrideDefault(btn) {
    if (!btn || typeof btn.closest !== "function") return;
    const row = btn.closest("[data-lifecycle]");
    if (!row) return;
    const cmd = row.querySelector(".tp-command");
    if (!cmd) return;
    const newer = btn.getAttribute ? btn.getAttribute("data-newer") || "" : "";
    if (!newer) return;
    cmd.value = newer;
    cmd.dispatchEvent(new Event("input", { bubbles: true }));
  }
  window._applyStaleOverrideDefault = _applyStaleOverrideDefault;
  function _fsAttr(v3) {
    return escapeHtml(String(v3 == null ? "" : v3));
  }
  function _renderFsRootsCard(projectId) {
    return `
    <details class="meridian-disclosure" style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0" data-fs-roots-card="${_fsAttr(projectId)}">
      <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Filesystem roots (live)</summary>
      <div style="padding:0 8px 8px">
        <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
          Directories the <code>/fs</code> connector may read, unioned across this account's projects.
          Add or remove one and the running <code>meridian --tunnel</code> picks it up immediately &mdash; no restart.
        </div>
        <div id="fs-roots-list-${_fsAttr(projectId)}" style="margin-bottom:8px"><div style="color:var(--muted);font-size:10px">Loading&hellip;</div></div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <input type="text" id="fs-roots-input-${_fsAttr(projectId)}" placeholder="add a directory (e.g. C:\\Users\\me\\Projects)"
            style="flex:1;min-width:180px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
          <button class="secondary admin-only" id="fs-roots-add-${_fsAttr(projectId)}" style="padding:2px 10px;font-size:10px;flex-shrink:0">Add</button>
        </div>
        <div id="fs-roots-status-${_fsAttr(projectId)}" style="font-size:10px;color:var(--muted);margin-top:4px"></div>
      </div>
    </details>`;
  }
  window._renderFsRootsCard = _renderFsRootsCard;
  function _renderFsRootsList(roots) {
    const list = Array.isArray(roots) ? roots.filter((r3) => typeof r3 === "string" && r3.trim()).map((r3) => r3.trim()) : [];
    if (!list.length) {
      return '<div style="color:var(--muted);font-size:10px">No roots &mdash; the connector falls back to your home directory.</div>';
    }
    return list.map((p3) => `
    <div style="border:1px solid var(--border);border-radius:4px;padding:6px 8px;margin-bottom:6px;display:flex;gap:8px;align-items:center">
      <div style="flex:1;min-width:0;font-size:10px;color:var(--text);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_fsAttr(p3)}">${escapeHtml(p3)}</div>
      <button class="fs-root-remove" data-root="${_fsAttr(p3)}" title="Remove this root" style="font-size:11px;line-height:1;padding:1px 7px;background:transparent;border:1px solid var(--border);border-radius:3px;color:var(--muted);cursor:pointer;flex-shrink:0">&#10005;</button>
    </div>`).join("");
  }
  window._renderFsRootsList = _renderFsRootsList;
  async function _wireFsRootsCard(section, projectId) {
    if (!section) return;
    const card = section.querySelector(`[data-fs-roots-card="${CSS.escape(projectId)}"]`);
    if (!card || card.dataset.wired === "1") return;
    card.dataset.wired = "1";
    const listEl = document.getElementById(`fs-roots-list-${projectId}`);
    const inputEl = document.getElementById(`fs-roots-input-${projectId}`);
    const addBtn = document.getElementById(`fs-roots-add-${projectId}`);
    const statusEl = document.getElementById(`fs-roots-status-${projectId}`);
    const setStatus = (m3) => {
      if (!statusEl) return;
      statusEl.textContent = m3 || "";
      if (m3) setTimeout(() => {
        if (statusEl.textContent === m3) statusEl.textContent = "";
      }, 3e3);
    };
    const liveNote = (live) => {
      const s3 = live && live.status;
      if (s3 === "ok") return " (applied to the running tunnel)";
      if (s3 === "not_connected") return " (saved \u2014 tunnel offline, applies on next start)";
      if (s3 === "error") return " (saved \u2014 could not reach the tunnel)";
      return "";
    };
    const renderList = (roots) => {
      if (!listEl) return;
      listEl.innerHTML = _renderFsRootsList(roots);
      listEl.querySelectorAll(".fs-root-remove").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const root = btn.dataset.root || "";
          if (!root) return;
          btn.disabled = true;
          try {
            const r3 = await api("/tunnel/filesystem-roots", {
              method: "DELETE",
              body: JSON.stringify({ path: root })
            });
            renderList(r3 && r3.roots || []);
            setStatus("Removed" + liveNote(r3 && r3.live));
          } catch (e3) {
            btn.disabled = false;
            toast("Remove failed: " + (e3 && e3.message || e3), true);
          }
        });
      });
    };
    const addRoot = async () => {
      const v3 = (inputEl && inputEl.value || "").trim();
      if (!v3) {
        toast("Enter a directory path", true);
        return;
      }
      if (addBtn) addBtn.disabled = true;
      try {
        const r3 = await api("/tunnel/filesystem-roots", {
          method: "POST",
          body: JSON.stringify({ path: v3 })
        });
        if (inputEl) inputEl.value = "";
        renderList(r3 && r3.roots || []);
        setStatus("Added" + liveNote(r3 && r3.live));
      } catch (e3) {
        toast("Add failed: " + (e3 && e3.message || e3), true);
      } finally {
        if (addBtn) addBtn.disabled = false;
      }
    };
    if (addBtn) addBtn.addEventListener("click", addRoot);
    if (inputEl) inputEl.addEventListener("keydown", (e3) => {
      if (e3.key === "Enter") {
        e3.preventDefault();
        addRoot();
      }
    });
    try {
      const data = await api("/tunnel/filesystem-roots");
      renderList(data && data.filesystem_roots || []);
    } catch (e3) {
      if (listEl) listEl.innerHTML = `<div style="color:var(--error);font-size:10px">Failed to load roots: ${escapeHtml(e3 && e3.message || String(e3))}</div>`;
    }
  }
  window._wireFsRootsCard = _wireFsRootsCard;
  async function loadTunnelPluginsSection2(projectId, hostname) {
    const host = document.getElementById(`settings-body-${projectId}`);
    if (!host) return;
    const existing = document.getElementById(`tunnel-plugins-section-${projectId}`);
    if (existing) existing.remove();
    const section = document.createElement("div");
    section.id = `tunnel-plugins-section-${projectId}`;
    section.style.cssText = "margin-top:18px;padding-top:14px;border-top:1px solid var(--border)";
    host.appendChild(section);
    const _selHost = (hostname || "").trim();
    const _hq = _selHost ? "?hostname=" + encodeURIComponent(_selHost) : "";
    try {
      const data = await api("/tunnel/plugins" + _hq);
      const plan = data && data.plan || "free";
      if (!(plan === "pro" || plan === "admin" || data && data.is_admin)) {
        section.remove();
        return;
      }
      const plugins = data && data.plugins || [];
      const active = data && data.active || {};
      const customPlugins = (data && data.custom || []).map((c3) => ({
        name: String(c3.name || ""),
        command: Array.isArray(c3.command) ? c3.command.join(" ") : String(c3.command || ""),
        port: c3.port,
        enabled: c3.enabled !== false
      }));
      const slotStatus = data && data.slot_status || {};
      const renderRow = (p3) => {
        const cmd = Array.isArray(p3.command) ? p3.command.join(" ") : "";
        const lifecycleState = _pluginLifecycleState(p3, active, slotStatus);
        const hint = _OPTIN_SLOT_HINTS[p3.slot];
        const installCmd = hint ? hint.pkg : cmd || "";
        const lifecycleBadge = _renderLifecycleBadge(p3, lifecycleState, installCmd);
        const hintHtml = hint && lifecycleState === "not_installed" ? `
          <div style="margin-top:6px;font-size:9px;color:var(--muted);line-height:1.6">
            Enable the toggle, then restart <code style="font-family:var(--font-mono)">meridian --tunnel</code> to launch
            <code style="font-family:var(--font-mono)">${escapeHtml(hint.pkg)}</code>.<br>${escapeHtml(hint.note)}
          </div>` : "";
        const warnHtml = _renderSlotHealthWarning(p3.slot, slotStatus);
        const toggle = p3.core ? `<span title="core tool \u2014 always on" style="font-size:8px;font-weight:700;letter-spacing:.3px;color:var(--muted);border:1px solid var(--border);border-radius:3px;padding:1px 5px;text-transform:uppercase">core</span>` : `<input type="checkbox" class="tp-enabled" data-name="${escapeHtml(p3.name)}" ${p3.enabled ? "checked" : ""}
                style="width:14px;height:14px;accent-color:var(--accent);cursor:pointer">`;
        return `
        <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:8px" data-lifecycle="${lifecycleState}">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <label style="display:flex;align-items:center;gap:8px;cursor:${p3.core ? "default" : "pointer"};font-size:11px;color:var(--text);font-weight:600">
              ${toggle}
              ${escapeHtml(p3.name)}
              <span style="font-size:9px;color:var(--muted);font-weight:400">/${escapeHtml(p3.slot)}</span>
            </label>
            ${lifecycleBadge}
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="text" class="tp-command" data-name="${escapeHtml(p3.name)}" value="${escapeHtml(cmd)}"
              placeholder="default (${escapeHtml(p3.description || "built-in command")})"
              style="flex:1;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="number" class="tp-port" data-name="${escapeHtml(p3.name)}" data-slot="${escapeHtml(p3.slot)}" value="${p3.port}"
              title="local proxy port"
              style="width:74px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
          </div>
          ${warnHtml}
          ${_renderStaleOverrideWarning(p3)}
          ${hintHtml}
          <details class="tp-tools" data-slot="${escapeHtml(p3.slot)}" data-loaded="0" style="margin-top:6px">
            <summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--accent);user-select:none">&#9656; tools</summary>
            <div class="tp-tools-body" style="margin-top:5px;font-size:10px;color:var(--muted);font-family:var(--font-mono)">&hellip;</div>
          </details>
        </div>`;
      };
      const coreRows = plugins.filter((p3) => p3.core && p3.slot !== "zotero").map(renderRow).join("");
      const pluginRows = plugins.filter((p3) => !p3.core && p3.slot !== "zotero").map(renderRow).join("");
      const zoteroRow = plugins.some((p3) => p3.slot === "zotero") ? _renderZoteroStatusRow({
        zotero_active: !!active.zotero,
        slot_status: slotStatus.zotero ? { zotero: slotStatus.zotero } : {}
      }) : "";
      const _sectionLabel = (text, note) => `<div style="font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin:2px 0 6px">${text} <span style="font-weight:400;text-transform:none">${note}</span></div>`;
      const rows = `
      ${coreRows ? _sectionLabel("Core Tools", "\u2014 always on") + coreRows : ""}
      ${zoteroRow ? _sectionLabel("Reference manager", "\u2014 citation resolution (Zotero)") + zoteroRow : ""}
      ${_sectionLabel("Plugins", "\u2014 opt-in, toggle to enable")}
      ${pluginRows || '<div style="color:var(--muted);font-size:10px">No plugins.</div>'}`;
      const detectedOs = _detectTunnelOs();
      const installCard = (label, cmds, prominent) => `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)${prominent ? "" : ";opacity:.85"}">
        <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:6px">${escapeHtml(label)}${prominent ? ' <span style="color:var(--muted);font-weight:400">(detected)</span>' : ""}</div>
        ${[["uv", "powers uvx plugins", cmds.uv], ["Node.js", "powers npx plugins", cmds.node]].map(([dep, note, command]) => `
          <div style="margin-bottom:6px">
            <div style="font-size:9px;color:var(--muted);margin-bottom:3px">${escapeHtml(dep)} <span style="opacity:.8">\u2014 ${escapeHtml(note)}</span></div>
            <div style="display:flex;gap:6px;align-items:center">
              <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(command)}</code>
              <button class="secondary tp-copy" data-copy="${escapeHtml(command)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy</button>
            </div>
          </div>`).join("")}
      </div>`;
      const otherOsCards = Object.keys(_TUNNEL_INSTALL_CMDS).filter((k3) => k3 !== detectedOs).map((k3) => installCard(_TUNNEL_INSTALL_CMDS[k3].label, _TUNNEL_INSTALL_CMDS[k3], false)).join("");
      const installSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Install dependencies</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Tunnel plugins launch via <code>uvx</code> (uv) and <code>npx</code> (Node.js). Install whichever a plugin's command needs.
          </div>
          ${installCard(_TUNNEL_INSTALL_CMDS[detectedOs].label, _TUNNEL_INSTALL_CMDS[detectedOs], true)}
          <details style="margin-top:2px">
            <summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--muted)">&#9656; other platforms</summary>
            <div style="margin-top:6px">${otherOsCards}</div>
          </details>
        </div>
      </details>`;
      const browseSection = await _renderPluginBrowseSection(projectId);
      const renderCustomList = () => {
        const listEl = document.getElementById(`tp-custom-list-${projectId}`);
        if (!listEl) return;
        if (!customPlugins.length) {
          listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">No custom plugins yet.</div>';
          return;
        }
        listEl.innerHTML = customPlugins.map((c3, i3) => `
        <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;display:flex;gap:8px;align-items:center">
          <div style="flex:1;min-width:0">
            <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(c3.name)}
              <span style="font-size:9px;color:var(--muted);font-weight:400">:${escapeHtml(String(c3.port))}</span></div>
            <div style="font-size:10px;color:var(--muted);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(c3.command)}</div>
          </div>
          <button class="secondary tp-custom-remove" data-idx="${i3}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Remove</button>
        </div>`).join("");
        listEl.querySelectorAll(".tp-custom-remove").forEach((btn) => {
          btn.addEventListener("click", () => {
            const idx = parseInt(btn.dataset.idx, 10);
            if (Number.isInteger(idx)) {
              customPlugins.splice(idx, 1);
              renderCustomList();
            }
          });
        });
      };
      const customSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Custom plugins</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Add your own MCP server. Runs locally as <code>http://127.0.0.1:&lt;port&gt;</code> and is written
            into this machine's <code>.mcp.json</code> for a co-located Cursor / Claude Code session.
            Local-only \u2014 it does not appear in the claude.ai connector. Use a port outside 8808\u20138813.
          </div>
          <div id="tp-custom-list-${projectId}" style="margin-bottom:8px"></div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <input type="text" id="tp-custom-name-${projectId}" placeholder="name (e.g. fetch)"
              style="width:120px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="text" id="tp-custom-command-${projectId}" placeholder="command (e.g. uvx mcp-server-fetch)"
              style="flex:1;min-width:160px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="number" id="tp-custom-port-${projectId}" placeholder="port"
              style="width:74px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <button class="secondary admin-only" id="tp-custom-add-${projectId}" style="padding:2px 10px;font-size:10px;flex-shrink:0">Add</button>
          </div>
        </div>
      </details>`;
      const _tpHosts = data && data.hosts || [];
      const _tpConfigured = new Set(data && data.configured_hosts || []);
      const _hostPicker = _tpHosts.length ? `
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;flex-wrap:wrap">
        <span style="font-size:10px;color:var(--muted)">Machine:</span>
        <select id="tp-host-${projectId}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 6px;outline:none">
          <option value="" ${!_selHost ? "selected" : ""}>Default (all machines)</option>
          ${_tpHosts.map((h3) => `<option value="${escapeHtml(h3)}" ${h3 === _selHost ? "selected" : ""}>${escapeHtml(h3)}${_tpConfigured.has(h3) ? " \u25CF" : ""}</option>`).join("")}
        </select>
        <span style="font-size:9px;color:var(--muted)">${_selHost ? "editing " + escapeHtml(_selHost) : "default \u2014 applies to machines without their own config"}</span>
      </div>` : "";
      section.innerHTML = `
      <details class="meridian-disclosure" open style="border:1px solid var(--border);border-radius:6px;background:var(--surface-2);padding:0">
      <summary style="cursor:pointer;list-style:none;padding:8px 10px;font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase">Tunnel Plugins</summary>
      <div style="padding:0 10px 10px">
      <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
        What <code>meridian --tunnel</code> spawns behind each transport slot. Leave a command
        blank for the built-in default, or set one to swap it (e.g. <code>code-intel</code> \u2192
        <code>codegraph</code>). Changes apply the next time the tunnel restarts.
      </div>
      ${_hostPicker}
      ${rows || '<div style="color:var(--muted);font-size:10px">No plugins.</div>'}
      ${_renderFsRootsCard(projectId)}
      <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:6px">
        <button class="secondary admin-only" id="tp-reset-${projectId}" style="padding:2px 10px;font-size:10px" title="Clear all overrides (back to built-in defaults)">Reset to defaults</button>
        <button class="primary admin-only" id="tp-save-${projectId}" style="padding:2px 10px;font-size:10px">Save</button>
      </div>
      <div id="tp-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:4px;text-align:right"></div>
      ${customSection}
      ${installSection}
      ${browseSection}
      </div>
      </details>`;
      const statusEl = document.getElementById(`tp-status-${projectId}`);
      const setStatus = (m3) => {
        if (statusEl) {
          statusEl.textContent = m3;
          if (m3) setTimeout(() => {
            if (statusEl.textContent === m3) statusEl.textContent = "";
          }, 2500);
        }
      };
      const _hostSel = document.getElementById(`tp-host-${projectId}`);
      if (_hostSel) _hostSel.onchange = () => loadTunnelPluginsSection2(projectId, _hostSel.value);
      const collectConfig = () => {
        const cfg = [];
        section.querySelectorAll(".tp-command").forEach((cmdEl) => {
          const name = cmdEl.dataset.name;
          const portEl = section.querySelector(`.tp-port[data-name="${CSS.escape(name)}"]`);
          const enEl = section.querySelector(`.tp-enabled[data-name="${CSS.escape(name)}"]`);
          const entry = { name };
          if (enEl) entry.enabled = enEl.checked;
          const cmdVal = (cmdEl.value || "").trim();
          if (cmdVal) entry.command = cmdVal;
          const portVal = parseInt(portEl && portEl.value, 10);
          const slot = portEl && portEl.dataset.slot;
          if (Number.isInteger(portVal) && portVal !== _TUNNEL_DEFAULT_PORTS[slot]) entry.port = portVal;
          if (entry.command !== void 0 || entry.port !== void 0 || entry.enabled !== void 0) {
            cfg.push(entry);
          }
        });
        customPlugins.forEach((c3) => {
          const name = (c3.name || "").trim();
          const command = (c3.command || "").trim();
          const port = parseInt(c3.port, 10);
          if (!name || !command || !Number.isInteger(port)) return;
          cfg.push({ name, command, port, enabled: c3.enabled !== false });
        });
        return cfg;
      };
      document.getElementById(`tp-save-${projectId}`).onclick = async () => {
        try {
          await api("/tunnel/plugins" + _hq, { method: "PUT", body: JSON.stringify({ config: collectConfig() }) });
          toast(_selHost ? `Saved for ${_selHost}` : "Tunnel plugins saved");
          setStatus("Saved \u2014 restart the tunnel to apply.");
        } catch (e3) {
          toast("Save failed: " + e3.message, true);
        }
      };
      document.getElementById(`tp-reset-${projectId}`).onclick = async () => {
        if (!confirm("Reset tunnel plugins?\n\nThis clears ALL command and port overrides for every slot and returns them to Meridian's built-in defaults. This cannot be undone.")) return;
        try {
          await api("/tunnel/plugins" + _hq, { method: "PUT", body: JSON.stringify({ config: [] }) });
          toast("Reset to defaults");
          loadTunnelPluginsSection2(projectId, _selHost);
        } catch (e3) {
          toast("Reset failed: " + e3.message, true);
        }
      };
      renderCustomList();
      const _addCustom = () => {
        const nameEl = document.getElementById(`tp-custom-name-${projectId}`);
        const cmdEl = document.getElementById(`tp-custom-command-${projectId}`);
        const portEl = document.getElementById(`tp-custom-port-${projectId}`);
        const name = (nameEl && nameEl.value || "").trim();
        const command = (cmdEl && cmdEl.value || "").trim();
        const port = parseInt(portEl && portEl.value, 10);
        if (!name || !command || !Number.isInteger(port)) {
          toast("Custom plugin needs a name, command, and port", true);
          return;
        }
        if (port < 1024 || port > 65535 || [8808, 8809, 8810, 8811, 8812, 8813].includes(port)) {
          toast("Pick a port in 1024\u201365535 and outside 8808\u20138813", true);
          return;
        }
        if (customPlugins.some((c3) => c3.name === name)) {
          toast(`A custom plugin named "${name}" already exists`, true);
          return;
        }
        customPlugins.push({ name, command, port, enabled: true });
        if (nameEl) nameEl.value = "";
        if (cmdEl) cmdEl.value = "";
        if (portEl) portEl.value = "";
        renderCustomList();
      };
      const _addBtn = document.getElementById(`tp-custom-add-${projectId}`);
      if (_addBtn) _addBtn.addEventListener("click", _addCustom);
      section.querySelectorAll(".tp-copy").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const ok = await _tunnelCopyToClipboard(btn.dataset.copy || "");
          if (ok) {
            const prev = btn.textContent;
            btn.textContent = "Copied";
            setTimeout(() => {
              btn.textContent = prev;
            }, 1200);
          } else {
            toast("Copy failed \u2014 select and copy manually", true);
          }
        });
      });
      _wireRegistryCopyButtons(section);
      _wireRegistryBrowse(section, projectId, _hq);
      _wireLifecycleInstallButtons(section);
      _wireFsRootsCard(section, projectId);
      let _tenantIdPromise = null;
      const _getTenantId = () => {
        if (!_tenantIdPromise) {
          _tenantIdPromise = api("/me").then((m3) => m3 && m3.tenant_id || null).catch(() => null);
        }
        return _tenantIdPromise;
      };
      section.querySelectorAll(".tp-tools").forEach((det) => {
        det.addEventListener("toggle", async () => {
          if (!det.open || det.dataset.loaded === "1") return;
          det.dataset.loaded = "1";
          const slot = det.dataset.slot;
          const bodyEl = det.querySelector(".tp-tools-body");
          if (!bodyEl) return;
          if (!active[slot]) {
            bodyEl.innerHTML = '<span style="color:var(--muted)">not connected \u2014 start the tunnel</span>';
            det.dataset.loaded = "0";
            return;
          }
          bodyEl.textContent = "loading\u2026";
          try {
            const tenantId = await _getTenantId();
            if (!tenantId) throw new Error("no tenant");
            const r3 = await fetch(`/${slot}/mcp/${tenantId}/mcp`, {
              method: "POST",
              headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" },
              body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} })
            });
            if (!r3.ok) throw new Error(`HTTP ${r3.status}`);
            const text = await r3.text();
            let parsed = null;
            if (text.trim().startsWith("{")) {
              parsed = JSON.parse(text);
            } else {
              for (const line of text.split("\n")) {
                if (line.startsWith("data:")) {
                  try {
                    parsed = JSON.parse(line.slice(5).trim());
                  } catch (_2) {
                  }
                }
              }
            }
            if (!parsed) throw new Error("empty response");
            if (parsed.error) throw new Error(parsed.error.message || String(parsed.error));
            const tools = parsed.result && parsed.result.tools || [];
            if (!tools.length) {
              bodyEl.innerHTML = '<span style="color:var(--muted)">no tools reported</span>';
              return;
            }
            bodyEl.innerHTML = `<div style="color:var(--muted);margin-bottom:3px">${tools.length} tool${tools.length !== 1 ? "s" : ""}</div>` + tools.map((t3) => `<div style="color:var(--text)">${escapeHtml(t3 && t3.name || String(t3))}</div>`).join("");
          } catch (e3) {
            bodyEl.innerHTML = `<span style="color:var(--muted)">not connected \u2014 start the tunnel</span>`;
            det.dataset.loaded = "0";
          }
        });
      });
    } catch (e3) {
      section.innerHTML = `<div class="empty" style="color:var(--error)">Failed to load tunnel plugins: ${escapeHtml(e3.message)}</div>`;
    }
  }
  window.loadTunnelPluginsSection = loadTunnelPluginsSection2;
  function _customNameFromRegistry(raw) {
    let s3 = String(raw || "").trim();
    if (!s3) return "";
    if (s3.includes("/")) s3 = s3.split("/").filter(Boolean).pop() || s3;
    s3 = s3.replace(/^@/, "");
    s3 = s3.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/-{2,}/g, "-");
    s3 = s3.replace(/^[._-]+/, "");
    s3 = s3.slice(0, 64).replace(/[._-]+$/, "");
    return s3;
  }
  window._customNameFromRegistry = _customNameFromRegistry;
  async function _renderPluginBrowseSection(projectId) {
    let servers = null;
    let nextCursor = null;
    try {
      const data = await api("/tunnel/registry?limit=20");
      if (data && Array.isArray(data.servers)) {
        servers = data.servers;
        nextCursor = data.next_cursor || null;
      }
    } catch (_2) {
    }
    if (!servers) {
      const curatedRows = _CURATED_TUNNEL_PLUGINS.map((c3) => `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(c3.name)}</span>
          <a href="${escapeHtml(c3.docs)}" target="_blank" rel="noopener" style="font-size:9px;color:var(--accent);text-decoration:none">docs &#8599;</a>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px">${escapeHtml(c3.description)}</div>
        <div style="display:flex;gap:6px;align-items:center">
          <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(c3.command)}</code>
          <button class="secondary tp-copy" data-copy="${escapeHtml(c3.command)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy command</button>
        </div>
      </div>`).join("");
      return `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Browse plugins</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Well-known MCP servers. Copy a command and paste it into a slot above to swap that slot's launcher.
          </div>
          ${curatedRows}
        </div>
      </details>`;
    }
    const _renderRegistryCard = (s3) => {
      const name = escapeHtml(s3.name || s3.id || "");
      const desc = escapeHtml(s3.description || "");
      const installCmd = s3.install_command || "";
      const homepage = escapeHtml(s3.homepage || s3.url || "");
      const addName = _customNameFromRegistry(s3.name || s3.id || "");
      const addBtn = installCmd && addName ? `<button class="primary rg-add" data-add-name="${escapeHtml(addName)}" data-add-cmd="${escapeHtml(installCmd)}" style="padding:2px 8px;font-size:10px;flex-shrink:0" title="Add as a local custom plugin">Add</button>` : "";
      return `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:11px;color:var(--text);font-weight:600">${name}</span>
          ${homepage ? `<a href="${homepage}" target="_blank" rel="noopener" style="font-size:9px;color:var(--accent);text-decoration:none">docs &#8599;</a>` : ""}
        </div>
        ${desc ? `<div style="font-size:10px;color:var(--muted);margin-bottom:5px">${desc}</div>` : ""}
        ${installCmd ? `<div style="display:flex;gap:6px;align-items:center">
          <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(installCmd)}</code>
          ${addBtn}
          <button class="secondary rg-copy" data-copy="${escapeHtml(installCmd)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy command</button>
        </div>` : ""}
      </div>`;
    };
    window._renderRegistryCard = _renderRegistryCard;
    const serverRows = servers.map(_renderRegistryCard).join("");
    const loadMoreAttr = nextCursor ? `data-cursor="${escapeHtml(nextCursor)}"` : "";
    const loadMoreBtn = nextCursor ? `<button class="secondary" id="rg-load-more-${projectId}" ${loadMoreAttr} style="width:100%;font-size:10px;padding:4px 0;margin-top:4px">Load more</button>` : "";
    return `
    <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
      <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Browse plugins (live registry)</summary>
      <div style="padding:0 8px 8px">
        <div style="display:flex;gap:6px;margin-bottom:8px;align-items:center">
          <input type="text" id="rg-search-${projectId}" placeholder="Search MCP servers\u2026"
            style="flex:1;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;padding:5px 7px;outline:none">
        </div>
        <div id="rg-list-${projectId}">${serverRows}</div>
        ${loadMoreBtn}
      </div>
    </details>`;
  }
  window._renderPluginBrowseSection = _renderPluginBrowseSection;
  function _wireRegistryCopyButtons(container) {
    if (!container) return;
    container.querySelectorAll(".rg-copy").forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", async () => {
        const ok = await _tunnelCopyToClipboard(btn.dataset.copy || "");
        if (ok) {
          const prev = btn.textContent;
          btn.textContent = "Copied";
          setTimeout(() => {
            btn.textContent = prev;
          }, 1200);
        } else {
          toast("Copy failed \u2014 select and copy manually", true);
        }
      });
    });
  }
  window._wireRegistryCopyButtons = _wireRegistryCopyButtons;
  function _wireRegistryAddButtons(container, hq) {
    if (!container) return;
    container.querySelectorAll(".rg-add").forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", async () => {
        const name = btn.dataset.addName || "";
        const command = btn.dataset.addCmd || "";
        if (!name || !command) return;
        const prev = btn.textContent;
        btn.disabled = true;
        btn.textContent = "Adding\u2026";
        try {
          const r3 = await api("/tunnel/plugins/custom" + (hq || ""), {
            method: "POST",
            body: JSON.stringify({ name, command })
          });
          btn.textContent = "Added";
          btn.classList.remove("primary");
          btn.classList.add("secondary");
          const added = r3 && r3.added || null;
          toast(added && added.port ? `Added "${name}" on port ${added.port} \u2014 restart the tunnel to launch it.` : `Added "${name}" \u2014 restart the tunnel to launch it.`);
        } catch (e3) {
          btn.disabled = false;
          btn.textContent = prev;
          toast("Add failed: " + (e3 && e3.message || e3), true);
        }
      });
    });
  }
  window._wireRegistryAddButtons = _wireRegistryAddButtons;
  function _wireRegistryBrowse(section, projectId, hq) {
    _wireRegistryAddButtons(document.getElementById(`rg-list-${projectId}`), hq);
    const searchEl = document.getElementById(`rg-search-${projectId}`);
    if (searchEl) {
      searchEl.addEventListener("input", () => {
        const q2 = searchEl.value.toLowerCase();
        const listEl = document.getElementById(`rg-list-${projectId}`);
        if (!listEl) return;
        listEl.querySelectorAll(":scope > div").forEach((row) => {
          const text = row.textContent.toLowerCase();
          row.style.display = text.includes(q2) ? "" : "none";
        });
      });
    }
    const loadMoreBtn = document.getElementById(`rg-load-more-${projectId}`);
    if (loadMoreBtn) {
      loadMoreBtn.addEventListener("click", async () => {
        const cursor = loadMoreBtn.dataset.cursor;
        if (!cursor) return;
        loadMoreBtn.textContent = "Loading\u2026";
        loadMoreBtn.disabled = true;
        try {
          const data = await api(`/tunnel/registry?limit=20&cursor=${encodeURIComponent(cursor)}`);
          if (data && Array.isArray(data.servers)) {
            const listEl = document.getElementById(`rg-list-${projectId}`);
            if (listEl) {
              data.servers.forEach((s3) => {
                const tmp = document.createElement("div");
                tmp.innerHTML = window._renderRegistryCard ? window._renderRegistryCard(s3) : "";
                while (tmp.firstChild) listEl.appendChild(tmp.firstChild);
              });
              _wireRegistryCopyButtons(listEl);
              _wireRegistryAddButtons(listEl, hq);
            }
            if (data.next_cursor) {
              loadMoreBtn.dataset.cursor = data.next_cursor;
              loadMoreBtn.textContent = "Load more";
              loadMoreBtn.disabled = false;
            } else {
              loadMoreBtn.remove();
            }
          }
        } catch (e3) {
          loadMoreBtn.textContent = "Load more";
          loadMoreBtn.disabled = false;
          toast("Failed to load more: " + e3.message, true);
        }
      });
    }
  }
  window._wireRegistryBrowse = _wireRegistryBrowse;
  function _pluginLifecycleState(plugin, active, slotStatus) {
    if (active && active[plugin.slot]) {
      if (slotStatus && slotStatus[plugin.slot]) return "unhealthy";
      if (plugin.enabled === false) return "active_disabled";
      return "active";
    }
    if (plugin.enabled !== false) return "installed_inactive";
    return "not_installed";
  }
  window._pluginLifecycleState = _pluginLifecycleState;
  function _renderLifecycleBadge(plugin, lifecycleState, installCmd) {
    const styles = {
      active: { dot: "var(--success, #3fb950)", label: "active", labelColor: "var(--success, #3fb950)" },
      // 678ec121 — same green "connected" dot as active (it genuinely is
      // connected right now), but the label + amber text call out the
      // enabled/active mismatch instead of silently agreeing with the toggle.
      active_disabled: { dot: "var(--success, #3fb950)", label: "active (disabled)", labelColor: "#f59e0b" },
      unhealthy: { dot: "var(--danger, #f85149)", label: "unhealthy", labelColor: "var(--danger, #f85149)" },
      installed_inactive: { dot: "#f59e0b", label: "inactive", labelColor: "#f59e0b" },
      not_installed: { dot: "var(--muted)", label: "not installed", labelColor: "var(--muted)" }
    };
    const s3 = styles[lifecycleState] || styles.not_installed;
    const dotHtml = `<span style="width:8px;height:8px;border-radius:50%;background:${s3.dot};flex-shrink:0"></span>`;
    const labelHtml = `<span style="font-size:9px;color:${s3.labelColor};font-weight:600">${s3.label}</span>`;
    let actionBtn = "";
    if (lifecycleState === "not_installed" && installCmd) {
      const safeCmd = escapeHtml(installCmd);
      actionBtn = `<button class="secondary tp-install-btn" data-install-cmd="${safeCmd}" style="padding:2px 8px;font-size:10px;flex-shrink:0" title="Copy the install command to run in your terminal">Copy command</button>`;
    } else if (lifecycleState === "installed_inactive") {
      actionBtn = `<span style="font-size:9px;color:var(--muted);font-style:italic">start tunnel to activate</span>`;
    } else if (lifecycleState === "active_disabled") {
      actionBtn = `<span style="font-size:9px;color:var(--muted);font-style:italic" title="Still connected from an earlier session even though it's toggled off. Live connection state is per-session and does not update until the tunnel restarts or this slot idles out.">disabled \u2014 still connected</span>`;
    } else if (lifecycleState === "unhealthy") {
      actionBtn = `<span style="font-size:9px;color:var(--muted);font-style:italic">recovering\u2026</span>`;
    }
    return `<span style="display:inline-flex;align-items:center;gap:4px">${dotHtml}${labelHtml}${actionBtn ? " " + actionBtn : ""}</span>`;
  }
  window._renderLifecycleBadge = _renderLifecycleBadge;
  function _wireLifecycleInstallButtons(container) {
    if (!container) return;
    container.querySelectorAll(".tp-install-btn").forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", async () => {
        const cmd = btn.dataset.installCmd || "";
        if (!cmd) return;
        const ok = await _tunnelCopyToClipboard(cmd);
        if (ok) {
          const prev = btn.textContent;
          btn.textContent = "Copied!";
          btn.title = "Paste in your terminal to install";
          setTimeout(() => {
            btn.textContent = prev;
            btn.title = "Copy install command";
          }, 2e3);
        } else {
          toast("Copy failed \u2014 manual copy needed", true);
        }
      });
    });
  }
  window._wireLifecycleInstallButtons = _wireLifecycleInstallButtons;
  function _renderZoteroStatusRow(status) {
    const st = status || {};
    const active = { zotero: !!st.zotero_active };
    const slotStatus = st.slot_status && st.slot_status.zotero ? { zotero: st.slot_status.zotero } : {};
    const lifecycleState = _pluginLifecycleState({ slot: "zotero" }, active, slotStatus);
    const installCmd = "uvx zotero-mcp";
    const badge = _renderLifecycleBadge({ slot: "zotero" }, lifecycleState, installCmd);
    const warnHtml = _renderSlotHealthWarning("zotero", slotStatus);
    return `
    <div data-zotero-status="${escapeHtml(lifecycleState)}"
      style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <label style="display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text);font-weight:600">
          Reference manager
          <span style="font-size:9px;color:var(--muted);font-weight:400">/zotero</span>
        </label>
        ${badge}
      </div>
      <div style="margin-top:4px;font-size:9px;color:var(--muted);line-height:1.6">
        Citation / reference-manager resolution against your local Zotero library
        (<code style="font-family:var(--font-mono)">zotero-mcp</code>).
      </div>
      ${warnHtml}
    </div>`;
  }
  window._renderZoteroStatusRow = _renderZoteroStatusRow;

  // meridian/static/dashboard-notes.ts
  function notesLoadMoreState(inp) {
    const { visibleCount, loadedCount, hasMore, totalCount, remaining, filterActive, pageSize } = inp;
    if (!hasMore) return { show: false, label: "" };
    if (!filterActive) {
      const n2 = remaining > 0 ? Math.min(pageSize, remaining) : pageSize;
      const total = totalCount || loadedCount;
      return { show: true, label: `Load ${n2} more \u2193  (${loadedCount} of ${total})` };
    }
    const shown = visibleCount === 1 ? "1 match" : `${visibleCount} matches`;
    return { show: true, label: `Load more \u2193  (${shown} loaded \u2014 search the rest)` };
  }
  async function loadNotesTab2(projectId) {
    const body = document.getElementById(`notes-body-${projectId}`);
    const searchInput = document.getElementById(`notes-search-${projectId}`);
    const tagSelect = document.getElementById(`notes-tagsel-${projectId}`);
    const kindSelect = document.getElementById(`notes-kindsel-${projectId}`);
    const KIND_STYLE = {
      wiki: { label: "wiki", color: "var(--muted)", border: "var(--border)" },
      insight: { label: "insight", color: "var(--accent)", border: "var(--accent)" },
      reference: { label: "reference", color: "#c9a227", border: "#c9a227" }
    };
    const noteKind = (n2) => {
      const k3 = String(n2.note_kind || "").toLowerCase();
      return KIND_STYLE[k3] ? k3 : "wiki";
    };
    const addTitle = document.getElementById(`notes-add-title-${projectId}`);
    const addBody = document.getElementById(`notes-add-body-${projectId}`);
    const addTags = document.getElementById(`notes-add-tags-${projectId}`);
    const addKind = document.getElementById(`notes-add-kind-${projectId}`);
    const addBtn = document.getElementById(`notes-add-btn-${projectId}`);
    if (!body) return;
    const isAutoCapture = (n2) => {
      const title = String(n2.title || "").trim().toLowerCase();
      const tags = String(n2.tags || "").split(",").map((t3) => t3.trim().toLowerCase());
      return title.startsWith("checkpoint:") || title.startsWith("session summary") || tags.includes("checkpoint") || tags.includes("auto-capture");
    };
    const noteTags = (n2) => String(n2.tags || "").split(",").map((t3) => t3.trim()).filter(Boolean);
    const isArchived = (n2) => String(n2.tags || "").split(",").map((t3) => t3.trim().toLowerCase()).includes("archived");
    const activeTabEl = document.getElementById(`notes-active-tab-${projectId}`);
    const tabButtons = ["notes", "log", "archive"].map(
      (t3) => document.getElementById(`notes-tab-${t3}-${projectId}`)
    );
    const getActiveTab = () => (activeTabEl?.textContent || "notes").trim() || "notes";
    let allNotes = [];
    const NOTES_PAGE = 100;
    let nextCursor = 0;
    let hasMore = false;
    let totalCount = 0;
    let remaining = 0;
    const renderLoadMore = (visibleCount, filterActive) => {
      const existing = document.getElementById(`notes-load-more-${projectId}`);
      if (existing) existing.remove();
      const st = notesLoadMoreState({
        visibleCount,
        loadedCount: allNotes.length,
        hasMore,
        totalCount,
        remaining,
        filterActive,
        pageSize: NOTES_PAGE
      });
      if (!st.show) return;
      const btn = document.createElement("button");
      btn.id = `notes-load-more-${projectId}`;
      btn.className = "secondary";
      btn.style = "width:100%;margin-top:8px;padding:5px;font-size:11px;font-family:var(--font-mono)";
      btn.textContent = st.label;
      btn.onclick = () => loadMore(btn);
      body.appendChild(btn);
    };
    const refreshTagOptions = () => {
      if (!tagSelect) return;
      const prev = tagSelect.value;
      const seen = /* @__PURE__ */ new Set();
      for (const n2 of allNotes) {
        for (const t3 of noteTags(n2)) seen.add(t3);
      }
      const tags = [...seen].sort((a3, b2) => a3.localeCompare(b2));
      tagSelect.innerHTML = `<option value="">all tags</option>` + tags.map((t3) => `<option value="${escapeHtml(t3)}">${escapeHtml(t3)}</option>`).join("");
      if (tags.includes(prev)) tagSelect.value = prev;
    };
    const applyFilters = () => {
      const q2 = (searchInput?.value || "").trim().toLowerCase();
      const selectedTag = (tagSelect?.value || "").trim().toLowerCase();
      const selectedKind = (kindSelect?.value || "").trim().toLowerCase();
      const tab = getActiveTab();
      const filterActive = tab !== "notes" || !!q2 || !!selectedTag || !!selectedKind;
      const visible = allNotes.filter((n2) => {
        if (tab === "log") {
          if (!isAutoCapture(n2)) return false;
        } else if (tab === "archive") {
          if (!isArchived(n2)) return false;
        } else if (isAutoCapture(n2) || isArchived(n2)) return false;
        if (selectedKind && noteKind(n2) !== selectedKind) return false;
        if (selectedTag && !noteTags(n2).map((t3) => t3.toLowerCase()).includes(selectedTag)) return false;
        if (q2) {
          const hay = `${n2.title || ""}
${n2.body || ""}
${n2.tags || ""}`.toLowerCase();
          if (!hay.includes(q2)) return false;
        }
        return true;
      });
      setVtabCountBadge(
        `.notes-vtab-badge[data-pid="${projectId}"]`,
        allNotes.filter((n2) => !isAutoCapture(n2) && !isArchived(n2)).length
      );
      if (!visible.length) {
        const reason = allNotes.length ? `(no notes in this tab match \u2014 clear the search/tag filter or switch tabs)` : `(no notes yet \u2014 use the form below or <code>add_note</code> MCP tool)`;
        body.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">${reason}</div>`;
        renderLoadMore(0, filterActive);
        return;
      }
      body.innerHTML = visible.map((n2) => {
        const pills = noteTags(n2).map(
          (t3) => `<span style="display:inline-block;background:var(--accent)22;color:var(--accent);font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;margin-right:4px">${escapeHtml(t3)}</span>`
        ).join("");
        const dt = (n2.created_at || "").slice(0, 10);
        const kind = noteKind(n2);
        const ks = KIND_STYLE[kind];
        const isInsight = kind === "insight";
        const kindPill = `<span title="note kind: ${ks.label}" style="display:inline-block;background:${ks.color}22;color:${ks.color};font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;margin-right:4px;text-transform:uppercase;letter-spacing:0.4px">${ks.label}</span>`;
        const autoPill = isAutoCapture(n2) ? `<span title="Auto-captured session summary" style="display:inline-block;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;margin-right:4px">session</span>` : "";
        return `<div style="background:var(--surface-2);border:1px solid var(--border);border-left:${isInsight ? "4px" : "3px"} solid ${ks.border};border-radius:0 4px 4px 0;padding:${isInsight ? "12px 14px" : "10px 12px"};margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px">
            <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">
              <span style="color:var(--accent);font-weight:600;font-size:${isInsight ? "13px" : "12px"}">${escapeHtml(n2.title || "")}</span>
              <span style="color:var(--muted);font-size:10px">${escapeHtml(dt)}</span>
            </div>
            <button class="secondary notes-del-btn guest-hidden" data-note-id="${escapeHtml(n2.id)}" style="padding:1px 8px;font-size:10px">Delete</button>
          </div>
          <div style="margin-bottom:6px">${kindPill}${autoPill}${pills}</div>
          <div class="note-body-md" style="color:var(--text);line-height:1.5;font-size:12px">${typeof marked !== "undefined" ? marked.parse(n2.body || "") : escapeHtml(n2.body || "")}</div>
        </div>`;
      }).join("");
      body.querySelectorAll(".notes-del-btn").forEach((btn) => {
        btn.onclick = async () => {
          if (!confirm("Delete this note?")) return;
          try {
            const r3 = await fetch(`/projects/${projectId}/notes/${btn.dataset.noteId}`, { method: "DELETE" });
            if (!r3.ok) throw new Error(`${r3.status}`);
            toast("note deleted");
            await load();
          } catch (e3) {
            toast("delete failed: " + e3.message, true);
          }
        };
      });
      renderLoadMore(visible.length, filterActive);
    };
    const loadMore = async (btn) => {
      if (btn) {
        btn.disabled = true;
        btn.textContent = "loading\u2026";
      }
      try {
        const page = await projectApi(
          projectId,
          `/projects/${projectId}/notes?paginate=true&limit=${NOTES_PAGE}&cursor=${nextCursor}`
        ) || {};
        allNotes = [...allNotes, ...page.notes || []];
        hasMore = !!page.has_more;
        nextCursor = page.next_cursor != null ? page.next_cursor : nextCursor;
        if (page.total_count != null) totalCount = page.total_count;
        if (page.remaining != null) remaining = page.remaining;
        refreshTagOptions();
        applyFilters();
      } catch (e3) {
        if (btn) {
          btn.disabled = false;
          btn.textContent = `Load ${NOTES_PAGE} more \u2193 (retry)`;
        }
        toast("load more failed: " + e3.message, true);
      }
    };
    const load = async () => {
      body.innerHTML = `<div class="empty" style="color:var(--muted)">loading notes\u2026</div>`;
      try {
        const page = await projectApi(
          projectId,
          `/projects/${projectId}/notes?paginate=true&limit=${NOTES_PAGE}&cursor=0`
        ) || {};
        allNotes = page.notes || [];
        hasMore = !!page.has_more;
        nextCursor = page.next_cursor != null ? page.next_cursor : 0;
        totalCount = page.total_count != null ? page.total_count : allNotes.length;
        remaining = page.remaining != null ? page.remaining : 0;
        refreshTagOptions();
        applyFilters();
      } catch (e3) {
        body.innerHTML = renderProjectLoadError(projectId, "Notes unavailable", `/projects/${projectId}/notes`, e3);
        wireProjectLoadRetry(body, projectId);
      }
    };
    if (searchInput) {
      let t3 = null;
      searchInput.oninput = () => {
        clearTimeout(t3);
        t3 = setTimeout(applyFilters, 150);
      };
    }
    if (tagSelect) tagSelect.onchange = applyFilters;
    if (kindSelect) kindSelect.onchange = applyFilters;
    tabButtons.forEach((btn) => {
      if (!btn) return;
      btn.onclick = () => {
        const tab = btn.getAttribute("data-notes-tab") || "notes";
        if (activeTabEl) activeTabEl.textContent = tab;
        tabButtons.forEach((b2) => {
          if (!b2) return;
          const on = b2 === btn;
          b2.style.borderBottom = on ? "2px solid var(--accent)" : "2px solid transparent";
          b2.style.color = on ? "var(--text)" : "var(--muted)";
        });
        applyFilters();
      };
    });
    if (addBtn) addBtn.onclick = async () => {
      const title = (addTitle && addTitle.value || "").trim();
      const text = (addBody && addBody.value || "").trim();
      const tags = (addTags && addTags.value || "").trim();
      if (!title || !text) {
        toast("title and body required", true);
        return;
      }
      if (title.length > 500) {
        toast("Title too long (500 char limit)", true);
        if (addTitle) addTitle.style.borderColor = "var(--red, #f87171)";
        return;
      }
      if (addTitle) addTitle.style.borderColor = "";
      try {
        const res = await api(`/projects/${projectId}/notes`, {
          method: "POST",
          body: JSON.stringify({ title, body: text, tags: tags || void 0, kind: addKind && addKind.value || void 0 })
        });
        if (addTitle) addTitle.value = "";
        if (addBody) addBody.value = "";
        if (addTags) addTags.value = "";
        if (res && res.lint) toast(res.lint, false);
        else toast("note added");
        await load();
      } catch (e3) {
        toast("add failed: " + e3.message, true);
      }
    };
    await load();
  }
  try {
    Object.assign(window, { loadNotesTab: loadNotesTab2 });
  } catch (e3) {
  }

  // meridian/static/dashboard-files.ts
  function _rewriteRepoImages(container, projectId) {
    if (!container || !projectId) return;
    container.querySelectorAll("img").forEach((img) => {
      const src = img.getAttribute("src") || "";
      if (!src) return;
      if (/^https?:\/\//i.test(src) || src.startsWith("data:") || src.startsWith("/")) return;
      const path = src.replace(/^\.\//, "");
      img.setAttribute("src", `/projects/${projectId}/repo-image?path=${encodeURIComponent(path)}`);
      img.setAttribute("loading", "lazy");
    });
  }
  async function loadFilesTab2(projectId) {
    const listEl = document.getElementById(`files-list-${projectId}`);
    if (!listEl) return;
    try {
      const files = await api(`/projects/${projectId}/files`);
      if (!files || !files.length) {
        listEl.innerHTML = `<div style="padding:14px;color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:11px">No editable files found.</div>`;
        return;
      }
      listEl.innerHTML = files.map(
        (f4) => `<div class="file-item" data-filename="${escapeHtml(f4)}">${escapeHtml(f4)}</div>`
      ).join("");
      listEl.querySelectorAll(".file-item").forEach((item) => {
        item.onclick = () => openFileEditor(projectId, item.dataset.filename);
      });
    } catch (e3) {
      listEl.innerHTML = `<div style="padding:14px;color:var(--status-failed);font-family:'IBM Plex Mono',monospace;font-size:11px">Error: ${escapeHtml(e3.message)}</div>`;
    }
  }
  async function openFileEditor(projectId, filename) {
    const browseEl = document.getElementById(`files-browse-${projectId}`);
    const editorEl = document.getElementById(`file-editor-wrap-${projectId}`);
    const nameEl = document.getElementById(`file-name-${projectId}`);
    const contentEl = document.getElementById(`file-content-${projectId}`);
    if (!browseEl || !editorEl || !contentEl || !nameEl) return;
    try {
      const data = await api(`/projects/${projectId}/files/${encodeURIComponent(filename)}`);
      contentEl.value = data.content || "";
      nameEl.textContent = filename;
      browseEl.style.display = "none";
      editorEl.style.display = "flex";
      const editBtn = document.getElementById(`file-mode-edit-${projectId}`);
      const previewBtn = document.getElementById(`file-mode-preview-${projectId}`);
      const previewDiv = document.getElementById(`file-preview-${projectId}`);
      if (editBtn) editBtn.classList.remove("active");
      if (previewBtn) previewBtn.classList.add("active");
      if (previewDiv) {
        const md = data.content || "";
        const html = typeof marked !== "undefined" ? marked.parse(md) : escapeHtml(md);
        previewDiv.innerHTML = html;
        _rewriteRepoImages(previewDiv, projectId);
        previewDiv.style.display = "";
      }
      contentEl.style.display = "none";
      if (editBtn && previewBtn && previewDiv && !editBtn._wired) {
        editBtn._wired = true;
        [editBtn, previewBtn].forEach((btn) => {
          btn.onclick = () => {
            [editBtn, previewBtn].forEach((b2) => b2.classList.toggle("active", b2 === btn));
            if (btn.dataset.fmode === "preview") {
              const md = contentEl.value || "";
              const html = typeof marked !== "undefined" ? marked.parse(md) : escapeHtml(md);
              previewDiv.innerHTML = html;
              _rewriteRepoImages(previewDiv, projectId);
              contentEl.style.display = "none";
              previewDiv.style.display = "";
            } else {
              previewDiv.style.display = "none";
              contentEl.style.display = "";
            }
          };
        });
      }
    } catch (e3) {
      toast("open failed: " + e3.message, true);
    }
  }
  async function saveFile2(projectId) {
    const nameEl = document.getElementById(`file-name-${projectId}`);
    const contentEl = document.getElementById(`file-content-${projectId}`);
    if (!nameEl || !contentEl) return;
    const filename = nameEl.textContent.trim();
    if (!filename) return;
    try {
      await api(`/projects/${projectId}/files/${encodeURIComponent(filename)}`, {
        method: "PUT",
        body: JSON.stringify({ content: contentEl.value })
      });
      toast(`saved ${filename}`);
    } catch (e3) {
      toast("save failed: " + e3.message, true);
    }
  }
  try {
    Object.assign(window, { _rewriteRepoImages, loadFilesTab: loadFilesTab2, openFileEditor, saveFile: saveFile2 });
  } catch (e3) {
  }

  // meridian/static/dashboard-rewind.ts
  function initRewindTab2(projectId) {
    const p3 = window.state.panels[projectId];
    if (!p3) return;
    if (p3.rewindWired) {
      if (p3.rewindDays) loadRewindTab2(projectId, p3.rewindDays);
      return;
    }
    p3.rewindWired = true;
    document.querySelectorAll(`.rewind-day-btn[data-pid="${projectId}"]`).forEach((btn) => {
      btn.onclick = () => {
        const days = parseInt(btn.dataset.days, 10) || 7;
        loadRewindTab2(projectId, days);
      };
    });
    const shareBtn = document.getElementById(`rewind-share-${projectId}`);
    if (shareBtn) shareBtn.onclick = () => copyRewindLink(projectId);
    const searchInp = document.getElementById(`rewind-search-${projectId}`);
    if (searchInp && !searchInp._wired) {
      searchInp._wired = true;
      const wrap = document.getElementById(`rewind-wrap-${projectId}`);
      let _st = null;
      searchInp.addEventListener("input", function() {
        clearTimeout(_st);
        const q2 = this.value.trim();
        _st = setTimeout(async () => {
          if (!q2) {
            if (p3.rewindDays) loadRewindTab2(projectId, p3.rewindDays);
            else {
              if (wrap) wrap.innerHTML = '<div class="empty" style="color:var(--muted)">pick a window above</div>';
            }
            return;
          }
          if (!wrap) return;
          wrap.innerHTML = '<div class="empty" style="color:var(--muted)">searching\u2026</div>';
          try {
            const results = await api(`/projects/${projectId}/search?q=${encodeURIComponent(q2)}&limit=15`);
            wrap.innerHTML = renderSearchResults(q2, results);
          } catch (e3) {
            wrap.innerHTML = `<div class="empty">search failed: ${escapeHtml(e3.message)}</div>`;
          }
        }, 350);
      });
    }
    loadRewindTab2(projectId, 7);
  }
  async function loadRewindTab2(projectId, days) {
    const wrap = document.getElementById(`rewind-wrap-${projectId}`);
    if (!wrap) return;
    const p3 = window.state.panels[projectId];
    if (p3) p3.rewindDays = days;
    document.querySelectorAll(`.rewind-day-btn[data-pid="${projectId}"]`).forEach((b2) => {
      b2.classList.toggle("active", parseInt(b2.dataset.days, 10) === days);
    });
    wrap.innerHTML = '<div style="color:var(--muted)">loading rewind\u2026</div>';
    try {
      const [data, history2, stats] = await Promise.all([
        api(`/projects/${projectId}/rewind?days=${days}`),
        api(`/projects/${projectId}/goal-history`).catch(() => []),
        api(`/projects/${projectId}/stats?days=30`).catch(() => null)
      ]);
      const activeTab = p3 && p3.rewindSubtab || "versions";
      wrap.innerHTML = renderRewindSubtabs(projectId, data, history2, stats, activeTab);
      wrap.querySelectorAll(".rewind-subtab-btn").forEach((btn) => {
        btn.onclick = () => {
          const tab = btn.dataset.tab;
          if (p3) p3.rewindSubtab = tab;
          wrap.querySelectorAll(".rewind-subtab-btn").forEach((b2) => b2.classList.toggle("active", b2.dataset.tab === tab));
          wrap.querySelectorAll(".rewind-subtab-pane").forEach((c3) => {
            c3.style.display = c3.dataset.tab === tab ? "" : "none";
          });
          if (tab === "charts") initRewindCharts(projectId, stats);
        };
      });
      if (activeTab === "charts" && stats) initRewindCharts(projectId, stats);
    } catch (e3) {
      wrap.innerHTML = `<div style="color:var(--status-failed)">rewind failed: ${escapeHtml(e3.message)}</div>`;
    }
  }
  function renderRewindSubtabs(projectId, data, history2, stats, activeTab) {
    const tabs = [
      { id: "versions", label: "\u{1F4E6} Milestones" },
      { id: "sprint", label: "\u26A1 Sprint items" },
      { id: "goals", label: "\u{1F3AF} Goal" },
      { id: "activity", label: "\u{1F4CB} Activity" },
      { id: "charts", label: "\u{1F4CA} Charts" }
    ];
    const tabBar = `<div class="rewind-subtab-bar">${tabs.map((t3) => `<button class="rewind-subtab-btn${activeTab === t3.id ? " active" : ""}" data-tab="${t3.id}">${t3.label}</button>`).join("")}</div>`;
    const make = (id, html) => `<div class="rewind-subtab-pane" data-tab="${id}" style="${activeTab === id ? "" : "display:none"}">${html}</div>`;
    return tabBar + make("activity", renderRewindActivity(projectId, data)) + make("versions", renderRewindVersions(projectId, data)) + make("sprint", renderRewindSprint(projectId, data)) + make("goals", renderRewindGoals(projectId, data, history2)) + make("charts", renderRewindCharts(projectId, stats));
  }
  function renderRewindCharts(projectId, stats) {
    if (!stats) {
      return '<div style="padding:14px;color:var(--muted);font-size:11px">Charts unavailable \u2014 stats endpoint not reachable.</div>';
    }
    const legendStyle = "display:flex;gap:14px;margin-top:6px;font-size:10px;color:var(--muted);font-family:var(--font-mono)";
    const swatch = (color) => `<span style="display:inline-block;width:12px;height:12px;background:${color};border-radius:2px;margin-right:4px;vertical-align:middle"></span>`;
    return `<div style="padding:8px 0">

    <div style="color:var(--accent);font-weight:600;font-size:11px;margin-bottom:8px">\u{1F4CA} Sprint items / day (last ${stats.period_days}d)</div>

    <canvas id="chart-tasks-${escapeHtml(projectId)}" style="max-width:100%;max-height:160px"></canvas>

    <div style="${legendStyle}"><span>${swatch("rgba(96,165,250,0.7)")}Sprint items</span></div>

    <div style="color:var(--accent);font-weight:600;font-size:11px;margin:18px 0 8px">\u26A1 Session task completion % by version</div>

    <canvas id="chart-sprint-${escapeHtml(projectId)}" style="max-width:100%;max-height:120px"></canvas>

    <div style="${legendStyle}">

      <span>${swatch("rgba(52,211,153,0.7)")}100% done</span>

      <span>${swatch("rgba(96,165,250,0.7)")}Partial</span>

    </div>

    <div style="color:var(--accent);font-weight:600;font-size:11px;margin:18px 0 8px">\u{1F5C2}\uFE0F Activity by domain / day (last ${stats.period_days}d)</div>

    <canvas id="chart-activity-${escapeHtml(projectId)}" style="max-width:100%;max-height:160px"></canvas>

    <div style="font-size:9px;color:var(--muted);margin-top:4px">docs/web/experiment/code/citation pointers touched each day \u2014 daily aggregate totals only.</div>

  </div>`;
  }
  function initRewindCharts(projectId, stats) {
    if (!stats || typeof Chart === "undefined") return;
    const p3 = window.state.panels[projectId];
    if (p3) {
      if (p3._chartTasks) {
        p3._chartTasks.destroy();
        p3._chartTasks = null;
      }
      if (p3._chartSprint) {
        p3._chartSprint.destroy();
        p3._chartSprint = null;
      }
      if (p3._chartActivity) {
        p3._chartActivity.destroy();
        p3._chartActivity = null;
      }
    }
    const tasksCanvas = document.getElementById(`chart-tasks-${projectId}`);
    const sprintItemsPerDay = stats.sprint_items_per_day || stats.tasks_per_day;
    if (tasksCanvas && sprintItemsPerDay) {
      const labels = sprintItemsPerDay.map((d3) => d3.day.slice(5));
      const totals = sprintItemsPerDay.map((d3) => d3.total);
      const chart = new Chart(tasksCanvas, {
        type: "bar",
        data: {
          labels,
          datasets: [{
            label: "sprint items",
            data: totals,
            backgroundColor: "rgba(96, 165, 250, 0.7)",
            borderRadius: 2
          }]
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#9ca3af", font: { size: 9 }, maxRotation: 45 }, grid: { color: "#1f2937" } },
            y: { ticks: { color: "#9ca3af", font: { size: 9 }, stepSize: 1 }, grid: { color: "#1f2937" }, beginAtZero: true }
          }
        }
      });
      if (p3) p3._chartTasks = chart;
    }
    const sprintCanvas = document.getElementById(`chart-sprint-${projectId}`);
    if (sprintCanvas && stats.sprint_velocity && stats.sprint_velocity.length) {
      const sv = stats.sprint_velocity;
      const chart = new Chart(sprintCanvas, {
        type: "bar",
        data: {
          labels: sv.map((v3) => v3.version),
          datasets: [{
            label: "% done",
            data: sv.map((v3) => v3.pct),
            backgroundColor: sv.map((v3) => v3.pct === 100 ? "rgba(52, 211, 153, 0.7)" : "rgba(96, 165, 250, 0.7)"),
            borderRadius: 2
          }]
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#9ca3af", font: { size: 9 } }, grid: { color: "#1f2937" } },
            y: {
              ticks: { color: "#9ca3af", font: { size: 9 } },
              grid: { color: "#1f2937" },
              min: 0,
              max: 100,
              title: { display: true, text: "% done", color: "#9ca3af", font: { size: 9 } }
            }
          }
        }
      });
      if (p3) p3._chartSprint = chart;
    }
    const activityCanvas = document.getElementById(`chart-activity-${projectId}`);
    const abd = stats.activity_by_domain;
    const domains = stats.activity_domains || [];
    if (activityCanvas && abd && domains.length) {
      const labels = abd.map((d3) => String(d3.day).slice(5));
      const PALETTE2 = {
        code: "rgba(96,165,250,0.75)",
        docs: "rgba(52,211,153,0.75)",
        web: "rgba(217,119,6,0.75)",
        experiment: "rgba(167,139,250,0.75)",
        citation: "rgba(244,114,182,0.75)",
        other: "rgba(156,163,175,0.6)"
      };
      const datasets = domains.map((dom, i3) => ({
        label: dom,
        data: abd.map((d3) => (d3.by_domain || {})[dom] || 0),
        backgroundColor: PALETTE2[dom] || `hsl(${i3 * 67 % 360} 60% 60% / 0.75)`,
        borderRadius: 2,
        stack: "activity"
      }));
      const chart = new Chart(activityCanvas, {
        type: "bar",
        data: { labels, datasets },
        options: {
          responsive: true,
          plugins: { legend: { display: true, position: "bottom", labels: { color: "#9ca3af", font: { size: 9 }, boxWidth: 10 } } },
          scales: {
            x: { stacked: true, ticks: { color: "#9ca3af", font: { size: 9 }, maxRotation: 45 }, grid: { color: "#1f2937" } },
            y: { stacked: true, ticks: { color: "#9ca3af", font: { size: 9 }, stepSize: 1 }, grid: { color: "#1f2937" }, beginAtZero: true }
          }
        }
      });
      if (p3) p3._chartActivity = chart;
    }
  }
  function renderRewindSprint(projectId, data) {
    const items = data.sprint_items_completed || [];
    const pending = data.sprint_items_pending || [];
    const allItems = [...items, ...pending];
    if (!allItems.length) {
      return '<div style="padding:14px;color:var(--muted);font-size:11px">No sprint items yet.</div>';
    }
    const byVersion = {};
    allItems.forEach((s3) => {
      const v3 = s3.version || "current";
      if (!byVersion[v3]) byVersion[v3] = [];
      byVersion[v3].push(s3);
    });
    const statusDot = (s3) => {
      if (s3.status === "done") return '<span style="color:var(--status-done)">\u2713</span>';
      if (s3.status === "failed") return '<span style="color:var(--status-failed)">\u2717</span>';
      if (s3.status === "pushed") return '<span style="color:var(--muted)">\u2192</span>';
      return '<span style="color:var(--status-pending)">\u25CB</span>';
    };
    let html = "";
    try {
      const bars = ganttBars(allItems);
      if (bars.length) {
        html += `<div class="rewind-gantt" style="margin:8px 0;padding:8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px"><div style="font-size:10px;color:var(--muted);margin-bottom:6px">\u23F1 Execution timeline (${bars.length} items)</div>` + bars.map(
          (b2) => `<div style="position:relative;height:14px;margin-bottom:2px"><div title="${escapeHtml(b2.title)}" style="position:absolute;left:${b2.leftPct}%;width:${b2.widthPct}%;height:12px;background:${sprintStatusColor(b2.status)};border-radius:2px;overflow:hidden;white-space:nowrap;font-size:8px;line-height:12px;color:#0b0e14;padding:0 3px;box-sizing:border-box">${escapeHtml(b2.title)}</div></div>`
        ).join("") + "</div>";
      }
    } catch (e3) {
    }
    const versions = Object.keys(byVersion).sort((a3, b2) => {
      if (a3 === "current") return -1;
      if (b2 === "current") return 1;
      return b2.localeCompare(a3);
    });
    versions.forEach((v3) => {
      const vItems = byVersion[v3];
      const doneCount = vItems.filter((s3) => s3.status === "done").length;
      const total = vItems.length;
      const pct = total ? Math.round(doneCount / total * 100) : 0;
      const id = `sprint-v-${projectId}-${v3.replace(/[^a-z0-9]/gi, "")}`;
      html += `<section style="margin-bottom:10px">

      <div style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border)" onclick="toggleExpand('${id}')">

        <span style="color:var(--accent);font-weight:600;font-size:11px">${escapeHtml(v3)}</span>

        <span style="color:var(--muted);font-size:10px">${doneCount}/${total} done (${pct}%) <span class="expand-arrow">\u25B6</span></span>

      </div>

      <div id="${id}" style="display:${v3 === "current" ? "block" : "none"}">

        ${vItems.map((s3) => `<div style="padding:3px 0 3px 8px;font-size:11px;display:flex;gap:6px;align-items:flex-start">

          ${statusDot(s3)}

          <span style="color:${s3.status === "done" ? "var(--muted)" : "var(--text)"};${s3.status === "done" ? "text-decoration:line-through" : ""}">${escapeHtml(s3.title || "")}</span>

        </div>`).join("")}

      </div>

    </section>`;
    });
    return `<div style="padding:8px 0">${html}</div>`;
  }
  function _rewindSec(icon, title, items, render) {
    if (!items || !items.length) {
      return `<section style="margin-bottom:14px">

      <div style="color:var(--accent);font-weight:600;margin-bottom:4px">${icon} ${title}</div>

      <div style="color:var(--muted);font-size:10px">(none)</div>

    </section>`;
    }
    return `<section style="margin-bottom:14px">

    <div style="color:var(--accent);font-weight:600;margin-bottom:4px">${icon} ${title}</div>

    ${items.map(render).join("")}

  </section>`;
  }
  function renderRewindActivity(projectId, data) {
    const sessByName = /* @__PURE__ */ new Map();
    (data.session_summaries || []).forEach((s3) => {
      const prev = sessByName.get(s3.session_name);
      if (!prev || (s3.tasks_completed || 0) > (prev.tasks_completed || 0)) {
        sessByName.set(s3.session_name, s3);
      }
    });
    const dedupedSessions = [...sessByName.values()];
    const sessions = _rewindSec("\u{1F9E0}", "Sessions", dedupedSessions, (s3) => `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">

      <div style="color:var(--accent)">${escapeHtml(s3.session_name)} <span style="color:var(--muted);font-size:10px">\xB7 ${s3.tasks_completed} done</span></div>

      <div style="color:var(--muted);font-size:10px">${escapeHtml(s3.summary || "")}</div>

    </div>`);
    const decisions = _rewindSec("\u{1F4CB}", "Decisions logged", data.decisions_logged, (d3) => `<div style="padding:2px 0"><span style="color:var(--muted);font-size:10px">[${escapeHtml(d3.logged_at || "")}]</span> ${escapeHtml(d3.text || "")}</div>`);
    const byStatus = data.tasks_by_status || {};
    const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">

    <div style="color:var(--accent);font-weight:600">\u{1F4CA} Tasks: ${byStatus.done || 0} done, ${byStatus.failed || 0} failed, ${byStatus.pending || 0} pending <span style="color:var(--muted);font-size:10px">(${data.tasks_total || 0} total over ${data.period_days}d)</span></div>

  </section>`;
    return sessions + decisions + summary;
  }
  function renderRewindVersions(projectId, data) {
    const versions = _rewindSec(
      "\u{1F4E6}",
      "Milestones shipped",
      data.versions_shipped,
      (v3) => `<div style="padding:5px 0;border-bottom:1px solid var(--border);font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.6;color:var(--text)">${escapeHtml(v3)}</div>`
    );
    const sprints = _rewindSec("\u2705", "Sprint items completed", data.sprint_items_completed, (s3) => `<div style="padding:2px 0"><span style="color:var(--accent-green)">${escapeHtml(s3.version || "")}</span> \u2014 ${escapeHtml(s3.title || "")} <span style="color:var(--muted);font-size:10px">${escapeHtml(s3.completed_at || "")}</span></div>`);
    const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">

    <div style="color:var(--accent);font-weight:600">\u{1F4CA} ${(data.sprint_items_completed || []).length} sprint items completed over ${data.period_days}d</div>

  </section>`;
    return versions + sprints + summary;
  }
  function renderRewindGoals(projectId, data, history2) {
    const preStyle = "margin:0;white-space:pre-wrap;word-break:break-word;background:var(--bg-card);padding:6px;border-radius:3px;font-size:10px;font-family:inherit";
    const goals = _rewindSec("\u{1F3AF}", "Goal changes", (data.goal_changes || []).slice().reverse(), (g2, idx) => {
      const id = `gc-expand-${projectId}-${idx}`;
      return `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">

      <div style="cursor:pointer;user-select:none" onclick="toggleExpand('${id}')">

        <div style="color:var(--muted);font-size:10px">${escapeHtml(g2.field)} \xB7 ${escapeHtml(g2.changed_at || "")} <span class="expand-arrow" style="font-size:9px">\u25B6</span></div>

        <div style="color:var(--text);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((g2.new_summary || "(empty)").slice(0, 120))}</div>

      </div>

      <div id="${id}" style="display:none;margin-top:6px;overflow:visible;max-height:none">

        <div style="color:var(--muted);font-size:10px;margin-bottom:2px">before:</div>

        <pre style="${preStyle};margin-bottom:6px">${escapeHtml(g2.old_full || "(empty)")}</pre>

        <div style="color:var(--muted);font-size:10px;margin-bottom:2px">after:</div>

        <pre style="${preStyle}">${escapeHtml(g2.new_full || "(empty)")}</pre>

      </div>

    </div>`;
    });
    let historyHtml = "";
    if (history2 && history2.length) {
      const rows = [...history2].reverse().map((v3, idx) => {
        const id = `gv-expand-${projectId}-${idx}`;
        const raw = (v3.version_goal || v3.north_star || "").replace(/\s+/g, " ").trim();
        const snippet = raw.length > 80 ? raw.slice(0, 79) + "\u2026" : raw;
        return `<div style="border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">

        <div style="cursor:pointer;user-select:none" onclick="toggleExpand('${id}')" title="${escapeHtml(raw)}">

          <span style="color:var(--accent)">v${v3.version}</span>

          <span style="color:var(--muted);font-size:10px"> \xB7 ${escapeHtml(v3.created_at || "")}</span>

          <span> ${escapeHtml(snippet)}</span>

          <span class="expand-arrow" style="color:var(--muted);font-size:9px"> \u25B6</span>

        </div>

        <div id="${id}" style="display:none;margin-top:6px">

          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">north_star:</div>

          <pre style="${preStyle};margin-bottom:6px">${escapeHtml(v3.north_star || "(empty)")}</pre>

          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">version_goal:</div>

          <pre style="${preStyle};margin-bottom:6px">${escapeHtml(v3.version_goal || "(empty)")}</pre>

          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">sprint:</div>

          <pre style="${preStyle}">${escapeHtml(v3.sprint || "(empty)")}</pre>

        </div>

      </div>`;
      });
      historyHtml = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">

      <div style="color:var(--accent);font-weight:600;margin-bottom:6px">\u{1F4DC} Goal version history (${history2.length} versions, newest first)</div>

      ${rows.join("")}

    </section>`;
    }
    return goals + historyHtml;
  }
  async function copyRewindLink(projectId) {
    const p3 = window.state.panels[projectId];
    const days = p3 && p3.rewindDays || 7;
    try {
      const res = await api(`/projects/${projectId}/rewind-token`, { method: "POST" });
      let base = "";
      try {
        const cfg = await api("/config");
        base = cfg.server_url || window.location.origin;
      } catch (_2) {
        base = window.location.origin;
      }
      const url = `${base}/projects/${projectId}/rewind?days=${days}&token=${encodeURIComponent(res.token)}`;
      try {
        await navigator.clipboard.writeText(url);
        toast("shareable link copied");
      } catch (_2) {
        window.prompt("copy this URL", url);
      }
    } catch (e3) {
      toast("share failed: " + e3.message);
    }
  }
  try {
    Object.assign(window, { initRewindTab: initRewindTab2, loadRewindTab: loadRewindTab2, renderRewindSubtabs, renderRewindCharts, initRewindCharts, renderRewindSprint, _rewindSec, renderRewindActivity, renderRewindVersions, renderRewindGoals, copyRewindLink });
  } catch (e3) {
  }

  // meridian/static/dashboard-blog.ts
  var _BLOG_STATUSES = ["draft", "published", "archived"];
  function blogEditorFormHtml(projectId, post) {
    const p3 = post || {};
    const id = String(p3.id || "");
    const title = String(p3.title || "");
    const body = String(p3.body_md || "");
    const status = _BLOG_STATUSES.includes(String(p3.status)) ? String(p3.status) : "draft";
    const editing = !!id;
    const pid = escapeHtml(String(projectId));
    const opts = _BLOG_STATUSES.map((s3) => `<option value="${s3}"${s3 === status ? " selected" : ""}>${s3}</option>`).join("");
    return `<div id="blog-editor-${pid}" style="border:1px solid var(--border);border-radius:4px;padding:10px;margin-bottom:14px;background:var(--surface-1)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span id="blog-editor-title-${pid}" style="font-size:10px;color:var(--accent);letter-spacing:.06em">${editing ? "EDIT POST" : "NEW POST"}</span>
      <button id="blog-editor-reset-${pid}" class="secondary" style="font-size:9px;padding:2px 8px;${editing ? "" : "display:none"}">New / clear</button>
    </div>
    <input type="hidden" id="blog-editor-id-${pid}" value="${escapeHtml(id)}" />
    <input type="text" id="blog-editor-title-input-${pid}" placeholder="Title" value="${escapeHtml(title)}" style="width:100%;box-sizing:border-box;font-size:11px;padding:5px 7px;margin-bottom:6px;background:var(--surface-0,var(--bg));color:var(--text);border:1px solid var(--border);border-radius:3px" />
    <textarea id="blog-editor-body-${pid}" placeholder="Body (markdown)" rows="6" style="width:100%;box-sizing:border-box;font-size:11px;font-family:var(--font-mono);padding:5px 7px;margin-bottom:6px;background:var(--surface-0,var(--bg));color:var(--text);border:1px solid var(--border);border-radius:3px;resize:vertical">${escapeHtml(body)}</textarea>
    <div style="display:flex;gap:6px;align-items:center">
      <select id="blog-editor-status-${pid}" style="font-size:10px;padding:3px 6px;background:var(--surface-0,var(--bg));color:var(--text);border:1px solid var(--border);border-radius:3px">${opts}</select>
      <button id="blog-editor-save-${pid}" style="font-size:10px;padding:4px 12px">${editing ? "Update post" : "Save post"}</button>
      <span id="blog-editor-status-msg-${pid}" style="font-size:9px;color:var(--muted)"></span>
    </div>
  </div>`;
  }
  function blogPostCardHtml(post) {
    const id = String(post.id || "");
    const slug = String(post.slug || "");
    const url = String(post.url || (slug ? `/blog/${slug}` : ""));
    const title = String(post.title || "Untitled");
    return `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
      <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(title)}</div>
      <button class="blog-edit-btn secondary" data-blog-id="${escapeHtml(id)}" style="font-size:9px;padding:2px 8px;flex:0 0 auto">Edit</button>
    </div>
    <div style="font-size:9px;color:var(--muted);font-family:var(--font-mono);margin-top:3px">${escapeHtml(slug)}</div>
    ${url ? `<div style="margin-top:4px"><a href="${escapeHtml(url)}" target="_blank" rel="noopener" style="font-size:10px;color:var(--accent)">${escapeHtml(url)}</a></div>` : ""}
  </div>`;
  }
  function populateBlogEditor(projectId, post) {
    const pid = String(projectId);
    const idEl = document.getElementById(`blog-editor-id-${pid}`);
    const titleEl = document.getElementById(`blog-editor-title-input-${pid}`);
    const bodyEl = document.getElementById(`blog-editor-body-${pid}`);
    const statusEl = document.getElementById(`blog-editor-status-${pid}`);
    const labelEl = document.getElementById(`blog-editor-title-${pid}`);
    const saveEl = document.getElementById(`blog-editor-save-${pid}`);
    const resetEl = document.getElementById(`blog-editor-reset-${pid}`);
    if (!idEl || !titleEl || !bodyEl) return false;
    idEl.value = String(post.id || "");
    titleEl.value = String(post.title || "");
    bodyEl.value = String(post.body_md || "");
    if (statusEl) {
      const st = _BLOG_STATUSES.includes(String(post.status)) ? String(post.status) : "draft";
      statusEl.value = st;
    }
    if (labelEl) labelEl.textContent = "EDIT POST";
    if (saveEl) saveEl.textContent = "Update post";
    if (resetEl) resetEl.style.display = "";
    return true;
  }
  function resetBlogEditor(projectId) {
    const pid = String(projectId);
    const idEl = document.getElementById(`blog-editor-id-${pid}`);
    const titleEl = document.getElementById(`blog-editor-title-input-${pid}`);
    const bodyEl = document.getElementById(`blog-editor-body-${pid}`);
    const statusEl = document.getElementById(`blog-editor-status-${pid}`);
    const labelEl = document.getElementById(`blog-editor-title-${pid}`);
    const saveEl = document.getElementById(`blog-editor-save-${pid}`);
    const resetEl = document.getElementById(`blog-editor-reset-${pid}`);
    if (idEl) idEl.value = "";
    if (titleEl) titleEl.value = "";
    if (bodyEl) bodyEl.value = "";
    if (statusEl) statusEl.value = "draft";
    if (labelEl) labelEl.textContent = "NEW POST";
    if (saveEl) saveEl.textContent = "Save post";
    if (resetEl) resetEl.style.display = "none";
  }
  try {
    Object.assign(window, {
      blogEditorFormHtml,
      blogPostCardHtml,
      populateBlogEditor,
      resetBlogEditor
    });
  } catch (e3) {
  }

  // ../../../node_modules/preact/dist/preact.module.js
  var n;
  var l;
  var u;
  var t;
  var i;
  var r;
  var o;
  var e;
  var f;
  var c;
  var a;
  var s;
  var h;
  var p;
  var v;
  var y;
  var d = {};
  var w = [];
  var _ = /acit|ex(?:s|g|n|p|$)|rph|grid|ows|mnc|ntw|ine[ch]|zoo|^ord|itera/i;
  var g = Array.isArray;
  function m(n2, l3) {
    for (var u4 in l3) n2[u4] = l3[u4];
    return n2;
  }
  function b(n2) {
    n2 && n2.parentNode && n2.parentNode.removeChild(n2);
  }
  function k(l3, u4, t3) {
    var i3, r3, o3, e3 = {};
    for (o3 in u4) "key" == o3 ? i3 = u4[o3] : "ref" == o3 ? r3 = u4[o3] : e3[o3] = u4[o3];
    if (arguments.length > 2 && (e3.children = arguments.length > 3 ? n.call(arguments, 2) : t3), "function" == typeof l3 && null != l3.defaultProps) for (o3 in l3.defaultProps) void 0 === e3[o3] && (e3[o3] = l3.defaultProps[o3]);
    return x(l3, e3, i3, r3, null);
  }
  function x(n2, t3, i3, r3, o3) {
    var e3 = { type: n2, props: t3, key: i3, ref: r3, __k: null, __: null, __b: 0, __e: null, __c: null, constructor: void 0, __v: null == o3 ? ++u : o3, __i: -1, __u: 0 };
    return null == o3 && null != l.vnode && l.vnode(e3), e3;
  }
  function S(n2) {
    return n2.children;
  }
  function C(n2, l3) {
    this.props = n2, this.context = l3;
  }
  function $(n2, l3) {
    if (null == l3) return n2.__ ? $(n2.__, n2.__i + 1) : null;
    for (var u4; l3 < n2.__k.length; l3++) if (null != (u4 = n2.__k[l3]) && null != u4.__e) return u4.__e;
    return "function" == typeof n2.type ? $(n2) : null;
  }
  function I(n2) {
    if (n2.__P && n2.__d) {
      var u4 = n2.__v, t3 = u4.__e, i3 = [], r3 = [], o3 = m({}, u4);
      o3.__v = u4.__v + 1, l.vnode && l.vnode(o3), q(n2.__P, o3, u4, n2.__n, n2.__P.namespaceURI, 32 & u4.__u ? [t3] : null, i3, null == t3 ? $(u4) : t3, !!(32 & u4.__u), r3), o3.__v = u4.__v, o3.__.__k[o3.__i] = o3, D(i3, o3, r3), u4.__e = u4.__ = null, o3.__e != t3 && P(o3);
    }
  }
  function P(n2) {
    if (null != (n2 = n2.__) && null != n2.__c) return n2.__e = n2.__c.base = null, n2.__k.some(function(l3) {
      if (null != l3 && null != l3.__e) return n2.__e = n2.__c.base = l3.__e;
    }), P(n2);
  }
  function A(n2) {
    (!n2.__d && (n2.__d = true) && i.push(n2) && !H.__r++ || r != l.debounceRendering) && ((r = l.debounceRendering) || o)(H);
  }
  function H() {
    try {
      for (var n2, l3 = 1; i.length; ) i.length > l3 && i.sort(e), n2 = i.shift(), l3 = i.length, I(n2);
    } finally {
      i.length = H.__r = 0;
    }
  }
  function L(n2, l3, u4, t3, i3, r3, o3, e3, f4, c3, a3) {
    var s3, h3, p3, v3, y3, _2, g2, m3 = t3 && t3.__k || w, b2 = l3.length;
    for (f4 = T(u4, l3, m3, f4, b2), s3 = 0; s3 < b2; s3++) null != (p3 = u4.__k[s3]) && (h3 = -1 != p3.__i && m3[p3.__i] || d, p3.__i = s3, _2 = q(n2, p3, h3, i3, r3, o3, e3, f4, c3, a3), v3 = p3.__e, p3.ref && h3.ref != p3.ref && (h3.ref && J(h3.ref, null, p3), a3.push(p3.ref, p3.__c || v3, p3)), null == y3 && null != v3 && (y3 = v3), (g2 = !!(4 & p3.__u)) || h3.__k === p3.__k ? (f4 = j(p3, f4, n2, g2), g2 && h3.__e && (h3.__e = null)) : "function" == typeof p3.type && void 0 !== _2 ? f4 = _2 : v3 && (f4 = v3.nextSibling), p3.__u &= -7);
    return u4.__e = y3, f4;
  }
  function T(n2, l3, u4, t3, i3) {
    var r3, o3, e3, f4, c3, a3 = u4.length, s3 = a3, h3 = 0;
    for (n2.__k = new Array(i3), r3 = 0; r3 < i3; r3++) null != (o3 = l3[r3]) && "boolean" != typeof o3 && "function" != typeof o3 ? ("string" == typeof o3 || "number" == typeof o3 || "bigint" == typeof o3 || o3.constructor == String ? o3 = n2.__k[r3] = x(null, o3, null, null, null) : g(o3) ? o3 = n2.__k[r3] = x(S, { children: o3 }, null, null, null) : void 0 === o3.constructor && o3.__b > 0 ? o3 = n2.__k[r3] = x(o3.type, o3.props, o3.key, o3.ref ? o3.ref : null, o3.__v) : n2.__k[r3] = o3, f4 = r3 + h3, o3.__ = n2, o3.__b = n2.__b + 1, e3 = null, -1 != (c3 = o3.__i = O(o3, u4, f4, s3)) && (s3--, (e3 = u4[c3]) && (e3.__u |= 2)), null == e3 || null == e3.__v ? (-1 == c3 && (i3 > a3 ? h3-- : i3 < a3 && h3++), "function" != typeof o3.type && (o3.__u |= 4)) : c3 != f4 && (c3 == f4 - 1 ? h3-- : c3 == f4 + 1 ? h3++ : (c3 > f4 ? h3-- : h3++, o3.__u |= 4))) : n2.__k[r3] = null;
    if (s3) for (r3 = 0; r3 < a3; r3++) null != (e3 = u4[r3]) && 0 == (2 & e3.__u) && (e3.__e == t3 && (t3 = $(e3)), K(e3, e3));
    return t3;
  }
  function j(n2, l3, u4, t3) {
    var i3, r3;
    if ("function" == typeof n2.type) {
      for (i3 = n2.__k, r3 = 0; i3 && r3 < i3.length; r3++) i3[r3] && (i3[r3].__ = n2, l3 = j(i3[r3], l3, u4, t3));
      return l3;
    }
    n2.__e != l3 && (t3 && (l3 && n2.type && !l3.parentNode && (l3 = $(n2)), u4.insertBefore(n2.__e, l3 || null)), l3 = n2.__e);
    do {
      l3 = l3 && l3.nextSibling;
    } while (null != l3 && 8 == l3.nodeType);
    return l3;
  }
  function O(n2, l3, u4, t3) {
    var i3, r3, o3, e3 = n2.key, f4 = n2.type, c3 = l3[u4], a3 = null != c3 && 0 == (2 & c3.__u);
    if (null === c3 && null == e3 || a3 && e3 == c3.key && f4 == c3.type) return u4;
    if (t3 > (a3 ? 1 : 0)) {
      for (i3 = u4 - 1, r3 = u4 + 1; i3 >= 0 || r3 < l3.length; ) if (null != (c3 = l3[o3 = i3 >= 0 ? i3-- : r3++]) && 0 == (2 & c3.__u) && e3 == c3.key && f4 == c3.type) return o3;
    }
    return -1;
  }
  function z(n2, l3, u4) {
    "-" == l3[0] ? n2.setProperty(l3, null == u4 ? "" : u4) : n2[l3] = null == u4 ? "" : "number" != typeof u4 || _.test(l3) ? u4 : u4 + "px";
  }
  function N(n2, l3, u4, t3, i3) {
    var r3, o3;
    n: if ("style" == l3) if ("string" == typeof u4) n2.style.cssText = u4;
    else {
      if ("string" == typeof t3 && (n2.style.cssText = t3 = ""), t3) for (l3 in t3) u4 && l3 in u4 || z(n2.style, l3, "");
      if (u4) for (l3 in u4) t3 && u4[l3] == t3[l3] || z(n2.style, l3, u4[l3]);
    }
    else if ("o" == l3[0] && "n" == l3[1]) r3 = l3 != (l3 = l3.replace(s, "$1")), o3 = l3.toLowerCase(), l3 = o3 in n2 || "onFocusOut" == l3 || "onFocusIn" == l3 ? o3.slice(2) : l3.slice(2), n2.l || (n2.l = {}), n2.l[l3 + r3] = u4, u4 ? t3 ? u4[a] = t3[a] : (u4[a] = h, n2.addEventListener(l3, r3 ? v : p, r3)) : n2.removeEventListener(l3, r3 ? v : p, r3);
    else {
      if ("http://www.w3.org/2000/svg" == i3) l3 = l3.replace(/xlink(H|:h)/, "h").replace(/sName$/, "s");
      else if ("width" != l3 && "height" != l3 && "href" != l3 && "list" != l3 && "form" != l3 && "tabIndex" != l3 && "download" != l3 && "rowSpan" != l3 && "colSpan" != l3 && "role" != l3 && "popover" != l3 && l3 in n2) try {
        n2[l3] = null == u4 ? "" : u4;
        break n;
      } catch (n3) {
      }
      "function" == typeof u4 || (null == u4 || false === u4 && "-" != l3[4] ? n2.removeAttribute(l3) : n2.setAttribute(l3, "popover" == l3 && 1 == u4 ? "" : u4));
    }
  }
  function V(n2) {
    return function(u4) {
      if (this.l) {
        var t3 = this.l[u4.type + n2];
        if (null == u4[c]) u4[c] = h++;
        else if (u4[c] < t3[a]) return;
        return t3(l.event ? l.event(u4) : u4);
      }
    };
  }
  function q(n2, u4, t3, i3, r3, o3, e3, f4, c3, a3) {
    var s3, h3, p3, v3, y3, d3, _2, k3, x2, M, $2, I2, P2, A3, H2, T3, j3 = u4.type;
    if (void 0 !== u4.constructor) return null;
    128 & t3.__u && (c3 = !!(32 & t3.__u), o3 = [f4 = u4.__e = t3.__e]), (s3 = l.__b) && s3(u4);
    n: if ("function" == typeof j3) {
      h3 = e3.length;
      try {
        if (x2 = u4.props, M = j3.prototype && j3.prototype.render, $2 = (s3 = j3.contextType) && i3[s3.__c], I2 = s3 ? $2 ? $2.props.value : s3.__ : i3, t3.__c ? k3 = (p3 = u4.__c = t3.__c).__ = p3.__E : (M ? u4.__c = p3 = new j3(x2, I2) : (u4.__c = p3 = new C(x2, I2), p3.constructor = j3, p3.render = Q), $2 && $2.sub(p3), p3.state || (p3.state = {}), p3.__n = i3, v3 = p3.__d = true, p3.__h = [], p3._sb = []), M && null == p3.__s && (p3.__s = p3.state), M && null != j3.getDerivedStateFromProps && (p3.__s == p3.state && (p3.__s = m({}, p3.__s)), m(p3.__s, j3.getDerivedStateFromProps(x2, p3.__s))), y3 = p3.props, d3 = p3.state, p3.__v = u4, v3) M && null == j3.getDerivedStateFromProps && null != p3.componentWillMount && p3.componentWillMount(), M && null != p3.componentDidMount && p3.__h.push(p3.componentDidMount);
        else {
          if (M && null == j3.getDerivedStateFromProps && x2 !== y3 && null != p3.componentWillReceiveProps && p3.componentWillReceiveProps(x2, I2), u4.__v == t3.__v || !p3.__e && null != p3.shouldComponentUpdate && false === p3.shouldComponentUpdate(x2, p3.__s, I2)) {
            u4.__v != t3.__v && (p3.props = x2, p3.state = p3.__s, p3.__d = false), u4.__e = t3.__e, u4.__k = t3.__k, u4.__k.some(function(n3) {
              n3 && (n3.__ = u4);
            }), w.push.apply(p3.__h, p3._sb), p3._sb = [], p3.__h.length && e3.push(p3);
            break n;
          }
          null != p3.componentWillUpdate && p3.componentWillUpdate(x2, p3.__s, I2), M && null != p3.componentDidUpdate && p3.__h.push(function() {
            p3.componentDidUpdate(y3, d3, _2);
          });
        }
        if (p3.context = I2, p3.props = x2, p3.__P = n2, p3.__e = false, P2 = l.__r, A3 = 0, M) p3.state = p3.__s, p3.__d = false, P2 && P2(u4), s3 = p3.render(p3.props, p3.state, p3.context), w.push.apply(p3.__h, p3._sb), p3._sb = [];
        else do {
          p3.__d = false, P2 && P2(u4), s3 = p3.render(p3.props, p3.state, p3.context), p3.state = p3.__s;
        } while (p3.__d && ++A3 < 25);
        p3.state = p3.__s, null != p3.getChildContext && (i3 = m(m({}, i3), p3.getChildContext())), M && !v3 && null != p3.getSnapshotBeforeUpdate && (_2 = p3.getSnapshotBeforeUpdate(y3, d3)), H2 = null != s3 && s3.type === S && null == s3.key ? E(s3.props.children) : s3, f4 = L(n2, g(H2) ? H2 : [H2], u4, t3, i3, r3, o3, e3, f4, c3, a3), p3.base = u4.__e, u4.__u &= -161, p3.__h.length && e3.push(p3), k3 && (p3.__E = p3.__ = null);
      } catch (n3) {
        if (e3.length = h3, u4.__v = null, c3 || null != o3) if (n3.then) {
          for (u4.__u |= c3 ? 160 : 128; f4 && 8 == f4.nodeType && f4.nextSibling; ) f4 = f4.nextSibling;
          null != o3 && (o3[o3.indexOf(f4)] = null), u4.__e = f4;
        } else {
          if (null != o3) for (T3 = o3.length; T3--; ) b(o3[T3]);
          B(u4);
        }
        else u4.__e = t3.__e, !u4.__k && t3.__k && (u4.__k = t3.__k), n3.then || B(u4);
        l.__e(n3, u4, t3);
      }
    } else null == o3 && u4.__v == t3.__v ? (u4.__k = t3.__k, u4.__e = t3.__e) : f4 = u4.__e = G(t3.__e, u4, t3, i3, r3, o3, e3, c3, a3);
    return (s3 = l.diffed) && s3(u4), 128 & u4.__u ? void 0 : f4;
  }
  function B(n2) {
    n2 && (n2.__c && (n2.__c.__e = true), n2.__k && n2.__k.some(B));
  }
  function D(n2, u4, t3) {
    for (var i3 = 0; i3 < t3.length; i3++) J(t3[i3], t3[++i3], t3[++i3]);
    l.__c && l.__c(u4, n2), n2.some(function(u5) {
      try {
        n2 = u5.__h, u5.__h = [], n2.some(function(n3) {
          n3.call(u5);
        });
      } catch (n3) {
        l.__e(n3, u5.__v);
      }
    });
  }
  function E(n2) {
    return "object" != typeof n2 || null == n2 || n2.__b > 0 ? n2 : g(n2) ? n2.map(E) : void 0 !== n2.constructor ? null : m({}, n2);
  }
  function G(u4, t3, i3, r3, o3, e3, f4, c3, a3) {
    var s3, h3, p3, v3, y3, w3, _2, m3 = i3.props || d, k3 = t3.props, x2 = t3.type;
    if ("svg" == x2 ? o3 = "http://www.w3.org/2000/svg" : "math" == x2 ? o3 = "http://www.w3.org/1998/Math/MathML" : o3 || (o3 = "http://www.w3.org/1999/xhtml"), null != e3) {
      for (s3 = 0; s3 < e3.length; s3++) if ((y3 = e3[s3]) && "setAttribute" in y3 == !!x2 && (x2 ? y3.localName == x2 : 3 == y3.nodeType)) {
        u4 = y3, e3[s3] = null;
        break;
      }
    }
    if (null == u4) {
      if (null == x2) return document.createTextNode(k3);
      u4 = document.createElementNS(o3, x2, k3.is && k3), c3 && (l.__m && l.__m(t3, e3), c3 = false), e3 = null;
    }
    if (null == x2) m3 === k3 || c3 && u4.data == k3 || (u4.data = k3);
    else {
      if (e3 = "textarea" == x2 && null != k3.defaultValue ? null : e3 && n.call(u4.childNodes), !c3 && null != e3) for (m3 = {}, s3 = 0; s3 < u4.attributes.length; s3++) m3[(y3 = u4.attributes[s3]).name] = y3.value;
      for (s3 in m3) y3 = m3[s3], "dangerouslySetInnerHTML" == s3 ? p3 = y3 : "children" == s3 || s3 in k3 || "value" == s3 && "defaultValue" in k3 || "checked" == s3 && "defaultChecked" in k3 || N(u4, s3, null, y3, o3);
      for (s3 in k3) y3 = k3[s3], "children" == s3 ? v3 = y3 : "dangerouslySetInnerHTML" == s3 ? h3 = y3 : "value" == s3 ? w3 = y3 : "checked" == s3 ? _2 = y3 : c3 && "function" != typeof y3 || m3[s3] === y3 || N(u4, s3, y3, m3[s3], o3);
      if (h3) c3 || p3 && (h3.__html == p3.__html || h3.__html == u4.innerHTML) || (u4.innerHTML = h3.__html), t3.__k = [];
      else if (p3 && (u4.innerHTML = ""), L("template" == t3.type ? u4.content : u4, g(v3) ? v3 : [v3], t3, i3, r3, "foreignObject" == x2 ? "http://www.w3.org/1999/xhtml" : o3, e3, f4, e3 ? e3[0] : i3.__k && $(i3, 0), c3, a3), null != e3) for (s3 = e3.length; s3--; ) b(e3[s3]);
      c3 && "textarea" != x2 || (s3 = "value", "progress" == x2 && null == w3 ? u4.removeAttribute("value") : null != w3 && (w3 !== u4[s3] || "progress" == x2 && !w3 || "option" == x2 && w3 != m3[s3]) && N(u4, s3, w3, m3[s3], o3), s3 = "checked", null != _2 && _2 != u4[s3] && N(u4, s3, _2, m3[s3], o3));
    }
    return u4;
  }
  function J(n2, u4, t3) {
    try {
      if ("function" == typeof n2) {
        var i3 = "function" == typeof n2.__u;
        i3 && n2.__u(), i3 && null == u4 || (n2.__u = n2(u4));
      } else n2.current = u4;
    } catch (n3) {
      l.__e(n3, t3);
    }
  }
  function K(n2, u4, t3) {
    var i3, r3;
    if (l.unmount && l.unmount(n2), (i3 = n2.ref) && (i3.current && i3.current != n2.__e || J(i3, null, u4)), null != (i3 = n2.__c)) {
      if (i3.componentWillUnmount) try {
        i3.componentWillUnmount();
      } catch (n3) {
        l.__e(n3, u4);
      }
      i3.base = i3.__P = i3.__n = null;
    }
    if (i3 = n2.__k) for (r3 = 0; r3 < i3.length; r3++) i3[r3] && K(i3[r3], u4, t3 || "function" != typeof n2.type);
    t3 || b(n2.__e), n2.__c = n2.__ = n2.__e = void 0;
  }
  function Q(n2, l3, u4) {
    return this.constructor(n2, u4);
  }
  function R(u4, t3, i3) {
    var r3, o3, e3, f4;
    t3 == document && (t3 = document.documentElement), l.__ && l.__(u4, t3), o3 = (r3 = "function" == typeof i3) ? null : i3 && i3.__k || t3.__k, e3 = [], f4 = [], q(t3, u4 = (!r3 && i3 || t3).__k = k(S, null, [u4]), o3 || d, d, t3.namespaceURI, !r3 && i3 ? [i3] : o3 ? null : t3.firstChild ? n.call(t3.childNodes) : null, e3, !r3 && i3 ? i3 : o3 ? o3.__e : t3.firstChild, r3, f4), D(e3, u4, f4), u4.props.children = null;
  }
  n = w.slice, l = { __e: function(n2, l3, u4, t3) {
    for (var i3, r3, o3; l3 = l3.__; ) if ((i3 = l3.__c) && !i3.__) try {
      if ((r3 = i3.constructor) && null != r3.getDerivedStateFromError && (i3.setState(r3.getDerivedStateFromError(n2)), o3 = i3.__d), null != i3.componentDidCatch && (i3.componentDidCatch(n2, t3 || {}), o3 = i3.__d), o3) return i3.__E = i3;
    } catch (l4) {
      n2 = l4;
    }
    throw n2;
  } }, u = 0, t = function(n2) {
    return null != n2 && void 0 === n2.constructor;
  }, C.prototype.setState = function(n2, l3) {
    var u4;
    u4 = null != this.__s && this.__s != this.state ? this.__s : this.__s = m({}, this.state), "function" == typeof n2 && (n2 = n2(m({}, u4), this.props)), n2 && m(u4, n2), null != n2 && this.__v && (l3 && this._sb.push(l3), A(this));
  }, C.prototype.forceUpdate = function(n2) {
    this.__v && (this.__e = true, n2 && this.__h.push(n2), A(this));
  }, C.prototype.render = S, i = [], o = "function" == typeof Promise ? Promise.prototype.then.bind(Promise.resolve()) : setTimeout, e = function(n2, l3) {
    return n2.__v.__b - l3.__v.__b;
  }, H.__r = 0, f = Math.random().toString(8), c = "__d" + f, a = "__a" + f, s = /(PointerCapture)$|Capture$/i, h = 0, p = V(false), v = V(true), y = 0;

  // ../../../node_modules/preact/hooks/dist/hooks.module.js
  var t2;
  var r2;
  var u2;
  var i2;
  var o2 = 0;
  var f2 = [];
  var c2 = l;
  var e2 = c2.__b;
  var a2 = c2.__r;
  var v2 = c2.diffed;
  var l2 = c2.__c;
  var m2 = c2.unmount;
  var p2 = c2.__;
  function s2(n2, t3) {
    c2.__h && c2.__h(r2, n2, o2 || t3), o2 = 0;
    var u4 = r2.__H || (r2.__H = { __: [], __h: [] });
    return n2 >= u4.__.length && u4.__.push({}), u4.__[n2];
  }
  function d2(n2) {
    return o2 = 1, y2(D2, n2);
  }
  function y2(n2, u4, i3) {
    var o3 = s2(t2++, 2);
    if (o3.t = n2, !o3.__c && (o3.__ = [i3 ? i3(u4) : D2(void 0, u4), function(n3) {
      var t3 = o3.__N ? o3.__N[0] : o3.__[0], r3 = o3.t(t3, n3);
      t3 !== r3 && (o3.__N = [r3, o3.__[1]], o3.__c.setState({}));
    }], o3.__c = r2, !r2.__f)) {
      var f4 = function(n3, t3, r3) {
        if (!o3.__c.__H) return true;
        var u5 = false, i4 = o3.__c.props !== n3;
        if (o3.__c.__H.__.some(function(n4) {
          if (n4.__N) {
            u5 = true;
            var t4 = n4.__[0];
            n4.__ = n4.__N, n4.__N = void 0, t4 !== n4.__[0] && (i4 = true);
          }
        }), c3) {
          var f5 = c3.call(this, n3, t3, r3);
          return u5 ? f5 || i4 : f5;
        }
        return !u5 || i4;
      };
      r2.__f = true;
      var c3 = r2.shouldComponentUpdate, e3 = r2.componentWillUpdate;
      r2.componentWillUpdate = function(n3, t3, r3) {
        if (this.__e) {
          var u5 = c3;
          c3 = void 0, f4(n3, t3, r3), c3 = u5;
        }
        e3 && e3.call(this, n3, t3, r3);
      }, r2.shouldComponentUpdate = f4;
    }
    return o3.__N || o3.__;
  }
  function h2(n2, u4) {
    var i3 = s2(t2++, 3);
    !c2.__s && C2(i3.__H, u4) && (i3.__ = n2, i3.u = u4, r2.__H.__h.push(i3));
  }
  function A2(n2) {
    return o2 = 5, T2(function() {
      return { current: n2 };
    }, []);
  }
  function T2(n2, r3) {
    var u4 = s2(t2++, 7);
    return C2(u4.__H, r3) && (u4.__ = n2(), u4.__H = r3, u4.__h = n2), u4.__;
  }
  function j2() {
    for (var n2; n2 = f2.shift(); ) {
      var t3 = n2.__H;
      if (n2.__P && t3) try {
        t3.__h.some(z2), t3.__h.some(B2), t3.__h = [];
      } catch (r3) {
        t3.__h = [], c2.__e(r3, n2.__v);
      }
    }
  }
  c2.__b = function(n2) {
    r2 = null, e2 && e2(n2);
  }, c2.__ = function(n2, t3) {
    n2 && t3.__k && t3.__k.__m && (n2.__m = t3.__k.__m), p2 && p2(n2, t3);
  }, c2.__r = function(n2) {
    a2 && a2(n2), t2 = 0;
    var i3 = (r2 = n2.__c).__H;
    i3 && (u2 === r2 ? (i3.__h = [], r2.__h = [], i3.__.some(function(n3) {
      n3.__N && (n3.__ = n3.__N), n3.u = n3.__N = void 0;
    })) : (i3.__h.length && j2(), t2 = 0)), u2 = r2;
  }, c2.diffed = function(n2) {
    v2 && v2(n2);
    var t3 = n2.__c;
    t3 && t3.__H && (t3.__H.__h.length && (1 !== f2.push(t3) && i2 === c2.requestAnimationFrame || ((i2 = c2.requestAnimationFrame) || w2)(j2)), t3.__H.__.some(function(n3) {
      n3.u && (n3.__H = n3.u, n3.u = void 0);
    })), u2 = r2 = null;
  }, c2.__c = function(n2, t3) {
    t3.some(function(n3) {
      try {
        n3.__h.some(z2), n3.__h = n3.__h.filter(function(n4) {
          return !n4.__ || B2(n4);
        });
      } catch (r3) {
        t3.some(function(n4) {
          n4.__h && (n4.__h = []);
        }), t3 = [], c2.__e(r3, n3.__v);
      }
    }), l2 && l2(n2, t3);
  }, c2.unmount = function(n2) {
    m2 && m2(n2);
    var t3, r3 = n2.__c;
    r3 && r3.__H && (r3.__H.__.some(function(n3) {
      try {
        z2(n3);
      } catch (n4) {
        t3 = n4;
      }
    }), r3.__H = void 0, t3 && c2.__e(t3, r3.__v));
  };
  var k2 = "function" == typeof requestAnimationFrame;
  function w2(n2) {
    var t3, r3 = function() {
      clearTimeout(u4), k2 && cancelAnimationFrame(t3), setTimeout(n2);
    }, u4 = setTimeout(r3, 35);
    k2 && (t3 = requestAnimationFrame(r3));
  }
  function z2(n2) {
    var t3 = r2, u4 = n2.__c;
    "function" == typeof u4 && (n2.__c = void 0, u4()), r2 = t3;
  }
  function B2(n2) {
    var t3 = r2;
    n2.__c = n2.__(), r2 = t3;
  }
  function C2(n2, t3) {
    return !n2 || n2.length !== t3.length || t3.some(function(t4, r3) {
      return t4 !== n2[r3];
    });
  }
  function D2(n2, t3) {
    return "function" == typeof t3 ? t3(n2) : t3;
  }

  // ../../../node_modules/preact/jsx-runtime/dist/jsxRuntime.module.js
  var f3 = 0;
  function u3(e3, t3, n2, o3, i3, u4) {
    t3 || (t3 = {});
    var a3, c3, p3 = t3;
    if ("ref" in p3) for (c3 in p3 = {}, t3) "ref" == c3 ? a3 = t3[c3] : p3[c3] = t3[c3];
    var l3 = { type: e3, props: p3, key: n2, ref: a3, __k: null, __: null, __b: 0, __e: null, __c: null, constructor: void 0, __v: --f3, __i: -1, __u: 0, __source: i3, __self: u4 };
    if ("function" == typeof e3 && (a3 = e3.defaultProps)) for (c3 in a3) void 0 === p3[c3] && (p3[c3] = a3[c3]);
    return l.vnode && l.vnode(l3), l3;
  }

  // meridian/static/components/CodeIntelPanel.tsx
  var PALETTE = ["#7dd3fc", "#a78bfa", "#34d399", "#fbbf24", "#fb7185", "#60a5fa", "#f472b6"];
  function buildLayers(arch) {
    const packages = arch && Array.isArray(arch.packages) ? arch.packages.filter((p3) => !!p3 && !!p3.name) : [];
    if (!packages.length) return [];
    const byRank = /* @__PURE__ */ new Map();
    for (const p3 of packages) {
      const key = String(p3.layer ?? "other");
      const bucket = byRank.get(key);
      if (bucket) bucket.push(p3);
      else byRank.set(key, [p3]);
    }
    const ranks = Array.from(byRank.keys()).sort((a3, b2) => {
      const na = Number(a3);
      const nb = Number(b2);
      const aNum = a3 !== "" && !Number.isNaN(na);
      const bNum = b2 !== "" && !Number.isNaN(nb);
      if (aNum && bNum) return nb - na;
      if (aNum) return -1;
      if (bNum) return 1;
      return a3.localeCompare(b2);
    });
    return ranks.map((rank) => ({
      rank,
      label: labelForRank(arch, rank),
      packages: byRank.get(rank).slice().sort((a3, b2) => (b2.node_count ?? 0) - (a3.node_count ?? 0))
    }));
  }
  function labelForRank(arch, rank) {
    const named = arch && Array.isArray(arch.layers) ? arch.layers.find((l3) => l3 && String(l3.layer) === rank && l3.name) : null;
    if (named && named.name) return `${named.name} \xB7 layer ${rank}`;
    return rank === "other" ? "unlayered" : `layer ${rank}`;
  }
  function isNotConnectedError(error) {
    if (!error) return false;
    const e3 = error.toLowerCase();
    return e3.includes("503") || e3.includes("not connected") || e3.includes("not_connected") || e3.includes("no active") || e3.includes("tunnel") && (e3.includes("not") || e3.includes("no "));
  }
  var CI_EDGE_TYPES = ["contains", "imports", "inherits", "invokes"];
  var CI_EDGE_COLORS = {
    contains: "#64748b",
    imports: "#38bdf8",
    inherits: "#a78bfa",
    invokes: "#f59e0b"
  };
  function _emitChildren(els, parentId, children) {
    if (!Array.isArray(children)) return;
    for (const c3 of children) {
      if (!c3 || !c3.name) continue;
      const id = `${parentId}/${c3.name}`;
      els.push({ group: "nodes", data: { id, parent: parentId, label: c3.name, kind: c3.kind || "node" } });
      for (const imp of c3.imports || []) {
        els.push({ group: "edges", data: { id: `imports:${id}->${imp}`, source: id, target: imp, etype: "imports", color: CI_EDGE_COLORS.imports } });
      }
      for (const base of c3.inherits || []) {
        els.push({ group: "edges", data: { id: `inherits:${id}->${base}`, source: id, target: base, etype: "inherits", color: CI_EDGE_COLORS.inherits } });
      }
      _emitChildren(els, id, c3.children);
    }
  }
  function buildCytoscapeElements(arch) {
    const rows = buildLayers(arch);
    const els = [];
    const pkgIds = /* @__PURE__ */ new Set();
    for (const layer of rows) {
      const parentId = `layer:${layer.rank}`;
      els.push({ group: "nodes", data: { id: parentId, label: layer.label, kind: "layer" } });
      for (const p3 of layer.packages) {
        const pid = `pkg:${p3.name}`;
        pkgIds.add(String(p3.name));
        els.push({
          group: "nodes",
          data: { id: pid, parent: parentId, label: p3.name, kind: "package", node_count: p3.node_count ?? 0 }
        });
        _emitChildren(els, pid, p3.children);
      }
    }
    const boundaries = arch && Array.isArray(arch.boundaries) ? arch.boundaries : [];
    for (const b2 of boundaries) {
      if (!b2 || !b2.from || !b2.to) continue;
      if (!pkgIds.has(String(b2.from)) || !pkgIds.has(String(b2.to))) continue;
      els.push({
        group: "edges",
        data: {
          id: `invokes:${b2.from}->${b2.to}`,
          source: `pkg:${b2.from}`,
          target: `pkg:${b2.to}`,
          etype: "invokes",
          weight: b2.call_count ?? 1,
          color: CI_EDGE_COLORS.invokes
        }
      });
    }
    return els;
  }
  function filterCyElements(elements, enabled) {
    return elements.filter(
      (el2) => el2.group === "nodes" || enabled.has(el2.data.etype)
    );
  }
  function mountCytoscapeGraph(container, elements, opts = {}) {
    const w3 = typeof window !== "undefined" ? window : void 0;
    const cy = w3 && w3.cytoscape;
    if (!cy || !container) return null;
    try {
      const fcose = w3.cytoscapeFcose;
      if (fcose && !cy.__meridianFcose) {
        cy.use(fcose);
        cy.__meridianFcose = true;
      }
    } catch {
    }
    const els = opts.edgeFilter ? filterCyElements(elements, opts.edgeFilter) : elements;
    try {
      return cy({
        container,
        elements: els.map((e3) => ({ group: e3.group, data: e3.data })),
        style: [
          { selector: "node", style: { "background-color": "#334155", label: "data(label)", color: "#cbd5e1", "font-size": 8, "text-valign": "center" } },
          { selector: ":parent", style: { "background-opacity": 0.12, "border-color": "#475569", label: "data(label)", "text-valign": "top" } },
          { selector: "edge", style: { width: 1, "line-color": "data(color)", "target-arrow-color": "data(color)", "target-arrow-shape": "triangle", "curve-style": "bezier" } }
        ],
        layout: { name: w3.cytoscapeFcose ? "fcose" : "cose", animate: false }
      });
    } catch {
      return null;
    }
  }
  function CodeIntelPanel(props) {
    const { status, error, architecture, onGenerateMap } = props;
    const [zoom, setZoom] = d2(1);
    const [mapStatus, setMapStatus] = d2("idle");
    const [mapUrl, setMapUrl] = d2("");
    const [mapError, setMapError] = d2("");
    const [enabled, setEnabled] = d2({
      contains: true,
      imports: true,
      inherits: true,
      invokes: true
    });
    const cyRef = A2(null);
    const [cyMounted, setCyMounted] = d2(false);
    const cyAvailable = typeof window !== "undefined" && !!window.cytoscape;
    h2(() => {
      if (status !== "ready" || !cyRef.current) return;
      const elements = buildCytoscapeElements(architecture);
      const edgeFilter = new Set(CI_EDGE_TYPES.filter((t3) => enabled[t3]));
      const cy = mountCytoscapeGraph(cyRef.current, elements, { edgeFilter });
      setCyMounted(!!cy);
      return () => {
        try {
          if (cy) cy.destroy();
        } catch {
        }
      };
    }, [status, architecture, enabled]);
    if (status === "loading") {
      return /* @__PURE__ */ u3("div", { class: "ci-state ci-loading", role: "status", style: STATE_STYLE, children: "Loading code intelligence\u2026" });
    }
    if (status === "error") {
      if (isNotConnectedError(error)) {
        return /* @__PURE__ */ u3(
          "div",
          {
            class: "ci-state ci-not-connected",
            role: "status",
            style: { ...STATE_STYLE, color: "var(--muted)", lineHeight: "1.6" },
            children: [
              /* @__PURE__ */ u3("div", { style: { fontSize: "12px", color: "var(--text)", fontWeight: 600, marginBottom: "4px" }, children: "Code index not connected" }),
              /* @__PURE__ */ u3("div", { children: "The code intelligence tunnel slot isn't connected yet. Start the tunnel and connect the code slot to populate the package graph:" }),
              /* @__PURE__ */ u3("div", { style: { marginTop: "6px" }, children: [
                "Run ",
                /* @__PURE__ */ u3("code", { children: "meridian --tunnel" }),
                " in your repo, then open this panel again."
              ] })
            ]
          }
        );
      }
      return /* @__PURE__ */ u3("div", { class: "ci-state ci-error", role: "alert", style: { ...STATE_STYLE, color: "var(--error,#ef4444)" }, children: [
        "Failed to load code intelligence: ",
        error || "unknown error"
      ] });
    }
    const layers = buildLayers(architecture);
    const hasGraph = layers.length > 0;
    async function handleGenerate() {
      if (!onGenerateMap) return;
      setMapStatus("loading");
      setMapError("");
      try {
        const url = await onGenerateMap();
        setMapUrl(url);
        setMapStatus("ready");
      } catch (e3) {
        setMapError(e3 instanceof Error ? e3.message : String(e3));
        setMapStatus("error");
      }
    }
    return /* @__PURE__ */ u3("div", { class: "ci-panel", children: [
      /* @__PURE__ */ u3(
        "div",
        {
          class: "ci-toolbar",
          style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "6px" },
          children: [
            /* @__PURE__ */ u3(
              "span",
              {
                class: "ci-title",
                style: { fontSize: "10px", color: "var(--accent)", textTransform: "uppercase", letterSpacing: ".06em" },
                children: "Package Graph"
              }
            ),
            /* @__PURE__ */ u3("div", { class: "ci-controls", style: { display: "flex", gap: "4px", alignItems: "center" }, children: [
              /* @__PURE__ */ u3(
                "button",
                {
                  class: "ci-zoom-out",
                  "aria-label": "Zoom out",
                  onClick: () => setZoom((z3) => Math.max(0.5, +(z3 - 0.1).toFixed(2))),
                  style: CTRL_STYLE,
                  children: "\u2212"
                }
              ),
              /* @__PURE__ */ u3("span", { class: "ci-zoom-label", style: { fontSize: "9px", color: "var(--muted)", minWidth: "30px", textAlign: "center" }, children: [
                Math.round(zoom * 100),
                "%"
              ] }),
              /* @__PURE__ */ u3(
                "button",
                {
                  class: "ci-zoom-in",
                  "aria-label": "Zoom in",
                  onClick: () => setZoom((z3) => Math.min(2, +(z3 + 0.1).toFixed(2))),
                  style: CTRL_STYLE,
                  children: "+"
                }
              ),
              onGenerateMap ? /* @__PURE__ */ u3(
                "button",
                {
                  class: "ci-genmap",
                  disabled: mapStatus === "loading",
                  onClick: handleGenerate,
                  title: "Render a static PNG map via Graphviz",
                  style: CTRL_STYLE,
                  children: mapStatus === "loading" ? "Generating\u2026" : "Generate Map"
                }
              ) : null
            ] })
          ]
        }
      ),
      cyAvailable && hasGraph ? /* @__PURE__ */ u3("div", { class: "ci-edge-filters", style: { display: "flex", gap: "8px", flexWrap: "wrap", margin: "0 0 6px" }, children: CI_EDGE_TYPES.map((t3) => /* @__PURE__ */ u3(
        "label",
        {
          class: "ci-edge-filter",
          style: { display: "flex", alignItems: "center", gap: "3px", fontSize: "9px", color: CI_EDGE_COLORS[t3], fontFamily: "var(--font-mono)" },
          children: [
            /* @__PURE__ */ u3("input", { type: "checkbox", checked: enabled[t3], "aria-label": t3, onChange: () => setEnabled((e3) => ({ ...e3, [t3]: !e3[t3] })) }),
            t3
          ]
        },
        t3
      )) }) : null,
      cyAvailable ? /* @__PURE__ */ u3(
        "div",
        {
          class: "ci-cy",
          ref: cyRef,
          style: {
            width: "100%",
            height: hasGraph ? "360px" : "0",
            background: "var(--surface-1)",
            border: hasGraph ? "1px solid var(--border)" : "none",
            borderRadius: "4px"
          }
        }
      ) : null,
      hasGraph && (!cyAvailable || !cyMounted) ? /* @__PURE__ */ u3(
        "div",
        {
          class: "ci-dag",
          style: {
            width: "100%",
            background: "var(--surface-1)",
            border: "1px solid var(--border)",
            borderRadius: "4px",
            padding: "10px",
            overflow: "auto"
          },
          children: /* @__PURE__ */ u3("div", { style: { transform: `scale(${zoom})`, transformOrigin: "top left", display: "flex", flexDirection: "column", gap: "10px" }, children: layers.map((layer, i3) => /* @__PURE__ */ u3("div", { class: "ci-layer", style: { display: "flex", alignItems: "center", gap: "8px" }, children: [
            /* @__PURE__ */ u3(
              "div",
              {
                class: "ci-layer-label",
                style: { width: "120px", flexShrink: 0, fontSize: "9px", color: "var(--muted)", textAlign: "right", fontFamily: "var(--font-mono)" },
                children: layer.label
              }
            ),
            /* @__PURE__ */ u3("div", { class: "ci-layer-nodes", style: { display: "flex", flexWrap: "wrap", gap: "6px", flex: 1, borderLeft: "2px solid var(--border)", paddingLeft: "8px" }, children: layer.packages.map((p3) => /* @__PURE__ */ u3(
              "div",
              {
                class: "ci-node",
                title: `${p3.name} \xB7 ${p3.node_count ?? 0} nodes`,
                style: {
                  display: "flex",
                  alignItems: "center",
                  gap: "5px",
                  background: "var(--surface-2)",
                  border: `1px solid ${PALETTE[i3 % PALETTE.length]}55`,
                  borderLeft: `3px solid ${PALETTE[i3 % PALETTE.length]}`,
                  borderRadius: "3px",
                  padding: "3px 7px",
                  fontSize: "10px",
                  color: "var(--text)",
                  fontFamily: "var(--font-mono)"
                },
                children: [
                  /* @__PURE__ */ u3("span", { class: "ci-node-name", children: p3.name }),
                  /* @__PURE__ */ u3("span", { class: "ci-node-count", style: { fontSize: "8px", color: "var(--muted)" }, children: p3.node_count ?? 0 })
                ]
              },
              p3.name
            )) })
          ] }, layer.rank)) })
        }
      ) : null,
      !hasGraph ? /* @__PURE__ */ u3("div", { class: "ci-state ci-empty", style: { ...STATE_STYLE, color: "var(--muted)" }, children: "No package graph yet \u2014 index the repo to populate it." }) : null,
      mapStatus === "ready" && mapUrl ? /* @__PURE__ */ u3("div", { class: "ci-map", style: { marginTop: "8px" }, children: /* @__PURE__ */ u3("img", { src: mapUrl, alt: "Codebase package map", style: { maxWidth: "100%", border: "1px solid var(--border)", borderRadius: "4px", background: "#0b0e14" } }) }) : null,
      mapStatus === "error" ? /* @__PURE__ */ u3("div", { class: "ci-state ci-map-error", role: "alert", style: { ...STATE_STYLE, color: "var(--error,#ef4444)", marginTop: "8px" }, children: [
        "Map generation failed: ",
        mapError
      ] }) : null
    ] });
  }
  var STATE_STYLE = { fontSize: "11px", padding: "16px", textAlign: "center", color: "var(--muted)" };
  var CTRL_STYLE = {
    background: "none",
    border: "1px solid var(--border)",
    borderRadius: "3px",
    color: "var(--muted)",
    fontSize: "9px",
    fontFamily: "var(--font-mono)",
    padding: "2px 8px",
    cursor: "pointer"
  };
  function mountCodeIntelPanel(container, props) {
    R(/* @__PURE__ */ u3(CodeIntelPanel, { ...props }), container);
  }

  // meridian/static/codegraph/model.ts
  var ROLE_ALIASES = {
    folder: "folder",
    dir: "folder",
    directory: "folder",
    file: "file",
    module: "module",
    package: "package",
    pkg: "package",
    class: "class",
    interface: "interface",
    trait: "interface",
    protocol: "interface",
    function: "function",
    func: "function",
    fn: "function",
    method: "method",
    route: "route",
    endpoint: "route",
    variable: "variable",
    var: "variable",
    const: "variable",
    constant: "variable"
  };
  function normalizeRole(raw) {
    if (typeof raw !== "string") return "unknown";
    const key = raw.trim().toLowerCase();
    return ROLE_ALIASES[key] ?? "unknown";
  }
  function splitPath(p3) {
    if (typeof p3 !== "string") return [];
    return p3.replace(/\\/g, "/").split("/").map((s3) => s3.trim()).filter((s3) => s3.length > 0 && s3 !== ".");
  }
  function toNumber(v3) {
    if (typeof v3 === "number") return Number.isFinite(v3) ? v3 : void 0;
    if (typeof v3 === "string" && v3.trim() !== "") {
      const n2 = Number(v3);
      return Number.isFinite(n2) ? n2 : void 0;
    }
    return void 0;
  }
  function toStringList(v3) {
    if (!Array.isArray(v3)) return void 0;
    const out = v3.map((x2) => String(x2)).filter((s3) => s3.length > 0);
    return out.length ? out : void 0;
  }
  function symbolRole(n2) {
    const explicit = normalizeRole(n2.role ?? n2.kind ?? n2.label);
    if (explicit !== "unknown") return explicit;
    return "function";
  }
  function symbolLabel(n2) {
    const qn = String(n2.qualified_name ?? n2.qualifiedName ?? "").trim();
    if (qn) {
      const parts = qn.split(".");
      const last = parts[parts.length - 1];
      if (last) return last;
      return qn;
    }
    const nm = String(n2.name ?? "").trim();
    return nm || "(anonymous)";
  }
  function extractMeta(n2) {
    const meta = {};
    const qn = String(n2.qualified_name ?? n2.qualifiedName ?? "").trim();
    if (qn) meta.qualifiedName = qn;
    const file = String(n2.file ?? n2.path ?? "").trim();
    if (file) meta.file = file;
    const sig = String(n2.signature ?? "").trim();
    if (sig) meta.signature = sig;
    const doc = String(n2.docstring ?? n2.doc ?? "").trim();
    if (doc) meta.docstring = doc;
    const cx = toNumber(n2.complexity);
    if (cx !== void 0) meta.complexity = cx;
    const callers = toStringList(n2.callers);
    if (callers) meta.callers = callers;
    const callees = toStringList(n2.callees);
    if (callees) meta.callees = callees;
    const fanIn = toNumber(n2.fanIn ?? n2.fan_in);
    if (fanIn !== void 0) meta.fanIn = fanIn;
    const fanOut = toNumber(n2.fanOut ?? n2.fan_out);
    if (fanOut !== void 0) meta.fanOut = fanOut;
    return meta;
  }
  function newBuildNode(id, label, role) {
    return { id, label, role, children: /* @__PURE__ */ new Map(), symbols: [], meta: {} };
  }
  function buildCodeGraphModel(payload) {
    const roots = /* @__PURE__ */ new Map();
    let symbolCount = 0;
    let fileCount = 0;
    const ensureFolderChain = (segments) => {
      if (!segments.length) return null;
      let level = roots;
      let node = null;
      let idPrefix = "";
      for (const seg of segments) {
        idPrefix = idPrefix ? `${idPrefix}/${seg}` : seg;
        let child = level.get(seg);
        if (!child) {
          child = newBuildNode(idPrefix, seg, "folder");
          level.set(seg, child);
        }
        node = child;
        level = child.children;
      }
      return node;
    };
    const arch = payload && payload.architecture;
    const packages = arch && Array.isArray(arch.packages) ? arch.packages : [];
    for (const p3 of packages) {
      const name = String(p3 && p3.name != null ? p3.name : "").trim();
      if (!name) continue;
      const segments = splitPath(name).length ? splitPath(name) : [name];
      const folder = ensureFolderChain(segments);
      if (folder) folder.role = "package";
    }
    const nodes = payload && Array.isArray(payload.nodes) ? payload.nodes : [];
    for (const n2 of nodes) {
      if (!n2 || typeof n2 !== "object") continue;
      const hasIdentity = !!String(n2.qualified_name ?? n2.qualifiedName ?? n2.name ?? "").trim();
      if (!hasIdentity) continue;
      const meta = extractMeta(n2);
      const file = meta.file;
      const segments = file ? splitPath(file) : [];
      if (!segments.length) {
        let unfiled = roots.get("(unfiled)");
        if (!unfiled) {
          unfiled = newBuildNode("(unfiled)", "(unfiled)", "folder");
          roots.set("(unfiled)", unfiled);
        }
        unfiled.symbols.push({
          id: `${unfiled.id}::${meta.qualifiedName ?? symbolLabel(n2)}`,
          label: symbolLabel(n2),
          role: symbolRole(n2),
          depth: 1,
          children: [],
          meta
        });
        symbolCount += 1;
        continue;
      }
      const fileSeg = segments[segments.length - 1];
      const folderSegs = segments.slice(0, -1);
      const parentSegs = folderSegs.length ? folderSegs : ["(root)"];
      const folder = ensureFolderChain(parentSegs);
      if (!folder) continue;
      let fileNode = folder.children.get(fileSeg);
      if (!fileNode) {
        fileNode = newBuildNode(`${folder.id}/${fileSeg}`, fileSeg, "file");
        fileNode.meta = { file };
        folder.children.set(fileSeg, fileNode);
        fileCount += 1;
      }
      const role = symbolRole(n2);
      if (role === "file" || role === "module") {
        fileNode.role = "file";
        fileNode.meta = { ...fileNode.meta, ...meta, file };
        continue;
      }
      fileNode.symbols.push({
        id: `${fileNode.id}::${meta.qualifiedName ?? symbolLabel(n2)}`,
        label: symbolLabel(n2),
        role,
        depth: 2,
        children: [],
        meta
      });
      symbolCount += 1;
    }
    const finalized = Array.from(roots.values()).map((r3) => finalize(r3, 0));
    return { roots: finalized, symbolCount, fileCount };
  }
  function finalize(b2, depth) {
    const folderChildren = [];
    const fileChildren = [];
    for (const child of b2.children.values()) {
      const fc = finalize(child, depth + 1);
      if (fc.role === "file") fileChildren.push(fc);
      else folderChildren.push(fc);
    }
    const byLabel = (a3, c3) => a3.label < c3.label ? -1 : a3.label > c3.label ? 1 : a3.id < c3.id ? -1 : a3.id > c3.id ? 1 : 0;
    folderChildren.sort(byLabel);
    fileChildren.sort(byLabel);
    const symbols = b2.symbols.map((s3) => ({ ...s3, depth: depth + 1 })).sort(byLabel);
    return {
      id: b2.id,
      label: b2.label,
      role: b2.role,
      depth,
      children: [...folderChildren, ...fileChildren, ...symbols],
      meta: b2.meta
    };
  }

  // meridian/static/codegraph/roles.ts
  var ROLE_COLORS = {
    folder: "#64748b",
    // slate — containers
    package: "#94a3b8",
    // lighter slate — top-level containers
    module: "#38bdf8",
    // sky
    file: "#7dd3fc",
    // light sky
    class: "#a78bfa",
    // violet
    interface: "#c084fc",
    // lighter violet
    function: "#34d399",
    // emerald
    method: "#4ade80",
    // green
    route: "#f59e0b",
    // amber — entry points
    variable: "#fbbf24",
    // yellow
    unknown: "#94a3b8"
    // neutral fallback (documented default)
  };
  var DEFAULT_ROLE_COLOR = ROLE_COLORS.unknown;
  function colorForRole(role) {
    if (typeof role === "string" && role in ROLE_COLORS) {
      return ROLE_COLORS[role];
    }
    return DEFAULT_ROLE_COLOR;
  }

  // meridian/static/codegraph/render.ts
  function subtreeSummary(node) {
    let files = 0;
    let symbols = 0;
    const walk = (n2) => {
      for (const c3 of n2.children) {
        if (c3.role === "file") files += 1;
        else if (c3.children.length === 0 && c3.role !== "folder" && c3.role !== "package") symbols += 1;
        walk(c3);
      }
    };
    walk(node);
    const parts = [];
    if (files) parts.push(`${files} file${files === 1 ? "" : "s"}`);
    if (symbols) parts.push(`${symbols} fn${symbols === 1 ? "" : "s"}`);
    return parts.join(" \xB7 ");
  }
  function isDrillable(node) {
    return node.children.length > 0;
  }
  function startsExpanded(node, expandDepth) {
    return node.depth < expandDepth;
  }
  function metadataLines(node) {
    const m3 = node.meta;
    const lines = [];
    if (m3.qualifiedName) lines.push(`qualified_name: ${m3.qualifiedName}`);
    if (m3.file) lines.push(`file: ${m3.file}`);
    if (m3.signature) lines.push(`signature: ${m3.signature}`);
    if (typeof m3.complexity === "number") lines.push(`complexity: ${m3.complexity}`);
    if (m3.callers && m3.callers.length) lines.push(`callers (${m3.callers.length}): ${m3.callers.join(", ")}`);
    else if (typeof m3.fanIn === "number") lines.push(`callers (fan-in): ${m3.fanIn}`);
    if (m3.callees && m3.callees.length) lines.push(`callees (${m3.callees.length}): ${m3.callees.join(", ")}`);
    else if (typeof m3.fanOut === "number") lines.push(`callees (fan-out): ${m3.fanOut}`);
    if (m3.docstring) lines.push(`docstring: ${m3.docstring}`);
    return lines;
  }
  function el(tag, style, text) {
    const node = document.createElement(tag);
    if (style) Object.assign(node.style, style);
    if (text != null) node.textContent = text;
    return node;
  }
  function roleBadge(role) {
    const badge = el("span", {
      display: "inline-block",
      fontSize: "8px",
      lineHeight: "1",
      padding: "2px 5px",
      borderRadius: "3px",
      color: "#0b0e14",
      background: colorForRole(role),
      fontFamily: "var(--font-mono, monospace)",
      textTransform: "uppercase",
      letterSpacing: ".03em",
      flexShrink: "0"
    });
    badge.textContent = role;
    return badge;
  }
  function renderCodeGraph(mount, model, options = {}) {
    const expandDepth = options.initiallyExpandedDepth ?? 1;
    mount.textContent = "";
    const root = el("div", {
      display: "flex",
      gap: "10px",
      alignItems: "flex-start",
      fontFamily: "var(--font-mono, monospace)"
    });
    root.className = "codegraph-root";
    const treePane = el("div", {
      flex: "1 1 auto",
      minWidth: "0",
      maxHeight: "420px",
      overflow: "auto",
      border: "1px solid var(--border, #334155)",
      borderRadius: "4px",
      padding: "6px",
      background: "var(--surface-1, #0f172a)"
    });
    treePane.className = "codegraph-tree";
    const detailPane = el("div", {
      flex: "0 0 300px",
      maxHeight: "420px",
      overflow: "auto",
      border: "1px solid var(--border, #334155)",
      borderRadius: "4px",
      padding: "10px",
      background: "var(--surface-1, #0f172a)",
      fontSize: "11px",
      color: "var(--text, #e2e8f0)"
    });
    detailPane.className = "codegraph-detail";
    renderEmptyDetail(detailPane);
    if (!model.roots.length) {
      treePane.appendChild(
        el(
          "div",
          { fontSize: "11px", color: "var(--muted, #94a3b8)", padding: "12px" },
          "No code graph yet \u2014 index the repo to populate it."
        )
      );
    } else {
      for (const node of model.roots) {
        treePane.appendChild(renderNode(node, detailPane, expandDepth, options));
      }
    }
    root.appendChild(treePane);
    root.appendChild(detailPane);
    mount.appendChild(root);
    return {
      element: mount,
      destroy() {
        mount.textContent = "";
      }
    };
  }
  function renderNode(node, detailPane, expandDepth, options) {
    const wrap = el("div", { marginLeft: node.depth > 0 ? "12px" : "0" });
    wrap.className = "codegraph-node";
    wrap.dataset.role = node.role;
    wrap.dataset.id = node.id;
    const row = el("div", {
      display: "flex",
      alignItems: "center",
      gap: "6px",
      padding: "2px 4px",
      borderRadius: "3px",
      cursor: "pointer",
      fontSize: "11px",
      color: "var(--text, #e2e8f0)",
      borderLeft: `3px solid ${colorForRole(node.role)}`
    });
    row.className = "codegraph-row";
    const drillable = isDrillable(node);
    const caret = el("span", {
      width: "10px",
      flexShrink: "0",
      color: "var(--muted, #94a3b8)",
      fontSize: "9px"
    });
    caret.textContent = drillable ? "\u25B8" : "\xB7";
    const label = el("span", {
      whiteSpace: "nowrap",
      overflow: "hidden",
      textOverflow: "ellipsis",
      flex: "1 1 auto",
      minWidth: "0"
    });
    label.textContent = node.label;
    const summary = subtreeSummary(node);
    const summaryEl = el("span", { fontSize: "9px", color: "var(--muted, #94a3b8)", flexShrink: "0" });
    summaryEl.textContent = summary;
    row.appendChild(caret);
    row.appendChild(roleBadge(node.role));
    row.appendChild(label);
    if (summary) row.appendChild(summaryEl);
    wrap.appendChild(row);
    const childrenWrap = el("div", { display: startsExpanded(node, expandDepth) ? "block" : "none" });
    childrenWrap.className = "codegraph-children";
    let built = false;
    const buildChildren = () => {
      if (built) return;
      for (const c3 of node.children) {
        childrenWrap.appendChild(renderNode(c3, detailPane, expandDepth, options));
      }
      built = true;
    };
    if (startsExpanded(node, expandDepth)) buildChildren();
    wrap.appendChild(childrenWrap);
    row.addEventListener("click", (ev) => {
      ev.stopPropagation();
      renderDetail(detailPane, node, options);
      if (options.onSelect) {
        try {
          options.onSelect(node);
        } catch {
        }
      }
      if (drillable) {
        const open = childrenWrap.style.display !== "none";
        if (!open) buildChildren();
        childrenWrap.style.display = open ? "none" : "block";
        caret.textContent = open ? "\u25B8" : "\u25BE";
      }
    });
    return wrap;
  }
  function renderEmptyDetail(pane) {
    pane.textContent = "";
    pane.appendChild(
      el(
        "div",
        { color: "var(--muted, #94a3b8)", fontSize: "10px" },
        "Select a node to see its static metadata."
      )
    );
  }
  function renderDetail(pane, node, options) {
    pane.textContent = "";
    const header = el("div", { display: "flex", gap: "6px", alignItems: "center", marginBottom: "8px" });
    header.appendChild(roleBadge(node.role));
    header.appendChild(el("span", { fontWeight: "600", fontSize: "12px", wordBreak: "break-all" }, node.label));
    pane.appendChild(header);
    const lines = metadataLines(node);
    if (!lines.length) {
      pane.appendChild(
        el(
          "div",
          { color: "var(--muted, #94a3b8)", fontSize: "10px" },
          "No static metadata on this node."
        )
      );
    } else {
      for (const line of lines) {
        pane.appendChild(
          el("div", {
            fontSize: "10px",
            color: "var(--text, #e2e8f0)",
            padding: "2px 0",
            borderBottom: "1px solid var(--border, #1f2937)",
            wordBreak: "break-word",
            whiteSpace: "pre-wrap"
          }, line)
        );
      }
    }
    if (options.onRequestSummary) {
      const btn = el("button", {
        marginTop: "10px",
        fontSize: "10px",
        padding: "3px 10px",
        border: "1px solid var(--border, #334155)",
        borderRadius: "3px",
        background: "none",
        color: "var(--accent, #7dd3fc)",
        cursor: "pointer",
        fontFamily: "var(--font-mono, monospace)"
      });
      btn.textContent = "\u2728 LLM summary";
      btn.title = "On-demand LLM summary \u2014 not part of the deterministic view";
      const out = el("div", { marginTop: "8px", fontSize: "10px", color: "var(--muted, #94a3b8)", whiteSpace: "pre-wrap" });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "summarizing\u2026";
        try {
          const summary = await options.onRequestSummary(node);
          out.style.color = "var(--text, #e2e8f0)";
          out.textContent = summary;
        } catch (e3) {
          out.style.color = "var(--error, #ef4444)";
          out.textContent = `Summary failed: ${e3 instanceof Error ? e3.message : String(e3)}`;
        } finally {
          btn.disabled = false;
          btn.textContent = "\u2728 LLM summary";
        }
      });
      pane.appendChild(btn);
      pane.appendChild(out);
    }
  }

  // ../../../node_modules/zustand/esm/vanilla.mjs
  var createStoreImpl = (createState) => {
    let state2;
    const listeners = /* @__PURE__ */ new Set();
    const setState = (partial, replace) => {
      const nextState = typeof partial === "function" ? partial(state2) : partial;
      if (!Object.is(nextState, state2)) {
        const previousState = state2;
        state2 = (replace != null ? replace : typeof nextState !== "object" || nextState === null) ? nextState : Object.assign({}, state2, nextState);
        listeners.forEach((listener) => listener(state2, previousState));
      }
    };
    const getState = () => state2;
    const getInitialState = () => initialState;
    const subscribe = (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    };
    const api3 = { setState, getState, getInitialState, subscribe };
    const initialState = state2 = createState(setState, getState, api3);
    return api3;
  };
  var createStore = ((createState) => createState ? createStoreImpl(createState) : createStoreImpl);

  // meridian/static/dashboard-tabgroups.ts
  var VTAB_GROUPS = [
    { id: "overview", label: "Overview", tabs: ["status", "live"] },
    { id: "planning", label: "Planning", tabs: ["goal", "insights", "blog"] },
    { id: "work", label: "Work", tabs: ["queue", "hitl", "team", "sessions"] },
    { id: "content", label: "Content", tabs: ["files", "notes", "devlog", "documents", "docs", "codeintel"] },
    { id: "history", label: "History", tabs: ["timeline", "rewind", "settings"] }
  ];
  var ALL_GROUPED_TABS = VTAB_GROUPS.flatMap((g2) => g2.tabs);
  function groupForTab(tab) {
    if (!tab) return null;
    for (const g2 of VTAB_GROUPS) {
      if (g2.tabs.includes(tab)) return g2.id;
    }
    return null;
  }
  function wireVtabGroups(stripEl) {
    const setExpanded = (groupEl, expanded) => {
      groupEl.classList.toggle("collapsed", !expanded);
      const header = groupEl.querySelector(".vtab-group-header");
      if (header) header.setAttribute("aria-expanded", String(expanded));
      const tabs = groupEl.querySelector(".vtab-group-tabs");
      if (tabs) tabs.style.display = expanded ? "flex" : "none";
    };
    stripEl.querySelectorAll(".vtab-group-header").forEach((header) => {
      header.onclick = () => {
        const groupEl = header.closest(".vtab-group");
        if (!groupEl) return;
        setExpanded(groupEl, groupEl.classList.contains("collapsed"));
      };
    });
    const revealGroupForTab = (tab) => {
      const groupId = groupForTab(tab);
      if (!groupId) return;
      const groupEl = stripEl.querySelector(`.vtab-group[data-vgroup="${groupId}"]`);
      if (groupEl) setExpanded(groupEl, true);
    };
    return { revealGroupForTab };
  }

  // meridian/static/dashboard-folders.ts
  var FOLDER_ASSIGN_KEY = "meridian.projectFolders";
  var FOLDER_COLLAPSE_KEY = "meridian.projectFolderCollapsed";
  var UNGROUPED_LABEL = "Ungrouped";
  function normalizeFolderName(name) {
    if (name == null) return "";
    return String(name).trim().replace(/\s+/g, " ");
  }
  function loadFolderAssignments(key = FOLDER_ASSIGN_KEY, storage = safeStorage()) {
    if (!storage) return {};
    try {
      const raw = storage.getItem(key);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      const out = {};
      for (const [pid, folder] of Object.entries(parsed)) {
        const norm = normalizeFolderName(typeof folder === "string" ? folder : "");
        if (norm) out[pid] = norm;
      }
      return out;
    } catch {
      return {};
    }
  }
  function saveFolderAssignments(assignments, key = FOLDER_ASSIGN_KEY, storage = safeStorage()) {
    if (!storage) return;
    try {
      const clean = {};
      for (const [pid, folder] of Object.entries(assignments)) {
        const norm = normalizeFolderName(folder);
        if (norm) clean[pid] = norm;
      }
      storage.setItem(key, JSON.stringify(clean));
    } catch {
    }
  }
  function assignProjectToFolder(assignments, projectId, folder) {
    const next = { ...assignments };
    const norm = normalizeFolderName(folder);
    if (norm) next[projectId] = norm;
    else delete next[projectId];
    return next;
  }
  function groupProjectsByFolder(projects, assignments) {
    const named = /* @__PURE__ */ new Map();
    const ungrouped = [];
    for (const p3 of projects) {
      const folder = normalizeFolderName(assignments[p3.id]);
      if (!folder) {
        ungrouped.push(p3);
        continue;
      }
      let bucket = named.get(folder);
      if (!bucket) {
        bucket = [];
        named.set(folder, bucket);
      }
      bucket.push(p3);
    }
    const groups = [];
    for (const [folder, members] of named) {
      groups.push({ folder, label: folder, key: folder, projects: members });
    }
    if (ungrouped.length) {
      groups.push({ folder: null, label: UNGROUPED_LABEL, key: "", projects: ungrouped });
    }
    return groups;
  }
  function knownFolderNames(projects, assignments) {
    const seen = /* @__PURE__ */ new Set();
    const out = [];
    for (const p3 of projects) {
      const folder = normalizeFolderName(assignments[p3.id]);
      if (folder && !seen.has(folder)) {
        seen.add(folder);
        out.push(folder);
      }
    }
    return out;
  }
  function loadCollapsedFolders(key = FOLDER_COLLAPSE_KEY, storage = safeStorage()) {
    if (!storage) return /* @__PURE__ */ new Set();
    try {
      const raw = storage.getItem(key);
      if (!raw) return /* @__PURE__ */ new Set();
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return /* @__PURE__ */ new Set();
      return new Set(parsed.filter((x2) => typeof x2 === "string"));
    } catch {
      return /* @__PURE__ */ new Set();
    }
  }
  function saveCollapsedFolders(collapsed, key = FOLDER_COLLAPSE_KEY, storage = safeStorage()) {
    if (!storage) return;
    try {
      storage.setItem(key, JSON.stringify([...collapsed]));
    } catch {
    }
  }
  function toggleFolderCollapsed(collapsed, folderKey, key = FOLDER_COLLAPSE_KEY, storage = safeStorage()) {
    let nowCollapsed;
    if (collapsed.has(folderKey)) {
      collapsed.delete(folderKey);
      nowCollapsed = false;
    } else {
      collapsed.add(folderKey);
      nowCollapsed = true;
    }
    saveCollapsedFolders(collapsed, key, storage);
    return nowCollapsed;
  }
  function safeStorage() {
    try {
      return typeof localStorage !== "undefined" ? localStorage : void 0;
    } catch {
      return void 0;
    }
  }

  // meridian/static/dashboard-subprojects.ts
  function normParent(pid) {
    if (pid == null) return null;
    const s3 = String(pid).trim();
    return s3 ? s3 : null;
  }
  function flattenHierarchy(projects) {
    const byId = /* @__PURE__ */ new Map();
    for (const p3 of projects) byId.set(p3.id, p3);
    const childrenOf = /* @__PURE__ */ new Map();
    const topLevel = [];
    for (const p3 of projects) {
      const parentId = normParent(p3.parent_project_id);
      const parent = parentId ? byId.get(parentId) : void 0;
      if (parent && !normParent(parent.parent_project_id)) {
        let bucket = childrenOf.get(parentId);
        if (!bucket) {
          bucket = [];
          childrenOf.set(parentId, bucket);
        }
        bucket.push(p3);
      } else {
        topLevel.push(p3);
      }
    }
    const rows = [];
    for (const p3 of topLevel) {
      rows.push({ project: p3, depth: 0 });
      const kids = childrenOf.get(p3.id);
      if (kids) {
        for (const kid of kids) rows.push({ project: kid, depth: 1 });
      }
    }
    return rows;
  }
  function hasSubprojects(projects, projectId) {
    return projects.some((p3) => normParent(p3.parent_project_id) === projectId);
  }
  function eligibleParents(projects, projectId) {
    if (hasSubprojects(projects, projectId)) return [];
    const self = projects.find((p3) => p3.id === projectId);
    const currentParent = self ? normParent(self.parent_project_id) : null;
    return projects.filter((p3) => {
      if (p3.id === projectId) return false;
      if (normParent(p3.parent_project_id)) return false;
      if (p3.id === currentParent) return false;
      return true;
    });
  }

  // meridian/static/dashboard.ts
  var TABS_KEY = "meridian.openTabs";
  var ACTIVE_PROJECT_KEY = "meridian.activeProject";
  var _initialDashboardState = {
    projects: [],
    tabs: [],
    // [{id, project}]
    activeTab: null,
    panels: {},
    // tabId -> { ws, taskCache, sessionName, goalRaw, goalIsJson }
    apiKeyConfigured: false,
    // v0.6.5 — server runtime config fetched from /config on startup.
    serverConfig: { server_url: "", host: "", port: 0, version: "" },
    // workspace switcher — tenant_id of the active workspace (null = own)
    activeWorkspaceTenantId: null
  };
  var dashboardStore = createStore(() => ({ ..._initialDashboardState }));
  var state = new Proxy(_initialDashboardState, {
    get: (_t, prop) => dashboardStore.getState()[prop],
    set: (_t, prop, value) => {
      dashboardStore.setState({ [prop]: value });
      return true;
    },
    has: (_t, prop) => prop in dashboardStore.getState(),
    deleteProperty: (_t, prop) => {
      const next = { ...dashboardStore.getState() };
      delete next[prop];
      dashboardStore.setState(next, true);
      return true;
    },
    ownKeys: () => Reflect.ownKeys(dashboardStore.getState()),
    getOwnPropertyDescriptor: (_t, prop) => ({
      enumerable: true,
      configurable: true,
      value: dashboardStore.getState()[prop]
    })
  });
  window.state = state;
  window.dashboardStore = dashboardStore;
  async function hideHostedAdminControls() {
    const toHide = [
      "#restart-server-btn",
      "#stop-server-btn",
      "#banner-restart-btn",
      "#git-check-btn",
      "#update-banner"
      // check-updates and update banner
    ];
    toHide.forEach((sel) => {
      document.querySelectorAll(sel).forEach((el2) => {
        el2.style.display = "none";
      });
    });
    const ctrlRow = document.getElementById("server-controls-row");
    if (ctrlRow) ctrlRow.style.display = "none";
    const connInd = document.getElementById("connection-indicator");
    if (!isHostedAdmin()) {
      if (connInd) connInd.style.display = "none";
      document.querySelectorAll(".conn-popup").forEach((p3) => p3.remove());
    } else {
      document.querySelectorAll(".hosted-label").forEach((el2) => el2.remove());
    }
    const footer = document.querySelector(".sidebar-footer");
    if (!isHostedAdmin() && footer && !footer.querySelector(".hosted-label")) {
      const lbl = document.createElement("div");
      lbl.className = "hosted-label";
      lbl.style.cssText = "font-size:10px;color:var(--accent-green);font-family:'IBM Plex Mono',monospace;padding:4px 6px;border:1px solid var(--accent-green)44;border-radius:3px;opacity:0.8;letter-spacing:.03em";
      lbl.textContent = "\u{1F9ED} usemeridian.us";
      footer.prepend(lbl);
    }
    ensureSignOutLink2();
    try {
      const me = await api("/me");
      if (me && me.plan) {
        _renderPlanBadge(me);
        ensureSignOutLink2(me.email);
      }
    } catch (e3) {
    }
    const advLink = document.getElementById("ez-advanced-link");
    if (advLink) advLink.textContent = "Close";
  }
  function ensureSignOutLink2(emailHint) {
    const footer = document.querySelector(".sidebar-footer");
    if (!footer) return;
    if (emailHint) {
      let who = document.getElementById("signed-in-as");
      if (!who) {
        who = document.createElement("div");
        who.id = "signed-in-as";
        who.style = "margin-top:8px;font-size:10px;color:var(--muted);font-family:var(--font-mono);text-align:center;opacity:0.75;word-break:break-all;line-height:1.3";
        const existingLink = document.getElementById("signout-link");
        if (existingLink) footer.insertBefore(who, existingLink);
        else footer.appendChild(who);
      }
      who.textContent = `signed in as ${emailHint}`;
      who.title = emailHint;
    }
    if (document.getElementById("signout-link")) {
      if (emailHint) document.getElementById("signout-link").title = `Signed in as ${emailHint}`;
      return;
    }
    const link = document.createElement("a");
    link.id = "signout-link";
    link.href = "/auth/logout";
    link.textContent = "Sign out";
    link.title = emailHint ? `Signed in as ${emailHint}` : "Sign out";
    link.style = "display:block;margin-top:8px;padding:6px 10px;font-size:11px;color:var(--text);font-family:var(--font-mono);text-align:center;text-decoration:none;background:var(--surface-1);border:1px solid var(--border);border-radius:5px;opacity:1";
    link.onmouseenter = () => {
      link.style.borderColor = "var(--accent)";
      link.style.color = "var(--accent)";
    };
    link.onmouseleave = () => {
      link.style.borderColor = "var(--border)";
      link.style.color = "var(--text)";
    };
    footer.appendChild(link);
  }
  async function ensureWorkspaceSwitcher2() {
    const footer = document.querySelector(".sidebar-footer");
    if (!footer || document.getElementById("workspace-switcher")) return;
    let workspaces;
    try {
      workspaces = await fetch("/me/workspaces").then((r3) => r3.ok ? r3.json() : null);
    } catch (_2) {
      return;
    }
    if (!workspaces || workspaces.length < 2) return;
    const wrap = document.createElement("div");
    wrap.id = "workspace-switcher";
    wrap.style.cssText = "margin-top:8px";
    const label = document.createElement("div");
    label.style.cssText = "font-size:9px;color:var(--muted);font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px;opacity:.7";
    label.textContent = "workspace";
    const sel = document.createElement("select");
    sel.style.cssText = "width:100%;font-size:11px;font-family:var(--font-mono);background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 6px;cursor:pointer;outline:none";
    workspaces.forEach((ws) => {
      const opt = document.createElement("option");
      opt.value = ws.tenant_id;
      opt.textContent = ws.is_own ? "My workspace" : ws.owner_email;
      if (!state.activeWorkspaceTenantId && ws.is_own) opt.selected = true;
      if (state.activeWorkspaceTenantId === ws.tenant_id) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.onchange = async () => {
      const chosen = sel.value;
      const own = workspaces.find((w3) => w3.is_own);
      state.activeWorkspaceTenantId = own && chosen === own.tenant_id ? null : chosen;
      [...state.tabs].forEach((t3) => {
        try {
          closeTab2(t3.id);
        } catch (_2) {
        }
      });
      await loadProjects();
      const active = workspaces.find((w3) => w3.tenant_id === chosen);
      sel.title = active ? active.is_own ? "My workspace" : `${active.owner_email} (${active.role})` : "";
      _renderWorkspaceContextBadge(wrap, workspaces);
      _refreshGuestMode();
    };
    const connectLink = document.createElement("a");
    connectLink.id = "connect-db-link";
    connectLink.href = "#";
    connectLink.textContent = "\u2295 Connect your DB";
    connectLink.style.cssText = "display:block;margin-top:4px;font-size:9px;font-family:var(--font-mono);color:var(--muted);text-decoration:none;opacity:.7;letter-spacing:.03em";
    connectLink.onmouseenter = () => {
      connectLink.style.opacity = "1";
      connectLink.style.color = "var(--accent)";
    };
    connectLink.onmouseleave = () => {
      connectLink.style.opacity = ".7";
      connectLink.style.color = "var(--muted)";
    };
    connectLink.onclick = (e3) => {
      e3.preventDefault();
      showConnectDbModal();
    };
    wrap.appendChild(label);
    wrap.appendChild(sel);
    _renderWorkspaceContextBadge(wrap, workspaces);
    wrap.appendChild(connectLink);
    const existingLabel = footer.querySelector(".hosted-label");
    if (existingLabel) footer.insertBefore(wrap, existingLabel);
    else footer.prepend(wrap);
  }
  async function getActiveWorkspaceRole2() {
    if (!isHostedMode() || !state.activeWorkspaceTenantId) return "owner";
    try {
      const wss = await fetch("/me/workspaces").then((r3) => r3.ok ? r3.json() : null);
      const ws = (wss || []).find((w3) => w3.tenant_id === state.activeWorkspaceTenantId);
      return ws && ws.role || "owner";
    } catch (_2) {
      return "owner";
    }
  }
  function _renderWorkspaceContextBadge(wrap, workspaces) {
    if (!wrap) return;
    let badge = wrap.querySelector(".ws-context-badge");
    if (!badge) {
      badge = document.createElement("div");
      badge.className = "ws-context-badge";
      badge.style.cssText = "display:inline-block;margin-top:6px;padding:2px 8px;border-radius:10px;font-size:9px;font-weight:700;letter-spacing:.05em;font-family:var(--font-mono);text-transform:uppercase";
      wrap.appendChild(badge);
    }
    const active = (workspaces || []).find((w3) => state.activeWorkspaceTenantId ? w3.tenant_id === state.activeWorkspaceTenantId : w3.is_own);
    const colors = { free: "#3b82f6", trial: "#059669", standard: "#3b82f6", pro: "#7c3aed", admin: "#9ca3af", invite: "#f59e0b" };
    let label, color;
    if (active && !active.is_own) {
      label = `invite \xB7 ${active.role || "member"}`;
      color = colors.invite;
    } else {
      const plan = window.state.tenantPlan || "free";
      label = window._PLAN_LABELS && window._PLAN_LABELS[plan] || plan;
      color = colors[plan] || "#9ca3af";
    }
    badge.textContent = label;
    badge.style.background = color + "22";
    badge.style.color = color;
    badge.style.border = "1px solid " + color + "44";
  }
  function _filterTabRows(query, container, rowSelector) {
    if (!container) return;
    const q2 = (query || "").trim().toLowerCase();
    container.querySelectorAll(rowSelector).forEach((row) => {
      const hay = (row.dataset.search || row.textContent || "").toLowerCase();
      row.style.display = !q2 || hay.includes(q2) ? "" : "none";
    });
  }
  function _wireTabSearch(inputId, containerId, rowSelector) {
    const input = document.getElementById(inputId);
    const container = document.getElementById(containerId);
    if (!input || !container) return;
    if (!input.dataset.searchWired) {
      input.dataset.searchWired = "1";
      input.addEventListener("input", () => _filterTabRows(input.value, container, rowSelector));
    }
    _filterTabRows(input.value, container, rowSelector);
  }
  async function _refreshGuestMode() {
    let guest = false;
    try {
      const r3 = await getActiveWorkspaceRole2();
      guest = r3 === "viewer" || r3 === "member";
    } catch (_2) {
    }
    document.body.classList.toggle("meridian-guest", guest);
  }
  function showConnectDbModal() {
    if (document.getElementById("connect-db-modal")) return;
    const overlay = document.createElement("div");
    overlay.id = "connect-db-modal";
    overlay.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center";
    const box = document.createElement("div");
    box.style.cssText = "background:var(--surface-0);border:1px solid var(--border);border-radius:8px;padding:24px 28px;width:460px;max-width:94vw;display:flex;flex-direction:column;gap:12px";
    box.innerHTML = `

    <div style="font-weight:700;font-size:14px">Connect your Meridian DB</div>

    <div style="font-size:12px;color:var(--muted)">Enter a PostgreSQL connection string to use your own Neon (or any Postgres) project as your workspace DB.</div>

    <input id="connect-db-url" type="password" placeholder="postgresql://user:pass@host/db?sslmode=require"

      style="font-family:var(--font-mono);font-size:11px;padding:7px 10px;border:1px solid var(--border);border-radius:5px;background:var(--surface-1);color:var(--text);width:100%;box-sizing:border-box">

    <div id="connect-db-status" style="font-size:11px;min-height:16px;color:var(--muted)"></div>

    <div style="display:flex;gap:8px;justify-content:flex-end">

      <button id="connect-db-cancel" class="secondary" style="font-size:12px">Cancel</button>

      <button id="connect-db-save" style="font-size:12px">Connect</button>

    </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const urlInput = box.querySelector("#connect-db-url");
    const statusEl = box.querySelector("#connect-db-status");
    box.querySelector("#connect-db-cancel").onclick = () => overlay.remove();
    overlay.onclick = (e3) => {
      if (e3.target === overlay) overlay.remove();
    };
    box.querySelector("#connect-db-save").onclick = async () => {
      const url = urlInput.value.trim();
      if (!url) {
        statusEl.textContent = "Enter a connection string.";
        statusEl.style.color = "var(--danger,#dc2626)";
        return;
      }
      statusEl.textContent = "Connecting\u2026";
      statusEl.style.color = "var(--muted)";
      try {
        await api("/workspace/connect-db", { method: "POST", body: JSON.stringify({ url }) });
        statusEl.textContent = "Connected! Reloading\u2026";
        statusEl.style.color = "#059669";
        setTimeout(() => {
          overlay.remove();
          loadProjects();
        }, 800);
      } catch (e3) {
        statusEl.textContent = e3.message || "Connection failed \u2014 check the URL and credentials.";
        statusEl.style.color = "var(--danger,#dc2626)";
      }
    };
    urlInput.focus();
  }
  function showLocalServerControls() {
    if (isHostedMode() || isDemoMode()) return;
    ["#git-check-btn", "#restart-server-btn", "#stop-server-btn"].forEach((sel) => {
      const el2 = document.querySelector(sel);
      if (el2) el2.style.display = "";
    });
  }
  var STORAGE_KEY2 = (k3) => (isDemoMode() ? "meridian_demo_" : "meridian_") + k3.replace(/^meridian[._]/, "");
  var NORTH_STAR_MIN_HEIGHT_PX = 180;
  var GITHUB_OCTICON_PATH = "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z";
  function _summarizeApiErrorText(raw) {
    if (raw === void 0 || raw === null) return "Request failed before data could load.";
    let summary = raw;
    if (typeof raw === "string") {
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") {
          summary = parsed.detail || parsed.error || parsed.message || raw;
        }
      } catch (_2) {
        summary = raw;
      }
    }
    return String(summary).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 240) || "Request failed before data could load.";
  }
  function _projectLoadErrorInfo(path, error) {
    const status = Number.isFinite(Number(error?.status)) ? Number(error.status) : String(error?.message || "").match(/^(\d{3})\s*:/) ? parseInt(String(error.message).match(/^(\d{3})\s*:/)[1], 10) : null;
    const rawText = error?.responseText || error?.message || String(error || "Request failed");
    return {
      endpoint: path,
      status,
      summary: _summarizeApiErrorText(rawText),
      at: Date.now()
    };
  }
  function wireProjectLoadRetry2(container, projectId) {
    container?.querySelectorAll("[data-project-retry]").forEach((btn) => {
      btn.onclick = () => retryProjectSurface(projectId);
    });
  }
  function renderProjectLoadError2(projectId, title, path, error) {
    const info = _projectLoadErrorInfo(path, error);
    const statusLabel = info.status ? `HTTP ${info.status}` : "Request failed";
    return `

    <div class="project-load-error">

      <div class="project-load-error__title">${escapeHtml(title)}</div>

      <div class="project-load-error__meta">${escapeHtml(statusLabel)} \xB7 <code>${escapeHtml(info.endpoint)}</code></div>

      <div class="project-load-error__body">${escapeHtml(info.summary)}</div>

      <div class="project-load-error__actions">

        <button class="secondary" data-project-retry="1" style="padding:4px 10px;font-size:10px">Retry failed loads</button>

      </div>

    </div>

  `;
  }
  function recordProjectLoadError2(projectId, path, error) {
    const panel = getPanelState(projectId);
    panel.loadErrors = panel.loadErrors || {};
    const info = _projectLoadErrorInfo(path, error);
    panel.loadErrors[path] = info;
    renderProjectLoadAlert(projectId);
    return info;
  }
  function clearProjectLoadError2(projectId, path) {
    const panel = getPanelState(projectId);
    if (!panel.loadErrors || !panel.loadErrors[path]) return;
    delete panel.loadErrors[path];
    renderProjectLoadAlert(projectId);
  }
  function renderProjectLoadAlert(projectId) {
    const host = document.getElementById(`project-fetch-alert-${projectId}`);
    if (!host) return;
    const panel = getPanelState(projectId);
    const errors = Object.values(panel.loadErrors || {}).sort((a3, b2) => b2.at - a3.at);
    if (!errors.length) {
      host.style.display = "none";
      host.innerHTML = "";
      return;
    }
    const visible = errors.slice(0, 3);
    const statusText = visible.length === 1 ? "A backing request failed, so part of this panel may be incomplete." : "Multiple backing requests failed, so part of this panel may be incomplete.";
    const moreText = errors.length > visible.length ? `<div class="project-fetch-alert__meta">+${errors.length - visible.length} more failing request${errors.length - visible.length === 1 ? "" : "s"} hidden.</div>` : "";
    host.style.display = "block";
    host.innerHTML = `

    <div class="project-fetch-alert__title">Project data failed to load</div>

    <div class="project-fetch-alert__summary">${escapeHtml(statusText)}</div>

    <div class="project-fetch-alert__list">

      ${visible.map((info) => {
      const statusLabel = info.status ? `HTTP ${info.status}` : "Request failed";
      return `

          <div class="project-fetch-alert__item">

            <div class="project-fetch-alert__endpoint"><code>${escapeHtml(info.endpoint)}</code></div>

            <div class="project-fetch-alert__meta">${escapeHtml(statusLabel)} \xB7 ${escapeHtml(info.summary)}</div>

          </div>

        `;
    }).join("")}

    </div>

    ${moreText}

    <div class="project-fetch-alert__actions">

      <button class="secondary" id="project-fetch-retry-${projectId}" style="padding:4px 10px;font-size:10px">Retry failed loads</button>

    </div>

  `;
    const retryBtn = document.getElementById(`project-fetch-retry-${projectId}`);
    if (retryBtn) retryBtn.onclick = () => retryProjectSurface(projectId);
  }
  async function retryProjectSurface(projectId) {
    const panel = getPanelState(projectId);
    await Promise.allSettled([
      refreshGoal(projectId),
      refreshSessions(projectId),
      refreshTasks(projectId)
    ]);
    const activeVtab = panel.activeVtab || "status";
    if (activeVtab === "live") await refreshLiveTab(projectId);
    if (activeVtab === "files") await loadFilesTab(projectId);
    if (activeVtab === "timeline") await loadTimeline2(projectId);
    if (activeVtab === "rewind") await loadRewindTab(projectId, panel.rewindDays || 7);
    if (activeVtab === "queue") {
      await loadQueue(projectId);
      await updateLiveFeed(projectId);
      await loadRecentRuns(projectId);
    }
    if (activeVtab === "team") await loadTeamTab(projectId);
    if (activeVtab === "notes") await loadNotesTab(projectId);
    if (activeVtab === "hitl") await loadHitlTab(projectId);
    if (activeVtab === "docs") await loadDocsTab(projectId);
    if (activeVtab === "settings") await loadSettingsTab(projectId);
    if (activeVtab === "codeintel") await loadCodeIntelTab(projectId);
  }
  function syncSidebarActiveProject() {
    document.querySelectorAll(".project-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.projectId === state.activeTab);
    });
  }
  function autosizeGoalField(el2, minPx = NORTH_STAR_MIN_HEIGHT_PX) {
    if (!el2) return;
    el2.style.height = "auto";
    el2.style.height = `${Math.max(el2.scrollHeight, minPx)}px`;
  }
  function githubIconSvg2(size = 12, color = "currentColor") {
    return `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" focusable="false" style="color:${color};flex-shrink:0"><path d="${GITHUB_OCTICON_PATH}"></path></svg>`;
  }
  function getConstitutionLimit(projectId) {
    const panel = getPanelState(projectId);
    const parsed = parseInt(String(panel._projectSettings?.max_pinned_decisions || ""), 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_PINNED_DECISIONS;
  }
  async function loadProjectSettings2(projectId, opts = {}) {
    const panel = getPanelState(projectId);
    if (!opts.force && panel._projectSettings) return panel._projectSettings;
    if (!opts.force && panel._projectSettingsPromise) return panel._projectSettingsPromise;
    panel._projectSettingsPromise = projectApi(projectId, `/projects/${projectId}/settings`).then((settings) => {
      panel._projectSettings = settings || { project_id: projectId, max_pinned_decisions: DEFAULT_MAX_PINNED_DECISIONS };
      return panel._projectSettings;
    }).finally(() => {
      panel._projectSettingsPromise = null;
    });
    return panel._projectSettingsPromise;
  }
  async function saveProjectSettings2(projectId, patch) {
    const panel = getPanelState(projectId);
    const settings = await api(`/projects/${projectId}/settings`, {
      method: "PATCH",
      body: JSON.stringify(patch || {})
    });
    panel._projectSettings = settings || { project_id: projectId, max_pinned_decisions: DEFAULT_MAX_PINNED_DECISIONS };
    return panel._projectSettings;
  }
  async function loadExecutorRulesSection(projectId) {
    const host = document.getElementById(`settings-body-${projectId}`);
    if (!host) return;
    const _existing = document.getElementById(`executor-rules-section-${projectId}`);
    if (_existing) _existing.remove();
    const section = document.createElement("div");
    section.id = `executor-rules-section-${projectId}`;
    host.appendChild(section);
    try {
      const [data, defaultData, settingsData] = await Promise.all([
        projectApi(projectId, `/projects/${projectId}/agent-instructions`),
        projectApi(projectId, `/projects/${projectId}/agent-instructions/default`),
        projectApi(projectId, `/projects/${projectId}/settings`)
      ]);
      const current = data.agent_instructions || "";
      const defaultText = defaultData.default_agent_instructions || "";
      const codeIntelEnabled = settingsData ? !!settingsData.code_intel_enabled : false;
      section.innerHTML = `

      <div style="margin-bottom:12px">

        <div style="font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase;margin-bottom:4px">Executor Rules</div>

        <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
          These rules are injected into every <code>start_session</code> response so AI coding
          sessions pick them up automatically \u2014 no per-repo file required.
        </div>

        <textarea id="agent-instructions-${projectId}"
          rows="24"
          style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:8px;resize:vertical;outline:none;line-height:1.5"
          placeholder="Enter executor rules\u2026"
        >${escapeHtml(current)}</textarea>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">

          <span id="agent-instructions-chars-${projectId}" style="font-size:10px;color:var(--muted)">${current.length} chars</span>

          <span style="display:flex;gap:6px">

            <button class="secondary admin-only" id="agent-instructions-reset-${projectId}"
              style="padding:2px 10px;font-size:10px"
              title="Restore Meridian default rules">Reset to defaults</button>

            <button class="primary admin-only" id="agent-instructions-save-${projectId}"
              style="padding:2px 10px;font-size:10px">Save</button>

          </span>

        </div>

      </div>

    `;
      const ta = document.getElementById(`agent-instructions-${projectId}`);
      const charEl = document.getElementById(`agent-instructions-chars-${projectId}`);
      ta.addEventListener("input", () => {
        charEl.textContent = `${ta.value.length} chars`;
      });
      document.getElementById(`agent-instructions-save-${projectId}`).onclick = async () => {
        try {
          await api(`/projects/${projectId}/agent-instructions`, {
            method: "PATCH",
            body: JSON.stringify({ agent_instructions: ta.value })
          });
          toast("Executor rules saved");
        } catch (e3) {
          toast("Save failed: " + e3.message, true);
        }
      };
      document.getElementById(`agent-instructions-reset-${projectId}`).onclick = async () => {
        if (!confirm("Reset to Meridian default executor rules? Your custom rules will be replaced.")) return;
        try {
          const r3 = await api(`/projects/${projectId}/agent-instructions`, {
            method: "PATCH",
            body: JSON.stringify({ agent_instructions: null })
          });
          ta.value = r3.agent_instructions || defaultText;
          charEl.textContent = `${ta.value.length} chars`;
          toast("Reset to defaults");
        } catch (e3) {
          toast("Reset failed: " + e3.message, true);
        }
      };
      const ciBlock = document.createElement("div");
      ciBlock.style.cssText = "margin-top:18px;padding-top:14px;border-top:1px solid var(--border)";
      ciBlock.innerHTML = `

      <div style="font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase;margin-bottom:4px">Code Intelligence</div>

      <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">

        Enable to connect <strong>codebase-memory-mcp</strong> \u2014 structural graph queries instead of raw file reads.
        120\xD7 fewer tokens, sub-ms lookups. <a href="https://github.com/DeusData/codebase-memory-mcp" target="_blank" style="color:var(--accent)">GitHub \u2197</a>
      </div>

      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:11px;color:var(--text)">

        <input type="checkbox" id="code-intel-toggle-${projectId}" ${codeIntelEnabled ? "checked" : ""}
          style="width:14px;height:14px;accent-color:var(--accent);cursor:pointer">

        Enable Code Intelligence for this project

      </label>

      <div id="code-intel-info-${projectId}" style="margin-top:10px;display:${codeIntelEnabled ? "block" : "none"}">

        <div style="font-size:10px;color:var(--muted);margin-bottom:4px">Install (once, on the machine running the tunnel):</div>

        <code style="display:block;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--text);word-break:break-all">curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash</code>

        <div style="font-size:10px;color:var(--muted);margin-top:8px;margin-bottom:4px">Add to claude.ai after starting <code>meridian --tunnel</code>:</div>

        <code id="code-intel-url-${projectId}" style="display:block;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--text);word-break:break-all">https://usemeridian.us/code/mcp/{your-tenant-id}/mcp</code>

      </div>

    `;
      section.appendChild(ciBlock);
      const ciToggle = document.getElementById(`code-intel-toggle-${projectId}`);
      const ciInfo = document.getElementById(`code-intel-info-${projectId}`);
      ciToggle.onchange = async () => {
        const enabled = ciToggle.checked ? 1 : 0;
        ciInfo.style.display = enabled ? "block" : "none";
        try {
          await api(`/projects/${projectId}/settings`, {
            method: "PATCH",
            body: JSON.stringify({ code_intel_enabled: enabled })
          });
          toast(enabled ? "Code Intelligence enabled" : "Code Intelligence disabled");
        } catch (e3) {
          toast("Save failed: " + e3.message, true);
        }
      };
    } catch (e3) {
      section.innerHTML = `<div class="empty" style="color:var(--error)">Failed to load executor rules: ${escapeHtml(e3.message)}</div>`;
    }
  }
  var _DEMO_TOUR_STEPS = [
    {
      vtab: null,
      target: () => document.querySelector('.session-list, .sidebar-sessions, [data-tour="sessions"], .sidebar'),
      title: "AI coding sessions",
      body: "Each row is one Claude Code run. Multiple sessions work in parallel on the same project \u2014 no collisions.",
      position: "right"
    },
    {
      vtab: "status",
      title: "Status & sessions",
      body: "The default panel: live session status and the task log. Every meaningful action a session takes shows up here in real time.",
      position: "bottom"
    },
    {
      vtab: "live",
      title: "Live view",
      body: "A right-now feed of what every active session is doing this second \u2014 tool calls, file claims, and progress as they happen.",
      position: "bottom"
    },
    {
      vtab: "goal",
      gtab: "north-star",
      title: "Shared goal state",
      body: "The north star and version goal every session reads on startup \u2014 so parallel runs stay aligned on one plan.",
      position: "bottom"
    },
    {
      vtab: "goal",
      gtab: "decisions",
      title: "Pinned decisions",
      body: "An append-only constitution of architectural calls. New sessions inherit them automatically instead of relitigating settled choices.",
      position: "bottom"
    },
    {
      vtab: "queue",
      title: "Work queue",
      body: "Pending tasks claimed atomically \u2014 parallel sessions grab work without stepping on each other.",
      position: "bottom"
    },
    {
      vtab: "timeline",
      title: "Activity timeline",
      body: "Every session laid out over time \u2014 when each ran, what changed, and how long each task took.",
      position: "bottom"
    },
    {
      vtab: "files",
      title: "Files",
      body: "File claims and previews. Sessions lock the files they are editing so two runs never clobber the same file.",
      position: "bottom"
    },
    {
      vtab: "hitl",
      title: "Human-in-the-loop",
      body: "When a session needs a human decision it parks the question here and waits \u2014 you answer, it resumes. No silent guessing.",
      position: "bottom"
    },
    {
      vtab: "notes",
      title: "Project notes",
      body: "A shared per-project wiki every session can read and append to \u2014 context that outlives any single run.",
      position: "bottom"
    },
    {
      vtab: "settings",
      title: "Settings",
      body: "Notifications, hooks, and integrations. In your own project this is where you wire Meridian into your AI tools.",
      position: "bottom"
    },
    {
      vtab: null,
      target: () => null,
      // centered finish step
      title: "You're all set",
      body: "Explore any project or session. When you're ready to coordinate your own AI sessions \u2014 sign in and create a project.",
      position: "center"
    }
  ];
  function _demoTourDone2() {
    try {
      return localStorage.getItem(STORAGE_KEY2("tour.done")) === "1";
    } catch (e3) {
      return false;
    }
  }
  function _demoTourSavedStep2() {
    try {
      return parseInt(localStorage.getItem(STORAGE_KEY2("tour.step")) || "0", 10) || 0;
    } catch (e3) {
      return 0;
    }
  }
  function _demoTourSaveStep(step) {
    try {
      localStorage.setItem(STORAGE_KEY2("tour.step"), String(step));
    } catch (e3) {
    }
  }
  function _demoTourMarkDone() {
    try {
      localStorage.setItem(STORAGE_KEY2("tour.done"), "1");
      localStorage.removeItem(STORAGE_KEY2("tour.step"));
    } catch (e3) {
    }
  }
  function _demoTourClose() {
    document.getElementById("demo-tour-tooltip")?.remove();
    document.getElementById("demo-tour-highlight")?.remove();
  }
  function _tourActivateVtab(vtab, gtab) {
    const pid = state.activeTab;
    if (!pid || !vtab) return;
    const btn = document.querySelector(`#vtab-strip-${pid} .vtab-btn[data-vtab="${vtab}"]`);
    if (btn) btn.click();
    if (gtab) {
      const gbtn = document.querySelector(`#drawer-goal-${pid} .goal-subtab-btn[data-gtab="${gtab}"]`);
      if (gbtn) gbtn.click();
    }
  }
  function startDemoTour2(step) {
    _demoTourClose();
    if (step < 0) step = 0;
    if (step >= _DEMO_TOUR_STEPS.length) {
      _demoTourMarkDone();
      return;
    }
    _demoTourSaveStep(step);
    const s3 = _DEMO_TOUR_STEPS[step];
    try {
      _tourActivateVtab(s3.vtab, s3.gtab);
    } catch (e3) {
    }
    const isLast = step === _DEMO_TOUR_STEPS.length - 1;
    let targetEl = null;
    if (s3.target) {
      targetEl = s3.target();
    } else if (s3.vtab) {
      const pid = state.activeTab;
      targetEl = pid ? document.querySelector(`#vtab-strip-${pid} .vtab-btn[data-vtab="${s3.vtab}"]`) : null;
    }
    if (targetEl) {
      const rect = targetEl.getBoundingClientRect();
      const ring = document.createElement("div");
      ring.id = "demo-tour-highlight";
      ring.style.cssText = `position:fixed;z-index:29998;pointer-events:none;

      top:${rect.top - 4}px;left:${rect.left - 4}px;

      width:${rect.width + 8}px;height:${rect.height + 8}px;

      border:2px solid #7c3aed;border-radius:8px;

      box-shadow:0 0 0 4000px rgba(0,0,0,0.45);`;
      document.body.appendChild(ring);
    }
    const tip = document.createElement("div");
    tip.id = "demo-tour-tooltip";
    const stepLabel = `${step + 1} / ${_DEMO_TOUR_STEPS.length}`;
    tip.innerHTML = `

    <div style="font-size:.82rem;color:#6c8fff;font-weight:600;margin-bottom:8px;letter-spacing:.3px">${stepLabel}</div>

    <div style="font-size:1.12rem;font-weight:700;color:#e8eaf0;margin-bottom:10px">${s3.title}</div>

    <div style="font-size:.98rem;color:#c4c6d4;line-height:1.65;margin-bottom:18px">${s3.body}</div>

    <div style="display:flex;gap:8px;align-items:center">

      <button id="demo-tour-finish" style="background:none;border:none;color:#6b7280;cursor:pointer;font-size:.86rem;padding:4px 6px;font-family:inherit;text-decoration:underline">Finish tutorial</button>

      <div style="flex:1"></div>

      ${step > 0 ? '<button id="demo-tour-back" style="background:none;border:1px solid #3a3d48;border-radius:6px;color:#c4c6d4;cursor:pointer;font-size:.9rem;padding:7px 13px;font-family:inherit">\u2190 Back</button>' : ""}

      <button id="demo-tour-next" style="background:#7c3aed;border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:.92rem;padding:7px 18px;font-family:inherit">

        ${isLast ? "Done" : "Next \u2192"}

      </button>

    </div>`;
    tip.style.cssText = `position:fixed;z-index:30000;background:#1e2029;border:1px solid #7c3aed88;

    border-radius:10px;padding:18px 22px;width:330px;max-width:calc(100vw - 24px);

    box-shadow:0 8px 32px rgba(0,0,0,0.6);font-family:inherit;`;
    if (!targetEl || s3.position === "center") {
      tip.style.top = "50%";
      tip.style.left = "50%";
      tip.style.transform = "translate(-50%, -50%)";
    } else {
      const rect = targetEl.getBoundingClientRect();
      const PAD = 12;
      if (s3.position === "right") {
        tip.style.top = `${Math.min(rect.top, window.innerHeight - 200)}px`;
        tip.style.left = `${rect.right + PAD}px`;
      } else {
        tip.style.top = `${Math.min(rect.bottom + PAD, window.innerHeight - 200)}px`;
        tip.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 342))}px`;
      }
    }
    document.body.appendChild(tip);
    document.getElementById("demo-tour-next").onclick = () => {
      if (isLast) {
        _demoTourClose();
        _demoTourMarkDone();
      } else startDemoTour2(step + 1);
    };
    const backBtn = document.getElementById("demo-tour-back");
    if (backBtn) backBtn.onclick = () => startDemoTour2(step - 1);
    document.getElementById("demo-tour-finish").onclick = () => {
      _demoTourClose();
      _demoTourMarkDone();
    };
  }
  function resumeDemoTour() {
    if (_demoTourDone2()) return;
    startDemoTour2(_demoTourSavedStep2());
  }
  async function loadServerConfig() {
    try {
      const cfg = await api("/config");
      state.serverConfig = cfg || state.serverConfig;
      const verEl = document.getElementById("server-version");
      if (verEl && cfg?.version) verEl.textContent = `v${cfg.version}`;
      if (cfg?.demo_mode && !document.getElementById("demo-mode-banner")) {
        const b2 = document.createElement("div");
        b2.id = "demo-mode-banner";
        b2.style = "position:fixed;top:0;left:0;right:0;z-index:9999;background:#7c3aed;color:#fff;text-align:center;padding:4px 12px;font-size:11px;font-family:inherit;letter-spacing:0.02em";
        b2.innerHTML = 'Preview mode \u2014 read only \xB7 <a href="/auth/login" style="color:#fff;text-decoration:underline;font-weight:600">Sign in \u2192</a>';
        document.body.prepend(b2);
        document.body.style.paddingTop = parseInt(document.body.style.paddingTop || "0", 10) + 22 + "px";
        if (!_demoTourDone2()) {
          resumeDemoTour();
        }
      }
      if (cfg?.demo_mode) hideDemoAdminControls();
      _updateConnectionIndicator(cfg);
    } catch (e3) {
    }
    if (window.location.pathname.startsWith("/demo")) {
      try {
        localStorage.removeItem(STORAGE_KEY2(TABS_KEY));
      } catch (e3) {
      }
      try {
        localStorage.removeItem(STORAGE_KEY2(ACTIVE_PROJECT_KEY));
      } catch (e3) {
      }
      hideDemoAdminControls();
      showDemoOnboardingOverlay();
    }
    try {
      const me = await api("/me");
      if (me && me.plan) {
        state.tenantPlan = me.plan;
        state.tenantEmail = me.email || "";
        state.tenantHasStripe = !!me.has_stripe_customer;
        state.tenantIsInternal = !!me.is_internal;
        state.tenantDaysRemaining = me.days_remaining;
        state.tenantExpired = !!me.expired;
        state.tenantExpiresAt = me.inactivity_expires_at || null;
        _renderPlanBadge(me);
        updateGitHubConnectionIndicator(me);
        updateTunnelConnectionIndicator(me);
        _armAccountSwitchWatch(me.email || "");
      }
    } catch (e3) {
    }
  }
  function _armAccountSwitchWatch(loadedEmail) {
    if (!isHostedMode()) return;
    if (state._acctWatchArmed) return;
    state._acctWatchArmed = true;
    state.loadedAccountEmail = loadedEmail || "";
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        _checkAccountSwitch2();
        _refreshOnFocus();
      }
    });
    setInterval(_checkAccountSwitch2, 6e4);
  }
  async function _refreshOnFocus() {
    try {
      await loadProjects();
    } catch (_2) {
    }
    const pid = state.activeTab;
    if (!pid) return;
    try {
      await Promise.allSettled([
        refreshGoal(pid),
        loadPinnedDecisions(pid),
        loadNotesTab(pid),
        refreshProjectCountBadges(pid),
        refreshHitl(pid),
        loadSprintBoard(pid)
      ]);
    } catch (_2) {
    }
  }
  async function _checkAccountSwitch2() {
    if (document.getElementById("account-switch-banner")) return;
    let me;
    try {
      me = await api("/me");
    } catch (_2) {
      return;
    }
    const now = me && me.email || "";
    const base = state.loadedAccountEmail || "";
    if (now && base && now !== base) _showAccountSwitchBanner(now);
  }
  function _showAccountSwitchBanner(newEmail) {
    if (document.getElementById("account-switch-banner")) return;
    const b2 = document.createElement("div");
    b2.id = "account-switch-banner";
    b2.style = "position:fixed;top:0;left:0;right:0;z-index:10000;background:#b45309;color:#fff;text-align:center;padding:6px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em;display:flex;align-items:center;justify-content:center;gap:12px";
    b2.innerHTML = `<span>You're now signed in as <strong>${escapeHtml(newEmail)}</strong> in another tab. Refresh to load this account.</span><button id="account-switch-refresh" style="background:#fff;color:#b45309;font-weight:700;border:none;text-decoration:none;padding:2px 12px;border-radius:4px;white-space:nowrap;cursor:pointer">Refresh</button>`;
    document.body.prepend(b2);
    document.body.style.paddingTop = parseInt(document.body.style.paddingTop || "0", 10) + 30 + "px";
    const btn = document.getElementById("account-switch-refresh");
    if (btn) btn.onclick = () => location.reload();
  }
  function updateGitHubConnectionIndicator(source) {
    const badge = document.getElementById("connection-github");
    if (!badge || !source) return;
    const connected = !!(source.github_connected ?? source.connected);
    const repo = source.github_repo || source.repo || "";
    const branch = source.github_branch || source.branch || "main";
    badge.style.display = connected ? "inline-flex" : "none";
    badge.title = connected ? repo ? `GitHub repo connected: ${repo} (${branch})` : "GitHub repo connected" : "GitHub repo not connected";
  }
  function updateTunnelConnectionIndicator(me) {
    const wrap = document.getElementById("connection-tunnel");
    if (!wrap || !me) return;
    const isPro = me.plan === "pro" || me.plan === "admin" || me.is_internal;
    if (!isPro) {
      wrap.style.display = "none";
      return;
    }
    const active = !!me.tunnel_active;
    const dot = document.getElementById("connection-tunnel-dot");
    wrap.style.display = "inline-flex";
    wrap.title = active ? "Pro tunnel connected" : "Pro tunnel disconnected \u2014 run `meridian --tunnel`";
    if (dot) dot.style.background = active ? "#22c55e" : "#ef4444";
    wrap.style.borderColor = active ? "#22c55e55" : "var(--border)";
    wrap.style.color = active ? "#22c55e" : "var(--muted)";
  }
  function _updateConnectionIndicator(cfg) {
    if (!cfg) return;
    if (isHostedMode() && !isHostedAdmin()) return;
    const wrap = document.getElementById("connection-indicator");
    const label = document.getElementById("connection-label");
    const dot = document.getElementById("connection-dot");
    const switcher = document.getElementById("connection-switcher");
    if (!wrap || !label) return;
    wrap.style.display = "inline-flex";
    if (cfg.demo_mode) {
      label.textContent = "demo (" + (cfg.demo_db || "sqlite") + ")";
      dot.style.background = "var(--accent-green)";
      wrap.style.cursor = "default";
      wrap.title = "Demo environment \u2014 read only";
      wrap.onclick = null;
      return;
    }
    const name = cfg.connection_name || (cfg.db === "postgres" ? "postgres" : "local");
    const dbType = cfg.db || "sqlite";
    let connLabelText = name + " (" + dbType + ")";
    if (cfg.db_host) connLabelText += ": " + cfg.db_host;
    label.textContent = connLabelText;
    dot.style.background = dbType === "postgres" ? "var(--accent)" : "var(--accent-green)";
    const conns = cfg.connections || [];
    if (conns.length > 0 && wrap) {
      wrap.style.cursor = "pointer";
      wrap.title = "Click to switch connection";
      wrap.onclick = (e3) => {
        e3.stopPropagation();
        document.querySelectorAll(".conn-popup").forEach((p3) => p3.remove());
        const popup = document.createElement("div");
        popup.className = "conn-popup";
        popup.style.cssText = "position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:180px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono);padding:6px 0";
        const rect = wrap.getBoundingClientRect();
        popup.style.bottom = window.innerHeight - rect.top + 4 + "px";
        popup.style.left = rect.left + "px";
        const hdr = document.createElement("div");
        hdr.style.cssText = "padding:4px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);margin-bottom:4px";
        hdr.textContent = "Select connection";
        popup.appendChild(hdr);
        const hosted = isHostedMode();
        const adminFull = !hosted || isHostedAdmin();
        const activeName = cfg.connection_name || (cfg.db === "postgres" ? "env (postgres)" : "local");
        let displayConns = (conns || []).map((c3) => ({ ...c3, active: c3.name === activeName }));
        if (hosted) {
          displayConns = displayConns.filter((c3) => (c3.type || "sqlite") === "postgres");
        }
        if (!displayConns.find((c3) => c3.active)) {
          displayConns.unshift({ name: activeName, type: cfg.db, active: true });
        }
        displayConns.forEach((c3) => {
          const item = document.createElement("div");
          item.style.cssText = `padding:6px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;justify-content:space-between;${c3.active ? "color:var(--accent)" : "color:var(--text)"}`;
          const left = document.createElement("div");
          left.style.cssText = "display:flex;align-items:center;gap:8px;flex:1;min-width:0";
          const dot2 = document.createElement("span");
          dot2.style.cssText = `display:inline-block;width:6px;height:6px;border-radius:50%;flex-shrink:0;background:${c3.active ? "var(--accent)" : "var(--muted)"}`;
          left.appendChild(dot2);
          let connLabel = c3.name + " (" + (c3.type || "sqlite") + ")";
          if (c3.url_masked) {
            try {
              const hostMatch = c3.url_masked.match(/@([^/:?]+)/);
              if (hostMatch) {
                const host = hostMatch[1];
                connLabel += " \u2014 " + (host.length > 22 ? host.slice(0, 20) + "\u2026" : host);
              }
            } catch (_2) {
            }
          }
          left.appendChild(document.createTextNode(connLabel));
          const _nonprod = /\b(dev|test|staging|sandbox)\b/i;
          if (_nonprod.test(c3.name)) {
            const badge = document.createElement("span");
            badge.textContent = "\u26A0";
            badge.title = "Non-production connection";
            badge.style.cssText = "color:var(--accent-yellow,#f5a623);font-size:11px;flex-shrink:0;margin-left:2px";
            left.appendChild(badge);
          }
          item.appendChild(left);
          if (c3.name && c3.name !== "local" && adminFull) {
            const del = document.createElement("button");
            del.textContent = "\xD7";
            del.title = c3.active ? "Remove connection (will switch to local)" : "Remove connection";
            del.style.cssText = "background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:0 2px;line-height:1;flex-shrink:0";
            del.onmouseenter = () => del.style.color = "var(--status-failed)";
            del.onmouseleave = () => del.style.color = "var(--muted)";
            del.onclick = async (e4) => {
              e4.stopPropagation();
              const msg = c3.active ? 'Remove active connection "' + c3.name + '"? This will switch to local (SQLite). Requires restart.' : 'Remove connection "' + c3.name + '"?';
              if (!confirm(msg)) return;
              try {
                await api("/config/connections/" + encodeURIComponent(c3.name), { method: "DELETE" });
                popup.remove();
                await loadServerConfig();
              } catch (ex) {
                toast("Remove failed: " + ex.message, true);
              }
            };
            item.appendChild(del);
          }
          item.onmouseenter = () => {
            if (!c3.active) left.style.color = "var(--accent)";
            item.style.background = "var(--surface-3)";
          };
          item.onmouseleave = () => {
            left.style.color = "";
            item.style.background = "";
          };
          item.onclick = async (e4) => {
            if (e4.target.tagName === "BUTTON") return;
            popup.remove();
            if (c3.active) return;
            try {
              await api("/config/connections", { method: "POST", body: JSON.stringify({ name: c3.name, activate: true }) });
              await loadServerConfig();
              if (isHostedMode()) {
                toast('Connection saved as "' + c3.name + '" \u2014 applies on next server restart');
              } else {
                const banner = document.getElementById("update-banner");
                const bannerSpan = banner?.querySelector("span");
                if (banner) {
                  banner.style.display = "block";
                  if (bannerSpan) bannerSpan.textContent = "\u26A0\uFE0F Connection changed to " + c3.name + " \u2014 restart to apply";
                }
              }
              const dot3 = document.getElementById("connection-dot");
              const label2 = document.getElementById("connection-label");
              if (dot3) dot3.style.background = "var(--accent)";
              if (label2) label2.textContent = c3.name + " (" + (c3.type || "sqlite") + ")";
            } catch (e5) {
              console.error("Switch failed:", e5);
              toast("Switch failed: " + e5.message, true);
            }
          };
          popup.appendChild(item);
        });
        if (adminFull) {
          const addItem = document.createElement("div");
          addItem.style.cssText = "padding:6px 12px;cursor:pointer;color:var(--muted);border-top:1px solid var(--border);margin-top:4px";
          addItem.textContent = "+ Add connection...";
          addItem.onmouseenter = () => addItem.style.color = "var(--text)";
          addItem.onmouseleave = () => addItem.style.color = "var(--muted)";
          addItem.onclick = () => {
            popup.remove();
            document.getElementById("conn-setup-modal").style.display = "flex";
          };
          popup.appendChild(addItem);
        }
        if (cfg.toml_path && adminFull && !hosted) {
          const pathRow = document.createElement("div");
          pathRow.style.cssText = "padding:4px 12px 6px;color:var(--muted);font-size:9px;border-top:1px solid var(--border);margin-top:2px;word-break:break-all";
          pathRow.textContent = "\u{1F4C4} " + cfg.toml_path;
          popup.appendChild(pathRow);
        }
        document.body.appendChild(popup);
        setTimeout(() => document.addEventListener("click", () => popup.remove(), { once: true }), 0);
      };
    }
    if (conns.length > 1 && switcher) {
      switcher.style.display = "none";
      switcher.innerHTML = conns.map(
        (c3) => `<option value="${c3.name}" ${c3.active ? "selected" : ""}>${c3.name}</option>`
      ).join("");
      switcher.onchange = async () => {
        try {
          const sel = switcher.value;
          const conn = (cfg.connections || []).find((c3) => c3.name === sel) || {};
          await api("/config/connections", {
            method: "POST",
            body: JSON.stringify({ name: sel, type: conn.type || "sqlite", activate: true })
          });
          if (isHostedMode()) {
            toast('Connection saved as "' + sel + '" \u2014 applies on next server restart');
          } else if (conn.type === "postgres") {
            toast("Switching to " + sel + " \u2014 restarting\u2026");
            await _doRestart();
          } else {
            toast("Switched to " + sel + " \u2014 restart to apply");
          }
        } catch (e3) {
          toast("Switch failed: " + e3.message, true);
        }
      };
    }
  }
  async function checkGitStatus() {
    if (isDemoMode()) {
      hideDemoAdminControls();
      return;
    }
    if (isHostedMode()) return;
    const btn = document.getElementById("git-check-btn");
    if (btn) {
      btn.textContent = "checking\u2026";
      btn.style.color = "var(--muted)";
    }
    try {
      const s3 = await api("/admin/git-status");
      if (!s3.ok) throw new Error(s3.error || "git check failed");
      if (s3.behind > 0) {
        const banner = document.getElementById("update-banner");
        const span = banner?.querySelector("span");
        if (banner) {
          banner.style.display = "block";
          if (span) span.textContent = `\u26A0\uFE0F ${s3.behind} commit${s3.behind > 1 ? "s" : ""} behind origin/${s3.branch} (${s3.local_hash} \u2260 ${s3.remote_hash}) \u2014 git pull recommended`;
        }
        if (btn) {
          btn.textContent = `\u2193 ${s3.behind} behind`;
          btn.style.color = "var(--status-failed)";
        }
      } else {
        if (btn) {
          btn.textContent = "\u2713 up to date";
          btn.style.color = "var(--status-done)";
        }
        setTimeout(() => {
          if (btn) {
            btn.textContent = "check updates";
            btn.style.color = "var(--muted)";
          }
        }, 3e3);
      }
    } catch (e3) {
      if (btn) {
        btn.textContent = "check updates";
        btn.style.color = "var(--muted)";
      }
    }
  }
  async function _doRestart(confirmFirst = true) {
    if (confirmFirst && !confirm("This will restart the server and disconnect all active sessions on this machine. Are you sure?")) {
      return;
    }
    try {
      await fetch("/admin/restart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true })
      });
    } catch (_2) {
    }
    document.querySelectorAll("#restart-server-btn, #banner-restart-btn").forEach((b2) => {
      b2.textContent = "Restarting\u2026";
      b2.disabled = true;
    });
    const started = Date.now();
    while (Date.now() - started < 3e4) {
      await new Promise((r3) => setTimeout(r3, 2e3));
      try {
        const r3 = await fetch("/health");
        if (r3.ok) {
          window.location.href = window.location.pathname + "?_cb=" + Date.now();
          return;
        }
      } catch (_2) {
      }
    }
    document.querySelectorAll("#restart-server-btn, #banner-restart-btn").forEach((b2) => {
      b2.textContent = "Restart timed out";
      b2.disabled = false;
    });
    toast("Server did not come back within 30s \u2014 start manually", true);
  }
  async function loadConfig() {
    try {
      const cfg = await api("/config/api-key");
      state.apiKeyConfigured = !!cfg.configured;
      const hintEl = document.getElementById("mcp-hint");
      if (hintEl) hintEl.style.display = cfg.configured ? "none" : "block";
      const methodEl = document.getElementById("auth-method");
      if (cfg.method === "oauth") {
        methodEl.textContent = "Auth: Claude Max OAuth";
        methodEl.style.display = "block";
      } else if (cfg.method === "api_key") {
        methodEl.textContent = "Auth: API key";
        methodEl.style.display = "block";
      } else {
        methodEl.style.display = "none";
      }
    } catch (e3) {
    }
  }
  async function loadProjects() {
    const list = document.getElementById("project-list");
    try {
      state.projects = await api("/projects");
    } catch (e3) {
      state.projects = [];
      if (list) {
        list.innerHTML = `<div class="empty" style="color:var(--status-failed);padding:6px 4px">projects failed: ${escapeHtml(e3.message)}</div>`;
      }
      return;
    }
    list.innerHTML = "";
    const assignments = loadFolderAssignments(STORAGE_KEY2("projectFolders"));
    const collapsed = loadCollapsedFolders(STORAGE_KEY2("projectFolderCollapsed"));
    const groups = groupProjectsByFolder(state.projects, assignments);
    const hasFolders = groups.some((g2) => g2.folder !== null);
    groups.forEach((group) => {
      if (hasFolders) {
        const isCollapsed = collapsed.has(group.key);
        const header = document.createElement("div");
        header.className = "project-folder-header";
        header.dataset.folderKey = group.key;
        header.style.cssText = "display:flex;align-items:center;gap:6px;padding:4px 4px;margin-top:4px;cursor:pointer;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.04em;user-select:none;";
        const caret = document.createElement("span");
        caret.textContent = isCollapsed ? "\u25B8" : "\u25BE";
        caret.style.cssText = "flex-shrink:0;font-size:9px;width:9px;";
        const label = document.createElement("span");
        label.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        label.textContent = group.label;
        const count = document.createElement("span");
        count.style.cssText = "flex-shrink:0;opacity:0.7;";
        count.textContent = String(group.projects.length);
        header.appendChild(caret);
        header.appendChild(label);
        header.appendChild(count);
        header.onclick = () => {
          toggleFolderCollapsed(collapsed, group.key, STORAGE_KEY2("projectFolderCollapsed"));
          loadProjects();
        };
        list.appendChild(header);
        if (isCollapsed) return;
      }
      flattenHierarchy(group.projects).forEach((row) => list.appendChild(_makeProjectItem(row.project, row.depth)));
    });
    const switcher = document.getElementById("project-switcher");
    if (switcher) {
      switcher.style.display = "none";
      const previous = switcher.value;
      switcher.innerHTML = "";
      state.projects.forEach((p3) => {
        const opt = document.createElement("option");
        opt.value = p3.id;
        opt.textContent = p3.name;
        switcher.appendChild(opt);
      });
      if (previous && state.projects.some((p3) => p3.id === previous)) switcher.value = previous;
    }
    syncSidebarActiveProject();
  }
  function _makeProjectItem(p3, depth = 0) {
    const div = document.createElement("div");
    const isSub = depth > 0;
    div.className = "project-item" + (state.activeTab === p3.id ? " active" : "") + (isSub ? " project-subitem" : "");
    div.dataset.projectId = p3.id;
    div.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:4px;" + (isSub ? `padding-left:${14 + depth * 12}px;` : "");
    const nameSpan = document.createElement("span");
    nameSpan.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    nameSpan.textContent = (isSub ? "\u2514 " : "") + p3.name;
    const menuBtn = document.createElement("button");
    menuBtn.textContent = "\u22EF";
    menuBtn.title = "Project actions";
    menuBtn.style.cssText = "background:none;border:none;color:var(--muted);cursor:pointer;padding:0 4px;font-size:14px;line-height:1;flex-shrink:0";
    menuBtn.onmouseenter = () => menuBtn.style.color = "var(--text)";
    menuBtn.onmouseleave = () => menuBtn.style.color = "var(--muted)";
    menuBtn.onclick = (e3) => {
      e3.stopPropagation();
      let t3 = state.tabs.find((tab) => tab.id === p3.id);
      if (!t3) {
        openTab(p3);
        t3 = state.tabs.find((tab) => tab.id === p3.id);
      }
      if (t3) _openTabMenu(t3, menuBtn);
    };
    div.appendChild(nameSpan);
    div.appendChild(menuBtn);
    div.onclick = (e3) => {
      if (e3.target !== menuBtn) openTab(p3);
    };
    return div;
  }
  function _moveProjectToFolder(t3) {
    const assignments = loadFolderAssignments(STORAGE_KEY2("projectFolders"));
    const current = assignments[t3.id] || "";
    const existing = knownFolderNames(state.projects, assignments);
    const hint = existing.length ? `

Existing folders: ${existing.join(", ")}` : "";
    const next = window.prompt(
      `Move "${t3.project.name}" to a folder (leave blank for ${UNGROUPED_LABEL}).${hint}`,
      current
    );
    if (next === null) return;
    const updated = assignProjectToFolder(assignments, t3.id, next);
    saveFolderAssignments(updated, STORAGE_KEY2("projectFolders"));
    loadProjects();
    const folder = (next || "").trim();
    toast(folder ? `Moved to folder "${folder}"` : `Moved to ${UNGROUPED_LABEL}`);
  }
  async function _makeSubproject(t3) {
    const candidates = eligibleParents(state.projects, t3.id);
    if (!candidates.length) {
      if (state.projects.some((p3) => p3.parent_project_id === t3.id)) {
        toast("This project has subprojects of its own \u2014 subprojects are one level deep.", true);
      } else {
        toast("No eligible parent project \u2014 add another top-level project first.", true);
      }
      return;
    }
    const lines = candidates.map((p3, i3) => `${i3 + 1}. ${p3.name}`).join("\n");
    const raw = window.prompt(
      `Make "${t3.project.name}" a subproject of which project?

${lines}

Enter a number (or leave blank to cancel):`,
      ""
    );
    if (raw === null) return;
    const idx = parseInt(raw.trim(), 10) - 1;
    if (Number.isNaN(idx) || idx < 0 || idx >= candidates.length) {
      if (raw.trim() !== "") toast("Invalid selection", true);
      return;
    }
    const parent = candidates[idx];
    try {
      await api(`/projects/${t3.id}/parent`, {
        method: "POST",
        body: JSON.stringify({ parent_project_id: parent.id })
      });
      t3.project = { ...t3.project, parent_project_id: parent.id };
      const proj = state.projects.find((p3) => p3.id === t3.id);
      if (proj) proj.parent_project_id = parent.id;
      await loadProjects();
      toast(`"${t3.project.name}" is now a subproject of "${parent.name}"`);
    } catch (e3) {
      toast("Could not set parent: " + e3.message, true);
    }
  }
  async function _detachSubproject(t3) {
    try {
      await api(`/projects/${t3.id}/parent`, {
        method: "POST",
        body: JSON.stringify({ parent_project_id: null })
      });
      t3.project = { ...t3.project, parent_project_id: null };
      const proj = state.projects.find((p3) => p3.id === t3.id);
      if (proj) proj.parent_project_id = null;
      await loadProjects();
      toast(`"${t3.project.name}" detached to top level`);
    } catch (e3) {
      toast("Could not detach: " + e3.message, true);
    }
  }
  function openTab(project) {
    const existing = state.tabs.find((t3) => t3.id === project.id);
    if (existing) {
      activateTab(project.id);
      return;
    }
    state.tabs.push({ id: project.id, project });
    saveTabs();
    renderTabs();
    buildTabBody(project);
    activateTab(project.id);
    setTimeout(() => refreshProjectCountBadges(project.id), 100);
  }
  function closeTab2(id) {
    state.tabs = state.tabs.filter((t3) => t3.id !== id);
    const panel = state.panels[id];
    if (panel) {
      try {
        panel.ws && panel.ws.close();
      } catch (e3) {
      }
      delete state.panels[id];
    }
    document.getElementById(`tab-body-${id}`)?.remove();
    saveTabs();
    renderTabs();
    if (state.activeTab === id) {
      const next = state.tabs[state.tabs.length - 1];
      state.activeTab = next ? next.id : null;
      if (next) activateTab(next.id);
      else document.getElementById("tab-bodies").innerHTML = '<div class="empty">no project open \u2014 pick one on the left</div>';
    }
    syncSidebarActiveProject();
  }
  function saveTabs() {
    try {
      localStorage.setItem(STORAGE_KEY2(TABS_KEY), JSON.stringify(state.tabs.map((t3) => t3.id)));
    } catch (e3) {
    }
  }
  var TAB_OVERFLOW_THRESHOLD = 10;
  function renderTabs() {
    const bar = document.getElementById("tabs");
    bar.innerHTML = "";
    const overflow = state.tabs.length >= TAB_OVERFLOW_THRESHOLD;
    const visible = overflow ? state.tabs.slice(0, TAB_OVERFLOW_THRESHOLD - 1) : state.tabs;
    const hidden = overflow ? state.tabs.slice(TAB_OVERFLOW_THRESHOLD - 1) : [];
    visible.forEach((t3) => bar.appendChild(_makeTabEl(t3)));
    if (overflow) {
      const more = document.createElement("div");
      more.className = "tab tab-overflow";
      more.textContent = `>> ${hidden.length} more`;
      more.title = hidden.map((t3) => t3.project.name).join(", ");
      more.onclick = (e3) => {
        e3.stopPropagation();
        let menu = document.getElementById("tab-overflow-menu");
        if (menu) {
          menu.remove();
          return;
        }
        menu = document.createElement("div");
        menu.id = "tab-overflow-menu";
        menu.style.cssText = "position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1000;min-width:160px;box-shadow:0 4px 12px rgba(0,0,0,0.4)";
        const rect = more.getBoundingClientRect();
        menu.style.top = rect.bottom + 4 + "px";
        menu.style.left = rect.left + "px";
        hidden.forEach((t3) => {
          const item = document.createElement("div");
          item.style.cssText = "padding:8px 12px;cursor:pointer;font-size:11px;font-family:var(--font-mono)";
          item.textContent = t3.project.name;
          item.onmouseenter = () => item.style.background = "var(--surface-1)";
          item.onmouseleave = () => item.style.background = "";
          item.onclick = () => {
            menu.remove();
            activateTab(t3.id);
          };
          menu.appendChild(item);
        });
        document.body.appendChild(menu);
        const close = () => {
          menu.remove();
          document.removeEventListener("click", close);
        };
        setTimeout(() => document.addEventListener("click", close), 0);
      };
      bar.appendChild(more);
    }
  }
  function _makeTabEl(t3) {
    const div = document.createElement("div");
    div.className = "tab" + (state.activeTab === t3.id ? " active" : "");
    div.dataset.tabId = t3.id;
    div.onclick = () => activateTab(t3.id);
    if (t3.project.icon) {
      const iconSpan = document.createElement("span");
      iconSpan.textContent = t3.project.icon;
      iconSpan.style.cssText = "margin-right:5px;font-size:1.05em";
      div.appendChild(iconSpan);
    }
    const nameSpan = document.createElement("span");
    nameSpan.textContent = t3.project.name;
    div.appendChild(nameSpan);
    const kebab = document.createElement("button");
    kebab.className = "tab-kebab";
    kebab.textContent = "\u22EF";
    kebab.title = "Project actions";
    kebab.onclick = (e3) => {
      e3.stopPropagation();
      _openTabMenu(t3, kebab);
    };
    div.appendChild(kebab);
    const close = document.createElement("button");
    close.className = "close";
    close.textContent = "\xD7";
    close.onclick = (e3) => {
      e3.stopPropagation();
      closeTab2(t3.id);
    };
    div.appendChild(close);
    return div;
  }
  function _openTabMenu(t3, anchor) {
    document.querySelectorAll(".tab-context-menu").forEach((m3) => m3.remove());
    const menu = document.createElement("div");
    menu.className = "tab-context-menu";
    menu.style.cssText = "position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:150px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono)";
    function menuItem(label, fn) {
      const item = document.createElement("div");
      item.style.cssText = "padding:8px 12px;cursor:pointer";
      item.textContent = label;
      item.onmouseenter = () => {
        item.style.background = "var(--surface-3)";
        item.style.color = "var(--accent)";
      };
      item.onmouseleave = () => {
        item.style.background = "";
        item.style.color = "";
      };
      item.onclick = () => {
        menu.remove();
        fn();
      };
      menu.appendChild(item);
    }
    const uuidDiv = document.createElement("div");
    uuidDiv.style.cssText = "padding:6px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);user-select:all;cursor:pointer";
    uuidDiv.title = "Click to copy project ID";
    uuidDiv.textContent = t3.id;
    uuidDiv.addEventListener("click", (e3) => {
      e3.stopPropagation();
      const _restore = () => setTimeout(() => {
        uuidDiv.textContent = t3.id;
      }, 1200);
      const _cb = navigator.clipboard;
      if (_cb && _cb.writeText) {
        _cb.writeText(t3.id).then(
          () => {
            uuidDiv.textContent = "\u2713 Copied project ID";
            _restore();
          },
          () => {
            uuidDiv.textContent = "Copy failed \u2014 select manually";
            _restore();
          }
        );
      } else {
        uuidDiv.textContent = "Copy failed \u2014 select manually";
        _restore();
      }
    });
    menu.appendChild(uuidDiv);
    menuItem("\u270F Rename", () => _renameProject(t3));
    menuItem("\u{1F3A8} Change icon\u2026", () => _setProjectIcon(t3));
    menuItem("\u{1F4C1} Move to folder\u2026", () => _moveProjectToFolder(t3));
    if (t3.project && t3.project.parent_project_id) {
      menuItem("\u2934 Detach from parent", () => _detachSubproject(t3));
    } else {
      menuItem("\u{1F517} Make subproject of\u2026", () => _makeSubproject(t3));
    }
    menuItem("\u2B07 Download DB", () => window.open("/admin/snapshot", "_blank"));
    menuItem("\u{1F5D1} Delete project\u2026", () => _deleteProject(t3));
    const rect = anchor.getBoundingClientRect();
    menu.style.top = rect.bottom + 4 + "px";
    menu.style.left = rect.left + "px";
    document.body.appendChild(menu);
    const dismiss = () => {
      menu.remove();
      document.removeEventListener("click", dismiss);
    };
    setTimeout(() => document.addEventListener("click", dismiss), 0);
  }
  async function _setProjectIcon(t3) {
    const current = t3.project.icon || "";
    const next = window.prompt(
      `Paste a single emoji to use as the project icon (or leave blank to clear).

Current: ${current || "(none)"}`,
      current
    );
    if (next === null) return;
    const icon = next.trim() ? next.trim().slice(0, 8) : null;
    try {
      const updated = await api(`/projects/${t3.id}/icon`, {
        method: "PATCH",
        body: JSON.stringify({ icon })
      });
      t3.project = { ...t3.project, icon: updated.icon || null };
      const proj = state.projects.find((p3) => p3.id === t3.id);
      if (proj) proj.icon = updated.icon || null;
      renderTabs();
      toast(icon ? `Icon set to ${icon}` : "Icon cleared");
    } catch (e3) {
      toast("Update failed: " + e3.message, true);
    }
  }
  async function _renameProject(t3) {
    const newName = window.prompt(`Rename "${t3.project.name}" to:`, t3.project.name);
    if (!newName || newName.trim() === t3.project.name) return;
    try {
      const updated = await api(`/projects/${t3.id}/rename`, {
        method: "POST",
        body: JSON.stringify({ name: newName.trim() })
      });
      t3.project = { ...t3.project, name: updated.name };
      const hdr = document.querySelector(`#drawer-status-${t3.id} .drawer-header span:first-child`);
      if (hdr) hdr.textContent = "STATUS \xB7 " + updated.name;
      renderTabs();
      toast("Renamed to " + updated.name);
    } catch (e3) {
      toast("Rename failed: " + e3.message, true);
    }
  }
  async function _deleteProject(t3) {
    await new Promise((resolve) => {
      if (document.getElementById("delete-project-modal")) return resolve();
      const overlay = document.createElement("div");
      overlay.id = "delete-project-modal";
      overlay.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center";
      const box = document.createElement("div");
      box.style.cssText = "background:var(--surface-0);border:1px solid var(--border);border-radius:8px;padding:24px 28px;width:420px;max-width:94vw;display:flex;flex-direction:column;gap:14px";
      const name = escapeHtml(t3.project.name);
      box.innerHTML = `<div style="font-weight:700;font-size:14px;color:var(--danger,#dc2626)">Delete "${name}"?</div><div style="font-size:12px;color:var(--muted)">Permanently deletes all sessions, tasks, decisions, and goal history. <strong>Cannot be undone.</strong></div><div style="display:flex;gap:8px;justify-content:flex-end"><button id="del-proj-cancel" class="secondary" style="font-size:12px">Cancel</button><button id="del-proj-confirm" style="font-size:12px;background:var(--danger,#dc2626);color:#fff;border:none;border-radius:5px;padding:6px 16px;cursor:pointer">Delete project</button></div>`;
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      box.querySelector("#del-proj-cancel").onclick = () => {
        overlay.remove();
        resolve();
      };
      overlay.onclick = (e3) => {
        if (e3.target === overlay) {
          overlay.remove();
          resolve();
        }
      };
      box.querySelector("#del-proj-confirm").onclick = async () => {
        overlay.remove();
        try {
          await api(`/projects/${t3.id}`, { method: "DELETE" });
          closeTab2(t3.id);
          state.projects = state.projects.filter((p3) => p3.id !== t3.id);
          await loadProjects();
          toast("Project deleted");
        } catch (e3) {
          if (e3.status === 404) {
            closeTab2(t3.id);
            state.projects = state.projects.filter((p3) => p3.id !== t3.id);
            await loadProjects();
            toast("Project removed");
          } else {
            toast(e3.message.includes("409") ? "Cannot delete \u2014 active tasks in progress." : "Delete failed: " + e3.message, true);
          }
        }
        resolve();
      };
    });
  }
  function activateTab(id) {
    state.activeTab = id;
    renderTabs();
    syncSidebarActiveProject();
    document.querySelectorAll(".tab-body").forEach((el2) => el2.classList.remove("active"));
    const body = document.getElementById(`tab-body-${id}`);
    if (body) body.classList.add("active");
    const empty = document.querySelector(".tab-bodies > .empty");
    if (empty) empty.remove();
    try {
      localStorage.setItem(STORAGE_KEY2(ACTIVE_PROJECT_KEY), id);
    } catch (e3) {
    }
    const switcher = document.getElementById("project-switcher");
    if (switcher) switcher.value = id;
    try {
      const _panel = getPanelState(id);
      let _activeVtab = _panel && _panel.activeVtab;
      if (!_activeVtab) {
        try {
          _activeVtab = localStorage.getItem("meridian_last_tab_" + id);
        } catch (_2) {
        }
      }
      if (_activeVtab === "settings" && typeof loadSettingsTab === "function") {
        loadSettingsTab(id, { force: true });
      }
    } catch (_2) {
    }
  }
  function buildTabBody(project) {
    const root = document.getElementById("tab-bodies");
    const empty = root.querySelector(":scope > .empty");
    if (empty) empty.remove();
    const body = document.createElement("div");
    body.className = "tab-body";
    body.id = `tab-body-${project.id}`;
    body.innerHTML = `

    <div class="vtab-strip" id="vtab-strip-${project.id}">

      <div class="vtab-group" data-vgroup="overview" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="overview" title="Overview" aria-label="Overview group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">\u{1F4CA}</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn active" data-vtab="status" title="Status &amp; Sessions" aria-label="Status and sessions">\u{1F4CA}</button>

          <button class="vtab-btn" data-vtab="live" title="Live \u2014 right-now view">\u26A1</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="planning" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="planning" title="Planning" aria-label="Planning group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">\u{1F3AF}</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn" data-vtab="goal" title="Goal State">\u{1F3AF}</button>

          <button class="vtab-btn" data-vtab="insights" title="Insights \u2014 durable strategic understanding">\u{1F4A1}</button>

          <button class="vtab-btn" data-vtab="blog" title="Blog \u2014 workspace posts (draft/published/archived)">\u270D\uFE0F</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="work" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="work" title="Work" aria-label="Work group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">\u{1F477}</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn" data-vtab="queue" title="Work Queue">\u{1F477}</button>

          <button class="vtab-btn" data-vtab="hitl" title="HITL \u2014 Human-in-the-Loop queue" style="position:relative">\u2753<span class="hitl-vtab-badge vtab-count-badge" data-pid="${project.id}" style="display:none;position:absolute;top:2px;right:2px;background:#f87171;color:#fff;font-size:8px;font-weight:700;padding:0 3px;border-radius:6px;line-height:14px;pointer-events:none">0</span></button>

          <button class="vtab-btn" data-vtab="team" title="Team \u2014 per-human activity">\u{1F465}</button>

          <button class="vtab-btn" data-vtab="sessions" title="Sessions \u2014 executor session timeline (done / failed / stopped-ambiguously)">\u{1F552}</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="content" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="content" title="Content" aria-label="Content group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">\u{1F4C1}</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          ${window.MERIDIAN_HOSTED && !(project.github_repo || project.repo) ? "" : '<button class="vtab-btn" data-vtab="files" title="Files">\u{1F4C1}</button>'}

          <button class="vtab-btn" data-vtab="notes" title="Notes \u2014 per-project wiki" style="position:relative">\u{1F4DD}<span class="notes-vtab-badge vtab-count-badge muted" data-pid="${project.id}" style="display:none;position:absolute;top:2px;right:2px;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:8px;font-weight:700;padding:0 3px;border-radius:6px;line-height:14px;pointer-events:none">0</span></button>

          <button class="vtab-btn" data-vtab="devlog" title="Dev Log">\u{1F4D3}</button>

          <button class="vtab-btn" data-vtab="documents" title="Documents \u2014 ingested docs &amp; structure">\u{1F4C4}</button>

          <button class="vtab-btn" data-vtab="docs" title="MCP Tool Reference">\u{1F4D6}</button>

          <button class="vtab-btn" data-vtab="codeintel" title="Code Intel \u2014 codebase index &amp; architecture" id="vtab-codeintel-${project.id}" style="display:none">\u{1F50D}</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="history" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="history" title="History" aria-label="History group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">\u{1F4C5}</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn" data-vtab="timeline" title="Activity Timeline">\u{1F4C5}</button>

          <button class="vtab-btn" data-vtab="rewind" title="Rewind \u2014 Last X days">\u21BB</button>

          <button class="vtab-btn" data-vtab="settings" title="Notification Settings">\u2699</button>

        </div>

      </div>

    </div>

    <div class="vtab-drawer open" id="drawer-${project.id}">

      <div class="project-fetch-alert" id="project-fetch-alert-${project.id}"></div>

      <div class="drawer-panel active" id="drawer-status-${project.id}">

        <div class="drawer-header">

          <span>STATUS \xB7 ${escapeHtml(project.name)}</span>

          <span class="ws-dot" id="ws-${project.id}"></span>

        </div>

        <div style="flex:1;overflow-y:auto">

          <div class="section">

            <h3>Active Sessions</h3>

            <div class="sessions-list" id="sessions-${project.id}"></div>

          </div>

          <div class="hitl-banner" id="hitl-banner-${project.id}" style="display:none">HITL queue</div>

          <div id="hitl-queue-${project.id}"></div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-live-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span>LIVE \xB7 ${escapeHtml(project.name)}</span>

          <span style="display:flex;gap:6px;align-items:center">

            <button class="secondary" id="live-auto-btn-${project.id}" title="Toggle auto-refresh" style="padding:2px 8px;font-size:10px">\u21BB Auto</button>

            <button class="secondary" id="live-pause-${project.id}" title="Pause queue (UI stub)" style="padding:2px 8px;font-size:10px">Pause</button>

            <button class="secondary" id="live-run-${project.id}" title="Run all pending (UI stub)" style="padding:2px 8px;font-size:10px">Run All</button>

          </span>

        </div>

        <div class="live-body" id="live-body-${project.id}">

          <div class="live-section">

            <div class="live-section-label">Sprint progress</div>

            <div id="live-sprint-progress-${project.id}" class="live-sprint-progress"></div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <div class="live-section-label">In progress \xB7 by session</div>

            <div id="live-inprogress-by-session-${project.id}" class="live-inprogress-by-session">

              <div class="live-empty">Nothing in progress right now.</div>

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <div class="live-section-label" title="Conflict-free batches the orchestrator can fan out \u2014 successive waves run in sequence (mirrors get_parallelizable_groups)">Parallel waves</div>

            <div id="live-wave-progress-${project.id}" class="live-wave-progress">

              <div class="live-empty">No parallelizable work right now.</div>

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <details class="live-section-collapse" open>

              <summary class="live-section-label" style="cursor:pointer;list-style:none">Active sessions</summary>

              <div class="live-sessions" id="live-sessions-${project.id}">

                <div class="live-empty">No active sessions.</div>

              </div>

            </details>

          </div>

          <hr class="live-divider">

          <div class="live-section" id="sprint-notes-section-${project.id}" style="display:none">

            <div class="live-section-label">Sprint notes (session scratch pad)</div>

            <div id="sprint-notes-${project.id}" style="font-size:11px"></div>

          </div>

          <hr class="live-divider" id="sprint-notes-divider-${project.id}" style="display:none">

          <div class="live-section">

            <div class="live-section-label" style="display:flex;justify-content:space-between;align-items:center">

              <span>Queue</span>

              <button class="secondary" id="new-sprint-btn-${project.id}" style="padding:1px 8px;font-size:9px" title="Start a new sprint">+ New Sprint</button>

            </div>

            <div class="live-queue" id="live-queue-${project.id}">

              <div class="live-empty">Queue is empty. Add a task above.</div>

            </div>

            <div class="live-add-row">

              <input type="text" class="live-add-input" id="live-add-input-${project.id}" placeholder="+ Add task\u2026 (Enter to submit)">

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <div class="live-section-label" style="display:flex;justify-content:space-between;align-items:center">

              <span>Add to run</span>

              <button class="secondary" id="add-to-run-toggle-${project.id}" style="padding:1px 8px;font-size:9px">+ Expand</button>

            </div>

            <div id="add-to-run-area-${project.id}" style="display:none;margin-top:6px">

              <textarea id="add-to-run-text-${project.id}" rows="3" placeholder="Describe what to add to the active session's goal\u2026"

                style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;resize:vertical;outline:none"></textarea>

              <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:4px">

                <button class="secondary" id="add-to-run-cancel-${project.id}" style="padding:2px 8px;font-size:10px">Cancel</button>

                <button class="primary" id="add-to-run-submit-${project.id}" data-project="${escapeHtml(project.id)}" style="padding:2px 10px;font-size:10px">\u2192 Send to run</button>

              </div>

            </div>

          </div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-goal-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>GOAL \xB7 ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Share your project context with AI sessions \u2014 north star, sprint, version goal</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center">

            <span class="goal-version" id="goal-version-${project.id}"></span>

          </span>

        </div>

        <div class="goal-subtab-strip">

          <button class="goal-subtab-btn active" data-gtab="north-star" title="Permanent product vision. Rarely changes \u2014 set once, then keep stable.">\u{1F52D} North Star</button>

          <button class="goal-subtab-btn" data-gtab="version-goal" title="Current milestone \u2014 what ships this cycle (v1.2, v2.0, etc).">\u{1F3AF} Version Goal</button>

          <button class="goal-subtab-btn" data-gtab="sprint" title="What this session is focused on right now \u2014 updated multiple times per day. Not a multi-week scrum sprint.">\u26A1 Session Focus</button>

          <button class="goal-subtab-btn" data-gtab="decisions" title="Pinned constitution + append-only decisions log.">\u{1F4CB} Decisions <span class="decisions-gtab-badge vtab-count-badge muted" data-pid="${project.id}" style="display:none;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:9px;font-weight:700;padding:0 5px;border-radius:8px;line-height:14px;margin-left:4px;vertical-align:1px">0</span></button>

        </div>

        <div class="goal-subtab-body">

          <div class="goal-subtab-panel active" id="gtab-north-star-${project.id}">

            <div style="color:var(--muted);font-size:10px;margin-bottom:6px">Permanent vision. Set once, change rarely or never.</div>

            <textarea class="goal-area goal-full mono" id="goal-north-star-${project.id}" placeholder="(north star not set \u2014 set once, rarely change)" style="overflow-y:hidden;min-height:0"></textarea>

            <div class="goal-actions">

              <button class="primary" id="save-north-star-${project.id}">save north star</button>

              <span class="goal-ts" id="goal-ns-ts-${project.id}"></span>

              <span id="goal-ns-lock-${project.id}" style="opacity:0.5;font-size:11px"></span>

            </div>

          </div>

          <div class="goal-subtab-panel" id="gtab-version-goal-${project.id}">

            <div style="color:var(--muted);font-size:10px;margin-bottom:6px">Current milestone \u2014 what ships this cycle (v1.2, v2.0, etc).</div>

            <div id="goal-title-${project.id}" style="font-family:var(--font-mono);font-size:11px;font-weight:600;color:var(--accent);padding:5px 8px;background:var(--surface-2);border:1px solid var(--border);border-radius:4px 4px 0 0;border-bottom:none;user-select:none;flex-shrink:0;white-space:pre-wrap;overflow:visible" title="Version title (read-only)"></div>

            <div id="goal-shipped-${project.id}" style="display:none;font-family:var(--font-mono);font-size:10px;color:var(--muted);padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);border-top:none;border-bottom:none;white-space:pre-wrap;user-select:none;flex-shrink:0" title="SHIPPED section (read-only \u2014 updated by Claude Code)"></div>

            <textarea class="goal-area goal-full mono" id="goal-${project.id}" placeholder="CURRENT FOCUS:" style="border-radius:0 0 4px 4px;font-size:13px"></textarea>

            <div class="goal-actions" style="flex-shrink:0">

              <button class="primary" id="save-goal-${project.id}">save version goal</button>

              <span class="goal-version" id="goal-state-${project.id}"></span>

              <span class="goal-ts" id="goal-vg-ts-${project.id}"></span>

            </div>

            <div id="goal-autoblocks-wrapper-${project.id}" style="display:none;flex-shrink:0">

              <button onclick="(function(b,c){var open=c.style.display!=='none';c.style.display=open?'none':'block';b.textContent=open?'\u{1F4CB} Session Log \u25B6':'\u{1F4CB} Session Log \u25BC';})(this,document.getElementById('goal-autoblocks-${project.id}'))" style="background:none;border:none;color:var(--muted);font-size:10px;font-weight:600;cursor:pointer;padding:2px 0;font-family:var(--font-mono);margin-top:6px">\u{1F4CB} Session Log \u25B6</button>

              <div id="goal-autoblocks-${project.id}" style="display:none;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:8px;font-family:var(--font-mono);font-size:13px;color:var(--text);white-space:pre-wrap;word-break:break-word;margin-top:4px"></div>

            </div>

          </div>

          <div class="goal-subtab-panel" id="gtab-sprint-${project.id}">

            <div style="color:var(--muted);font-size:10px;margin-bottom:4px">What this session is doing right now. Updated frequently \u2014 not a multi-week scrum sprint.</div>

            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;align-items:center">

              <select id="goal-sprint-select-${project.id}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--muted);font-family:var(--font-mono);font-size:11px;outline:none"><option value="" disabled selected>loading sessions\u2026</option></select>

              <input type="text" id="goal-sprint-${project.id}" placeholder="v1.0.x \u2014 description" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--muted);font-family:var(--font-mono);font-size:11px;outline:none;display:none">

              <button class="secondary" id="save-sprint-${project.id}" style="white-space:nowrap">save</button>

              <span class="goal-ts" id="goal-sp-ts-${project.id}" style="font-size:10px;color:var(--muted)"></span>

            </div>

            <div id="sprint-board-goal-${project.id}"></div>

          </div>

          <div class="goal-subtab-panel" id="gtab-decisions-${project.id}">

            <div style="margin-bottom:14px">

              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">

                <div style="color:var(--accent);font-weight:600;font-size:12px">\u{1F4CC} Pinned (Constitution)</div>

                <div style="display:flex;gap:6px">

                  <button class="secondary" id="consolidate-decisions-${project.id}" style="padding:3px 10px;font-size:10px" title="Use AI to deduplicate and merge decisions">\u2728 Consolidate</button>

                  <button class="secondary" id="add-pinned-decision-${project.id}" style="padding:3px 10px;font-size:10px">+ Pin</button>

                </div>

              </div>

              <div id="add-decision-form-${project.id}" style="display:none;margin-bottom:10px;padding:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:6px">
                <div style="display:grid;gap:6px">
                  <input id="dec-form-title-${project.id}" type="text" placeholder="Title" style="background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;width:100%;box-sizing:border-box">
                  <textarea id="dec-form-body-${project.id}" rows="2" placeholder="Body" style="background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;width:100%;box-sizing:border-box;resize:vertical"></textarea>
                  <select id="dec-form-cat-${project.id}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px">
                    <option value="TECHNICAL">TECHNICAL</option>
                    <option value="ARCHITECTURAL">ARCHITECTURAL</option>
                    <option value="PRODUCT">PRODUCT</option>
                    <option value="TACTICAL">TACTICAL</option>
                    <option value="STRATEGIC">STRATEGIC</option>
                    <option value="COMPETITIVE">COMPETITIVE</option>
                    <option value="BUSINESS">BUSINESS</option>
                  </select>
                  <div style="display:flex;gap:6px;align-items:center">
                    <button id="dec-form-add-${project.id}" class="primary" style="font-size:10px;padding:3px 10px">Add</button>
                    <button id="dec-form-cancel-${project.id}" class="secondary" style="font-size:10px;padding:3px 10px">Cancel</button>
                    <span id="dec-form-status-${project.id}" style="font-size:10px;color:var(--muted)"></span>
                  </div>
                </div>
              </div>

              <div style="color:var(--muted);font-size:10px;margin-bottom:8px">Editable current truth. Use <code>pin_decision</code> MCP tool or <code>update_decision</code> with new_title+new_body to supersede.</div>

              <div id="constitution-warning-${project.id}" style="margin-bottom:8px"></div>

              <div id="pinned-decisions-${project.id}" style="font-family:var(--font-mono);font-size:12px"></div>

            </div>

            <details open style="margin-top:14px">

              <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:12px;padding:4px 0">\u{1F4CB} History (Append-only log)</summary>

              <div style="color:var(--muted);font-size:10px;margin:8px 0">Append-only via <code>set_decision</code>. Captures every micro-decision; the constitution above is the live truth.</div>

              <div id="decisions-table-${project.id}" style="font-family:var(--font-mono);font-size:12px"></div>

            </details>

          </div>

        </div>

        <div style="flex-shrink:0;padding:8px 14px;border-top:1px solid var(--border)">

          <a class="secondary" style="display:inline-block;padding:5px 12px;border:1px solid var(--border);border-radius:4px;color:var(--muted);font-size:10px;text-decoration:none;font-family:'IBM Plex Mono',monospace;cursor:pointer" href="/projects/${project.id}/export/pdf" download>\u2B07 Export IP Record (PDF)</a>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-files-${project.id}">

        <div class="drawer-header">FILES \xB7 ${escapeHtml(project.name)}</div>

        <div id="files-browse-${project.id}" style="flex:1;overflow-y:auto">

          <div class="file-list" id="files-list-${project.id}"></div>

        </div>

        <div id="file-editor-wrap-${project.id}" style="display:none;flex:1;flex-direction:column;overflow:hidden">

          <div class="drawer-header" style="flex-shrink:0">

            <button class="secondary" id="file-back-${project.id}" style="padding:2px 8px;font-size:10px">\u2190 back</button>

            <span id="file-name-${project.id}" style="flex:1;color:var(--accent);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>

            <button class="primary" id="file-save-${project.id}" style="padding:2px 8px;font-size:10px">save</button>

          </div>

          <div class="preview-toggle-row" style="padding:6px 14px 4px;flex-shrink:0" id="file-toggle-row-${project.id}">

            <button class="preview-btn active" data-fmode="edit" id="file-mode-edit-${project.id}">edit</button>

            <button class="preview-btn" data-fmode="preview" id="file-mode-preview-${project.id}">preview</button>

          </div>

          <textarea id="file-content-${project.id}" style="flex:1;background:var(--surface-2);border:none;border-top:1px solid var(--border);color:var(--text);padding:10px 14px;font-family:'IBM Plex Mono',monospace;font-size:12px;resize:none;outline:none;overflow-y:auto"></textarea>

          <div id="file-preview-${project.id}" class="goal-preview" style="display:none;flex:1;margin:10px 14px;"></div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-devlog-${project.id}">

        <div class="drawer-header">DEV LOG \xB7 ${escapeHtml(project.name)}</div>

        <div style="padding:8px 10px;border-bottom:1px solid var(--border);background:var(--surface-2)">
          <textarea id="devlog-append-text-${project.id}" rows="2" placeholder="Add a note to DEVLOG.md\u2026" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;resize:vertical;box-sizing:border-box;outline:none"></textarea>
          <div style="display:flex;gap:6px;align-items:center;margin-top:4px">
            <button id="devlog-append-btn-${project.id}" class="primary" style="font-size:10px;padding:3px 10px">Append</button>
            <span id="devlog-append-status-${project.id}" style="font-size:10px;color:var(--muted)"></span>
          </div>
        </div>

        <div style="padding:6px 10px;border-bottom:1px solid var(--border)">
          <input type="text" id="devlog-search-${project.id}" placeholder="Search dev log (description, session)\u2026" style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;outline:none">
        </div>

        <div class="scroll-area"><div class="task-list" id="tasks-${project.id}"></div></div>

      </div>

      <div class="drawer-panel" id="drawer-timeline-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span>TIMELINE \xB7 ${escapeHtml(project.name)}</span>

          <span style="display:flex;gap:6px;align-items:center">

            <button class="secondary" id="timeline-axis-${project.id}" title="Toggle relative/absolute time" style="padding:2px 8px;font-size:10px">relative</button>

            <button class="secondary" id="timeline-refresh-${project.id}" title="Refresh" style="padding:2px 8px;font-size:10px">refresh</button>

          </span>

        </div>

        <div style="padding:4px 14px 4px;font-size:10px;color:var(--muted);border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;gap:10px;flex-wrap:wrap">

          <span>Session activity across time \u2014 each row is one AI session, each bar is one task.</span>

          <span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">

            <span><span style="color:#34d399">\u25A0</span> done</span>

            <span><span style="color:#f87171">\u25A0</span> pending/failed</span>

            <span><span style="color:#6c8fff">\u25A0</span> sprint</span>

            <span><span style="color:#fbbf24">\u25A0</span> north star</span>

            <span><span style="color:#a78bfa">\u25A0</span> goal</span>

          </span>

        </div>

        <div style="padding:5px 14px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--border);flex-shrink:0">

          <label style="font-size:10px;color:var(--muted)">From</label>

          <input type="date" id="timeline-from-${project.id}" style="font-size:10px;background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:1px 4px;outline:none">

          <label style="font-size:10px;color:var(--muted)">To</label>

          <input type="date" id="timeline-to-${project.id}" style="font-size:10px;background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:1px 4px;outline:none">

          <button class="secondary" id="timeline-r7d-${project.id}" style="padding:1px 7px;font-size:10px">7d</button>

          <button class="secondary" id="timeline-r30d-${project.id}" style="padding:1px 7px;font-size:10px">30d</button>

          <button class="secondary" id="timeline-rall-${project.id}" style="padding:1px 7px;font-size:10px">All</button>

          <span id="timeline-range-err-${project.id}" style="color:#f87171;font-size:10px;display:none"></span>

        </div>

        <div class="timeline-wrap" id="timeline-wrap-${project.id}" style="flex:1;overflow:auto;padding:14px"></div>

      </div>

      <div class="drawer-panel" id="drawer-rewind-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span>REWIND \xB7 ${escapeHtml(project.name)}</span>

          <span style="display:flex;gap:6px;align-items:center">

            <button class="secondary rewind-day-btn" data-days="7" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">7d</button>

            <button class="secondary rewind-day-btn" data-days="14" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">14d</button>

            <button class="secondary rewind-day-btn" data-days="30" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">30d</button>

            <button class="secondary rewind-day-btn" data-days="90" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">90d</button>

            <button class="secondary rewind-day-btn" data-days="365" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">1y</button>

            <button class="secondary rewind-day-btn" data-days="3650" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">All</button>

          </span>

        </div>

        <div style="flex-shrink:0;padding:6px 14px;border-bottom:1px solid var(--border)">

          <input type="text" id="rewind-search-${project.id}" placeholder="Search tasks, notes, decisions\u2026"

            style="width:100%;padding:5px 10px;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;font-family:var(--font-mono);font-size:11px;color:var(--text);outline:none;box-sizing:border-box">

        </div>

        <div class="rewind-wrap" id="rewind-wrap-${project.id}" style="flex:1;overflow:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px">

          <div class="empty" style="color:var(--muted)">pick a window above</div>

        </div>

        <div style="flex-shrink:0;padding:8px 14px;border-top:1px solid var(--border);display:flex;gap:8px">

          <button class="secondary" id="rewind-share-${project.id}" style="padding:4px 10px;font-size:10px">Copy shareable link</button>

          <a class="secondary" href="/projects/${project.id}/export/pdf" download

             style="padding:4px 10px;font-size:10px;border:1px solid var(--border);border-radius:4px;color:var(--muted);text-decoration:none;font-family:'IBM Plex Mono',monospace">Export as PDF</a>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-queue-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>QUEUE \xB7 ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Work items \u2014 claimed atomically so parallel sessions never collide</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center">
            <button class="secondary" id="queue-reconcile-${project.id}" style="padding:2px 8px;font-size:10px" title="Check if any pending items may already be done based on recent commits">reconcile</button>
            <button class="secondary" id="queue-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>
          </span>

        </div>

        <div id="live-session-${project.id}" style="display:none;flex-shrink:0;border-bottom:1px solid var(--border);background:var(--surface-2);padding:8px 14px 10px"></div>

        <div id="reconcile-results-${project.id}" style="display:none;flex-shrink:0;border-bottom:1px solid var(--border);background:var(--surface-2);padding:8px 14px 10px;font-family:var(--font-mono);font-size:11px"></div>

        <div style="padding:8px 14px 0;flex-shrink:0">

          <input type="text" id="task-search-${project.id}" placeholder="Search tasks\u2026"

            style="width:100%;padding:5px 10px;background:var(--surface-2);border:1px solid var(--border);

            color:var(--text);border-radius:4px;font-family:var(--font-mono);font-size:11px;outline:none">

        </div>

        <div class="queue-scroll" style="flex:1;min-height:0;overflow-y:auto" id="queue-scroll-${project.id}">

          <div id="queue-body-${project.id}">

          <div class="empty" style="color:var(--muted)">select queue to load</div>

        </div>

        <div id="recent-sessions-${project.id}" style="display:none;border-top:1px solid var(--border);background:var(--surface-2);padding:8px 14px 8px"></div>

        <div id="recent-runs-${project.id}" style="border-top:1px solid var(--border);background:var(--surface-2)">

          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 14px;cursor:pointer"

               id="recent-runs-toggle-${project.id}">

            <span style="font-family:var(--font-mono);font-size:10px;color:var(--muted);letter-spacing:0.05em">RECENT RUNS</span>

            <span id="recent-runs-chevron-${project.id}" style="font-size:10px;color:var(--muted)">\u25B2</span>

          </div>

          <div id="recent-runs-body-${project.id}" style="padding:0 14px 8px;font-family:var(--font-mono);font-size:11px">

            <div style="color:var(--muted)">loading\u2026</div>

          </div>

        </div>

      </div>

      </div>

      <div class="drawer-panel" id="drawer-notes-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>NOTES \xB7 ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Persistent notes readable by your team and AI sessions</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end">

            <input type="text" id="notes-search-${project.id}" placeholder="search notes\u2026" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;width:120px">

            <select id="notes-kindsel-${project.id}" title="Filter by kind" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px"><option value="">all kinds</option><option value="wiki">wiki</option><option value="insight">insight</option><option value="reference">reference</option></select>

            <select id="notes-tagsel-${project.id}" title="Filter by tag" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;max-width:130px"><option value="">all tags</option></select>

            <!-- 42e9f7b5 \u2014 the "summaries" toggle is replaced by the Notes/Log/Archive tab bar below -->
            <input type="checkbox" id="notes-show-auto-${project.id}" style="display:none">
            <span data-notes-tab-active="notes" id="notes-active-tab-${project.id}" style="display:none">notes</span>

          </span>

        </div>

        <div class="notes-tabbar" style="display:flex;gap:6px;padding:6px 14px 0;border-bottom:1px solid var(--border)">
          <button data-notes-tab="notes" id="notes-tab-notes-${project.id}" class="notes-tab-btn" style="background:none;border:none;border-bottom:2px solid var(--accent);color:var(--text);font-size:10px;font-weight:600;padding:3px 6px;cursor:pointer">Notes</button>
          <button data-notes-tab="log" id="notes-tab-log-${project.id}" class="notes-tab-btn" style="background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);font-size:10px;font-weight:600;padding:3px 6px;cursor:pointer">Log</button>
          <button data-notes-tab="archive" id="notes-tab-archive-${project.id}" class="notes-tab-btn" style="background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);font-size:10px;font-weight:600;padding:3px 6px;cursor:pointer">Archive</button>
        </div>

        <div style="flex:1;overflow-y:auto;overflow-x:hidden;word-break:break-word;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:12px" id="notes-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading notes\u2026</div>

        </div>

        <div style="flex-shrink:0;padding:10px 14px;border-top:1px solid var(--border);background:var(--surface-2)">

          <div style="display:flex;gap:6px;margin-bottom:6px">

            <input type="text" id="notes-add-title-${project.id}" placeholder="Title" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none">

            <input type="text" id="notes-add-tags-${project.id}" placeholder="tags (comma-sep)" style="width:140px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none">

            <select id="notes-add-kind-${project.id}" title="Note kind" style="width:100px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none"><option value="wiki">wiki</option><option value="insight">insight</option><option value="reference">reference</option></select>

          </div>

          <textarea id="notes-add-body-${project.id}" placeholder="Body (markdown ok)" rows="3" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:6px 8px;outline:none;resize:vertical"></textarea>

          <div style="display:flex;justify-content:flex-end;margin-top:6px">

            <button class="primary" id="notes-add-btn-${project.id}" style="padding:4px 12px;font-size:11px">+ Add note</button>

          </div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-hitl-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>YOUR TURN \xB7 ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Blocking questions from your AI agents that need a human decision</span>

          </span>

          <div style="display:flex;gap:6px;align-items:center">

            <input type="text" id="hitl-search-${project.id}" placeholder="search\u2026" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;outline:none;width:110px">

            <select id="hitl-status-filter-${project.id}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px">

              <option value="pending">pending</option>

              <option value="all">all</option>

              <option value="answered">answered</option>

              <option value="dismissed">dismissed</option>

            </select>

            <button class="secondary" id="hitl-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>

          </div>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:12px" id="hitl-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading HITL queue\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-docs-${project.id}">

        <div class="drawer-header">

          <span>MCP TOOL REFERENCE</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px" id="docs-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading tools\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-settings-${project.id}">

        <div class="drawer-header">

          <span>SETTINGS \xB7 ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="settings-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-team-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>TEAM \xB7 ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Manage project members and access</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center">

            <input type="text" id="team-search-${project.id}" placeholder="search\u2026" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;outline:none;width:100px">

            <select id="team-days-${project.id}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 4px">

              <option value="1">last 1d</option>

              <option value="7">last 7d</option>

              <option value="14" selected>last 14d</option>

              <option value="30">last 30d</option>

            </select>

            <button class="secondary" id="team-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>

          </span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px" id="team-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading team summary\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-codeintel-${project.id}">

        <div class="drawer-header">

          <span>CODE INTEL \xB7 ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="codeintel-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-documents-${project.id}">

        <div class="drawer-header">

          <span>DOCUMENTS \xB7 ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="documents-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-insights-${project.id}">

        <div class="drawer-header">

          <span>INSIGHTS \xB7 ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="insights-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-sessions-${project.id}">

        <div class="drawer-header">

          <span>SESSIONS \xB7 ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="sessions-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading\u2026</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-blog-${project.id}">

        <div class="drawer-header">

          <span>BLOG \xB7 workspace</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="blog-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading\u2026</div>

        </div>

      </div>

    </div>

    <section class="claude-handoff-panel">

      <div class="panel-header">

        <span>CLAUDE</span>

        <span class="server-version-pill" id="server-version"></span>

      </div>

      <div class="claude-launch-body">

        <div class="claude-section" data-section="start">

          <div class="claude-section-label">Start a new session</div>

          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

            <button class="primary claude-section-btn" id="copy-start-code-${project.id}" title="Copies start_session() command for Claude Code">Claude Code \u2B21</button>

            <button class="secondary claude-section-btn" id="copy-start-chat-${project.id}" title="Copies context for Claude or Codex">Open in Claude / Codex</button>

            <button class="secondary claude-section-btn" id="btn-setup-hooks-${project.id}" title="Auto-wire SessionStart + Stop hooks for your AI tools" style="font-size:10px">\u26A1 Setup Hooks</button>

          </div>

          <p class="claude-hint">Claude Code: pastes <code>start_session()</code> command. Open in Claude / Codex: pastes handoff context. Hooks: opens setup instructions.</p>

        </div>

        <hr class="claude-divider">

        <div class="claude-section" data-section="continue">

          <div class="claude-section-label">Resume Claude Code session (<code>start_session</code> + <code>get_context_block</code>)</div>

          <select class="claude-session-select" id="continue-session-${project.id}">

            <option value="">(no sessions yet)</option>

          </select>

          <button class="primary claude-section-btn" id="copy-resume-${project.id}" title="MCP flow: start_session() + get_context_block()">Copy resume MCP commands</button>

          <p class="claude-hint">Uses <code>start_session()</code> to reopen the session and <code>get_context_block()</code> to reload the working context.</p>

        </div>

        <hr class="claude-divider">

        <div class="claude-section" data-section="worker">

          <div class="claude-section-label">Start Claude Code worker (<code>start_worker_session</code> + claim)</div>

          <button class="primary claude-section-btn" id="start-worker-${project.id}" title="MCP: start_worker_session() claims the next task and returns worker context">Claim &amp; start worker</button>

          <div class="claude-worker-result" id="worker-result-${project.id}" style="display:none">

            <pre class="claude-worker-xml" id="worker-xml-${project.id}"></pre>

            <button class="secondary claude-section-btn" id="copy-worker-${project.id}" title="Copy the worker_context returned by start_worker_session()">Copy worker context</button>

            <p class="claude-hint">Uses <code>start_worker_session()</code> to claim the next task and produce a worker-ready context block.</p>

          </div>

          <div class="claude-worker-empty" id="worker-empty-${project.id}" style="display:none">

            <p class="claude-hint">No pending tasks \u2014 add one to the queue first.</p>

          </div>

        </div>

        <hr class="claude-divider">

        <div class="claude-section" data-section="handoff">

          <div class="claude-section-label">Claude Code handoff (<code>generate_handoff</code>)</div>

          <label style="display:flex;align-items:center;gap:6px;font-size:10px;color:var(--text);font-family:var(--font-mono);cursor:pointer">
            <input type="checkbox" id="sequential-mode-${project.id}" style="cursor:pointer">
            <span>Sequential mode</span>
          </label>
          <p class="claude-hint" id="touches-files-warning-${project.id}" style="display:none;color:#f59e0b">\u26A0 touches_files overlap detected in active sprint items. Coordinate or serialize before handing this off.</p>

          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

            <button class="primary claude-section-btn" id="copy-handoff-${project.id}" title="Fetch generate_handoff() output and copy raw plain text">Copy handoff (plain text)</button>

            <button class="secondary claude-section-btn" id="regen-handoff-${project.id}" title="Regenerate the on-disk handoff markdown via generate_handoff()">Regenerate</button>

            <span class="claude-handoff-ts" id="handoff-ts-${project.id}" style="font-size:10px;color:var(--muted)"></span>

          </div>

          <div id="handoff-raw-${project.id}" style="display:none;margin-top:8px">

            <textarea id="handoff-raw-text-${project.id}" readonly style="width:100%;height:220px;font-family:var(--font-mono);font-size:10px;background:#0d1117;color:#e6edf3;border:1px solid var(--border);padding:8px;border-radius:4px;resize:vertical;outline:none"></textarea>

            <div style="display:flex;gap:6px;margin-top:4px;align-items:center">

              <button class="secondary" id="handoff-copy-text-${project.id}" style="font-size:10px;padding:3px 10px">Copy text</button>

              <button class="secondary" id="handoff-close-raw-${project.id}" style="font-size:10px;padding:3px 10px">Close</button>

            </div>

          </div>

          <p class="claude-hint">Fetches raw plain-text handoff for a fresh Claude Code session. Select all or use Copy text.</p>

        </div>

        <hr class="claude-divider">

        <div class="claude-section claude-section-narrow" data-section="open">

          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">

            <a class="claude-cta-secondary-btn" id="open-in-claude-${project.id}"

               href="https://claude.ai/new" target="_blank" rel="noopener"

               title="Open in Claude">New Chat \u2192</a>

            <button class="secondary claude-section-btn" id="copy-context-${project.id}" style="font-size:11px">Copy chat context</button>

          </div>

          <p class="claude-hint">Open a new Claude.ai chat, paste the context to get up to speed</p>

        </div>

      </div>

      </div>

    </section>

  `;
    root.appendChild(body);
    state.panels[project.id] = {
      ws: null,
      taskCache: [],
      goalRaw: null,
      goalIsJson: false,
      activeVtab: "status",
      loadErrors: {}
    };
    const vtabStrip = document.getElementById(`vtab-strip-${project.id}`);
    const drawer = document.getElementById(`drawer-${project.id}`);
    if (vtabStrip && drawer) {
      const { revealGroupForTab } = wireVtabGroups(vtabStrip);
      vtabStrip.querySelectorAll(".vtab-btn").forEach((btn) => {
        btn.onclick = () => {
          const vtab = btn.dataset.vtab;
          const p3 = state.panels[project.id];
          revealGroupForTab(vtab);
          vtabStrip.querySelectorAll(".vtab-btn").forEach((b2) => {
            b2.classList.toggle("active", b2.dataset.vtab === vtab);
          });
          drawer.querySelectorAll(".drawer-panel").forEach((dp) => {
            dp.classList.toggle("active", dp.id === `drawer-${vtab}-${project.id}`);
          });
          p3.activeVtab = vtab;
          try {
            localStorage.setItem("meridian_last_tab_" + project.id, vtab);
          } catch (_2) {
          }
          if (vtab === "files") loadFilesTab(project.id);
          if (vtab === "devlog") refreshTasks(project.id);
          if (vtab === "timeline") loadTimeline2(project.id);
          if (vtab === "rewind") initRewindTab(project.id);
          if (vtab === "queue") {
            loadQueue(project.id);
            updateLiveFeed(project.id);
            loadRecentRuns(project.id);
            clearInterval(p3._liveFeedInterval);
            p3._liveFeedInterval = setInterval(() => {
              if (p3.activeVtab === "queue") updateLiveFeed(project.id);
              else clearInterval(p3._liveFeedInterval);
            }, 5e3);
          } else {
            clearInterval(p3._liveFeedInterval);
          }
          if (vtab === "live") loadLiveTab(project.id);
          if (vtab === "team") loadTeamTab(project.id);
          if (vtab === "notes") loadNotesTab(project.id);
          if (vtab === "hitl") loadHitlTab(project.id);
          if (vtab === "docs") loadDocsTab(project.id);
          if (vtab === "settings") loadSettingsTab(project.id);
          if (vtab === "codeintel") loadCodeIntelTab(project.id);
          if (vtab === "documents") loadDocumentsTab(project.id);
          if (vtab === "insights") loadInsightsTab(project.id);
          if (vtab === "blog") loadBlogTab(project.id);
          if (vtab === "sessions") loadSessionTimelineTab(project.id);
        };
      });
      try {
        const saved = localStorage.getItem("meridian_last_tab_" + project.id);
        if (saved) {
          revealGroupForTab(saved);
          const savedBtn = vtabStrip.querySelector('.vtab-btn[data-vtab="' + saved + '"]');
          if (savedBtn) savedBtn.click();
        }
      } catch (_2) {
      }
      _initCodeIntelTabVisibility(project.id);
    }
    const goalDrawer = document.getElementById(`drawer-goal-${project.id}`);
    if (goalDrawer) {
      goalDrawer.querySelectorAll(".goal-subtab-btn").forEach((btn) => {
        btn.onclick = () => {
          goalDrawer.querySelectorAll(".goal-subtab-btn").forEach((b2) => b2.classList.toggle("active", b2 === btn));
          const gtab = btn.dataset.gtab;
          goalDrawer.querySelectorAll(".goal-subtab-panel").forEach((p3) => {
            p3.classList.toggle("active", p3.id === `gtab-${gtab}-${project.id}`);
          });
          if (gtab === "decisions") loadPinnedDecisions(project.id);
        };
      });
    }
    const addPinBtn = document.getElementById(`add-pinned-decision-${project.id}`);
    const decForm = document.getElementById(`add-decision-form-${project.id}`);
    const decFormTitle = document.getElementById(`dec-form-title-${project.id}`);
    const decFormBody = document.getElementById(`dec-form-body-${project.id}`);
    const decFormCat = document.getElementById(`dec-form-cat-${project.id}`);
    const decFormAdd = document.getElementById(`dec-form-add-${project.id}`);
    const decFormCancel = document.getElementById(`dec-form-cancel-${project.id}`);
    const decFormStatus = document.getElementById(`dec-form-status-${project.id}`);
    if (addPinBtn && decForm) {
      addPinBtn.onclick = () => {
        const visible = decForm.style.display !== "none";
        decForm.style.display = visible ? "none" : "block";
        if (!visible && decFormTitle) decFormTitle.focus();
      };
    }
    if (decFormCancel) decFormCancel.onclick = () => {
      decForm.style.display = "none";
    };
    if (decFormAdd) {
      const doAddDecision = async () => {
        const title = (decFormTitle?.value || "").trim();
        const body2 = (decFormBody?.value || "").trim();
        const category = decFormCat?.value || "TECHNICAL";
        if (!title || !body2) {
          if (decFormStatus) decFormStatus.textContent = "Title and body required.";
          return;
        }
        if (title.length > 500) {
          if (decFormStatus) decFormStatus.textContent = "Title too long (500 char limit).";
          if (decFormTitle) decFormTitle.style.borderColor = "var(--red, #f87171)";
          return;
        }
        if (body2.length > 1e5) {
          if (decFormStatus) decFormStatus.textContent = "Body too long (100,000 char limit).";
          if (decFormBody) decFormBody.style.borderColor = "var(--red, #f87171)";
          return;
        }
        decFormAdd.disabled = true;
        if (decFormStatus) decFormStatus.textContent = "";
        try {
          await api(`/projects/${project.id}/decisions-pinned`, { method: "POST", body: JSON.stringify({ title, body: body2, category }) });
          if (decFormTitle) decFormTitle.value = "";
          if (decFormBody) decFormBody.value = "";
          decForm.style.display = "none";
          toast("decision pinned");
          loadPinnedDecisions(project.id);
        } catch (e3) {
          if (decFormStatus) decFormStatus.textContent = `Error: ${escapeHtml(String(e3))}`;
        } finally {
          decFormAdd.disabled = false;
        }
      };
      decFormAdd.onclick = doAddDecision;
      if (decFormTitle) decFormTitle.oninput = () => {
        const over = decFormTitle.value.length > 500;
        decFormTitle.style.borderColor = over ? "var(--red, #f87171)" : "";
        if (decFormStatus) decFormStatus.textContent = over ? `Title: ${decFormTitle.value.length}/500` : "";
      };
      if (decFormBody) decFormBody.oninput = () => {
        const len = decFormBody.value.length, limit = 1e5;
        const over = len > limit, near = len > limit * 0.9;
        decFormBody.style.borderColor = over ? "var(--red, #f87171)" : near ? "var(--warning, #fb923c)" : "";
        if (decFormStatus) decFormStatus.textContent = over || near ? `Body: ${len.toLocaleString()}/${limit.toLocaleString()}` : "";
      };
      [decFormTitle, decFormBody].forEach((el2) => {
        if (el2) el2.addEventListener("keydown", (e3) => {
          if ((e3.ctrlKey || e3.metaKey) && e3.key === "Enter") doAddDecision();
        });
      });
    }
    const consolidateBtn = document.getElementById(`consolidate-decisions-${project.id}`);
    if (consolidateBtn) consolidateBtn.onclick = () => consolidateDecisions(project.id);
    const saveGoalBtn = document.getElementById(`save-goal-${project.id}`);
    if (saveGoalBtn) saveGoalBtn.onclick = () => saveGoal(project.id);
    const saveNorthStarBtn = document.getElementById(`save-north-star-${project.id}`);
    if (saveNorthStarBtn) saveNorthStarBtn.onclick = () => saveNorthStar(project.id);
    const saveSprintBtn = document.getElementById(`save-sprint-${project.id}`);
    if (saveSprintBtn) saveSprintBtn.onclick = () => saveSprint(project.id);
    async function loadSprintBoard2() {
      const sprintItemsPath = `/projects/${project.id}/sprint-items`;
      try {
        const items = await projectApi(project.id, sprintItemsPath);
        const board = document.getElementById(`sprint-board-goal-${project.id}`);
        if (!board) return;
        if (!items || !items.length) {
          board.innerHTML = '<div style="color:var(--muted);font-size:10px;padding:4px 0">(no sprint items \u2014 use LIVE tab to add)</div>';
          return;
        }
        const activeStatuses = /* @__PURE__ */ new Set(["pending", "todo", "in_progress"]);
        const activeVersions = new Set(items.filter((it) => activeStatuses.has(it.status)).map((it) => it.version));
        const scopeItems = items.filter(
          (it) => activeStatuses.has(it.status) || it.version && activeVersions.has(it.version)
        );
        const doneCount = scopeItems.filter((i3) => i3.status === "done" || i3.status === "skipped").length;
        const activeCount = scopeItems.filter((i3) => activeStatuses.has(i3.status)).length;
        const total = scopeItems.length;
        const pct = total > 0 ? Math.round(doneCount / total * 100) : 0;
        const pctColor = doneCount === 0 ? "var(--muted)" : doneCount === total ? "var(--accent-green)" : "#fbbf24";
        const pendingItems = scopeItems.filter((i3) => activeStatuses.has(i3.status));
        const statusColors = { pending: "var(--muted)", todo: "var(--muted)", in_progress: "#fbbf24" };
        const itemsHtml = pendingItems.slice(0, 10).map((it) => {
          const color = statusColors[it.status] || "var(--muted)";
          const badge = it.status === "in_progress" ? "\u26A1" : "\xB7";
          return `<div style="display:flex;align-items:center;gap:5px;padding:2px 0;border-top:1px solid var(--border)20"><span style="color:${color};font-size:9px;flex-shrink:0">${badge}</span><span style="font-size:10px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</span>` + (it.version ? `<span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(it.version)}</span>` : "") + `</div>`;
        }).join("");
        board.innerHTML = `<div style="font-size:10px;color:var(--muted);padding:3px 0;display:flex;align-items:center;gap:8px;margin-bottom:${pendingItems.length ? "4px" : "0"}">

        <span style="font-weight:600;color:var(--accent)">all active</span>

        <span style="color:${pctColor}">${doneCount}/${total} done (${pct}%)</span>

        ${activeCount > 0 ? `<span style="color:var(--accent)">${activeCount} pending</span>` : '<span style="color:var(--accent-green)">\u2713 complete</span>'}

        <span style="opacity:0.5">\xB7 See LIVE tab for full board</span>

      </div>${itemsHtml}`;
      } catch (e3) {
        const board = document.getElementById(`sprint-board-goal-${project.id}`);
        if (!board) return;
        board.innerHTML = renderProjectLoadError2(project.id, "Sprint board unavailable", sprintItemsPath, e3);
        wireProjectLoadRetry2(board, project.id);
      }
    }
    _sprintBoardReloaders[project.id] = loadSprintBoard2;
    loadSprintBoard2();
    setTimeout(async () => {
      const sel = document.getElementById(`goal-sprint-select-${project.id}`);
      const inp = document.getElementById(`goal-sprint-${project.id}`);
      if (!sel || !inp) return;
      try {
        const sessions = await projectApi(project.id, `/projects/${project.id}/sessions`);
        const active = (sessions || []).filter((s3) => s3.status !== "closed" && s3.status !== "archived");
        const opts = active.map((s3) => `<option value="${escapeHtml(s3.name)}">${escapeHtml(s3.name)}</option>`).join("");
        sel.innerHTML = opts + '<option value="__custom__">Custom\u2026</option>';
        _sprintSelectSyncers[project.id] = function(val) {
          if (!sel) return;
          const match = Array.from(sel.options).find((o3) => o3.value === val && o3.value !== "__custom__");
          if (match) {
            sel.value = val;
            inp.value = val;
            inp.style.display = "none";
          } else if (val) {
            sel.value = "__custom__";
            inp.style.display = "block";
            inp.value = val;
          } else {
            if (sel.options.length) sel.selectedIndex = 0;
            inp.value = sel.value && sel.value !== "__custom__" ? sel.value : "";
            inp.style.display = "none";
          }
        };
        if (inp.value) _sprintSelectSyncers[project.id](inp.value);
      } catch (_2) {
        sel.innerHTML = '<option value="__custom__">Custom\u2026</option>';
        inp.style.display = "block";
      }
      sel.onchange = () => {
        if (sel.value === "__custom__") {
          inp.style.display = "block";
          inp.focus();
        } else {
          inp.style.display = "none";
          inp.value = sel.value;
        }
      };
    }, 200);
    const sprintAddBtn = document.getElementById(`sprint-add-btn-${project.id}`);
    const sprintAddInput = document.getElementById(`sprint-add-input-${project.id}`);
    if (sprintAddBtn && sprintAddInput) {
      const doAdd = async () => {
        const title = sprintAddInput.value.trim();
        if (!title) return;
        try {
          await api(`/projects/${project.id}/sprint-items`, { method: "POST", body: JSON.stringify({ title, version: state.panels[project.id]?.sprint || "current" }) });
          sprintAddInput.value = "";
          loadSprintBoard2();
        } catch (e3) {
          console.error("Add sprint item failed:", e3);
        }
      };
      sprintAddBtn.onclick = doAdd;
      sprintAddInput.onkeydown = (e3) => {
        if (e3.key === "Enter") doAdd();
      };
    }
    {
      const appendBtn = document.getElementById(`devlog-append-btn-${project.id}`);
      const appendText = document.getElementById(`devlog-append-text-${project.id}`);
      const appendStatus = document.getElementById(`devlog-append-status-${project.id}`);
      if (appendBtn && appendText) {
        appendBtn.onclick = async () => {
          const text = appendText.value.trim();
          if (!text) return;
          appendBtn.disabled = true;
          if (appendStatus) appendStatus.textContent = "";
          try {
            await api(`/projects/${project.id}/devlog`, { method: "POST", body: JSON.stringify({ text }) });
            appendText.value = "";
            toast("Appended to DEVLOG.md");
          } catch (e3) {
            if (appendStatus) appendStatus.textContent = `Error: ${escapeHtml(String(e3))}`;
          } finally {
            appendBtn.disabled = false;
          }
        };
        appendText.addEventListener("keydown", (e3) => {
          if ((e3.ctrlKey || e3.metaKey) && e3.key === "Enter") appendBtn.click();
        });
      }
    }
    wireClaudeLaunchPanel(project.id);
    document.getElementById(`goal-${project.id}`).addEventListener("blur", () => saveGoal(project.id));
    document.getElementById(`goal-north-star-${project.id}`).addEventListener("blur", () => saveNorthStar(project.id));
    document.getElementById(`goal-sprint-${project.id}`).addEventListener("blur", () => saveSprint(project.id));
    document.getElementById(`goal-${project.id}`).addEventListener("input", function() {
      const p3 = state.panels[project.id];
      this.classList.toggle("dirty", this.value !== (p3._lastSaved || ""));
    });
    document.getElementById(`goal-north-star-${project.id}`).addEventListener("input", function() {
      const p3 = state.panels[project.id];
      this.classList.toggle("dirty", this.value !== (p3._serverNorthStar || ""));
      autosizeGoalField(this);
    });
    document.getElementById(`goal-sprint-${project.id}`).addEventListener("input", function() {
      const p3 = state.panels[project.id];
      this.classList.toggle("dirty", this.value !== (p3._serverSprint || ""));
    });
    autosizeGoalField(document.getElementById(`goal-north-star-${project.id}`));
    const fileBackBtn = document.getElementById(`file-back-${project.id}`);
    const fileSaveBtn = document.getElementById(`file-save-${project.id}`);
    if (fileBackBtn) fileBackBtn.onclick = () => {
      const browse = document.getElementById(`files-browse-${project.id}`);
      const editorWrap = document.getElementById(`file-editor-wrap-${project.id}`);
      if (browse) browse.style.display = "";
      if (editorWrap) editorWrap.style.display = "none";
    };
    if (fileSaveBtn) fileSaveBtn.onclick = () => saveFile(project.id);
    refreshTab(project.id);
    connectWs(project.id);
  }
  var LIVE_REFRESH_MS = 3e4;
  var LIVE_THROTTLE_MS = 1e4;
  var liveRefreshState = {};
  function renderInProgressBySession(projectId, sprintItems, sessions) {
    const root = document.getElementById(`live-inprogress-by-session-${projectId}`);
    if (!root) return;
    const sessById = {};
    for (const s3 of sessions || []) sessById[String(s3.id)] = s3;
    const inProgress = (sprintItems || []).filter((it) => String(it.status) === "in_progress");
    if (!inProgress.length) {
      root.innerHTML = '<div class="live-empty">Nothing in progress right now.</div>';
      return;
    }
    const byActor = {};
    const unassigned = [];
    for (const it of inProgress) {
      const actor = it.actor ? String(it.actor) : "";
      if (actor) (byActor[actor] = byActor[actor] || []).push(it);
      else unassigned.push(it);
    }
    const _hue = (s3) => {
      let h3 = 0;
      for (let i3 = 0; i3 < s3.length; i3++) h3 = (h3 * 31 + s3.charCodeAt(i3)) % 360;
      return h3;
    };
    const _elapsed = (ts) => {
      if (!ts) return "";
      const t3 = Date.parse(String(ts).replace(" ", "T"));
      if (isNaN(t3)) return "";
      const mins = Math.max(0, Math.floor((Date.now() - t3) / 6e4));
      return mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h${mins % 60}m`;
    };
    const _item = (it, color) => `<div class="live-ip-item" style="display:flex;gap:6px;align-items:center;margin-top:4px">
      <span style="width:5px;height:5px;border-radius:50%;background:${color};flex:none"></span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${escapeHtml(String(it.nickname || it.title || it.id || ""))}</span>
      ${_elapsed(it.claimed_at) ? `<span style="font-size:9px;color:var(--muted);flex:none">${_elapsed(it.claimed_at)}</span>` : ""}
    </div>`;
    const actors = Object.keys(byActor).sort((a3, b2) => byActor[b2].length - byActor[a3].length);
    let html = "";
    for (const actor of actors) {
      const sess = sessById[actor] || {};
      const color = `hsl(${_hue(actor)} 70% 55%)`;
      const name = sess.name || `${actor.slice(0, 8)}\u2026`;
      const status = String(sess.status || "unknown");
      html += `<div class="live-ip-owner" data-session="${escapeHtml(actor)}" style="border-left:3px solid ${color};padding:4px 0 4px 8px;margin-bottom:8px">
      <div style="display:flex;gap:6px;align-items:center;justify-content:space-between">
        <span style="font-size:11px;font-weight:600;color:var(--text)">${escapeHtml(String(name))}</span>
        <span style="font-size:9px;padding:0 5px;border-radius:3px;border:1px solid ${color};color:${color}">${status === "active" ? "ACTIVE" : escapeHtml(status.toUpperCase())} \xB7 ${byActor[actor].length}</span>
      </div>
      ${byActor[actor].map((it) => _item(it, color)).join("")}
    </div>`;
    }
    if (unassigned.length) {
      html += `<div class="live-ip-owner" style="border-left:3px solid var(--muted);padding:4px 0 4px 8px">
      <div style="font-size:10px;font-weight:600;color:var(--muted)">Unassigned \xB7 ${unassigned.length}</div>
      ${unassigned.map((it) => _item(it, "var(--muted)")).join("")}
    </div>`;
    }
    root.innerHTML = html;
  }
  function scheduleLiveRefresh(projectId) {
    const s3 = liveRefreshState[projectId] || (liveRefreshState[projectId] = {});
    clearTimeout(s3.timer);
    if (!s3.enabled) return;
    const sinceLastMs = Date.now() - (s3.lastRefresh || 0);
    const wait = Math.max(LIVE_THROTTLE_MS, LIVE_REFRESH_MS - sinceLastMs);
    s3.timer = setTimeout(async () => {
      s3.lastRefresh = Date.now();
      const panel = state.panels[projectId];
      if (panel && panel.activeVtab === "live") {
        await refreshLiveTab(projectId);
      }
      scheduleLiveRefresh(projectId);
    }, wait);
  }
  function initLiveAutoRefresh(projectId) {
    const s3 = liveRefreshState[projectId] || (liveRefreshState[projectId] = {});
    const stored = localStorage.getItem(STORAGE_KEY2("meridian.liveAutoRefresh"));
    s3.enabled = stored === null ? true : stored === "true";
    const btn = document.getElementById(`live-auto-btn-${projectId}`);
    if (btn) {
      btn.textContent = s3.enabled ? "\u21BB Auto" : "\u21BB Off";
      btn.style.opacity = s3.enabled ? "1" : "0.4";
      btn.onclick = () => {
        s3.enabled = !s3.enabled;
        localStorage.setItem(STORAGE_KEY2("meridian.liveAutoRefresh"), String(s3.enabled));
        btn.textContent = s3.enabled ? "\u21BB Auto" : "\u21BB Off";
        btn.style.opacity = s3.enabled ? "1" : "0.4";
        if (s3.enabled) scheduleLiveRefresh(projectId);
        else clearTimeout(s3.timer);
      };
    }
    if (s3.enabled) scheduleLiveRefresh(projectId);
  }
  async function loadLiveTab(projectId) {
    const panel = state.panels[projectId];
    if (!panel) return;
    panel.liveWired = panel.liveWired || false;
    if (!panel.liveWired) {
      const pause = document.getElementById(`live-pause-${projectId}`);
      const runAll = document.getElementById(`live-run-${projectId}`);
      if (pause) pause.onclick = () => toast("Pause is a stub \u2014 coming soon");
      if (runAll) runAll.onclick = () => toast("Run All is a stub \u2014 coming soon");
      const input = document.getElementById(`live-add-input-${projectId}`);
      if (input) input.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        const text = (input.value || "").trim();
        if (!text) return;
        addLiveTask(projectId, text).then((ok) => {
          if (ok) input.value = "";
        });
      });
      const addToggle = document.getElementById(`add-to-run-toggle-${projectId}`);
      const addArea = document.getElementById(`add-to-run-area-${projectId}`);
      const addCancel = document.getElementById(`add-to-run-cancel-${projectId}`);
      const addSubmit = document.getElementById(`add-to-run-submit-${projectId}`);
      const addText = document.getElementById(`add-to-run-text-${projectId}`);
      if (addToggle && addArea) {
        addToggle.onclick = () => {
          const open = addArea.style.display !== "none";
          addArea.style.display = open ? "none" : "block";
          addToggle.textContent = open ? "+ Expand" : "\u2212 Collapse";
          if (!open && addText) addText.focus();
        };
      }
      if (addCancel && addArea) {
        addCancel.onclick = () => {
          addArea.style.display = "none";
          addToggle.textContent = "+ Expand";
          if (addText) addText.value = "";
        };
      }
      if (addSubmit) {
        addSubmit.onclick = async () => {
          const text = (addText && addText.value || "").trim();
          if (!text) {
            toast("Enter text first", true);
            return;
          }
          const pid = addSubmit.dataset.project;
          try {
            const sessions = await api(`/projects/${pid}/sessions?active_only=true`);
            const activeSid = sessions && sessions[0] && sessions[0].id;
            await api(`/projects/${pid}/hitl-requests`, {
              method: "POST",
              body: JSON.stringify({
                question: `Add to current goal: ${text}`,
                urgency: "high",
                session_id: activeSid || void 0
              })
            });
            toast("Sent to active session HITL queue");
            if (addText) addText.value = "";
            addArea.style.display = "none";
            addToggle.textContent = "+ Expand";
          } catch (e3) {
            toast("Failed: " + e3.message, true);
          }
        };
      }
      const newSprintBtn = document.getElementById(`new-sprint-btn-${projectId}`);
      if (newSprintBtn) {
        newSprintBtn.onclick = () => {
          const overlay = document.createElement("div");
          overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9999;display:flex;align-items:center;justify-content:center";
          overlay.innerHTML = `

          <div style="background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:22px 24px;min-width:320px;max-width:460px;width:90%;box-shadow:0 8px 32px #0008">

            <div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:12px">New Sprint</div>

            <input id="_new-sprint-input" type="text" placeholder="e.g. v1.1 \u2014 auth + billing" autofocus

              style="width:100%;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px;font-family:var(--font-mono);padding:6px 10px;outline:none;box-sizing:border-box">

            <div id="_new-sprint-err" style="color:#f87171;font-size:10px;margin-top:4px;display:none"></div>

            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">

              <button class="secondary" id="_new-sprint-cancel" style="padding:4px 12px;font-size:11px">Cancel</button>

              <button class="primary" id="_new-sprint-submit" style="padding:4px 12px;font-size:11px">Set Sprint</button>

            </div>

          </div>`;
          document.body.appendChild(overlay);
          const inp = overlay.querySelector("#_new-sprint-input");
          const errEl = overlay.querySelector("#_new-sprint-err");
          const close = () => overlay.remove();
          overlay.querySelector("#_new-sprint-cancel").onclick = close;
          overlay.onclick = (e3) => {
            if (e3.target === overlay) close();
          };
          const submit = async () => {
            const name = (inp.value || "").trim();
            if (!name) {
              errEl.textContent = "Sprint name is required";
              errEl.style.display = "";
              return;
            }
            try {
              overlay.querySelector("#_new-sprint-submit").disabled = true;
              await api(`/projects/${projectId}/goal/sprint`, { method: "POST", body: JSON.stringify({ sprint: name }) });
              toast(`Sprint set: ${name}`);
              close();
            } catch (e3) {
              errEl.textContent = e3.message || "Failed";
              errEl.style.display = "";
              overlay.querySelector("#_new-sprint-submit").disabled = false;
            }
          };
          overlay.querySelector("#_new-sprint-submit").onclick = submit;
          inp.addEventListener("keydown", (e3) => {
            if (e3.key === "Enter") submit();
            if (e3.key === "Escape") close();
          });
          setTimeout(() => inp.focus(), 50);
        };
      }
      panel.liveWired = true;
    }
    await refreshLiveTab(projectId);
    initLiveAutoRefresh(projectId);
  }
  async function refreshLiveTab(projectId) {
    try {
      const sessionsPath = `/projects/${projectId}/sessions?active_only=false`;
      const tasksPath = `/projects/${projectId}/tasks?limit=200`;
      const sprintItemsPath = `/projects/${projectId}/sprint-items`;
      const worktreesPath = `/projects/${projectId}/worktrees`;
      const [sessionsResult, tasksResult, sprintItemsResult, worktreesResult] = await Promise.allSettled([
        projectApi(projectId, sessionsPath),
        projectApi(projectId, tasksPath),
        projectApi(projectId, sprintItemsPath),
        projectApi(projectId, worktreesPath)
      ]);
      if (sprintItemsResult.status === "fulfilled") {
        renderSprintProgress(projectId, sprintItemsResult.value || []);
        renderWaveProgress(projectId, sprintItemsResult.value || []);
      } else {
        const sprintRoot = document.getElementById(`live-sprint-progress-${projectId}`);
        if (sprintRoot) {
          sprintRoot.innerHTML = renderProjectLoadError2(projectId, "Sprint progress unavailable", sprintItemsPath, sprintItemsResult.reason);
          wireProjectLoadRetry2(sprintRoot, projectId);
        }
      }
      if (sprintItemsResult.status === "fulfilled" && sessionsResult.status === "fulfilled") {
        renderInProgressBySession(projectId, sprintItemsResult.value || [], sessionsResult.value || []);
      }
      if (sessionsResult.status === "fulfilled" && tasksResult.status === "fulfilled") {
        const worktrees = worktreesResult.status === "fulfilled" ? worktreesResult.value || [] : [];
        const sessions = sessionsResult.value || [];
        renderLiveSessions(projectId, sessions, tasksResult.value || [], worktrees);
        cacheMostRecentSession(projectId, sessions);
        const activeSession = sessions.find((s3) => s3.status === "active") || sessions[0];
        if (activeSession && activeSession.id) {
          loadSprintNotesPanel(projectId, activeSession.id).catch(() => {
          });
        }
      } else {
        const sessionsRoot = document.getElementById(`live-sessions-${projectId}`);
        if (sessionsRoot) {
          const liveError = sessionsResult.status === "rejected" ? sessionsResult.reason : tasksResult.reason;
          const livePath = sessionsResult.status === "rejected" ? sessionsPath : tasksPath;
          sessionsRoot.innerHTML = renderProjectLoadError2(projectId, "Live sessions unavailable", livePath, liveError);
          wireProjectLoadRetry2(sessionsRoot, projectId);
        }
      }
      if (tasksResult.status === "fulfilled") {
        renderLiveQueue(projectId, tasksResult.value || []);
      } else {
        const queueRoot = document.getElementById(`live-queue-${projectId}`);
        if (queueRoot) {
          queueRoot.innerHTML = renderProjectLoadError2(projectId, "Live queue unavailable", tasksPath, tasksResult.reason);
          wireProjectLoadRetry2(queueRoot, projectId);
        }
      }
    } catch (e3) {
    }
  }
  function wireSprintAddEnter2(projectId, root) {
    const inp = root.querySelector(`#sprint-add-input-${projectId}`);
    if (inp) inp.onkeydown = (e3) => {
      if (e3.key === "Enter") addSprintItemFromInput2(projectId);
    };
  }
  async function sprintAction(projectId, itemId, action) {
    try {
      await api(
        `/projects/${projectId}/sprint-items/${itemId}/${action}`,
        { method: "POST", body: JSON.stringify({}) }
      );
      toast(`Sprint item ${action}d`);
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast(`Failed: ${e3.message}`, true);
    }
  }
  async function sprintArchive(projectId, itemId) {
    if (!confirm("Permanently delete this backburner item? This cannot be undone.")) return;
    try {
      const r3 = await fetch(`/projects/${projectId}/sprint-items/${itemId}`, { method: "DELETE" });
      if (!r3.ok && r3.status !== 204) throw new Error(`${r3.status}`);
      toast("Backburner item deleted");
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast(`Delete failed: ${e3.message}`, true);
    }
  }
  function filterBackburner(projectId, value) {
    const q2 = (value || "").trim().toLowerCase();
    const sec = document.querySelector('.queue-section[data-section="backburner"]');
    if (!sec) return;
    sec.querySelectorAll(".queue-item").forEach((el2) => {
      const hit = !q2 || (el2.dataset.bbTitle || "").includes(q2) || (el2.dataset.bbGroup || "").includes(q2);
      el2.style.display = hit ? "" : "none";
    });
    sec.querySelectorAll(".bb-group").forEach((g2) => {
      const anyVisible = Array.from(g2.querySelectorAll(".queue-item")).some((el2) => el2.style.display !== "none");
      g2.style.display = anyVisible ? "" : "none";
    });
  }
  async function sprintPushPrompt(projectId, itemId) {
    const toVersion = window.prompt("Push to version (e.g. v2.0):");
    if (!toVersion) return;
    try {
      await api(
        `/projects/${projectId}/sprint-items/${itemId}/push`,
        { method: "POST", body: JSON.stringify({ to_version: toVersion }) }
      );
      toast("Sprint item pushed to " + toVersion);
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast(`Push failed: ${e3.message}`, true);
    }
  }
  async function sprintFeedback(projectId, itemId, thumb, currentThumb, event) {
    event && event.stopPropagation();
    const newThumb = currentThumb === thumb ? null : thumb;
    try {
      await api(
        `/projects/${projectId}/sprint-items/${itemId}`,
        { method: "PATCH", body: JSON.stringify({ feedback_thumb: newThumb }) }
      );
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast("Feedback failed: " + e3.message, true);
    }
  }
  async function sprintFeedbackNote(projectId, itemId, note) {
    if (!note || !note.trim()) return;
    try {
      await api(
        `/projects/${projectId}/sprint-items/${itemId}`,
        { method: "PATCH", body: JSON.stringify({ feedback_note: note.trim() }) }
      );
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast("Note save failed: " + e3.message, true);
    }
  }
  async function sprintItemEdit(projectId, itemId) {
    const row = document.querySelector(`.sprint-item-row[data-item="${CSS.escape(itemId)}"]`);
    if (!row) return;
    const curTitle = row.dataset.title || "";
    const curVersion = row.dataset.version || "";
    const titleSpan = row.querySelector(".sprint-item-title");
    const verSpan = row.querySelector(".sprint-item-ver");
    if (!titleSpan || !verSpan) return;
    if (row.querySelector(".sprint-edit-input")) return;
    const titleInput = document.createElement("input");
    titleInput.className = "sprint-edit-input";
    titleInput.value = curTitle;
    titleInput.style.cssText = "flex:1;min-width:60px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;padding:1px 4px;color:var(--text);font-size:12px;font-family:var(--font-mono)";
    const verInput = document.createElement("input");
    verInput.className = "sprint-edit-input";
    verInput.value = curVersion;
    verInput.style.cssText = "width:60px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;padding:1px 4px;color:var(--muted);font-size:10px;font-family:var(--font-mono)";
    titleSpan.replaceWith(titleInput);
    verSpan.replaceWith(verInput);
    titleInput.focus();
    titleInput.select();
    const save = async () => {
      const newTitle = titleInput.value.trim();
      const newVersion = verInput.value.trim();
      if (!newTitle) {
        cancel();
        return;
      }
      try {
        await api(`/projects/${projectId}/sprint-items/${itemId}`, {
          method: "PATCH",
          body: JSON.stringify({ title: newTitle, version: newVersion || void 0 })
        });
        await refreshLiveTab(projectId);
      } catch (e3) {
        toast(`Save failed: ${e3.message}`, true);
        cancel();
      }
    };
    const cancel = () => {
      titleInput.replaceWith(titleSpan);
      verInput.replaceWith(verSpan);
    };
    titleInput.onkeydown = verInput.onkeydown = (e3) => {
      if (e3.key === "Enter") {
        e3.preventDefault();
        save();
      }
      if (e3.key === "Escape") cancel();
    };
    titleInput.onblur = verInput.onblur = () => {
      setTimeout(() => {
        if (!row.contains(document.activeElement)) save();
      }, 150);
    };
  }
  async function loadSprintNotesPanel(projectId, sessionId) {
    const section = document.getElementById(`sprint-notes-section-${projectId}`);
    const divider = document.getElementById(`sprint-notes-divider-${projectId}`);
    const container = document.getElementById(`sprint-notes-${projectId}`);
    if (!section || !container) return;
    try {
      const notes = await projectApi(projectId, `/sessions/${sessionId}/notes`);
      if (!notes || !notes.length) {
        section.style.display = "none";
        if (divider) divider.style.display = "none";
        return;
      }
      section.style.display = "";
      if (divider) divider.style.display = "";
      container.innerHTML = notes.map((n2) => `

      <div style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid var(--accent-green,#22c55e);border-radius:0 4px 4px 0;padding:6px 8px;margin-bottom:6px">

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">

          <span style="color:var(--accent-green,#22c55e);font-weight:600;font-size:11px">${escapeHtml(n2.title || "")}</span>

          <span style="color:var(--muted);font-size:9px">${escapeHtml((n2.created_at || "").slice(0, 16).replace("T", " "))}</span>

        </div>

        <div style="color:var(--text);font-size:10px;line-height:1.5;white-space:pre-wrap;word-break:break-word">${typeof marked !== "undefined" ? marked.parse(n2.body || "") : escapeHtml(n2.body || "")}</div>

      </div>

    `).join("");
    } catch (_2) {
      section.style.display = "none";
      if (divider) divider.style.display = "none";
    }
  }
  async function addSprintItemFromInput2(projectId) {
    const inp = document.getElementById(`sprint-add-input-${projectId}`);
    if (!inp) return;
    const val = inp.value.trim();
    if (!val) return;
    let version, title;
    const colonIdx = val.indexOf(":");
    if (colonIdx > 0) {
      version = val.slice(0, colonIdx).trim();
      title = val.slice(colonIdx + 1).trim();
    } else {
      const panel = state.panels[projectId];
      const sprint = panel && panel._serverSprint || "";
      const m3 = sprint.match(/v[\w.+-]+/i);
      version = m3 ? m3[0] : "current";
      title = val;
    }
    if (!title) {
      toast("Title required", true);
      return;
    }
    if (title.length > 500) {
      toast("Title too long (500 char limit)", true);
      inp.style.borderColor = "var(--red, #f87171)";
      return;
    }
    try {
      await api(`/projects/${projectId}/sprint-items`, {
        method: "POST",
        body: JSON.stringify({ version, title })
      });
      inp.value = "";
      inp.style.borderColor = "";
      toast("Sprint item added");
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast("Add failed: " + e3.message, true);
    }
  }
  function cacheMostRecentSession(projectId, sessions) {
    const panel = state.panels[projectId];
    if (!panel) return;
    const sorted = sortSessionsMostRecentFirst(sessions);
    const top = sorted.find((s3) => isLiveSession(s3)) || sorted.find((s3) => s3.status !== "closed") || sorted[0];
    if (top) panel.liveLastSessionId = top.id;
  }
  function renderLiveSessions(projectId, sessions, tasks, worktrees) {
    const root = document.getElementById(`live-sessions-${projectId}`);
    if (!root) return;
    const worktreeMap = /* @__PURE__ */ new Map();
    (worktrees || []).forEach((wt) => {
      if (!worktreeMap.has(wt.session_id)) worktreeMap.set(wt.session_id, []);
      worktreeMap.get(wt.session_id).push(wt.branch);
    });
    const claimMap = /* @__PURE__ */ new Map();
    const taskMap = /* @__PURE__ */ new Map();
    tasks.forEach((t3) => {
      if (t3.claimed_by && (t3.status === "pending" || t3.status === "in_progress")) {
        claimMap.set(t3.claimed_by, t3);
      }
      const sid = t3.session_id || t3.claimed_by;
      if (sid) {
        if (!taskMap.has(sid)) taskMap.set(sid, []);
        taskMap.get(sid).push(t3);
      }
    });
    taskMap.forEach((rows2) => rows2.sort((a3, b2) => String(b2.created_at || "").localeCompare(String(a3.created_at || ""))));
    const rows = sessions.map((s3) => {
      const ageMs = sessionAgeMs(s3);
      return { s: s3, ageMs };
    }).filter(({ ageMs }) => ageMs > 0 && ageMs <= 24 * 3600 * 1e3).sort((a3, b2) => a3.ageMs - b2.ageMs);
    const LIVE_PRESENCE_MS = 10 * 60 * 1e3;
    const liveSection = root.closest(".live-section");
    const sectionDivider = liveSection ? liveSection.nextElementSibling : null;
    const anyLivePresence = rows.some(({ ageMs }) => ageMs <= LIVE_PRESENCE_MS);
    if (!anyLivePresence) {
      if (liveSection) liveSection.style.display = "none";
      if (sectionDivider && sectionDivider.classList && sectionDivider.classList.contains("live-divider")) sectionDivider.style.display = "none";
      root.innerHTML = "";
      return;
    }
    if (liveSection) liveSection.style.display = "";
    if (sectionDivider && sectionDivider.classList && sectionDivider.classList.contains("live-divider")) sectionDivider.style.display = "";
    if (!rows.length) {
      root.innerHTML = '<div class="live-empty">No active sessions.</div>';
      return;
    }
    root.innerHTML = rows.map(({ s: s3, ageMs }) => {
      const mins = ageMs / 6e4;
      const live = isLiveSession(s3, ageMs);
      const dot = live ? mins < 5 ? "\u{1F7E2}" : "\u{1F7E1}" : "\u26AB";
      const displayStatus = live ? "live" : s3.status === "closed" || s3.status === "archived" ? s3.status : "idle";
      const label = s3.human_id ? `${s3.human_id}/${s3.name}` : s3.name;
      const claimed = claimMap.get(s3.id);
      const claimedRow = claimed ? `<div class="live-session-task">\u21B3 ${escapeHtml((claimed.description || "").slice(0, 140))}</div>` : "";
      const sessionTasks = taskMap.get(s3.id) || [];
      const taskRows = sessionTasks.slice(0, 3).map(
        (t3) => `<div class="live-session-task">\u21B3 ${escapeHtml((t3.description || "").slice(0, 140))}</div>`
      ).join("") || claimedRow;
      const taskLink = sessionTasks.length > 0 ? `<button class="link-button live-session-task-link" data-session-id="${escapeHtml(s3.id)}" style="margin-left:18px;margin-top:3px;font-size:10px;color:var(--accent);background:none;border:none;padding:0;cursor:pointer">View all ${sessionTasks.length} tasks \u2192</button>` : "";
      const summary = s3.session_summary;
      const summaryRow = summary && summary.summary && (s3.status === "closed" || s3.status === "archived") ? `<div class="live-session-outcome" style="font-size:10px;color:var(--muted);margin-top:3px;padding-left:18px">\u2713 ${escapeHtml((summary.summary || "").slice(0, 160))}` + (summary.tasks_completed != null ? ` \xB7 ${summary.tasks_completed} tasks` : "") + `</div>` : "";
      const fw = s3.agent_framework || "claude_code";
      const fwBadge = fw && fw !== "claude_code" ? `<span class="framework-badge" title="framework: ${escapeHtml(fw)}" style="display:inline-block;background:var(--surface-2);color:var(--accent);font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">${escapeHtml(fw)}</span>` : "";
      const sessionWorktrees = worktreeMap.get(s3.id) || [];
      const worktreeBadges = sessionWorktrees.map(
        (branch) => `<span class="worktree-badge" title="active worktree: ${escapeHtml(branch)}" style="display:inline-block;background:var(--surface-2);color:#a78bfa;font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">\u2387 ${escapeHtml(branch.replace("worktree/", ""))}</span>`
      ).join("");
      const endBtn = live ? `<button class="secondary live-session-end" data-session-id="${escapeHtml(s3.id)}" style="padding:1px 6px;font-size:9px;margin-left:6px" title="Mark this session idle">End session</button>` : "";
      return `<div class="live-session-row" data-session-status="${escapeHtml(displayStatus)}">

      <div class="live-session-head">

        <span class="live-dot">${dot}</span>

        <span class="live-session-name">${escapeHtml(label)}</span>${fwBadge}${worktreeBadges}

        <span class="live-session-status" style="font-size:9px;color:var(--muted);text-transform:uppercase">${escapeHtml(displayStatus)}</span>

        <span class="live-session-age">${escapeHtml(formatRelativeTime(s3.last_seen))}</span>

        ${endBtn}

      </div>

      ${taskRows}${taskLink}${summaryRow}

    </div>`;
    }).join("");
    root.querySelectorAll(".live-session-end").forEach((btn) => {
      btn.onclick = () => endLiveSession(projectId, btn.dataset.sessionId);
    });
    root.querySelectorAll(".live-session-task-link").forEach((btn) => {
      btn.onclick = () => openTimelineForSession(projectId, btn.dataset.sessionId);
    });
  }
  async function endLiveSession(projectId, sessionId) {
    if (!sessionId) return;
    try {
      await api(`/sessions/${sessionId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "idle" })
      });
      toast("Session marked idle");
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast(`End session failed: ${e3.message}`, true);
    }
  }
  function openTimelineForSession(projectId, sessionId) {
    const panel = getPanelState(projectId);
    panel.timelineSessionFilter = sessionId || null;
    try {
      localStorage.setItem("meridian_tl_view_" + projectId, "tasks");
    } catch (_2) {
    }
    const btn = document.querySelector(`#vtab-strip-${projectId} .vtab-btn[data-vtab="timeline"]`);
    if (btn) btn.click();
    else loadTimeline2(projectId);
  }
  function renderLiveQueue(projectId, tasks) {
    const root = document.getElementById(`live-queue-${projectId}`);
    if (!root) return;
    const live = tasks.filter((t3) => t3.status === "pending" || t3.status === "in_progress");
    if (!live.length) {
      root.innerHTML = '<div class="live-empty">Queue is empty. Add a task above.</div>';
      return;
    }
    live.sort((a3, b2) => {
      if (a3.status !== b2.status) return a3.status === "in_progress" ? -1 : 1;
      return (b2.created_at || "").localeCompare(a3.created_at || "");
    });
    root.innerHTML = live.map((t3) => {
      const dot = t3.status === "in_progress" ? "\u{1F535}" : "\u{1F4CB}";
      const claimLabel = t3.claimed_by_human_id || t3.claimed_by_session_name || t3.claimed_by || "";
      const claimed = t3.claimed_by ? `<span class="live-task-claim">claimed by: ${escapeHtml(claimLabel.slice(0, 24))}</span>` : "";
      const ts = formatRelativeTime(t3.created_at);
      const eid = `live-expand-${projectId}-${t3.id.slice(0, 8)}`;
      const expandMeta = [
        t3.session_name ? `session: ${t3.session_name}` : "",
        t3.claimed_by ? `claimed_by: ${t3.claimed_by_human_id || t3.claimed_by_session_name || t3.claimed_by}` : "",
        t3.created_at ? `created: ${t3.created_at}` : "",
        t3.claimed_at ? `claimed: ${t3.claimed_at}` : ""
      ].filter(Boolean).join(" \xB7 ");
      return `<div class="live-task-row" data-task="${escapeHtml(t3.id)}">

      <span class="live-dot">${dot}</span>

      <div class="live-task-body" style="cursor:pointer" onclick="toggleExpand('${eid}')">

        <div class="live-task-desc">${escapeHtml((t3.description || "").slice(0, 200))}</div>

        <div class="live-task-meta">${escapeHtml(ts)} ${claimed} <span class="expand-arrow" style="font-size:9px;color:var(--muted)">\u25B6</span></div>

        <div id="${eid}" style="display:none;margin-top:4px;font-size:10px;color:var(--muted);white-space:pre-wrap;word-break:break-word">${escapeHtml(t3.description || "")}${expandMeta ? "\n" + escapeHtml(expandMeta) : ""}</div>

      </div>

      <button class="live-task-cancel" data-cancel="${escapeHtml(t3.id)}" title="Mark done / cancel">\xD7</button>

    </div>`;
    }).join("");
    root.querySelectorAll("button[data-cancel]").forEach((btn) => {
      btn.onclick = () => cancelLiveTask(projectId, btn.dataset.cancel);
    });
  }
  async function addLiveTask(projectId, description) {
    const panel = state.panels[projectId];
    const sessionId = panel && panel.liveLastSessionId;
    if (!sessionId) {
      try {
        const sessions = await api(`/projects/${projectId}/sessions`);
        cacheMostRecentSession(projectId, sessions || []);
      } catch (e3) {
      }
    }
    const sid = panel && panel.liveLastSessionId;
    if (!sid) {
      toast("No active session to attribute the task to", true);
      return false;
    }
    try {
      await api("/tasks", {
        method: "POST",
        body: JSON.stringify({
          session_id: sid,
          project_id: projectId,
          description,
          status: "pending"
        })
      });
      toast("task queued");
      await refreshLiveTab(projectId);
      return true;
    } catch (e3) {
      toast("add task failed: " + e3.message, true);
      return false;
    }
  }
  async function cancelLiveTask(projectId, taskId) {
    try {
      await api(`/tasks/${taskId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "done" })
      });
      toast("task closed");
      await refreshLiveTab(projectId);
    } catch (e3) {
      toast("cancel failed: " + e3.message, true);
    }
  }
  function showCopyPreview(title, content) {
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9998;display:flex;align-items:center;justify-content:center";
    overlay.innerHTML = `<div style="background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:20px;width:620px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;gap:12px;box-shadow:0 8px 32px #0008">

    <div style="font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--accent)">${escapeHtml(title)}</div>

    <textarea style="width:100%;height:300px;font-family:var(--font-mono);font-size:11px;background:#0d1117;color:#e6edf3;border:1px solid var(--border);padding:10px;border-radius:4px;resize:vertical;outline:none">${escapeHtml(content)}</textarea>

    <div style="display:flex;gap:8px;justify-content:flex-end">

      <button class="secondary" style="font-size:11px;padding:5px 14px">Cancel</button>

      <button class="primary" style="font-size:11px;padding:5px 14px">Copy &amp; Close</button>

    </div>

  </div>`;
    document.body.appendChild(overlay);
    const [cancelBtn, copyBtn] = overlay.querySelectorAll("button");
    const ta = overlay.querySelector("textarea");
    const close = () => overlay.remove();
    cancelBtn.onclick = close;
    overlay.addEventListener("click", (e3) => {
      if (e3.target === overlay) close();
    });
    copyBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(ta.value);
      } catch (_2) {
        ta.select();
        document.execCommand("copy");
      }
      copyBtn.textContent = "Copied!";
      setTimeout(close, 700);
    };
  }
  function wireClaudeLaunchPanel(projectId) {
    const PROJECT_QUOTE = projectId.replace(/"/g, '\\"');
    const sequentialKey = `meridian.sequentialMode.${projectId}`;
    function normalizeTouchesFile(path) {
      return String(path || "").trim().replace(/\\/g, "/").replace(/^\.\//, "");
    }
    function parseTouchesFiles(raw) {
      if (!raw) return [];
      if (Array.isArray(raw)) return raw.map(normalizeTouchesFile).filter(Boolean);
      const text = String(raw).trim();
      if (!text) return [];
      try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) return parsed.map(normalizeTouchesFile).filter(Boolean);
        return [normalizeTouchesFile(parsed)].filter(Boolean);
      } catch (e3) {
        return text.split(",").map(normalizeTouchesFile).filter(Boolean);
      }
    }
    function findTouchesFilesConflicts(items) {
      const active = (items || []).filter((it) => ["pending", "todo", "in_progress"].includes(it.status || "pending"));
      const byFile = /* @__PURE__ */ new Map();
      active.forEach((item) => {
        parseTouchesFiles(item.touches_files).forEach((file) => {
          const key = file.toLowerCase();
          const list = byFile.get(key) || [];
          list.push({ file, item });
          byFile.set(key, list);
        });
      });
      return Array.from(byFile.values()).filter((list) => list.length > 1 && list.some((entry) => entry.item.status === "in_progress")).flat();
    }
    function applySequentialMode(text) {
      const toggle = document.getElementById(`sequential-mode-${projectId}`);
      if (!toggle || !toggle.checked || !text) return text;
      return `${text}

SEQUENTIAL MODE:
- Work one sprint item at a time.
- Call claim_file(session_id, path) before editing shared files.
- Stop and coordinate if start_session returns file_warnings or claim_sprint_item returns CONFLICT.`;
    }
    async function warnBeforeHandoffCopy() {
      try {
        const items = await projectApi(projectId, `/projects/${projectId}/sprint-items`);
        const conflicts = findTouchesFilesConflicts(items || []);
        const warnEl = document.getElementById(`touches-files-warning-${projectId}`);
        if (warnEl) warnEl.style.display = conflicts.length ? "" : "none";
        if (!conflicts.length) return true;
        const files = Array.from(new Set(conflicts.map((c3) => c3.file))).join(", ");
        return confirm(`touches_files conflict warning:

${files}

Continue copying the handoff?`);
      } catch (e3) {
        return true;
      }
    }
    const sequentialToggle = document.getElementById(`sequential-mode-${projectId}`);
    if (sequentialToggle) {
      try {
        sequentialToggle.checked = localStorage.getItem(sequentialKey) === "1";
      } catch (e3) {
      }
      sequentialToggle.onchange = () => {
        try {
          localStorage.setItem(sequentialKey, sequentialToggle.checked ? "1" : "0");
        } catch (e3) {
        }
      };
    }
    const copyStartCodeBtn = document.getElementById(`copy-start-code-${projectId}`);
    if (copyStartCodeBtn) copyStartCodeBtn.onclick = () => {
      const cmd = `start_session(project_id="${PROJECT_QUOTE}", session_name="describe-what-youre-doing", human_id="adam")`;
      showCopyPreview("Start Claude Code Session", cmd);
    };
    const copyStartChatBtn = document.getElementById(`copy-start-chat-${projectId}`);
    if (copyStartChatBtn) copyStartChatBtn.onclick = async () => {
      const orig = copyStartChatBtn.textContent;
      copyStartChatBtn.disabled = true;
      copyStartChatBtn.textContent = "Loading\u2026";
      try {
        if (!await warnBeforeHandoffCopy()) return;
        const r3 = await fetch(`/projects/${projectId}/handoff`, { method: "POST" });
        if (!r3.ok) throw new Error(`${r3.status}`);
        const payload = await r3.json();
        const text = applySequentialMode(payload.content || "");
        showCopyPreview("Claude / Codex Handoff", text);
      } catch (e3) {
        toast("handoff failed: " + e3.message, true);
      } finally {
        copyStartChatBtn.disabled = false;
        copyStartChatBtn.textContent = orig;
      }
    };
    const setupHooksBtn = document.getElementById(`btn-setup-hooks-${projectId}`);
    if (setupHooksBtn) setupHooksBtn.onclick = () => {
      const baseUrl = window.location.origin;
      const instructions = `Auto-setup Meridian hooks for your AI tools:

macOS / Linux / WSL:
  curl -fsSL ${baseUrl}/install.sh | sh

Windows PowerShell:
  irm ${baseUrl}/install.ps1 | iex

These scripts detect Claude Code and Codex, then wire SessionStart + Stop
hooks pointing to ${baseUrl}/hooks/ with your project_id.

Project ID: ${projectId}`;
      showCopyPreview("\u26A1 Setup Hooks", instructions);
    };
    if (isDemoMode()) {
      [
        copyStartChatBtn,
        setupHooksBtn,
        document.getElementById(`copy-resume-${projectId}`),
        document.getElementById(`start-worker-${projectId}`),
        document.getElementById(`copy-handoff-${projectId}`),
        document.getElementById(`regen-handoff-${projectId}`)
      ].forEach((btn) => {
        if (!btn) return;
        btn.title = "Sign in to use";
        btn.style.opacity = "0.45";
        btn.style.cursor = "not-allowed";
        btn.onclick = (e3) => {
          e3.preventDefault();
          showDemoReadonlyToast();
        };
      });
      return;
    }
    const copyResumeBtn = document.getElementById(`copy-resume-${projectId}`);
    if (copyResumeBtn) copyResumeBtn.onclick = async () => {
      const sel = document.getElementById(`continue-session-${projectId}`);
      const sessionName = sel && sel.value ? sel.value : "";
      if (!sessionName) {
        toast("pick a session first", true);
        return;
      }
      const cmd = `start_session(project_id="${PROJECT_QUOTE}", session_name="${sessionName.replace(/"/g, '\\"')}", human_id="adam")
get_context_block(project_id="${PROJECT_QUOTE}", mode="full")`;
      showCopyPreview("Resume MCP Flow", cmd);
    };
    const startWorkerBtn = document.getElementById(`start-worker-${projectId}`);
    if (startWorkerBtn) startWorkerBtn.onclick = async () => {
      if (isDemoMode()) {
        showDemoReadonlyToast();
        return;
      }
      const resultEl = document.getElementById(`worker-result-${projectId}`);
      const emptyEl = document.getElementById(`worker-empty-${projectId}`);
      const xmlEl = document.getElementById(`worker-xml-${projectId}`);
      if (resultEl) resultEl.style.display = "none";
      if (emptyEl) emptyEl.style.display = "none";
      try {
        const r3 = await fetch(`/projects/${projectId}/start-worker-session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        });
        if (r3.status === 404) {
          if (emptyEl) emptyEl.style.display = "";
          return;
        }
        if (!r3.ok) throw new Error(`${r3.status}`);
        const body = await r3.json();
        const xml = body.worker_context || "";
        if (xmlEl) xmlEl.textContent = xml;
        if (resultEl) resultEl.style.display = "";
        toast("worker session ready");
      } catch (e3) {
        toast("start worker failed: " + e3.message, true);
      }
    };
    const copyWorkerBtn = document.getElementById(`copy-worker-${projectId}`);
    if (copyWorkerBtn) copyWorkerBtn.onclick = async () => {
      const xmlEl = document.getElementById(`worker-xml-${projectId}`);
      const text = xmlEl ? xmlEl.textContent : "";
      if (!text) {
        toast("nothing to copy", true);
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        toast("worker context copied");
      } catch (e3) {
        toast("copy failed: " + e3.message, true);
      }
    };
    const copyHandoffBtn = document.getElementById(`copy-handoff-${projectId}`);
    if (copyHandoffBtn) copyHandoffBtn.onclick = async () => {
      const orig = copyHandoffBtn.textContent;
      copyHandoffBtn.disabled = true;
      copyHandoffBtn.textContent = "Loading\u2026";
      try {
        if (!await warnBeforeHandoffCopy()) return;
        const r3 = await fetch(`/projects/${projectId}/handoff`, { method: "POST" });
        if (!r3.ok) throw new Error(`${r3.status}`);
        const payload = await r3.json();
        const text = applySequentialMode(payload.content || "");
        if (text) {
          const rawContainer = document.getElementById(`handoff-raw-${projectId}`);
          const rawTextEl = document.getElementById(`handoff-raw-text-${projectId}`);
          if (rawContainer && rawTextEl) {
            rawTextEl.value = text;
            rawContainer.style.display = "";
            rawTextEl.focus();
            rawTextEl.select();
          }
          try {
            await navigator.clipboard.writeText(text);
            toast("handoff copied to clipboard");
          } catch (_2) {
            toast("text shown below \u2014 select all and copy");
          }
          stampHandoffTs(projectId, /* @__PURE__ */ new Date());
        }
      } catch (e3) {
        toast("handoff failed: " + e3.message, true);
      } finally {
        copyHandoffBtn.disabled = false;
        copyHandoffBtn.textContent = orig;
      }
    };
    const handoffCopyTextBtn = document.getElementById(`handoff-copy-text-${projectId}`);
    if (handoffCopyTextBtn) handoffCopyTextBtn.onclick = async () => {
      const rawTextEl = document.getElementById(`handoff-raw-text-${projectId}`);
      if (!rawTextEl) return;
      try {
        await navigator.clipboard.writeText(rawTextEl.value);
        toast("copied");
      } catch (_2) {
        rawTextEl.select();
        document.execCommand("copy");
      }
    };
    const handoffCloseBtn = document.getElementById(`handoff-close-raw-${projectId}`);
    if (handoffCloseBtn) handoffCloseBtn.onclick = () => {
      const rawContainer = document.getElementById(`handoff-raw-${projectId}`);
      if (rawContainer) rawContainer.style.display = "none";
    };
    const copyContextBtn = document.getElementById(`copy-context-${projectId}`);
    if (copyContextBtn) copyContextBtn.onclick = async () => {
      const orig = copyContextBtn.textContent;
      copyContextBtn.disabled = true;
      copyContextBtn.textContent = "Loading\u2026";
      try {
        const r3 = await fetch(`/projects/${projectId}/context-block?mode=chat`);
        if (!r3.ok) throw new Error(`${r3.status}`);
        const text = await r3.text();
        showCopyPreview("Chat Context \u2014 paste into claude.ai", text);
      } catch (e3) {
        toast("copy context failed: " + e3.message, true);
      } finally {
        copyContextBtn.disabled = false;
        copyContextBtn.textContent = orig;
      }
    };
    const regenBtn = document.getElementById(`regen-handoff-${projectId}`);
    if (regenBtn) regenBtn.onclick = async () => {
      const tsEl = document.getElementById(`handoff-ts-${projectId}`);
      const orig = regenBtn.textContent;
      regenBtn.disabled = true;
      regenBtn.textContent = "Regenerating\u2026";
      try {
        const r3 = await fetch(`/projects/${projectId}/handoff`, { method: "POST" });
        if (!r3.ok) throw new Error(`${r3.status}`);
        await r3.json();
        stampHandoffTs(projectId, /* @__PURE__ */ new Date());
        if (tsEl) {
          const prev = tsEl.textContent;
          tsEl.textContent = "Regenerated \u2713";
          setTimeout(() => stampHandoffTs(projectId, /* @__PURE__ */ new Date()), 2e3);
        }
        toast("handoff regenerated");
      } catch (e3) {
        toast("regenerate failed: " + e3.message, true);
      } finally {
        regenBtn.disabled = false;
        regenBtn.textContent = orig;
      }
    };
  }
  function stampHandoffTs(projectId, when) {
    const tsEl = document.getElementById(`handoff-ts-${projectId}`);
    if (!tsEl) return;
    const iso = when.toISOString().replace("T", " ").slice(0, 19);
    tsEl.textContent = "Last generated: " + formatRelativeTime(iso);
  }
  function populateSessionDropdown(projectId, sessions) {
    const sel = document.getElementById(`continue-session-${projectId}`);
    if (!sel) return;
    const sorted = sortSessionsMostRecentFirst(sessions).slice(0, 5);
    if (!sorted.length) {
      sel.innerHTML = '<option value="">(no sessions yet)</option>';
      return;
    }
    const prev = sel.value;
    sel.innerHTML = sorted.map((s3) => {
      const label = `${s3.name} \u2014 ${formatRelativeTime(s3.last_seen)}`;
      return `<option value="${escapeHtml(s3.name)}">${escapeHtml(label)}</option>`;
    }).join("");
    if (prev && sorted.some((s3) => s3.name === prev)) sel.value = prev;
  }
  async function loadTimeline2(projectId) {
    const wrap = document.getElementById(`timeline-wrap-${projectId}`);
    if (!wrap) return;
    wrap.innerHTML = `<div class="timeline-empty">loading\u2026</div>`;
    let data;
    try {
      data = await api(`/projects/${projectId}/timeline`);
    } catch (e3) {
      wrap.innerHTML = `<div class="timeline-empty">timeline failed: ${escapeHtml(e3.message)}</div>`;
      return;
    }
    renderTimeline(projectId, data);
    const axisBtn = document.getElementById(`timeline-axis-${projectId}`);
    if (axisBtn) axisBtn.style.display = "none";
    const refreshBtn = document.getElementById(`timeline-refresh-${projectId}`);
    if (refreshBtn) refreshBtn.onclick = () => loadTimeline2(projectId);
  }
  function _renderTimelineLog2(projectId, data) {
    const wrap = document.getElementById(`timeline-wrap-${projectId}`);
    if (!wrap) return;
    const { tasks = [], goal_events = [] } = data || {};
    const isAbs = !!(state.panels[projectId] && state.panels[projectId]._timelineAbsolute);
    const fmtTs = (ts) => {
      if (!ts) return "";
      const iso = ts.includes("T") ? ts : ts.replace(" ", "T") + "Z";
      return isAbs ? new Date(iso).toISOString().replace("T", " ").slice(0, 16) : formatRelativeTime(ts);
    };
    const events = [];
    tasks.forEach((t3) => {
      const icon = { done: "\u2705", failed: "\u274C" }[t3.status] || "\u2022";
      events.push({ ts: t3.created_at, actor: t3.session_name || "(unknown)", desc: `${icon} ${(t3.description || "").slice(0, 100)}` });
    });
    const goalByField = /* @__PURE__ */ new Map();
    goal_events.forEach((g2) => {
      const key = g2.field + (g2.updated_at || "").slice(0, 13);
      if (!goalByField.has(key) || g2.version > (goalByField.get(key).version || 0)) goalByField.set(key, g2);
    });
    goalByField.forEach((g2) => events.push({ ts: g2.updated_at || "", actor: "goal", desc: `\u{1F4CB} ${g2.field} \u2192 v${g2.version}` }));
    events.sort((a3, b2) => (b2.ts || "").localeCompare(a3.ts || ""));
    wrap.innerHTML = `<div class="timeline-log">${events.map(
      (e3) => `<div class="timeline-log-entry"><span class="timeline-log-ts">${escapeHtml(fmtTs(e3.ts))}</span><span class="timeline-log-actor">${escapeHtml(e3.actor)}</span><span class="timeline-log-desc">${escapeHtml(e3.desc)}</span></div>`
    ).join("")}</div>`;
  }
  var _TOOL_CATEGORIES = {
    goal: ["set_goal", "get_goal", "set_north_star", "set_sprint"],
    task: [
      "log_task",
      "complete_task",
      "fail_sprint_item",
      "add_sprint_item",
      "get_tasks",
      "complete_sprint_item",
      "skip_sprint_item",
      "push_sprint_item",
      "get_sprint_items"
    ],
    session: [
      "start_session",
      "register_session",
      "get_sessions",
      "start_worker_session",
      "heartbeat",
      "get_context_block",
      "claim_task",
      "release_task",
      "enqueue_claude_task"
    ],
    hitl: ["request_hitl", "get_hitl_request"],
    notes: ["add_note", "get_notes", "delete_note"],
    decisions: ["pin_decision", "get_pinned_decisions", "set_decision", "update_decision"],
    project: ["create_project", "list_projects", "get_project_by_name", "generate_handoff"]
  };
  var _CATEGORY_LABELS = {
    goal: "Goal Tools",
    task: "Task & Sprint Tools",
    session: "Session Tools",
    hitl: "HITL Tools",
    notes: "Notes Tools",
    decisions: "Decision Tools",
    project: "Project Tools"
  };
  async function loadDocsTab(projectId) {
    const body = document.getElementById(`docs-body-${projectId}`);
    if (!body) return;
    if (body.dataset.loaded) return;
    body.dataset.loaded = "1";
    try {
      const tools = await api("/tools");
      if (!tools || !tools.length) {
        body.innerHTML = '<div class="empty" style="color:var(--muted)">No tools returned.</div>';
        return;
      }
      const _catLabels = { ..._CATEGORY_LABELS, other: "Other" };
      const html = _renderToolSections(tools, _TOOL_CATEGORIES, _catLabels);
      const _toolSearch = `<div style="position:sticky;top:0;background:var(--surface-1,#10131a);padding:0 0 8px;margin-bottom:6px;z-index:2"><input type="text" id="docs-search-${projectId}" placeholder="Search tools by name or description\u2026" style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px;font-family:var(--font-mono);padding:5px 9px;outline:none"></div>`;
      body.innerHTML = _toolSearch + html;
      _wireTabSearch(`docs-search-${projectId}`, `docs-body-${projectId}`, ".tool-entry");
    } catch (e3) {
      body.innerHTML = `<div style="color:var(--error)">Failed to load tools: ${escapeHtml(String(e3))}</div>`;
    }
  }
  async function _initCodeIntelTabVisibility(projectId) {
    if (!window.MERIDIAN_HOSTED) return;
    try {
      const data = await api("/tunnel/plugins");
      const btn = document.getElementById(`vtab-codeintel-${projectId}`);
      if (!btn) return;
      const isActive = !!(data && data.active && data.active.code);
      btn.style.display = isActive ? "" : "none";
    } catch (_2) {
    }
  }
  function _repoPathToProject(repoPath) {
    return String(repoPath || "").replace(/[\\/:]+/g, "-").replace(/^-+|-+$/g, "");
  }
  function _codeArchSection(archText) {
    const rawPre = (t3) => `<pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(t3 || "(no architecture returned)")}</pre>`;
    let arch = null;
    try {
      arch = JSON.parse(archText);
    } catch (_2) {
      arch = null;
    }
    if (!arch || typeof arch !== "object") return { html: rawPre(archText), charts: [] };
    const arr = (v3) => Array.isArray(v3) ? v3 : [];
    const num = (v3) => typeof v3 === "number" ? v3 : parseFloat(v3);
    const fin = (v3) => Number.isFinite(num(v3));
    const charts = [];
    let html = "";
    const nodes = arr(arch.node_labels).filter((d3) => d3 && d3.label != null && fin(d3.count));
    if (nodes.length) {
      html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Node types</div><canvas id="ci-nodes" height="120"></canvas></div>`;
      charts.push({ id: "ci-nodes", config: {
        type: "bar",
        data: { labels: nodes.map((d3) => String(d3.label)), datasets: [{ data: nodes.map((d3) => num(d3.count)), backgroundColor: "rgba(96,165,250,0.7)", borderRadius: 2 }] },
        options: { responsive: true, plugins: { legend: { display: false } }, scales: {
          x: { ticks: { color: "#9ca3af", font: { size: 9 }, maxRotation: 45 }, grid: { color: "#1f2937" } },
          y: { beginAtZero: true, ticks: { color: "#9ca3af", font: { size: 9 } }, grid: { color: "#1f2937" } }
        } }
      } });
    }
    const edges = arr(arch.edge_types).filter((d3) => d3 && d3.type != null && fin(d3.count)).sort((a3, b2) => num(b2.count) - num(a3.count)).slice(0, 6);
    if (edges.length) {
      const palette = ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#f472b6"];
      html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Top edge types</div><div style="max-width:230px;margin:0 auto"><canvas id="ci-edges" height="200"></canvas></div></div>`;
      charts.push({ id: "ci-edges", config: {
        type: "doughnut",
        data: { labels: edges.map((d3) => String(d3.type)), datasets: [{ data: edges.map((d3) => num(d3.count)), backgroundColor: palette, borderWidth: 0 }] },
        options: { responsive: true, plugins: { legend: { position: "right", labels: { color: "#9ca3af", font: { size: 9 }, boxWidth: 10 } } } }
      } });
    }
    const hot = arr(arch.hotspots).filter((d3) => d3 && d3.name != null && fin(d3.fan_in)).sort((a3, b2) => num(b2.fan_in) - num(a3.fan_in)).slice(0, 10);
    if (hot.length) {
      const maxFan = Math.max(...hot.map((d3) => num(d3.fan_in)), 1);
      html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Hotspots (fan-in)</div>`;
      for (const h3 of hot) {
        const pct = Math.round(num(h3.fan_in) / maxFan * 100);
        html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <div style="flex:1;min-width:0;font-size:10px;color:var(--text);font-family:var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(String(h3.name))}">${escapeHtml(String(h3.name))}</div>
        <div style="flex:1;background:var(--surface-1);border-radius:3px;height:10px;overflow:hidden"><div style="width:${pct}%;height:100%;background:var(--accent)"></div></div>
        <div style="width:30px;text-align:right;font-size:10px;color:var(--muted)">${num(h3.fan_in)}</div>
      </div>`;
      }
      html += `</div>`;
    }
    const pkgs = arr(arch.packages).filter((d3) => d3 && d3.name != null && fin(d3.node_count)).sort((a3, b2) => num(b2.node_count) - num(a3.node_count)).slice(0, 15);
    if (pkgs.length) {
      html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Packages</div><table style="width:100%;border-collapse:collapse;font-size:10px">`;
      for (const pk of pkgs) {
        html += `<tr style="border-bottom:1px solid var(--border)"><td style="padding:3px 6px;color:var(--text);font-family:var(--font-mono)">${escapeHtml(String(pk.name))}</td><td style="padding:3px 6px;text-align:right;color:var(--muted)">${num(pk.node_count)}</td></tr>`;
      }
      html += `</table></div>`;
    }
    const layers = arr(arch.layers).filter((d3) => d3 && d3.name != null);
    if (layers.length) {
      const sorted = [...layers].sort((a3, b2) => a3.layer > b2.layer ? -1 : a3.layer < b2.layer ? 1 : 0);
      html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Layers</div><div style="display:flex;flex-direction:column;gap:4px">`;
      for (const ly of sorted) {
        html += `<div style="padding:6px 10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;font-size:10px;color:var(--text);display:flex;justify-content:space-between">
        <span style="font-family:var(--font-mono)">${escapeHtml(String(ly.name))}</span>
        <span style="color:var(--muted)">layer ${escapeHtml(String(ly.layer))}</span>
      </div>`;
      }
      html += `</div></div>`;
    }
    if (!html) return { html: rawPre(archText), charts: [] };
    html += `<details style="margin-top:4px"><summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--accent)">&#9656; raw JSON</summary>${rawPre(archText)}</details>`;
    return { html, charts };
  }
  function _normalizeGraphEdges(queryResult) {
    let rows = [];
    try {
      const txt = (queryResult && queryResult.content || []).map((c3) => c3.text || "").join("").trim();
      if (!txt) return [];
      const obj = JSON.parse(txt);
      rows = Array.isArray(obj) ? obj : obj.rows || obj.data || obj.results || obj.records || [];
    } catch (_2) {
      return [];
    }
    const edges = [];
    for (const row of Array.isArray(rows) ? rows : []) {
      if (Array.isArray(row) && row.length >= 2) {
        edges.push({ source: String(row[0]), target: String(row[1]), value: Number(row[2]) || 1 });
      } else if (row && typeof row === "object") {
        const src = row.source ?? row.from ?? row["a.package"] ?? row.a;
        const tgt = row.target ?? row.to ?? row["b.package"] ?? row.b;
        const val = row.value ?? row.count ?? row["count(r)"] ?? row.weight ?? 1;
        if (src != null && tgt != null) {
          edges.push({ source: String(src), target: String(tgt), value: Number(val) || 1 });
        }
      }
    }
    return edges;
  }
  if (typeof window !== "undefined") window._normalizeGraphEdges = _normalizeGraphEdges;
  function _resolvePackageLayers(packages, layers) {
    const pkgs = Array.isArray(packages) ? packages : [];
    const lys = Array.isArray(layers) ? layers : [];
    if (!pkgs.length || !lys.length) return pkgs;
    if (pkgs.some((p3) => p3 && p3.layer != null)) return pkgs;
    const memberToLayer = {};
    const layerNames = [];
    for (const ly of lys) {
      if (!ly || ly.name == null) continue;
      const rank = ly.layer != null ? ly.layer : ly.name;
      layerNames.push({ name: String(ly.name), layer: rank });
      const members = ly.members || ly.packages || ly.package_names || ly.modules;
      if (Array.isArray(members)) {
        for (const m3 of members) {
          const key = String(typeof m3 === "string" ? m3 : m3 && m3.name || "").trim();
          if (key) memberToLayer[key] = rank;
        }
      }
    }
    layerNames.sort((a3, b2) => b2.name.length - a3.name.length);
    const resolve = (name) => {
      if (name in memberToLayer) return memberToLayer[name];
      for (const ly of layerNames) {
        if (name === ly.name || name.startsWith(ly.name + ".") || name.startsWith(ly.name + "/")) {
          return ly.layer;
        }
      }
      return null;
    };
    let matched = false;
    const out = pkgs.map((p3) => {
      if (!p3 || p3.name == null) return p3;
      const lyr = resolve(String(p3.name));
      if (lyr == null) return p3;
      matched = true;
      return { ...p3, layer: lyr };
    });
    return matched ? out : pkgs;
  }
  if (typeof window !== "undefined") window._resolvePackageLayers = _resolvePackageLayers;
  function _buildCodebaseForceGraph(packages, edges, view) {
    const pkgs = (packages || []).filter((p3) => p3 && p3.name != null);
    if (!pkgs.length) return null;
    const layers = [...new Set(pkgs.map((p3) => String(p3.layer != null ? p3.layer : "other")))];
    const palette = ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#f472b6", "#22d3ee"];
    const layerColor = {};
    layers.forEach((l3, i3) => {
      layerColor[l3] = palette[i3 % palette.length];
    });
    const degree = {};
    (edges || []).forEach((e3) => {
      if (!e3) return;
      degree[e3.source] = (degree[e3.source] || 0) + (Number(e3.value) || 1);
      degree[e3.target] = (degree[e3.target] || 0) + (Number(e3.value) || 1);
    });
    const metric = (p3) => view === "hotspots" ? degree[String(p3.name)] || 0 : Number(p3.node_count) || 0;
    const maxMetric = Math.max(1, ...pkgs.map(metric));
    const nodes = pkgs.map((p3) => {
      const lyr = String(p3.layer != null ? p3.layer : "other");
      return {
        name: String(p3.name),
        value: metric(p3),
        symbolSize: 8 + 42 * (metric(p3) / maxMetric),
        category: layers.indexOf(lyr),
        itemStyle: { color: layerColor[lyr] }
      };
    });
    const names = new Set(nodes.map((n2) => n2.name));
    const links = (edges || []).filter((e3) => e3 && names.has(e3.source) && names.has(e3.target) && e3.source !== e3.target).map((e3) => ({
      source: e3.source,
      target: e3.target,
      value: Number(e3.value) || 1,
      lineStyle: { width: Math.min(6, 1 + (Number(e3.value) || 1) / 3) }
    }));
    return {
      tooltip: { confine: true },
      legend: [{ data: layers, textStyle: { color: "#9ca3af", fontSize: 9 }, top: 0 }],
      series: [{
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        categories: layers.map((l3) => ({ name: l3 })),
        label: { show: true, fontSize: 9, color: "#e5e7eb", position: "right" },
        force: { repulsion: 140, edgeLength: 90, gravity: 0.08 },
        data: nodes,
        links,
        emphasis: { focus: "adjacency" },
        lineStyle: { color: "source", opacity: 0.5, curveness: 0.1 }
      }]
    };
  }
  if (typeof window !== "undefined") window._buildCodebaseForceGraph = _buildCodebaseForceGraph;
  function _renderCodebaseGraph(containerId, packages, edges) {
    if (typeof window === "undefined" || !window.echarts) return;
    const el2 = document.getElementById(containerId);
    if (!el2) return;
    const opt = _buildCodebaseForceGraph(packages, edges, "packages");
    if (!opt) return;
    let chart;
    try {
      chart = window.echarts.init(el2);
    } catch (_2) {
      return;
    }
    chart.setOption(opt);
    const setView = (view) => {
      const o3 = _buildCodebaseForceGraph(packages, edges, view);
      if (o3) chart.setOption(o3, true);
      document.querySelectorAll(`[data-cg-toggle][data-cg-for="${containerId}"]`).forEach((b2) => {
        const on = b2.getAttribute("data-cg-toggle") === view;
        b2.style.color = on ? "var(--text)" : "var(--muted)";
        b2.style.borderColor = on ? "var(--accent)" : "var(--border)";
      });
    };
    document.querySelectorAll(`[data-cg-toggle][data-cg-for="${containerId}"]`).forEach((b2) => {
      b2.onclick = () => setView(b2.getAttribute("data-cg-toggle"));
    });
    setView("packages");
  }
  if (typeof window !== "undefined") window._renderCodebaseGraph = _renderCodebaseGraph;
  async function _generateCodebaseMap(projectId) {
    const data = (window._codeGraphData || {})[projectId];
    const out = data && document.getElementById(`${data.cgId}-map`);
    if (!data || !out) return;
    if (!(data.packages || []).length) {
      out.innerHTML = `<div style="font-size:10px;color:var(--muted)">No packages to map yet \u2014 index the repo first.</div>`;
      return;
    }
    out.innerHTML = `<div style="font-size:10px;color:var(--muted)">rendering map\u2026</div>`;
    try {
      const r3 = await fetch(`/projects/${encodeURIComponent(projectId)}/codebase-map`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ packages: data.packages, edges: data.edges, hotspots: false })
      });
      if (r3.status === 503) {
        const body2 = await r3.json().catch(() => ({}));
        out.innerHTML = `<div style="font-size:10px;color:#e0b400;border:1px solid #c79a00;border-radius:4px;padding:6px 8px;background:rgba(199,154,0,0.10)">&#9888; ${escapeHtml(body2.message || "Graphviz is not installed on the server.")}</div>`;
        return;
      }
      if (!r3.ok) throw new Error(`HTTP ${r3.status}`);
      const body = await r3.json();
      if (body.image) {
        out.innerHTML = `<img src="${body.image}" alt="Codebase package map" style="max-width:100%;border:1px solid var(--border);border-radius:4px;background:#0b0e14">`;
      } else {
        out.innerHTML = `<div style="font-size:10px;color:var(--error)">No image returned.</div>`;
      }
    } catch (e3) {
      out.innerHTML = `<div style="font-size:10px;color:var(--error)">Map generation failed: ${escapeHtml(String(e3))}</div>`;
    }
  }
  if (typeof window !== "undefined") window._generateCodebaseMap = _generateCodebaseMap;
  async function loadInsightsTab(projectId) {
    const body = document.getElementById(`insights-body-${projectId}`);
    if (!body) return;
    body.innerHTML = '<div class="empty" style="color:var(--muted)">loading\u2026</div>';
    let insights = [];
    try {
      insights = await api(`/projects/${projectId}/insights`) || [];
    } catch (e3) {
      body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load insights: ${escapeHtml(String(e3))}</div>`;
      return;
    }
    const HORIZONS = [
      { key: "permanent", label: "PERMANENT", color: "var(--accent)" },
      { key: "year", label: "YEAR", color: "var(--warning, #d29922)" },
      { key: "quarter", label: "QUARTER", color: "var(--muted)" }
    ];
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="font-size:11px;color:var(--text)"><b>${insights.length}</b> insight${insights.length === 1 ? "" : "s"}</div>
  </div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:10px">Durable strategic understanding \u2014 separate from decisions (choices) and notes (reference). Add via the <code>add_insight</code> MCP tool. Permanent insights always appear in the planning brief.</div>`;
    if (!insights.length) {
      html += `<div class="empty" style="color:var(--muted);padding:8px 0">No insights yet. Capture accumulated understanding with <code>add_insight(project_id, title, body, horizon)</code>.</div>`;
    } else {
      const buckets = { permanent: [], year: [], quarter: [] };
      for (const ins of insights) {
        const h3 = String(ins.horizon || "quarter").toLowerCase();
        (buckets[h3] || buckets.quarter).push(ins);
      }
      const _renderCard = (ins) => {
        const tags = String(ins.tags || "").split(",").map((t3) => t3.trim()).filter(Boolean);
        return `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
        <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(String(ins.title || ""))}</div>
        ${ins.body ? `<div style="font-size:10px;color:var(--muted);margin-top:4px;white-space:pre-wrap">${escapeHtml(String(ins.body))}</div>` : ""}
        ${tags.length ? `<div style="margin-top:4px;display:flex;gap:3px;flex-wrap:wrap">${tags.map((t3) => `<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">#${escapeHtml(t3)}</span>`).join("")}</div>` : ""}
      </div>`;
      };
      for (const hz of HORIZONS) {
        const items = buckets[hz.key];
        if (!items.length) continue;
        html += `<div class="insights-horizon-section" data-horizon="${hz.key}" style="display:flex;align-items:center;gap:6px;margin:12px 0 6px;padding-bottom:3px;border-bottom:1px solid ${hz.color}">
        <span style="font-size:9px;font-weight:700;letter-spacing:.05em;color:${hz.color}">${hz.label}</span>
        <span style="font-size:9px;color:var(--muted)">${items.length}</span>
      </div>`;
        for (const ins of items) html += _renderCard(ins);
      }
    }
    body.innerHTML = html;
  }
  async function loadSessionTimelineTab(projectId) {
    const body = document.getElementById(`sessions-body-${projectId}`);
    if (!body) return;
    body.innerHTML = '<div class="empty" style="color:var(--muted)">loading\u2026</div>';
    let data = null;
    try {
      data = await api(`/projects/${projectId}/session-timeline`);
    } catch (e3) {
      body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load session timeline: ${escapeHtml(String(e3))}</div>`;
      return;
    }
    const sessions = data && data.sessions || [];
    const OUTCOME = {
      "done": { label: "DONE", color: "var(--accent)" },
      "failed": { label: "FAILED", color: "var(--status-failed, #f85149)" },
      "stopped-ambiguously": { label: "STOPPED?", color: "var(--warning, #d29922)" },
      "in_progress": { label: "RUNNING", color: "var(--muted)" }
    };
    const _pill = (o3) => {
      const m3 = OUTCOME[String(o3)] || { label: String(o3 || "").toUpperCase(), color: "var(--muted)" };
      return `<span style="font-size:8px;padding:1px 5px;border-radius:3px;border:1px solid ${m3.color};color:${m3.color};letter-spacing:.04em" title="${escapeHtml(String(o3))}">${m3.label}</span>`;
    };
    let html = `<div style="font-size:9px;color:var(--muted);margin-bottom:10px">Per executor session: start/end + the sprint items it worked, grouped by item group. <b>STOPPED?</b> = the session ended while it still had an item claimed (a silent stop) \u2014 distinct from <b>FAILED</b> (the item actively errored).</div>`;
    if (!sessions.length) {
      html += `<div class="empty" id="session-timeline-empty" style="color:var(--muted);padding:8px 0">No executor sessions yet.</div>`;
    } else {
      for (const s3 of sessions) {
        const when = s3.ended_at ? `${escapeHtml(String(s3.started_at || ""))} \u2192 ${escapeHtml(String(s3.ended_at))}` : `${escapeHtml(String(s3.started_at || ""))} \u2192 (${escapeHtml(String(s3.status || "active"))})`;
        html += `<div class="session-timeline-row" style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(String(s3.name || s3.id || ""))}</span>
          <span style="font-size:8px;color:var(--muted)">${s3.item_count || 0} item${s3.item_count === 1 ? "" : "s"}</span>
        </div>
        <div style="font-size:9px;color:var(--muted);margin-top:2px">${when}</div>`;
        const groups = s3.groups || [];
        if (!groups.length) {
          html += `<div style="font-size:9px;color:var(--muted);margin-top:6px">no sprint items attributed</div>`;
        } else {
          for (const g2 of groups) {
            html += `<div style="margin-top:6px"><div style="font-size:8px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:2px">${escapeHtml(String(g2.item_group || ""))}</div>`;
            for (const it of g2.items || []) {
              html += `<div style="display:flex;gap:6px;align-items:center;justify-content:space-between;padding:2px 0">
              <span style="font-size:10px;color:var(--text)">${escapeHtml(String(it.nickname || it.title || ""))}</span>
              ${_pill(it.outcome)}
            </div>`;
            }
            html += `</div>`;
          }
        }
        html += `</div>`;
      }
    }
    body.innerHTML = html;
  }
  async function loadBlogTab(projectId) {
    const body = document.getElementById(`blog-body-${projectId}`);
    if (!body) return;
    body.innerHTML = '<div class="empty" style="color:var(--muted)">loading\u2026</div>';
    let posts = [];
    try {
      posts = await api("/workspace/blog") || [];
    } catch (e3) {
      body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load blog posts: ${escapeHtml(String(e3))}</div>`;
      return;
    }
    const GROUPS = [
      { key: "draft", label: "DRAFTS", color: "var(--muted)" },
      { key: "published", label: "PUBLISHED", color: "var(--accent)" },
      { key: "archived", label: "ARCHIVED", color: "var(--warning, #d29922)" }
    ];
    const pid = String(projectId);
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="font-size:11px;color:var(--text)"><b>${posts.length}</b> post${posts.length === 1 ? "" : "s"}</div>
  </div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:10px">Workspace-scoped blog. Edit a draft below (or author via the <code>save_blog_post</code> MCP tool); published posts are live at <code>/blog/&lt;slug&gt;</code>.</div>`;
    html += blogEditorFormHtml(pid, null);
    if (!posts.length) {
      html += `<div class="empty" style="color:var(--muted);padding:8px 0">No posts yet. Create one above, or with <code>save_blog_post(title, body, status="published")</code>.</div>`;
    } else {
      for (const g2 of GROUPS) {
        const inGroup = posts.filter((p3) => String(p3.status || "draft") === g2.key);
        if (!inGroup.length) continue;
        html += `<div style="font-size:9px;color:${g2.color};letter-spacing:.06em;margin:12px 0 6px">${g2.label} \xB7 ${inGroup.length}</div>`;
        for (const p3 of inGroup) {
          html += blogPostCardHtml(p3);
        }
      }
    }
    body.innerHTML = html;
    const byId = (suffix) => document.getElementById(`blog-editor-${suffix}-${pid}`);
    const saveBtn = byId("save");
    const resetBtn = byId("reset");
    const msgEl = document.getElementById(`blog-editor-status-msg-${pid}`);
    const doSave = async () => {
      const idEl = document.getElementById(`blog-editor-id-${pid}`);
      const titleEl = document.getElementById(`blog-editor-title-input-${pid}`);
      const bodyEl = document.getElementById(`blog-editor-body-${pid}`);
      const statusEl = document.getElementById(`blog-editor-status-${pid}`);
      const title = (titleEl?.value || "").trim();
      if (!title) {
        if (msgEl) msgEl.textContent = "Title required";
        return;
      }
      if (saveBtn) saveBtn.disabled = true;
      if (msgEl) msgEl.textContent = "Saving\u2026";
      try {
        await api("/workspace/blog", {
          method: "POST",
          body: JSON.stringify({
            id: idEl?.value || void 0,
            // present ⇒ update in place (upsert by id)
            title,
            body: bodyEl?.value || "",
            status: statusEl?.value || "draft"
          })
        });
        loadBlogTab(projectId);
      } catch (e3) {
        if (saveBtn) saveBtn.disabled = false;
        if (msgEl) msgEl.textContent = `Error: ${escapeHtml(String(e3))}`;
      }
    };
    if (saveBtn) saveBtn.onclick = doSave;
    if (resetBtn) resetBtn.onclick = () => resetBlogEditor(pid);
    body.querySelectorAll(".blog-edit-btn").forEach((btn) => {
      btn.onclick = () => {
        const bid = btn.getAttribute("data-blog-id") || "";
        const post = posts.find((p3) => String(p3.id || "") === bid);
        if (!post) return;
        populateBlogEditor(pid, post);
        document.getElementById(`blog-editor-${pid}`)?.scrollIntoView({ block: "nearest" });
        document.getElementById(`blog-editor-title-input-${pid}`)?.focus();
      };
    });
  }
  async function loadDocumentsTab(projectId) {
    const body = document.getElementById(`documents-body-${projectId}`);
    if (!body) return;
    body.innerHTML = '<div class="empty" style="color:var(--muted)">loading\u2026</div>';
    let docs = [];
    try {
      const dp = await api(`/projects/${projectId}/notes?paginate=true&limit=200`);
      docs = (dp && dp.notes || []).filter((n2) => String(n2.note_kind || "").toLowerCase() === "document");
    } catch (e3) {
      body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load documents: ${escapeHtml(String(e3))}</div>`;
      return;
    }
    let peeks = [];
    try {
      const pk = await api("/document-peeks");
      peeks = pk && pk.peeks || [];
    } catch (_2) {
      peeks = [];
    }
    const _srcBadge = (src) => {
      const s3 = String(src || "local").toLowerCase();
      const label = s3.includes("onedrive") ? "OneDrive" : s3.includes("gdrive") || s3.includes("google") ? "GDrive" : src || "local";
      return `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">${escapeHtml(String(label))}</span>`;
    };
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div style="font-size:11px;color:var(--text)"><b>${docs.length}</b> document${docs.length === 1 ? "" : "s"} <span style="color:var(--muted)">ingested</span></div>
  </div>`;
    html += `<div style="display:flex;gap:6px;align-items:center;margin-bottom:12px;padding:8px 10px;border:1px dashed var(--border);border-radius:4px;background:var(--surface-1)">
    <input type="file" id="doc-upload-input-${escapeHtml(String(projectId))}" accept=".txt,.md" style="font-size:10px;flex:1;min-width:0" />
    <button id="doc-upload-btn-${escapeHtml(String(projectId))}" class="secondary" style="font-size:10px;padding:3px 10px" disabled>Upload .txt/.md</button>
  </div>
  <div id="doc-upload-status-${escapeHtml(String(projectId))}" style="font-size:9px;color:var(--muted);margin-bottom:4px">Plain .txt / .md only \u2014 read in your browser, stored as a searchable document note.</div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:10px;padding:6px 8px;border-left:2px solid var(--accent);background:var(--surface-1)">\u{1F4C4} <b>Word / PDF?</b> Ingest it with the <code>ingest_document</code> MCP tool (<code>file_path</code> or <code>content</code>) \u2014 it saves as a searchable <code>kind=document</code> note and appears above. A <code>get_document_structure</code> peek only reads the outline; it does <b>not</b> save the doc (those show under \u201CRecently viewed\u201D below).</div>`;
    if (!docs.length) {
      html += `<div class="empty" style="color:var(--muted);padding:8px 0">No documents ingested yet. Upload a .txt/.md above, or ingest a Word/PDF doc with the <code>ingest_document</code> MCP tool (file_path or content) \u2014 it is stored as a project note with kind=document and appears here.</div>`;
    } else {
      for (const d3 of docs) {
        const title = d3.title || d3.slug || d3.id;
        const fp = d3.file_path || "";
        const tags = Array.isArray(d3.tags) ? d3.tags : [];
        const did = String(d3.id);
        html += `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(String(title))}</span>
          ${_srcBadge(d3.source)}
        </div>
        ${fp ? `<div style="font-size:9px;color:var(--muted);font-family:var(--font-mono);margin-top:3px;word-break:break-all">${escapeHtml(String(fp))}</div>` : ""}
        ${tags.length ? `<div style="margin-top:4px;display:flex;gap:3px;flex-wrap:wrap">${tags.map((t3) => `<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">#${escapeHtml(String(t3))}</span>`).join("")}</div>` : ""}
        ${fp ? `<div style="margin-top:6px"><button class="doc-struct-btn" data-fp="${escapeHtml(String(fp))}" data-did="${escapeHtml(did)}" style="font-size:9px;padding:2px 8px">View structure</button></div><div id="doc-struct-${escapeHtml(did)}" style="margin-top:6px"></div>` : '<div style="font-size:9px;color:var(--muted);margin-top:4px">No server-side file_path \u2014 structure view unavailable.</div>'}
      </div>`;
      }
      html += `<div style="font-size:9px;color:var(--muted);margin-top:6px">Structure = heading tree + paragraph/heading counts (docs_intel Phase 1). Figures, cross-references, equations and comments are not yet extracted. Structure needs the file on the tunnel/self-host server.</div>`;
    }
    if (peeks.length) {
      html += `<div id="doc-peeks-section-${escapeHtml(String(projectId))}" style="margin-top:16px;border-top:1px dashed var(--border);padding-top:12px">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;font-weight:600;color:var(--accent)">Recently viewed (not saved) \u2014 ${peeks.length}</span>
        <span title="These outline peeks are recorded per workspace, not per project, so the same list shows in every project's Documents tab." style="font-size:8px;font-weight:600;padding:1px 5px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted);text-transform:uppercase;letter-spacing:0.4px">workspace-wide</span>
      </div>
      <div style="font-size:9px;color:var(--muted);margin:2px 0 8px">Peeked with <code>get_document_structure</code> (a stateless outline read) but never ingested \u2014 so they are NOT searchable here, and they are <b>not attached to this project</b> (this list is shared across every project in your workspace). Ingest one to save it as a document on <i>this</i> project.</div>`;
      for (const pk of peeks) {
        const fp = String(pk.file_path || "");
        const failed = pk.ok === false;
        html += `<div style="border:1px dashed var(--border);border-radius:4px;padding:6px 10px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <span style="font-size:9px;color:var(--muted);font-family:var(--font-mono);word-break:break-all">${escapeHtml(fp)}</span>
          <button class="doc-peek-ingest-btn" data-fp="${escapeHtml(fp)}" style="font-size:9px;padding:2px 8px;white-space:nowrap">Ingest this</button>
        </div>
        <div style="font-size:8px;color:var(--muted);margin-top:2px">viewed ${escapeHtml(String(pk.viewed_at || ""))}${failed ? " \xB7 peek failed (hosted \u2014 no file access)" : ""}</div>
      </div>`;
      }
      html += `</div>`;
    }
    body.innerHTML = html;
    body.querySelectorAll(".doc-peek-ingest-btn").forEach((el2) => {
      el2.addEventListener("click", () => {
        const fp = el2.getAttribute("data-fp") || "";
        const cmd = `ingest_document(file_path="${fp}")`;
        try {
          navigator.clipboard.writeText(cmd);
        } catch (_2) {
        }
        const prev = el2.textContent;
        el2.textContent = "Copied \u2713";
        setTimeout(() => {
          el2.textContent = prev || "Ingest this";
        }, 1500);
      });
    });
    const _fileInput = document.getElementById(`doc-upload-input-${projectId}`);
    const _uploadBtn = document.getElementById(`doc-upload-btn-${projectId}`);
    const _uploadStatus = document.getElementById(`doc-upload-status-${projectId}`);
    const _setStatus = (msg, color) => {
      if (_uploadStatus) {
        _uploadStatus.textContent = msg;
        _uploadStatus.style.color = color;
      }
    };
    if (_fileInput && _uploadBtn) {
      _fileInput.addEventListener("change", () => {
        _uploadBtn.disabled = !(_fileInput.files && _fileInput.files.length > 0);
      });
      _uploadBtn.addEventListener("click", async () => {
        const file = _fileInput.files && _fileInput.files[0];
        if (!file) return;
        const name = file.name || "";
        if (!/\.(txt|md)$/i.test(name)) {
          _setStatus("Only .txt and .md files are supported.", "var(--error)");
          return;
        }
        _uploadBtn.disabled = true;
        _setStatus(`Reading ${name}\u2026`, "var(--muted)");
        try {
          const text = await file.text();
          const res = await api(`/projects/${projectId}/documents/upload`, {
            method: "POST",
            body: JSON.stringify({ filename: name, content: text })
          });
          if (res && res.error) throw new Error(String(res.error));
          _setStatus(`Uploaded "${name}".`, "var(--accent)");
          await loadDocumentsTab(projectId);
        } catch (e3) {
          let msg = String(e3 && e3.message ? e3.message : e3);
          try {
            const rt = e3 && e3.responseText;
            if (rt) {
              const j3 = JSON.parse(rt);
              if (j3 && j3.detail) msg = String(j3.detail);
            }
          } catch (_2) {
          }
          _setStatus(`Upload failed: ${msg}`, "var(--error)");
          _uploadBtn.disabled = false;
        }
      });
    }
    body.querySelectorAll(".doc-struct-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const fp = btn.dataset.fp || "";
        const did = btn.dataset.did || "";
        const target = document.getElementById(`doc-struct-${did}`);
        if (!target) return;
        target.innerHTML = '<span style="font-size:9px;color:var(--muted)">loading structure\u2026</span>';
        try {
          const st = await api(`/projects/${projectId}/document-structure?path=${encodeURIComponent(fp)}`);
          if (st && st.error) {
            target.innerHTML = `<span style="font-size:9px;color:var(--warning,#d29922)">${escapeHtml(String(st.error))}</span>`;
            return;
          }
          const headings = st && st.headings || [];
          const tree = headings.map((h3) => {
            const lvl = Math.max(1, Math.min(6, parseInt(h3.level, 10) || 1));
            return `<div style="font-size:10px;color:var(--text);padding-left:${(lvl - 1) * 12}px">${escapeHtml(String(h3.text || ""))}</div>`;
          }).join("");
          target.innerHTML = `<div style="font-size:9px;color:var(--muted);margin-bottom:3px">${st.paragraph_count} paragraphs \xB7 ${st.heading_count} headings</div>${tree || '<span style="font-size:9px;color:var(--muted)">No headings found.</span>'}`;
        } catch (e3) {
          target.innerHTML = `<span style="font-size:9px;color:var(--error)">Failed: ${escapeHtml(String(e3))}</span>`;
        }
      });
    });
  }
  async function loadCodeIntelTab(projectId) {
    const body = document.getElementById(`codeintel-body-${projectId}`);
    if (!body) return;
    body.innerHTML = '<div class="empty" style="color:var(--muted)">loading\u2026</div>';
    try {
      const [pluginsData, meData, settingsData] = await Promise.all([
        api("/tunnel/plugins"),
        api("/me"),
        loadProjectSettings2(projectId)
      ]);
      if (!pluginsData?.active?.code) {
        body.innerHTML = '<div class="empty" style="color:var(--muted)">Code intel tunnel is not active. Run <code>meridian --tunnel</code> to connect it.</div>';
        return;
      }
      const tenantId = meData?.tenant_id;
      if (!tenantId) {
        body.innerHTML = '<div class="empty" style="color:var(--error)">Could not resolve tenant ID from /me.</div>';
        return;
      }
      const codeBase = `/code/mcp/${tenantId}/mcp`;
      async function _codeMcpCall(method, params) {
        const r3 = await fetch(codeBase, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: params || {} })
        });
        if (!r3.ok) throw new Error(`HTTP ${r3.status}`);
        const text = await r3.text();
        let parsed = null;
        if (text.trim().startsWith("{")) {
          parsed = JSON.parse(text);
        } else {
          for (const line of text.split("\n")) {
            if (line.startsWith("data:")) {
              try {
                parsed = JSON.parse(line.slice(5).trim());
              } catch (_2) {
              }
            }
          }
        }
        if (!parsed) throw new Error("empty response from code MCP");
        if (parsed.error) throw new Error(parsed.error.message || String(parsed.error));
        return parsed.result;
      }
      let toolCount = 0;
      try {
        const tlResult = await _codeMcpCall("tools/list", {});
        toolCount = (tlResult?.tools || []).length;
      } catch (_2) {
      }
      const execCfg = settingsData?.executor_config || {};
      const repoPaths = Array.isArray(execCfg.repo_paths) ? execCfg.repo_paths : [];
      let html = "";
      let archCharts = [];
      let _graphPackages = [];
      let _graphEdges = [];
      let _graphArch = null;
      let _archError = "";
      const _cgId = `ci-forcegraph-${projectId}`;
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block;flex-shrink:0"></span>
      <span style="font-size:11px;color:var(--text);font-weight:600">Code Intel Live</span>
      ${toolCount ? `<span style="font-size:10px;color:var(--muted)">${toolCount} tool${toolCount !== 1 ? "s" : ""}</span>` : ""}
    </div>`;
      let _docsCount = 0;
      try {
        const _dp = await api(`/projects/${projectId}/notes?paginate=true&limit=200`);
        _docsCount = (_dp && _dp.notes || []).filter((n2) => String(n2.note_kind || "").toLowerCase() === "document").length;
      } catch (_2) {
      }
      const _cwds = repoPaths.map((rp) => typeof rp === "string" ? rp : rp.cwd || "").filter(Boolean);
      html += `<div style="margin-bottom:16px;padding:10px 12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px">
      <div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Resources</div>
      <div style="font-size:11px;color:var(--text);line-height:1.7">
        <div>Codebases: <b>${_cwds.length}</b> repo path${_cwds.length !== 1 ? "s" : ""}${_cwds.length ? ` \u2014 ${escapeHtml(_cwds.join(", "))}` : ' <span style="color:var(--muted)">(add in Settings \u2192 Executor Config)</span>'}</div>
        <div>Documents: <b>${_docsCount}</b> ingested <span style="color:var(--muted)">(kind=document)</span></div>
      </div>
    </div>`;
      html += `<div style="margin-bottom:16px">
      <div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Code Tree</div>
      <div id="${_cgId}-codegraph"></div>
    </div>`;
      html += `<div style="margin-bottom:16px"><div id="${_cgId}-panel"></div></div>`;
      html += `<div style="margin-bottom:16px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)"><span style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em">Index Status</span><button id="${_cgId}-reindex" class="secondary" style="padding:2px 10px;font-size:10px" title="Re-run index_repository for each repo path (31d0caa6)">&#8635; Reindex</button></div>`;
      if (repoPaths.length) {
        for (const rp of repoPaths) {
          const cwd = typeof rp === "string" ? rp : rp.cwd || "";
          const hostname = typeof rp === "object" ? rp.hostname || "" : "";
          if (!cwd) continue;
          try {
            const result = await _codeMcpCall("tools/call", { name: "index_status", arguments: { project: _repoPathToProject(cwd) } });
            const text = (result?.content || []).map((c3) => c3.text || "").join("").trim();
            html += `<div style="margin-bottom:10px">
            <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:4px">${escapeHtml(cwd)}${hostname ? `<span style="color:var(--muted);font-weight:400"> \xB7 ${escapeHtml(hostname)}</span>` : ""}</div>
            <pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:8px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(text || "(no status returned)")}</pre>
          </div>`;
          } catch (e3) {
            html += `<div style="margin-bottom:10px">
            <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:4px">${escapeHtml(cwd)}</div>
            <div style="font-size:10px;color:var(--error)">index_status failed: ${escapeHtml(String(e3))}</div>
          </div>`;
          }
        }
      } else {
        html += `<div style="font-size:10px;color:var(--muted)">No repo paths configured. Add them in Settings \u2192 Executor Config to see index status.</div>`;
      }
      html += "</div>";
      html += `<div><div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Architecture Summary</div>`;
      try {
        const archPath = repoPaths.length ? typeof repoPaths[0] === "string" ? repoPaths[0] : repoPaths[0].cwd || "" : "";
        const archArgs = archPath ? { project: _repoPathToProject(archPath) } : {};
        const archResult = await _codeMcpCall("tools/call", { name: "get_architecture", arguments: archArgs });
        const archText = (archResult?.content || []).map((c3) => c3.text || "").join("").trim();
        const archSection = _codeArchSection(archText);
        html += archSection.html;
        archCharts = archSection.charts;
        try {
          const _arch = JSON.parse(archText);
          _graphArch = _arch;
          _graphPackages = _resolvePackageLayers(_arch?.packages, _arch?.layers);
        } catch (_2) {
          _graphPackages = [];
        }
        if (_graphPackages.length) {
          try {
            const _edgeRes = await _codeMcpCall("tools/call", { name: "query_graph", arguments: {
              ...archArgs,
              cypher: "MATCH (a)-[r]->(b) WHERE a.package <> b.package RETURN a.package, b.package, count(r)"
            } });
            _graphEdges = _normalizeGraphEdges(_edgeRes);
          } catch (_2) {
            _graphEdges = [];
          }
        }
      } catch (e3) {
        _archError = String(e3);
        html += `<div style="font-size:10px;color:var(--error)">get_architecture failed: ${escapeHtml(String(e3))}</div>`;
      }
      html += `<div style="margin-top:8px;display:flex;gap:6px">
      <button class="secondary" style="font-size:10px;padding:3px 10px" onclick="loadCodeIntelTab(${JSON.stringify(projectId)})">\u21BA Refresh</button>
    </div></div>`;
      body.innerHTML = html;
      if (window.Chart && archCharts.length) {
        for (const c3 of archCharts) {
          const el2 = document.getElementById(c3.id);
          if (el2) {
            try {
              new Chart(el2, c3.config);
            } catch (_2) {
            }
          }
        }
      }
      window._codeGraphData = window._codeGraphData || {};
      window._codeGraphData[projectId] = { packages: _graphPackages, edges: _graphEdges, cgId: _cgId };
      const _panelEl = document.getElementById(`${_cgId}-panel`);
      if (_panelEl) {
        mountCodeIntelPanel(_panelEl, {
          status: _archError ? "error" : "ready",
          error: _archError,
          architecture: _graphArch || { packages: _graphPackages },
          onGenerateMap: async () => {
            const r3 = await fetch(`/projects/${encodeURIComponent(projectId)}/codebase-map`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ packages: _graphPackages, edges: _graphEdges, hotspots: false })
            });
            if (r3.status === 503) {
              const b3 = await r3.json().catch(() => ({}));
              throw new Error(b3.message || "Graphviz is not installed on the server.");
            }
            if (!r3.ok) throw new Error(`HTTP ${r3.status}`);
            const b2 = await r3.json();
            if (!b2.image) throw new Error("No image returned.");
            return b2.image;
          }
        });
      }
      const _codegraphEl = document.getElementById(`${_cgId}-codegraph`);
      if (_codegraphEl) {
        const archArgs = repoPaths.length ? { project: _repoPathToProject(typeof repoPaths[0] === "string" ? repoPaths[0] : repoPaths[0].cwd || "") } : {};
        let _nodes = [];
        try {
          const _res = await _codeMcpCall("tools/call", {
            name: "search_graph",
            arguments: { ...archArgs, limit: 1e3, min_degree: 0 }
          });
          const _txt = (_res?.content || []).map((c3) => c3.text || "").join("").trim();
          if (_txt) {
            const _parsed = JSON.parse(_txt);
            const _rows = Array.isArray(_parsed) ? _parsed : _parsed.results || _parsed.nodes || _parsed.data || [];
            if (Array.isArray(_rows)) _nodes = _rows;
          }
        } catch (_2) {
          _nodes = [];
        }
        try {
          const _model = buildCodeGraphModel({
            architecture: _graphArch || { packages: _graphPackages },
            nodes: _nodes
          });
          renderCodeGraph(_codegraphEl, _model, {
            // LLM summary — SEPARATE, last-resort on-click action wired to the
            // existing code-intel get_code_snippet path. Only invoked on demand;
            // never part of the deterministic render.
            onRequestSummary: async (node) => {
              const qn = node.meta.qualifiedName;
              if (!qn) throw new Error("No qualified_name to summarize.");
              const _snip = await _codeMcpCall("tools/call", {
                name: "get_code_snippet",
                arguments: { ...archArgs, qualified_name: qn }
              });
              const _snipTxt = (_snip?.content || []).map((c3) => c3.text || "").join("").trim();
              return _snipTxt || "No snippet returned.";
            }
          });
        } catch (e3) {
          _codegraphEl.innerHTML = `<div style="font-size:10px;color:var(--error)">Code tree failed: ${escapeHtml(String(e3))}</div>`;
        }
      }
      const _reindexBtn = document.getElementById(`${_cgId}-reindex`);
      if (_reindexBtn) {
        _reindexBtn.addEventListener("click", async () => {
          _reindexBtn.disabled = true;
          _reindexBtn.textContent = "Reindexing\u2026";
          try {
            for (const rp of repoPaths) {
              const cwd = typeof rp === "string" ? rp : rp.cwd || "";
              if (cwd) await _codeMcpCall("tools/call", { name: "index_repository", arguments: { path: cwd } });
            }
            loadCodeIntelTab(projectId);
          } catch (e3) {
            _reindexBtn.disabled = false;
            _reindexBtn.textContent = "Reindex failed \u2014 retry";
          }
        });
      }
    } catch (e3) {
      body.innerHTML = `<div style="color:var(--error)">Failed to load code intel: ${escapeHtml(String(e3))}</div>`;
    }
  }
  function normalizeNotifyTarget(raw) {
    const v3 = (raw || "").trim();
    if (!v3) return "";
    if (v3.includes("://") || v3.includes("@") || v3.includes("/")) return v3;
    return `https://ntfy.sh/${v3}`;
  }
  function displayNotifyTarget2(raw) {
    const v3 = (raw || "").trim();
    if (!v3) return "";
    const lower = v3.toLowerCase();
    for (const prefix of ["https://ntfy.sh/", "http://ntfy.sh/", "ntfy.sh/"]) {
      if (lower.startsWith(prefix)) return v3.slice(prefix.length).replace(/\/+$/, "");
    }
    return v3;
  }
  function osExecutorHintBanner2(projectId) {
    try {
      if (localStorage.getItem("meridian.hooks.osbanner.dismissed") === "1") return "";
    } catch (e3) {
    }
    const ua = String(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || "").toLowerCase();
    const isWin = ua.includes("win");
    const msg = isWin ? "Windows detected \u2014 executors use <strong>PowerShell</strong>; run Python with <code>pixi run python</code>." : "Mac / Linux detected \u2014 executors use <strong>bash</strong>; run Python with <code>python3</code>.";
    return `<div data-os-hint style="display:flex;align-items:flex-start;gap:8px;background:var(--surface-1);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:4px;padding:8px 10px;margin-bottom:10px;font-size:10px;color:var(--text);line-height:1.5"><span style="flex:1">${msg}</span><button title="Dismiss" onclick="try{localStorage.setItem('meridian.hooks.osbanner.dismissed','1')}catch(e: any){}; var _b=this.closest('[data-os-hint]'); if(_b)_b.remove();" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;line-height:1;padding:0 2px;flex-shrink:0">\xD7</button></div>`;
  }
  function showFailoverBannerIfNeeded() {
    try {
      if (sessionStorage.getItem("meridian.failover.dismissed") === "1") return;
    } catch (e3) {
    }
    fetch("/failover-status").then((r3) => r3.ok ? r3.json() : null).then((data) => {
      if (!data || !data.is_failover) return;
      if (document.getElementById("failover-banner")) return;
      const bar = document.createElement("div");
      bar.id = "failover-banner";
      bar.style.cssText = "position:sticky;top:0;z-index:9999;background:#fef3c7;color:#92400e;font-size:12px;font-weight:600;padding:8px 14px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #f59e0b";
      const label = document.createElement("span");
      label.style.flex = "1";
      label.textContent = "\u26A0 Meridian is running in failover mode \u2014 some data may be read-only or delayed.";
      const btn = document.createElement("button");
      btn.textContent = "\xD7";
      btn.title = "Dismiss";
      btn.style.cssText = "background:none;border:none;color:#92400e;font-size:16px;font-weight:700;cursor:pointer;line-height:1;padding:0 4px";
      btn.onclick = () => {
        try {
          sessionStorage.setItem("meridian.failover.dismissed", "1");
        } catch (e3) {
        }
        bar.remove();
      };
      bar.appendChild(label);
      bar.appendChild(btn);
      document.body.insertBefore(bar, document.body.firstChild);
    }).catch(() => {
    });
  }
  function _removeHitlCard(id) {
    document.querySelectorAll(`[data-hitl-id="${id}"]`).forEach((el2) => {
      (el2.closest(".hitl-row") || el2).remove();
    });
  }
  window._removeHitlCard = _removeHitlCard;
  async function loadHitlTab(projectId) {
    const body = document.getElementById(`hitl-body-${projectId}`);
    const statusFilter = document.getElementById(`hitl-status-filter-${projectId}`);
    const refreshBtn = document.getElementById(`hitl-refresh-${projectId}`);
    if (!body) return;
    const urgencyColor = { blocking: "var(--red,#e05252)", high: "var(--yellow,#d4a017)", normal: "var(--muted)" };
    const statusBadge = { pending: "#f59e0b", answered: "#22c55e", dismissed: "var(--muted)" };
    const render = async () => {
      body.innerHTML = `<div class="empty" style="color:var(--muted)">loading\u2026</div>`;
      const status = statusFilter && statusFilter.value || "pending";
      const qs = status === "all" ? "?status=all" : `?status=${status}`;
      try {
        const rows = await api(`/projects/${projectId}/hitl${qs}&limit=50`);
        if (!rows || rows.length === 0) {
          body.innerHTML = `<div style="color:var(--muted);padding:12px;text-align:center;border:1px dashed var(--border);border-radius:4px">

          ${status === "pending" ? "No pending HITL requests \u2014 queue is clear \u2713" : "No items found"}

        </div>`;
          return;
        }
        const pending = rows.filter((r3) => r3.status === "pending");
        const resolved = rows.filter((r3) => r3.status !== "pending");
        const renderDiff = (diffText) => {
          const lines = String(diffText || "").split("\n").map((ln) => {
            let color = "var(--text)";
            if (ln.startsWith("+++") || ln.startsWith("---")) color = "var(--muted)";
            else if (ln.startsWith("+")) color = "#22c55e";
            else if (ln.startsWith("-")) color = "#e05252";
            else if (ln.startsWith("@@")) color = "#38bdf8";
            return `<span style="color:${color};display:block;white-space:pre-wrap">${escapeHtml(ln)}</span>`;
          });
          return `<pre style="margin-top:8px;padding:8px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;font-size:10px;font-family:var(--font-mono);max-height:260px;overflow:auto">${lines.join("")}</pre>`;
        };
        const renderCard = (r3) => {
          const urg = r3.urgency || "normal";
          const st = r3.status || "pending";
          const dt = (r3.created_at || "").slice(0, 16).replace("T", " ");
          const isMd = r3.kind === "md_section_update";
          let pl = null;
          if (isMd && r3.payload) {
            try {
              pl = JSON.parse(r3.payload);
            } catch (e3) {
              pl = null;
            }
          }
          const mdMeta = isMd && pl ? `<div style="margin-top:6px;font-size:10px;color:var(--accent)"><b>${escapeHtml(pl.file || "")}</b> \xA7 ${escapeHtml(pl.anchor || "")}</div>` : "";
          const diffHtml = isMd && pl && pl.diff ? renderDiff(pl.diff) : "";
          const answerHtml = r3.answer ? `<div style="margin-top:8px;padding:6px 8px;background:var(--surface-1);border-radius:3px;border-left:3px solid #22c55e;color:var(--text);font-size:11px"><b>Answer:</b> ${escapeHtml(r3.answer)}</div>` : "";
          const applyErr = r3.apply_error ? `<div style="margin-top:6px;color:#e05252;font-size:10px"><b>Not applied:</b> ${escapeHtml(r3.apply_error)}</div>` : "";
          const ctxHtml = r3.context ? `<div style="margin-top:6px;color:var(--muted);font-size:11px;font-style:italic">${escapeHtml(r3.context.slice(0, 200))}</div>` : "";
          let optPayload = null;
          try {
            optPayload = r3.payload ? JSON.parse(r3.payload) : null;
          } catch (e3) {
            optPayload = null;
          }
          const hitlOpts = optPayload && Array.isArray(optPayload.options) ? optPayload.options : [];
          const hitlRec = optPayload && typeof optPayload.recommended === "string" ? optPayload.recommended : null;
          const dualChannelHint = st === "pending" && !isMd && (urg === "blocking" || urg === "high") ? `<div style="margin-top:6px;display:flex;align-items:center;gap:6px;font-size:10px;color:var(--accent)">
               <span title="Also displayed inline in Claude Code \u2014 first answer (dashboard or chat) wins">\u{1F4DF} Dual-channel \u2014 also shown in Claude Code chat</span>
               <button class="secondary hitl-copy-id-btn" data-hitl-id="${escapeHtml(r3.id)}" title="Copy HITL ID to clipboard" style="padding:1px 7px;font-size:9px">Copy ID</button>
             </div>` : "";
          let actionBtns = "";
          if (st === "pending" && isMd) {
            actionBtns = `

          <div style="display:flex;gap:6px;margin-top:8px;align-items:center">

            <button class="primary hitl-approve-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:3px 12px;font-size:10px">Approve &amp; write</button>

            <button class="secondary hitl-reject-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:3px 10px;font-size:10px">Reject</button>

          </div>`;
          } else if (st === "pending" && hitlOpts.length) {
            const optBtns = hitlOpts.map((o3, i3) => {
              const isRec = hitlRec !== null && String(o3) === hitlRec;
              const recStyle = isRec ? "border:1px solid var(--accent);background:var(--accent)1a;font-weight:600" : "";
              const recBadge = isRec ? ' <span style="font-size:8px;color:var(--accent);font-weight:700;text-transform:uppercase;letter-spacing:.04em">(recommended)</span>' : "";
              return `<button class="secondary hitl-opt-btn" data-hitl-id="${escapeHtml(r3.id)}" data-answer="${escapeHtml(o3)}"${isRec ? ' data-recommended="1" autofocus' : ""} style="padding:3px 10px;font-size:10px;text-align:left;${recStyle}">${i3 + 1}. ${escapeHtml(o3)}${recBadge}</button>`;
            }).join("\n");
            const kbHint = hitlOpts.length ? `<div style="font-size:9px;color:var(--muted);margin-top:2px">Press <b>1\u2013${Math.min(9, hitlOpts.length)}</b> to choose${hitlRec !== null ? ", <b>Enter</b> for the recommended option" : ""}.</div>` : "";
            actionBtns = `

          <div class="hitl-opts" data-hitl-id="${escapeHtml(r3.id)}" tabindex="0" style="display:flex;flex-direction:column;gap:4px;margin-top:8px;outline:none">

            ${optBtns}

            ${kbHint}

            <button class="secondary hitl-dismiss-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:3px 10px;font-size:10px;margin-top:2px;align-self:flex-start">Dismiss</button>

          </div>`;
          } else if (st === "pending") {
            actionBtns = `

          <div style="display:flex;gap:6px;margin-top:8px;align-items:center">

            <input type="text" placeholder="Answer\u2026" id="hitl-ans-${r3.id}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;outline:none">

            <button class="primary hitl-answer-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:3px 10px;font-size:10px">Answer</button>

            <button class="secondary hitl-dismiss-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:3px 10px;font-size:10px">Dismiss</button>

          </div>`;
          }
          return `<div class="hitl-row" data-search="${escapeHtml((r3.question || "") + " " + (r3.status || "") + " " + (r3.context || ""))}" style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid ${urgencyColor[urg] || "var(--accent)"};border-radius:0 4px 4px 0;padding:10px 12px;margin-bottom:8px">

          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:4px">

            <div style="font-weight:600;font-size:12px;color:var(--text)">${escapeHtml(r3.question || "")}</div>

            <div style="display:flex;gap:4px;flex-shrink:0">

              ${r3.answered_by === "auto" ? `<span title="Auto-answered \u2014 no human reviewed this" style="font-size:9px;font-weight:600;background:var(--accent)22;color:var(--accent);padding:1px 6px;border-radius:3px">auto</span>` : ""}

              ${r3.kind === "correction" ? `<span title="Mid-run correction \u2014 non-blocking; the executor applies it at the next item boundary" style="font-size:9px;font-weight:600;background:#f59e0b22;color:#f59e0b;padding:1px 6px;border-radius:3px">\u270E correction</span>` : ""}

              <span style="font-size:9px;font-weight:600;background:${urgencyColor[urg] || "var(--accent)"}22;color:${urgencyColor[urg] || "var(--accent)"};padding:1px 6px;border-radius:3px">${escapeHtml(urg)}</span>

              <span style="font-size:9px;font-weight:600;background:${statusBadge[st] || "var(--muted)"}22;color:${statusBadge[st] || "var(--muted)"};padding:1px 6px;border-radius:3px">${escapeHtml(st)}</span>

            </div>

          </div>

          <div style="color:var(--muted);font-size:10px">${escapeHtml(dt)}${r3.assigned_to ? " \xB7 @" + escapeHtml(r3.assigned_to) : ""}</div>

          ${mdMeta}${ctxHtml}${dualChannelHint}${diffHtml}${answerHtml}${applyErr}${actionBtns}

        </div>`;
        };
        let html = pending.map(renderCard).join("");
        if (resolved.length > 0) {
          html += `<div style="color:var(--muted);font-size:10px;margin:12px 0 6px;border-top:1px solid var(--border);padding-top:8px">RESOLVED (${resolved.length})</div>`;
          html += resolved.map(renderCard).join("");
        }
        body.innerHTML = html;
        _wireTabSearch(`hitl-search-${projectId}`, `hitl-body-${projectId}`, ".hitl-row");
        body.querySelectorAll(".hitl-answer-btn").forEach((btn) => {
          btn.onclick = async () => {
            const id = btn.dataset.hitlId;
            const inp = document.getElementById(`hitl-ans-${id}`);
            const answer = (inp && inp.value || "").trim();
            if (!answer) {
              toast("answer required", true);
              return;
            }
            try {
              await api(`/hitl/${id}`, { method: "PATCH", body: JSON.stringify({ action: "answer", answer }) });
              toast("answered \u2713");
              _removeHitlCard(id);
              render();
            } catch (e3) {
              toast("failed: " + e3.message, true);
            }
          };
        });
        body.querySelectorAll(".hitl-opt-btn").forEach((btn) => {
          btn.onclick = async () => {
            const id = btn.dataset.hitlId;
            const answer = btn.dataset.answer || "";
            try {
              await api(`/hitl/${id}`, { method: "PATCH", body: JSON.stringify({ action: "answer", answer }) });
              toast("answered \u2713");
              _removeHitlCard(id);
              render();
            } catch (e3) {
              toast("failed: " + e3.message, true);
            }
          };
        });
        body.querySelectorAll(".hitl-opts").forEach((box) => {
          box.addEventListener("keydown", (e3) => {
            const btns = Array.from(box.querySelectorAll(".hitl-opt-btn"));
            if (!btns.length) return;
            if (e3.key === "Enter") {
              const rec = box.querySelector('.hitl-opt-btn[data-recommended="1"]');
              const target = rec || (document.activeElement && document.activeElement.classList.contains("hitl-opt-btn") ? document.activeElement : null);
              if (target) {
                e3.preventDefault();
                target.click();
              }
            } else if (/^[1-9]$/.test(e3.key)) {
              const idx = parseInt(e3.key, 10) - 1;
              if (idx < btns.length) {
                e3.preventDefault();
                btns[idx].click();
              }
            }
          });
        });
        body.querySelectorAll(".hitl-dismiss-btn").forEach((btn) => {
          btn.onclick = async () => {
            if (!confirm("Dismiss this HITL request?")) return;
            try {
              await api(`/hitl/${btn.dataset.hitlId}`, { method: "PATCH", body: JSON.stringify({ action: "dismiss" }) });
              toast("dismissed");
              _removeHitlCard(btn.dataset.hitlId);
              render();
            } catch (e3) {
              toast("failed: " + e3.message, true);
            }
          };
        });
        body.querySelectorAll(".hitl-approve-btn").forEach((btn) => {
          btn.onclick = async () => {
            if (!confirm("Approve and write this markdown change? It will be committed at the next checkpoint.")) return;
            try {
              const res = await api(`/hitl/${btn.dataset.hitlId}`, { method: "PATCH", body: JSON.stringify({ action: "answer", answer: "approved" }) });
              if (res && res.applied === false) toast("not applied: " + (res.apply_error || "see card"), true);
              else toast("approved \u2713 \u2014 section written, staged for checkpoint");
              _removeHitlCard(btn.dataset.hitlId);
              render();
            } catch (e3) {
              toast("failed: " + e3.message, true);
            }
          };
        });
        body.querySelectorAll(".hitl-reject-btn").forEach((btn) => {
          btn.onclick = async () => {
            if (!confirm("Reject this proposed change?")) return;
            try {
              await api(`/hitl/${btn.dataset.hitlId}`, { method: "PATCH", body: JSON.stringify({ action: "dismiss" }) });
              toast("rejected");
              _removeHitlCard(btn.dataset.hitlId);
              render();
            } catch (e3) {
              toast("failed: " + e3.message, true);
            }
          };
        });
        body.querySelectorAll(".hitl-copy-id-btn").forEach((btn) => {
          btn.onclick = () => {
            const id = btn.dataset.hitlId;
            navigator.clipboard.writeText(id).then(() => toast("HITL ID copied \u2713")).catch(() => {
              const tmp = document.createElement("textarea");
              tmp.value = id;
              document.body.appendChild(tmp);
              tmp.select();
              document.execCommand("copy");
              document.body.removeChild(tmp);
              toast("HITL ID copied \u2713");
            });
          };
        });
      } catch (e3) {
        body.innerHTML = `<div style="color:var(--muted)">failed to load HITL queue: ${escapeHtml(String(e3))}</div>`;
      }
    };
    if (statusFilter) statusFilter.onchange = render;
    if (refreshBtn) refreshBtn.onclick = render;
    render();
  }
  async function loadTeamTab(projectId) {
    const body = document.getElementById(`team-body-${projectId}`);
    const daySel = document.getElementById(`team-days-${projectId}`);
    const refreshBtn = document.getElementById(`team-refresh-${projectId}`);
    if (!body) return;
    const render = async () => {
      body.innerHTML = `<div class="empty" style="color:var(--muted)">loading team summary\u2026</div>`;
      const days = parseInt(daySel && daySel.value || "14", 10);
      try {
        const data = await projectApi(projectId, `/team/summary?project_id=${encodeURIComponent(projectId)}&days=${days}`);
        const humans = data.humans || [];
        if (humans.length === 0) {
          body.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">

          (no human-attributed activity in the last ${data.period_days}d \u2014 set <code>MERIDIAN_HUMAN_ID</code> or pass <code>human_id</code> to register_session)

        </div>`;
          return;
        }
        const dotColor = { active: "#4ade80", recent: "#fbbf24", idle: "#6b7280" };
        const cards = humans.map((h3) => {
          const c3 = _colorForHuman(h3.human_id);
          const dc = dotColor[h3.presence] || dotColor.idle;
          const fw = h3.agent_framework && h3.agent_framework !== "claude_code" ? `<span style="background:var(--surface-2);color:var(--accent);font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">${escapeHtml(h3.agent_framework)}</span>` : "";
          const tasksLine = `${h3.tasks_done} done \xB7 ${h3.tasks_pending} pending${h3.tasks_failed ? " \xB7 " + h3.tasks_failed + " failed" : ""}`;
          const lastSeen = h3.last_seen ? formatRelativeTime(h3.last_seen) : "never";
          const recent = (h3.recent || []).slice(0, 3).map((t3) => {
            const s3 = (t3.status || "?").toUpperCase();
            const desc = (t3.description || "").slice(0, 90);
            return `<div style="color:var(--muted);font-size:10px;padding:1px 0">[${escapeHtml(s3)}] ${escapeHtml(desc)}</div>`;
          }).join("");
          return `<div class="team-card" data-search="${escapeHtml((h3.human_id || "") + " " + (h3.active_session || "") + " " + (h3.agent_framework || ""))}" style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid ${c3};border-radius:4px;padding:10px 12px;margin-bottom:8px">

          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">

            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dc}"></span>

            <span style="color:${c3};font-weight:600">${escapeHtml(h3.human_id)}</span>${fw}

            <span style="color:var(--muted);font-size:10px;margin-left:auto">${escapeHtml(lastSeen)}</span>

          </div>

          <div style="color:var(--text);font-size:11px;margin-bottom:2px">${escapeHtml(h3.active_session || "(no active session)")}</div>

          <div style="color:var(--accent);font-size:10px;margin-bottom:4px">${escapeHtml(tasksLine)}</div>

          ${recent}

        </div>`;
        }).join("");
        let goalMarkers = [];
        try {
          const windowStart = Date.now() - days * 86400 * 1e3;
          const history2 = await api(`/projects/${projectId}/goal-history`);
          const sorted = [...history2 || []].sort(
            (a3, b2) => Date.parse((a3.created_at || "").replace(" ", "T") + "Z") - Date.parse((b2.created_at || "").replace(" ", "T") + "Z")
          );
          sorted.forEach((entry, i3) => {
            const ts = entry.created_at;
            if (!ts) return;
            const t3 = Date.parse(ts.replace(" ", "T") + "Z");
            if (!isFinite(t3) || t3 < windowStart) return;
            const prev = sorted[i3 - 1];
            if (!prev) {
              goalMarkers.push({ ts, field: "content_updated_at", label: "" });
              return;
            }
            if ((entry.sprint || "") !== (prev.sprint || "")) {
              goalMarkers.push({
                ts,
                field: "sprint_updated_at",
                label: (entry.sprint || "").split(/[\n—]/)[0].trim().slice(0, 22)
              });
            }
            if ((entry.north_star || "") !== (prev.north_star || "")) {
              goalMarkers.push({ ts, field: "ns_updated_at", label: "" });
            }
            if ((entry.version_goal || "") !== (prev.version_goal || "")) {
              goalMarkers.push({ ts, field: "content_updated_at", label: "" });
            }
          });
          goalMarkers = goalMarkers.filter((m3, i3) => !goalMarkers.slice(0, i3).some(
            (p3) => p3.field === m3.field && Math.abs(Date.parse((m3.ts || "").replace(" ", "T") + "Z") - Date.parse((p3.ts || "").replace(" ", "T") + "Z")) < 6e4
          ));
        } catch (_2) {
        }
        const standup = humans.map((h3) => {
          const c3 = _colorForHuman(h3.human_id);
          const last = (h3.recent || []).map((t3) => (t3.description || "").slice(0, 60)).slice(0, 4).join("; ");
          return `<div style="padding:3px 0;border-left:2px solid ${c3};padding-left:8px;font-size:11px">

          <span style="color:${c3};font-weight:600">${escapeHtml(h3.human_id)}</span> \xB7 ${h3.tasks_done} done \u2014 <span style="color:var(--muted)">${escapeHtml(last) || "\u2014"}</span>

        </div>`;
        }).join("");
        let decisionsHtml = "";
        try {
          const pinned = await api(`/projects/${projectId}/decisions-pinned`);
          if (pinned && pinned.length) {
            const rows = pinned.slice(0, 8).map((d3) => {
              const cat = d3.category ? `<span style="font-size:9px;color:var(--muted);margin-left:4px">${escapeHtml(d3.category)}</span>` : "";
              return `<div style="padding:4px 0;border-bottom:1px solid var(--border)">

              <div style="font-size:11px;font-weight:600;color:var(--text)">${escapeHtml(d3.title)}${cat}</div>

              <div style="font-size:10px;color:var(--muted);margin-top:1px;white-space:pre-wrap">${escapeHtml((d3.body || "").slice(0, 160))}</div>

            </div>`;
            }).join("");
            decisionsHtml = `<section style="margin-top:18px;padding-top:10px;border-top:1px solid var(--border)">

            <div style="color:var(--accent);font-weight:600;margin-bottom:8px">\u{1F4CC} Active decisions (${pinned.length})</div>

            ${rows}

          </section>`;
          }
        } catch (_2) {
        }
        body.innerHTML = `

        <section>

          <div style="color:var(--accent);font-weight:600;margin-bottom:8px">\u{1F465} Live (${data.active_count} active)</div>

          ${cards}

        </section>

        <section style="margin-top:18px;padding-top:10px;border-top:1px solid var(--border)">

          <div style="color:var(--accent);font-weight:600;margin-bottom:8px">\u{1F5DE} Standup digest</div>

          ${standup}

        </section>

        ${decisionsHtml}`;
        _wireTabSearch(`team-search-${projectId}`, `team-body-${projectId}`, ".team-card");
      } catch (e3) {
        body.innerHTML = renderProjectLoadError2(projectId, "Team summary unavailable", `/team/summary?project_id=${encodeURIComponent(projectId)}&days=${days}`, e3);
        wireProjectLoadRetry2(body, projectId);
      }
    };
    if (daySel) daySel.onchange = render;
    if (refreshBtn) refreshBtn.onclick = render;
    render();
  }
  async function updateLiveFeed(projectId) {
    const el2 = document.getElementById(`live-session-${projectId}`);
    if (!el2) return;
    const panel = getPanelState(projectId);
    try {
      const sessions = await api(`/projects/${projectId}/sessions?active_only=true`);
      const active = sessions && sessions.filter((s3) => s3.status === "active");
      if (!active || active.length === 0) {
        panel.liveSessionId = null;
        el2.style.display = "none";
        return;
      }
      const sess = active[0];
      panel.liveSessionId = sess.id;
      const tasks = await api(`/projects/${projectId}/sessions/${sess.id}/tasks/live?limit=5`).catch(() => []);
      const elapsed = sess.last_seen ? Math.round((Date.now() - (/* @__PURE__ */ new Date(sess.last_seen + "Z")).getTime()) / 6e4) : null;
      const elapsedStr = elapsed !== null ? elapsed < 2 ? "just now" : `${elapsed}m ago` : "";
      const taskRows = (tasks || []).map((t3) => {
        const icon = t3.status === "done" ? "\u2713" : t3.status === "failed" ? "\u2717" : t3.status === "in_progress" ? "\u25B6" : "\xB7";
        const color = t3.status === "done" ? "var(--status-done)" : t3.status === "failed" ? "var(--status-failed)" : t3.status === "in_progress" ? "var(--accent)" : "var(--muted)";
        const desc = (t3.description || "").length > 80 ? t3.description.slice(0, 80) + "\u2026" : t3.description;
        return `<div style="display:flex;gap:6px;align-items:baseline;padding:1px 0">

        <span style="color:${color};font-size:10px;flex-shrink:0">${icon}</span>

        <span style="color:var(--text);font-size:10px;word-break:break-word">${escapeHtml(desc || "")}</span>

      </div>`;
      }).join("");
      const extraCount = active.length - 1;
      const extraRows = active.slice(1).map((s3) => {
        const age = s3.last_seen ? Math.round((Date.now() - (/* @__PURE__ */ new Date(s3.last_seen + "Z")).getTime()) / 6e4) : null;
        const ageStr = age !== null ? age < 2 ? "just now" : `${age}m ago` : "";
        return `<div style="display:flex;align-items:center;gap:6px;padding:2px 0">
        <span style="font-size:9px;color:var(--accent)">\u25CF</span>
        <span style="font-size:10px;color:var(--text);font-family:var(--font-mono)">${escapeHtml(s3.name || "unnamed")}</span>
        ${s3.human_id ? `<span style="font-size:9px;color:var(--muted)">${escapeHtml(s3.human_id)}</span>` : ""}
        ${ageStr ? `<span style="font-size:9px;color:var(--muted);margin-left:auto">${ageStr}</span>` : ""}
      </div>`;
      }).join("");
      el2.innerHTML = `

      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">

        <span style="font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600;background:var(--accent)1a;border:1px solid var(--accent)44;border-radius:3px;padding:1px 5px">\u25CF LIVE</span>

        <span style="font-size:11px;font-weight:600;color:var(--text);font-family:var(--font-mono)">${escapeHtml(sess.name || "unnamed session")}</span>

        ${sess.human_id ? `<span style="font-size:10px;color:var(--muted)">${escapeHtml(sess.human_id)}</span>` : sess.name ? `<span style="font-size:10px;color:var(--muted);font-style:italic">${escapeHtml(sess.name)}</span>` : ""}

        ${extraCount > 0 ? `<button class="secondary" id="live-feed-extra-toggle-${projectId}" style="font-size:9px;padding:1px 6px;margin-left:4px">+${extraCount} more \u25B8</button>` : ""}

        ${elapsedStr ? `<span style="font-size:10px;color:var(--muted);margin-left:auto">${elapsedStr}</span>` : ""}

      </div>

      ${extraCount > 0 ? `<div id="live-feed-extra-${projectId}" style="display:none;margin-bottom:6px;padding:4px 8px;background:var(--surface-2);border-radius:3px">${extraRows}</div>` : ""}

      <div style="font-family:var(--font-mono)">

        ${taskRows || '<div style="color:var(--muted);font-size:10px">no recent tasks</div>'}

      </div>`;
      if (extraCount > 0) {
        const toggleBtn = el2.querySelector(`#live-feed-extra-toggle-${projectId}`);
        const extraEl = el2.querySelector(`#live-feed-extra-${projectId}`);
        if (toggleBtn && extraEl) {
          toggleBtn.onclick = () => {
            const open = extraEl.style.display !== "none";
            extraEl.style.display = open ? "none" : "block";
            toggleBtn.textContent = open ? `+${extraCount} more \u25B8` : `${extraCount} others \u25BE`;
          };
        }
      }
      el2.style.display = "block";
    } catch (e3) {
      panel.liveSessionId = null;
      el2.style.display = "none";
    }
  }
  async function loadRecentSessions(projectId, sessions = null) {
    const el2 = document.getElementById(`recent-sessions-${projectId}`);
    if (!el2) return;
    try {
      const panel = getPanelState(projectId);
      const allSessions = Array.isArray(sessions) ? sessions : await api(`/projects/${projectId}/sessions?active_only=false`);
      const recent = sortSessionsMostRecentFirst(
        (allSessions || []).filter((s3) => s3.id !== panel.liveSessionId && !isLiveSession(s3))
      ).slice(0, 5);
      if (!recent.length) {
        el2.style.display = "none";
        return;
      }
      el2.innerHTML = `

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">Recent Sessions</div>

      ${recent.map((s3) => {
        const seenAt = s3.last_seen || s3.created_at || "";
        const dt = seenAt ? formatRelativeTime(seenAt) : "";
        const name = escapeHtml(s3.name || s3.id || "session");
        const status = s3.status === "idle" ? "idle" : s3.status === "closed" ? "done" : s3.status || "session";
        const _rawSummary = s3.session_summary;
        const summaryText = typeof _rawSummary === "string" ? _rawSummary : _rawSummary && _rawSummary.summary ? _rawSummary.summary : "";
        const summaryPreview = summaryText ? escapeHtml(summaryText.slice(0, 90)) : "";
        const hasSummary = !!summaryText;
        const humanClause = s3.human_id ? `, human_id="${String(s3.human_id).replace(/"/g, '\\"')}"` : "";
        const cmd = `start_session(project_id="${projectId}", session_name="${String(s3.name || "resume-session").replace(/"/g, '\\"')}"${humanClause})`;
        const safeCmd = escapeHtml(cmd);
        return `<div class="recent-session-row" data-session-id="${escapeHtml(s3.id)}" style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-1);cursor:pointer">

          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">

            <span style="font-weight:600;font-size:10px;color:var(--text);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s3.name || "")}">${name}</span>

            <div style="display:flex;gap:4px;align-items:center;flex-shrink:0">

              <span style="font-size:9px;color:var(--muted)">${escapeHtml(status)}${dt ? ` \xB7 ${escapeHtml(dt)}` : ""}</span>

              <button class="secondary resume-session-btn" data-cmd="${safeCmd}"

                style="padding:1px 6px;font-size:9px" title="Copy start_session() to clipboard">Resume</button>
              <button class="secondary recent-session-timeline-btn" data-session-id="${escapeHtml(s3.id)}"
                style="padding:1px 6px;font-size:9px" title="Open filtered timeline">Timeline</button>
              <span class="recent-session-chevron" style="font-size:9px;color:var(--muted);margin-left:2px">\u25BC</span>

            </div>

          </div>

          ${summaryPreview ? `<div style="font-size:9px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(summaryText)}">${summaryPreview}</div>` : ""}
          <div class="recent-session-tasks" data-full-summary="${escapeHtml(summaryText)}" style="display:none;margin-top:6px;padding-top:5px;border-top:1px solid var(--border);font-size:10px;color:var(--muted)"></div>

        </div>`;
      }).join("")}`;
      el2.querySelectorAll(".resume-session-btn").forEach((btn) => {
        btn.onclick = (event) => {
          event.stopPropagation();
          const cmd = btn.dataset.cmd || "";
          navigator.clipboard.writeText(cmd).then(() => toast("Copied start_session() to clipboard")).catch(() => toast("copy failed", true));
        };
      });
      el2.querySelectorAll(".recent-session-timeline-btn").forEach((btn) => {
        btn.onclick = (event) => {
          event.stopPropagation();
          openTimelineForSession(projectId, btn.dataset.sessionId);
        };
      });
      el2.querySelectorAll(".recent-session-row").forEach((row) => {
        row.onclick = async (evt) => {
          if (evt.target.closest(".resume-session-btn, .recent-session-timeline-btn")) return;
          const target = row.querySelector(".recent-session-tasks");
          const chevron = row.querySelector(".recent-session-chevron");
          const sid = row.dataset.sessionId;
          if (!target || !sid) return;
          if (target.style.display !== "none") {
            target.style.display = "none";
            if (chevron) chevron.textContent = "\u25BC";
            return;
          }
          if (!target.dataset.loaded) {
            target.textContent = "loading...";
            try {
              const fullSummary = target.dataset.fullSummary || "";
              const taskRows = await api(`/projects/${projectId}/sessions/${sid}/tasks/live?limit=20`);
              const summaryHtml = fullSummary ? `<div style="color:var(--text-dim);margin-bottom:5px;white-space:pre-wrap;word-break:break-word">${escapeHtml(fullSummary)}</div>` : "";
              const tasksHtml = taskRows && taskRows.length ? taskRows.map((t3) => `<div style="padding:2px 0"><span style="color:var(--accent)">${escapeHtml((t3.status || "").toUpperCase())}</span> ${escapeHtml((t3.description || "").slice(0, 180))}</div>`).join("") : "<div>(no task log for this session)</div>";
              target.innerHTML = summaryHtml + tasksHtml;
              target.dataset.loaded = "1";
            } catch (e3) {
              target.textContent = "failed to load tasks";
            }
          }
          target.style.display = "block";
          if (chevron) chevron.textContent = "\u25B2";
        };
      });
      el2.style.display = "block";
    } catch (_2) {
      el2.style.display = "none";
    }
  }
  async function loadMilestones(projectId) {
    const el2 = document.getElementById(`milestones-strip-${projectId}`);
    if (!el2) return;
    try {
      const all = await api(`/projects/${projectId}/sprint-items`);
      const doneStatuses = /* @__PURE__ */ new Set(["done", "skipped", "failed", "pushed"]);
      const milestones = (all || []).filter(
        (i3) => i3.milestone_type === "milestone" || doneStatuses.has(i3.status)
      ).sort((a3, b2) => {
        const aTs = a3.completed_at || a3.added_at || "";
        const bTs = b2.completed_at || b2.added_at || "";
        return bTs.localeCompare(aTs);
      });
      if (!milestones.length) {
        el2.style.display = "none";
        return;
      }
      const statusIcon = (s3) => s3 === "done" ? "\u2713" : s3 === "failed" ? "\u2717" : s3 === "pushed" ? "\u2192" : s3 === "skipped" ? "\u2014" : s3 === "in_progress" ? "\u25B6" : "\u25E6";
      const statusColor = (s3) => s3 === "done" ? "var(--accent-green,#34d399)" : s3 === "failed" ? "#e05" : s3 === "pushed" ? "var(--accent)" : s3 === "in_progress" ? "var(--accent)" : "var(--muted)";
      el2.innerHTML = `

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">Completed (${milestones.length})</div>

      <div style="display:flex;flex-wrap:wrap;gap:6px">

        ${milestones.slice(0, 20).map((m3) => {
        const date = (m3.completed_at || m3.added_at || "").slice(0, 10);
        const ic = statusIcon(m3.status);
        const col = statusColor(m3.status);
        return `<div style="display:flex;align-items:center;gap:4px;border:1px solid var(--border);border-radius:3px;padding:3px 7px;background:var(--surface-1);opacity:${m3.status === "done" ? 1 : 0.7}">

            <span style="color:${col};font-size:11px">${ic}</span>

            <span style="font-family:var(--font-mono);font-size:10px;color:var(--text)">${escapeHtml((m3.title || "").slice(0, 40))}</span>

            ${date ? `<span style="font-size:9px;color:var(--muted)">${escapeHtml(date)}</span>` : ""}

          </div>`;
      }).join("")}

        ${milestones.length > 20 ? `<span style="font-size:10px;color:var(--muted);padding:3px 4px">+${milestones.length - 20} more</span>` : ""}

      </div>`;
      el2.style.display = "block";
    } catch (_2) {
      el2.style.display = "none";
    }
  }
  async function loadRecentRuns(projectId) {
    const body = document.getElementById(`recent-runs-body-${projectId}`);
    const toggle = document.getElementById(`recent-runs-toggle-${projectId}`);
    const chevron = document.getElementById(`recent-runs-chevron-${projectId}`);
    if (!body) return;
    let collapsed = false;
    if (toggle) {
      toggle.onclick = () => {
        collapsed = !collapsed;
        body.style.display = collapsed ? "none" : "";
        if (chevron) chevron.textContent = collapsed ? "\u25BC" : "\u25B2";
      };
    }
    try {
      const runs = await api(`/projects/${projectId}/runs?limit=10`);
      if (!runs || !runs.length) {
        body.innerHTML = '<div style="color:var(--muted);font-size:10px">No runs yet.</div>';
        return;
      }
      body.innerHTML = runs.map((run) => {
        const sid = (run.session_id || "").slice(0, 8);
        const runLabel = run.session_name || sid;
        const ts = (run.started_at || "").slice(0, 16).replace("T", " ");
        const dur = run.duration_s != null ? run.duration_s < 60 ? `${run.duration_s}s` : `${Math.round(run.duration_s / 60)}m` : isLiveSession({ status: run.session_status, last_seen: run.started_at }) ? "live" : "\u2014";
        const cnt = run.task_count || 0;
        const displayRunStatus = run.status === "running" && run.session_status && run.session_status !== "active" ? "done" : run.status;
        const statusColor = displayRunStatus === "running" ? "var(--accent)" : displayRunStatus === "failed" ? "var(--danger,#e05)" : "var(--muted)";
        const dots = displayRunStatus === "running" ? " \xB7" : "";
        return `<div class="run-row" data-run-id="${escapeHtml(run.id)}" data-project-id="${escapeHtml(projectId)}"

          style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-1);cursor:pointer">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">

          <span style="font-size:10px;color:var(--text);font-family:var(--font-mono);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(run.session_id || "")}">${escapeHtml(runLabel)}${dots}</span>

          <span style="font-size:9px;color:var(--muted)">${cnt} tasks \xB7 ${dur} \xB7 ${ts}${run.session_name && sid ? ` \xB7 ${escapeHtml(sid)}` : ""}</span>

          <span style="font-size:9px;color:${statusColor}">${displayRunStatus}</span>

        </div>

        <div class="run-transcript-${escapeHtml(run.id)}" style="display:none;margin-top:6px;padding:6px 8px;background:var(--surface-2);border-radius:3px;font-size:10px;white-space:pre-wrap;color:var(--muted)"></div>

      </div>`;
      }).join("");
      body.querySelectorAll(".run-row").forEach((row) => {
        row.addEventListener("click", async () => {
          const runId = row.dataset.runId;
          const pid = row.dataset.projectId;
          const transcript = row.querySelector(`.run-transcript-${runId}`);
          if (!transcript) return;
          if (transcript.style.display !== "none") {
            transcript.style.display = "none";
            return;
          }
          if (!transcript.textContent.trim()) {
            try {
              const full = await api(`/projects/${pid}/runs/${runId}`);
              transcript.textContent = full.transcript || "(empty)";
            } catch {
              transcript.textContent = "failed to load";
            }
          }
          transcript.style.display = "block";
        });
      });
    } catch (_2) {
      body.innerHTML = '<div style="color:var(--muted);font-size:10px">Could not load runs.</div>';
    }
  }
  async function loadQueue(projectId) {
    const body = document.getElementById(`queue-body-${projectId}`);
    if (!body) return;
    const panel = getPanelState(projectId);
    if (!panel.queueDoneLimit) panel.queueDoneLimit = QUEUE_DONE_PAGE_SIZE;
    body.innerHTML = '<div class="empty" style="color:var(--muted)">loading\u2026</div>';
    try {
      const [sessions, sprintItems] = await Promise.all([
        projectApi(projectId, `/projects/${projectId}/sessions?active_only=false`).catch(() => []),
        projectApi(projectId, `/projects/${projectId}/sprint-items?with_counts=true`)
      ]);
      const liveSession = (sessions || []).find((s3) => isLiveSession(s3));
      panel.liveSessionId = liveSession ? liveSession.id : null;
      const sprintPayload = sprintItems || [];
      panel.queueSprintItems = Array.isArray(sprintPayload) ? sprintPayload : sprintPayload.items || [];
      panel.queueTotalDoneCount = Array.isArray(sprintPayload) ? panel.queueSprintItems.filter((it) => it.status === "done").length : sprintPayload.total_done_count || 0;
      const renderCurrentQueue = () => {
        body.innerHTML = renderQueue(projectId, panel.queueSprintItems || []);
        wireQueueSectionToggles(projectId);
        const moreBtn = document.getElementById(`queue-done-more-${projectId}`);
        if (moreBtn) {
          moreBtn.onclick = () => {
            panel.queueDoneLimit = (panel.queueDoneLimit || QUEUE_DONE_PAGE_SIZE) + QUEUE_DONE_PAGE_SIZE;
            renderCurrentQueue();
          };
        }
      };
      renderCurrentQueue();
      loadRecentSessions(projectId, sessions || []);
      const refreshBtn = document.getElementById(`queue-refresh-${projectId}`);
      if (refreshBtn) refreshBtn.onclick = () => loadQueue(projectId);
      const reconcileBtn = document.getElementById(`queue-reconcile-${projectId}`);
      if (reconcileBtn) reconcileBtn.onclick = () => runReconcile(projectId);
      const searchInput = document.getElementById(`task-search-${projectId}`);
      if (searchInput) {
        searchInput.placeholder = "Search sprint items, notes, decisions\u2026";
        if (!searchInput._wired) {
          searchInput._wired = true;
          let _searchTimer = null;
          searchInput.addEventListener("input", function() {
            clearTimeout(_searchTimer);
            const q2 = this.value.trim();
            _searchTimer = setTimeout(async () => {
              if (!q2) {
                renderCurrentQueue();
                return;
              }
              try {
                const results = await api(`/projects/${projectId}/search?q=${encodeURIComponent(q2)}&limit=10`);
                body.innerHTML = renderSearchResults2(q2, results);
              } catch (e3) {
                body.innerHTML = `<div class="empty">search failed: ${escapeHtml(e3.message)}</div>`;
              }
            }, 300);
          });
        }
      }
    } catch (e3) {
      body.innerHTML = renderProjectLoadError2(projectId, "Queue unavailable", `/projects/${projectId}/sprint-items`, e3);
      wireProjectLoadRetry2(body, projectId);
    }
  }
  async function runReconcile(projectId) {
    const container = document.getElementById(`reconcile-results-${projectId}`);
    const btn = document.getElementById(`queue-reconcile-${projectId}`);
    if (!container) return;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "checking\u2026";
    }
    container.style.display = "block";
    container.innerHTML = '<span style="color:var(--muted)">Checking commits against sprint board\u2026</span>';
    try {
      const data = await projectApi(projectId, `/projects/${projectId}/reconcile`);
      if (!data.matches || data.matches.length === 0) {
        container.innerHTML = `<span style="color:var(--muted)">\u2713 No drift detected (checked ${data.commit_count || 0} commits against ${data.pending_count || 0} pending items)</span>
        <button onclick="document.getElementById('reconcile-results-${projectId}')!.style.display='none'" style="margin-left:10px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px">\u2715</button>`;
      } else {
        const n2 = data.matches.length;
        let html = `<div style="margin-bottom:6px;color:var(--warning,#f59e0b);font-weight:600">${n2} item${n2 !== 1 ? "s" : ""} may already be shipped \u2014 verify before executing</div>`;
        data.matches.forEach((m3) => {
          const confidence = m3.confidence === "high" ? "\u{1F534} high" : "\u{1F7E1} medium";
          const commits = (m3.matching_commits || []).slice(0, 2).map(
            (c3) => `<span style="color:var(--muted)">${escapeHtml(c3.sha)} \u2014 ${escapeHtml(c3.message)}</span>`
          ).join("<br>");
          html += `<div style="border:1px solid var(--border);border-radius:4px;padding:6px 8px;margin-bottom:6px;background:var(--surface-3,var(--surface-2))">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
            <div>
              <span style="color:var(--text)">${escapeHtml(m3.title.slice(0, 80))}${m3.title.length > 80 ? "\u2026" : ""}</span>
              <span style="margin-left:6px;font-size:9px;opacity:0.7">${confidence}</span>
              <div style="margin-top:3px;font-size:9px">${commits}</div>
            </div>
            <div style="display:flex;gap:4px;flex-shrink:0">
              <button class="primary" style="padding:2px 7px;font-size:9px"
                onclick="reconcileMarkDone('${projectId}','${m3.item_id}',this)">Mark done</button>
              <button class="secondary" style="padding:2px 7px;font-size:9px"
                onclick="this.closest('div[style]').remove()">Keep</button>
            </div>
          </div>
        </div>`;
        });
        html += `<button onclick="document.getElementById('reconcile-results-${projectId}')!.style.display='none'" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px;margin-top:2px">Dismiss</button>`;
        container.innerHTML = html;
      }
    } catch (e3) {
      container.innerHTML = `<span style="color:var(--danger,#ef4444)">Reconcile failed: ${escapeHtml(e3.message)}</span>
      <button onclick="document.getElementById('reconcile-results-${projectId}')!.style.display='none'" style="margin-left:10px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px">\u2715</button>`;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "reconcile";
      }
    }
  }
  function renderSearchResults2(query, results) {
    if (!results || results.total === 0) {
      return `<div class="empty" style="color:var(--muted);padding:12px 14px">No results for "${escapeHtml(query)}"</div>`;
    }
    const section = (label, items, renderFn) => {
      if (!items || !items.length) return "";
      return `<div style="padding:10px 14px 0">

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">${label}</div>

      ${items.map(renderFn).join("")}

    </div>`;
    };
    const taskRow = (t3) => `<div style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;gap:6px">

      <span style="font-size:11px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(t3.description || "")}">${escapeHtml((t3.description || "").slice(0, 100))}</span>

      <span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(t3.status || "")}</span>

    </div>

  </div>`;
    const noteRow = (n2) => `<div style="border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:0 3px 3px 0;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)" title="${escapeHtml(n2.body || "")}">

    <div style="font-size:11px;font-weight:600;color:var(--accent)" title="${escapeHtml(n2.title || "")}">${escapeHtml((n2.title || "").slice(0, 80))}</div>

    <div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((n2.body || "").slice(0, 80))}</div>

  </div>`;
    const decisionRow = (d3) => `<div style="border:1px solid var(--border);border-left:3px solid var(--warning,#fa0);border-radius:0 3px 3px 0;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)" title="${escapeHtml(d3.body || "")}">

    <div style="font-size:11px;font-weight:600;color:var(--text)" title="${escapeHtml(d3.title || "")}">${escapeHtml((d3.title || "").slice(0, 80))}</div>

    <div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((d3.body || "").slice(0, 80))}</div>

  </div>`;
    const sprintRow = (s3) => `<div style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;gap:6px">

      <span style="font-size:11px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s3.title || "")}">${escapeHtml((s3.title || "").slice(0, 100))}</span>

      <span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(s3.version || "")} \xB7 ${escapeHtml(s3.status || "")}</span>

    </div>

  </div>`;
    return `<div style="padding-bottom:10px">

    ${section("Tasks", results.tasks, taskRow)}

    ${section("Notes", results.notes, noteRow)}

    ${section("Decisions", results.decisions, decisionRow)}

    ${section("Sprint Items", results.sprint_items, sprintRow)}

  </div>`;
  }
  function wireQueueSectionToggles(projectId) {
    const body = document.getElementById(`queue-body-${projectId}`);
    if (!body) return;
    const panel = getPanelState(projectId);
    const sectionState = panel.queueSectionState || (panel.queueSectionState = {
      backburner: true,
      pending: false,
      in_progress: false,
      done: true,
      failed: true
    });
    body.querySelectorAll(".queue-section").forEach((section) => {
      const header = section.querySelector(".queue-section-header");
      const sectionBody = section.querySelector(".queue-section-body");
      const key = section.dataset.section || header?.dataset.sectionKey || "";
      if (!header || !sectionBody || !key) return;
      const applyState = (collapsed, animate) => {
        section.dataset.collapsed = collapsed ? "true" : "false";
        header.setAttribute("aria-expanded", String(!collapsed));
        sectionBody.setAttribute("aria-hidden", String(collapsed));
        sectionState[key] = collapsed;
        if (sectionBody._queueTransitionEnd) {
          sectionBody.removeEventListener("transitionend", sectionBody._queueTransitionEnd);
          sectionBody._queueTransitionEnd = null;
        }
        if (!animate) {
          sectionBody.style.height = collapsed ? "0px" : "auto";
          return;
        }
        if (collapsed) {
          const currentHeight = sectionBody.getBoundingClientRect().height;
          sectionBody.style.height = `${currentHeight}px`;
          sectionBody.offsetHeight;
          sectionBody.style.height = "0px";
        } else {
          sectionBody.style.height = "0px";
          sectionBody.offsetHeight;
          const targetHeight = sectionBody.scrollHeight;
          sectionBody.style.height = `${targetHeight}px`;
          const onEnd = (ev) => {
            if (ev.target !== sectionBody || ev.propertyName !== "height") return;
            if (section.dataset.collapsed !== "true") sectionBody.style.height = "auto";
            sectionBody.removeEventListener("transitionend", onEnd);
            sectionBody._queueTransitionEnd = null;
          };
          sectionBody._queueTransitionEnd = onEnd;
          sectionBody.addEventListener("transitionend", onEnd);
        }
      };
      if (!header._queueWired) {
        header._queueWired = true;
        const toggle = () => applyState(section.dataset.collapsed !== "true", true);
        header.onclick = toggle;
        header.onkeydown = (e3) => {
          if (e3.key === "Enter" || e3.key === " ") {
            e3.preventDefault();
            toggle();
          }
        };
      }
      applyState(section.dataset.collapsed === "true", false);
    });
  }
  window._queueAction = async function(taskId, action) {
    try {
      if (action === "delete") {
        if (!confirm("Delete this task?")) return;
        await api(`/tasks/${taskId}`, { method: "DELETE" });
      } else if (action === "done") {
        await api(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ status: "done" }) });
      } else if (action === "backlog") {
        await api(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ status: "backlog" }) });
      }
      document.querySelectorAll('[id^="queue-body-"]').forEach((el2) => {
        const pid = el2.id.replace("queue-body-", "");
        loadQueue(pid);
      });
    } catch (e3) {
      toast("Action failed: " + e3.message, true);
    }
  };
  async function refreshTab(projectId) {
    await Promise.all([
      refreshGoal(projectId),
      refreshSessions(projectId),
      refreshTasks(projectId)
    ]);
  }
  async function refreshGoal(projectId) {
    const ta = document.getElementById(`goal-${projectId}`);
    const v3 = document.getElementById(`goal-version-${projectId}`);
    if (!ta) return;
    const goalPath = `/projects/${projectId}/goal`;
    try {
      const goal = await projectApi(projectId, goalPath);
      state.panels[projectId].goalRaw = goal.content;
      let text;
      if (typeof goal.content === "string") {
        state.panels[projectId].goalIsJson = false;
        text = goal.content;
      } else {
        state.panels[projectId].goalIsJson = true;
        text = JSON.stringify(goal.content, null, 2);
      }
      const AUTO_SPLIT = "--- AUTO BLOCKS BELOW ---";
      const splitIdx = text.indexOf(AUTO_SPLIT);
      const mainText = splitIdx !== -1 ? text.slice(0, splitIdx).trimEnd() : text;
      const allLines = mainText.split("\n");
      const _firstLine = allLines[0] || "";
      const _isVersionLabel = /^v\d+\.\d+/.test(_firstLine.trim()) || _firstLine.trim().length === 0;
      const titleLine = _isVersionLabel ? _firstLine : "";
      const titleEl = document.getElementById(`goal-title-${projectId}`);
      if (titleEl) {
        titleEl.textContent = titleLine;
        const hasTitle = !!titleLine.trim();
        titleEl.style.display = hasTitle ? "block" : "none";
        const taEl = document.getElementById(`goal-${projectId}`);
        if (taEl) taEl.style.borderRadius = hasTitle ? "0 0 4px 4px" : "4px";
      }
      const body = (_isVersionLabel ? allLines.slice(1) : allLines).join("\n").replace(/^\n/, "");
      const editStart = body.search(/^(CURRENT FOCUS|KEY FILES)/m);
      if (editStart > 0) {
        const shippedEl2 = document.getElementById(`goal-shipped-${projectId}`);
        if (shippedEl2) {
          shippedEl2.textContent = body.slice(0, editStart).trimEnd();
          shippedEl2.style.display = shippedEl2.textContent.trim() ? "block" : "none";
        }
        ta.value = body.slice(editStart);
      } else {
        const shippedEl2 = document.getElementById(`goal-shipped-${projectId}`);
        if (shippedEl2) shippedEl2.style.display = "none";
        ta.value = body;
      }
      const shippedEl = document.getElementById(`goal-shipped-${projectId}`);
      if (shippedEl && !shippedEl.textContent.trim()) shippedEl.style.display = "none";
      autosizeGoalField(ta);
      const autoBlocksEl = document.getElementById(`goal-autoblocks-${projectId}`);
      if (autoBlocksEl) {
        if (splitIdx !== -1) {
          const abWrapper = document.getElementById(`goal-autoblocks-wrapper-${projectId}`);
          if (abWrapper) abWrapper.style.display = "block";
          autoBlocksEl.style.display = "block";
          autoBlocksEl.textContent = text.slice(splitIdx + "--- AUTO BLOCKS BELOW ---".length).trimStart();
        } else {
          const abWrapper2 = document.getElementById(`goal-autoblocks-wrapper-${projectId}`);
          if (abWrapper2) abWrapper2.style.display = "none";
          autoBlocksEl.style.display = "none";
        }
      }
      v3.textContent = `v${goal.version}`;
      const vState = document.getElementById(`goal-state-${projectId}`);
      if (vState) vState.textContent = `v${goal.version}`;
      const nsTA = document.getElementById(`goal-north-star-${projectId}`);
      const spTA = document.getElementById(`goal-sprint-${projectId}`);
      if (nsTA && "north_star" in goal) {
        nsTA.value = goal.north_star || "";
        autosizeGoalField(nsTA);
      }
      if (spTA && "sprint" in goal) {
        spTA.value = goal.sprint || "";
        if (_sprintSelectSyncers[projectId]) _sprintSelectSyncers[projectId](goal.sprint || "");
      }
      const p3 = state.panels[projectId];
      p3._serverNorthStar = goal.north_star || "";
      p3._serverSprint = goal.sprint || "";
      const nsLock = document.getElementById(`goal-ns-lock-${projectId}`);
      if (nsLock) nsLock.textContent = goal.north_star ? "locked" : "unlocked";
      p3._lastSaved = text;
      if (nsTA) {
        nsTA.classList.remove("dirty");
      }
      if (spTA) {
        spTA.classList.remove("dirty");
      }
      ta.classList.remove("dirty");
      const tsNs = document.getElementById(`goal-ns-ts-${projectId}`);
      const tsVg = document.getElementById(`goal-vg-ts-${projectId}`);
      const tsSp = document.getElementById(`goal-sp-ts-${projectId}`);
      const updAt = goal.updated_at ? formatRelativeTime(goal.updated_at) : "";
      if (tsNs) tsNs.textContent = updAt ? `\xB7 ${updAt}` : "";
      if (tsVg) tsVg.textContent = updAt ? `\xB7 ${updAt}` : "";
      if (tsSp) tsSp.textContent = updAt ? `\xB7 ${updAt}` : "";
      renderDecisionsTable(projectId, goal.decisions || "");
      loadPinnedDecisions(projectId);
    } catch (e3) {
      ta.value = "";
      ta.placeholder = "Goal state failed to load.";
      v3.textContent = "(load failed)";
      const titleEl = document.getElementById(`goal-title-${projectId}`);
      if (titleEl) titleEl.textContent = "Goal state unavailable";
    }
  }
  function parseDecisionsBlob(blob) {
    if (!blob || typeof blob !== "string") return [];
    const chunks = blob.split(/\n\s*\n/).map((c3) => c3.trim()).filter(Boolean);
    return chunks.map((chunk) => {
      const m3 = chunk.match(/^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$/s);
      if (m3) return { date: m3[1], text: m3[2].trim() };
      return { date: "", text: chunk };
    });
  }
  var _DECISION_CATEGORY_COLORS = {
    STRATEGIC: "#a78bfa",
    COMPETITIVE: "#f87171",
    TECHNICAL: "#6c8fff",
    TACTICAL: "#fbbf24",
    BUSINESS: "#4ade80",
    PRODUCT: "#22d3ee",
    ARCHITECTURAL: "#fb923c"
  };
  var _DECISION_PRIORITY_COLORS = {
    urgent: "#f87171",
    normal: "#94a3b8",
    low: "#64748b"
  };
  var _DECISION_PRIORITY_ORDER = ["urgent", "normal", "low"];
  function renderConstitutionWarning2(projectId) {
    const host = document.getElementById(`constitution-warning-${projectId}`);
    if (!host) return;
    const items = state.panels[projectId]?._pinnedDecisions || [];
    const count = items.length;
    if (!count) {
      host.innerHTML = "";
      return;
    }
    const limit = getConstitutionLimit(projectId);
    const warn = count >= limit;
    const archiveCount = Math.max(1, count - limit + 1);
    const border = warn ? "#f59e0b" : "var(--border)";
    const fg = warn ? "#fbbf24" : "var(--muted)";
    const tone = warn ? `Constitution has ${count} items \u2014 consider consolidating.` : `Constitution: ${count}/${limit} pinned decisions.`;
    host.innerHTML = `

    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;background:var(--surface-2);border:1px solid ${border};border-radius:4px;padding:8px 10px">

      <div style="font-size:10px;color:${fg}">${escapeHtml(tone)}</div>

      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

        ${warn ? `<button class="secondary" id="constitution-archive-${projectId}" style="padding:2px 8px;font-size:10px">Archive oldest ${archiveCount}</button>` : ""}

        <button class="secondary" id="constitution-consolidate-${projectId}" style="padding:2px 8px;font-size:10px">Consolidate now</button>

      </div>

    </div>`;
    const consolidateBtn = document.getElementById(`constitution-consolidate-${projectId}`);
    if (consolidateBtn) consolidateBtn.onclick = () => consolidateDecisions(projectId);
    const archiveBtn = document.getElementById(`constitution-archive-${projectId}`);
    if (archiveBtn) {
      archiveBtn.onclick = async () => {
        if (!confirm(`Archive the oldest ${archiveCount} pinned decisions?`)) return;
        try {
          await api(`/projects/${projectId}/decisions-pinned/archive-oldest`, {
            method: "POST",
            body: JSON.stringify({ count: archiveCount })
          });
          toast(`Archived ${archiveCount} pinned decision${archiveCount === 1 ? "" : "s"}`);
          loadPinnedDecisions(projectId);
        } catch (e3) {
          toast("archive failed: " + e3.message, true);
        }
      };
    }
  }
  var _HITL_URGENCY_COLOR = {
    blocking: "#f87171",
    // red — session paused, answer now
    high: "#fbbf24",
    // amber — should answer soon
    normal: "#6c8fff"
    // blue — nice-to-have
  };
  var _hitlPollTimer = null;
  function _hitlBadgeClick() {
    const pid = state.activeTab;
    if (pid) {
      const hitlBtn = document.querySelector(`#vtab-strip-${pid} [data-vtab="hitl"]`);
      if (hitlBtn) hitlBtn.click();
    }
    const panel = document.getElementById("hitl-panel");
    const toggleBtn = document.getElementById("hitl-toggle-btn");
    if (panel && panel.style.display === "none") {
      panel.style.display = "block";
      if (toggleBtn) toggleBtn.textContent = "Close";
    }
  }
  function initHitlPanel() {
    const toggleBtn = document.getElementById("hitl-toggle-btn");
    if (toggleBtn) {
      toggleBtn.onclick = () => {
        const panel = document.getElementById("hitl-panel");
        if (!panel) return;
        const isOpen = panel.style.display !== "none";
        panel.style.display = isOpen ? "none" : "block";
        toggleBtn.textContent = isOpen ? "Open" : "Close";
      };
    }
    refreshHitl();
    if (_hitlPollTimer) clearInterval(_hitlPollTimer);
    _hitlPollTimer = setInterval(refreshHitl, 1e4);
  }
  function setVtabCountBadge2(selector, count) {
    document.querySelectorAll(selector).forEach((badge) => {
      badge.textContent = String(count);
      badge.style.display = count > 0 ? "inline-block" : "none";
    });
  }
  async function refreshProjectCountBadges(projectId) {
    if (!projectId) return;
    const [notesRes, pinnedRes] = await Promise.allSettled([
      projectApi(projectId, `/projects/${projectId}/notes`),
      api(`/projects/${projectId}/decisions-pinned`)
    ]);
    if (notesRes.status === "fulfilled") {
      const visible = (notesRes.value || []).filter((n2) => {
        const title = String(n2.title || "").trim().toLowerCase();
        const tags = String(n2.tags || "").split(",").map((t3) => t3.trim().toLowerCase()).filter(Boolean);
        return !title.startsWith("checkpoint:") && !tags.includes("checkpoint");
      });
      setVtabCountBadge2(`.notes-vtab-badge[data-pid="${projectId}"]`, visible.length);
    }
    if (pinnedRes.status === "fulfilled") {
      setVtabCountBadge2(`.decisions-gtab-badge[data-pid="${projectId}"]`, (pinnedRes.value || []).length);
    }
  }
  function _renderAutoAnsweredHitls(answered) {
    const auto = (answered || []).filter((r3) => r3 && r3.answered_by === "auto").slice(0, 5);
    if (!auto.length) return "";
    const rows = auto.map((r3) => {
      const ts = formatRelativeTime(r3.answered_at || r3.created_at);
      return `<div data-hitl-auto-id="${escapeHtml(r3.id)}" style="opacity:0.55;border-left:3px solid var(--muted);background:var(--surface-1);padding:8px 12px;margin-bottom:6px;border-radius:0 4px 4px 0">
        <div style="display:flex;justify-content:space-between;gap:8px;margin-bottom:4px">
          <span style="background:var(--surface-2);color:var(--muted);font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px">AUTO-ANSWERED</span>
          <span style="color:var(--muted);font-size:10px">${escapeHtml(ts)}</span>
        </div>
        <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;font-size:11px;margin-bottom:3px">${escapeHtml(r3.question || "")}</div>
        <div style="color:var(--muted);font-size:11px"><span style="font-weight:600">\u2192</span> ${escapeHtml(r3.answer || "")}</div>
      </div>`;
    }).join("");
    return `<div data-hitl-auto-section style="margin:10px 0 4px;font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Recently auto-answered</div>${rows}`;
  }
  window._renderAutoAnsweredHitls = _renderAutoAnsweredHitls;
  async function refreshHitl(_pid) {
    const bar = document.getElementById("hitl-bar");
    const countEl = document.getElementById("hitl-count");
    const list = document.getElementById("hitl-list");
    if (!bar || !countEl || !list) return;
    try {
      const items = await api("/hitl?status=pending&limit=50");
      const n2 = items.length;
      countEl.textContent = String(n2);
      const perProject = /* @__PURE__ */ new Map();
      for (const r3 of items) {
        const pid = r3 && r3.project_id;
        if (!pid) continue;
        perProject.set(pid, (perProject.get(pid) || 0) + 1);
      }
      document.querySelectorAll(".hitl-vtab-badge").forEach((badge) => {
        const pid = badge.getAttribute("data-pid");
        setVtabCountBadge2(`.hitl-vtab-badge[data-pid="${pid}"]`, perProject.get(pid) || 0);
      });
      if (n2 === 0) {
        bar.style.display = "none";
        document.getElementById("hitl-panel").style.display = "none";
        return;
      }
      bar.style.display = "flex";
      list.innerHTML = items.map((r3) => {
        const color = _HITL_URGENCY_COLOR[r3.urgency] || _HITL_URGENCY_COLOR.normal;
        const ts = formatRelativeTime(r3.created_at);
        const ctx = r3.context ? `<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--muted);font-size:10px">context</summary><pre style="margin:6px 0 0;padding:6px 8px;background:var(--surface-1);border-radius:3px;font-size:11px;white-space:pre-wrap;word-break:break-word">${escapeHtml(r3.context)}</pre></details>` : "";
        const assigned = r3.assigned_to ? `<span style="color:var(--muted);font-size:10px">\u2192 ${escapeHtml(r3.assigned_to)}</span>` : "";
        return `<div data-hitl-id="${escapeHtml(r3.id)}" style="border-left:3px solid ${color};background:var(--surface-1);padding:10px 12px;margin-bottom:8px;border-radius:0 4px 4px 0">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">

          <div style="display:flex;align-items:center;gap:6px;min-width:0;flex:1">

            <span style="background:${color}22;color:${color};font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px">${escapeHtml((r3.urgency || "normal").toUpperCase())}</span>

            ${r3.kind === "correction" ? `<span title="Mid-run correction \u2014 non-blocking" style="background:#f59e0b22;color:#f59e0b;font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px">\u270E CORRECTION</span>` : ""}

            ${assigned}

            <span style="color:var(--muted);font-size:10px">${escapeHtml(ts)}</span>

          </div>

          <div style="display:flex;gap:4px">

            <button class="secondary hitl-dismiss-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:2px 8px;font-size:10px">Dismiss</button>

          </div>

        </div>

        <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5;font-size:12px;margin-bottom:8px">${escapeHtml(r3.question || "")}</div>

        ${ctx}

        <div style="display:flex;gap:6px;margin-top:8px">

          <input type="text" class="hitl-answer-input" data-hitl-id="${escapeHtml(r3.id)}" placeholder="Type answer and hit Enter\u2026" style="flex:1;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:6px 10px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

          <button class="primary hitl-answer-btn" data-hitl-id="${escapeHtml(r3.id)}" style="padding:5px 12px;font-size:11px">Answer</button>

        </div>

      </div>`;
      }).join("");
      list.querySelectorAll(".hitl-answer-btn").forEach((btn) => {
        btn.onclick = () => _hitlAnswer(btn.dataset.hitlId);
      });
      list.querySelectorAll(".hitl-dismiss-btn").forEach((btn) => {
        btn.onclick = () => _hitlDismiss(btn.dataset.hitlId);
      });
      list.querySelectorAll(".hitl-answer-input").forEach((inp) => {
        inp.onkeydown = (ev) => {
          if (ev.key === "Enter") _hitlAnswer(inp.dataset.hitlId);
        };
      });
      try {
        const answered = await api("/hitl?status=answered&limit=10");
        const autoHtml = _renderAutoAnsweredHitls(answered);
        if (autoHtml) list.insertAdjacentHTML("beforeend", autoHtml);
      } catch (e3) {
        console.error("[meridian] auto-answered HITL fetch failed:", e3);
      }
    } catch (e3) {
      console.error("[meridian] refreshHitl failed \u2014 HITL bar may be stale:", e3);
    }
  }
  async function _hitlAnswer(id) {
    const inp = document.querySelector(`.hitl-answer-input[data-hitl-id="${id}"]`);
    const answer = (inp && inp.value || "").trim();
    if (!answer) {
      toast("answer required", true);
      return;
    }
    try {
      await api(`/hitl/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ action: "answer", answer })
      });
      toast("HITL answered");
      _removeHitlCard(id);
      refreshHitl();
    } catch (e3) {
      toast("answer failed: " + e3.message, true);
    }
  }
  async function _hitlDismiss(id) {
    if (!confirm("Dismiss this HITL request without answering?")) return;
    try {
      await api(`/hitl/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ action: "dismiss" })
      });
      _removeHitlCard(id);
      refreshHitl();
    } catch (e3) {
      toast("dismiss failed: " + e3.message, true);
    }
  }
  async function loadPinnedDecisions(projectId, { showArchived = false } = {}) {
    const host = document.getElementById(`pinned-decisions-${projectId}`);
    if (!host) return;
    try {
      await loadProjectSettings2(projectId);
      const url = showArchived ? `/projects/${projectId}/decisions-pinned?include_superseded=true` : `/projects/${projectId}/decisions-pinned`;
      const allItems = await api(url);
      const items = showArchived ? allItems || [] : (allItems || []).filter((d3) => d3.status !== "superseded");
      getPanelState(projectId)._pinnedDecisions = items || [];
      setVtabCountBadge2(`.decisions-gtab-badge[data-pid="${projectId}"]`, (items || []).length);
      renderConstitutionWarning2(projectId);
      if (!items || items.length === 0) {
        host.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">(no pinned decisions yet \u2014 call <code>pin_decision</code> from MCP)</div>`;
        return;
      }
      host.innerHTML = items.map((d3) => {
        const cat = d3.category || "TECHNICAL";
        const color = _DECISION_CATEGORY_COLORS[cat] || _DECISION_CATEGORY_COLORS.TECHNICAL;
        const prio = _DECISION_PRIORITY_ORDER.includes(d3.priority) ? d3.priority : "normal";
        const prioColor = _DECISION_PRIORITY_COLORS[prio] || _DECISION_PRIORITY_COLORS.normal;
        const editCount = Array.isArray(d3.edit_log) ? d3.edit_log.length : 0;
        const dateStr = (d3.created_at || "").slice(0, 10);
        return `<div data-decision-card="${escapeHtml(d3.id)}" style="background:var(--surface-2);border:1px solid var(--border);border-left:4px solid ${color};border-radius:4px;padding:10px 12px;margin-bottom:8px">

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px">

          <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">

            <span class="decision-cat-tag" data-id="${escapeHtml(d3.id)}" data-cat="${escapeHtml(cat)}" title="Click to change category" style="display:inline-block;background:${color}22;color:${color};font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px;flex-shrink:0;cursor:pointer">${escapeHtml(cat)} \u25BE</span>

            <span class="decision-prio-tag" data-id="${escapeHtml(d3.id)}" data-prio="${escapeHtml(prio)}" data-project="${escapeHtml(projectId)}" title="Click to change priority (urgent \u2192 normal \u2192 low)" style="display:inline-block;background:${prioColor}22;color:${prioColor};font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px;flex-shrink:0;cursor:pointer">${prio === "urgent" ? "\u26A1 " : ""}${escapeHtml(prio.toUpperCase())} \u25BE</span>

            <span class="decision-title-view" data-id="${escapeHtml(d3.id)}" title="Click to edit title" style="color:var(--accent);font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer">${escapeHtml(d3.title || "")}</span>

          </div>

          <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">

            ${editCount ? `<span class="decision-edit-count" data-id="${escapeHtml(d3.id)}" title="${editCount} previous ${editCount === 1 ? "revision" : "revisions"} \u2014 body has been edited" style="color:var(--muted);font-size:9px;border:1px solid var(--border);border-radius:3px;padding:1px 5px;cursor:default">\u270E ${editCount}</span>` : ""}

            <span style="color:var(--muted);font-size:10px">${escapeHtml(dateStr)}</span>

            <button class="secondary" data-supersede="${escapeHtml(d3.id)}" style="padding:1px 6px;font-size:9px">Supersede</button>

            <button class="secondary guest-hidden" data-archive-decision="${escapeHtml(d3.id)}" title="Archive this decision (soft-delete; visible via 'View archived')" style="padding:1px 6px;font-size:9px;color:var(--muted)">Archive</button>

          </div>

        </div>

        <div class="decision-body-view" data-id="${escapeHtml(d3.id)}" title="Click to edit" style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5;font-size:12px;cursor:pointer">${escapeHtml(d3.body || "")}</div>

        <div class="decision-edit-area" data-id="${escapeHtml(d3.id)}" style="display:none;margin-top:6px">

          <input type="text" class="decision-edit-title" data-id="${escapeHtml(d3.id)}"

            value="${escapeHtml(d3.title || "")}"

            style="width:100%;padding:4px 6px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;color:var(--text);font-size:12px;font-family:var(--font-mono);outline:none;margin-bottom:4px">

          <textarea class="decision-edit-body" data-id="${escapeHtml(d3.id)}" rows="4"

            style="width:100%;padding:4px 6px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;color:var(--text);font-size:12px;font-family:var(--font-mono);resize:vertical;outline:none">${escapeHtml(d3.body || "")}</textarea>

          <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px">

            <button class="secondary decision-edit-cancel" data-id="${escapeHtml(d3.id)}" style="padding:2px 8px;font-size:10px">Cancel</button>

            <button class="primary decision-edit-save" data-id="${escapeHtml(d3.id)}" data-project="${escapeHtml(projectId)}" style="padding:2px 8px;font-size:10px">Save</button>

          </div>

        </div>

      </div>`;
      }).join("") + `<div id="decisions-view-archived-${escapeHtml(projectId)}" style="margin-top:8px;font-size:10px"></div>`;
      const showEdit = (id) => {
        const card = host.querySelector(`[data-decision-card="${id}"]`);
        if (!card) return;
        card.querySelector(".decision-body-view").style.display = "none";
        card.querySelector(".decision-title-view").style.display = "none";
        card.querySelector(".decision-edit-area").style.display = "block";
      };
      const hideEdit = (id) => {
        const card = host.querySelector(`[data-decision-card="${id}"]`);
        if (!card) return;
        card.querySelector(".decision-body-view").style.display = "";
        card.querySelector(".decision-title-view").style.display = "";
        card.querySelector(".decision-edit-area").style.display = "none";
      };
      host.querySelectorAll(".decision-body-view, .decision-title-view").forEach((el2) => {
        el2.onclick = () => showEdit(el2.dataset.id);
      });
      const _CATS = ["TECHNICAL", "STRATEGIC", "ARCHITECTURAL", "PRODUCT", "TACTICAL", "BUSINESS", "COMPETITIVE"];
      host.querySelectorAll(".decision-cat-tag").forEach((tag) => {
        tag.onclick = (e3) => {
          e3.stopPropagation();
          const id = tag.dataset.id;
          const cur = tag.dataset.cat;
          document.querySelectorAll(".decision-cat-dropdown").forEach((d3) => d3.remove());
          const sel = document.createElement("select");
          sel.className = "decision-cat-dropdown";
          sel.style.cssText = "position:absolute;z-index:9999;background:#1a1a1a;color:#f0f0f0;font-size:10px;font-weight:700;border:1px solid var(--border);border-radius:4px;padding:3px 5px;cursor:pointer";
          _CATS.forEach((c3) => {
            const o3 = document.createElement("option");
            o3.value = c3;
            o3.textContent = c3;
            if (c3 === cur) o3.selected = true;
            sel.appendChild(o3);
          });
          const rect = tag.getBoundingClientRect();
          sel.style.left = rect.left + window.scrollX + "px";
          sel.style.top = rect.bottom + window.scrollY + "px";
          sel.style.minWidth = Math.max(rect.width, 140) + "px";
          document.body.appendChild(sel);
          sel.focus({ preventScroll: true });
          if (typeof sel.showPicker === "function") {
            try {
              sel.showPicker();
            } catch (_2) {
              sel.click();
            }
          } else {
            sel.click();
          }
          sel.onblur = () => sel.remove();
          sel.onchange = async () => {
            const newCat = sel.value;
            sel.remove();
            try {
              const pid = host.closest("[data-project-tab]")?.dataset.projectTab || host.dataset.projectId || "";
              await api(`/projects/${pid}/decisions-pinned/${id}`, { method: "PATCH", body: JSON.stringify({ category: newCat }) });
              await loadPinnedDecisions(pid);
            } catch (err) {
              toast("category update failed: " + err.message, true);
            }
          };
        };
      });
      host.querySelectorAll(".decision-edit-cancel").forEach((btn) => {
        btn.onclick = () => hideEdit(btn.dataset.id);
      });
      host.querySelectorAll(".decision-edit-save").forEach((btn) => {
        btn.onclick = async () => {
          const id = btn.dataset.id;
          const pid = btn.dataset.project;
          const card = host.querySelector(`[data-decision-card="${id}"]`);
          const newTitle = card.querySelector(".decision-edit-title").value.trim();
          const newBody = card.querySelector(".decision-edit-body").value.trim();
          if (!newTitle || !newBody) return toast("title and body required", true);
          try {
            await api(`/projects/${pid}/decisions-pinned/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ title: newTitle, body: newBody })
            });
            toast("decision saved");
            loadPinnedDecisions(pid);
          } catch (e3) {
            toast("save failed: " + e3.message, true);
          }
        };
      });
      host.querySelectorAll("[data-supersede]").forEach((btn) => {
        btn.onclick = () => supersedePinnedDecision(projectId, btn.dataset.supersede);
      });
      host.querySelectorAll(".decision-prio-tag").forEach((tag) => {
        tag.onclick = async () => {
          const id = tag.dataset.id;
          const pid = tag.dataset.project;
          const cur = _DECISION_PRIORITY_ORDER.includes(tag.dataset.prio) ? tag.dataset.prio : "normal";
          const next = _DECISION_PRIORITY_ORDER[(_DECISION_PRIORITY_ORDER.indexOf(cur) + 1) % _DECISION_PRIORITY_ORDER.length];
          try {
            await api(`/projects/${pid}/decisions-pinned/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ priority: next })
            });
            toast(`priority \u2192 ${next}`);
            loadPinnedDecisions(pid);
          } catch (e3) {
            toast("priority change failed: " + e3.message, true);
          }
        };
      });
      host.querySelectorAll("[data-archive-decision]").forEach((btn) => {
        btn.onclick = async () => {
          const id = btn.dataset.archiveDecision;
          try {
            await api(`/projects/${projectId}/decisions-pinned/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ status: "superseded" })
            });
            toast("decision archived");
            loadPinnedDecisions(projectId);
          } catch (e3) {
            toast("archive failed: " + e3.message, true);
          }
        };
      });
      const toggleEl = document.getElementById(`decisions-view-archived-${projectId}`);
      if (toggleEl) {
        if (showArchived) {
          const archivedCount = (allItems || []).filter((d3) => d3.status === "superseded").length;
          toggleEl.innerHTML = `<button class="secondary" style="padding:2px 8px;font-size:10px" onclick="loadPinnedDecisions('${escapeHtml(projectId)}', {showArchived:false})">\u2190 Hide archived</button> <span style="color:var(--muted)">${archivedCount} archived</span>`;
        } else {
          api(`/projects/${projectId}/decisions-pinned?include_superseded=true`).then((all) => {
            const n2 = (all || []).filter((d3) => d3.status === "superseded").length;
            const el2 = document.getElementById(`decisions-view-archived-${projectId}`);
            if (el2) el2.innerHTML = n2 > 0 ? `<button class="secondary" style="padding:2px 8px;font-size:10px" onclick="loadPinnedDecisions('${escapeHtml(projectId)}', {showArchived:true})">View archived (${n2}) \u25B8</button>` : "";
          }).catch(() => {
          });
        }
      }
      let supersededEl = document.getElementById(`superseded-decisions-${projectId}`);
      if (!supersededEl) {
        supersededEl = document.createElement("div");
        supersededEl.id = `superseded-decisions-${projectId}`;
        host.parentElement.insertBefore(supersededEl, host.nextSibling);
      }
      try {
        const all = await api(`/projects/${projectId}/decisions-pinned?include_superseded=true`);
        const superseded = (all || []).filter((d3) => d3.status === "superseded");
        if (superseded.length > 0) {
          supersededEl.innerHTML = `<details style="margin-top:8px;margin-bottom:6px">

          <summary style="cursor:pointer;color:var(--muted);font-size:10px;font-family:var(--font-mono);letter-spacing:.05em;user-select:none">

            Superseded (${superseded.length})

          </summary>

          <div style="margin-top:6px">

            ${superseded.map((d3) => {
            const cat = d3.category || "TECHNICAL";
            const color = _DECISION_CATEGORY_COLORS[cat] || _DECISION_CATEGORY_COLORS.TECHNICAL;
            const dateStr = (d3.created_at || "").slice(0, 10);
            return `<div style="background:var(--surface-1);border:1px solid var(--border);border-left:4px solid ${color}55;border-radius:4px;padding:8px 12px;margin-bottom:6px;opacity:0.6">

                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">

                  <span style="display:inline-block;background:${color}11;color:${color}88;font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px">${escapeHtml(cat)}</span>

                  <span style="color:var(--muted);font-weight:600;font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(d3.title || "")}">${escapeHtml(d3.title || "")}</span>

                  <span style="color:var(--muted);font-size:9px;flex-shrink:0">${escapeHtml(dateStr)}</span>

                  <span style="background:var(--surface-2);color:var(--muted);font-size:8px;font-weight:700;padding:1px 5px;border-radius:3px;letter-spacing:.04em;flex-shrink:0">SUPERSEDED</span>

                </div>

                <div style="color:var(--muted);font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.5">${escapeHtml((d3.body || "").slice(0, 200))}</div>

              </div>`;
          }).join("")}

          </div>

        </details>`;
        } else {
          supersededEl.innerHTML = "";
        }
      } catch (_2) {
      }
    } catch (e3) {
      if (state.panels[projectId]) state.panels[projectId]._pinnedDecisions = [];
      renderConstitutionWarning2(projectId);
      host.innerHTML = `<div style="color:var(--muted)">failed to load pinned decisions: ${escapeHtml(String(e3))}</div>`;
    }
  }
  async function supersedePinnedDecision(projectId, decisionId) {
    const newTitle = prompt("New decision title (replaces this one):");
    if (!newTitle) return;
    const newBody = prompt("New decision body:");
    if (!newBody) return;
    try {
      await api(`/projects/${projectId}/decisions-pinned/${decisionId}`, {
        method: "PATCH",
        body: JSON.stringify({ new_title: newTitle, new_body: newBody })
      });
      toast("decision superseded");
      loadPinnedDecisions(projectId);
    } catch (e3) {
      toast("supersede failed: " + e3.message, true);
    }
  }
  async function addPinnedDecision(projectId) {
    const title = prompt("Decision title:");
    if (!title) return;
    const body = prompt("Decision body:");
    if (!body) return;
    const category = prompt("Category (STRATEGIC/COMPETITIVE/TECHNICAL/TACTICAL/BUSINESS/PRODUCT/ARCHITECTURAL):", "TECHNICAL");
    if (!category) return;
    try {
      await api(`/projects/${projectId}/decisions-pinned`, {
        method: "POST",
        body: JSON.stringify({ title, body, category: category.toUpperCase() })
      });
      toast("decision pinned");
      loadPinnedDecisions(projectId);
    } catch (e3) {
      toast("pin failed: " + e3.message, true);
    }
  }
  async function consolidateDecisions(projectId) {
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9998;display:flex;align-items:center;justify-content:center";
    overlay.innerHTML = `

    <div style="background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:20px;width:480px;max-width:92vw;display:flex;flex-direction:column;gap:12px;box-shadow:0 8px 32px #0008">

      <div style="font-weight:600;font-size:13px;color:var(--accent)">\u2728 AI Decision Consolidation</div>

      <div style="font-size:11px;color:var(--muted)">Sends your pinned decisions to an LLM to deduplicate and merge. Preview before applying. API key is never stored.</div>

      <div>

        <div style="font-size:10px;color:var(--muted);margin-bottom:4px;font-family:var(--font-mono)">MODEL</div>

        <select id="_consolidate-model" style="width:100%;padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:4px;font-family:var(--font-mono);font-size:11px">

          <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5 (fast, cheap)</option>

          <option value="claude-sonnet-4-6">Claude Sonnet 4.6 (better)</option>

          <option value="gpt-4o-mini">GPT-4o mini</option>

          <option value="gpt-4o">GPT-4o</option>

          <option value="deepseek-chat">DeepSeek Chat</option>

        </select>

      </div>

      <div>

        <div style="font-size:10px;color:var(--muted);margin-bottom:4px;font-family:var(--font-mono)">API KEY</div>

        <input id="_consolidate-key" type="password" placeholder="sk-ant-... / sk-... / sk-..." style="width:100%;padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:4px;font-family:var(--font-mono);font-size:11px;outline:none">

      </div>

      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">

        <button class="secondary" id="_consolidate-cancel" style="padding:5px 14px;font-size:11px">Cancel</button>

        <button class="primary" id="_consolidate-run" style="padding:5px 14px;font-size:11px">Consolidate \u2192</button>

      </div>

    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e3) => {
      if (e3.target === overlay) overlay.remove();
    });
    document.getElementById("_consolidate-cancel").onclick = () => overlay.remove();
    document.getElementById("_consolidate-run").onclick = async () => {
      const apiKey = document.getElementById("_consolidate-key").value.trim();
      const model = document.getElementById("_consolidate-model").value;
      if (!apiKey) {
        toast("API key required", true);
        return;
      }
      const runBtn = document.getElementById("_consolidate-run");
      runBtn.textContent = "Working\u2026";
      runBtn.disabled = true;
      try {
        const result = await api(`/projects/${projectId}/decisions/consolidate`, {
          method: "POST",
          body: JSON.stringify({ api_key: apiKey, model })
        });
        overlay.remove();
        const consolidated = result.consolidated || [];
        const previewOverlay = document.createElement("div");
        previewOverlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9999;display:flex;align-items:center;justify-content:center";
        const previewHtml = consolidated.map((d3) => {
          const cat = (d3.category || "TECHNICAL").toUpperCase();
          const color = _DECISION_CATEGORY_COLORS[cat] || _DECISION_CATEGORY_COLORS.TECHNICAL;
          return `<div style="background:var(--surface-2);border-left:4px solid ${color};border-radius:4px;padding:8px 10px;margin-bottom:8px">

          <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">

            <span style="background:${color}22;color:${color};font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px">${escapeHtml(cat)}</span>

            <span style="color:var(--accent);font-weight:600;font-size:12px">${escapeHtml(d3.title || "")}</span>

          </div>

          <div style="color:var(--text);font-size:11px;white-space:pre-wrap">${escapeHtml(d3.body || "")}</div>

        </div>`;
        }).join("");
        previewOverlay.innerHTML = `

        <div style="background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:20px;width:620px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;gap:12px;box-shadow:0 8px 32px #0008">

          <div style="font-weight:600;font-size:13px;color:var(--accent)">Preview \u2014 ${consolidated.length} decisions (was ${result.original_count})</div>

          <div style="flex:1;overflow-y:auto;font-family:var(--font-mono)">${previewHtml}</div>

          <div style="color:var(--muted);font-size:10px">This will supersede all ${result.original_count} existing decisions and create ${consolidated.length} new ones.</div>

          <div style="display:flex;gap:8px;justify-content:flex-end">

            <button class="secondary" id="_preview-cancel" style="padding:5px 14px;font-size:11px">Cancel</button>

            <button class="primary" id="_preview-apply" style="padding:5px 14px;font-size:11px">Apply \u2192</button>

          </div>

        </div>`;
        document.body.appendChild(previewOverlay);
        previewOverlay.addEventListener("click", (e3) => {
          if (e3.target === previewOverlay) previewOverlay.remove();
        });
        document.getElementById("_preview-cancel").onclick = () => previewOverlay.remove();
        document.getElementById("_preview-apply").onclick = async () => {
          const applyBtn = document.getElementById("_preview-apply");
          applyBtn.textContent = "Applying\u2026";
          applyBtn.disabled = true;
          try {
            await api(`/projects/${projectId}/decisions-pinned/replace-all`, {
              method: "POST",
              body: JSON.stringify({ decisions: consolidated })
            });
            previewOverlay.remove();
            toast(`Consolidated: ${consolidated.length} decisions applied`);
            loadPinnedDecisions(projectId);
          } catch (e3) {
            toast("Apply failed: " + e3.message, true);
            applyBtn.textContent = "Apply \u2192";
            applyBtn.disabled = false;
          }
        };
      } catch (e3) {
        runBtn.textContent = "Consolidate \u2192";
        runBtn.disabled = false;
        toast("Consolidation failed: " + e3.message, true);
      }
    };
  }
  function renderDecisionsTable(projectId, blob) {
    const host = document.getElementById(`decisions-table-${projectId}`);
    if (!host) return;
    const rows = parseDecisionsBlob(blob);
    if (rows.length === 0) {
      host.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">(no decisions logged yet \u2014 call <code>set_decision</code> from MCP to record one)</div>`;
      return;
    }
    const html = rows.map((row) => {
      const date = row.date ? escapeHtml(row.date) : "\u2014";
      return `<div style="display:grid;grid-template-columns:96px 1fr;gap:10px;padding:8px 10px;border-left:3px solid var(--accent);background:var(--surface-2);border-radius:0 4px 4px 0;margin-bottom:6px;align-items:start">

      <div style="color:var(--accent);font-weight:600;white-space:nowrap">${date}</div>

      <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5">${escapeHtml(row.text)}</div>

    </div>`;
    }).join("");
    host.innerHTML = html;
  }
  function wireGoalPreviewToggle(taEl, previewEl) {
    if (!taEl || !previewEl) return;
    const row = document.createElement("div");
    row.className = "preview-toggle-row";
    row.innerHTML = `<button class="preview-btn active" data-mode="edit">edit</button><button class="preview-btn"        data-mode="preview">preview</button>`;
    taEl.parentNode.insertBefore(row, taEl);
    row.querySelectorAll(".preview-btn").forEach((btn) => {
      btn.onclick = () => {
        const mode = btn.dataset.mode;
        row.querySelectorAll(".preview-btn").forEach((b2) => {
          b2.classList.toggle("active", b2.dataset.mode === mode);
        });
        if (mode === "preview") {
          const md = taEl.value || "";
          const html = typeof marked !== "undefined" ? marked.parse(md) : escapeHtml(md);
          previewEl.innerHTML = html;
          taEl.style.display = "none";
          previewEl.style.display = "";
        } else {
          previewEl.style.display = "none";
          taEl.style.display = "";
        }
      };
    });
  }
  async function saveGoal(projectId) {
    const ta = document.getElementById(`goal-${projectId}`);
    if (!ta) return;
    const autoBlocksEl = document.getElementById(`goal-autoblocks-${projectId}`);
    const autoBlocksText = autoBlocksEl && autoBlocksEl.style.display !== "none" ? "\n--- AUTO BLOCKS BELOW ---\n" + autoBlocksEl.textContent : "";
    const titleEl = document.getElementById(`goal-title-${projectId}`);
    const titleLine = titleEl && titleEl.textContent ? titleEl.textContent + "\n" : "";
    const shippedEl2 = document.getElementById(`goal-shipped-${projectId}`);
    const shippedText = shippedEl2 && shippedEl2.style.display !== "none" && shippedEl2.textContent ? "\n" + shippedEl2.textContent + "\n" : "";
    const raw = titleLine + shippedText + ta.value + autoBlocksText;
    if (raw === state.panels[projectId]._lastSaved) return;
    let content = raw;
    if (state.panels[projectId].goalIsJson) {
      try {
        content = JSON.parse(raw);
      } catch (e3) {
      }
    }
    try {
      await api(`/projects/${projectId}/goal`, { method: "POST", body: JSON.stringify({ content }) });
      state.panels[projectId]._lastSaved = raw;
      toast("version goal saved");
      refreshGoal(projectId);
    } catch (e3) {
      toast("save failed: " + e3.message, true);
    }
  }
  async function saveNorthStar(projectId) {
    const ta = document.getElementById(`goal-north-star-${projectId}`);
    if (!ta) return;
    const val = ta.value.trim();
    if (!val) return;
    const saved = state.panels[projectId]?._serverNorthStar || "";
    if (saved && val !== saved && !confirm("North star is intended to be stable. Save changes?")) {
      ta.value = saved;
      autosizeGoalField(ta);
      ta.classList.remove("dirty");
      return;
    }
    try {
      const humanInput = document.getElementById("new-project-human");
      const humanId = humanInput ? humanInput.value.trim() : "";
      await api(`/projects/${projectId}/goal/north-star`, {
        method: "POST",
        body: JSON.stringify({ north_star: val, human_id: humanId || "owner" })
      });
      toast("north star saved");
      refreshGoal(projectId);
    } catch (e3) {
      toast("save failed: " + e3.message, true);
    }
  }
  async function saveSprint(projectId) {
    const ta = document.getElementById(`goal-sprint-${projectId}`);
    const sel = document.getElementById(`goal-sprint-select-${projectId}`);
    if (!ta) return;
    const rawVal = ta.style.display === "none" && sel && sel.value && sel.value !== "__custom__" ? sel.value : ta.value;
    const val = rawVal.trim();
    if (!val) return;
    try {
      await api(`/projects/${projectId}/goal/sprint`, {
        method: "POST",
        body: JSON.stringify({ sprint: val })
      });
      toast("sprint saved");
      refreshGoal(projectId);
    } catch (e3) {
      toast("save failed: " + e3.message, true);
    }
  }
  function _sessionPresenceDot(last_seen) {
    if (!last_seen) return "\u26AB";
    const mins = (Date.now() - (/* @__PURE__ */ new Date(last_seen.replace(" ", "T") + "Z")).getTime()) / 6e4;
    if (mins < 6) return "\u{1F7E2}";
    if (mins < 30) return "\u{1F7E1}";
    return "\u26AB";
  }
  async function refreshSessions(projectId) {
    const root = document.getElementById(`sessions-${projectId}`);
    if (!root) return;
    const sessionsPath = `/projects/${projectId}/sessions`;
    try {
      const sessions = await projectApi(projectId, sessionsPath);
      populateSessionDropdown(projectId, sessions);
      if (!sessions.length) {
        root.innerHTML = '<div class="session-row meta">(no active sessions)</div>';
        return;
      }
      const groups = {};
      const order = [];
      for (const s3 of sessions) {
        const h3 = s3.human_id || "\0unknown";
        if (!groups[h3]) {
          groups[h3] = [];
          order.push(h3);
        }
        groups[h3].push(s3);
      }
      for (const h3 of order) {
        groups[h3] = sortSessionsMostRecentFirst(groups[h3]);
      }
      const rows = order.map((h3) => {
        const humanSessions = groups[h3];
        const label = h3 === "\0unknown" ? humanSessions.length === 1 ? humanSessions[0].name : "unknown" : h3;
        const topDot = _sessionPresenceDot(humanSessions[0]?.last_seen);
        const header = `<div class="session-row" style="font-weight:600;padding-top:4px"><span class="name">${topDot} ${escapeHtml(label)}</span><span class="meta">${humanSessions.length} session${humanSessions.length > 1 ? "s" : ""}</span></div>`;
        const children = humanSessions.map((s3) => {
          let ageMs = 0;
          try {
            const ts = s3.last_seen ? s3.last_seen.replace(" ", "T") + "Z" : "";
            if (ts) ageMs = Date.now() - new Date(ts).getTime();
          } catch (e3) {
          }
          const ageH = ageMs / 36e5;
          const opacity = ageH < 1 ? 1 : ageH < 24 ? 0.7 : 0.4;
          const clientBadge = s3.client_type ? `<span style="font-size:9px;color:var(--muted);margin-left:4px">${escapeHtml(s3.client_type)}</span>` : "";
          return `<div class="session-row" style="opacity:${opacity};padding-left:18px;font-size:11px"><span class="name">${escapeHtml(s3.name)}${clientBadge}</span><span class="meta">${escapeHtml(s3.status)} \xB7 ${escapeHtml(formatRelativeTime(s3.last_seen))}</span></div>`;
        }).join("");
        return header + children;
      }).join("");
      root.innerHTML = rows;
    } catch (e3) {
      root.innerHTML = renderProjectLoadError2(projectId, "Sessions unavailable", sessionsPath, e3);
      wireProjectLoadRetry2(root, projectId);
    }
  }
  async function refreshTasks(projectId) {
    const tasksPath = `/projects/${projectId}/tasks?limit=100`;
    try {
      const tasks = await projectApi(projectId, tasksPath);
      state.panels[projectId].taskCache = tasks;
      state.panels[projectId].taskOffset = tasks.length;
      renderTasks(projectId);
    } catch (e3) {
      const root = document.getElementById(`tasks-${projectId}`);
      const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);
      const banner = document.getElementById(`hitl-banner-${projectId}`);
      if (banner) banner.style.display = "none";
      if (hitlRoot) hitlRoot.innerHTML = "";
      if (root) {
        root.innerHTML = renderProjectLoadError2(projectId, "Task log unavailable", tasksPath, e3);
        wireProjectLoadRetry2(root, projectId);
      }
    }
  }
  function renderTasks(projectId) {
    const tasks = state.panels[projectId].taskCache || [];
    const root = document.getElementById(`tasks-${projectId}`);
    const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);
    const banner = document.getElementById(`hitl-banner-${projectId}`);
    if (!root || !hitlRoot) return;
    const hitl = tasks.filter((t3) => t3.status === "pending-hitl");
    banner.style.display = hitl.length ? "block" : "none";
    hitlRoot.innerHTML = hitl.map((t3) => renderHitlRow(projectId, t3)).join("");
    hitl.forEach((t3) => wireHitlRow(projectId, t3));
    root.innerHTML = tasks.map((t3) => renderTaskRow(t3)).join("");
    _wireTabSearch(`devlog-search-${projectId}`, `tasks-${projectId}`, ".task");
    const existingBtn = document.getElementById(`devlog-load-more-${projectId}`);
    if (existingBtn) existingBtn.remove();
    if (tasks.length === 100) {
      const btn = document.createElement("button");
      btn.id = `devlog-load-more-${projectId}`;
      btn.className = "secondary";
      btn.style = "width:100%;margin-top:8px;padding:5px;font-size:11px;font-family:var(--font-mono)";
      btn.textContent = "Load 100 more \u2193";
      btn.onclick = () => _loadMoreTasks(projectId, btn);
      root.parentElement.appendChild(btn);
    }
  }
  async function _loadMoreTasks(projectId, btn) {
    const p3 = state.panels[projectId];
    const offset = p3.taskOffset || 0;
    btn.disabled = true;
    btn.textContent = "loading\u2026";
    try {
      const more = await api(`/projects/${projectId}/tasks?limit=100&offset=${offset}`);
      p3.taskCache = [...p3.taskCache || [], ...more];
      p3.taskOffset = offset + more.length;
      const root = document.getElementById(`tasks-${projectId}`);
      if (root) root.innerHTML += more.map((t3) => renderTaskRow(t3)).join("");
      if (more.length < 100) {
        btn.remove();
      } else {
        btn.disabled = false;
        btn.textContent = "Load 100 more \u2193";
      }
    } catch (e3) {
      btn.disabled = false;
      btn.textContent = "Load 100 more \u2193 (retry)";
    }
  }
  function renderTaskRow(t3) {
    const claimBadge = t3.claimed_by ? `<span class="claim-badge" title="claimed at ${escapeHtml(t3.claimed_at || "")}">U0001f512 ${escapeHtml((t3.claimed_by_human_id || t3.claimed_by_session_name || t3.claimed_by || "").slice(0, 16))}</span>` : "";
    const deleteBtn = `<button class="guest-hidden" title="Delete from task log (permanent)" onclick="deleteTaskRow(event,'${t3.id}','${t3.status}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0 4px;flex-shrink:0;line-height:1" onmouseenter="this.style.color='var(--status-failed)'" onmouseleave="this.style.color='var(--muted)'">\xD7</button>`;
    return `

    <div class="task ${t3.status}" id="task-row-${t3.id}" data-search="${escapeHtml((t3.description || "") + " " + (t3.session_name || "") + " " + (t3.claimed_by_session_name || "") + " " + (t3.status || ""))}" style="display:flex;align-items:flex-start;gap:4px">

      <span class="status-badge">${t3.status}</span>

      <div style="flex:1;min-width:0">

        <div class="desc">${escapeHtml(t3.description)}</div>

        <div class="meta">${escapeHtml(t3.created_at)} ${claimBadge}</div>

      </div>

      ${deleteBtn}

    </div>`;
  }
  async function deleteTaskRow(e3, taskId, status) {
    e3.stopPropagation();
    const warn = status === "pending" || status === "in_progress" ? "This task is " + status + ". Deleting it is permanent. Continue?" : "Permanently delete this task from the log?";
    if (!confirm(warn)) return;
    try {
      await api("/tasks/" + taskId, { method: "DELETE" });
      const row = document.getElementById("task-row-" + taskId);
      if (row) row.remove();
    } catch (e22) {
      console.error("Delete failed:", e22);
    }
  }
  function renderHitlRow(projectId, t3) {
    const isExecute = t3.description.startsWith("[EXECUTE]");
    const label = isExecute ? "EXECUTE REQUEST" : "QUESTION";
    const body = t3.description.replace(/^\[(ASK|EXECUTE)\]:?\s*/, "");
    if (isExecute) {
      return `

      <div class="hitl-row" data-task="${t3.id}">

        <div class="prompt"><strong>${label}</strong> \xB7 ${escapeHtml(body)}</div>

        <div class="controls">

          <button class="execute" data-action="confirm" data-task="${t3.id}">EXECUTE</button>

          <button class="danger"  data-action="reject"  data-task="${t3.id}">REJECT</button>

        </div>

      </div>`;
    }
    return `

    <div class="hitl-row" data-task="${t3.id}">

      <div class="prompt"><strong>${label}</strong> \xB7 ${escapeHtml(body)}</div>

      <div class="controls">

        <input type="text" placeholder="your reply" data-input="${t3.id}">

        <button class="primary" data-action="reply" data-task="${t3.id}">reply</button>

      </div>

    </div>`;
  }
  function wireHitlRow(projectId, t3) {
    const row = document.querySelector(`#hitl-queue-${projectId} [data-task="${t3.id}"]`);
    if (!row) return;
    row.querySelectorAll("button[data-action]").forEach((btn) => {
      btn.onclick = () => {
        const action = btn.dataset.action;
        if (action === "reply") {
          const inp = row.querySelector(`input[data-input="${t3.id}"]`);
          const text = (inp && inp.value || "").trim();
          if (!text) {
            toast("enter a reply first", true);
            return;
          }
          hitlReply(projectId, t3.id, text);
        } else if (action === "confirm") {
          hitlExecute(projectId, t3.id, true);
        } else if (action === "reject") {
          hitlExecute(projectId, t3.id, false);
        }
      };
    });
  }
  async function appendToGoal(projectId, line) {
    let current = "";
    try {
      const goal = await api(`/projects/${projectId}/goal`);
      current = typeof goal.content === "string" ? goal.content : JSON.stringify(goal.content, null, 2);
    } catch (e3) {
    }
    const next = current ? current.trimEnd() + "\n" + line : line;
    await api(`/projects/${projectId}/goal`, { method: "POST", body: JSON.stringify({ content: next }) });
  }
  async function hitlReply(projectId, taskId, text) {
    try {
      await appendToGoal(projectId, `[HITL-REPLY:${taskId}:] ${text}`);
      await api(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ status: "done", description: `[ANSWERED] ${text}` }) });
      toast("reply sent");
    } catch (e3) {
      toast("reply failed: " + e3.message, true);
    }
  }
  async function hitlExecute(projectId, taskId, confirmed) {
    try {
      if (confirmed) {
        await appendToGoal(projectId, `[EXECUTE-CONFIRMED:${taskId}]`);
        await api(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ status: "done" }) });
        toast("execute confirmed");
      } else {
        await appendToGoal(projectId, `[EXECUTE-REJECTED:${taskId}]`);
        await api(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ status: "failed" }) });
        toast("execute rejected");
      }
    } catch (e3) {
      toast("execute failed: " + e3.message, true);
    }
  }
  function connectWs(projectId) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/${projectId}`);
    const dot = document.getElementById(`ws-${projectId}`);
    ws.onopen = () => {
      dot && dot.classList.add("connected");
    };
    ws.onclose = () => {
      dot && dot.classList.remove("connected");
      setTimeout(() => {
        if (state.panels[projectId]) connectWs(projectId);
      }, 1500);
    };
    ws.onerror = () => {
      dot && dot.classList.remove("connected");
    };
    ws.onmessage = (ev) => {
      try {
        const event = JSON.parse(ev.data);
        handleWsEvent(projectId, event);
      } catch (e3) {
      }
    };
    state.panels[projectId].ws = ws;
  }
  function handleWsEvent(projectId, event) {
    if (event.type === "update_available") {
      if (isDemoMode()) {
        hideDemoAdminControls();
        return;
      }
      const banner = document.getElementById("update-banner");
      if (banner) banner.style.display = "block";
      return;
    }
    if (event.type === "project_renamed") {
      const tab = state.tabs.find((t3) => t3.id === event.project_id);
      if (tab) {
        tab.project = { ...tab.project, name: event.name };
        const hdr = document.querySelector(`#drawer-status-${event.project_id} .drawer-header span:first-child`);
        if (hdr) hdr.textContent = "STATUS \xB7 " + event.name;
        renderTabs();
      }
      const proj = state.projects.find((p3) => p3.id === event.project_id);
      if (proj) {
        proj.name = event.name;
        loadProjects();
      }
      return;
    }
    if (event.type === "sprint_item_updated") {
      const panel2 = state.panels[projectId];
      if (panel2 && panel2.activeVtab === "queue") loadQueue(projectId);
      scheduleLiveRefresh(projectId);
      return;
    }
    if (event.type === "goal_updated") {
      refreshGoal(projectId);
      return;
    }
    if (event.type === "session_started") {
      const panel2 = state.panels[projectId];
      if (panel2 && panel2.activeVtab === "queue") loadQueue(projectId);
      scheduleLiveRefresh(projectId);
      return;
    }
    if (event.type === "sprint_item_added") {
      const panel2 = state.panels[projectId];
      if (panel2 && panel2.activeVtab === "queue") loadQueue(projectId);
      scheduleLiveRefresh(projectId);
      refreshProjectCountBadges(projectId);
      return;
    }
    if (event.type === "note_added") {
      const panel2 = state.panels[projectId];
      if (panel2 && panel2.activeVtab === "notes") loadNotesTab(projectId);
      refreshProjectCountBadges(projectId);
      return;
    }
    if (event.type === "decision_pinned") {
      if (state.panels[projectId]) loadPinnedDecisions(projectId);
      refreshProjectCountBadges(projectId);
      return;
    }
    if (event.type === "hitl_filed") {
      refreshHitl();
      refreshProjectCountBadges(projectId);
      const _hitlPanel = state.panels[projectId];
      if (_hitlPanel && _hitlPanel.activeVtab === "hitl") loadHitlTab(projectId);
      return;
    }
    const cache = state.panels[projectId].taskCache;
    if (event.type === "task_created") {
      cache.unshift(event.task);
    } else if (event.type === "task_updated") {
      const i3 = cache.findIndex((t3) => t3.id === event.task.id);
      if (i3 >= 0) cache[i3] = event.task;
      else cache.unshift(event.task);
    }
    renderTasks(projectId);
    refreshGoal(projectId);
    const panel = state.panels[projectId];
    if (panel && panel.activeVtab === "queue" && (event.type === "task_created" || event.type === "task_updated")) {
      updateLiveFeed(projectId);
    }
    if (panel && panel.activeVtab === "live") {
      scheduleLiveRefresh(projectId);
    }
  }
  document.getElementById("new-project-btn").onclick = async () => {
    const inp = document.getElementById("new-project-name");
    const humanInp = document.getElementById("new-project-human");
    const name = inp.value.trim();
    if (!name) return;
    const body = { name };
    const humanId = (humanInp && humanInp.value || "").trim();
    if (humanId) body.human_id = humanId;
    try {
      const p3 = await api("/projects", { method: "POST", body: JSON.stringify(body) });
      inp.value = "";
      if (humanInp) humanInp.value = "";
      await loadProjects();
      openTab(p3);
    } catch (e3) {
      toast("create failed: " + e3.message, true);
    }
  };
  {
    const switcher = document.getElementById("project-switcher");
    if (switcher) {
      switcher.addEventListener("change", (ev) => {
        const id = ev.target.value;
        if (!id) return;
        const p3 = state.projects.find((x2) => x2.id === id);
        if (p3) openTab(p3);
      });
    }
  }
  async function restoreTabs() {
    let saved = [];
    let preferred = null;
    try {
      preferred = localStorage.getItem(STORAGE_KEY2(ACTIVE_PROJECT_KEY));
    } catch (e3) {
    }
    try {
      saved = JSON.parse(localStorage.getItem(STORAGE_KEY2(TABS_KEY)) || "[]");
    } catch (e3) {
    }
    for (const id of saved) {
      const p3 = state.projects.find((x2) => x2.id === id);
      if (p3) openTab(p3);
    }
    if (state.tabs.length === 0 && state.projects.length > 0) {
      const fallback = state.projects.find((p3) => p3.id === preferred) || state.projects[0];
      if (fallback) openTab(fallback);
    } else if (preferred && state.tabs.find((t3) => t3.id === preferred)) {
      activateTab(preferred);
    }
  }
  (async function init() {
    const _wsParam = new URLSearchParams(window.location.search).get("ws");
    if (_wsParam && !state.activeWorkspaceTenantId) {
      state.activeWorkspaceTenantId = _wsParam;
      try {
        history.replaceState(null, "", window.location.pathname);
      } catch (_2) {
      }
    }
    await loadServerConfig();
    showFailoverBannerIfNeeded();
    if (typeof window._showConnSetupIfNeeded === "function") {
      window._showConnSetupIfNeeded(state.serverConfig);
    }
    await loadConfig();
    await loadProjects();
    if (state.projects.length === 0 && !state.activeWorkspaceTenantId && isHostedMode() && !isDemoMode()) {
      try {
        const wss = await fetch("/me/workspaces").then((r3) => r3.ok ? r3.json() : null);
        const first = wss && wss.find((w3) => !w3.is_own);
        if (first) {
          state.activeWorkspaceTenantId = first.tenant_id;
          await loadProjects();
        }
      } catch (_2) {
      }
    }
    if (isDemoMode()) hideDemoAdminControls();
    if (isHostedMode()) hideHostedAdminControls();
    if (isHostedMode() && !isDemoMode()) ensureWorkspaceSwitcher2();
    _refreshGuestMode();
    showLocalServerControls();
    ensureTourButton();
    ensureFeedbackButton();
    if (state.projects.length === 0 && !isDemoMode()) {
      document.getElementById("ez-wizard").style.display = "flex";
      return;
    }
    await restoreTabs();
    const dashboardParams = new URLSearchParams(window.location.search);
    const requestedProjectId = dashboardParams.get("project_id") || "";
    const requestedTab = dashboardParams.get("tab") || "";
    if (requestedProjectId) {
      const requestedProject = state.projects.find((p3) => p3.id === requestedProjectId);
      if (requestedProject) {
        openTab(requestedProject);
        if (requestedTab && requestedTab !== "status") {
          setTimeout(() => {
            document.querySelector(`#vtab-strip-${requestedProject.id} .vtab-btn[data-vtab="${requestedTab}"]`)?.click();
          }, 0);
        }
      }
    }
    initHitlPanel();
    if (!isHostedMode()) {
      const stopBtn = document.getElementById("stop-server-btn");
      if (stopBtn) {
        stopBtn.onclick = async () => {
          if (!confirm("Stop the Meridian server? You will need to run `pixi run start` to restart.")) return;
          try {
            await api("/admin/shutdown", { method: "POST" });
            stopBtn.textContent = "Stopped \u2014 run pixi run start";
            stopBtn.disabled = true;
            const restartBtn2 = document.getElementById("restart-server-btn");
            if (restartBtn2) restartBtn2.style.display = "none";
          } catch (e3) {
            toast("Shutdown request sent.", false);
          }
        };
      }
      const restartBtn = document.getElementById("restart-server-btn");
      if (restartBtn) {
        restartBtn.onclick = async () => {
          await _doRestart();
        };
      }
    }
    const bannerRestartBtn = document.getElementById("banner-restart-btn");
    if (bannerRestartBtn) {
      bannerRestartBtn.onclick = async () => {
        await _doRestart();
      };
    }
    async function _checkGitStatus() {
      try {
        const data = await api("/admin/git-status");
        const banner = document.getElementById("git-banner");
        const msg = document.getElementById("git-banner-msg");
        if (banner && data.warning) {
          if (msg) msg.textContent = data.warning;
          banner.style.display = "block";
        }
      } catch (_2) {
      }
    }
    _checkGitStatus();
    setInterval(_checkGitStatus, 6e4);
    const workspaceEntry = document.getElementById("workspace-entry");
    if (workspaceEntry) {
      workspaceEntry.onclick = () => {
        const targetId = state.activeTab || state.projects[0]?.id;
        const project = state.projects.find((p3) => p3.id === targetId);
        if (!project) return;
        if (!document.getElementById(`tab-body-${targetId}`)) openTab(project);
        document.querySelector(`#vtab-strip-${targetId} [data-vtab="settings"]`)?.click();
      };
    }
  })();
  var _sprintBoardReloaders = {};
  var _sprintSelectSyncers = {};
  async function _deleteSprintItem(projectId, itemId) {
    if (!confirm("Remove this sprint item?")) return;
    try {
      await api(`/projects/${projectId}/sprint-items/${itemId}`, { method: "DELETE" });
      if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
    } catch (e3) {
      console.error("Delete sprint item failed:", e3);
    }
  }
  async function _sprintAction(projectId, itemId, action) {
    try {
      await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`, { method: "POST" });
      if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
    } catch (e3) {
      console.error("Sprint action failed:", action, e3);
    }
  }
  async function completeSprintItem(projectId, itemId) {
    try {
      await api(`/projects/${projectId}/sprint-items/${itemId}/complete`, { method: "POST" });
      if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
    } catch (e3) {
      console.error("Complete sprint item failed:", e3);
    }
  }
  async function failSprintItem(projectId, itemId) {
    try {
      await api(`/projects/${projectId}/sprint-items/${itemId}/fail`, { method: "POST" });
      if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();
    } catch (e3) {
      console.error("Fail sprint item failed:", e3);
    }
  }
  document.getElementById("ez-create-btn").onclick = async () => {
    const nameEl = document.getElementById("ez-project-name");
    const humanEl = document.getElementById("ez-human-name");
    const errEl = document.getElementById("ez-error");
    const name = nameEl.value.trim();
    if (!name) {
      errEl.textContent = "project name is required";
      errEl.style.display = "block";
      return;
    }
    errEl.style.display = "none";
    try {
      const body = { name };
      if (humanEl.value.trim()) body.human_id = humanEl.value.trim();
      const p3 = await api("/projects", { method: "POST", body: JSON.stringify(body) });
      document.getElementById("ez-wizard").style.display = "none";
      await loadProjects();
      await restoreTabs();
      openTab(p3);
    } catch (e3) {
      errEl.textContent = "create failed: " + e3.message;
      errEl.style.display = "block";
    }
  };
  document.getElementById("ez-project-name").addEventListener("keydown", (e3) => {
    if (e3.key === "Enter") document.getElementById("ez-create-btn").click();
  });
  document.getElementById("ez-advanced-link").onclick = (e3) => {
    e3.preventDefault();
    document.getElementById("ez-wizard").style.display = "none";
    document.getElementById("new-project-name").focus();
    restoreTabs();
  };
  (function() {
    const modal = document.getElementById("conn-setup-modal");
    const localBtn = document.getElementById("conn-local-btn");
    const sqliteForm = document.getElementById("conn-sqlite-form");
    const sqlitePath = document.getElementById("conn-sqlite-path");
    const sqliteName = document.getElementById("conn-sqlite-name");
    const sqliteSave = document.getElementById("conn-sqlite-save-btn");
    const pgToggle = document.getElementById("conn-pg-toggle-btn");
    const pgForm = document.getElementById("conn-pg-form");
    const pgSave = document.getElementById("conn-pg-save-btn");
    const pgUrl = document.getElementById("conn-pg-url");
    const pgName = document.getElementById("conn-pg-name");
    const errEl = document.getElementById("conn-setup-err");
    if (!modal) return;
    function showErr(msg) {
      if (errEl) {
        errEl.textContent = msg;
        errEl.style.display = msg ? "block" : "none";
      }
    }
    document.addEventListener("keydown", (e3) => {
      if (e3.key === "Escape") {
        const m3 = document.getElementById("conn-setup-modal");
        if (m3 && m3.style.display !== "none") m3.style.display = "none";
      }
    });
    window._showConnSetupIfNeeded = (cfg) => {
      if (typeof isDemoMode === "function" && isDemoMode()) return;
      if (!cfg?.toml_exists && cfg?.db !== "postgres") modal.style.display = "flex";
      const pathEl = document.getElementById("conn-toml-path");
      if (pathEl && cfg?.toml_path) {
        pathEl.innerHTML = '\u{1F4C4} Config: <span style="color:var(--text)">' + escapeHtml(cfg.toml_path) + "</span>";
      }
    };
    const PG_BTN_STYLE = "padding:12px;font-size:12px;text-align:left;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);cursor:pointer;font-family:'IBM Plex Mono',monospace";
    const PRIMARY_BTN_STYLE = "padding:12px;font-size:12px;text-align:left;border-radius:4px;cursor:pointer;font-family:'IBM Plex Mono',monospace;background:var(--accent);border:1px solid var(--accent);color:#001020;font-weight:600";
    function setActiveBtn(which) {
      if (localBtn) localBtn.style.cssText = which === "sqlite" ? PRIMARY_BTN_STYLE : PG_BTN_STYLE;
      if (pgToggle) pgToggle.style.cssText = which === "postgres" ? PRIMARY_BTN_STYLE : PG_BTN_STYLE;
    }
    if (localBtn) localBtn.onclick = () => {
      if (!sqliteForm) return;
      const open = sqliteForm.style.display === "flex";
      sqliteForm.style.display = open ? "none" : "flex";
      if (!open && sqlitePath && !sqlitePath.value) sqlitePath.value = "data/meridian.db";
      setActiveBtn(open ? null : "sqlite");
      if (pgForm) pgForm.style.display = "none";
    };
    if (sqliteSave) sqliteSave.onclick = async () => {
      const path = sqlitePath?.value.trim() || "data/meridian.db";
      const name = sqliteName?.value.trim() || "local";
      showErr("");
      try {
        sqliteSave.textContent = "Saving\u2026";
        sqliteSave.disabled = true;
        await api("/config/connections", {
          method: "POST",
          body: JSON.stringify({ name, type: "sqlite", path, activate: true })
        });
        modal.style.display = "none";
        toast("Saved \u2014 restarting\u2026");
        await _doRestart(false);
      } catch (e3) {
        showErr("Failed: " + e3.message);
        sqliteSave.textContent = "Save & Restart \u2192";
        sqliteSave.disabled = false;
      }
    };
    if (pgToggle) pgToggle.onclick = () => {
      if (pgForm) {
        const open = pgForm.style.display === "flex";
        pgForm.style.display = open ? "none" : "flex";
        setActiveBtn(open ? null : "postgres");
      }
      if (sqliteForm) sqliteForm.style.display = "none";
    };
    if (pgSave) pgSave.onclick = async () => {
      const url = pgUrl?.value.trim() || "";
      const name = pgName?.value.trim() || "postgres";
      showErr("");
      if (!url) {
        showErr("Postgres URL is required");
        return;
      }
      try {
        pgSave.textContent = "Saving\u2026";
        pgSave.disabled = true;
        await api("/config/connections", {
          method: "POST",
          body: JSON.stringify({ name, type: "postgres", url, activate: true })
        });
        modal.style.display = "none";
        toast("Saved \u2014 restarting\u2026");
        await _doRestart(false);
      } catch (e3) {
        showErr("Failed: " + e3.message);
        pgSave.textContent = "Save & Restart \u2192";
        pgSave.disabled = false;
      }
    };
  })();
  function toggleExpand(id) {
    const el2 = document.getElementById(id);
    if (!el2) return;
    const open = el2.style.display !== "none";
    el2.style.display = open ? "none" : "";
    const trigger = el2.previousElementSibling;
    if (trigger) {
      const arrow = trigger.querySelector(".expand-arrow");
      if (arrow) arrow.textContent = open ? "\u25B6" : "\u25BC";
    }
  }
  try {
    Object.assign(window, { loadCodeIntelTab, _initCodeIntelTabVisibility, hideHostedAdminControls, ensureSignOutLink: ensureSignOutLink2, ensureWorkspaceSwitcher: ensureWorkspaceSwitcher2, getActiveWorkspaceRole: getActiveWorkspaceRole2, showConnectDbModal, showLocalServerControls, _summarizeApiErrorText, _projectLoadErrorInfo, wireProjectLoadRetry: wireProjectLoadRetry2, renderProjectLoadError: renderProjectLoadError2, recordProjectLoadError: recordProjectLoadError2, clearProjectLoadError: clearProjectLoadError2, renderProjectLoadAlert, retryProjectSurface, syncSidebarActiveProject, autosizeGoalField, githubIconSvg: githubIconSvg2, getConstitutionLimit, loadProjectSettings: loadProjectSettings2, saveProjectSettings: saveProjectSettings2, loadExecutorRulesSection, loadTunnelPluginsSection, _demoTourDone: _demoTourDone2, _demoTourSavedStep: _demoTourSavedStep2, _demoTourSaveStep, _demoTourMarkDone, _demoTourClose, _tourActivateVtab, startDemoTour: startDemoTour2, resumeDemoTour, api, projectApi, loadServerConfig, _armAccountSwitchWatch, _refreshOnFocus, _checkAccountSwitch: _checkAccountSwitch2, _showAccountSwitchBanner, updateGitHubConnectionIndicator, _updateConnectionIndicator, checkGitStatus, _doRestart, loadConfig, loadProjects, _makeProjectItem, openTab, closeTab: closeTab2, saveTabs, renderTabs, _makeTabEl, _openTabMenu, _setProjectIcon, _renameProject, _makeSubproject, _detachSubproject, _deleteProject, activateTab, buildTabBody, scheduleLiveRefresh, initLiveAutoRefresh, loadLiveTab, refreshLiveTab, wireSprintAddEnter: wireSprintAddEnter2, sprintAction, sprintArchive, filterBackburner, sprintPushPrompt, sprintFeedback, sprintFeedbackNote, sprintItemEdit, addSprintItemFromInput: addSprintItemFromInput2, cacheMostRecentSession, renderLiveSessions, endLiveSession, openTimelineForSession, renderLiveQueue, addLiveTask, cancelLiveTask, showCopyPreview, wireClaudeLaunchPanel, stampHandoffTs, populateSessionDropdown, loadTimeline: loadTimeline2, _renderTimelineLog: _renderTimelineLog2, loadDocsTab, normalizeNotifyTarget, displayNotifyTarget: displayNotifyTarget2, osExecutorHintBanner: osExecutorHintBanner2, showFailoverBannerIfNeeded, suggestNtfyTopic, loadHitlTab, loadTeamTab, updateLiveFeed, loadRecentSessions, loadMilestones, loadRecentRuns, loadQueue, renderSearchResults: renderSearchResults2, wireQueueSectionToggles, refreshTab, refreshGoal, parseDecisionsBlob, renderConstitutionWarning: renderConstitutionWarning2, _hitlBadgeClick, initHitlPanel, setVtabCountBadge: setVtabCountBadge2, refreshProjectCountBadges, refreshHitl, _hitlAnswer, _hitlDismiss, loadPinnedDecisions, supersedePinnedDecision, addPinnedDecision, consolidateDecisions, renderDecisionsTable, wireGoalPreviewToggle, saveGoal, saveNorthStar, saveSprint, _sessionPresenceDot, refreshSessions, refreshTasks, renderTasks, _loadMoreTasks, renderTaskRow, deleteTaskRow, renderHitlRow, wireHitlRow, appendToGoal, hitlReply, hitlExecute, connectWs, handleWsEvent, restoreTabs, _deleteSprintItem, _sprintAction, completeSprintItem, failSprintItem, toggleExpand, flattenHierarchy, eligibleParents, state });
  } catch (e3) {
  }
})();
