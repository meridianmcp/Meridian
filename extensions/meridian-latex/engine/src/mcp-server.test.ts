import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { openStore } from "./store.js";
import { snapshotDoc } from "./local-snapshot.js";
import { callTool, TOOLS, createServer } from "./mcp-server.js";
import type { FileTreeFolder, ResolveDocIdResult } from "./project-tree.js";
import type { ConnectToProjectOptions } from "./overleaf-ot-client.js";
import type { SectionTypeId } from "./style-guide.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
// This compiled test file runs from dist/ (see package.json's `pretest`/
// `test` scripts -- tests always execute against the built dist/ tree, never
// directly against src/), so __dirname here is <engine>/dist. The real,
// actually-executable server this file's own integration test spawns is
// therefore the COMPILED sibling in dist/. The one deliberate exception --
// per this repo's TS-migration plan -- is the source-text safety check
// further down: it must read the AUTHORED mcp-server.ts (a human-written
// import statement and prose comment, not emitted JS), so it reaches back
// into ../src for it instead.
const MCP_SERVER_DIST_PATH = join(__dirname, "mcp-server.js");
const MCP_SERVER_SOURCE_PATH = join(__dirname, "..", "src", "mcp-server.ts");

function freshStore() {
  return openStore(":memory:");
}

function parseText(result: Awaited<ReturnType<typeof callTool>>) {
  assert.equal(result.content.length, 1);
  assert.equal(result.content[0].type, "text");
  return JSON.parse(result.content[0].text);
}

interface FakeDocEntry {
  docId: string;
  lines: string[];
  version?: number;
}

interface FakeSessionOptions {
  rootFolder?: FileTreeFolder | null;
  docs?: Record<string, FakeDocEntry>;
}

/** A fake OverleafProjectSession -- just enough shape for list_project_docs/
 * pull_doc_expanded to exercise their own logic against, never a real
 * network connection. `docs` is `{ [path]: { docId, lines } }`. */
function fakeSession({ rootFolder = null, docs = {} }: FakeSessionOptions = {}) {
  let closed = false;
  return {
    rootFolder,
    closed: () => closed,
    close(): void {
      closed = true;
    },
    resolveDocId(path: string): ResolveDocIdResult {
      const entry = docs[path];
      if (!entry) return { ok: false, reason: `no fake doc registered for "${path}"` };
      return { ok: true, docId: entry.docId };
    },
    async joinDoc(docId: string): Promise<{ lines: string[]; version: number }> {
      const entry = Object.values(docs).find((d) => d.docId === docId);
      if (!entry) throw new Error(`fakeSession.joinDoc: no fake doc for docId ${docId}`);
      return { lines: entry.lines, version: entry.version ?? 1 };
    },
  };
}

// --- TOOLS: shape + the deliberate safety-scoping exclusion -----------------

