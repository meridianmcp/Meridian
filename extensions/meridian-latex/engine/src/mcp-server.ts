#!/usr/bin/env node
// MCP (Model Context Protocol) server exposing this engine's outline /
// claim-coordination / provenance / citation-lookup capabilities directly to
// an AI agent session (Claude Code, etc.), over stdio.
//
// This is a THIRD way to reach the engine, alongside the two that already
// exist:
//   1. The CLI (cli.js): outline/serve/login/status/logout/ls/write/pull
//      subcommands.
//   2. The local HTTP server (server.js, port 8471): the Chrome extension's
//      popup talks to it via fetch() for the human-in-the-browser flow.
// All three wrap the SAME underlying functions exported from index.js --
// this file adds no new logic, no HTTP hop, no re-implementation. It imports
// directly from ./index.js and calls straight through, in-process, exactly
// like server.js's own handlers do.
//
// Each of the first 10 tools below mirrors the equivalent HTTP endpoint's
// request/response shape (see server.js's own header comment for the
// canonical list) so a caller already familiar with the HTTP API sees the
// identical semantics here, just addressed as an MCP tool instead of a POST
// body. The 5 tools after those wrap capability that didn't exist when this
// file was first written (project-tree.js, bibliography.js,
// section-alias.js, local-snapshot.js) -- see the safety-scoping section
// immediately below for the rule that governs which of that capability is,
// and is not, safe to expose this way. The final 3 (overleaf_login_status,
// list_citation_keys, snapshot_document) close a gap an inventory audit
// found: overleaf-login.js's status(), zotero.js's fetchAllTags(), and
// local-snapshot.js's snapshotDoc() were each already used INTERNALLY by
// other tools/CLI commands but had no direct MCP tool of their own -- same
// safe-tier bar as everything else here (no new logic, no new network
// surface, nothing that reaches applyFieldEdit or OverleafProjectSession).
// 2 more (lint_tex, lint_tex_file) wrap lint.js's static-AST lint suite,
// mirroring outline_tex/outline_tex_file's own text-vs-path split -- same
// safe tier again: pure local parsing, no network, no write path. The final
// 3 (get_style_guide, check_section_style, lookup_published_framing) wrap
// style-guide.js's rhetorical-move-sequence reference -- get_style_guide and
// check_section_style are the same safe tier again (pure local data lookup /
// heuristic text comparison, no network); lookup_published_framing is
// DIFFERENT and clearly separated -- an opt-in, off-by-default STRETCH tool
// that performs a live, attributed fetch from a real published paper via an
// injected research-provider dependency, gated by an env var a human
// controls on this server's own startup environment (never a per-call tool
// argument) -- see style-guide.js's own header comment on
// lookupPublishedFraming for the full rationale, and its "no research-tool
// dependency available" fail-closed contract.
//
// ============================================================================
// CRITICAL SAFETY SCOPING -- read before adding a tool here
// ============================================================================
// This server deliberately does NOT expose write.js's applyFieldEdit -- the
// direct WebSocket/OT WRITE path into a live Overleaf document -- as an MCP
// tool, and nothing here can dispatch a live edit into a real, open Overleaf
// document.
//
// Actually writing into a live Overleaf doc requires either:
//   (a) a live browser tab running the Chrome extension's own CM6-dispatch
//       path (extension/injected.js) -- unreachable from a pure Node MCP
//       server process, full stop; or
//   (b) a saved human Overleaf login cookie (overleaf-login.js) driving
//       overleaf-ot-client.js's OT/WebSocket session's own submitOtUpdate
//       path (write.js's applyFieldEdit).
//
// This project's hard rule is that an automated/agent process must NEVER
// perform a live WRITE on a human's behalf without an explicit, separate
// confirmation gate -- the same reasoning cli.js's own `login` command
// documents ("human-only -- never run this from an agent session"). An MCP
// tool call is exactly the kind of automated invocation that rule exists to
// stop: an agent session could call an MCP tool autonomously, with no human
// clicking anything, so putting a live-write capability behind one would
// silently defeat the human-in-the-loop gate the Chrome-extension-only path
// currently guarantees. applyFieldEdit is never imported into this file, and
// no tool here accepts arguments shaped like an edit (a field name + new
// text targeting a live doc) -- if a future contributor is tempted to add
// one, don't, without first re-deriving why this was left out and building
// the confirmation gate this file deliberately does not have.
//
// A NARROWER capability -- connecting to a live Overleaf project to READ it
// (list its file tree, fetch a doc's current text) -- is used below by
// list_project_docs and pull_doc_expanded. This is a deliberate, later
// addition, not an oversight of the rule above: reading is not writing, it
// carries no risk of corrupting a human's live document, and it is the exact
// same class of operation outline_tex_file already performs for a LOCAL
// file, just reached over the network instead of from disk -- it is also
// exactly what cli.js's own `ls`/`pull` commands already do, safely, today.
// Both of those two tools go through loadSavedCookie() (overleaf-login.js)
// exactly as cli.js's writeField()/pullDoc()/listProjectDocs() do: the
// cookie is read from the local, human-created `login` session on disk and
// used in-process to open the connection -- it is NEVER accepted as a tool
// argument, never echoed back in a tool result, and never otherwise crosses
// the MCP protocol boundary itself. Neither tool accepts anything resembling
// edit content. If there is no saved session, both return a clear tool-level
// error ("no saved Overleaf session...") rather than throwing raw or
// prompting for credentials.
// ============================================================================

