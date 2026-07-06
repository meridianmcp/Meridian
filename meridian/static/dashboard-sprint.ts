// dashboard-sprint.js — sprint/queue renderers extracted from dashboard.js
// Depends on: dashboard-utils.js (escapeHtml, getPanelState, formatRelativeTime, _PLAN_LABELS)

// 233bae67 — sprint dependency DAG (Cytoscape, guarded/CDN-global).
import { buildSprintDagElements, mountSprintDag } from "./components/sprintGraph";

// c2fe20c3 — history-signal badges for a sprint item so pending items aren't
// indistinguishable: a stall counter (↻N), a "retried" tag (claimed before, now
// back to pending), and a live pulse dot for an in-progress (claimed) item. Pure
// + exported for the UI test. The pulse keyframes are injected once.
function _ensureSprintPulseStyle() {
  if (typeof document === 'undefined') return;
  if (document.getElementById('sprint-pulse-style')) return;
  const st = document.createElement('style');
  st.id = 'sprint-pulse-style';
  st.textContent = '@keyframes sprintPulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,0.6)}70%{box-shadow:0 0 0 5px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}';
  document.head.appendChild(st);
}

function _sprintHistoryBadges(it: any) {
  if (!it) return '';
  _ensureSprintPulseStyle();
  let html = '';
  const stall = Number(it.stall_count || 0);
  if (stall > 0) {
    html += `<span class="sprint-stall-badge" title="Stalled and re-queued ${stall} time(s)" style="margin-left:6px;font-size:9px;font-weight:700;color:#f59e0b;background:#f59e0b22;border-radius:3px;padding:1px 5px">↻${stall}</span>`;
  }
  const isPending = it.status === 'pending' || it.status === 'todo';
  if (isPending && it.claimed_at) {
    html += `<span class="sprint-retried-badge" title="Was claimed by a session before — now back to pending" style="margin-left:6px;font-size:9px;color:var(--muted);border:1px solid var(--border);border-radius:3px;padding:1px 5px">retried</span>`;
  }
  if (it.status === 'in_progress' && it.claimed_at) {
    html += `<span class="sprint-live-dot" title="Claimed by an active session" style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#22c55e;margin-left:6px;animation:sprintPulse 1.6s infinite;vertical-align:middle"></span>`;
  }
  return html;
}
if (typeof window !== 'undefined') window._sprintHistoryBadges = _sprintHistoryBadges;

