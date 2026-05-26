# Meridian Playtester Guide

## Access

- **Site**: https://usemeridian.us
- **Password**: meridian2026
- **Demo** (no login required): https://usemeridian.us/demo
- **Docs**: https://docs.usemeridian.us (password: meridian2026)

---

## What to Test

### 1. Demo — 5 min

Go to https://usemeridian.us and enter the password, then navigate to `/demo` or click "Try demo" on the dashboard.

You should see a **backend-api-v2** project in the sidebar. Click it. Verify:
- Sprint board shows tasks with statuses
- Team tab shows active sessions
- Goal tab shows north star + sprint goal

No login required for demo. If the first load is slow (~5 sec), that's Neon waking up — normal.

---

### 2. Sign Up — 5 min

Go to https://usemeridian.us/auth/login. Sign in with **Google** or **GitHub**.

After OAuth, you should land on `/dashboard` (not a 404 or error page).

Create a project:
1. Click the **+** button in the sidebar
2. Give it a name (e.g. "test-project")
3. It should appear in the sidebar immediately

---

### 3. MCP Setup — 10 min

1. Open your dashboard and go to the **Settings** tab
2. Find **MCP Configuration** — select your client from the dropdown (Claude Desktop / Claude Code / Cursor)
3. Click **Copy** to copy the JSON config
4. Paste it into your client's MCP config file (see below for locations)
5. Restart your client

**Config file locations:**
- Claude Desktop (Mac): `~/Library/Application Support/Claude/claude_desktop_config.json`
- Claude Desktop (Windows): `%APPDATA%\Claude\claude_desktop_config.json`
- Claude Code: `~/.claude/claude_code_config.json`

You'll need to generate an API key first — in Settings, click **Generate API key**, copy it immediately (shown once).

---

### 4. Run a Session — 10 min

In Claude Code or Claude Desktop (with Meridian MCP connected), paste this into your chat:

```
Use the start_session tool with:
  project_id = "<your project ID from the dashboard URL>"
  session_name = "test"
  human_id = "you"
```

You should get back a goal state + recent tasks (empty for a new project — that's fine).

Then:
```
Use the log_task tool with:
  description = "first test task"
  status = "done"
```

Check the **Team** tab in the dashboard — your task should appear there.

---

### 5. Report Back

After testing, please share:
- Did anything break or confuse you?
- How long did MCP setup take end-to-end?
- Did the demo make the product clear without explanation?
- Any wording or UI that felt off or unclear?
- What's missing that you'd need before using this on a real project?

---

## Known Limitations (Beta)

- **Site password required**: `meridian2026` — removed on public launch day
- **First demo load may be slow**: Neon database auto-suspends after inactivity; first request wakes it up (~5 sec)
- **Overage billing UI** exists in Settings but consumption tracking starts from account creation
- **docs.usemeridian.us**: Full documentation site — may have a few missing pages still being written
- **Free plan**: 10 CU-hours / 0.1 GB Neon storage — plenty for testing

---

## Quick Reference

| Action | Where |
|--------|--------|
| Sign in | https://usemeridian.us/auth/login |
| Dashboard | https://usemeridian.us/dashboard |
| Demo | https://usemeridian.us/demo |
| Docs | https://docs.usemeridian.us |
| Your project ID | Dashboard URL: `/dashboard?project=<ID>` or Settings tab |
| MCP config JSON | Settings tab → MCP Configuration |
| API key | Settings tab → Generate API key |
