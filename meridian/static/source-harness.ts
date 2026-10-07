// Test support (not shipped: nothing in the dashboard import graph references it, so
// build.mjs never bundles it).
//
// dashboard.ts is one ~13,800-line legacy *script*: it has no exports and runs DOM
// wiring at module scope (`document.getElementById('new-project-btn')!.onclick = ...`),
// so it cannot be imported into a unit test. The behaviour that decides whether a view
// repaints live (handleWsEvent, the sprint mutation handlers, deleteTaskRow ...) lives in
// top-level function declarations inside it. This helper parses the REAL source with the
// TypeScript compiler, lifts the named declarations out, transpiles them and evaluates
// them with the free identifiers they call (api, toast, loadQueue, state, ...) supplied
// by the test. The code under test is therefore the shipped code, not a copy: change the
// function and the test notices; rename or move it and loadDashboardFunctions() throws a
// clear "not found" instead of silently testing nothing.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";

const parsed = new Map<string, ts.SourceFile>();

function sourceFileFor(relPath: string): ts.SourceFile {
  const abs = resolve(process.cwd(), relPath);
  let sf = parsed.get(abs);
  if (!sf) {
    sf = ts.createSourceFile(abs, readFileSync(abs, "utf8"), ts.ScriptTarget.ES2020, true, ts.ScriptKind.TS);
    parsed.set(abs, sf);
  }
  return sf;
}

/** Source text of top-level function declarations, keyed by name. */
export function topLevelFunctionSource(relPath: string, names: string[]): Record<string, string> {
  const sf = sourceFileFor(relPath);
  const wanted = new Set(names);
  const found: Record<string, string> = {};
  for (const stmt of sf.statements) {
    if (ts.isFunctionDeclaration(stmt) && stmt.name && wanted.has(stmt.name.text)) {
      found[stmt.name.text] = stmt.getText(sf);
    }
  }
  const missing = names.filter((n) => !(n in found));
  if (missing.length) {
    throw new Error(`source-harness: ${relPath} has no top-level function ${missing.join(", ")}`);
  }
  return found;
}

/**
 * Evaluate the named top-level functions of `relPath` and return them. `scope` supplies
 * every other identifier those functions reference; anything not supplied resolves to a
 * jsdom/node global, so a forgotten dependency fails loudly with a ReferenceError when
 * the code path is exercised.
 */
export function loadDashboardFunctions<N extends string>(
  relPath: string,
  names: N[],
  scope: Record<string, unknown> = {},
): Record<N, (...args: any[]) => any> {
  const sources = topLevelFunctionSource(relPath, names);
  const ts_code = names.map((n) => sources[n]).join("\n\n");
  const js = ts.transpileModule(ts_code, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None },
  }).outputText;
  const factory = new Function(...Object.keys(scope), `${js}\nreturn { ${names.join(", ")} };`);
  return factory(...Object.values(scope));
}