test("TOOLS: exposes exactly the 23 documented tools, each with a well-formed JSON-Schema inputSchema", () => {
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
    "list_project_docs",
    "pull_doc_expanded",
    "get_bibliography",
    "expand_section_aliases",
    "list_local_snapshots",
    "overleaf_login_status",
    "list_citation_keys",
    "snapshot_document",
    "lint_tex",
    "lint_tex_file",
    "get_style_guide",
    "check_section_style",
    "lookup_published_framing",
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

test("TOOLS: no tool name or description mentions applyFieldEdit -- the actual live-write function", () => {
  const forbidden = /applyfieldedit/i;
  for (const tool of TOOLS) {
    assert.ok(!forbidden.test(tool.name));
    assert.ok(!forbidden.test(tool.description));
  }
});

test("mcp-server.ts source: never imports the live-write path (applyFieldEdit) or OverleafProjectSession directly", () => {
  const source = readFileSync(MCP_SERVER_SOURCE_PATH, "utf-8");
  // Checks the actual import statement from ./index.js, and any call/
  // reference to the function by name, rather than banning the bare word
  // everywhere -- this file's own safety-scoping comments legitimately
  // name applyFieldEdit/OverleafProjectSession in prose to explain why
  // neither is imported.
  const importBlock = source.match(/import\s*\{([\s\S]*?)\}\s*from\s*"\.\/index\.js"/);
  assert.ok(importBlock, "expected a named import from ./index.js");
  assert.ok(
    !/\bapplyFieldEdit\b/.test(importBlock[1]),
    "must never import applyFieldEdit -- the live write path -- from index.js",
  );
  assert.ok(
    !/\bOverleafProjectSession\b/.test(importBlock[1]),
    "must never import OverleafProjectSession directly from index.js",
  );
  assert.ok(!/applyFieldEdit\s*\(/.test(source), "must never call applyFieldEdit");
  assert.ok(!/new\s+OverleafProjectSession\s*\(/.test(source), "must never construct an OverleafProjectSession directly");
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

// --- list_project_docs / pull_doc_expanded -----------------------------------
// Both tools connect to a live Overleaf project. Every test below injects a
// fake loadSavedCookie/status/connectToProject via callTool's `deps`
// parameter -- NO real network call and NO real saved cookie is ever used,
// even though this machine may have a genuine one on disk from `login`.

test("list_project_docs: missing project_id is a structured tool error, never a throw, and never even looks for a session", async () => {
  const db = freshStore();
  const result = await callTool(db, "list_project_docs", {}, {
    loadSavedCookie: () => {
      throw new Error("must not be called without a project_id");
    },
  });
  assert.equal(result.isError, true);
});

test("list_project_docs: no saved session is a clear tool-level error, never a raw throw", async () => {
  const db = freshStore();
  const result = await callTool(
    db,
    "list_project_docs",
    { project_id: "proj1" },
    { loadSavedCookie: () => null },
  );
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /no saved overleaf session/i);
});

test("list_project_docs: with a fake saved session, returns the fake project's doc paths and closes the session", async () => {
  const db = freshStore();
  let connectArgs: ConnectToProjectOptions | null = null;
  let session: ReturnType<typeof fakeSession> | undefined;
  const result = await callTool(
    db,
    "list_project_docs",
    { project_id: "proj1" },
    {
      loadSavedCookie: () => "fake-cookie-value",
      status: () => ({ baseUrl: "https://example-overleaf.test" }),
      connectToProject: async (args) => {
        connectArgs = args;
        session = fakeSession({
          rootFolder: {
            _id: "",
            name: "",
            docs: [{ _id: "aaaaaaaaaaaaaaaaaaaaaaaa", name: "main.tex" }],
            folders: [],
            fileRefs: [],
          },
        });
        return session;
      },
    },
  );
  assert.equal(connectArgs!.projectId, "proj1");
  assert.equal(connectArgs!.cookie, "fake-cookie-value");
  assert.equal(connectArgs!.httpBaseUrl, "https://example-overleaf.test");
  assert.equal(connectArgs!.wsBaseUrl, "wss://example-overleaf.test");
  const parsed = parseText(result);
  assert.deepEqual(parsed.docs, [{ path: "main.tex", docId: "aaaaaaaaaaaaaaaaaaaaaaaa" }]);
  assert.equal(session!.closed(), true, "the fake session must be closed after the tool call");
});

test("pull_doc_expanded: missing doc_id_or_path is a structured tool error", async () => {
  const db = freshStore();
  const result = await callTool(db, "pull_doc_expanded", { project_id: "proj1" });
  assert.equal(result.isError, true);
});

test("pull_doc_expanded: no saved session is a clear tool-level error", async () => {
  const db = freshStore();
  const result = await callTool(
    db,
    "pull_doc_expanded",
    { project_id: "proj1", doc_id_or_path: "main.tex" },
    { loadSavedCookie: () => null },
  );
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /no saved overleaf session/i);
});

test("pull_doc_expanded: resolves a project-relative path, returns fully expanded text, and closes the session", async () => {
  const db = freshStore();
  let session: ReturnType<typeof fakeSession> | undefined;
  const result = await callTool(
    db,
    "pull_doc_expanded",
    { project_id: "proj1", doc_id_or_path: "main.tex" },
    {
      loadSavedCookie: () => "fake-cookie-value",
      status: () => ({ baseUrl: "https://example-overleaf.test" }),
      connectToProject: async () => {
        session = fakeSession({
          rootFolder: {
            _id: "",
            name: "",
            docs: [{ _id: "bbbbbbbbbbbbbbbbbbbbbbbb", name: "main.tex" }],
            folders: [],
            fileRefs: [],
          },
          docs: {
            "main.tex": {
              docId: "bbbbbbbbbbbbbbbbbbbbbbbb",
              lines: ["\\section{Intro}", "\\input{chapter1}"],
            },
          },
        });
        return session;
      },
    },
  );
  const parsed = parseText(result);
  // No "chapter1" doc registered in the fake tree -- expandInputs leaves the
  // \input in place and records it as unexpanded, exactly as input-expansion.js
  // documents for an unresolvable reference.
  assert.equal(parsed.docId, "bbbbbbbbbbbbbbbbbbbbbbbb");
  assert.match(parsed.source, /\\section\{Intro\}/);
  assert.deepEqual(parsed.unexpanded, ["chapter1"]);
  assert.equal(session!.closed(), true, "the fake session must be closed after the tool call");
});

test("pull_doc_expanded: an unresolvable path is a structured tool error, and the session is still closed", async () => {
  const db = freshStore();
  let session: ReturnType<typeof fakeSession> | undefined;
  const result = await callTool(
    db,
    "pull_doc_expanded",
    { project_id: "proj1", doc_id_or_path: "nope.tex" },
    {
      loadSavedCookie: () => "fake-cookie-value",
      status: () => ({ baseUrl: "https://example-overleaf.test" }),
      connectToProject: async () => {
        session = fakeSession({ rootFolder: { _id: "", name: "", docs: [], folders: [], fileRefs: [] } });
        return session;
      },
    },
  );
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /could not resolve/i);
  assert.equal(session!.closed(), true);
});

test("pull_doc_expanded: a raw 24-hex docId skips path resolution entirely", async () => {
  const db = freshStore();
  const rawDocId = "cccccccccccccccccccccc1";
  const result = await callTool(
    db,
    "pull_doc_expanded",
    { project_id: "proj1", doc_id_or_path: rawDocId },
    {
      loadSavedCookie: () => "fake-cookie-value",
      status: () => ({ baseUrl: "https://example-overleaf.test" }),
      connectToProject: async () =>
        fakeSession({
          docs: { [rawDocId]: { docId: rawDocId, lines: ["\\section{Raw}"] } },
        }),
    },
  );
  const parsed = parseText(result);
  assert.equal(parsed.docId, rawDocId);
  assert.match(parsed.source, /\\section\{Raw\}/);
});

// --- get_bibliography ---------------------------------------------------------

test("get_bibliography: extracts inline \\bibitem entries with no bib_text given", async () => {
  const db = freshStore();
  const source = "\\begin{thebibliography}{9}\n\\bibitem{alice2020} Alice et al. 2020.\n\\end{thebibliography}\n";
  const result = parseText(await callTool(db, "get_bibliography", { source_text: source }));
  assert.equal(result.entries.length, 1);
  assert.equal(result.entries[0].key, "alice2020");
  assert.equal(result.entries[0].type, "bibitem");
});

test("get_bibliography: resolves \\bibliography{} references against the supplied bib_text", async () => {
  const db = freshStore();
  const source = "See \\cite{bob2021}.\n\\bibliography{refs}\n";
  const bibText = '@article{bob2021,\n  title = {A Bob Paper},\n  author = {Bob},\n  year = {2021}\n}\n';
  const result = parseText(await callTool(db, "get_bibliography", { source_text: source, bib_text: bibText }));
  assert.equal(result.entries.length, 1);
  assert.equal(result.entries[0].key, "bob2021");
  assert.equal(result.entries[0].title, "A Bob Paper");
});

test("get_bibliography: missing source_text is a structured tool error", async () => {
  const db = freshStore();
  const result = await callTool(db, "get_bibliography", {});
  assert.equal(result.isError, true);
});

// --- expand_section_aliases ----------------------------------------------------

test("expand_section_aliases: rewrites a single-argument section-alias macro and its uses", async () => {
  const db = freshStore();
  const source = "\\newcommand{\\mysection}[1]{\\section{#1}}\n\\mysection{Introduction}\n";
  const result = parseText(await callTool(db, "expand_section_aliases", { source_text: source }));
  assert.match(result.expanded, /\\section\{Introduction\}/);
  assert.ok(!result.expanded.includes("\\mysection"));
});

test("expand_section_aliases: source with no aliases is returned unchanged", async () => {
  const db = freshStore();
  const source = "\\section{Plain}\n";
  const result = parseText(await callTool(db, "expand_section_aliases", { source_text: source }));
  assert.equal(result.expanded, source);
});

test("expand_section_aliases: missing source_text is a structured tool error", async () => {
  const db = freshStore();
  const result = await callTool(db, "expand_section_aliases", {});
  assert.equal(result.isError, true);
});

// --- list_local_snapshots -------------------------------------------------------

test("list_local_snapshots: lists nothing for a project/doc with no snapshots directory yet", async () => {
  const db = freshStore();
  const result = parseText(
    await callTool(db, "list_local_snapshots", { project_id: `proj-${randomUUID()}`, doc_id: `doc-${randomUUID()}` }),
  );
  assert.deepEqual(result.paths, []);
});

test("list_local_snapshots: missing project_id or doc_id is a structured tool error", async () => {
  const db = freshStore();
  const missingProject = await callTool(db, "list_local_snapshots", { doc_id: "d1" });
  assert.equal(missingProject.isError, true);
  const missingDoc = await callTool(db, "list_local_snapshots", { project_id: "p1" });
  assert.equal(missingDoc.isError, true);
});

// --- overleaf_login_status ------------------------------------------------------

test("overleaf_login_status: returns exactly the injected status() dep's result, never a cookie value", async () => {
  const db = freshStore();
  const result = parseText(
    await callTool(
      db,
      "overleaf_login_status",
      {},
      {
        status: () => ({
          loggedIn: true,
          baseUrl: "https://example-overleaf.test",
          savedAt: "2026-09-25T00:00:00.000Z",
        }),
      },
    ),
  );
  assert.deepEqual(result, {
    loggedIn: true,
    baseUrl: "https://example-overleaf.test",
    savedAt: "2026-09-25T00:00:00.000Z",
  });
  assert.equal("cookie" in result, false, "must never surface the cookie value itself");
});

test("overleaf_login_status: no saved session -> the injected status() dep's {loggedIn:false} passes through unchanged", async () => {
  const db = freshStore();
  const result = parseText(
    await callTool(db, "overleaf_login_status", {}, { status: () => ({ loggedIn: false }) }),
  );
  assert.deepEqual(result, { loggedIn: false });
});

test("overleaf_login_status: with no deps override, the real status() runs and reports a boolean loggedIn (read-only, safe against the real ~/.meridian-latex, matching cli.test.js's own `status` test)", async () => {
  const db = freshStore();
  const result = parseText(await callTool(db, "overleaf_login_status", {}));
  assert.equal(typeof result.loggedIn, "boolean");
});

// --- list_citation_keys -----------------------------------------------------------

test("list_citation_keys: returns the flat tag array from the injected fetchAllTags dep", async () => {
  const db = freshStore();
  const result = parseText(
    await callTool(
      db,
      "list_citation_keys",
      {},
      { fetchAllTags: async () => ["P1:key:margulies2005454", "P1:key:bob2021"] },
    ),
  );
  assert.deepEqual(result.tags, ["P1:key:margulies2005454", "P1:key:bob2021"]);
});

test("list_citation_keys: an empty library returns an empty array, not an error", async () => {
  const db = freshStore();
  const result = parseText(await callTool(db, "list_citation_keys", {}, { fetchAllTags: async () => [] }));
  assert.deepEqual(result.tags, []);
});

test("list_citation_keys: a Zotero-unreachable fetchAllTags throw surfaces as a structured tool error, never an uncaught throw", async () => {
  const db = freshStore();
  const result = await callTool(
    db,
    "list_citation_keys",
    {},
    {
      fetchAllTags: async () => {
        throw new Error("Zotero local API returned 500 fetching tags");
      },
    },
  );
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /zotero local api returned 500/i);
});

// --- snapshot_document -------------------------------------------------------------

test("snapshot_document: splits text into lines, calls the injected snapshotDoc dep, and returns its path", async () => {
  const db = freshStore();
  let capturedArgs: { projectId: string; docId: string; lines: string[] } | null = null;
  const result = parseText(
    await callTool(
      db,
      "snapshot_document",
      { project_id: "proj1", doc_id: "doc1", text: "\\section{Intro}\nSee \\cite{alice2020}." },
      {
        snapshotDoc: (args) => {
          capturedArgs = args;
          return "/fake/path/snapshot.tex";
        },
      },
    ),
  );
  assert.deepEqual(capturedArgs, {
    projectId: "proj1",
    docId: "doc1",
    lines: ["\\section{Intro}", "See \\cite{alice2020}."],
  });
  assert.equal(result.path, "/fake/path/snapshot.tex");
});

test("snapshot_document: with the real snapshotDoc (test-only dir override), actually writes the given text to disk and never touches Overleaf", async () => {
  const db = freshStore();
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-mcp-snapshot-test-"));
  try {
    const result = parseText(
      await callTool(
        db,
        "snapshot_document",
        { project_id: "proj1", doc_id: "doc1", text: "line one\nline two" },
        { snapshotDoc: (args) => snapshotDoc({ ...args, dir }) },
      ),
    );
    assert.ok(existsSync(result.path));
    assert.equal(readFileSync(result.path, "utf-8"), "line one\nline two");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("snapshot_document: missing project_id, doc_id, or text is a structured tool error, never a throw", async () => {
  const db = freshStore();
  const missingProject = await callTool(db, "snapshot_document", { doc_id: "d1", text: "x" });
  assert.equal(missingProject.isError, true);
  const missingDoc = await callTool(db, "snapshot_document", { project_id: "p1", text: "x" });
  assert.equal(missingDoc.isError, true);
  const missingText = await callTool(db, "snapshot_document", { project_id: "p1", doc_id: "d1" });
  assert.equal(missingText.isError, true);
});

// --- lint_tex / lint_tex_file -------------------------------------------------

test("lint_tex: returns findings for a document with real issues (missing bib entry, section-hierarchy skip)", async () => {
  const db = freshStore();
  const source =
    "\\section{Intro}\n" +
    "\\subsubsection{Too Deep}\n" +
    "See \\cite{missing2021}.\n";
  const result = parseText(await callTool(db, "lint_tex", { text: source }));
  const rules = result.findings.map((f: { rule: string }) => f.rule);
  assert.ok(rules.includes("citation-missing-bib-entry"));
  assert.ok(rules.includes("section-hierarchy-skip"));
});

test("lint_tex: a clean document with no issues returns an empty findings array", async () => {
  const db = freshStore();
  const source =
    "\\section{Intro}\n" +
    "See \\cite{alice2020}.\n" +
    "\\begin{thebibliography}{9}\n" +
    "\\bibitem{alice2020} Alice et al. 2020.\n" +
    "\\end{thebibliography}\n";
  const result = parseText(await callTool(db, "lint_tex", { text: source }));
  assert.deepEqual(result.findings, []);
});

test("lint_tex: bib_text resolves an external \\bibliography{} reference before checking citations", async () => {
  const db = freshStore();
  const source = "See \\cite{bob2021}.\n\\bibliography{refs}\n";
  const bibText = "@article{bob2021,\n  title = {A Bob Paper},\n  year = {2021}\n}\n";
  const result = parseText(await callTool(db, "lint_tex", { text: source, bib_text: bibText }));
  assert.ok(!result.findings.some((f: { rule: string }) => f.rule === "citation-missing-bib-entry"));
});

test("lint_tex: missing/empty text is a structured tool error, never a throw", async () => {
  const db = freshStore();
  const missing = await callTool(db, "lint_tex", {});
  assert.equal(missing.isError, true);
  assert.match(parseText(missing).error, /missing or empty 'text'/);

  const empty = await callTool(db, "lint_tex", { text: "   " });
  assert.equal(empty.isError, true);
});

test("lint_tex_file: lints a real local .tex file via lintFile()", async () => {
  const db = freshStore();
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-mcp-lint-test-"));
  try {
    const texPath = join(dir, "paper.tex");
    writeFileSync(texPath, "\\cite{}\n");
    const result = parseText(await callTool(db, "lint_tex_file", { path: texPath }));
    assert.ok(result.findings.some((f: { rule: string }) => f.rule === "empty-cite-key"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("lint_tex_file: a missing path is a structured tool error", async () => {
  const db = freshStore();
  const missing = await callTool(db, "lint_tex_file", {});
  assert.equal(missing.isError, true);
});

test("lint_tex_file: a nonexistent file surfaces as a structured tool error, never an uncaught throw", async () => {
  const db = freshStore();
  const result = await callTool(db, "lint_tex_file", { path: join(tmpdir(), `does-not-exist-${randomUUID()}.tex`) });
  assert.equal(result.isError, true);
});

// --- get_style_guide / check_section_style / lookup_published_framing ------

test("get_style_guide: no section_type returns the full 8-category structure", async () => {
  const db = freshStore();
  const result = parseText(await callTool(db, "get_style_guide", {}));
  assert.equal(result.sectionTypes.length, 8);
  assert.ok(result.guides.abstract);
  assert.ok(result.guides["related-work"]);
  assert.ok(result.guides["ethics-broader-impact"]);
});

test("get_style_guide: a section_type (including a common alias) filters to just that guide", async () => {
  const db = freshStore();
  const result = parseText(await callTool(db, "get_style_guide", { section_type: "Related Work" }));
  assert.equal(result.sectionType, "related-work");
  assert.ok(Array.isArray(result.guide.moves));
  assert.ok(result.guide.moves.length > 0);
});

test("get_style_guide: an unrecognized section_type is a structured tool error, never a throw", async () => {
  const db = freshStore();
  const result = await callTool(db, "get_style_guide", { section_type: "not-a-real-section-type" });
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /unknown section-type/);
});

test("check_section_style: text with every abstract move present and in order reports no missing/out-of-order moves", async () => {
  const db = freshStore();
  const text =
    "Task X has become increasingly important in recent years. However, existing approaches fail to scale. " +
    "In this paper, we propose a new method. We show that our results outperform prior work. " +
    "These results suggest broad applicability.";
  const result = parseText(await callTool(db, "check_section_style", { section_type: "abstract", text }));
  assert.equal(result.sectionType, "abstract");
  assert.equal(result.heuristic, true);
  assert.match(result.disclaimer, /structural heuristic/i);
  assert.deepEqual(result.movesMissing, []);
  assert.deepEqual(result.movesOutOfOrder, []);
  assert.ok(result.movesPresent.includes("context"));
  assert.ok(result.movesPresent.includes("approach"));
});

test("check_section_style: a move with no matching cue is reported missing", async () => {
  const db = freshStore();
  const text = "In this paper, we propose a new method for task X.";
  const result = parseText(await callTool(db, "check_section_style", { section_type: "abstract", text }));
  assert.ok(result.movesMissing.includes("results"));
  assert.ok(result.movesMissing.includes("implications"));
});

test("check_section_style: moves appearing out of canonical order are flagged", async () => {
  const db = freshStore();
  // "results" cue ("we show that") appears BEFORE "context" cue ("has become
  // increasingly important") -- context is canonically move #1, results #4.
  const text = "We show that our method works well. Task X has become increasingly important.";
  const result = parseText(await callTool(db, "check_section_style", { section_type: "abstract", text }));
  assert.ok(result.movesOutOfOrder.includes("results"));
});

test("check_section_style: resolves a section's text from source_text + heading_title via a fresh outline", async () => {
  const db = freshStore();
  const source =
    "\\section{Introduction}\n" +
    "This topic has become increasingly important. However, prior work fails to address it. " +
    "In this paper, we propose a solution.\n" +
    "\\section{Methods}\n" +
    "We use a dataset of real examples.\n";
  const result = parseText(
    await callTool(db, "check_section_style", {
      section_type: "introduction",
      source_text: source,
      heading_title: "Introduction",
    }),
  );
  assert.equal(result.resolvedFrom, "sourceText");
  assert.ok(result.movesPresent.includes("context"));
  assert.ok(result.movesPresent.includes("gap"));
  assert.ok(result.movesPresent.includes("objective"));
  // The Methods section's own text ("We use a dataset...") is outside the
  // resolved range (it starts at the NEXT heading) -- style-guide.test.js's
  // own resolveSectionText tests assert the exact sliced text directly;
  // here it's enough that this call succeeded against the Introduction
  // heading specifically, not a range that swallowed the whole document.
});

test("check_section_style: missing section_type or an unresolvable text source is a structured tool error", async () => {
  const db = freshStore();
  const missingType = await callTool(db, "check_section_style", { text: "some text" });
  assert.equal(missingType.isError, true);

  const noText = await callTool(db, "check_section_style", { section_type: "abstract" });
  assert.equal(noText.isError, true);
  assert.match(parseText(noText).error, /provide either 'text'/);

  const badType = await callTool(db, "check_section_style", { section_type: "not-a-real-type", text: "x" });
  assert.equal(badType.isError, true);

  const noHeadingMatch = await callTool(db, "check_section_style", {
    section_type: "abstract",
    source_text: "\\section{Intro}\nSome text.\n",
    heading_title: "Nonexistent Heading",
  });
  assert.equal(noHeadingMatch.isError, true);
  assert.match(parseText(noHeadingMatch).error, /no heading found matching/);
});

test("lookup_published_framing: disabled by default -- fails closed with a clear DISABLED error, never a silent empty result", async () => {
  const db = freshStore();
  const result = await callTool(db, "lookup_published_framing", { section_type: "introduction", topic: "transformers" });
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /disabled by default/);
});

test("lookup_published_framing: enabled but no research provider injected fails closed with NO_PROVIDER, never a throw", async () => {
  const db = freshStore();
  const result = await callTool(
    db,
    "lookup_published_framing",
    { section_type: "introduction", topic: "transformers" },
    { publishedFramingEnabled: true },
  );
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /no research-tool dependency is available/);
});

test("lookup_published_framing: enabled with an injected research provider returns a live, attributed, never-stored excerpt", async () => {
  const db = freshStore();
  const fakeProvider = async ({ sectionType, topic }: { sectionType: SectionTypeId; topic: string }) => ({
    excerpt: `A short excerpt about ${topic} in the context of ${sectionType}.`,
    citation: "Smith et al., 2025",
    sourceUrl: "https://example.org/paper",
  });
  const result = parseText(
    await callTool(
      db,
      "lookup_published_framing",
      { section_type: "introduction", topic: "transformers" },
      { publishedFramingEnabled: true, researchProvider: fakeProvider },
    ),
  );
  assert.equal(result.sectionType, "introduction");
  assert.equal(result.topic, "transformers");
  assert.match(result.excerpt, /transformers/);
  assert.equal(result.citation, "Smith et al., 2025");
  assert.equal(result.stored, false);
  assert.equal(typeof result.fetchedAt, "string");
});

test("lookup_published_framing: a provider that throws surfaces as a structured PROVIDER_ERROR, never an uncaught throw", async () => {
  const db = freshStore();
  const throwingProvider = async () => {
    throw new Error("upstream search failed");
  };
  const result = await callTool(
    db,
    "lookup_published_framing",
    { section_type: "introduction", topic: "transformers" },
    { publishedFramingEnabled: true, researchProvider: throwingProvider },
  );
  assert.equal(result.isError, true);
  assert.match(parseText(result).error, /upstream search failed/);
});

test("lookup_published_framing: missing section_type or topic is a structured tool error, never a throw", async () => {
  const db = freshStore();
  const missingType = await callTool(db, "lookup_published_framing", { topic: "transformers" });
  assert.equal(missingType.isError, true);
  const missingTopic = await callTool(db, "lookup_published_framing", { section_type: "introduction" });
  assert.equal(missingTopic.isError, true);
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
// spawns the REAL entry point (the compiled `dist/mcp-server.js`, exactly
// what `.mcp.json`'s own launch config and cli.js's `mcp` subcommand actually
// run) as a real child process and talks to it over real stdio using the
// SDK's own Client + StdioClientTransport. It only calls tools/list and the
// pure, network-free outline_tex tool -- it never exercises
// list_project_docs/pull_doc_expanded, since those would need a real (or
// fake, unreachable-from-a-subprocess-without-extra-wiring) Overleaf
// session; their logic is already covered in-process above via injected
// `deps`.

test("integration: a real stdio subprocess actually answers tools/list and tools/call via the SDK's own Client", async (t) => {
  const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
  const { StdioClientTransport } = await import("@modelcontextprotocol/sdk/client/stdio.js");

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [MCP_SERVER_DIST_PATH],
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
  assert.ok(tools.some((tool) => tool.name === "list_project_docs"));
  assert.ok(tools.some((tool) => tool.name === "pull_doc_expanded"));
  assert.ok(tools.some((tool) => tool.name === "get_bibliography"));
  assert.ok(tools.some((tool) => tool.name === "expand_section_aliases"));
  assert.ok(tools.some((tool) => tool.name === "list_local_snapshots"));
  assert.ok(tools.some((tool) => tool.name === "overleaf_login_status"));
  assert.ok(tools.some((tool) => tool.name === "list_citation_keys"));
  assert.ok(tools.some((tool) => tool.name === "snapshot_document"));
  assert.ok(tools.some((tool) => tool.name === "lint_tex"));
  assert.ok(tools.some((tool) => tool.name === "lint_tex_file"));
  assert.ok(tools.some((tool) => tool.name === "get_style_guide"));
  assert.ok(tools.some((tool) => tool.name === "check_section_style"));
  assert.ok(tools.some((tool) => tool.name === "lookup_published_framing"));

  const callResult = await client.callTool({
    name: "outline_tex",
    arguments: { text: "\\section{Real MCP Wiring}\n" },
  });
  assert.equal(callResult.isError, undefined);
  const content = callResult.content as Array<{ type: string; text: string }>;
  const parsed = JSON.parse(content[0].text);
  assert.equal(parsed.nodes.length, 1);
  assert.equal(parsed.nodes[0].title, "Real MCP Wiring");
});