import type Database from "better-sqlite3";
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
  loadSavedCookie,
  status,
  connectToProject,
  listDocPaths,
  joinDocExpanded,
  getBibliography,
  expandSectionAliases,
  listSnapshots,
  fetchAllTags,
  snapshotDoc,
  lintText,
  lintFile,
  getStyleGuide,
  checkSectionStyle,
  lookupPublishedFraming,
} from "./index.js";
import type { OutlineNode } from "./outline.js";
import type { ConnectToProjectOptions } from "./overleaf-ot-client.js";
import type { FileTreeFolder, ResolveDocIdResult } from "./project-tree.js";
import type { LookupPublishedFramingOptions } from "./style-guide.js";

/** Overleaf's real-time layer uses raw Mongo ObjectIds (24 lowercase hex
 * chars) for doc ids -- same check cli.js's own writeField()/pullDoc()/
 * listProjectDocs() use to tell "already a raw docId" apart from "a
 * project-relative path" without a separate flag. Kept in sync by hand
 * (cli.js doesn't export its copy). */
const OBJECT_ID_RE = /^[0-9a-f]{24}$/;

interface ToolResultContent {
  type: "text";
  text: string;
}

/** The shape every tool call below resolves to -- a subset of the MCP SDK's
 * own CallToolResult, precise enough for this file's own logic and test
 * suite without importing that broader (partly optional, deeply-nested) SDK
 * type. The index signature is required, not decorative: the SDK's own
 * `Server.setRequestHandler` return type is checked against its `Result`
 * schema type, which itself carries a passthrough index signature -- a named
 * interface without one is not assignable there even though every actual
 * value it describes trivially satisfies it (see `createServer` below). */
interface ToolResult {
  content: ToolResultContent[];
  isError?: boolean;
  [key: string]: unknown;
}

function textResult(payload: unknown): ToolResult {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

function errorResult(message: string): ToolResult {
  return { content: [{ type: "text", text: JSON.stringify({ error: message }) }], isError: true };
}

interface JsonSchemaProperty {
  type: string;
  description?: string;
  items?: { type: string };
}

interface ToolInputSchema {
  type: "object";
  properties: Record<string, JsonSchemaProperty>;
  required: string[];
}

/** One MCP tool definition, as `TOOLS` below declares each entry and
 * `createServer`'s `tools/list` handler reports verbatim. */
interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: ToolInputSchema;
}

