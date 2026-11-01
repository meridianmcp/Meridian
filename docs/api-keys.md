# API keys and tokens: which key is which

Meridian uses long secret strings, usually starting with `sk_meridian_`, to let a program act on your account. This page explains each kind, where it comes from, and what happens when you make a new one or delete an old one.

**Treat every key like a password.** Do not paste a key into a chat, an issue, a log, a screenshot, or a URL. If a key leaks, revoke it and generate a new one.

This page matches the code on `origin/dev` as of 2026-10-31. Some behaviour described here, marked *known issue*, is being fixed.

## The keys you will see

Each key has a **label**. The dashboard lists every key with its label and when it was made. The label tells you what the key is for.

| Label | Where you make it | Used by | Lifetime | What a new one does to the old one |
|---|---|---|---|---|
| `mcp-config` | Dashboard settings, MCP token "Generate" | An MCP client config, such as Claude Code or Cursor | Until you revoke it | **Replaces** the previous `mcp-config` key. Any client still using the old key stops working. |
| `hooks-config` | Dashboard settings, hooks "Generate" | Local Claude Code hooks | Until you revoke it | **Replaces** the previous `hooks-config` key. |
| `readonly` | Dashboard settings, "Generate read-only key" | Clients that should only read | Until you revoke it | **Replaces** the previous `readonly` key. See the read-only note below. |
| `install` | The install page | The installer on one machine | 10 minutes | Older `install` keys are removed when a new one is made. |
| `hooks-installer` | The hooks installer scripts | Hook installs | Until you revoke it | **Replaces** the previous `hooks-installer` key. |
| `tunnel-cli:<computer name>` | Tunnel sign-in from the tunnel client | The tunnel on one computer | Until you revoke it | **One key per computer name.** Signing in again from the same name replaces it. Two computers with the same name share one slot, and each sign-in kicks the other one out. |
| `oauth` | Signing in through an OAuth connector | The connector, such as a Claude or ChatGPT connector | About 90 days in the client | *Known issue:* the server removes `oauth` keys older than 24 hours that the sign-in no longer needs. A key can stop working after a server restart if it is more than a day old. If that happens, sign in again. |
| `device-code` | Device sign-in approval | Clients that sign in with a device code | Until you revoke it | Does not replace anything. Each approval adds a key. |
| `welcome` | Created by billing when an account is set up | Your first client | Until you revoke it | Does not replace anything. Each billing event adds one. |

Keys made through the API with a label not listed here work the same way as the rows above: making a new key with the same label replaces the old one.

## Which key a program uses

**Command-line tools and the tunnel** take the key from the first of these that is set:

1. A `--token` argument
2. The `MERIDIAN_TOKEN` environment variable
3. The `MERIDIAN_API_KEY` environment variable
4. The `BEARER_TOKEN` environment variable
5. A saved token in `~/.meridian/config.json`
6. A browser sign-in, if nothing else is set

An environment variable outranks a saved token that still works. If you rotated a key and things still fail, check these variables first, in your shell and in your user environment.

**Meridian Desktop** keeps its hosted key in the operating system keychain, under the service `meridian-desktop`. You paste it into Settings and press **Save token**. Nothing is written to `settings.json`.

## Rotating and revoking

- **To rotate:** generate the new key, then update every client that uses the old one. A client keeps working with the old key until that key is replaced or revoked.
- **To revoke:** press **Revoke** on the key's row. Any client using that key stops working right away. *Known issue:* an `oauth` key can keep working in the process that issued it until that process restarts.
- **To check which keys exist:** open the dashboard key list and read the labels and creation times.

## Errors you will see

- **`invalid API token` (HTTP 401):** the key is revoked, replaced, expired, or mistyped. Check the key list. If it is gone, generate a new one.
- **`invalid or mismatched tenant credential`:** the key belongs to a different account, or the client is sending a key from another account.
- **Meridian Desktop: "Hosted Meridian did not accept the saved token (HTTP 401)":** paste a current token into Settings and press **Save token**.
- **Meridian Desktop: "Hosted Meridian could not be reached":** the request got no answer. Check the hosted URL and your network. The reason is written to `desktop.log` on the same line as the words `hosted request failed`. Find it at `%LOCALAPPDATA%\meridian-desktop\logs\desktop.log` on Windows.

## Read-only keys

A read-only key is meant to read, not change anything. Meridian checks that on the MCP endpoint. We are still verifying whether the REST endpoints that change data also check it. Until that is confirmed, give a read-only key only to a client you would trust with a full key.

## Safety

- Do not share keys, and do not put them in a URL.
- Use a separate key for each client, so you can revoke one without breaking the others.
- Revoke a key you no longer use.
