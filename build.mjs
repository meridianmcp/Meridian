// build.mjs — bundle the dashboard's module scripts into one IIFE file.
// Entry imports each module for side effects; every module re-exposes its
// top-level symbols on window (see scripts/_esbuild_convert.py), so the
// existing global-function architecture (inline onclick handlers, cross-file
// `state`/helper references) keeps working after bundling.
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const OUTFILE = "meridian/static/dashboard.bundle.js";

await build({
  entryPoints: ["meridian/static/dashboard.ts"],
  bundle: true,
  format: "iife",
  outfile: OUTFILE,
  legalComments: "none",
  logLevel: "info",
  target: ["es2020"],
  // TypeScript + Preact foundation (0a88d328). Purely additive: the existing
  // .js modules import each other with explicit .js extensions, so resolution
  // and output stay byte-identical until the first .ts/.tsx file is imported.
  // These options only take effect for future TypeScript/Preact code — JSX in
  // .tsx files compiles to Preact's automatic runtime, and bare react/react-dom
  // imports (e.g. pulled in by zustand) alias to preact/compat.
  resolveExtensions: [".tsx", ".ts", ".jsx", ".js", ".json"],
  jsx: "automatic",
  jsxImportSource: "preact",
  alias: {
    react: "preact/compat",
    "react-dom": "preact/compat",
  },
});
console.log(`built ${OUTFILE}`);

// 9aba783f — content-hash cache-busting. Hash the freshly-built bundle and write
// it to asset-manifest.json. The server reads this hash and stamps it onto the
// bundle's ?v= token, so a deploy serves the new bundle even when no version /
// git SHA is available in prod (the old _ASSET_VERSION fallback). The hash only
// changes when the bundle bytes change, so unchanged deploys keep the cache warm.
const bundleHash = createHash("sha256").update(readFileSync(OUTFILE)).digest("hex").slice(0, 12);
writeFileSync(
  "meridian/static/asset-manifest.json",
  JSON.stringify({ bundle: "dashboard.bundle.js", bundle_hash: bundleHash }, null, 2) + "\n",
);
console.log(`asset-manifest.json: bundle_hash=${bundleHash}`);
