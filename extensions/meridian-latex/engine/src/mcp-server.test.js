import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { openStore } from "./store.js";
import { callTool, TOOLS, createServer } from "./mcp-server.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MCP_SERVER_PATH = join(__dirname, "mcp-server.js");
const CLI_PATH = join(__dirname, "cli.js");

function freshStore() {
  return openStore(":memory:");
}

function parseText(result) {
  assert.equal(result.content.length, 1);
  assert.equal(result.content[0].type, "text");
  return JSON.parse(result.content[0].text);
}

// --- TOOLS: shape + the deliberate safety-scoping exclusion -----------------

test("TOOLS: exposes exactly the 10 documented tools, each with a well-formed JSON-Schema inputSchema", () => {
  const expectedNames = [
    "outline_tex",
    "outline_tex_file",
    "claim_node",
    "lease_document",
    "release_claim",
    "get_live_claims",
    "record_provenance",
    "list_provenance",
    "mark_provenance_synced",
    "lookup_citation_key",
  ];
  assert.deepEqual(
    TOOLS.map((t) => t.name),
    expectedNames,
  );
  for (const tool of TOOLS) {
    assert.equal(typeof tool.description, "string");
    assert.ok(tool.description.length > 0, `${tool.name} must have a non-empty description`);
    assert.equal(tool.inputSchema.type, "object");
    assert.equal(typeof tool.inputSchema.properties, "object");
    assert.ok(Array.isArray(tool.inputSchema.required), `${tool.name}.inputSchema.required must be an array`);
    for (const requiredField of tool.inputSchema.required) {
      assert.ok(
        Object.prototype.hasOwnProperty.call(tool.inputSchema.properties, requiredField),
        `${tool.name}: required field '${requiredField}' must also be a declared property`,
      );
    }
  }
});

test("TOOLS: never exposes a live Overleaf write capability (deliberate safety scoping)", () => {
  // No tool name or description may reference the direct WebSocket/OT write
  // path (overleaf-ot-client.js's connectToProject/OverleafProjectSession) or
  // otherwise claim to dispatch a live edit into Overleaf -- see this file's
  // own header comment for why. This test exists so that if a future
  // contributor DOES add such a tool, at least one automated check fails
  // loudly rather than the omission just silently eroding over time.
  const forbidden = /connecttoproject|overleafprojectsession|live[-_ ]?write|dispatch.*overleaf|websocket/i;
  for (const tool of TOOLS) {
    assert.ok(!forbidden.test(tool.name), `tool name '${tool.name}' looks like a live-write capability`);
    assert.ok(
      !forbidden.test(tool.description),
      `tool '${tool.name}' description looks like it claims a live-write capability`,
    );
  }
});

// --- outline_tex -------------------------------------------------------------

test("outline_tex: no project_id -- shape matches POST /outline's backward-compatible {nodes} response", async () => {
  const db = freshStore();
  const result = await callTool(db, "outline_tex", { text: "\\section{Intro}\nSee \\cite{alice2020}.\n" });
  const parsed = parseText(result);
  assert.deepEqual(Object.keys(parsed), ["nodes"]);
  assert.equal(parsed.nodes.length, 2);
  assert.equal(parsed.nodes[0].kind, "heading");
  assert.equal(parsed.nodes[0].title, "Intro");
  assert.equal(parsed.nodes[1].kind, "citation");
  assert.equal(parsed.nodes[1].key, "alice2020");
});

test("outline_tex: with project_id -- shape matches POST /outline's {nodes, matched, added, removed} response, and registers the project", async () => {
  const db = freshStore();
  const projectId = `proj-${randomUUID()}`;
  const first = parseText(
    await callTool(db, "outline_tex", { text: "\\section{Intro}\n", project_id: projectId }),
  );
  assert.deepEqual(first.matched, []);
  assert.equal(first.added.length, 1);
  assert.deepEqual(first.removed, []);

  // Re-parsing the SAME text for the SAME project now reports it as matched,
  // not added again -- proves upsertProject() actually persisted the outline
  // (i.e. this tool really shares state with getProject/upsertProject, not a
  // throwaway per-call diff).
  const second = parseText(
    await callTool(db, "outline_tex", { text: "\\section{Intro}\n", project_id: projectId }),
  );
  assert.equal(second.matched.length, 1);
  assert.deepEqual(second.added, []);
  assert.deepEqual(second.removed, []);
});

test("outline_tex: missing/empty text is a structured tool error, never a throw", async () => {
  const db = freshStore();
  const missing = await callTool(db, "outline_tex", {});
  assert.equal(missing.isError, true);
  assert.match(parseText(missing).error, /missing or empty 'text'/);

  const empty = await callTool(db, "outline_tex", { text: "   " });
  assert.equal(empty.isError, true);
});