const TOOLS: ToolDefinition[] = [
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
  {
    name: "list_project_docs",
    description:
      "List every document's project-relative path and doc id in a live Overleaf project's file tree. Read-only: " +
      "connects using the locally saved Overleaf session (the same one `meridian-latex login` creates) to fetch the " +
      "current file tree, exactly like the CLI's own `ls` command, and never modifies anything in the project. " +
      "Returns a clear tool-level error, never a raw throw, if no saved session exists.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string", description: "The Overleaf project id to list." },
      },
      required: ["project_id"],
    },
  },
  {
    name: "pull_doc_expanded",
    description:
      "Fetch one document's current, fully-expanded text from a live Overleaf project (every \\input/\\include " +
      "reference spliced in) and return it directly, without writing a local file. Read-only, matching the CLI's " +
      "own `pull` command's fetch step exactly, minus its local-file-write side effect -- an MCP tool's job is to " +
      "return data to the calling agent, not manage files on disk. Connects using the locally saved Overleaf " +
      "session. doc_id_or_path may be a raw doc id or a project-relative path like \"chapters/intro.tex\".",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        doc_id_or_path: {
          type: "string",
          description: "A raw doc id, or a project-relative path such as \"main.tex\".",
        },
      },
      required: ["project_id", "doc_id_or_path"],
    },
  },
  {
    name: "get_bibliography",
    description:
      "Parse bibliography entries out of .tex source text: inline \\bibitem entries, plus -- if bib_text is " +
      "supplied -- bibtex/biblatex @-entries from an already-fetched .bib file's text. Pure and local: takes plain " +
      "source text as input and performs no file or network access of its own.",
    inputSchema: {
      type: "object",
      properties: {
        source_text: { type: "string", description: "Raw .tex source text to extract bibliography references from." },
        bib_text: {
          type: "string",
          description:
            "Optional. The text of a .bib file already fetched by the caller, used to resolve any " +
            "\\bibliography{}/\\addbibresource{} references found in source_text.",
        },
      },
      required: ["source_text"],
    },
  },
  {
    name: "expand_section_aliases",
    description:
      "Rewrite \\newcommand-defined section-aliasing macros (e.g. \\newcommand{\\mysection}[1]{\\section{#1}}) to " +
      "their underlying sectioning macro, so a paper's own aliased headings become visible to outline_tex. Pure " +
      "text in, text out -- no file or network access.",
    inputSchema: {
      type: "object",
      properties: {
        source_text: { type: "string" },
      },
      required: ["source_text"],
    },
  },
  {
    name: "list_local_snapshots",
    description:
      "List the local, on-disk pre-write snapshot files already saved for one project/doc (under " +
      "~/.meridian-latex/snapshots), newest first. Read-only -- lists what already exists on disk; never creates a " +
      "new snapshot itself.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        doc_id: { type: "string" },
      },
      required: ["project_id", "doc_id"],
    },
  },
  {
    name: "overleaf_login_status",
    description:
      "Check whether a saved Overleaf session exists, without ever touching or returning the session cookie " +
      "itself. Wraps overleaf-login.js's status(): a pure local file read + JSON parse of the cookie file under " +
      "~/.meridian-latex (or a saved-session-free {loggedIn:false} if none exists or it can't be parsed). Mirrors " +
      "the CLI's own `status` command. Read-only, no network access -- does NOT connect to Overleaf, and never " +
      "returns the cookie value.",
    inputSchema: {
      type: "object",
      properties: {},
      required: [],
    },
  },
  {
    name: "list_citation_keys",
    description:
      "List every tag string in the local Zotero library (127.0.0.1:23119, Zotero desktop's own local API), " +
      "including the ':key:<citekey>' tags lookup_citation_key resolves against. Wraps zotero.js's fetchAllTags() " +
      "directly -- the same paginated, read-only GET already used internally by lookup_citation_key, just returned " +
      "as a flat list instead of tested against one key. No side effects. Returns a tool-level error (never a raw " +
      "throw) if the local Zotero API is unreachable.",
    inputSchema: {
      type: "object",
      properties: {},
      required: [],
    },
  },
  {
    name: "snapshot_document",
    description:
      "Save a full-text local snapshot of a document under ~/.meridian-latex/snapshots -- never touches Overleaf " +
      "itself, purely a local, one-directional, timestamped copy on disk (see local-snapshot.js's own header for " +
      "the pre-write-safety-net rationale). Wraps snapshotDoc(): accepts plain 'text' and splits it into lines " +
      "internally before calling through, the same shape pull_doc_expanded's own 'source' field already produces " +
      "and an agent could pass straight through. Returns the written file's absolute path.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string", description: "Used only as a path segment -- format not validated." },
        doc_id: { type: "string", description: "Used only as a path segment -- format not validated." },
        text: { type: "string", description: "The document's full text to snapshot; split into lines internally." },
      },
      required: ["project_id", "doc_id", "text"],
    },
  },
  {
    name: "lint_tex",
    description:
      "Run the static-AST lint suite (see lint.js's own header for the full list of 11 checks: missing/duplicate " +
      "citations and labels, unresolved \\ref targets, section-hierarchy skips, unclosed-environment/parse-failure " +
      "recovery, duplicate/unused bibliography entries, empty captions/labels, empty citation keys, stray " +
      "TODO/FIXME/XXX markers, and unresolved nested sub-labels) against raw .tex source text. Never throws -- a " +
      "parse failure itself becomes a single finding rather than a tool error. Mirrors outline_tex's text-in split.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Raw .tex source text to lint." },
        bib_text: {
          type: "string",
          description:
            "Optional. The text of a .bib file already fetched by the caller, used to resolve any " +
            "\\bibliography{}/\\addbibresource{} references found in text -- same shape as get_bibliography's own " +
            "bib_text argument.",
        },
      },
      required: ["text"],
    },
  },
  {
    name: "lint_tex_file",
    description:
      "Run the same static-AST lint suite as lint_tex, but reading a LOCAL .tex file on disk via lintFile() -- no " +
      "server or browser tab involved, same relationship to lint_tex that outline_tex_file has to outline_tex.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string", description: "Filesystem path to a .tex file." },
        bib_text: {
          type: "string",
          description: "Optional -- see lint_tex's own bib_text argument.",
        },
      },
      required: ["path"],
    },
  },
  {
    name: "get_style_guide",
    description:
      "Pure data lookup (no network) of the 8-category, originally-written reference of rhetorical move " +
      "sequences per section-type (Abstract, Introduction, Related Work, Methods, Results, Limitations, " +
      "Discussion/Conclusion, Ethics/Broader Impact) -- see style-guide.js's own header for the tier-1, " +
      "npm-publishable content note. Omit section_type for the full structure, or pass one to filter to a " +
      "single section-type's guide (accepts common aliases, e.g. \"related work\", \"prior work\", \"intro\").",
    inputSchema: {
      type: "object",
      properties: {
        section_type: {
          type: "string",
          description: "Optional. One of the 8 section-types (or a common alias); omit for the full guide.",
        },
      },
      required: [],
    },
  },
  {
    name: "check_section_style",
    description:
      "Heuristically compare a section's text against get_style_guide's expected move sequence for a declared " +
      "section_type, reporting moves present/missing/out-of-order. THIS IS A STRUCTURAL HEURISTIC BASED ON " +
      "KEYWORD/CUE MATCHING, NOT A GROUND-TRUTH OR COMPILER-VERIFIED CHECK -- the result's own `disclaimer` field " +
      "repeats this. Provide either 'text' (already-resolved raw section text) or 'source_text' plus " +
      "'heading_id'/'heading_title' to resolve the section's range from a heading node in a fresh outline of " +
      "source_text (see style-guide.js's resolveSectionText for the exact range rule and its documented v0 " +
      "nesting-depth limitation).",
    inputSchema: {
      type: "object",
      properties: {
        section_type: { type: "string", description: "Declared section-type (or alias) this text is claimed to be." },
        text: { type: "string", description: "Already-resolved raw section text. Mutually exclusive with source_text." },
        source_text: {
          type: "string",
          description: "Raw .tex source text to outline and resolve a heading's range from. Requires heading_id or heading_title.",
        },
        heading_id: { type: "string", description: "A heading node id from a fresh outline of source_text." },
        heading_title: { type: "string", description: "A heading's title text (case-insensitive) from source_text." },
      },
      required: ["section_type"],
    },
  },
  {
    name: "lookup_published_framing",
    description:
      "STRETCH, OFF BY DEFAULT -- clearly separated from get_style_guide/check_section_style's safe, static, " +
      "no-network default tier. Given a section_type + topic, fetches a short, properly attributed excerpt from " +
      "a REAL published paper LIVE at call time, via a research/paper-search capability the calling session must " +
      "wire in -- this package never hardcodes a specific provider. The excerpt is NEVER cached or bundled into " +
      "this npm package: it is an ephemeral tool response only (see the result's own stored:false field). Gated " +
      "off by default behind the MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING environment variable, set by whoever " +
      "configures this server's own startup environment -- NOT a per-call argument here, deliberately, so an " +
      "agent session can't just turn this on itself. If no research-tool dependency is available in the calling " +
      "session, this FAILS CLOSED with a clear, explicit error -- never a silent empty result.",
    inputSchema: {
      type: "object",
      properties: {
        section_type: { type: "string", description: "One of the 8 section-types (or a common alias)." },
        topic: { type: "string", description: "The topic to search published papers for framing/excerpts about." },
      },
      required: ["section_type", "topic"],
    },
  },
];

