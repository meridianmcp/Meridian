#!/usr/bin/env node
// Copies the meridian-latex engine's source into npm/latex/ so it ships as
// part of the @meridianmcp/mcp published tarball, as a real subpath
// (`@meridianmcp/mcp/latex`) rather than a separate npm package.
//
// Source of truth stays at extensions/meridian-latex/engine/src -- this is
// a build step, not a move, specifically so that directory keeps its own
// git history, its own SYNC.md fast-lane to the standalone repo, and stays
// usable as its own isolated git-worktree for focused LaTeX-only iteration
// (see workspace decision 66089b95's successor). npm/latex/ itself is
// generated, gitignored, and rebuilt fresh by this script every time --
// never edit npm/latex/ directly, edit extensions/meridian-latex/engine/src.
//
// Runs automatically as this package's own "prepack" script (npm runs
// prepack before both `npm pack` and `npm publish`), so a real
// `npm publish` always ships fresh source with no separate build step for
// CI to remember. *.test.js files and the socketio09/ test files are
// excluded -- a published package should not ship its own test suite.
// data/ (the runtime SQLite path from store.js's DEFAULT_DB_PATH) is never
// part of engine/src to begin with, so there's nothing to exclude there.

import { readdirSync, statSync, mkdirSync, copyFileSync, rmSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_DIR = join(__dirname, "..", "..", "extensions", "meridian-latex", "engine", "src");
const DEST_DIR = join(__dirname, "..", "latex");

function copyRecursive(srcDir, destDir) {
  mkdirSync(destDir, { recursive: true });
  for (const name of readdirSync(srcDir)) {
    const srcPath = join(srcDir, name);
    const stat = statSync(srcPath);
    if (stat.isDirectory()) {
      copyRecursive(srcPath, join(destDir, name));
      continue;
    }
    if (name.endsWith(".test.js")) continue; // never ship tests
    copyFileSync(srcPath, join(destDir, name));
  }
}

rmSync(DEST_DIR, { recursive: true, force: true });
copyRecursive(SOURCE_DIR, DEST_DIR);
console.log(`bundle-latex: copied ${SOURCE_DIR} -> ${DEST_DIR}`);