export function _renderPlanBadge(me: any) {

  const planColors: Record<string, string> = { free: '#3b82f6', trial: '#059669', standard: '#3b82f6', pro: '#7c3aed', admin: '#9ca3af' };

  const planLabels = _PLAN_LABELS;

  const plan = (me.is_internal || me.is_admin) ? 'admin' : (me.plan || 'free');

  // Plan badge near version string

  const verEl = document.getElementById('server-version');

  if (verEl && !document.getElementById('plan-badge')) {

    const badge = document.createElement('span');

    badge.id = 'plan-badge';

    const badgeColor = planColors[plan] || '#9ca3af';

    const badgeLabel = plan === 'free' && me.days_remaining != null
      ? `Free · ${me.days_remaining}d left`
      : (planLabels[plan] || plan);

    badge.title = `${planLabels[plan] || plan} plan`;

    badge.style = `margin-left:6px;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:0.04em;background:${badgeColor}22;color:${badgeColor};border:1px solid ${badgeColor}44;vertical-align:middle`;

    badge.textContent = badgeLabel;

    verEl.parentNode!.insertBefore(badge, verEl.nextSibling);

  }

  // G2.11 — Billing button. With a Stripe customer → "Manage" opens the

  // Stripe Customer Portal; without one (free/trial that never paid) →

  // "Upgrade" routes to /pricing.

  // Admin / internal staff plans have nothing to upgrade — only show a

  // billing affordance when there's a real Stripe customer to manage.

  const noUpgrade = plan === 'admin' || !!me.is_internal;

  const planBadge = document.getElementById('plan-badge');

  if (planBadge && !document.getElementById('billing-link')) {

    const hasStripe = !!me.has_stripe_customer;

    if (hasStripe || !noUpgrade) {

      const link = document.createElement('a');

      link.id = 'billing-link';

      link.href = hasStripe ? '/billing/portal' : '/pricing';

      link.textContent = hasStripe ? 'Manage' : 'Upgrade';

      link.title = hasStripe ? 'Open Stripe billing portal' : 'See plans and upgrade';

      link.style = 'margin-left:6px;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:600;letter-spacing:0.03em;background:transparent;color:var(--accent);border:1px solid var(--accent)55;vertical-align:middle;text-decoration:none;cursor:pointer';

      planBadge.parentNode!.insertBefore(link, planBadge.nextSibling);

    }

  }

  // B — Single dismissable trial banner for free-tier users (893e2a1f)
  // Color changes based on days elapsed in the 30-day trial.

  if (plan === 'free' && !me.is_internal && !me.expired && !isDemoMode()
      && !document.getElementById('trial-banner')
      && !sessionStorage.getItem('trial-banner-dismissed')) {

    const daysLeft = me.days_remaining != null ? me.days_remaining : 30;

    const elapsed = Math.max(0, 30 - daysLeft);

    const bannerBg = elapsed >= 28 ? '#dc2626' : elapsed >= 25 ? '#d97706' : '#ca8a04';

    const upgradeUrl = window.state?.serverConfig?.stripe_payment_link || '/pricing';

    const daysStr = me.days_remaining != null ? `${me.days_remaining} day${me.days_remaining !== 1 ? 's' : ''}` : 'limited time';

    const b = document.createElement('div');

    b.id = 'trial-banner';

    b.style = `position:fixed;top:0;left:0;right:0;z-index:9997;background:${bannerBg};color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em;display:flex;align-items:center;justify-content:center;gap:10px`;

    b.innerHTML = `<span>Free trial · <strong>${daysStr} remaining</strong></span><a href="${escapeHtml(upgradeUrl)}" style="color:#fff;text-decoration:underline;font-weight:600;white-space:nowrap">Upgrade to Standard →</a><button onclick="sessionStorage.setItem('trial-banner-dismissed','1');this.closest('#trial-banner').remove();document.body.style.paddingTop=Math.max(0,parseInt(document.body.style.paddingTop||'0',10)-28)+'px'" style="background:none;border:none;color:rgba(255,255,255,0.7);font-size:16px;cursor:pointer;padding:0 0 0 6px;line-height:1" title="Dismiss for this session">×</button>`;

    document.body.prepend(b);

    document.body.style.paddingTop = ((parseInt(document.body.style.paddingTop || '0', 10)) + 28) + 'px';

  }

  // C — Update hosted-label to "Hosted (shared pool)" + upgrade link for free tier (0f9ce3fb)

  if (isHostedMode() && !isHostedAdmin()) {

    const hostedLabel = document.querySelector('.hosted-label');

    if (hostedLabel && !hostedLabel.dataset.planUpdated) {

      hostedLabel.dataset.planUpdated = '1';

      hostedLabel.textContent = 'Hosted (shared pool)';

      if (plan === 'free' && !me.is_internal && !document.getElementById('db-upgrade-link')) {

        const upgradeLink = document.createElement('a');

        upgradeLink.id = 'db-upgrade-link';

        upgradeLink.href = '/pricing';

        upgradeLink.textContent = 'Upgrade for dedicated DB →';

        upgradeLink.style.cssText = 'display:block;margin-top:3px;font-size:9px;color:var(--accent);text-decoration:none;opacity:.85;font-family:var(--font-mono);letter-spacing:.02em';

        hostedLabel.insertAdjacentElement('afterend', upgradeLink);

      }

    }

  }

  if (me.expired && !document.getElementById('expired-banner')) {

    const b = document.createElement('div');

    b.id = 'expired-banner';

    const expLabel = (_PLAN_LABELS[plan] || plan) + ' expired';

    b.style = 'position:fixed;top:0;left:0;right:0;z-index:9998;background:#dc2626;color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em';

    b.innerHTML = `${expLabel}. <a href="/pricing" style="color:#fff;text-decoration:underline">Upgrade to continue →</a>`;

    document.body.prepend(b);

    document.body.style.paddingTop = ((parseInt(document.body.style.paddingTop || '0', 10)) + 28) + 'px';

  }

  // GitHub onboarding banner — show once to hosted users who haven't connected a repo

  if (me.github_connected === false && !isDemoMode() && !document.getElementById('github-onboarding-banner') && !sessionStorage.getItem('github-banner-dismissed')) {

    const b = document.createElement('div');

    b.id = 'github-onboarding-banner';

    b.style = 'position:fixed;top:0;left:0;right:0;z-index:9997;background:#7c3aed;color:#fff;text-align:center;padding:5px 12px;font-size:12px;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:10px';

    b.innerHTML = `<span>Connect your GitHub repo — give your AI sessions live code access, no extra installs needed.</span><a href="#settings" onclick="document.querySelector('.vtab-btn[data-vtab=settings]')?.click()" style="color:#fff;text-decoration:underline;white-space:nowrap">Connect now →</a><button onclick="sessionStorage.setItem('github-banner-dismissed','1');this.closest('#github-onboarding-banner').remove();document.body.style.paddingTop=Math.max(0,parseInt(document.body.style.paddingTop||'0',10)-28)+'px'" style="background:none;border:none;color:rgba(255,255,255,0.7);font-size:16px;cursor:pointer;padding:0 0 0 6px;line-height:1" title="Dismiss">×</button>`;

    document.body.prepend(b);

    document.body.style.paddingTop = ((parseInt(document.body.style.paddingTop || '0', 10)) + 28) + 'px';

  }



  // Sign-out link is created earlier in hideHostedAdminControls() so it

  // appears for all hosted users (incl. free tier) even before /me returns.

  // Here we just enrich the tooltip with the signed-in email if available.

  ensureSignOutLink(me.email);

  // Show workspace switcher if the user belongs to more than one workspace.

  ensureWorkspaceSwitcher();

}

