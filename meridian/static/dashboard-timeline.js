// Extracted from dashboard.js -- timeline/heatmap/gantt functions
// DO NOT DELETE -- loaded via script tag in dashboard.html

function renderTimeline(projectId, data) {

  const wrap = document.getElementById(`timeline-wrap-${projectId}`);

  if (!wrap) return;

  const p = state.panels[projectId];
  const sessionFilter = p && p.timelineSessionFilter;
  const rawTasks = (data && data.tasks) || [];
  const tasks = sessionFilter ? rawTasks.filter(t => t.session_id === sessionFilter) : rawTasks;
  const goal_events = sessionFilter ? [] : ((data && data.goal_events) || []);
  data = { ...(data || {}), tasks, goal_events };



  if (!tasks.length && !goal_events.length) {

    wrap.innerHTML = `<div class="timeline-empty">no activity yet — log a task to see it here</div>`;

    return;

  }



  if (p && p._echart) { try { p._echart.dispose(); } catch (_) {} p._echart = null; }

  if (p && p._heatchart) { try { p._heatchart.dispose(); } catch (_) {} p._heatchart = null; }



  if (typeof echarts === 'undefined') {

    _renderTimelineLog(projectId, data);

    return;

  }



  // [Heatmap] [Detail] sub-tabs + [By Sprint/Tasks/Sprints only] view switcher.
  // View switcher is a dropdown added next to the sub-tabs.

  const savedTlView = (() => { try { return localStorage.getItem('meridian_tl_view_' + projectId) || 'heatmap'; } catch(_) { return 'heatmap'; } })();

  wrap.innerHTML = `

    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">

      <div class="tl-subtabs" style="margin-bottom:0">

        <button class="tl-subtab${savedTlView === 'heatmap' ? ' active' : ''}" data-sub="heatmap">Heatmap</button>

        <button class="tl-subtab${savedTlView === 'detail' ? ' active' : ''}" data-sub="detail">Detail</button>

      </div>

      <select id="tl-view-select-${projectId}" style="padding:3px 6px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:10px;font-family:var(--font-mono);cursor:pointer" title="Timeline grouping mode">

        <option value="heatmap"${savedTlView === 'heatmap' ? ' selected' : ''}>By Heatmap</option>

        <option value="detail"${savedTlView === 'detail' ? ' selected' : ''}>By Session</option>

        <option value="tasks"${savedTlView === 'tasks' ? ' selected' : ''}>Tasks</option>

        <option value="sprints"${savedTlView === 'sprints' ? ' selected' : ''}>Sprints only</option>

        <option value="by-sprint"${savedTlView === 'by-sprint' ? ' selected' : ''}>By Sprint</option>

      </select>
      ${sessionFilter ? `<span style="font-size:10px;color:var(--accent);border:1px solid var(--accent)55;border-radius:3px;padding:2px 6px">session ${escapeHtml(sessionFilter.slice(0, 8))}</span><button class="secondary" id="tl-clear-session-${projectId}" style="padding:2px 8px;font-size:10px">Clear</button>` : ''}

    </div>

    <div class="tl-pane${savedTlView === 'heatmap' || savedTlView === 'detail' ? ' active' : ''}" id="tl-pane-heatmap-${projectId}"${savedTlView !== 'heatmap' ? ' style="display:none"' : ''}></div>

    <div class="tl-pane" id="tl-pane-detail-${projectId}" style="display:${savedTlView === 'detail' ? '' : 'none'}"></div>

    <div id="tl-pane-tasks-${projectId}" style="display:${savedTlView === 'tasks' ? '' : 'none'}"></div>

    <div id="tl-pane-sprints-${projectId}" style="display:${savedTlView === 'sprints' || savedTlView === 'by-sprint' ? '' : 'none'}"></div>`;

  const heatPane = document.getElementById(`tl-pane-heatmap-${projectId}`);

  const detailPane = document.getElementById(`tl-pane-detail-${projectId}`);

  const tasksPane = document.getElementById(`tl-pane-tasks-${projectId}`);

  const sprintsPane = document.getElementById(`tl-pane-sprints-${projectId}`);



  const _renderTasksFlat = () => {

    const { tasks = [] } = data || {};

    if (!tasks.length) { tasksPane.innerHTML = `<div class="timeline-empty">no tasks logged yet</div>`; return; }

    tasksPane.innerHTML = tasks.map(t => {

      const ts = (t.created_at || '').slice(0, 16).replace('T', ' ');

      const who = t.human_id || t.session_name || '';

      const status = (t.status || '').toUpperCase();

      return `<div style="padding:5px 8px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:baseline">

        <span style="font-size:9px;color:var(--muted);white-space:nowrap;min-width:100px">${escapeHtml(ts)}</span>

        ${who ? `<span style="font-size:9px;color:var(--accent);white-space:nowrap">${escapeHtml(who)}</span>` : ''}

        <span style="font-size:10px;color:var(--muted);white-space:nowrap">[${escapeHtml(status)}]</span>

        <span style="font-size:11px;color:var(--text);word-break:break-word">${escapeHtml(t.description || '')}</span>

      </div>`;

    }).join('');

  };



  const _renderSprintsView = (groupBySprint) => {

    api(`/projects/${projectId}/sprint-items?status=done`).then(items => {

      const done = (items || []).filter(it => it.status === 'done').sort((a,b) =>

        String(b.completed_at || b.added_at || '').localeCompare(String(a.completed_at || a.added_at || '')));

      if (!done.length) { sprintsPane.innerHTML = `<div class="timeline-empty">no completed sprint items</div>`; return; }

      if (groupBySprint) {

        const groups = {};

        done.forEach(it => {

          const key = it.version || it.item_group || '(unversioned)';

          (groups[key] = groups[key] || []).push(it);

        });

        sprintsPane.innerHTML = Object.entries(groups).map(([grp, items]) =>

          `<div style="margin-bottom:12px">

            <div style="font-size:10px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;padding:4px 0;border-bottom:1px solid var(--border);margin-bottom:4px">${escapeHtml(grp)} (${items.length})</div>

            ${items.map(it => `<div style="padding:3px 0;font-size:11px;color:var(--text);display:flex;gap:8px"><span style="font-size:9px;color:var(--muted);white-space:nowrap;min-width:70px">${escapeHtml((it.completed_at || it.added_at || '').slice(0,10))}</span><span>${escapeHtml(it.title || '')}</span></div>`).join('')}

          </div>`

        ).join('');

      } else {

        sprintsPane.innerHTML = done.map(it =>

          `<div style="padding:4px 0;font-size:11px;color:var(--text);display:flex;gap:8px;border-bottom:1px solid var(--border)33">

            <span style="font-size:9px;color:var(--muted);white-space:nowrap;min-width:70px">${escapeHtml((it.completed_at || it.added_at || '').slice(0,10))}</span>

            ${it.version ? `<span style="font-size:9px;color:var(--accent)">${escapeHtml(it.version)}</span>` : ''}

            <span>${escapeHtml(it.title || '')}</span>

          </div>`

        ).join('');

      }

    }).catch(e => { sprintsPane.innerHTML = `<div class="timeline-empty">failed: ${escapeHtml(e.message)}</div>`; });

  };



  _renderTimelineHeatmap(projectId, data, heatPane);

  _renderTimelineGantt(projectId, data, detailPane);

  if (savedTlView === 'tasks') _renderTasksFlat();

  if (savedTlView === 'sprints') _renderSprintsView(false);

  if (savedTlView === 'by-sprint') _renderSprintsView(true);



  const viewSelect = document.getElementById(`tl-view-select-${projectId}`);
  const clearSessionBtn = document.getElementById(`tl-clear-session-${projectId}`);

  if (clearSessionBtn) clearSessionBtn.onclick = () => {
    if (p) p.timelineSessionFilter = null;
    loadTimeline(projectId);
  };

  if (viewSelect) {

    viewSelect.onchange = () => {

      const view = viewSelect.value;

      try { localStorage.setItem('meridian_tl_view_' + projectId, view); } catch(_) {}

      wrap.querySelectorAll('.tl-subtab').forEach(b => b.classList.toggle('active', b.dataset.sub === view));

      heatPane.style.display = view === 'heatmap' ? '' : 'none';

      detailPane.style.display = view === 'detail' ? '' : 'none';

      tasksPane.style.display = view === 'tasks' ? '' : 'none';

      sprintsPane.style.display = (view === 'sprints' || view === 'by-sprint') ? '' : 'none';

      if (view === 'heatmap' && p && p._heatchart) { try { p._heatchart.resize(); } catch(_) {} }

      if (view === 'detail' && p && p._echart) { try { p._echart.resize(); } catch(_) {} }

      if (view === 'tasks') _renderTasksFlat();

      if (view === 'sprints') _renderSprintsView(false);

      if (view === 'by-sprint') _renderSprintsView(true);

    };

  }



  wrap.querySelectorAll('.tl-subtab').forEach(btn => {

    btn.onclick = () => {

      const sub = btn.dataset.sub;

      if (viewSelect) viewSelect.value = sub;

      try { localStorage.setItem('meridian_tl_view_' + projectId, sub); } catch(_) {}

      wrap.querySelectorAll('.tl-subtab').forEach(b => b.classList.toggle('active', b === btn));

      heatPane.style.display = sub === 'heatmap' ? '' : 'none';

      detailPane.style.display = sub === 'detail' ? '' : 'none';

      tasksPane.style.display = 'none';

      sprintsPane.style.display = 'none';

      // ECharts can't measure a display:none container, so resize on reveal.

      if (sub === 'heatmap' && p && p._heatchart) { try { p._heatchart.resize(); } catch (_) {} }

      if (sub === 'detail' && p && p._echart) { try { p._echart.resize(); } catch (_) {} }

    };

  });

}

