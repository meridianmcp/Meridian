// dashboard-mcp.js — MCP tool renderer extracted from dashboard.js
// Depends on: dashboard-utils.js (escapeHtml)

export function _renderToolEntry(tool: any): string {

  const props = (tool.inputSchema && tool.inputSchema.properties) ? tool.inputSchema.properties : {};

  const required = new Set((tool.inputSchema && tool.inputSchema.required) || []);

  const params = Object.entries(props).map(([name, schema]: [string, any]) => {

    const req = required.has(name) ? 'required' : 'optional';

    const type = schema.type || 'any';

    const desc = schema.description ? escapeHtml(schema.description) : '';

    return `<tr><td style="color:var(--text);padding:2px 10px 2px 0">${escapeHtml(name)}</td><td style="color:var(--muted);padding:2px 10px 2px 0">${type}</td><td style="color:var(--muted);padding:2px 10px 2px 0;font-style:italic">${req}</td><td style="color:var(--muted);padding:2px 0">${desc}</td></tr>`;

  }).join('');

  const signature = Object.keys(props).map(n => required.has(n) ? n : `${n}?`).join(', ');

  return `<div class="tool-entry" data-search="${escapeHtml((tool.name || '') + ' ' + (tool.description || ''))}" style="margin-bottom:14px"><div style="color:var(--text);font-weight:600;font-size:13px">${escapeHtml(tool.name)}(<span style="color:var(--muted);font-weight:400">${escapeHtml(signature)}</span>)</div><div style="color:var(--muted);margin:3px 0 5px 0;font-size:12px;line-height:1.45">${escapeHtml(tool.description || '')}</div>${params ? `<table style="font-size:11px;border-collapse:collapse;width:100%">${params}</table>` : ''}</div>`;

}



// 70ac52e4 — Group the flat MCP tool list into ordered category buckets.
// `categories` maps a category key -> ordered list of tool names. Tools are
// emitted in category order (only categories that actually have tools appear),
// and anything not claimed by a category falls into a trailing "other" bucket
// so no tool is ever dropped. Pure function — no DOM — so it is unit-testable.

export interface ToolGroup {

  key: string;

  tools: any[];

}

export function _groupToolsByCategory(

  tools: any[],

  categories: Record<string, string[]>,

): ToolGroup[] {

  const byName: Record<string, any> = {};

  (tools || []).forEach((t: any) => { if (t && t.name) byName[t.name] = t; });

  const groups: ToolGroup[] = [];

  const claimed = new Set<string>();

  for (const [key, names] of Object.entries(categories || {})) {

    const catTools = (names || []).map(n => byName[n]).filter(Boolean);

    if (!catTools.length) continue;

    catTools.forEach(t => claimed.add(t.name));

    groups.push({ key, tools: catTools });

  }

  const rest = (tools || []).filter((t: any) => t && t.name && !claimed.has(t.name));

  if (rest.length) groups.push({ key: 'other', tools: rest });

  return groups;

}


// 70ac52e4 — Render the grouped tool list as collapsible <details> sections
// (matches the existing dashboard <details open> collapsible pattern). Sections
// default to open so the tab is fully visible on load and the tool search box
// can still match every entry; the user collapses categories they don't need.

export function _renderToolSections(

  tools: any[],

  categories: Record<string, string[]>,

  labels: Record<string, string>,

): string {

  const groups = _groupToolsByCategory(tools, categories);

  return groups.map(({ key, tools: catTools }) => {

    const label = (labels && labels[key]) || 'Other';

    const summary = `<summary style="cursor:pointer;list-style:none;color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border);user-select:none">${escapeHtml(label)} <span style="color:var(--muted)">(${catTools.length})</span></summary>`;

    const body = catTools.map(_renderToolEntry).join('');

    return `<details open class="tool-category" data-category="${escapeHtml(key)}" style="margin-bottom:18px">${summary}${body}</details>`;

  }).join('');

}


// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { _renderToolEntry, _groupToolsByCategory, _renderToolSections }); } catch (e) {}
