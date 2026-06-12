// dashboard-mcp.js — MCP tool renderer extracted from dashboard.js
// Depends on: dashboard-utils.js (escapeHtml)

function _renderToolEntry(tool) {

  const props = (tool.inputSchema && tool.inputSchema.properties) ? tool.inputSchema.properties : {};

  const required = new Set((tool.inputSchema && tool.inputSchema.required) || []);

  const params = Object.entries(props).map(([name, schema]) => {

    const req = required.has(name) ? 'required' : 'optional';

    const type = schema.type || 'any';

    const desc = schema.description ? escapeHtml(schema.description) : '';

    return `<tr><td style="color:var(--text);padding:2px 10px 2px 0">${escapeHtml(name)}</td><td style="color:var(--muted);padding:2px 10px 2px 0">${type}</td><td style="color:var(--muted);padding:2px 10px 2px 0;font-style:italic">${req}</td><td style="color:var(--muted);padding:2px 0">${desc}</td></tr>`;

  }).join('');

  const signature = Object.keys(props).map(n => required.has(n) ? n : `${n}?`).join(', ');

  return `<div style="margin-bottom:12px"><div style="color:var(--text);font-weight:600">${escapeHtml(tool.name)}(<span style="color:var(--muted)">${escapeHtml(signature)}</span>)</div><div style="color:var(--muted);margin:3px 0 4px 0;font-size:10.5px">${escapeHtml(tool.description || '')}</div>${params ? `<table style="font-size:10px;border-collapse:collapse;width:100%">${params}</table>` : ''}</div>`;

}

