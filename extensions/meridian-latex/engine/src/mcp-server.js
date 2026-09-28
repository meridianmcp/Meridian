#!/usr/bin/env node
// MCP (Model Context Protocol) server exposing this engine's outline /
// claim-coordination / provenance / citation-lookup capabilities directly to
// an AI agent session (Claude Code, etc.), over stdio.
//
// This is a THIRD way to reach the engine, alongside the two that already
// exist:
//   1. The CLI (cli.js): outline/serve/login/status/logout subcommands.
//   2. The local HTTP server (server.js, port 8471): the Chrome extension's
//      popup talks to it via fetch() for the human-in-the-browser flow.
// Both of those, and this file, wrap the SAME underlying functions exported
// from index.js -- this file adds no new logic, no HTTP hop, no
// re-implementation. It imports directly from ./index.js and calls straight
// through, in-process, exactly like server.js's own handlers do.
//
// Each tool below mirrors the equivalent HTTP endpoint's request/response
// shape (see server.js's own header comment for the canonical list) so a
// caller already familiar with the HTTP API sees the identical semantics
// here, just addressed as an MCP tool instead of a POST body.
//
// ============================================================================
// CRITICAL SAFETY SCOPING -- read before adding a tool here
// ============================================================================
// This server deliberately does NOT expose overleaf-ot-client.js's
// connectToProject()/OverleafProjectSession -- the direct WebSocket write
// path into a live Overleaf document -- as an MCP tool, and nothing here can
// dispatch a live edit into a real, open Overleaf document.
//
// Actually writing into a live Overleaf doc requires either:
//   (a) a live browser tab running the Chrome extension's own CM6-dispatch
//       path (extension/injected.js) -- unreachable from a pure Node MCP
//       server process, full stop; or
//   (b) a saved human Overleaf login cookie (overleaf-login.js) driving
//       overleaf-ot-client.js's OT/WebSocket session directly.
//
// This project's hard rule is that an automated/agent process must NEVER
// handle that cookie or perform a live write on a human's behalf without an
// explicit, separate confirmation gate -- the same reasoning cli.js's own
// `login` command documents ("human-only -- never run this from an agent
// session"). An MCP tool call is exactly the kind of automated invocation
// that rule exists to stop: an agent session could call an MCP tool
// autonomously, with no human clicking anything, so putting a live-write
// capability behind one would silently defeat the human-in-the-loop gate
// the Chrome-extension-only path currently guarantees.
//
// The tools below are read/coordination-only: they parse .tex text/files,
// and read/write the LOCAL claim/provenance SQLite state (the exact same
// state store.js/claims.js/provenance.js already back the Chrome extension's
// HTTP server with -- see the shared-state note on openStore() below).
// Nothing here can touch a live Overleaf document.
//
// If a future contributor is tempted to add a tool that calls
// connectToProject()/OverleafProjectSession, or otherwise dispatches a write
// into a live Overleaf tab: don't, without first re-deriving why this was
// left out and building the confirmation gate this file deliberately does
// not have.
// ============================================================================

import { pathToFileURL } from "node:url";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import {
  outlineText,
  outlineFile,
  openStore,
  getProject,
  upsertProject,
  matchOutlines,
  claimNode,
  leaseWholeDocument,
  releaseClaims,
  getLiveClaims,
  recordEdit,
  listProvenance,
  markSynced,
  lookupCitationKey,
} from "./index.js";