/** The minimal shape this file's own list_project_docs/pull_doc_expanded
 * tools need from a live session -- same minimal-interface pattern write.ts's
 * WriteSession / input-expansion.ts's ExpandableSession already establish
 * elsewhere in this codebase, rather than requiring the full
 * OverleafProjectSession class shape (which this file's own test suite's
 * fakeSession() deliberately never implements in full -- it only ever needs
 * `rootFolder`, `resolveDocId`, `joinDoc`, and `close`). The real
 * OverleafProjectSession (overleaf-ot-client.ts) satisfies this structurally
 * -- it is never imported here BY NAME, per this file's own safety-scoping
 * rule above. */
interface McpSession {
  rootFolder: FileTreeFolder | null;
  resolveDocId(path: string): ResolveDocIdResult;
  joinDoc(docId: string): Promise<{ lines: string[]; version: number }>;
  close(): void;
}

/** The minimal shape callTool's own `status` dependency needs to satisfy.
 * The real overleaf-login.ts `status()` returns a discriminated
 * `OverleafLoginStatus` whose `loggedIn:false` branch has no `baseUrl` field
 * at all -- but this file's own test suite injects fakes shaped more loosely
 * than that (e.g. just `{baseUrl}`, to exercise the connect-baseUrl-
 * extraction path in isolation, independently of the full status() result
 * shape `overleaf_login_status` itself passes straight through). This looser
 * shape is what both the real implementation and every test double actually
 * satisfy structurally. */
