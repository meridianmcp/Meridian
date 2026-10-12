import { test } from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI_PATH = join(__dirname, "cli.js");

// 189fde6b (RT-LEAD-003) — `write` changes a LIVE Overleaf document, so it must refuse
// to run unless a human is at an interactive terminal. spawnSync with an explicit
// `input` gives the child a piped (non-TTY) stdin, the same condition an agent's shell
// command produces, so the refusal must fire before any saved session is loaded.
test("write: refuses to edit a live document when stdin is not an interactive terminal", () => {
  const result = spawnSync(
    process.execPath,
    [CLI_PATH, "write", "proj1", "doc1", "node1", "title", "new text"],
    { encoding: "utf-8", input: "" },
  );
  assert.equal(result.status, 1);
  assert.match(result.stderr, /only runs from an interactive terminal/);
  assert.doesNotMatch(result.stderr, /No saved Overleaf session/);
});
