path = 'pixi.toml'
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

# Remove any leftover mcp-text-editor dep line
c = c.replace('\nmcp-text-editor = ">=1.2"', '')

# Append new tasks at end
addition = '''
install-companions = "npm install -g repomix && echo Done"
mcp = "python -m meridian --mcp"
repomix-mcp = "npx repomix --mcp"
text-editor-mcp = "uvx mcp-text-editor"'''

if 'install-companions' not in c:
    # Insert before end of file
    c = c.rstrip() + '\n' + addition + '\n'

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

# verify
bad = 'mcp-text-editor = ">=1.2"' in c
print(f"Bad dep still present: {bad}")
print(f"install-companions present: {'install-companions' in c}")
print(f"text-editor-mcp present: {'text-editor-mcp' in c}")