function _heatmapPieces(maxScale) {

  // Six-bucket green→red ramp scaled proportionally to maxScale so projects

  // with very different activity levels stay legible.

  const colors = ['#bbf7d0', '#4ade80', '#16a34a', '#ca8a04', '#ea580c', '#dc2626'];

  const n = colors.length;

  const pieces = [];

  let lo = 1;

  for (let i = 0; i < n; i++) {

    if (i === n - 1) {

      pieces.push({ min: lo, color: colors[i], label: `${lo}+` });

      break;

    }

    const hi = Math.max(lo, Math.round((maxScale * (i + 1)) / n));

    pieces.push({ min: lo, max: hi, color: colors[i], label: lo === hi ? `${lo}` : `${lo}–${hi}` });

    lo = hi + 1;

  }

  return pieces;

}

function _heatmapMaxFor(projectId) {

  const raw = parseInt(localStorage.getItem(`meridian_heatmap_max_${projectId}`), 10);

  if (!Number.isFinite(raw)) return 25;

  return Math.min(100, Math.max(10, raw));

}

function _renderTimelineHeatmap(projectId, data, paneEl) {

  /** Contribution calendar — one colored square per day, intensity by task

   * count. Multi-human projects get one calendar row per human_id. Click a

   * day to expand the sessions that contributed that day. */

  if (!paneEl) return;

  const daily = (data && data.daily_counts) || [];

  if (!daily.length) {

    paneEl.innerHTML = `<div class="timeline-empty">no activity yet — log a task to see it here</div>`;

    return;

  }



  const cssVar = (name, fallback) => {

    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();

    return v || fallback;

  };

  const emptyColor = cssVar('--surface-2', '#1a2740');

  const borderCol = cssVar('--border', '#232830');

  const textPrimary = cssVar('--text', '#d8dde6');

  const textMuted = cssVar('--muted', '#9ba5b5');



  // Canonical people + client apps drive the filter chips (items 30/31). Fall

  // back to deriving them from the per-day session entries if the backend

  // didn't send the top-level lists.

  let allPeople = (data.people && data.people.slice()) || [];

  let allClients = (data.clients && data.clients.slice()) || [];

  if (!allPeople.length || !allClients.length) {

    const ps = new Set(), cs = new Set();

    daily.forEach(d => (d.sessions || []).forEach(s => {

      ps.add(s.person || s.human || '(unknown)');

      cs.add(s.client || '(none)');

    }));

    if (!allPeople.length) allPeople = [...ps].sort();

    if (!allClients.length) allClients = [...cs].sort();

  }



  const dates = daily.map(d => d.date).sort();

  const rangeStart = dates[0];

  const rangeEnd = dates[dates.length - 1];



  // Filter selection, persisted per project. A stale/empty selection falls

  // back to "all" so it can never blank the calendar.

  const selKey = (k) => `meridian_tl_${k}_${projectId}`;

  const loadSel = (k, all) => {

    try {

      const raw = JSON.parse(localStorage.getItem(selKey(k)) || 'null');

      if (Array.isArray(raw)) {

        const keep = raw.filter(x => all.includes(x));

        if (keep.length) return new Set(keep);

      }

    } catch (_) {}

    return new Set(all);

  };

  let selPeople = loadSel('people', allPeople);

  let selClients = loadSel('clients', allClients);

  const clientOK = (s) => selClients.size === allClients.length || selClients.has(s.client || '(none)');



  const CELL = 16;

  const CAL_TOP = 28;

  const CAL_H = CELL * 7 + 34;   // 7 weekday rows + month/label gutters

  const ROW_GAP = 18;

  const rowH = CAL_H + ROW_GAP;



  // Per (rowKey,day) detail so clicking a cell shows that row's sessions.

  let detailByPersonDay = {};



  // Recompute calendars/series/titles for the current person+client selection.

  // One calendar row per selected person; a single selected person (or a

  // single-person project) renders one unlabeled calendar.

  function computeView() {

    let rows = allPeople.filter(p => selPeople.has(p));

    if (!rows.length) rows = allPeople.slice();

    const multi = rows.length > 1;

    const rowKeys = multi ? rows : ['__all__'];



    detailByPersonDay = {};

    const countByKeyDay = {};

    daily.forEach(d => {

      (d.sessions || []).forEach(s => {

        if (!clientOK(s)) return;

        const person = s.person || s.human || '(unknown)';

        if (!selPeople.has(person)) return;

        const key = multi ? person : '__all__';

        countByKeyDay[`${key}|${d.date}`] = (countByKeyDay[`${key}|${d.date}`] || 0) + s.count;

        (detailByPersonDay[`${key}|${d.date}`] = detailByPersonDay[`${key}|${d.date}`] || []).push(s);

      });

    });



    const calendars = [], series = [], titles = [];

    rowKeys.forEach((rk, i) => {

      const top = CAL_TOP + i * rowH;

      calendars.push({

        top: top,

        left: multi ? 120 : 40,

        right: 12,

        cellSize: [CELL, CELL],

        range: rangeStart === rangeEnd ? rangeStart : [rangeStart, rangeEnd],

        splitLine: { show: true, lineStyle: { color: borderCol, type: 'dashed', width: 1 } },

        itemStyle: { color: emptyColor, borderColor: '#0d1b2e', borderWidth: 1 },

        yearLabel: { show: false },

        monthLabel: { color: textPrimary, fontFamily: 'IBM Plex Mono', fontSize: 13, fontWeight: 'bold' },

        dayLabel: { color: textMuted, fontFamily: 'IBM Plex Mono', fontSize: 10, firstDay: 1 },

      });

      if (multi) {

        titles.push({

          text: rk.length > 16 ? rk.slice(0, 15) + '…' : rk,

          left: 6,

          top: top + CAL_H / 2 - 6,

          textStyle: { color: _colorForHuman(rk === '(unknown)' ? '' : rk), fontFamily: 'IBM Plex Mono', fontSize: 10, fontWeight: 'bold' },

        });

      }

      const pts = daily.map(d => {

        const count = countByKeyDay[`${rk}|${d.date}`] || 0;

        const dayDetail = detailByPersonDay[`${rk}|${d.date}`] || [];

        const scount = new Set(dayDetail.map(s => s.session_id)).size;

        return { value: [d.date, count], scount: scount, person: rk };

      }).filter(pt => pt.value[1] > 0);

      series.push({

        type: 'heatmap',

        coordinateSystem: 'calendar',

        calendarIndex: i,

        data: pts,

        // Stamp the day-of-month (dark) onto cells that had activity, so the

        // calendar reads as a calendar — empty days carry no label since pts

        // only includes count > 0.

        label: {

          show: true,

          color: '#0b1220',

          fontFamily: 'IBM Plex Mono',

          fontSize: 10,

          fontWeight: 'bold',

          formatter: (p) => {

            const v = p.value && p.value[0];

            if (!v) return '';

            const dd = parseInt(String(v).slice(8, 10), 10);

            return Number.isFinite(dd) ? String(dd) : '';

          },

        },

      });

    });

    const totalH = CAL_TOP + rowKeys.length * rowH + 28;

    return { calendars, series, titles, totalH };

  }



  let { calendars, series, titles, totalH } = computeView();



  paneEl.innerHTML = '';

  let scaleMax = _heatmapMaxFor(projectId);



  // Person + client filter chips. Hidden when there's nothing to filter

  // (single person, single client). applyFilters is reassigned after the chart

  // is created; the chip handlers close over it.

  let applyFilters = () => {};

  if (allPeople.length > 1 || allClients.length > 1) {

    const bar = document.createElement('div');

    bar.className = 'tl-filter-bar';

    bar.style.cssText = 'display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:0 4px 8px;font-size:10px;font-family:IBM Plex Mono,monospace;color:var(--muted)';

    paneEl.appendChild(bar);



    // Toggle a value in a selection set, persisting to localStorage. '__all__'

    // resets to everything; emptying a set also falls back to everything so the

    // calendar can never blank out.

    const toggle = (sel, all, key, x) => {

      if (x === '__all__') sel = new Set(all);

      else { sel.has(x) ? sel.delete(x) : sel.add(x); if (!sel.size) sel = new Set(all); }

      localStorage.setItem(selKey(key), JSON.stringify([...sel]));

      return sel;

    };



    const closeAllPanels = () => bar.querySelectorAll('[data-tl-panel]').forEach(p => { p.style.display = 'none'; });



    // Compact multi-select dropdown — a long roster collapses to one chip

    // instead of overflowing the row off-screen. The menu scrolls; selected

    // rows are highlighted blue/white.

    const mkDropdown = (labelText, all, getSel, setSel, key) => {

      const wrap = document.createElement('div');

      wrap.style.cssText = 'position:relative;display:inline-block';

      const btn = document.createElement('button');

      btn.style.cssText = 'display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:6px;cursor:pointer;font-size:10px;font-family:inherit;border:1px solid var(--border);background:var(--surface-2);color:var(--text)';

      const panel = document.createElement('div');

      panel.setAttribute('data-tl-panel', '1');

      panel.style.cssText = 'position:absolute;z-index:30;top:calc(100% + 4px);left:0;min-width:170px;max-height:240px;overflow-y:auto;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:4px;box-shadow:0 6px 20px rgba(0,0,0,0.4);display:none';

      const sync = () => {

        const sel = getSel();

        const total = all.length;

        const allSel = sel.size === total;

        btn.textContent = '';

        const cap = document.createElement('span');

        cap.textContent = `${labelText}: ${allSel ? 'All' : sel.size + '/' + total}`;

        const caret = document.createElement('span');

        caret.textContent = '▾'; caret.style.cssText = 'opacity:0.6';

        btn.appendChild(cap); btn.appendChild(caret);

        panel.innerHTML = '';

        const mkRow = (text, value, active) => {

          const row = document.createElement('div');

          row.textContent = text;

          row.title = text;

          row.style.cssText = `padding:4px 8px;border-radius:4px;cursor:pointer;white-space:nowrap;font-size:10px;margin-bottom:1px;background:${active ? '#2563eb' : 'transparent'};color:${active ? '#fff' : 'var(--text)'}`;

          row.onmouseenter = () => { if (!active) row.style.background = 'var(--surface-2)'; };

          row.onmouseleave = () => { if (!active) row.style.background = 'transparent'; };

          row.onclick = (e) => {

            e.stopPropagation();

            setSel(toggle(getSel(), all, key, value));

            sync();

            applyFilters();

          };

          return row;

        };

        panel.appendChild(mkRow('All', '__all__', allSel));

        all.forEach(x => panel.appendChild(mkRow(x.length > 30 ? x.slice(0, 29) + '…' : x, x, sel.has(x))));

      };

      btn.onclick = (e) => {

        e.stopPropagation();

        const willOpen = panel.style.display === 'none';

        closeAllPanels();

        panel.style.display = willOpen ? 'block' : 'none';

      };

      sync();

      wrap.appendChild(btn);

      wrap.appendChild(panel);

      return wrap;

    };



    if (allPeople.length > 1) {

      bar.appendChild(mkDropdown('People', allPeople, () => selPeople, (s) => { selPeople = s; }, 'people'));

    }

    if (allClients.length > 1) {

      bar.appendChild(mkDropdown('Client', allClients, () => selClients, (s) => { selClients = s; }, 'clients'));

    }

    // Any click elsewhere in the timeline pane closes open menus. paneEl is

    // recreated each render, so this listener doesn't accumulate.

    paneEl.addEventListener('click', closeAllPanels);

  }



  const ctrl = document.createElement('div');

  ctrl.style.cssText = 'display:flex;align-items:center;gap:8px;justify-content:flex-end;padding:0 4px 6px;font-size:11px;color:var(--muted);font-family:IBM Plex Mono,monospace';

  const ctrlLabel = document.createElement('label');

  ctrlLabel.textContent = 'Scale max';

  ctrlLabel.style.cssText = 'cursor:default';

  const slider = document.createElement('input');

  slider.type = 'range';

  slider.min = '10'; slider.max = '100'; slider.step = '5';

  slider.value = String(scaleMax);

  slider.style.cssText = 'width:120px;accent-color:#16a34a;cursor:pointer';

  const valOut = document.createElement('span');

  valOut.textContent = String(scaleMax);

  valOut.style.cssText = 'min-width:24px;text-align:right;color:var(--text)';

  ctrlLabel.setAttribute('for', `heatscale-${projectId}`);

  slider.id = `heatscale-${projectId}`;

  ctrl.appendChild(ctrlLabel);

  ctrl.appendChild(slider);

  ctrl.appendChild(valOut);

  paneEl.appendChild(ctrl);



  const container = document.createElement('div');

  container.style.cssText = `width:100%;height:${totalH}px;min-height:${totalH}px`;

  paneEl.appendChild(container);

  const detailBox = document.createElement('div');

  detailBox.className = 'tl-heat-detail';

  detailBox.style.cssText = 'padding:8px 4px 4px;font-size:11px;color:var(--muted)';

  detailBox.textContent = 'Click a day to see the sessions that contributed.';

  paneEl.appendChild(detailBox);



  const chart = echarts.init(container, null, { renderer: 'canvas' });

  chart.setOption({

    backgroundColor: 'transparent',

    animation: false,

    title: titles,

    tooltip: {

      trigger: 'item',

      backgroundColor: '#0d1b2e',

      borderColor: '#1e3a5f',

      textStyle: { color: '#c7d5ef', fontSize: 11, fontFamily: 'IBM Plex Mono' },

      formatter: params => {

        const d = params.data;

        if (!d || !d.value) return '';

        const date = d.value[0], count = d.value[1];

        return `<b>${escapeHtml(date)}</b> — ${count} task${count === 1 ? '' : 's'} across ${d.scount} session${d.scount === 1 ? '' : 's'}`;

      },

    },

    visualMap: {

      type: 'piecewise',

      show: true,

      orient: 'horizontal',

      left: 'center',

      bottom: 0,

      itemWidth: 11, itemHeight: 11,

      textStyle: { color: '#8b9cba', fontSize: 9, fontFamily: 'IBM Plex Mono' },

      pieces: _heatmapPieces(scaleMax),

    },

    calendar: calendars,

    series: series,

  });



  const renderDetail = (person, date) => {

    const list = detailByPersonDay[`${person}|${date}`] || [];

    if (!list.length) {

      detailBox.innerHTML = `<span style="color:var(--muted)">${escapeHtml(date)} — no sessions</span>`;

      return;

    }

    const total = list.reduce((a, s) => a + s.count, 0);

    const rows = list.map(s => {

      const cli = s.client && s.client !== '(none)'

        ? `<span class="tl-heat-sess-client">${escapeHtml(s.client)}</span>` : '';

      return `<div class="tl-heat-sess"><span class="tl-heat-sess-name">${escapeHtml(s.name || '(unknown)')}</span>` +

        cli +

        `<span class="tl-heat-sess-count">${s.count} task${s.count === 1 ? '' : 's'}</span></div>`;

    }).join('');

    detailBox.innerHTML =

      `<div class="tl-heat-detail-head">${escapeHtml(person)} · ${escapeHtml(date)} · ${total} task${total === 1 ? '' : 's'} · ${list.length} session${list.length === 1 ? '' : 's'}</div>${rows}`;

  };



  chart.on('click', params => {

    if (params.componentType !== 'series' || !params.data || !params.data.value) return;

    renderDetail(params.data.person, params.data.value[0]);

  });



  slider.addEventListener('input', () => {

    scaleMax = Math.min(100, Math.max(10, parseInt(slider.value, 10) || 25));

    valOut.textContent = String(scaleMax);

    localStorage.setItem(`meridian_heatmap_max_${projectId}`, String(scaleMax));

    chart.setOption({ visualMap: { pieces: _heatmapPieces(scaleMax) } });

  });



  const pnl = state.panels[projectId];

  if (pnl) pnl._heatchart = chart;



  applyFilters = () => {

    ({ calendars, series, titles, totalH } = computeView());

    container.style.height = `${totalH}px`;

    container.style.minHeight = `${totalH}px`;

    chart.setOption(

      { title: titles, calendar: calendars, series: series },

      { replaceMerge: ['calendar', 'series', 'title'] }

    );

    try { chart.resize(); } catch (_) {}

  };



  try { new ResizeObserver(() => { try { chart.resize(); } catch (_) {} }).observe(container); } catch (_) {}

}

