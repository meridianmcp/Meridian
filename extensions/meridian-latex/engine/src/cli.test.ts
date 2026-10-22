import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI_PATH = join(__dirname, "cli.js");

// Every test here spawns cli.js as a real subprocess -- this is the single
// user-facing front door npm publishes as the `meridian-latex` bin, so what
// matters is that IT actually works end-to-end, not that its constituent
// pieces (outlineFile, login/status/logout, server.js) do in isolation --
// those already have their own dedicated test files. See
// overleaf-login.test.js's own header comment for why a CLI entry point's
// behavior can only be genuinely verified by actually spawning it.

test("outline: parses a real .tex file and prints its outline as JSON", () => {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-cli-test-"));
  try {
    const texPath = join(dir, "test.tex");
    writeFileSync(texPath, "\\section{Intro}\nSee \\cite{alice2020}.\n");
    const output = execFileSync(process.execPath, [CLI_PATH, "outline", texPath], { encoding: "utf-8" });
    const nodes = JSON.parse(output);
    assert.equal(nodes.length, 2);
    assert.equal(nodes[0].kind, "heading");
    assert.equal(nodes[0].title, "Intro");
    assert.equal(nodes[1].kind, "citation");
    assert.equal(nodes[1].key, "alice2020");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("outline: missing path argument exits non-zero with a usage message", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "outline"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex outline/);
      return true;
    },
  );
});

test("status: runs and prints real JSON (uses the real ~/.meridian-latex -- read-only, safe)", () => {
  const output = execFileSync(process.execPath, [CLI_PATH, "status"], { encoding: "utf-8" });
  const parsed = JSON.parse(output);
  assert.equal(typeof parsed.loggedIn, "boolean");
});

test("serve: actually starts the HTTP server and responds to /health", async () => {
  const port = 18471 + Math.floor(Math.random() * 1000); // avoid clashing with a real running instance
  const { spawn } = await import("node:child_process");
  const proc = spawn(process.execPath, [CLI_PATH, "serve"], {
    env: { ...process.env, MERIDIAN_LATEX_PORT: String(port) },
  });
  try {
    // Poll for readiness rather than a fixed sleep -- the server logs a
    // line once it's actually listening, but polling the real endpoint is
    // the more direct signal that "serve" genuinely works end-to-end.
    let ok = false;
    for (let i = 0; i < 50 && !ok; i++) {
      await new Promise((r) => setTimeout(r, 100));
      try {
        const res = await fetch(`http://127.0.0.1:${port}/health`);
        ok = res.ok;
      } catch {
        // not up yet -- keep polling
      }
    }
    assert.equal(ok, true, "the serve subcommand must actually start a working HTTP server");
  } finally {
    proc.kill();
  }
});

test("ls: missing project-id argument exits non-zero with a usage message", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "ls"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex ls <project-id>/);
      return true;
    },
  );
});

test("write: missing arguments exits non-zero with a usage message naming doc-id-or-path", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "write", "proj1"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex write <project-id> <doc-id-or-path>/);
      return true;
    },
  );
});

test("pull: missing arguments exits non-zero with a usage message", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "pull", "proj1"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex pull <project-id> <doc-id-or-path>/);
      return true;
    },
  );
});

test("style-guide: no section-type prints the full 8-category structure as JSON", () => {
  const output = execFileSync(process.execPath, [CLI_PATH, "style-guide"], { encoding: "utf-8" });
  const parsed = JSON.parse(output);
  assert.equal(parsed.sectionTypes.length, 8);
  assert.ok(parsed.guides.abstract);
});

test("style-guide: a section-type argument filters to just that guide", () => {
  const output = execFileSync(process.execPath, [CLI_PATH, "style-guide", "related work"], { encoding: "utf-8" });
  const parsed = JSON.parse(output);
  assert.equal(parsed.sectionType, "related-work");
  assert.ok(Array.isArray(parsed.guide.moves));
});

