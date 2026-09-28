// Public programmatic API -- the `main` entry point npm resolves for
// `import "meridian-latex"` (as opposed to running its CLI, `bin/cli.js`).
// Real bug found 2026-09-19 while preparing this package for npm
// publishing: package.json declared "main": "src/index.js", but this file
// never existed -- anyone importing the package programmatically (rather
// than shelling out to its CLI) would have hit a bare MODULE_NOT_FOUND.
//
// Re-exports each module's own genuinely public surface, not everything --
// e.g. socketio09/*'s packet/transport internals are an implementation
// detail of overleaf-ot-client.js, not something an external consumer
// should ever need to reach into directly.

export { outlineText, outlineFile, extractOutline } from "./outline.js";
export { matchOutlines } from "./matching.js";
export { openStore, getProject, upsertProject, ensureProjectRow, WHOLE_DOCUMENT_LEASE_NODE_ID, CLAIM_TTL_MINUTES } from "./store.js";
export { claimNode, leaseWholeDocument, releaseClaims, getLiveClaims, identityConfidenceFor } from "./claims.js";
export { recordEdit, listProvenance, markSynced } from "./provenance.js";
export { fetchAllTags, findKeyTag, lookupCitationKey } from "./zotero.js";
export { connectToProject, OverleafProjectSession, OverleafOtError } from "./overleaf-ot-client.js";
export { login, status, logout, loadSavedCookie } from "./overleaf-login.js";
