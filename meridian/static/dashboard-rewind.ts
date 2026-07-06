// dashboard-rewind.js — rewind/analytics tab extracted from dashboard.js
// Re-exposes its symbols on window so inline handlers + cross-file references resolve after esbuild IIFE bundling.

// 233bae67 — historical sprint-execution gantt (CSS bars from a pure model).
import { ganttBars, sprintStatusColor } from "./components/sprintGraph";

export function initRewindTab(projectId: string) {

  const p = window.state.panels[projectId];

  if (!p) return;

  if (p.rewindWired) {

    // Already set up — repaint if we already have a window selected.

    if (p.rewindDays) loadRewindTab(projectId, p.rewindDays);

    return;

  }

  p.rewindWired = true;

  document.querySelectorAll(`.rewind-day-btn[data-pid="${projectId}"]`).forEach(btn => {

    btn.onclick = () => {

      const days = parseInt(btn.dataset.days, 10) || 7;

      loadRewindTab(projectId, days);

    };

  });

  const shareBtn = document.getElementById(`rewind-share-${projectId}`);

  if (shareBtn) shareBtn.onclick = () => copyRewindLink(projectId);

  // Search bar — debounced 350ms, shows results in rewind-wrap

  const searchInp = document.getElementById(`rewind-search-${projectId}`);

  if (searchInp && !searchInp._wired) {

    searchInp._wired = true;

    const wrap = document.getElementById(`rewind-wrap-${projectId}`);

    let _st: any = null;

    searchInp.addEventListener('input', function() {

      clearTimeout(_st);

      const q = this.value.trim();

      _st = setTimeout(async () => {

        if (!q) {

          if (p.rewindDays) loadRewindTab(projectId, p.rewindDays);

          else { if (wrap) wrap.innerHTML = '<div class="empty" style="color:var(--muted)">pick a window above</div>'; }

          return;

        }

        if (!wrap) return;

        wrap.innerHTML = '<div class="empty" style="color:var(--muted)">searching…</div>';

        try {

          const results = await api(`/projects/${projectId}/search?q=${encodeURIComponent(q)}&limit=15`);

          wrap.innerHTML = renderSearchResults(q, results);

        } catch (e: any) { wrap.innerHTML = `<div class="empty">search failed: ${escapeHtml(e.message)}</div>`; }

      }, 350);

    });

  }

  // Default to the 7-day view on first open.

  loadRewindTab(projectId, 7);

}

export async function loadRewindTab(projectId: string, days: any) {

  const wrap = document.getElementById(`rewind-wrap-${projectId}`);

  if (!wrap) return;

  const p = window.state.panels[projectId];

  if (p) p.rewindDays = days;

  // Highlight the active window button.

  document.querySelectorAll(`.rewind-day-btn[data-pid="${projectId}"]`).forEach(b => {

    b.classList.toggle('active', parseInt(b.dataset.days, 10) === days);

  });

  wrap.innerHTML = '<div style="color:var(--muted)">loading rewind…</div>';

  try {

    const [data, history, stats] = await Promise.all([

      api(`/projects/${projectId}/rewind?days=${days}`),

      api(`/projects/${projectId}/goal-history`).catch(() => []),

      api(`/projects/${projectId}/stats?days=30`).catch(() => null),

    ]);

    const activeTab = (p && p.rewindSubtab) || 'versions';

    wrap.innerHTML = renderRewindSubtabs(projectId, data, history, stats, activeTab);

    wrap.querySelectorAll('.rewind-subtab-btn').forEach(btn => {

      btn.onclick = () => {

        const tab = btn.dataset.tab;

        if (p) p.rewindSubtab = tab;

        wrap.querySelectorAll('.rewind-subtab-btn').forEach(b =>

          b.classList.toggle('active', b.dataset.tab === tab));

        wrap.querySelectorAll('.rewind-subtab-pane').forEach(c =>

          { c.style.display = c.dataset.tab === tab ? '' : 'none'; });

        if (tab === 'charts') initRewindCharts(projectId, stats);

      };

    });

    if (activeTab === 'charts' && stats) initRewindCharts(projectId, stats);

  } catch (e: any) {

    wrap.innerHTML = `<div style="color:var(--status-failed)">rewind failed: ${escapeHtml(e.message)}</div>`;

  }

}

