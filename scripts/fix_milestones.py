with open('meridian/static/dashboard.js', encoding='utf-8') as f:
    content = f.read()

# Find milestones render — the _rewindSec call for versions_shipped
old = "    v => `<div style=\"padding:3px 0;border-bottom:1px solid var(--border);font-size:10px;white-space:normal;word-break:break-word;line-height:1.5;color:var(--text)\">${escapeHtml(v)}</div>`)"
new = "    v => `<div style=\"padding:5px 0;border-bottom:1px solid var(--border);font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.5;color:var(--text)\">${escapeHtml(v)}</div>`)"

if old in content:
    content = content.replace(old, new, 1)
    with open('meridian/static/dashboard.js', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Fixed milestones full text + bigger font')
else:
    print('Not found — checking pattern')
    idx = content.find('versions_shipped')
    print(repr(content[idx:idx+300]))
