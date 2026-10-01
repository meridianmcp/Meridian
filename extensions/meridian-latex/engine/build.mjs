#!/usr/bin/env node
// Compiles every src/**/*.{js,ts} file into the matching dist/**/*.js, one
// esbuild call, bundle:false (per-file transpile, not a single bundle) --
// see the TypeScript-migration plan's toolchain-foundation batch for why:
// this is a multi-file Node package with several independent entry points
// (index/cli/server/mcp-server) and a native-binary dependency
// (better-sqlite3) that must stay an external, separately-resolved module,
// unlike the dashboard's one-bundle browser <script> tag.
//
// allowJs:true / checkJs:false (see tsconfig.json) means an untouched .js
// file flows through this SAME esbuild call unchanged into dist/ alongside
// freshly-converted .ts files -- there is no separate "JS passthrough" step
// and no dual-mode import resolution to maintain during the file-by-file
// migration. tsc itself is never invoked here: it runs separately, --noEmit
// only, as the strict type-check gate (see tsconfig.json / README).
//
// esbuild preserves a `#!/usr/bin/env node` shebang on any entry point that
// has one (cli.js, server.js, mcp-server.js), so `dist/cli.js` etc. stay
// directly executable post-build with no extra config.

import { build } from "esbuild";
import { readdirSync, statSync } from "node:fs";
import { join, relative, extname } from "node:path";
import { fileURLToPath } from "node:url";

const engineRoot = fileURLToPath(new URL(".", import.meta.url));
const srcDir = join(engineRoot, "src");

/**
 * Recursively collects every `.js`/`.ts` file under `dir` (skipping any
 * `.d.ts` declaration file, so a future hand-written ambient shim doesn't
 * get fed to esbuild as if it were emittable source).
 *
 * This is a plain directory walk, not a glob call or a new dependency:
 * Node's own `fs.glob`/`fs.globSync` is 22+-only (behind
 * `--experimental-*` before that), and this script must also run under the
 * CI/`engines`-pinned Node 20 floor (see package.json), so it cannot rely
 * on either.
 */
function collectEntryPoints(dir) {
  const found = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      found.push(...collectEntryPoints(full));
      continue;
    }
    const ext = extname(name);
    if (ext !== ".js" && ext !== ".ts") continue;
    if (name.endsWith(".d.ts")) continue;
    found.push(full);
  }
  return found;
}

const entryPoints = collectEntryPoints(srcDir).map((absPath) => relative(engineRoot, absPath));

await build({
  entryPoints,
  outbase: "src",
  outdir: "dist",
  bundle: false,
  platform: "node",
  format: "esm",
  target: "node20",
  sourcemap: true,
  logLevel: "info",
});