export function renderRewindSubtabs(projectId: string, data: any, history: any, stats: any, activeTab: any) {

  /** Render rewind content split into five subtabs: Activity, Milestones, Sprint, Goals, Charts. */

  const tabs = [

    { id: 'versions', label: '📦 Milestones' },

    { id: 'sprint',   label: '⚡ Sprint items' },

    { id: 'goals',    label: '🎯 Goal' },

    { id: 'activity', label: '📋 Activity' },

    { id: 'charts',   label: '📊 Charts' },

  ];

  const tabBar = `<div class="rewind-subtab-bar">${

    tabs.map(t => `<button class="rewind-subtab-btn${activeTab === t.id ? ' active' : ''}" data-tab="${t.id}">${t.label}</button>`).join('')

  }</div>`;

  const make = (id: any, html: any) =>

    `<div class="rewind-subtab-pane" data-tab="${id}" style="${activeTab === id ? '' : 'display:none'}">${html}</div>`;

  return tabBar +

    make('activity', renderRewindActivity(projectId, data)) +

    make('versions', renderRewindVersions(projectId, data)) +

    make('sprint',   renderRewindSprint(projectId, data)) +

    make('goals',    renderRewindGoals(projectId, data, history)) +

    make('charts',   renderRewindCharts(projectId, stats));

}

export function renderRewindCharts(projectId: string, stats: any) {

  /** Charts subtab: tasks/day bar chart + sprint completion % by version. */

  if (!stats) {

    return '<div style="padding:14px;color:var(--muted);font-size:11px">Charts unavailable — stats endpoint not reachable.</div>';

  }

  const legendStyle = 'display:flex;gap:14px;margin-top:6px;font-size:10px;color:var(--muted);font-family:var(--font-mono)';

  const swatch = (color: any) => `<span style="display:inline-block;width:12px;height:12px;background:${color};border-radius:2px;margin-right:4px;vertical-align:middle"></span>`;

  return `<div style="padding:8px 0">

    <div style="color:var(--accent);font-weight:600;font-size:11px;margin-bottom:8px">📊 Sprint items / day (last ${stats.period_days}d)</div>

    <canvas id="chart-tasks-${escapeHtml(projectId)}" style="max-width:100%;max-height:160px"></canvas>

    <div style="${legendStyle}"><span>${swatch('rgba(96,165,250,0.7)')}Sprint items</span></div>

    <div style="color:var(--accent);font-weight:600;font-size:11px;margin:18px 0 8px">⚡ Session task completion % by version</div>

    <canvas id="chart-sprint-${escapeHtml(projectId)}" style="max-width:100%;max-height:120px"></canvas>

    <div style="${legendStyle}">

      <span>${swatch('rgba(52,211,153,0.7)')}100% done</span>

      <span>${swatch('rgba(96,165,250,0.7)')}Partial</span>

    </div>

    <div style="color:var(--accent);font-weight:600;font-size:11px;margin:18px 0 8px">🗂️ Activity by domain / day (last ${stats.period_days}d)</div>

    <canvas id="chart-activity-${escapeHtml(projectId)}" style="max-width:100%;max-height:160px"></canvas>

    <div style="font-size:9px;color:var(--muted);margin-top:4px">docs/web/experiment/code/citation pointers touched each day — daily aggregate totals only.</div>

  </div>`;

}

