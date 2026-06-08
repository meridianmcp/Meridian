# Team self-hosting

The hosted tier at [usemeridian.us](https://usemeridian.us) handles a real
team for you — auth, Neon-backed databases, invites by email, billing.
This page is for everyone running Meridian themselves and wondering how
to put a small team on it.

## The honor-system model

The self-hosted Meridian process is single-tenant. There is no
`tenants` table in your local DB, no per-user database, no per-user
session enforcement. Everyone who can reach the dashboard and present
a valid Bearer token can read and write everything.

That is **fine** for a small trusted team and it's intentionally simple,
but it's not multi-tenant security. If you need hard isolation between
co-workers, use the hosted tier.

### `MERIDIAN_MAX_MEMBERS`

The single safety knob is `MERIDIAN_MAX_MEMBERS`: if you've handed out
bearer tokens to more than this number of distinct humans, the server
refuses to mint new tokens. It's not a license check — nothing breaks
for existing users — it's just there to keep one person from
accidentally turning their personal install into a public service.

```bash
# .env or systemd unit
MERIDIAN_MAX_MEMBERS=5
```

## Letting teammates reach your server

The two paths that work well for small teams:

### Tailscale (recommended)

The cleanest option for a private team. Install Tailscale on the
machine running Meridian, install Tailscale on each teammate's
machine, point them at the Tailscale IP / MagicDNS name of the
Meridian host. No public exposure, no certificate trouble.

```bash
# On the Meridian host
sudo tailscale up
# Note the 100.x.y.z IP printed by `tailscale ip -4`

# On a teammate's machine, after they join your tailnet:
MERIDIAN_URL=http://meridian-host:7878
```

Bonus: the Meridian dashboard, MCP endpoint, and SSE stream all
work over Tailscale without any extra configuration.

### cloudflared / Cloudflare Tunnel

If you need teammates who can't join your tailnet (a contractor on
their own laptop, a phone), point a Cloudflare tunnel at the
Meridian host:

```bash
cloudflared tunnel --url http://localhost:7878
```

The tunnel hands you a public `*.trycloudflare.com` URL. Pair it
with Cloudflare Access if you want auth at the edge.

### What about ngrok / a raw public port?

Don't. The self-hosted server has no auth in front of the dashboard
beyond optional `SITE_PASSWORD` and the demo gate; exposing it
directly to the public internet is asking for trouble. Either of
the two paths above is safer with the same effort.

## Invite emails

If you want Meridian to email invitees instead of you copy-pasting
the accept link into Slack, set the Resend API key:

```bash
RESEND_API_KEY=re_xxx
```

Without `RESEND_API_KEY` the `/workspace/invite` endpoint still
generates a working accept URL; it just doesn't send mail. You can
share the URL however suits your team.

## Recommended setup

A small team self-hosting Meridian usually wants:

1. Postgres connection (not SQLite) so concurrent writes from
   several humans don't fight for the file lock.
2. Tailscale for reachability.
3. `MERIDIAN_MAX_MEMBERS` set to your headcount + 1.
4. `BACKUP_S3_BUCKET` or some other off-host backup of the
   Postgres data — the self-hosted server doesn't take backups
   for you.

If any of that gets in the way of getting work done, switch to
the hosted tier and don't look back; that's the point.
