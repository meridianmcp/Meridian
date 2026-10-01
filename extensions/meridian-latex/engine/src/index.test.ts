import { test } from "node:test";
import assert from "node:assert/strict";
import * as api from "./index.js";

// This is the package's npm "main" -- what `import "meridian-latex"`
// actually resolves to. Real bug found 2026-09-19 while creating this file:
// package.json declared "main": "src/index.js" but the file never existed
// at all (a bare MODULE_NOT_FOUND for anyone using the package
// programmatically rather than via its CLI). This test exists so that gap
// can never silently reopen -- if a re-export here breaks, or a module
// this imports throws on load, this test fails immediately rather than
// only being discovered by an actual downstream consumer.

test("index.js exports the full expected public API surface", () => {
  const expected = [
    "outlineText", "outlineFile", "extractOutline",
    "matchOutlines",
    "openStore", "getProject", "upsertProject", "ensureProjectRow", "WHOLE_DOCUMENT_LEASE_NODE_ID", "CLAIM_TTL_MINUTES",
    "claimNode", "leaseWholeDocument", "releaseClaims", "getLiveClaims", "identityConfidenceFor",
    "recordEdit", "listProvenance", "markSynced",
    "fetchAllTags", "findKeyTag", "lookupCitationKey",
    "connectToProject", "OverleafProjectSession", "OverleafOtError",
    "login", "status", "logout", "loadSavedCookie",
  ];
  for (const name of expected) {
    assert.ok(name in api, `expected export "${name}" is missing from index.js`);
  }
});

test("outlineText (re-exported) actually works, not just exists", () => {
  const nodes = api.outlineText("\\section{Intro}\n");
  assert.equal(nodes[0].kind, "heading");
  assert.equal(nodes[0].title, "Intro");
});

test("status (re-exported) actually works, not just exists", () => {
  const result = api.status();
  assert.equal(typeof result.loggedIn, "boolean");
});