export function initRewindCharts(projectId: string, stats: any) {

  /** Draw (or redraw) Chart.js instances for the Charts subtab. Destroys prior instances first. */

  if (!stats || typeof Chart === 'undefined') return;

  const p = window.state.panels[projectId];



  // Destroy stale instances before re-creating to avoid duplicate chart warning.

  if (p) {

    if (p._chartTasks) { p._chartTasks.destroy(); p._chartTasks = null; }

    if (p._chartSprint) { p._chartSprint.destroy(); p._chartSprint = null; }

    if (p._chartActivity) { p._chartActivity.destroy(); p._chartActivity = null; }

  }



  const tasksCanvas = document.getElementById(`chart-tasks-${projectId}`);

  const sprintItemsPerDay = stats.sprint_items_per_day || stats.tasks_per_day;

  if (tasksCanvas && sprintItemsPerDay) {

    const labels = sprintItemsPerDay.map((d: any) => d.day.slice(5));  // MM-DD

    const totals = sprintItemsPerDay.map((d: any) => d.total);

    const chart = new Chart(tasksCanvas, {

      type: 'bar',

      data: {

        labels,

        datasets: [{

          label: 'sprint items',

          data: totals,

          backgroundColor: 'rgba(96, 165, 250, 0.7)',

          borderRadius: 2,

        }],

      },

      options: {

        responsive: true,

        plugins: { legend: { display: false } },

        scales: {

          x: { ticks: { color: '#9ca3af', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#1f2937' } },

          y: { ticks: { color: '#9ca3af', font: { size: 9 }, stepSize: 1 }, grid: { color: '#1f2937' }, beginAtZero: true },

        },

      },

    });

    if (p) p._chartTasks = chart;

  }



  const sprintCanvas = document.getElementById(`chart-sprint-${projectId}`);

  if (sprintCanvas && stats.sprint_velocity && stats.sprint_velocity.length) {

    const sv = stats.sprint_velocity;

    const chart = new Chart(sprintCanvas, {

      type: 'bar',

      data: {

        labels: sv.map((v: any) => v.version),

        datasets: [{

          label: '% done',

          data: sv.map((v: any) => v.pct),

          backgroundColor: sv.map((v: any) => v.pct === 100 ? 'rgba(52, 211, 153, 0.7)' : 'rgba(96, 165, 250, 0.7)'),

          borderRadius: 2,

        }],

      },

      options: {

        responsive: true,

        plugins: { legend: { display: false } },

        scales: {

          x: { ticks: { color: '#9ca3af', font: { size: 9 } }, grid: { color: '#1f2937' } },

          y: { ticks: { color: '#9ca3af', font: { size: 9 } }, grid: { color: '#1f2937' }, min: 0, max: 100,

               title: { display: true, text: '% done', color: '#9ca3af', font: { size: 9 } } },

        },

      },

    });

    if (p) p._chartSprint = chart;

  }

  // c975b6ef — activity by domain / day: stacked bars, one dataset per pointer
  // source_type, over the same last-N-days window as the tasks chart above.
  const activityCanvas = document.getElementById(`chart-activity-${projectId}`);
  const abd = stats.activity_by_domain;
  const domains: string[] = stats.activity_domains || [];
  if (activityCanvas && abd && domains.length) {
    const labels = abd.map((d: any) => String(d.day).slice(5));  // MM-DD
    const PALETTE: Record<string, string> = {
      code: 'rgba(96,165,250,0.75)', docs: 'rgba(52,211,153,0.75)',
      web: 'rgba(217,119,6,0.75)', experiment: 'rgba(167,139,250,0.75)',
      citation: 'rgba(244,114,182,0.75)', other: 'rgba(156,163,175,0.6)',
    };
    const datasets = domains.map((dom: string, i: number) => ({
      label: dom,
      data: abd.map((d: any) => (d.by_domain || {})[dom] || 0),
      backgroundColor: PALETTE[dom] || `hsl(${(i * 67) % 360} 60% 60% / 0.75)`,
      borderRadius: 2,
      stack: 'activity',
    }));
    const chart = new Chart(activityCanvas, {
      type: 'bar',
      data: { labels, datasets },
      options: {
        responsive: true,
        plugins: { legend: { display: true, position: 'bottom', labels: { color: '#9ca3af', font: { size: 9 }, boxWidth: 10 } } },
        scales: {
          x: { stacked: true, ticks: { color: '#9ca3af', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#1f2937' } },
          y: { stacked: true, ticks: { color: '#9ca3af', font: { size: 9 }, stepSize: 1 }, grid: { color: '#1f2937' }, beginAtZero: true },
        },
      },
    });
    if (p) p._chartActivity = chart;
  }

}

export function renderRewindSprint(projectId: string, data: any) {

  /** Sprint subtab: sprint_items grouped by version, showing done/pending/failed counts. */

  const items = data.sprint_items_completed || [];

  const pending = data.sprint_items_pending || [];

  const allItems = [...items, ...pending];



  if (!allItems.length) {

    return '<div style="padding:14px;color:var(--muted);font-size:11px">No sprint items yet.</div>';

  }



  // Group by version

  const byVersion: Record<string, any[]> = {};

  allItems.forEach(s => {

    const v = s.version || 'current';

    if (!byVersion[v]) byVersion[v] = [];

    byVersion[v].push(s);

  });



  const statusDot = (s: any) => {

    if (s.status === 'done') return '<span style="color:var(--status-done)">✓</span>';

    if (s.status === 'failed') return '<span style="color:var(--status-failed)">✗</span>';

    if (s.status === 'pushed') return '<span style="color:var(--muted)">→</span>';

    return '<span style="color:var(--status-pending)">○</span>';

  };



  // 233bae67 — execution-timeline gantt across all versions (bars positioned by
  // claimed_at→completed_at). Pure geometry from ganttBars(); no charting lib.
  let html = '';
  try {
    const bars = ganttBars(allItems);
    if (bars.length) {
      html += '<div class="rewind-gantt" style="margin:8px 0;padding:8px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px">'
        + `<div style="font-size:10px;color:var(--muted);margin-bottom:6px">⏱ Execution timeline (${bars.length} items)</div>`
        + bars.map(b =>
            '<div style="position:relative;height:14px;margin-bottom:2px">'
            + `<div title="${escapeHtml(b.title)}" style="position:absolute;left:${b.leftPct}%;width:${b.widthPct}%;height:12px;`
            + `background:${sprintStatusColor(b.status)};border-radius:2px;overflow:hidden;white-space:nowrap;`
            + `font-size:8px;line-height:12px;color:#0b0e14;padding:0 3px;box-sizing:border-box">${escapeHtml(b.title)}</div>`
            + '</div>',
          ).join('')
        + '</div>';
    }
  } catch (e) { /* gantt is best-effort */ }

  // Sort versions: current first, then descending

  const versions = Object.keys(byVersion).sort((a, b) => {

    if (a === 'current') return -1;

    if (b === 'current') return 1;

    return b.localeCompare(a);

  });



  versions.forEach(v => {

    const vItems = byVersion[v];

    const doneCount = vItems.filter(s => s.status === 'done').length;

    const total = vItems.length;

    const pct = total ? Math.round((doneCount / total) * 100) : 0;

    const id = `sprint-v-${projectId}-${v.replace(/[^a-z0-9]/gi, '')}`;

    html += `<section style="margin-bottom:10px">

      <div style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border)" onclick="toggleExpand('${id}')">

        <span style="color:var(--accent);font-weight:600;font-size:11px">${escapeHtml(v)}</span>

        <span style="color:var(--muted);font-size:10px">${doneCount}/${total} done (${pct}%) <span class="expand-arrow">▶</span></span>

      </div>

      <div id="${id}" style="display:${v === 'current' ? 'block' : 'none'}">

        ${vItems.map(s => `<div style="padding:3px 0 3px 8px;font-size:11px;display:flex;gap:6px;align-items:flex-start">

          ${statusDot(s)}

          <span style="color:${s.status === 'done' ? 'var(--muted)' : 'var(--text)'};${s.status === 'done' ? 'text-decoration:line-through' : ''}">${escapeHtml(s.title || '')}</span>

        </div>`).join('')}

      </div>

    </section>`;

  });



  return `<div style="padding:8px 0">${html}</div>`;

}

export function _rewindSec(icon: any, title: any, items: any, render: any) {

  /** Shared section renderer for rewind subtabs. */

  if (!items || !items.length) {

    return `<section style="margin-bottom:14px">

      <div style="color:var(--accent);font-weight:600;margin-bottom:4px">${icon} ${title}</div>

      <div style="color:var(--muted);font-size:10px">(none)</div>

    </section>`;

  }

  return `<section style="margin-bottom:14px">

    <div style="color:var(--accent);font-weight:600;margin-bottom:4px">${icon} ${title}</div>

    ${items.map(render).join('')}

  </section>`;

}

export function renderRewindActivity(projectId: string, data: any) {

  /** Activity subtab: sessions + decisions + task stats. */

  // Deduplicate sessions: same name may appear multiple times if registered

  // multiple times. Keep highest done-count entry per session name.

  const sessByName = new Map();

  (data.session_summaries || []).forEach((s: any) => {

    const prev = sessByName.get(s.session_name);

    if (!prev || (s.tasks_completed || 0) > (prev.tasks_completed || 0)) {

      sessByName.set(s.session_name, s);

    }

  });

  const dedupedSessions = [...sessByName.values()];

  const sessions = _rewindSec('🧠', 'Sessions', dedupedSessions, (s: any) =>

    `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">

      <div style="color:var(--accent)">${escapeHtml(s.session_name)} <span style="color:var(--muted);font-size:10px">· ${s.tasks_completed} done</span></div>

      <div style="color:var(--muted);font-size:10px">${escapeHtml(s.summary || '')}</div>

    </div>`);

  const decisions = _rewindSec('📋', 'Decisions logged', data.decisions_logged, (d: any) =>

    `<div style="padding:2px 0"><span style="color:var(--muted);font-size:10px">[${escapeHtml(d.logged_at || '')}]</span> ${escapeHtml(d.text || '')}</div>`);

  const byStatus = data.tasks_by_status || {};

  const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">

    <div style="color:var(--accent);font-weight:600">📊 Tasks: ${byStatus.done || 0} done, ${byStatus.failed || 0} failed, ${byStatus.pending || 0} pending <span style="color:var(--muted);font-size:10px">(${data.tasks_total || 0} total over ${data.period_days}d)</span></div>

  </section>`;

  return sessions + decisions + summary;

}

export function renderRewindVersions(projectId: string, data: any) {

  /** Versions subtab: milestones shipped + sprint items completed + stats. */

  const versions = _rewindSec('📦', 'Milestones shipped', data.versions_shipped,

    (v: any) => `<div style="padding:5px 0;border-bottom:1px solid var(--border);font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.6;color:var(--text)">${escapeHtml(v)}</div>`);

  const sprints = _rewindSec('✅', 'Sprint items completed', data.sprint_items_completed, (s: any) =>

    `<div style="padding:2px 0"><span style="color:var(--accent-green)">${escapeHtml(s.version || '')}</span> — ${escapeHtml(s.title || '')} <span style="color:var(--muted);font-size:10px">${escapeHtml(s.completed_at || '')}</span></div>`);

  const summary = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">

    <div style="color:var(--accent);font-weight:600">📊 ${(data.sprint_items_completed || []).length} sprint items completed over ${data.period_days}d</div>

  </section>`;

  return versions + sprints + summary;

}

export function renderRewindGoals(projectId: string, data: any, history: any) {

  /** Goals subtab: goal changes (newest first) + goal version history. */

  const preStyle = 'margin:0;white-space:pre-wrap;word-break:break-word;background:var(--bg-card);padding:6px;border-radius:3px;font-size:10px;font-family:inherit';

  const goals = _rewindSec('🎯', 'Goal changes', (data.goal_changes || []).slice().reverse(), (g: any, idx: any) => {

    const id = `gc-expand-${projectId}-${idx}`;

    return `<div style="padding:3px 0;border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">

      <div style="cursor:pointer;user-select:none" onclick="toggleExpand('${id}')">

        <div style="color:var(--muted);font-size:10px">${escapeHtml(g.field)} · ${escapeHtml(g.changed_at || '')} <span class="expand-arrow" style="font-size:9px">▶</span></div>

        <div style="color:var(--text);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((g.new_summary || '(empty)').slice(0,120))}</div>

      </div>

      <div id="${id}" style="display:none;margin-top:6px;overflow:visible;max-height:none">

        <div style="color:var(--muted);font-size:10px;margin-bottom:2px">before:</div>

        <pre style="${preStyle};margin-bottom:6px">${escapeHtml(g.old_full || '(empty)')}</pre>

        <div style="color:var(--muted);font-size:10px;margin-bottom:2px">after:</div>

        <pre style="${preStyle}">${escapeHtml(g.new_full || '(empty)')}</pre>

      </div>

    </div>`;

  });

  let historyHtml = '';

  if (history && history.length) {

    const rows = [...history].reverse().map((v, idx) => {

      const id = `gv-expand-${projectId}-${idx}`;

      const raw = (v.version_goal || v.north_star || '').replace(/\s+/g, ' ').trim();

      const snippet = raw.length > 80 ? raw.slice(0, 79) + '…' : raw;

      return `<div style="border-left:2px solid var(--border);padding-left:8px;margin-bottom:4px">

        <div style="cursor:pointer;user-select:none" onclick="toggleExpand('${id}')" title="${escapeHtml(raw)}">

          <span style="color:var(--accent)">v${v.version}</span>

          <span style="color:var(--muted);font-size:10px"> · ${escapeHtml(v.created_at || '')}</span>

          <span> ${escapeHtml(snippet)}</span>

          <span class="expand-arrow" style="color:var(--muted);font-size:9px"> ▶</span>

        </div>

        <div id="${id}" style="display:none;margin-top:6px">

          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">north_star:</div>

          <pre style="${preStyle};margin-bottom:6px">${escapeHtml(v.north_star || '(empty)')}</pre>

          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">version_goal:</div>

          <pre style="${preStyle};margin-bottom:6px">${escapeHtml(v.version_goal || '(empty)')}</pre>

          <div style="color:var(--muted);font-size:10px;margin-bottom:2px">sprint:</div>

          <pre style="${preStyle}">${escapeHtml(v.sprint || '(empty)')}</pre>

        </div>

      </div>`;

    });

    historyHtml = `<section style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">

      <div style="color:var(--accent);font-weight:600;margin-bottom:6px">📜 Goal version history (${history.length} versions, newest first)</div>

      ${rows.join('')}

    </section>`;

  }

  return goals + historyHtml;

}

export async function copyRewindLink(projectId: string) {

  const p = window.state.panels[projectId];

  const days = (p && p.rewindDays) || 7;

  try {

    const res = await api(`/projects/${projectId}/rewind-token`, { method: 'POST' });

    let base = '';

    try {

      const cfg = await api('/config');

      base = cfg.server_url || window.location.origin;

    } catch (_) {

      base = window.location.origin;

    }

    const url = `${base}/projects/${projectId}/rewind?days=${days}&token=${encodeURIComponent(res.token)}`;

    try {

      await navigator.clipboard.writeText(url);

      toast('shareable link copied');

    } catch (_) {

      // Older browsers — surface the URL so the user can copy manually.

      window.prompt('copy this URL', url);

    }

  } catch (e: any) {

    toast('share failed: ' + e.message);

  }

}

// --- esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { initRewindTab, loadRewindTab, renderRewindSubtabs, renderRewindCharts, initRewindCharts, renderRewindSprint, _rewindSec, renderRewindActivity, renderRewindVersions, renderRewindGoals, copyRewindLink }); } catch (e: any) {}