export function renderSprintProgress(projectId: string, items: any) {

  /** Full grouped sprint board — replaces the old plain progress bar. */

  const root = document.getElementById(`live-sprint-progress-${projectId}`);

  if (!root) return;



  const statusIcon = (s: any) => (({

    pending: '○', todo: '○', in_progress: '◑',

    done: '●', failed: '✕', skipped: '—', pushed: '→', indeterminate: '⚠'

  } as Record<string, string>)[s] || '?');

  const statusColor = (s: any) => (({

    pending: 'var(--muted)', todo: 'var(--muted)',

    in_progress: 'var(--accent)',

    done: 'var(--accent-green)',

    failed: '#e05',

    skipped: 'var(--muted)',

    pushed: 'var(--accent)',

    indeterminate: '#fbbf24'

  } as Record<string, string>)[s] || 'var(--muted)');

  const activeSet = new Set(['pending', 'todo', 'in_progress']);



  if (items.length === 0) {

    root.innerHTML = `

      <div class="live-empty">No sprint items. Add one below.</div>

      <div class="sprint-add-row" style="margin-top:6px">

        <input class="live-add-input" id="sprint-add-input-${projectId}"

               placeholder="version:title (e.g. v1.0:My item)">

        <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"

                style="margin-left:4px">+ Add</button>

      </div>`;

    root.querySelector('.sprint-add-btn')!.onclick =

      () => addSprintItemFromInput(projectId);

    wireSprintAddEnter(projectId, root);

    return;

  }



  // Show ALL pending items grouped by version — don't filter to current sprint version.

  // Current sprint string is still used for new-item version defaulting (addSprintItemFromInput).

  const activeStatuses = new Set(['pending', 'todo', 'in_progress']);



  // Include active items + done/skipped items from versions that still have active peers.

  const activeVersions = new Set(items.filter((it: any) => activeStatuses.has(it.status)).map((it: any) => it.version));

  let displayItems = items.filter((it: any) =>

    activeStatuses.has(it.status) || (it.version && activeVersions.has(it.version))

  );

  if (displayItems.length === 0) displayItems = items.filter((it: any) => activeStatuses.has(it.status));



  if (displayItems.length === 0) {

    root.innerHTML = `

      <div class="live-empty" style="color:var(--accent-green)">🎉 Sprint complete! All items done.</div>

      <div class="sprint-add-row" style="margin-top:6px">

        <input class="live-add-input" id="sprint-add-input-${projectId}"

               placeholder="version:title  or  just title" style="flex:1">

        <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"

                style="margin-left:4px">+ Add</button>

      </div>`;

    root.querySelector('.sprint-add-btn')!.onclick = () => addSprintItemFromInput(projectId);

    wireSprintAddEnter(projectId, root);

    return;

  }



  // Indeterminate items — amber "⚠ Needs attention" section above the queue.

  const indeterminateItems = items.filter((it: any) => it.status === 'indeterminate');

  let html = '';

  if (indeterminateItems.length > 0) {

    html += `<div style="background:#422b00;border:1px solid #fbbf24;border-radius:6px;padding:8px 10px;margin-bottom:10px">

      <div style="color:#fbbf24;font-weight:600;margin-bottom:6px;font-size:12px">⚠ Needs attention (${indeterminateItems.length})</div>`;

    html += indeterminateItems.map((it: any) => `

      <div class="sprint-item-row" data-item="${escapeHtml(it.id)}" style="background:transparent;border-bottom:1px solid #5a3b00;padding:4px 0">

        <span class="sprint-item-icon" style="color:#fbbf24">⚠</span>

        <span class="sprint-item-title">${escapeHtml(it.title)}</span>

        <span class="sprint-item-ver">${escapeHtml(it.version)}</span>

        <span class="sprint-item-actions">

          <button class="sprint-btn" title="Mark done"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">✓ Done</button>

          <button class="sprint-btn" title="Back to pending"

            onclick="fetch('/projects/${escapeHtml(projectId)}/sprint-items/${escapeHtml(it.id)}',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'pending'})}).then(()=>renderSprintProgress(${JSON.stringify(projectId)},items.map(x=>x.id===it.id?{...x,status:'pending'}:x)))">↩ Pending</button>

          <button class="sprint-btn sprint-btn-fail" title="Mark failed"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">✕ Fail</button>

          <button class="sprint-btn" title="Backburner (skip)" style="color:var(--muted)"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">— Backburner</button>

        </span>

      </div>`).join('');

    html += `</div>`;

  }

  // Human-assigned items — "Your tasks" section above the pending queue.

  const humanItems = items.filter((it: any) => it.milestone_type === 'human' && activeSet.has(it.status));

  if (humanItems.length > 0) {

    html += `<div style="background:rgba(59,130,246,0.08);border:1px solid rgba(59,130,246,0.35);border-radius:6px;padding:8px 10px;margin-bottom:10px">

      <div style="color:var(--accent);font-weight:600;margin-bottom:6px;font-size:12px">👤 Your tasks (${humanItems.length})</div>`;

    html += humanItems.map((it: any) => `

      <div class="sprint-item-row" data-item="${escapeHtml(it.id)}" style="background:transparent;border-bottom:1px solid rgba(59,130,246,0.2);padding:4px 0">

        <span class="sprint-item-icon" style="color:var(--accent)">👤</span>

        <span class="sprint-item-title">${escapeHtml(it.title)}</span>

        <span class="sprint-item-ver">${escapeHtml(it.version)}</span>

        <span class="sprint-item-actions">

          <button class="sprint-btn" title="Mark done"

            onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">✓ Done</button>

        </span>

      </div>`).join('');

    html += `</div>`;

  }

  // Build parent → children map across all items (for progress counts + tree display).

  const allChildrenOf = new Map();

  items.forEach((it: any) => {

    if (it.parent_id) {

      if (!allChildrenOf.has(it.parent_id)) allChildrenOf.set(it.parent_id, []);

      allChildrenOf.get(it.parent_id).push(it);

    }

  });

  // IDs of displayed items that have children (parents in displayItems).

  const displayedParentIds = new Set(

    displayItems.map((it: any) => it.id).filter((id: any) => allChildrenOf.has(id))

  );

  // Children of displayed parents, keyed by parent id (to render under parent, not standalone).

  const displayChildrenOf = new Map();

  displayItems.forEach((it: any) => {

    if (it.parent_id && displayedParentIds.has(it.parent_id)) {

      if (!displayChildrenOf.has(it.parent_id)) displayChildrenOf.set(it.parent_id, []);

      displayChildrenOf.get(it.parent_id).push(it);

    }

  });

  const renderItem = (it: any, isChild: any) => {

    const icon = statusIcon(it.status);

    const color = statusColor(it.status);

    const isActive = activeSet.has(it.status);

    const meta = it.pushed_to

      ? `<span class="sprint-item-meta">→ ${escapeHtml(it.pushed_to)}</span>`

      : '';

    const notesHtml = it.notes && !it.pushed_to
      ? `<div class="sprint-item-notes" style="font-size:10px;color:var(--muted);margin-top:2px;line-height:1.4;white-space:pre-wrap;word-break:break-word">${escapeHtml(it.notes.length > 180 ? it.notes.slice(0, 180) + '…' : it.notes)}</div>`
      : '';

    const _resources = (() => { try { return JSON.parse(it.touches_resources || '[]'); } catch { return []; } })();
    const resourcesHtml = _resources.length > 0
      ? `<div class="sprint-item-resources" style="display:flex;flex-wrap:wrap;gap:3px;margin-top:3px">${_resources.map((r: any) => {
          const chipColor = r.startsWith('note:') ? 'var(--accent-blue,#3b82f6)' : r.startsWith('decision:') ? '#a78bfa' : 'var(--muted)';
          return `<span class="resource-chip" onclick="resourceChipClick('${escapeHtml(projectId)}','${escapeHtml(r)}')" style="font-size:9px;padding:1px 5px;border-radius:3px;cursor:pointer;background:var(--surface-2);border:1px solid var(--border);color:${chipColor};font-family:var(--font-mono)">${escapeHtml(r)}</span>`;
        }).join('')}</div>`
      : '';

    const editBtn = `<button class="sprint-btn" title="Edit title/version"

             onclick="sprintItemEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">✏</button>`;

    const notesBtn = `<button class="sprint-btn" title="Add/edit notes"

             onclick="sprintItemNotesEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">📝</button>`;

    const resourcesBtn = `<button class="sprint-btn" title="Edit touches_resources"
             onclick="sprintItemResourcesEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}',${JSON.stringify(it.touches_resources||null)})">🔗</button>`;

    const feedbackHtml = '';

    const canEdit = it.status === 'pending' || it.status === 'todo';

    const actions = isActive

      ? `<span class="sprint-item-actions">

           <button class="sprint-btn" title="Done"

             onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">✓</button>

           <button class="sprint-btn" title="Skip"

             onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">—</button>

           <button class="sprint-btn sprint-btn-fail" title="Fail"

             onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">✕</button>

           <button class="sprint-btn sprint-btn-push" title="Push to next version"

             onclick="sprintPushPrompt('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">→</button>

           ${canEdit ? editBtn : ''}

           ${notesBtn}

           ${resourcesBtn}

         </span>`

      : `<span class="sprint-item-actions">${meta}${feedbackHtml}</span>`;

    // Children progress badge for parent items.

    const allKids = allChildrenOf.get(it.id) || [];

    const kidDone = allKids.filter((c: any) => c.status === 'done').length;

    const childBadge = allKids.length > 0

      ? `<span style="font-size:10px;color:var(--muted);margin-left:4px">[${kidDone}/${allKids.length}]</span>`

      : '';

    const indBadge = it.status === 'indeterminate'

      ? `<span style="color:#fbbf24;margin-left:4px;font-size:11px">⚠</span>`

      : '';

    const indentStyle = isChild

      ? 'margin-left:16px;border-left:2px solid var(--border);padding-left:8px;'

      : '';

    const rowHtml = `<div class="sprint-item-row" data-item="${escapeHtml(it.id)}"

      data-title="${escapeHtml(it.title)}" data-version="${escapeHtml(it.version || '')}"

      data-notes="${escapeHtml(it.notes || '')}"

      style="${indentStyle}">

      <span class="sprint-item-icon" style="color:${color}">${icon}</span>

      <div style="flex:1;min-width:0">

        <span class="sprint-item-title">${escapeHtml(it.title)}${indBadge}${childBadge}${_sprintHistoryBadges(it)}</span>

        ${notesHtml}

        ${resourcesHtml}

      </div>

      <span class="sprint-item-ver">${escapeHtml(it.version || '')}</span>

      ${actions}

    </div>`;

    // Subtasks collapsed by default; expand on click.

    const dispKids = displayChildrenOf.get(it.id) || [];

    const childrenBlock = dispKids.length > 0

      ? `<details style="margin-left:16px;border-left:2px solid var(--border);margin-bottom:2px">

           <summary style="cursor:pointer;padding:2px 6px;font-family:var(--font-mono);font-size:10px;color:var(--muted);list-style:none;display:flex;align-items:center;gap:4px;user-select:none">

             <span>▸</span><span>${dispKids.length} subtask${dispKids.length !== 1 ? 's' : ''} · ${kidDone}/${allKids.length} done</span>

           </summary>

           <div style="padding:2px 0">

             ${dispKids.map((c: any) => renderItem(c, true)).join('')}

           </div>

         </details>`

      : '';

    return rowHtml + childrenBlock;

  };

  // Group by version (then item_group within version).

  const versionOrder = [...new Set(displayItems.map((it: any) => it.version || ''))];

  const groups = new Map();

  displayItems.forEach((it: any) => {

    const g = it.version || '';

    if (!groups.has(g)) groups.set(g, []);

    groups.get(g).push(it);

  });

  for (const [groupName, groupItems] of groups) {

    if (groupName) {

      html += `<div class="sprint-group-header">${escapeHtml(groupName)}</div>`;

    }

    // Render only top-level items; children of displayed parents are rendered under their parent.

    const topLevel = groupItems.filter((it: any) => !it.parent_id || !displayedParentIds.has(it.parent_id));

    html += topLevel.map((it: any) => renderItem(it, false)).join('');

  }



  // Footer: progress bar + add input (count only displayed sprint items)

  const total = displayItems.length;

  const done = displayItems.filter((i: any) => i.status === 'done').length;

  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  // v0.9 — color-code completion: gray (0 done), amber (some), green (all).

  // One CSS variable; no backend change. Communicates "we've started" vs

  // "we're nearly there" at a glance during standup.

  const pctColor = (done === 0)

    ? 'var(--muted)'

    : (done === total ? 'var(--accent-green)' : '#fbbf24');

  html += `<div class="sprint-footer">

    <span class="sprint-pct" style="color:${pctColor};font-weight:600">${done}/${total} · ${pct}%</span>

    <div class="sprint-add-row">

      <input class="live-add-input" id="sprint-add-input-${projectId}"

             placeholder="version:title  or  just title" style="flex:1">

      <button class="secondary sprint-add-btn" data-pid="${escapeHtml(projectId)}"

              style="margin-left:4px">+ Add</button>

    </div>

  </div>`;



  // Backburner section — pushed/post-launch items collapsed by default

  const pushedItems = items.filter((it: any) => it.status === 'pushed');

  if (pushedItems.length > 0) {

    html += `<details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-1)">

      <summary style="cursor:pointer;padding:6px 10px;font-family:var(--font-mono);font-size:10px;color:var(--muted);letter-spacing:.05em;user-select:none;list-style:none;display:flex;align-items:center;gap:6px">

        <span>⏸</span><span>Backburner (${pushedItems.length} pushed)</span>

      </summary>

      <div style="padding:4px 10px 8px">

        ${pushedItems.map((it: any) => `<div class="sprint-item-row" data-item="${escapeHtml(it.id)}" data-title="${escapeHtml(it.title)}" data-version="${escapeHtml(it.version || '')}" style="display:flex;align-items:center;gap:6px;padding:3px 0;border-top:1px solid var(--border)">

          <span style="color:var(--muted);font-size:10px;flex-shrink:0">→</span>

          <span class="sprint-item-title" style="font-family:var(--font-mono);font-size:10px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</span>

          ${it.pushed_to ? `<span style="font-size:9px;color:var(--accent);background:var(--accent)1a;border:1px solid var(--accent)33;border-radius:3px;padding:0 5px;flex-shrink:0;font-family:var(--font-mono)">${escapeHtml(it.pushed_to)}</span>` : ''}

          <span class="sprint-item-ver" style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(it.version || '')}</span>

          <button class="sprint-btn" title="Edit title/version" onclick="sprintItemEdit('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">✏</button>

        </div>`).join('')}

      </div>

    </details>`;

  }



  root.innerHTML = html;

  root.querySelector('.sprint-add-btn')!.onclick =

    () => addSprintItemFromInput(projectId);

  wireSprintAddEnter(projectId, root);

  // 233bae67 — collapsible dependency DAG below the board. Lazy-mounts a
  // Cytoscape graph (depends_on + file-conflict edges, status colors, critical
  // path) on first open, when window.cytoscape is present. Best-effort: any
  // failure leaves the board untouched.
  try {
    if (Array.isArray(items) && items.length) {
      const dag = document.createElement('details');
      dag.className = 'sprint-dag-wrap';
      dag.style.marginTop = '8px';
      dag.innerHTML =
        '<summary style="cursor:pointer;font-size:10px;color:var(--muted)">Dependency graph</summary>' +
        `<div id="sprint-dag-${escapeHtml(projectId)}" class="sprint-dag" ` +
        'style="width:100%;height:320px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;margin-top:4px"></div>';
      root.appendChild(dag);
      dag.addEventListener('toggle', () => {
        if (!dag.open) return;
        const host = dag.querySelector('.sprint-dag') as HTMLElement | null;
        if (host && !host.dataset.mounted) {
          const cy = mountSprintDag(host, buildSprintDagElements(items));
          if (cy) host.dataset.mounted = '1';
        }
      });
    }
  } catch (e) { /* dependency graph is best-effort */ }

}