test("style-guide: an unrecognized section-type exits non-zero with a clear error", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "style-guide", "not-a-real-type"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /unknown section-type/);
      return true;
    },
  );
});

test("style-check: heuristically checks a real .tex file's section text against its declared section-type", () => {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-cli-style-test-"));
  try {
    const texPath = join(dir, "abstract.tex");
    writeFileSync(
      texPath,
      "This problem has become increasingly important. However, existing approaches fail to scale. " +
        "In this paper, we propose a new method. We show that our results outperform prior work. " +
        "Our findings suggest broad applicability.\n",
    );
    const output = execFileSync(process.execPath, [CLI_PATH, "style-check", texPath, "abstract"], { encoding: "utf-8" });
    const parsed = JSON.parse(output);
    assert.equal(parsed.sectionType, "abstract");
    assert.equal(parsed.heuristic, true);
    assert.match(parsed.disclaimer, /structural heuristic/i);
    assert.deepEqual(parsed.movesMissing, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("style-check: missing arguments exits non-zero with a usage message", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "style-check"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex style-check <path\.tex> <section-type>/);
      return true;
    },
  );
});

test("style-lookup: disabled by default (no env var set) exits non-zero with a clear DISABLED error", () => {
  assert.throws(
    () =>
      execFileSync(process.execPath, [CLI_PATH, "style-lookup", "introduction", "transformers"], {
        encoding: "utf-8",
        stdio: "pipe",
        env: { ...process.env, MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING: "" },
      }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /disabled by default/);
      return true;
    },
  );
});

test("style-lookup: enabled via env var, but no research provider wired into a bare CLI process, fails closed with NO_PROVIDER", () => {
  assert.throws(
    () =>
      execFileSync(process.execPath, [CLI_PATH, "style-lookup", "introduction", "transformers"], {
        encoding: "utf-8",
        stdio: "pipe",
        env: { ...process.env, MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING: "1" },
      }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /no research-tool dependency is available/);
      return true;
    },
  );
});

test("style-lookup: missing arguments exits non-zero with a usage message", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "style-lookup", "introduction"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex style-lookup <section-type> <topic>/);
      return true;
    },
  );
});

test("mcp: actually starts the MCP server over stdio and answers tools/list + tools/call via the SDK's own Client", async (t) => {
  // Mirrors mcp-server.test.js's own "integration" test, but spawns THIS
  // subcommand (`node cli.js mcp`) rather than `node mcp-server.js` directly
  // -- what's under test here is cli.js's own dynamic-import-then-call-
  // runStdioServer() wiring, not mcp-server.js's tool logic (already covered
  // in mcp-server.test.js).
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
  assert.ok(tools.some((tool) => tool.name === "outline_tex"));
  assert.ok(tools.some((tool) => tool.name === "overleaf_login_status"));
  assert.ok(tools.some((tool) => tool.name === "list_citation_keys"));
  assert.ok(tools.some((tool) => tool.name === "snapshot_document"));
  assert.ok(tools.some((tool) => tool.name === "get_style_guide"));

  const callResult = await client.callTool({
    name: "outline_tex",
    arguments: { text: "\\section{CLI MCP Wiring}\n" },
  });
  assert.equal(callResult.isError, undefined);
  const content = callResult.content as Array<{ type: string; text: string }>;
  const parsed = JSON.parse(content[0].text);
  assert.equal(parsed.nodes.length, 1);
  assert.equal(parsed.nodes[0].title, "CLI MCP Wiring");
});

test("an unknown command exits non-zero with the full usage text", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "not-a-real-command"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /unknown command: not-a-real-command/);
      assert.match(e.stderr, /available commands|commands:/);
      return true;
    },
  );
});

test("no command at all prints usage and exits non-zero", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      const e = err as NodeJS.ErrnoException & { status: number; stderr: string };
      assert.equal(e.status, 1);
      assert.match(e.stderr, /usage: meridian-latex/);
      return true;
    },
  );
});
