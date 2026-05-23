# Configuration

All Meridian configuration is via environment variables. Set them in a `.env` file
in the project root, via shell export, or via your deployment platform's secret store.

---

## Database

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `MERIDIAN_DB_URL` | Postgres connection string. When set, Meridian uses Postgres instead of SQLite. Example: `postgresql://user:pass@host/dbname` | — | No (SQLite if unset) |
| `MERIDIAN_DB` | Path to the SQLite database file. Ignored when `MERIDIAN_DB_URL` is set. | `data/meridian.db` | No |
| `MERIDIAN_DEMO_DB_URL` | Postgres URL for the isolated demo database. When set, `/demo` uses this DB (wiped and reseeded on every restart). | — | No |
| `MERIDIAN_DATA_DIR` | Directory for data files (SQLite DB, handoff files). | `data/` | No |

---

## Authentication

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `GOOGLE_CLIENT_ID` | Google OAuth app client ID. Get from [console.cloud.google.com](https://console.cloud.google.com). | — | For hosted Google login |
| `GOOGLE_CLIENT_SECRET` | Google OAuth app client secret. | — | For hosted Google login |
| `GITHUB_CLIENT_ID` | GitHub OAuth app client ID. Get from [github.com/settings/apps](https://github.com/settings/apps). | — | For hosted GitHub login |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth app client secret. | — | For hosted GitHub login |
| `SESSION_SECRET` | Secret key for signing session cookies (itsdangerous). Use a long random string. | `dev-secret-change-me` | **Yes for production** |
| `MERIDIAN_SESSION_SECRET` | Alias for `SESSION_SECRET`. Either name works. | — | — |

---

## Payments (Stripe)

!!! warning
    Never switch `STRIPE_SECRET_KEY` from test to live without thorough testing.
    Keep `STRIPE_SECRET_KEY=sk_test_...` during development.

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_...` or `sk_live_...`). | — | For billing |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret (`whsec_...`). From the Stripe dashboard → Webhooks. | — | For billing |
| `STRIPE_PAYMENT_LINK` | URL of your Stripe Payment Link for the Standard plan. Shown as the "Get Started" button on the landing page. | `/auth/login` | No |

---

## Email (Resend)

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `RESEND_API_KEY` | Resend API key for sending transactional email. If unset, email sending is silently skipped (dev mode). | — | For production email |
| `MERIDIAN_FROM_EMAIL` | Sender address for Resend emails. | `Meridian <noreply@usemeridian.us>` | No |

---

## Neon Provisioning

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `NEON_API_KEY` | Neon API key for the standard-tier account (`hello@usemeridian.us`). Used to provision per-customer Neon databases. | — | For hosted standard provisioning |
| `NEON_API_KEY_PRO` | Neon API key for the pro-tier account (`pro@usemeridian.us`). Used when provisioning pro-plan customers. Not yet required. | — | Future — pro tier only |
| `MAX_CUSTOMERS_PER_PROJECT` | Maximum customer databases per Neon pool project. | `8` | No |
| `MAX_PROJECTS_STANDARD` | Hard cap on pool projects for the standard account. Provisioning blocks at this limit. | `90` | No |
| `MAX_PROJECTS_PRO` | Hard cap on pool projects for the pro account. | `95` | No |
| `ALERT_THRESHOLD_STANDARD` | Send capacity alert email when standard pool project count exceeds this. | `85` | No |
| `ALERT_THRESHOLD_PRO` | Send capacity alert email when pro pool project count exceeds this. | `90` | No |

---

## App / General

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `APP_URL` | Public base URL of the deployment. Used in OAuth callbacks, emails, and MCP endpoint URLs. Example: `https://usemeridian.us` | `http://localhost:7878` | **Yes for production** |
| `MERIDIAN_BASE_URL` | Alias for `APP_URL`. Either name works. | — | — |
| `ADMIN_EMAIL` | Email address for capacity alerts and admin dashboard access. | — | For admin features |
| `SITE_PASSWORD` | When set, all routes (except `/health`, `/demo`, `/__gate__`) require entering this password in a gate page. Used for preview deployments. | — | No |
| `MERIDIAN_DEMO_DB_URL` | (repeated from Database section) Enables the `/demo` route with isolated seed data. | — | No |
| `MERIDIAN_AFTER_LOGIN_URL` | Where to redirect after successful OAuth login. | `/dashboard` | No |
| `MERIDIAN_PORT` | HTTP port for the dashboard/API server. | `7878` | No |
| `MERIDIAN_HOST` | Host to bind the server to. | `127.0.0.1` | No |
| `MERIDIAN_HUMAN_ID` | Default human identifier for task attribution. Falls back to `$USER` / `$USERNAME` / hostname. | — | No |
| `MERIDIAN_AUTO_SUMMARY_INTERVAL` | Seconds between auto-summary cycles (background task). | `600` | No |

---

## Example .env file

```bash
# Database — Postgres (production)
MERIDIAN_DB_URL=postgresql://neondb_owner:...@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require

# Demo database — isolated Neon project
MERIDIAN_DEMO_DB_URL=postgresql://neondb_owner:...@ep-yyy.us-east-2.aws.neon.tech/neondb?sslmode=require

# Auth
GOOGLE_CLIENT_ID=123456789.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GITHUB_CLIENT_ID=Ov23li...
GITHUB_CLIENT_SECRET=...
SESSION_SECRET=replace-with-a-long-random-string

# Payments
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PAYMENT_LINK=https://buy.stripe.com/...

# Email
RESEND_API_KEY=re_...

# Neon provisioning
NEON_API_KEY=...

# App
APP_URL=https://usemeridian.us
ADMIN_EMAIL=admin@example.com
```

!!! danger
    Never commit `.env` to git. Add it to `.gitignore`.
    Meridian's `.gitignore` already excludes `secrets.env` and `.env`.
