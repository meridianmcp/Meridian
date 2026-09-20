#!/usr/bin/env node
// Minimal post-bundle smoke check: does the freshly-copied npm/latex/
// actually import cleanly and export what index.js promises, and does one
// real call (outlineText on a trivial .tex string) actually work.
//
// This is NOT a substitute for the full functional test suite (186 tests)
// -- that suite lives, unchanged, at extensions/meridian-latex/engine/ and
// is what CI's own gating job runs directly against the original source.
// This script only proves the BUNDLING mechanism itself (file copy, import
// resolution, no syntax errors) produces working code, since it deliberately
// runs against ./latex/ -- the same output a real `npm pack`/`npm publish`
// ships -- not the original source tree.

const mod = await import("../latex/index.js");

const expectedExports = [
  "outlineText", "outlineFile", "extractOutline",
  "matchOutlines",
  "openStore", "getProject", "upsertProject", "ensureProjectRow", "WHOLE_DOCUMENT_LEASE_NODE_ID", "CLAIM_TTL_MINUTES",
  "claimNode", "leaseWholeDocument", "releaseClaims", "getLiveClaims", "identityConfidenceFor",
  "recordEdit", "listProvenance", "markSynced",
  "fetchAllTags", "findKeyTag", "lookupCitationKey",
  "connectToProject", "OverleafProjectSession", "OverleafOtError",
  "login", "status", "logout", "loadSavedCookie",
];

const missing = expectedExports.filter((name) => !(name in mod));
if (missing.length > 0) {
  console.error(`bundle smoke test FAILED: missing exports: ${missing.join(", ")}`);
  process.exit(1);
}

const nodes = mod.outlineText("\\section{Introduction}\n");
if (!Array.isArray(nodes) || nodes.length !== 1 || nodes[0].kind !== "heading") {
  console.error(`bundle smoke test FAILED: outlineText produced unexpected output: ${JSON.stringify(nodes)}`);
  process.exit(1);
}

console.log(`bundle smoke test OK: ${expectedExports.length} exports present, outlineText() works.`);