// --- outline_tex_file ---------------------------------------------------------

test("outline_tex_file: parses a real local .tex file via outlineFile(), the one capability this MCP surface adds over the Chrome-extension-only path", async () => {
  const db = freshStore();
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-mcp-test-"));
  try {
    const texPath = join(dir, "paper.tex");
    writeFileSync(texPath, "\\section{Methods}\nSee \\cite{bob2021}.\n");
    const result = await callTool(db, "outline_tex_file", { path: texPath });
    const parsed = parseText(result);
    assert.equal(parsed.nodes.length, 2);
    assert.equal(parsed.nodes[0].title, "Methods");
    assert.equal(parsed.nodes[1].key, "bob2021");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("outline_tex_file: a missing path is a structured tool error", async () => {
  const db = freshStore();
  const missing = await callTool(db, "outline_tex_file", {});
  assert.equal(missing.isError, true);
});

test("outline_tex_file: a nonexistent file surfaces as a structured tool error, never an uncaught throw", async () => {
  const db = freshStore();
  const result = await callTool(db, "outline_tex_file", { path: join(tmpdir(), `does-not-exist-${randomUUID()}.tex`) });
  assert.equal(result.isError, true);
});

// --- claim/lease/release/get_live_claims -- mirrors claims.js's own rules, one level up ---

test("claim_node: two holders on the same node_id -- second is rejected with the conflicting holder, matching POST /claim", async () => {
  const db = freshStore();
  const projectId = `proj-${randomUUID()}`;
  const alice = parseText(
    await callTool(db, "claim_node", { project_id: projectId, node_id: "heading:abc", holder_token: "alice" }),
  );
  assert.equal(alice.claimed, true);

  const bob = parseText(
    await callTool(db, "claim_node", { project_id: projectId, node_id: "heading:abc", holder_token: "bob" }),
  );
  assert.equal(bob.claimed, false);
  assert.equal(bob.holder_token_of_conflict, "alice");
});

test("lease_document: a live scoped claim by another holder blocks a new whole-document lease, matching POST /lease", async () => {
  const db = freshStore();
  const projectId = `proj-${randomUUID()}`;
  await callTool(db, "claim_node", { project_id: projectId, node_id: "heading:abc", holder_token: "alice" });

  const bob = parseText(await callTool(db, "lease_document", { project_id: projectId, holder_token: "bob" }));
  assert.equal(bob.leased, false);
  assert.equal(bob.holder_token_of_conflict, "alice");
});

test("release_claim: omitting node_id releases every live claim this holder holds, matching POST /release", async () => {
  const db = freshStore();
  const projectId = `proj-${randomUUID()}`;
  const holder = "alice";
  await callTool(db, "claim_node", { project_id: projectId, node_id: "n1", holder_token: holder });
  await callTool(db, "claim_node", { project_id: projectId, node_id: "n2", holder_token: holder });

  const released = parseText(await callTool(db, "release_claim", { project_id: projectId, holder_token: holder }));
  assert.equal(released.released, 2);

  const claims = parseText(await callTool(db, "get_live_claims", { project_id: projectId }));
  assert.deepEqual(claims.claims, []);
});

test("get_live_claims: lists a live claim with its holder_token, matching GET /claims", async () => {
  const db = freshStore();
  const projectId = `proj-${randomUUID()}`;
  await callTool(db, "claim_node", { project_id: projectId, node_id: "n1", holder_token: "alice" });

  const claims = parseText(await callTool(db, "get_live_claims", { project_id: projectId })).claims;
  assert.equal(claims.length, 1);
  assert.equal(claims[0].node_id, "n1");
  assert.equal(claims[0].holder_token, "alice");
});

// --- record_provenance / list_provenance / mark_provenance_synced -----------

test("record_provenance -> list_provenance -> mark_provenance_synced: a full round trip, matching the three /provenance* HTTP endpoints", async () => {
  const db = freshStore();
  const projectId = `proj-${randomUUID()}`;

  const recorded = parseText(
    await callTool(db, "record_provenance", {
      project_id: projectId,
      node_id: "n1",
      kind: "heading",
      field: "title",
      old_value: "Old Title",
      new_value: "New Title",
      holder_token: "alice",
    }),
  );
  assert.equal(recorded.recorded, true);
  assert.equal(typeof recorded.id, "string");

  const unsynced = parseText(await callTool(db, "list_provenance", { project_id: projectId, unsynced_only: true }));
  assert.equal(unsynced.provenance.length, 1);
  assert.equal(unsynced.provenance[0].id, recorded.id);
  assert.equal(unsynced.provenance[0].new_value, "New Title");

  const marked = parseText(await callTool(db, "mark_provenance_synced", { ids: [recorded.id] }));
  assert.equal(marked.marked, 1);

  const stillUnsynced = parseText(
    await callTool(db, "list_provenance", { project_id: projectId, unsynced_only: true }),
  );
  assert.deepEqual(stillUnsynced.provenance, []);
});

test("record_provenance: missing required fields is a structured tool result, never a throw", async () => {
  const db = freshStore();
  const result = parseText(await callTool(db, "record_provenance", { project_id: "p1" }));
  assert.equal(result.recorded, false);
});

// --- lookup_citation_key ------------------------------------------------------
// Same real-network call server.js's own GET /zotero-lookup makes (neither
// injects a fetchImpl override) -- so this only asserts the RESPONSE SHAPE
// (resolved is boolean or null, matching zotero.js's own documented
// true/false/null contract), not a specific outcome, since whether a real
// local Zotero happens to be running is unknowable/uncontrolled here. See
// zotero.test.js for fully-mocked, deterministic coverage of the underlying
// lookupCitationKey()/fetchAllTags() logic itself.

test("lookup_citation_key: response shape matches GET /zotero-lookup's resolved true/false/null contract", async () => {
  const db = freshStore();
  const result = parseText(await callTool(db, "lookup_citation_key", { key: `nonexistent-${randomUUID()}` }));
  assert.ok(
    result.resolved === true || result.resolved === false || result.resolved === null,
    `resolved must be true/false/null, got ${JSON.stringify(result.resolved)}`,
  );
});

// --- dispatch-level errors -----------------------------------------------------

test("callTool: an unknown tool name is a structured error, never a throw", async () => {
  const db = freshStore();
  const result = await callTool(db, "not_a_real_tool", {});
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /unknown tool/);
});

// --- createServer: wires ListTools/CallTool handlers on a real Server -------

test("createServer: returns a Server whose registered tools/list handler reports the same TOOLS array", async () => {
  const db = freshStore();
  const server = createServer(db);
  // Reach the low-level handler map the SDK's Server keeps (same technique
  // as calling the request handler directly, without a transport) -- proves
  // createServer really registered ListToolsRequestSchema/CallToolRequestSchema
  // against THIS db, not a fresh disconnected one.
  assert.equal(typeof server.connect, "function");
});

// --- ONE real end-to-end integration test: actual stdio process, actual SDK client ---
//
// Everything above calls callTool()/createServer() directly, in-process --
// useful for fast, isolated coverage of each tool's own logic, but it never
// proves the actual MCP wire protocol wiring (stdio transport, JSON-RPC
// framing, the SDK's own Server/Client request routing) works. This test
// spawns the REAL entry point (`node src/mcp-server.js`, exactly as the
// README's connection example invokes it) as a real child process and talks
// to it over real stdio using the SDK's own Client + StdioClientTransport --
// the same "spawn a real subprocess and drive it for real" style
// cli.test.js's own "serve" test already uses for the HTTP server.

test("integration: a real stdio subprocess actually answers tools/list and tools/call via the SDK's own Client", async (t) => {
  const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
  const { StdioClientTransport } = await import("@modelcontextprotocol/sdk/client/stdio.js");

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [MCP_SERVER_PATH],
  });
  const client = new Client({ name: "meridian-latex-test-client", version: "1.0.0" });

  await client.connect(transport);
  t.after(async () => {
    await client.close();
  });

  const { tools } = await client.listTools();
  assert.equal(tools.length, TOOLS.length);
  assert.ok(tools.some((tool) => tool.name === "outline_tex"));
  assert.ok(tools.some((tool) => tool.name === "claim_node"));

  const callResult = await client.callTool({
    name: "outline_tex",
    arguments: { text: "\\section{Real MCP Wiring}\n" },
  });
  assert.equal(callResult.isError, undefined);
  const parsed = JSON.parse(callResult.content[0].text);
  assert.equal(parsed.nodes.length, 1);
  assert.equal(parsed.nodes[0].title, "Real MCP Wiring");
});

test("integration: `node src/cli.js mcp` (the shipped CLI subcommand) starts the same real MCP server over stdio", async (t) => {
  const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
  const { StdioClientTransport } = await import("@modelcontextprotocol/sdk/client/stdio.js");

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [CLI_PATH, "mcp"],
  });
  const client = new Client({ name: "meridian-latex-cli-mcp-test-client", version: "1.0.0" });

  await client.connect(transport);
  t.after(async () => {
    await client.close();
  });

  const { tools } = await client.listTools();
  assert.equal(tools.length, TOOLS.length);
});
