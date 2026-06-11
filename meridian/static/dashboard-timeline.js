// dashboard-timeline.js — extracted from dashboard.js
// Dependency trace: _heatmapPieces and _heatmapMaxFor have no category-C deps (B only).
// renderTimeline, _renderTimelineHeatmap, _renderTimelineGantt have C deps (state, api,
// escapeHtml, _colorForHuman, loadTimeline) and remain in dashboard.js.

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
