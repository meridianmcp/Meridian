# Hosted Tier — usemeridian.us

The hosted tier gives you a managed Meridian instance with zero local setup.
Sign in with Google or GitHub, get an isolated Neon Postgres database, and connect
your AI clients over HTTPS in minutes.

**[usemeridian.us →](https://usemeridian.us)**

---

## Pricing

**Try free for 30 days — no card required.**

| | **Free** | **Standard** | **Pro** |
|--|--|--|--|
| **Price** | Free · 30 days | $20 / mo | $49 / mo |
| **Card required** | No | Yes | Yes |
| **Storage** | Shared pool | 1 GB | 10 GB |
| **Compute** | 0.5 CU shared | 50 CU-hrs / mo | 200 CU-hrs / mo |
| **Projects** | 1 | Unlimited | Unlimited |
| **Concurrent sessions** | 1 | Unlimited | Unlimited |
| **Team members** | — | 25 | 50 |
| **BYODB** | ✓ | ✓ | ✓ |
| **OAuth + magic link** | ✓ | ✓ | ✓ |
| **Support** | — | Email | Priority |

Overage: $0.16 / CU-hr · $0.50 / GB-month · billed via Stripe.
Self-hosted is always free — no limits, no expiry, [see self-hosting guide](self-hosting.md).

---

## Sign Up Flow

1. **Visit [usemeridian.us](https://usemeridian.us)** and click "Get Started"
2. **Sign in** with Google or GitHub — no passwords, no card for free tier
3. **Check your email** — welcome email contains your bearer token and MCP config snippet
4. **Upgrade anytime** via Settings → Billing when you're ready for dedicated compute

---

## Receiving Your Welcome Email

After signing up, you'll receive an email from `noreply@usemeridian.us` with:

- Your **bearer token** (`sk_meridian_...`) — keep this private
- **Claude Code config** — ready-to-paste `.mcp.json` snippet
- **Claude Desktop config** — ready-to-paste config snippet
- **Dashboard link** — `https://usemeridian.us/dashboard`

---

## Configuring Claude Code

Paste this into your project's `.mcp.json` (or `~/.claude/mcp.json` for global):

```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": {
        "BEARER_TOKEN": "sk_meridian_your_token_here"
      }
    }
  }
}
```

!!! tip "No local install required"
    With the hosted tier, you don't need to clone the repo or install anything locally.
    `npx mcp-remote` handles the connection transparently.

---

## Configuring Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": {
        "BEARER_TOKEN": "sk_meridian_your_token_here"
      }
    }
  }
}
```

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

---

## Configuring Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": {
        "BEARER_TOKEN": "sk_meridian_your_token_here"
      }
    }
  }
}
```

---

## Dashboard Walkthrough

After signing in at `https://usemeridian.us/auth/login`:

### Projects tab
- **Create projects** — one per codebase or initiative
- **Sprint board** — see all sprint items, mark done/fail/push
- **Goal editor** — edit the shared goal your sessions align to

### Live tab
- **Active sessions** — all connected Claude sessions with last-seen timestamp
- **Task queue** — real-time feed of what every session is doing

### Rewind tab
- **Goal history** — every version of the goal, newest first
- **Activity timeline** — every task, session event, and goal change
- **Charts** — tasks/day per team member, sprint velocity

### Files tab
- **AGENTS.md / ROADMAP.md / DEVLOG.md** — project markdown files, editable in browser

---

## Managing Projects

**Create a project:**
In the dashboard, click ➕ **New Project**, enter a name.

**Rename a project:**
Click the kebab (⋮) menu on a project card → Rename.

**Delete a project:**
Click the kebab menu → Delete. Deletes all sessions, tasks, and goals.

**Switch projects:**
Use the project selector in the sidebar.

---

## Account Settings

Your account page is at `https://usemeridian.us/dashboard` → ⚙️ Settings.

From there you can:
- View your current plan
- Copy your bearer token
- Manage billing (Stripe portal)
- Delete your account

---

## BYODB Setup

Prefer to manage your own Neon database? You can use BYODB:

1. Create a [Neon account](https://neon.tech) and project
2. Copy your connection string: `postgresql://...@....neon.tech/neondb?sslmode=require`
3. At signup, choose "Bring your own database" and paste the URL

Meridian will initialize its schema in your database on first run. Your data never touches our Neon account.

---

## FAQ

**Q: Can I switch from managed DB to BYODB after signup?**
A: Email hello@usemeridian.us — we'll migrate you manually.

**Q: What happens to my data if I cancel?**
A: Your Neon database is deleted 28 days after payment failure. You'll receive warning emails on day 3-7 and day 14.

**Q: Can multiple team members share one account?**
A: Yes — unlimited members on all plans. Each member signs in with their own Google/GitHub and connects to the same workspace.

**Q: Is there a rate limit on the MCP endpoint?**
A: 100 requests per minute per bearer token. Sufficient for all normal usage patterns.

**Q: I lost my bearer token. How do I get a new one?**
A: Email hello@usemeridian.us — we'll regenerate it.

**Q: Does the hosted tier work with Cursor / Windsurf?**
A: Yes — any MCP-compatible client works with the remote `/mcp` endpoint via `mcp-remote`.

**Q: Where is my data stored?**
A: Each workspace gets an isolated Neon Postgres database in `aws-us-east-2` (US East). No data mixing between customers.
