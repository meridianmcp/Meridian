# GitHub Integration

Connect your GitHub repo to Meridian and your AI sessions get live code access — no extra connector, no Copilot subscription, no local install.

## Setup in 60 seconds

1. **Generate a PAT** — go to [github.com/settings/tokens/new](https://github.com/settings/tokens/new), select the `repo` scope (read-only is enough), give it a name like "Meridian", and copy the token.

2. **Open Settings** — in your Meridian dashboard, click the ⚙️ Settings tab for any project.

3. **Paste and connect** — fill in the PAT, your repo in `owner/repo` format, and the default branch. Click **Connect**.

That's it. Meridian validates the token against the GitHub API, encrypts it at rest, and injects five new MCP tools into your planning sessions.

## The 5 GitHub tools

Once connected, every MCP session for your account gets these tools automatically.

### `read_file(path, ref?)`

Read a file from the repo. Returns the decoded UTF-8 content.

```
read_file(path="src/auth.py")
read_file(path="README.md", ref="feature-branch")
```

### `list_files(path?)`

List all files in the repo (recursive). Pass a path prefix to filter.

```
list_files()                  # entire repo
list_files(path="src/")       # only files under src/
```

### `search_code(query)`

Search the codebase using GitHub's code search. Returns up to 20 matches with file paths and links.

```
search_code(query="def authenticate")
search_code(query="TODO rate limit")
```

### `git_log(limit?)`

Return the most recent commits. Default 10, max 50.

```
git_log()
git_log(limit=25)
```

### `get_commit(sha)`

Return full details for a specific commit: message, author, date, and list of changed files.

```
get_commit(sha="a1b2c3d")
```

## Security

- **Encrypted at rest** — the PAT is encrypted with AES-256 (Fernet) before storage. The raw token is never logged or returned by any API.
- **Read-only by default** — the five tools only call read endpoints on the GitHub API. No writes, no webhooks, no repo modifications.
- **Per-account** — the connection belongs to your Meridian account, not to a single project. All your projects share the same GitHub connection.
- **Revokable any time** — click **Disconnect** in Settings or revoke the PAT on GitHub to immediately remove access.

## Troubleshooting

| Error | Fix |
|-------|-----|
| "GitHub PAT is invalid or expired" | Regenerate the PAT on GitHub and reconnect. |
| `read_file` returns 404 | Check the path (case-sensitive) and the `ref` (branch/tag/SHA). |
| `search_code` returns 0 results | GitHub code search has a short indexing delay for new repos. Wait a minute and retry. |
| `list_files` is slow on large repos | Normal — GitHub returns the full recursive tree in one call. Results are cached per session. |

## Disconnecting

To remove the GitHub connection:

- **Dashboard** — Settings tab → GitHub card → **Disconnect**.
- **API** — `DELETE /projects/{id}/github/disconnect` with your bearer token.

Disconnecting immediately removes the PAT from storage and stops the GitHub tools from appearing in new MCP sessions.