function _renderTimelineGantt(projectId, data, paneEl) {

  /** Per-session ECharts gantt — the secondary "Detail" timeline view. */

  if (!paneEl) return;

  const p = state.panels[projectId];

  const { tasks = [], goal_events = [] } = data || {};



  const parseTs = ts => {

    if (!ts) return null;

    try { return new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z'); } catch (_) { return null; }

  };



  const sessionNames = [...new Set(tasks.map(t => t.session_name || '(unknown)'))];

  const yCategories = [...sessionNames, 'goal'];



  const STATUS_COLOR = { done: '#34d399', failed: '#f87171', in_progress: '#6c8fff', pending: '#9ca3af' };



  const byStatus = {};

  tasks.forEach(t => {

    const d = parseTs(t.created_at);

    if (!d) return;

    const st = t.status || 'pending';

    if (!byStatus[st]) byStatus[st] = [];

    byStatus[st].push({

      value: [d.getTime(), t.session_name || '(unknown)'],

      desc: t.description || '',

      sess: t.session_name || '(unknown)',

      ts: t.created_at,

      status: st,

    });

  });



  const series = Object.entries(byStatus).map(([st, pts]) => ({

    name: st,

    type: 'scatter',

    symbol: 'rect',

    symbolSize: [36, 10],

    itemStyle: { color: STATUS_COLOR[st] || '#6b7280', opacity: 0.85 },

    emphasis: { scale: 1.4, itemStyle: { opacity: 1 } },

    data: pts,

  }));



  const goalByKey = new Map();

  goal_events.forEach(g => {

    if (g.field === 'version_goal') {

      const s = g.new_summary || '';

      if (s.startsWith('[AUTO SUMMARY') || s.startsWith('- [DONE]') || s.startsWith('- [PENDING]')) return;

    }

    const key = g.field + (g.updated_at || '').slice(0, 13);

    if (!goalByKey.has(key) || g.version > (goalByKey.get(key).version || 0)) goalByKey.set(key, g);

  });



  const GOAL_COLOR = { sprint_updated_at: '#6c8fff', ns_updated_at: '#fbbf24', content_updated_at: '#a78bfa' };

  const goalPts = [];

  const markLineData = [];

  goalByKey.forEach(g => {

    const d = parseTs(g.updated_at);

    if (!d) return;

    const color = GOAL_COLOR[g.field] || '#a78bfa';

    const lbl = g.field.replace('_updated_at', '').replace('_', ' ');

    const ms = d.getTime();

    goalPts.push({ value: [ms, 'goal'], field: lbl, version: g.version, ts: g.updated_at, itemStyle: { color } });

    markLineData.push({ xAxis: ms, lineStyle: { color, type: 'dashed', width: 1, opacity: 0.5 }, label: { show: false } });

  });



  if (goalPts.length) {

    series.push({

      name: 'goal',

      type: 'scatter',

      symbol: 'diamond',

      symbolSize: 9,

      data: goalPts,

      markLine: { silent: true, symbol: 'none', data: markLineData },

    });

  }



  paneEl.innerHTML = '';

  const container = document.createElement('div');

  container.style.cssText = 'width:100%;height:100%;min-height:300px';

  paneEl.appendChild(container);



  const chart = echarts.init(container, null, { renderer: 'canvas' });



  chart.setOption({

    backgroundColor: 'transparent',

    animation: false,

    tooltip: {

      trigger: 'item',

      backgroundColor: '#0d1b2e',

      borderColor: '#1e3a5f',

      textStyle: { color: '#c7d5ef', fontSize: 11, fontFamily: 'IBM Plex Mono' },

      confine: true,

      className: 'timeline-tooltip',

      extraCssText: 'max-width:340px;white-space:normal;',

      position: (point, params, dom, rect, size) => {

        const x = point[0], y = point[1];

        const containerWidth = (size && size.viewSize && size.viewSize[0]) || 0;

        return x > containerWidth * 0.6 ? [x - 300, y] : [x + 20, y];

      },

      formatter: params => {

        const d = params.data;

        if (d.field) return `<b>${escapeHtml(d.field)}</b> v${d.version}<br><span style="color:#8b9cba;font-size:9px">${escapeHtml(d.ts || '')}</span>`;

        return `<b>${escapeHtml(d.sess)}</b><br><span style="color:${STATUS_COLOR[d.status] || '#9ca3af'}">${escapeHtml(d.status)}</span> · <span style="color:#8b9cba;font-size:9px">${escapeHtml(d.ts || '')}</span><br><span class="timeline-tooltip-desc" style="color:#c7d5ef">${escapeHtml(d.desc)}</span>`;

      },

    },

    legend: {

      top: 0, right: 0,

      textStyle: { color: '#8b9cba', fontSize: 10, fontFamily: 'IBM Plex Mono' },

      itemWidth: 10, itemHeight: 8,

    },

    grid: { top: 26, right: 12, bottom: 26, left: 8, containLabel: true },

    xAxis: {

      type: 'time',

      axisLabel: { color: '#8b9cba', fontFamily: 'IBM Plex Mono', fontSize: 9, hideOverlap: true },

      splitLine: { lineStyle: { color: '#1e2d4a' } },

      axisLine: { lineStyle: { color: '#1e2d4a' } },

    },

    yAxis: {

      type: 'category',

      data: yCategories,

      inverse: true,

      axisLabel: {

        color: '#8b9cba', fontFamily: 'IBM Plex Mono', fontSize: 9,

        formatter: v => v.length > 22 ? v.slice(0, 21) + '…' : v,

        width: 148, overflow: 'truncate',

      },

      splitLine: { lineStyle: { color: '#1e2d4a55' } },

      axisLine: { lineStyle: { color: '#1e2d4a' } },

    },

    series,

    dataZoom: [{ type: 'inside', xAxisIndex: 0 }],

  });



  if (p) p._echart = chart;



  const tlRangeKey = `meridian_tl_range_${projectId}`;

  const fromInput = document.getElementById(`timeline-from-${projectId}`);

  const toInput   = document.getElementById(`timeline-to-${projectId}`);

  const errEl     = document.getElementById(`timeline-range-err-${projectId}`);



  const setZoom = (from, to) => {

    if (from || to) {

      try { chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0, startValue: from ? from.getTime() : undefined, endValue: to ? to.getTime() : undefined }); } catch (_) {}

    } else {

      try { chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0, start: 0, end: 100 }); } catch (_) {}

    }

  };



  const applyRange = () => {

    const fv = fromInput ? fromInput.value : '';

    const tv = toInput ? toInput.value : '';

    const from = fv ? new Date(fv) : null;

    const to   = tv ? new Date(tv + 'T23:59:59Z') : null;

    if (from && to && from >= to) {

      if (errEl) { errEl.textContent = 'From must be before To'; errEl.style.display = ''; }

      return;

    }

    if (errEl) errEl.style.display = 'none';

    try {

      if (fv || tv) { localStorage.setItem(tlRangeKey, JSON.stringify({ from: fv, to: tv })); }

      else          { localStorage.removeItem(tlRangeKey); }

    } catch (_) {}

    setZoom(from, to);

  };



  const savedRange = (() => { try { return JSON.parse(localStorage.getItem(tlRangeKey) || 'null'); } catch (_) { return null; } })();

  if (savedRange && fromInput && toInput) {

    fromInput.value = savedRange.from || '';

    toInput.value   = savedRange.to   || '';

    if (savedRange.from && savedRange.to) setZoom(new Date(savedRange.from), new Date(savedRange.to + 'T23:59:59Z'));

  }



  if (fromInput) fromInput.addEventListener('change', applyRange);

  if (toInput)   toInput.addEventListener('change',   applyRange);



  const nowD = new Date();

  const todayStr = nowD.toISOString().slice(0, 10);

  const r7Btn  = document.getElementById(`timeline-r7d-${projectId}`);

  const r30Btn = document.getElementById(`timeline-r30d-${projectId}`);

  const rAllBtn = document.getElementById(`timeline-rall-${projectId}`);

  if (r7Btn) r7Btn.onclick = () => {

    if (fromInput) fromInput.value = new Date(nowD - 7 * 86400000).toISOString().slice(0, 10);

    if (toInput) toInput.value = todayStr;

    applyRange();

  };

  if (r30Btn) r30Btn.onclick = () => {

    if (fromInput) fromInput.value = new Date(nowD - 30 * 86400000).toISOString().slice(0, 10);

    if (toInput) toInput.value = todayStr;

    applyRange();

  };

  if (rAllBtn) rAllBtn.onclick = () => {

    if (fromInput) fromInput.value = '';

    if (toInput)   toInput.value   = '';

    if (errEl) errEl.style.display = 'none';

    try { localStorage.removeItem(tlRangeKey); } catch (_) {}

    setZoom(null, null);

  };



  try { new ResizeObserver(() => { try { chart.resize(); } catch (_) {} }).observe(container); } catch (_) {}

}