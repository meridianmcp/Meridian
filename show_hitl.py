with open('meridian/db.py', encoding='utf-8') as f:
    content = f.read()

# Find and read the hitl migration
idx = content.find('async def _migrate_task_log_hitl(')
end = content.find('\nasync def _migrate_task_log_backlog', idx)
print('hitl migration:')
print(content[idx:end])
