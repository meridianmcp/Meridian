/**
 * Pre-deploy JS bundle smoke test.
 * Catches esbuild IIFE scope bugs before they reach prod.
 *
 * Run: node scripts/smoke_bundle.mjs
 * Returns exit code 1 if any check fails.
 */
import { readFileSync } from "fs";
import { createContext, runInContext } from "vm";

const BUNDLE = "meridian/static/dashboard.bundle.js";
const src = readFileSync(BUNDLE, "utf8");

let errors = [];
let warnings = [];

// ── 1. Parse check ────────────────────────────────────────────────────────────
try {
  new Function(src); // syntax check only
  console.log("✓ Bundle parses as valid JS");
} catch (e) {
  errors.push(`Bundle syntax error: ${e.message}`);
}

// ── 2. Bare `state.` reference check (must use window.state in modules) ───────
// The bundle is an IIFE; extracted modules run in a sub-scope and must not
// reference `state` as a bare identifier (they can't see dashboard.js scope).
// They should use window.state instead.
const moduleBlocks = src.match(/\/\/ meridian\/static\/dashboard-[^\n]+\n([\s\S]*?)(?=\/\/ meridian\/static\/dashboard-|\n\s*\/\/ dashboard\.js|\(\) =>\s*\{|$)/g) || [];
for (const block of moduleBlocks) {
  const headerMatch = block.match(/\/\/ meridian\/static\/(dashboard-[^\n]+)/);
  const file = headerMatch ? headerMatch[1] : "unknown";
  // Match bare `state.` NOT preceded by `window.`
  const bareRefs = [...block.matchAll(/(?<!window\.)(?<!\w)state\.(panels|projects|tabs|activeTab|serverConfig|apiKeyConfigured|activeWorkspaceTenantId)/g)];
  if (bareRefs.length > 0) {
    errors.push(`${file}: ${bareRefs.length} bare state.* reference(s) — use window.state instead`);
  }
}
if (errors.filter(e => e.includes("bare state")).length === 0) {
  console.log("✓ No bare state.* references in extracted modules");
}

// ── 3. window.state assignment exists ─────────────────────────────────────────
if (src.includes("window.state = state")) {
  console.log("✓ window.state = state assignment present");
} else {
  errors.push("Missing: window.state = state (needed for IIFE scope isolation)");
}

// ── 4. Key functions present ───────────────────────────────────────────────────
const required = ["loadProjects", "restoreTabs", "init", "isDemoMode", "isHostedMode", "getPanelState"];
for (const fn of required) {
  if (src.includes(fn)) {
    console.log(`✓ ${fn} present`);
  } else {
    errors.push(`Missing function: ${fn}`);
  }
}

// ── 5. Bundle size sanity ──────────────────────────────────────────────────────
const kb = src.length / 1024;
if (kb < 200) {
  errors.push(`Bundle too small: ${kb.toFixed(0)}kb — likely missing modules`);
} else if (kb > 2000) {
  warnings.push(`Bundle large: ${kb.toFixed(0)}kb — check for accidental duplication`);
} else {
  console.log(`✓ Bundle size: ${kb.toFixed(0)}kb`);
}

// ── 6. No duplicate IIFE wrapping ─────────────────────────────────────────────
const iifeCount = (src.match(/^\(\(\) => \{/gm) || []).length;
if (iifeCount > 1) {
  errors.push(`Multiple IIFE wrappers (${iifeCount}) — possible double-bundle`);
} else {
  console.log(`✓ Single IIFE wrapper`);
}

// ── Report ─────────────────────────────────────────────────────────────────────
console.log("");
if (warnings.length) {
  console.warn("WARNINGS:");
  warnings.forEach(w => console.warn(`  ⚠ ${w}`));
}
if (errors.length) {
  console.error("ERRORS:");
  errors.forEach(e => console.error(`  ✗ ${e}`));
  console.error(`\n${errors.length} error(s) — bundle is NOT safe to deploy`);
  process.exit(1);
} else {
  console.log(`Bundle smoke test PASSED ✓`);
  process.exit(0);
}
