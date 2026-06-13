"""Remove a stale duplicate `meridian` MCP server entry (the one carrying a
hardcoded `Authorization` header) from the local `~/.claude.json`, leaving the
global connector definition intact. One-off maintenance helper used when a
rotated token left a dead Bearer entry behind. Operates only on the local
machine's Claude Code config; contains no credentials itself.
"""
r = open(r'C:\Users\13144\.claude.json', encoding='utf-8').read()

# Find the meridian entry in global mcpServers that has Authorization
# Pattern: "meridian": { ... "Authorization" ... }
idx = r.find('"Authorization"', r.find('Authorization') + 1)
meridian_key_idx = r.rfind('"meridian":', 0, idx)
print("meridian key at:", meridian_key_idx)
print("context:", repr(r[meridian_key_idx:meridian_key_idx+50]))

# Find the full value object
brace_start = r.index('{', meridian_key_idx)
depth = 0
end = brace_start
for i in range(brace_start, len(r)):
    if r[i] == '{': depth += 1
    elif r[i] == '}':
        depth -= 1
        if depth == 0:
            end = i + 1
            break

# Include the key + preceding comma
# Go back from meridian_key_idx to find the comma
before = r[:meridian_key_idx].rstrip()
if before.endswith(','):
    remove_start = len(before) - 1  # include the comma
else:
    remove_start = meridian_key_idx

old = r[remove_start:end]
print("Removing:", repr(old[:100]))

fixed = r[:remove_start] + r[end:]

# Verify
remaining_auths = fixed.count('"Authorization"')
print("Authorization entries remaining:", remaining_auths)
if remaining_auths == 1:  # only Neon
    open(r'C:\Users\13144\.claude.json', 'w', encoding='utf-8').write(fixed)
    print("DONE - written")
else:
    print("Unexpected count, not writing")
