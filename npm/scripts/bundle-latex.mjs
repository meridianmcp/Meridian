#!/usr/bin/env node
// Copies the meridian-latex engine's BUILT output into npm/latex/ so it
// ships as part of the @meridianmcp/mcp published tarball, as a real
// subpath (`@meridianmcp/mcp/latex`) rather than a separate npm package.
//
// 2026-09-29 reconciliation: the engine finished its JS -> TS migration
// (standalone repo, batches 1-6), so hand-authored source now lives at
// extensions/meridian-latex/engine/src/**/*.ts -- raw .ts is not runnable
// Node ESM, so this script copies engine/dist (the tsc/esbuild-compiled
// output produced by the engine's own `npm run build`, per build.mjs) INTO
// npm/latex/ instead of copying src/ directly like it used to. Callers of
// this script (npm-publish.yml's CI job, a local prepack) must run the
// engine's build first -- see that workflow / package.json's own build
// step ordering. Hand-edited source of truth is still
// extensions/meridian-latex/engine/src -- this is a build step, not a
// move, specifically so that directory keeps its own git history, its own
// SYNC.md fast-lane to the standalone repo, and stays usable as its own
// isolated git-worktree for focused LaTeX-only iteration (see workspace
// decision 66089b95's successor). npm/latex/ itself is generated,
// gitignored, and rebuilt fresh by this script every time -- never edit
// npm/latex/ directly, edit extensions/meridian-latex/engine/src and run
// the engine's own `npm run build`.
//
// Runs automatically as this package's own "prepack" script (npm runs
// prepack before both `npm pack` and `npm publish`), so a real
// `npm publish` always ships fresh output with no separate build step for
// CI to remember -- AS LONG AS the engine's own dist/ is already fresh at
// that point (npm-publish.yml's job order handles this). Compiled test
// output (*.test.js, *.test.js.map, *.test.d.ts, *.test.d.ts.map) is
// excluded -- a published package should not ship its own test suite.
// data/ (the runtime SQLite path from store.js's DEFAULT_DB_PATH) is never
// part of engine/src (or its compiled dist/) to begin with, so there's
// nothing to exclude there.

import { readdirSync, statSync, mkdirSync, copyFileSync, rmSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_DIR = join(__dirname, "..", "..", "extensions", "meridian-latex", "engine", "dist");
const DEST_DIR = join(__dirname, "..", "latex");

function isCompiledTestArtifact(name) {
  return (
    name.endsWith(".test.js") ||
    name.endsWith(".test.js.map") ||
    name.endsWith(".test.d.ts") ||
    name.endsWith(".test.d.ts.map")
  );
}

function copyRecursive(srcDir, destDir) {
  mkdirSync(destDir, { recursive: true });
  for (const name of readdirSync(srcDir)) {
    const srcPath = join(srcDir, name);
    const stat = statSync(srcPath);
    if (stat.isDirectory()) {
      copyRecursive(srcPath, join(destDir, name));
      continue;
    }
    if (isCompiledTestArtifact(name)) continue; // never ship tests
    copyFileSync(srcPath, join(destDir, name));
  }
}

rmSync(DEST_DIR, { recursive: true, force: true });
copyRecursive(SOURCE_DIR, DEST_DIR);
console.log(`bundle-latex: copied ${SOURCE_DIR} -> ${DEST_DIR}`);
