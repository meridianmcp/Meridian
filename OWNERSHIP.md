# OWNERSHIP

This document records the IP ownership of the Meridian project in plain English.
It is not a legal document but is a clear record of intent, signed and timestamped by the parties involved.

## Pre-existing IP (solely owned by Adam Camerer)

The following components were designed and built by Adam Camerer prior to any
external collaboration, as evidenced by git commit history in this repository
dated from 2026-05-13 onwards:

- The core Meridian MCP server architecture (meridian/server.py)
- The SQLite coordination layer and schema (meridian/db.py)
- The HITL queue design and implementation
- The goal state versioning system
- The session registry and TTL system
- The FastAPI dashboard (meridian/dashboard.py)
- The handoff generation system (meridian/handoff.py)
- The enqueue worker system (meridian/enqueue.py)
- All Pydantic models (meridian/models.py)
- The test suite (tests/)
- The pixi.toml environment configuration
- LICENSE (MSL-1.0), ROADMAP.md, DEVLOG.md, AGENTS.md

## Joint contributions

Contributions made after the date a new contributor's first commit appear in the
git history are jointly owned by Adam Camerer and that contributor under the
following terms:

- Any sale, license, or acquisition of Meridian IP requires written consent of
  all parties who have contributed to the component being transferred.
- Revenue from Meridian as a product is split proportionally based on equity
  agreements made separately in writing between contributors.
- A contributor's joint ownership applies only to the specific components they
  materially contributed to, as evidenced by git blame and commit history.

## Enforcement

The git commit history of this repository, hosted at its canonical remote, is
the authoritative tamper-evident record of who built what and when.
Meridian's own task log (accessible via the MCP server) provides a secondary
timestamped record of session-level contributions.

---

Signed:

Adam Camerer — 2026-05-15