function textResult(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

function errorResult(message) {
  return { content: [{ type: "text", text: JSON.stringify({ error: message }) }], isError: true };
}

const TOOLS = [
  {
    name: "outline_tex",
    description:
      "Parse raw .tex source text into its structural outline (headings, citations, tables, figures, equations). " +
      "Mirrors POST /outline exactly, including its auto-register-project behavior: when project_id is given, " +
      "the project's previous outline (if any) is diffed against the new one and this project_id is (re-)registered " +
      "in the local store, same as a real popup /outline call would do.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Raw .tex source text to parse." },
        project_id: {
          type: "string",
          description:
            "Optional project id. When given, diffs against the project's previously stored outline (matched/added/removed) " +
            "and upserts this outline as the new stored one -- there is no separate registration step, showing up here IS registration.",
        },
      },
      required: ["text"],
    },
  },
  {
    name: "outline_tex_file",
    description:
      "Parse a LOCAL .tex file on disk into its structural outline, via outlineFile(). No server or browser tab is " +
      "involved at all -- this is the one thing this MCP surface can do that the Chrome-extension-only path never " +
      "could: parse a paper's .tex source directly from a checked-out repo.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string", description: "Filesystem path to a .tex file." },
      },
      required: ["path"],
    },
  },
  {
    name: "claim_node",
    description:
      "Claim a single addressable node (section, table, figure, equation, citation, ...) for exclusive editing. " +
      "Mirrors POST /claim exactly, including routing a claim aimed at the reserved whole-document sentinel to " +
      "lease semantics. Read-only against Overleaf itself -- this only writes to the local claims table.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        node_id: { type: "string" },
        holder_token: { type: "string" },
      },
      required: ["project_id", "node_id", "holder_token"],
    },
  },
  {
    name: "lease_document",
    description:
      "Take a whole-document lease (mutually exclusive with any other holder's live claim on the same project). " +
      "Mirrors POST /lease exactly.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        holder_token: { type: "string" },
      },
      required: ["project_id", "holder_token"],
    },
  },
  {
    name: "release_claim",
    description:
      "Release this holder's claim(s). Omitting node_id releases every live claim this holder_token holds on the " +
      "project (including a whole-document lease). Mirrors POST /release exactly.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        holder_token: { type: "string" },
        node_id: {
          type: "string",
          description: "Optional. Omit to release every live claim this holder_token holds on the project.",
        },
      },
      required: ["project_id", "holder_token"],
    },
  },
  {
    name: "get_live_claims",
    description:
      "List every currently-live claim (scoped or whole-document) on a project. Mirrors GET /claims exactly.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
      },
      required: ["project_id"],
    },
  },
  {
    name: "record_provenance",
    description:
      "Record one applied edit in the local, durable provenance ledger (NOT a live meridian-outputs call -- see " +
      "provenance.js's header comment for why this is a local buffer a later agent session syncs from). Mirrors " +
      "POST /provenance exactly.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        node_id: { type: "string" },
        kind: { type: "string" },
        field: { type: "string" },
        old_value: { type: "string", description: "Optional -- the field's prior value, if any." },
        new_value: { type: "string" },
        holder_token: { type: "string" },
      },
      required: ["project_id", "node_id", "kind", "field", "new_value", "holder_token"],
    },
  },
  {
    name: "list_provenance",
    description:
      "List provenance rows for a project, newest first. Set unsynced_only to restrict to rows a meridian-outputs " +
      "sync hasn't consumed yet. Mirrors GET /provenance exactly.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        unsynced_only: { type: "boolean", description: "Optional, defaults to false." },
      },
      required: ["project_id"],
    },
  },
  {
    name: "mark_provenance_synced",
    description:
      "Mark a set of provenance rows as synced, after actually pushing them into meridian-outputs yourself in this " +
      "same agent session. Mirrors POST /provenance/mark-synced exactly. Idempotent.",
    inputSchema: {
      type: "object",
      properties: {
        ids: { type: "array", items: { type: "string" }, description: "Provenance row ids to mark synced." },
      },
      required: ["ids"],
    },
  },
  {
    name: "lookup_citation_key",
    description:
      "Validate a citation key against the local Zotero library's ':key:' tag convention. Mirrors " +
      "GET /zotero-lookup exactly: resolved:true/false/null (null means Zotero's local API was unreachable, not " +
      "'no match').",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string" },
      },
      required: ["key"],
    },
  },
];

/**
 * The actual tool dispatch logic, factored out from request-handling so it
 * can be called directly (by tests, or by anything else in-process) without
 * going through a full MCP request/response round trip. Takes the already-
 * open `db` handle rather than opening its own, so callers -- including
 * tests, which use store.js's `openStore(":memory:")` isolation pattern
 * (see store.test.js) -- control exactly which store instance is used.
 *
 * Never-raises convention at the tool boundary, matching server.js's own
 * try/catch-around-every-handler shape and claims.js/provenance.js's
 * never-throw convention one level down: a thrown error inside a case below
 * is still caught here and turned into a structured `isError` result, never
 * an uncaught exception that would crash the MCP connection.
 */
