with open('meridian/static/dashboard.js', encoding='utf-8') as f:
    content = f.read()

# 1. Reorder tabs: Milestones first, Goals second, Activity last
old = """  const tabs = [
    { id: 'activity', label: '\\U0001f4cb Activity' },
    { id: 'versions', label: '\\U0001f4e6 Milestones' },
    { id: 'goals',    label: '\\U0001f3af Goals' },
  ];"""

# Check what's actually there
idx = content.find("const tabs = [")
print(repr(content[idx:idx+200]))