type StatusLike = Record<string, unknown> & { baseUrl?: string };

/** Deps `callTool` accepts to override the functions that reach outside this
 * process -- see this function's own doc comment below for the full
 * rationale. Every field is optional; omitting `deps` entirely (every
 * production caller) falls back to the real imports above. */
interface CallToolDeps {
  loadSavedCookie?: typeof loadSavedCookie;
  status?: () => StatusLike;
  connectToProject?: (options: ConnectToProjectOptions) => Promise<McpSession>;
  fetchAllTags?: typeof fetchAllTags;
  snapshotDoc?: typeof snapshotDoc;
  researchProvider?: LookupPublishedFramingOptions["researchProvider"];
  publishedFramingEnabled?: boolean;
}

/**
 * The actual tool dispatch logic, factored out from request-handling so it
 * can be called directly (by tests, or by anything else in-process) without
 * going through a full MCP request/response round trip. Takes the already-
 * open `db` handle rather than opening its own, so callers -- including
 * tests, which use store.js's `openStore(":memory:")` isolation pattern
 * (see store.test.js) -- control exactly which store instance is used.
 *
 * `deps` is an optional override for the functions that reach outside this
 * process -- a live Overleaf project (loadSavedCookie/status/connectToProject),
 * the local Zotero HTTP API (fetchAllTags), local disk (snapshotDoc), and
 * lookup_published_framing's own two knobs (researchProvider,
 * publishedFramingEnabled) -- defaulted to the real imports above (or, for
 * the latter two, to `undefined`, since this package bundles no real
 * research provider of its own) so production callers (createServer() below)
 * never pass it and get the real behavior. Tests for list_project_docs/
 * pull_doc_expanded/list_citation_keys/snapshot_document/lookup_published_framing
 * inject fakes here instead, the same "swap the real implementation for a
 * test double via an argument" shape overleaf-ot-client.js's own
 * connectToProject already uses for its Socket09ClientImpl parameter -- so
 * no test in this file ever makes a real network call, touches a real saved
 * cookie, or writes under the real ~/.meridian-latex directory.
 *
 * Never-raises convention at the tool boundary, matching server.js's own
 * try/catch-around-every-handler shape and claims.js/provenance.js's
 * never-throw convention one level down: a thrown error inside a case below
 * is still caught here and turned into a structured `isError` result, never
 * an uncaught exception that would crash the MCP connection.
 */
