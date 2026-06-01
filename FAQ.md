# Meridian FAQ

Honest answers to the questions HN will ask.

---

**Q: How is this different from just using a long CLAUDE.md?**

CLAUDE.md is a static file you maintain by hand. It has no structured task log, no multi-session coordination, no atomic claiming, no handoff compression. When Claude fills its context, CLAUDE.md doesn't help you resume — you still re-explain everything. Meridian tracks *what was done* dynamically, across sessions, without manual maintenance. CLAUDE.md complements Meridian (we generate one for you); it doesn't replace it.

---

**Q: Why not just use a shared Notion / Linear / GitHub Issues?**

Those tools aren't MCP-native. Your AI session can't call `claim_task()` on a GitHub issue and have the server atomically mark it in-progress so no other session touches it. Meridian is built for AI-to-AI coordination. It's the difference between a wiki and a mutex.

---

**Q: This is just a wrapper around a database. Why is that a product?**

The same could be said of most developer tools. The value is: (1) the MCP tools your AI sessions can actually call, (2) the handoff compression algorithm that fits 3 days of work into a 400-token context block, (3) the session coordination primitives (claiming, HITL, stale-release). A SQLite file doesn't give you any of that.

---

**Q: What happens when context fills up? You still lose work.**

No — that's what `checkpoint()` is for. Call it (or have the Stop hook do it automatically) before context fills. It runs `auto_capture` (buckets completed tasks into a note), generates a delta handoff, and returns a `/goal` string the next session can paste to resume. The new session reads the handoff and picks up where the last one left off. You lose at most the last few minutes of unreported work.

---

**Q: Why not just use Anthropic's Managed Agents / Projects feature?**

Managed Agents is cloud-only, Claude-only, and closed source. It has no equivalent to `pin_decision`, `claim_task`, `request_hitl`, or the human-in-the-loop queue. And it doesn't work with Cursor, Codex, or anything that isn't Claude. Meridian is model-agnostic and self-hostable.

---

**Q: 539 tests but it's alpha. What's actually broken?**

The hosted demo DB is still being wired to a persistent Neon project (currently falls back to in-memory SQLite on cold start). The pricing page exists but billing isn't enforced yet. Binary releases need smoke-testing on clean machines. The core MCP server and dashboard are solid — I use them daily.

---

**Q: Is the MSL-1.0 license actually open source?**

It's source-available, not OSI-certified open source. You can self-host for free, read and modify the code, and use it for personal/internal projects. Commercial SaaS use requires a license from me. This is the same model as SSPL, BSL, Elastic, and others. The self-hosted tier has no functional restrictions.

---

**Q: How does multi-session coordination actually prevent conflicts?**

`claim_task()` is an atomic DB write — only one session can claim a given task. The server rejects a second claim attempt and returns a conflict error. For sprint items, there's also a `depends_on` field: a session can't claim item B until item A is done. Parallel sessions can safely work the same codebase as long as sprint items are designed to touch distinct files (current limitation: no file-level conflict detection).

---

**Q: What's the actual install experience? "pixi" is unknown to me.**

pixi is a cross-platform package manager (like conda but fast, based on rattler). One `curl | bash` installs it in ~10 seconds. Alternatively, download the binary from the releases page — no Python, no pixi, no dependencies. Double-click runs the server. Or use the hosted tier and skip all of it.

---

**Q: Will you keep working on this? Is it going to be abandoned?**

I use it every day for my own development. The dogfood dependency makes abandonment painful. The hosted tier generates revenue (once billing goes live). I'm not building this to flip it; I'm building it because I need it.