export async function callTool(db, name, args = {}) {
  try {
    switch (name) {
      case "outline_tex": {
        const { text, project_id } = args;
        if (typeof text !== "string" || !text.trim()) {
          return errorResult("missing or empty 'text' field");
        }
        const nodes = outlineText(text);
        if (typeof project_id === "string" && project_id) {
          const existing = getProject(db, project_id);
          const oldNodes = existing ? JSON.parse(existing.last_outline) : [];
          const { matched, added, removed } = matchOutlines(oldNodes, nodes);
          upsertProject(db, project_id, nodes);
          return textResult({ nodes, matched, added, removed });
        }
        return textResult({ nodes });
      }

      case "outline_tex_file": {
        const { path } = args;
        if (typeof path !== "string" || !path) {
          return errorResult("missing 'path' field");
        }
        const nodes = outlineFile(path);
        return textResult({ nodes });
      }

      case "claim_node": {
        const { project_id, node_id, holder_token } = args;
        const result = claimNode(db, { project_id, node_id, holder_token });
        return textResult(result);
      }

      case "lease_document": {
        const { project_id, holder_token } = args;
        const result = leaseWholeDocument(db, { project_id, holder_token });
        return textResult(result);
      }

      case "release_claim": {
        const { project_id, holder_token, node_id } = args;
        const result = releaseClaims(db, { project_id, holder_token, node_id });
        return textResult(result);
      }

      case "get_live_claims": {
        const { project_id } = args;
        const claims = getLiveClaims(db, project_id);
        return textResult({ claims });
      }

      case "record_provenance": {
        const { project_id, node_id, kind, field, old_value, new_value, holder_token } = args;
        const result = recordEdit(db, { project_id, node_id, kind, field, old_value, new_value, holder_token });
        return textResult(result);
      }

      case "list_provenance": {
        const { project_id, unsynced_only } = args;
        const provenance = listProvenance(db, { project_id, unsynced_only: Boolean(unsynced_only) });
        return textResult({ provenance });
      }

      case "mark_provenance_synced": {
        const { ids } = args;
        const result = markSynced(db, ids);
        return textResult(result);
      }

      case "lookup_citation_key": {
        const { key } = args;
        const result = await lookupCitationKey(key);
        return textResult(result);
      }

      default:
        return errorResult(`unknown tool: ${name}`);
    }
  } catch (err) {
    return errorResult(String((err && err.message) || err));
  }
}

/**
 * Build a fresh MCP Server instance wired to `db`. Factored out (rather than
 * a single module-level singleton) so tests can construct one against an
 * isolated `openStore(":memory:")` instance, and so the real stdio-serving
 * path below and a test's in-process/subprocess path share this exact same
 * wiring instead of two divergent copies.
 */
export function createServer(db) {
  const server = new Server(
    { name: "meridian-latex", version: "0.1.0" },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, async (request) =>
    callTool(db, request.params.name, request.params.arguments ?? {}),
  );

  return server;
}

/**
 * Open the real (fixed-path) store, build the server, and connect it to a
 * real stdio transport. This is the actual "start the MCP server" entry
 * point -- exported (rather than only ever run as an import-time side
 * effect, the way server.js's bottom-of-file `server.listen(...)` runs)
 * specifically so:
 *   - `node src/mcp-server.js` directly (the README's own connection
 *     example -- see point 6) can trigger it via the "was I run directly"
 *     check just below, AND
 *   - cli.js's `mcp` subcommand can trigger the identical startup by calling
 *     this function explicitly after its dynamic import, AND
 *   - a test can `import { TOOLS, callTool, createServer } from
 *     "./mcp-server.js"` to reach the pure, testable pieces WITHOUT that
 *     import itself opening the real on-disk store or attaching a live
 *     stdio transport to the test process's own stdin/stdout (which would
 *     never happen with server.js's unconditional-on-import style, since
 *     nothing here ever needs to import server.js as a library the way
 *     tests need to import this file).
 *
 * openStore() with NO path argument defaults to store.js's DEFAULT_DB_PATH
 * (engine/data/meridian-latex.db, a fixed on-disk file, not ":memory:" and
 * not a per-process temp path -- see store.js's own
 * `openStore(dbPath = DEFAULT_DB_PATH)` signature). server.js calls
 * `openStore()` the exact same way with no override, so this MCP server
 * process and the HTTP server process -- if both running at once on the same
 * machine -- see the SAME claims/provenance state via the SAME SQLite file.
 * better-sqlite3 supports multiple processes opening one file concurrently,
 * so a human clicking through the Chrome extension and an agent session
 * calling these MCP tools stay coordinated through one shared source of
 * truth rather than silently diverging into two.
 */
export async function runStdioServer() {
  const db = openStore();
  const server = createServer(db);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("meridian-latex MCP server running on stdio");
}

/**
 * True iff this module is the process's actual entry point (`node
 * mcp-server.js`), never true for a plain `import`/`await import(...)`.
 *
 * Uses pathToFileURL(...).href rather than a hand-built `file://${path}`
 * template literal: on Windows process.argv[1] is a backslash path
 * ("C:\...\mcp-server.js"), which a naive template string never turns into a
 * valid, comparable file:// URL (wrong separators, no drive-letter/percent
 * -encoding handling) -- pathToFileURL does that conversion correctly on
 * every platform. Wrapped in try/catch because process.argv[1] is not
 * guaranteed to be a well-formed filesystem path in every host (e.g. under
 * `node --test`, argv[1] can be a glob/flag rather than a real file) --
 * anything that isn't a real absolute path to THIS file just means "not the
 * entry point", not a crash.
 */
function isMainModule() {
  try {
    return Boolean(process.argv[1]) && import.meta.url === pathToFileURL(process.argv[1]).href;
  } catch {
    return false;
  }
}

if (isMainModule()) {
  await runStdioServer();
}

export { TOOLS };
