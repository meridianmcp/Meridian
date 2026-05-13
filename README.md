# Meridian — Multi-Session Claude Coordinator

A local MCP server that gives multiple Claude Desktop sessions a shared
persistent brain. Goal state, task log, session registry, and auto-generated
context handoff files — so you can run parallel conversations without losing
context or repeating yourself.

## What it solves

- Claude Desktop sessions are isolated. Each tab knows nothing about the others.
- Context fills up and dies. You lose everything and start over.
- Running multiple sessions on the same project means manually syncing state.

Meridian fixes all three. Every session connects to the same local MCP server.
They share goal state, see each other's task logs, and can generate a compressed
handoff file that lets a new session resume with full context in seconds.

## Quick start

```bash
pixi run start
```

Then add Meridian to your Claude Desktop MCP config and start building.

## License

Meridian Source License 1.0 — free for individual personal use.
Commercial use requires a license. See LICENSE for details.
Contact: [your-email@domain.com]