export function renderQueue(projectId: string, sprintItems: any = []) {

  /** Render the 5-group sprint board for the Queue tab.

   * Legacy task_log statuses like 'future' still belong in Dev Log, not here. */

  const panel = getPanelState(projectId);

  const sectionState = panel.queueSectionState || (panel.queueSectionState = {

    backburner: true,

    pending: false,

    in_progress: false,

    done: true,

    failed: true,

  });

  const doneLimit = panel.queueDoneLimit || QUEUE_DONE_PAGE_SIZE;
  const totalDoneCount = panel.queueTotalDoneCount != null
    ? panel.queueTotalDoneCount
    : (sprintItems || []).filter((it: any) => it.status === 'done').length;

  const items = (sprintItems || []).slice();

  const sortByNewest = (a: any, b: any) =>

    String(b.completed_at || b.added_at || '').localeCompare(String(a.completed_at || a.added_at || ''));



  const backburner = items

    .filter((it: any) => ['pushed', 'skipped'].includes(it.status))

    .sort(sortByNewest);

  const pending = items

    .filter((it: any) => it.status === 'pending' || it.status === 'todo')

    .sort(sortByNewest);

  const inProgress = items

    .filter((it: any) => it.status === 'in_progress')

    .sort(sortByNewest);

  const failed = items

    .filter((it: any) => it.status === 'failed')

    .sort(sortByNewest);

  const doneAll = items

    .filter((it: any) => it.status === 'done')

    .sort(sortByNewest);

  const done = doneAll.slice(0, doneLimit);



  const renderItem = (it: any) => {

    const version = it.version ? `<span style="font-size:9px;color:var(--accent);background:var(--accent)1a;border:1px solid var(--accent)33;border-radius:999px;padding:1px 6px;font-family:var(--font-mono)">${escapeHtml(it.version)}</span>` : '';

    const pushedTo = it.pushed_to ? `<span style="font-size:9px;color:var(--muted)">→ ${escapeHtml(it.pushed_to)}</span>` : '';

    const tsSource = it.completed_at || it.added_at || '';

    const meta = [

      it.item_group ? `group: ${it.item_group}` : '',

      it.human_id ? `human: ${it.human_id}` : '',

      it.depends_on ? `depends_on: ${it.depends_on}` : '',

      tsSource ? formatRelativeTime(tsSource) : '',

    ].filter(Boolean).join(' · ');

    const canAct = ['pending', 'todo', 'in_progress'].includes(it.status);

    const actions = canAct ? `

      <div style="display:flex;gap:4px;align-items:center;flex-shrink:0">

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Mark done"

          onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','complete')">✓</button>

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Skip"

          onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','skip')">—</button>

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Fail"

          onclick="sprintAction('${escapeHtml(projectId)}','${escapeHtml(it.id)}','fail')">✕</button>

        <button class="secondary" style="padding:1px 6px;font-size:9px" title="Push to next version"

          onclick="sprintPushPrompt('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">→</button>

      </div>` : '';

    // e62ce019 — backburner (pushed/skipped) items get a permanent-delete button.
    const isBackburner = ['pushed', 'skipped'].includes(it.status);

    const archiveBtn = isBackburner ? `

      <div style="flex-shrink:0">

        <button class="secondary" data-demo-hide style="padding:1px 6px;font-size:9px" title="Delete permanently"

          onclick="sprintArchive('${escapeHtml(projectId)}','${escapeHtml(it.id)}')">🗑</button>

      </div>` : '';

    return `<div class="queue-item" data-bb-title="${escapeHtml((it.title || '').toLowerCase())}" data-bb-group="${escapeHtml((it.item_group || '').toLowerCase())}">

      <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">

        <div style="min-width:0;flex:1">

          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

            ${version}

            ${it.slug ? `<span style="font-size:9px;font-family:var(--font-mono);color:var(--muted);background:var(--surface-1);border:1px solid var(--border);border-radius:3px;padding:0 4px;white-space:nowrap" title="${escapeHtml(it.id || '')}">${escapeHtml(it.slug)}</span>` : ''}

            <span style="color:var(--text);font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis">${escapeHtml(it.title || '')}</span>

            ${pushedTo}

          </div>

          ${meta ? `<div class="queue-item-ts" style="margin-left:0;margin-top:3px">${escapeHtml(meta)}</div>` : ''}

          ${it.notes ? `<div style="margin-top:4px;font-size:10px;color:var(--muted);white-space:pre-wrap;word-break:break-word">${escapeHtml(it.notes)}</div>` : ''}

        </div>

        ${actions}${archiveBtn}

      </div>

    </div>`;

  };



  const section = (icon: any, title: any, rows: any, emptyMsg: any, opts: any = {}) => {

    const key = opts.key || '';

    const collapsed = key ? (sectionState[key] ?? !!opts.collapsed) : !!opts.collapsed;

    const footer = opts.footer || '';

    return `<div class="queue-section" data-section="${escapeHtml(key)}" data-collapsed="${collapsed ? 'true' : 'false'}">

      <div class="queue-section-header" role="button" tabindex="0" aria-expanded="${collapsed ? 'false' : 'true'}" data-section-key="${escapeHtml(key)}">

        <span class="queue-section-header-label">${icon} ${title} <span class="queue-section-count">(${opts.count != null ? opts.count : rows.length})</span></span>

        <span class="queue-section-chevron" aria-hidden="true">▶</span>

      </div>

      <div class="queue-section-body">

        <div class="queue-section-body-inner">

          ${rows.length ? rows.map(renderItem).join('') : `<div class="queue-empty">${emptyMsg}</div>`}

          ${footer}

        </div>

      </div>

    </div>`;

  };



  // e62ce019 — backburner section: search box + grouping by item_group +
  // per-item permanent delete. Falls back to the plain section when empty.
  const backburnerSection = () => {

    if (!backburner.length) {

      return section('⏸', 'Backburner', backburner, 'no backburner items', { key: 'backburner', collapsed: true });

    }

    const collapsed = sectionState.backburner ?? true;

    const groups: Record<string, any[]> = {};

    for (const it of backburner) {

      const g = it.item_group || '(ungrouped)';

      (groups[g] = groups[g] || []).push(it);

    }

    const groupNames = Object.keys(groups).sort((a, b) => a.localeCompare(b));

    const search = `<input type="text" id="backburner-search-${escapeHtml(projectId)}" placeholder="filter backburner…"

      oninput="filterBackburner('${escapeHtml(projectId)}', this.value)"

      style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:3px 8px;margin-bottom:8px;outline:none">`;

    const groupHtml = groupNames.map(g => `

      <div class="bb-group">

        <div style="font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.4px;margin:8px 0 4px">${escapeHtml(g)} <span style="opacity:0.6">(${groups[g].length})</span></div>

        ${groups[g].map(renderItem).join('')}

      </div>`).join('');

    return `<div class="queue-section" data-section="backburner" data-collapsed="${collapsed ? 'true' : 'false'}">

      <div class="queue-section-header" role="button" tabindex="0" aria-expanded="${collapsed ? 'false' : 'true'}" data-section-key="backburner">

        <span class="queue-section-header-label">⏸ Backburner <span class="queue-section-count">(${backburner.length})</span></span>

        <span class="queue-section-chevron" aria-hidden="true">▶</span>

      </div>

      <div class="queue-section-body">

        <div class="queue-section-body-inner">

          ${search}${groupHtml}

        </div>

      </div>

    </div>`;

  };

  const doneFooter = doneAll.length > done.length

    ? `<div style="padding-top:6px">

        <button class="secondary" id="queue-done-more-${projectId}" style="padding:3px 10px;font-size:10px">

          Load more (${done.length}/${doneAll.length})

        </button>

      </div>`

    : '';



  const doneTitle = totalDoneCount
    ? `${totalDoneCount} completed`
    : 'Done';

  return [

    section('⏳', 'Pending', pending, 'no pending sprint items', { key: 'pending' }),

    section('🔄', 'In Progress', inProgress, 'nothing in progress', { key: 'in_progress' }),

    backburnerSection(),

    section('✅', doneTitle, done, 'no completed sprint items', { key: 'done', collapsed: true, footer: doneFooter, count: totalDoneCount }),

    section('✕', 'Failed', failed, 'no failed sprint items', { key: 'failed', collapsed: true }),

  ].join('');

}



// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { _renderPlanBadge, renderSprintProgress, renderQueue }); } catch (e) {}
