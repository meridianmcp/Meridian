# Security Policy

Thank you for helping keep Meridian and its users safe.

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public issue, pull
request, or discussion for a vulnerability.

Use GitHub's private vulnerability reporting:

<https://github.com/meridianmcp/Meridian/security/advisories/new>

A useful report includes:

- what is affected (file and line, endpoint, or component) and the version or commit
- what an attacker could do, and what they would need first
- the smallest set of steps that shows the problem
- your suggested fix, if you have one

Keep proof-of-concept material minimal and harmless. Show that the problem
exists; do not build something that does damage.

## Supported versions

Only the **latest release** receives security fixes. If you are running an
older version, please update before reporting, or tell us if the issue only
reproduces on the older one.

## Scope

In scope:

- the hosted service at **usemeridian.us**
- the code in this repository (the MCP server, dashboard, tunnel and tray
  client, and the extensions in this repo)

Out of scope:

- third-party services and dependencies themselves (report those upstream; do
  tell us if the way Meridian uses one is the problem)
- social engineering, phishing, or physical attacks
- denial-of-service or volumetric testing
- findings that need an already-compromised device or account

## Testing rules

- **Never test against the production service or against other people's data.**
  Run your own copy of Meridian locally (self-hosting from this repository is
  supported) and test there, with your own synthetic data.
- Do not access, change, or delete data that is not yours. If you come across
  someone else's data by accident, stop, do not keep or share it, and tell us.
- Do not run automated scanners or load tests against usemeridian.us.
- Do not publish details of an issue until it has been fixed or we have agreed a
  date together.

## What to expect

Meridian is a small project. We read every report and will do our best to
acknowledge it and keep you updated, but we cannot promise a response time or a
fix date. There is no bug bounty. With your permission we are happy to credit
you in the fix notes.
