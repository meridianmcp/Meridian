// build.mjs — bundle the dashboard's module scripts into one IIFE file.
// Entry imports each module for side effects; every module re-exposes its
// top-level symbols on window (see scripts/_esbuild_convert.py), so the
// existing global-function architecture (inline onclick handlers, cross-file
// `state`/helper references) keeps working after bundling.
import { build } from "esbuild";

await build({
  entryPoints: ["meridian/static/dashboard.js"],
  bundle: true,
  format: "iife",
  outfile: "meridian/static/dashboard.bundle.js",
  legalComments: "none",
  logLevel: "info",
  target: ["es2020"],
});
console.log("built meridian/static/dashboard.bundle.js");
