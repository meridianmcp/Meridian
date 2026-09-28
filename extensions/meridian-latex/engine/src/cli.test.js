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
      assert.equal(err.status, 1);
      assert.match(err.stderr, /usage: meridian-latex outline/);
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

test("an unknown command exits non-zero with the full usage text", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH, "not-a-real-command"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      assert.equal(err.status, 1);
      assert.match(err.stderr, /unknown command: not-a-real-command/);
      assert.match(err.stderr, /available commands|commands:/);
      return true;
    },
  );
});

test("no command at all prints usage and exits non-zero", () => {
  assert.throws(
    () => execFileSync(process.execPath, [CLI_PATH], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      assert.equal(err.status, 1);
      assert.match(err.stderr, /usage: meridian-latex/);
      return true;
    },
  );
});