export async function callTool(
  db: Database.Database,
  name: string,
  args: Record<string, unknown> = {},
  deps: CallToolDeps = {},
): Promise<ToolResult> {
  // Deliberately NOT a single destructured-with-defaults block (`const
  // {loadSavedCookie: loadSavedCookieImpl = loadSavedCookie, ...} = deps`):
  // when a destructured binding's default value's own type differs from the
  // (optional) declared property type -- exactly the case here, since the
  // real `status` returns the stricter `OverleafLoginStatus` while
  // `CallToolDeps.status` is typed to the looser `StatusLike` every test
  // double also needs to satisfy -- TS infers the binding as the UNION of
  // both types rather than the declared one, which then breaks a plain
  // `.baseUrl` read below. An explicit annotation per binding sidesteps that
  // inference entirely.
  const loadSavedCookieImpl: typeof loadSavedCookie = deps.loadSavedCookie ?? loadSavedCookie;
  const statusImpl: () => StatusLike = deps.status ?? status;
  const connectToProjectImpl: (options: ConnectToProjectOptions) => Promise<McpSession> =
    deps.connectToProject ?? connectToProject;
  const fetchAllTagsImpl: typeof fetchAllTags = deps.fetchAllTags ?? fetchAllTags;
  const snapshotDocImpl: typeof snapshotDoc = deps.snapshotDoc ?? snapshotDoc;
  // lookup_published_framing's own two injectable knobs -- both undefined by
  // default (production callers never pass deps), which is exactly correct:
  // this package bundles no real research provider and the feature is off
  // unless MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING (or an injected
  // `publishedFramingEnabled: true`) says otherwise. Tests inject fakes for
  // both the same way every other *Impl above is injected.
  const researchProviderImpl = deps.researchProvider;
  const publishedFramingEnabledImpl = deps.publishedFramingEnabled;

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
          const oldNodes: OutlineNode[] = existing ? (JSON.parse(existing.last_outline) as OutlineNode[]) : [];
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
        // `key` arrives as an unknown JSON-RPC argument; lookupCitationKey's
        // own runtime contract (a bare `!citationKey` falsy check) already
        // treats anything that isn't a genuine, truthy string exactly like a
        // missing key, so this cast changes nothing about the real behavior.
        const result = await lookupCitationKey(key as string | null | undefined);
        return textResult(result);
      }

      case "list_project_docs": {
        const { project_id } = args;
        if (typeof project_id !== "string" || !project_id) {
          return errorResult("missing 'project_id' field");
        }
        const cookie = loadSavedCookieImpl();
        if (!cookie) {
          return errorResult("no saved Overleaf session -- run `meridian-latex login` first");
        }
        const httpBaseUrl = statusImpl().baseUrl || "https://www.overleaf.com";
        const wsBaseUrl = httpBaseUrl.replace(/^http/, "ws");
        const session = await connectToProjectImpl({ projectId: project_id, httpBaseUrl, wsBaseUrl, cookie });
        try {
          return textResult({ docs: listDocPaths(session.rootFolder) });
        } finally {
          session.close();
        }
      }

      case "pull_doc_expanded": {
        const { project_id, doc_id_or_path } = args;
        if (typeof project_id !== "string" || !project_id) {
          return errorResult("missing 'project_id' field");
        }
        if (typeof doc_id_or_path !== "string" || !doc_id_or_path) {
          return errorResult("missing 'doc_id_or_path' field");
        }
        const cookie = loadSavedCookieImpl();
        if (!cookie) {
          return errorResult("no saved Overleaf session -- run `meridian-latex login` first");
        }
        const httpBaseUrl = statusImpl().baseUrl || "https://www.overleaf.com";
        const wsBaseUrl = httpBaseUrl.replace(/^http/, "ws");
        const session = await connectToProjectImpl({ projectId: project_id, httpBaseUrl, wsBaseUrl, cookie });
        try {
          let docId = doc_id_or_path;
          if (!OBJECT_ID_RE.test(doc_id_or_path)) {
            const resolved = session.resolveDocId(doc_id_or_path);
            if (!resolved.ok) {
              return errorResult(`could not resolve "${doc_id_or_path}" to a doc: ${resolved.reason}`);
            }
            docId = resolved.docId;
          }
          const { source, unexpanded, version } = await joinDocExpanded(session, docId);
          return textResult({ source, unexpanded, version, docId });
        } finally {
          session.close();
        }
      }

      case "get_bibliography": {
        const { source_text, bib_text } = args;
        if (typeof source_text !== "string" || !source_text) {
          return errorResult("missing 'source_text' field");
        }
        const options = typeof bib_text === "string" && bib_text ? { resolveBibText: () => bib_text } : undefined;
        const entries = getBibliography(source_text, options);
        return textResult({ entries });
      }

      case "expand_section_aliases": {
        const { source_text } = args;
        if (typeof source_text !== "string" || !source_text) {
          return errorResult("missing 'source_text' field");
        }
        const expanded = expandSectionAliases(source_text);
        return textResult({ expanded });
      }

      case "list_local_snapshots": {
        const { project_id, doc_id } = args;
        if (typeof project_id !== "string" || !project_id) {
          return errorResult("missing 'project_id' field");
        }
        if (typeof doc_id !== "string" || !doc_id) {
          return errorResult("missing 'doc_id' field");
        }
        const paths = listSnapshots({ projectId: project_id, docId: doc_id });
        return textResult({ paths });
      }

      case "overleaf_login_status": {
        const result = statusImpl();
        return textResult(result);
      }

      case "list_citation_keys": {
        const tags = await fetchAllTagsImpl();
        return textResult({ tags });
      }

      case "snapshot_document": {
        const { project_id, doc_id, text } = args;
        if (typeof project_id !== "string" || !project_id) {
          return errorResult("missing 'project_id' field");
        }
        if (typeof doc_id !== "string" || !doc_id) {
          return errorResult("missing 'doc_id' field");
        }
        if (typeof text !== "string") {
          return errorResult("missing 'text' field");
        }
        const path = snapshotDocImpl({ projectId: project_id, docId: doc_id, lines: text.split("\n") });
        return textResult({ path });
      }

      case "lint_tex": {
        const { text, bib_text } = args;
        if (typeof text !== "string" || !text.trim()) {
          return errorResult("missing or empty 'text' field");
        }
        const options = typeof bib_text === "string" && bib_text ? { bibText: bib_text } : undefined;
        const findings = lintText(text, options);
        return textResult({ findings });
      }

      case "lint_tex_file": {
        const { path, bib_text } = args;
        if (typeof path !== "string" || !path) {
          return errorResult("missing 'path' field");
        }
        const options = typeof bib_text === "string" && bib_text ? { bibText: bib_text } : undefined;
        const findings = lintFile(path, options);
        return textResult({ findings });
      }

      case "get_style_guide": {
        const { section_type } = args;
        const result = getStyleGuide(section_type);
        if ("error" in result) return errorResult(result.error);
        return textResult(result);
      }

      case "check_section_style": {
        const { section_type, text, source_text, heading_id, heading_title } = args;
        if (typeof section_type !== "string" || !section_type) {
          return errorResult("missing 'section_type' field");
        }
        // text/source_text/heading_id/heading_title arrive as unknown
        // JSON-RPC arguments; checkSectionStyle's own resolveSectionText only
        // ever treats a non-string value the same as "not provided" (via its
        // own `typeof text === "string"` checks), so this cast changes
        // nothing about the real behavior -- it just tells the type checker
        // what the runtime already tolerates.
        const result = checkSectionStyle({
          sectionType: section_type,
          text: text as string | undefined,
          sourceText: source_text as string | undefined,
          headingId: heading_id as string | undefined,
          headingTitle: heading_title as string | undefined,
        });
        if ("error" in result) return errorResult(result.error);
        return textResult(result);
      }

      case "lookup_published_framing": {
        const { section_type, topic } = args;
        if (typeof section_type !== "string" || !section_type) {
          return errorResult("missing 'section_type' field");
        }
        if (typeof topic !== "string" || !topic) {
          return errorResult("missing 'topic' field");
        }
        // publishedFramingEnabledImpl lets a test override the env-var gate
        // deterministically without mutating process.env; production callers
        // never pass it, so real behavior always falls through to the real
        // ENABLE_PUBLISHED_FRAMING_ENV_VAR check inside lookupPublishedFraming
        // itself (see its own header for why that's an env-var gate, not a
        // tool argument).
        const lookupOptions: LookupPublishedFramingOptions = { researchProvider: researchProviderImpl };
        if (typeof publishedFramingEnabledImpl === "boolean") {
          lookupOptions.enabled = publishedFramingEnabledImpl;
        }
        const result = await lookupPublishedFraming(section_type, topic, lookupOptions);
        if ("error" in result) return errorResult(result.error);
        return textResult(result);
      }

      default:
        return errorResult(`unknown tool: ${name}`);
    }
  } catch (err) {
    return errorResult(String((err && (err as Error).message) || err));
  }
}

/**
 * Build a fresh MCP Server instance wired to `db`. Factored out (rather than
 * a single module-level singleton) so tests can construct one against an
 * isolated `openStore(":memory:")` instance, and so the real stdio-serving
 * path below and a test's in-process/subprocess path share this exact same
 * wiring instead of two divergent copies.
 */
export function createServer(db: Database.Database): Server {
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
export async function runStdioServer(): Promise<void> {
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
function isMainModule(): boolean {
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
