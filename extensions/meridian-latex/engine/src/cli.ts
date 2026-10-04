#!/usr/bin/env node
// Unified CLI entry point -- the `bin` npm publishes as `meridian-latex`.
// Each subcommand delegates to an already-existing, already-tested module
// rather than duplicating logic here; this file is just the single,
// user-facing front door (`npx meridian-latex <command>`) instead of
// requiring someone who installed the npm package to know that `serve`
// lives in server.js and `login` lives in overleaf-login.js.
import { outlineFile, outlineText } from "./outline.js";
import { lintFile } from "./lint.js";
import { getStyleGuide, checkSectionStyle, lookupPublishedFraming, ENABLE_PUBLISHED_FRAMING_ENV_VAR } from "./style-guide.js";
import { login, status, logout, loadSavedCookie } from "./overleaf-login.js";
import { connectToProject } from "./overleaf-ot-client.js";
import { applyFieldEdit, type FieldName } from "./write.js";
import { type OutlineNodeLike } from "./range-locate.js";
import { listDocPaths } from "./project-tree.js";
import { joinDocExpanded } from "./input-expansion.js";
import { snapshotDoc } from "./local-snapshot.js";
import { compileLocalLatex, type LatexEngine } from "./overleaf-workflow.js";
import { openStore } from "./store.js";
import { writeFileSync, readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

/** Overleaf's real-time layer uses raw Mongo ObjectIds (24 lowercase hex
 * chars) for doc ids -- see services/real-time/app/js/Router.js's own
 * `zz.objectId()` validation on `joinDoc`'s `doc_id` argument. Used to tell
 * "the user already has a raw docId" apart from "the user typed a project-
 * relative path" without requiring a separate flag. */
const OBJECT_ID_RE = /^[0-9a-f]{24}$/;

const [, , command, ...rest] = process.argv;

const USAGE = `usage: meridian-latex <command> [args]

commands:
  outline <path.tex>   parse a .tex file and print its structural outline as JSON
  lint <path.tex>       run the static-AST lint suite (lint.js) against a .tex
                        file and print its findings as JSON -- 11 checks
                        covering citations, refs/labels, section hierarchy,
                        bibliography hygiene, empty captions/labels, empty
                        cite keys, stray TODO/FIXME/XXX markers, and
                        unresolved nested sub-labels. Never throws: a parse
                        failure itself becomes a single finding, printed the
                        same as any other.
  compile <path.tex> [--engine=pdflatex|xelatex|lualatex] [--project-id=<id>]
                        compile a local source tree with shell escape disabled;
                        hash its TeX/BibTeX inputs and PDF/log into a local
                        receipt. BibTeX and Biber runs follow their source syntax.
                        A receipt never attests that local changes were synced to
                        Overleaf. Output and receipts stay in ~/.meridian-latex.
  serve                start the local engine server (http://127.0.0.1:8471) --
                        this is what the Chrome extension's popup talks to
  login                open a dedicated browser window to capture your Overleaf
                        session (human-only -- never run this from an agent session)
  status               show whether a saved Overleaf session exists
  logout               remove the saved Overleaf session
  ls <project-id>       list every doc's project-relative path (and its
                        underlying docId), resolved from a live
                        joinProjectResponse -- use this to find the
                        <doc-id-or-path> a \`write\` call needs
  write <project-id> <doc-id-or-path> <node-id> <field> <new-text>
                        edit one outline node's field (title|key|caption|label)
                        directly over Overleaf's real-time OT protocol -- no
                        browser tab needed. Requires a saved session (\`login\`
                        first). <doc-id-or-path> is either a raw 24-hex-char
                        docId or a project-relative path like
                        "chapters/intro.tex" (resolved against the project's
                        own file tree -- see project-tree.js). <node-id> comes
                        from a fresh outline of the SAME doc (this command
                        re-outlines it itself before editing, so a stale id
                        from an old \`outline\` run is still safely rejected by
                        write.js's own range checks, never silently
                        mis-applied).
  pull <project-id> <doc-id-or-path> [output-path]
                        pull a doc's fully-expanded text (every \\input/
                        \\include spliced in) to a local file -- one-way only,
                        never synced back to Overleaf. Omit [output-path] to
                        write under ~/.meridian-latex/snapshots instead.
  mcp                   start the MCP (Model Context Protocol) server on stdio,
                        exposing this engine's outline/lint/claim/provenance/
                        citation/snapshot/style-guide tools to an AI agent session
                        (Claude Code, etc.) -- see mcp-server.js's own header comment
                        for the full tool list and its safety scoping (no tool here
                        can ever dispatch a live write into a real Overleaf doc).
  style-guide [section-type]
                        print the static, originally-written reference of rhetorical
                        move sequences per section-type (Abstract, Introduction,
                        Related Work, Methods, Results, Limitations,
                        Discussion/Conclusion, Ethics/Broader Impact) as JSON. Omit
                        [section-type] for the full structure. No network dependency.
  style-check <path.tex> <section-type>
                        heuristically compare a .tex file's text against
                        style-guide's expected move sequence for <section-type>,
                        reporting moves present/missing/out-of-order. This is a
                        STRUCTURAL HEURISTIC based on keyword/cue matching, NOT a
                        ground-truth or compiler-verified check -- see the printed
                        result's own "disclaimer" field.
  style-lookup <section-type> <topic>
                        STRETCH, OFF BY DEFAULT. Fetch a short, attributed excerpt
                        from a real published paper about <topic>, framed for
                        <section-type> -- never cached or bundled, an ephemeral
                        response only. Gated behind the ${ENABLE_PUBLISHED_FRAMING_ENV_VAR}
                        environment variable (set to "1" to enable); even then, this
                        standalone CLI process has no research/paper-search
                        capability of its own wired in, so it fails closed with a
                        clear NO_PROVIDER error unless a host embedding this engine
                        injects one programmatically (see style-guide.js's own header
                        comment on lookupPublishedFraming).`;

interface WriteFieldArgs {
  projectId: string;
  docId: string;
  nodeId: string;
  fieldName: string;
  newText: string;
}

/**
 * The `write` subcommand's I/O glue: load the saved cookie, connect,
 * resolve `docIdOrPath` to a real docId (a path is resolved against the
 * live joinProjectResponse file tree -- see project-tree.js's header for
 * the confirmed rootFolder/docs/folders/fileRefs shape this now relies on,
 * closing the gap this comment used to flag as unsolved), re-derive a FRESH
 * outline of the live doc (never trust a stale node list for a live
 * document -- same discipline joinDoc() itself already applies), find the
 * requested node, and delegate the actual edit to write.js's already-tested
 * `applyFieldEdit`. Opens the real local claims store (store.js's
 * `openStore()`, same default on-disk path server.js's `/claim` etc. routes
 * use) and passes a fresh per-invocation holder_token through, so a CLI
 * write is claim-aware exactly like the browser extension's popup flow --
 * two writers (another CLI invocation, a parallel agent, or a human in the
 * extension) racing the same node get real coordination instead of a silent
 * clobber. Always closes the session (and the claims db) on the way out,
 * success or failure -- this is a one-shot CLI invocation, not a
 * long-lived process.
 */
async function writeField({ projectId, docId: docIdOrPath, nodeId, fieldName, newText }: WriteFieldArgs): Promise<void> {
  const cookie = loadSavedCookie();
  if (!cookie) {
    console.error("No saved Overleaf session -- run `meridian-latex login` first.");
    process.exit(1);
  }
  const httpBaseUrl = (status() as { baseUrl?: string }).baseUrl || "https://www.overleaf.com";
  const wsBaseUrl = httpBaseUrl.replace(/^http/, "ws");

  const session = await connectToProject({ projectId, httpBaseUrl, wsBaseUrl, cookie });
  const claimsDb = openStore();
  // A short, per-invocation random id -- same generation style as
  // claims.js's own row ids (node:crypto's randomUUID), not a new
  // convention: this CLI process is a single one-shot holder for the
  // duration of exactly one write, so a UUID is a plausible, sufficiently-
  // unique holder_token with no format requirement to match (unlike
  // overleaf-ot-client.js's generateTcIdSeed, which is shaped specifically
  // for Overleaf's own RangesTracker id-seed format and isn't a fit here).
  const holderToken = `cli-${randomUUID()}`;
  try {
    let docId = docIdOrPath;
    if (!OBJECT_ID_RE.test(docIdOrPath)) {
      const resolved = session.resolveDocId(docIdOrPath);
      if (!resolved.ok) {
        console.error(`Could not resolve "${docIdOrPath}" to a doc: ${resolved.reason}`);
        process.exit(1);
      }
      docId = resolved.docId;
    }
    const { lines } = await session.joinDoc(docId);
    // outlineText() returns outline.ts's own precise OutlineNode[]; write.ts's
    // applyFieldEdit expects the looser, index-signature-bearing
    // OutlineNodeLike[] range-locate.ts declares for testability --
    // structurally compatible field-for-field, just missing that index
    // signature, so a direct cast is safe here (same convention write.ts's
    // own reconciliation path already uses).
    const nodes = outlineText(lines.join("\n")) as OutlineNodeLike[];
    const target = nodes.find((n) => n.id === nodeId);
    if (!target) {
      console.error(
        `Node "${nodeId}" not found in a fresh outline of doc ${docId} (${nodes.length} nodes found). ` +
          "It may have been edited/removed since you last looked, or belongs to a different doc.",
      );
      process.exit(1);
    }
    // fieldName arrives as a raw CLI argument -- never statically validated
    // against FieldName's four literal values, exactly as before this file
    // was typed: an invalid value is still safely rejected at runtime by
    // write.ts's own fieldLineNumber()/computeFieldEditOps() range checks
    // (a node genuinely has no such field), not by this cast.
    const result = await applyFieldEdit(session, docId, nodes, target, fieldName as FieldName, newText, false, undefined, {
      db: claimsDb,
      holder_token: holderToken,
    });
    if (!result.ok) {
      // Checked in this order (reconciled first) rather than the "reason"
      // check first: ApplyFieldEditResult's reconciled:true branch never
      // declares a `reason` field at all (it's a disjoint branch from the
      // pre-write-locate-failure one below), so narrowing on `reconciled`
      // first is what lets TS see `.reason`/`.claimedBy`/`.claimReason`
      // below as real properties of the remaining branch -- same net
      // routing as checking `reason` first, since the two conditions can
      // never both match the same branch.
      if (result.reconciled) {
        // A submission failure (timeout/disconnect/etc.) that reconciliation
        // resolved to something OTHER than "applied" -- see write.js's own
        // applyFieldEdit doc comment for the full contract. Never a bare
        // "undefined" reason (result.reason is a different, unrelated field
        // used only by the pre-write locate-failure path below).
        console.error(
          `Edit did not land (reconciled: ${result.reconcileStatus}): ${result.reconcileReason || "(no further detail)"}\n` +
            `Original failure: ${result.originalError}`,
        );
      } else if (result.reason === "node already claimed") {
        console.error(
          `Edit rejected: this node is already claimed${result.claimedBy ? ` by "${result.claimedBy}"` : ""}` +
            ` (${result.claimReason || "node already claimed"}). Wait for the other holder to finish, or check` +
            " GET /claims on the running `meridian-latex serve` instance to see who holds it.",
        );
      } else {
        console.error(`Edit rejected: ${result.reason}`);
      }
      process.exit(1);
    }
    if (result.reconciled) {
      console.error(
        `Note: the write's own confirmation failed (${result.originalError}), but a fresh check confirmed the edit DID land.`,
      );
    }
    console.log(JSON.stringify(result, null, 2));
  } finally {
    session.close();
    claimsDb.close();
  }
}

interface PullDocArgs {
  projectId: string;
  docId: string;
  outputPath?: string;
}

/**
 * The `pull` subcommand's I/O glue: connect, resolve `docIdOrPath` (same
 * dual raw-id/path handling as `writeField`), pull the doc's FULLY
 * EXPANDED text (every `\input`/`\include` spliced in via
 * input-expansion.js's `joinDocExpanded` -- a real multi-file paper is
 * incomplete as just its root doc's own text), and write it to a local
 * file: ONE-DIRECTIONAL only (Overleaf -> local), never read back by this
 * engine and never synced back to Overleaf -- matching local-snapshot.js's
 * own "never synced back" contract, reused here via `snapshotDoc` rather
 * than a second, parallel file-writing implementation. This is the "local,
 * linked copy" Adam asked for so meridian-outputs (or any other tool) has
 * something real on disk to point provenance/pointers at -- registering it
 * as a tracked artifact is a separate, later step, not this command's job.
 *
 * With an explicit `outputPath`, writes there directly instead (a plain
 * file write, not namespaced under the snapshots directory) -- useful when
 * the caller wants the pulled copy somewhere specific, e.g. alongside a
 * paper's own repo.
 */
async function pullDoc({ projectId, docId: docIdOrPath, outputPath }: PullDocArgs): Promise<void> {
  const cookie = loadSavedCookie();
  if (!cookie) {
    console.error("No saved Overleaf session -- run `meridian-latex login` first.");
    process.exit(1);
  }
  const httpBaseUrl = (status() as { baseUrl?: string }).baseUrl || "https://www.overleaf.com";
  const wsBaseUrl = httpBaseUrl.replace(/^http/, "ws");

  const session = await connectToProject({ projectId, httpBaseUrl, wsBaseUrl, cookie });
  try {
    let docId = docIdOrPath;
    if (!OBJECT_ID_RE.test(docIdOrPath)) {
      const resolved = session.resolveDocId(docIdOrPath);
      if (!resolved.ok) {
        console.error(`Could not resolve "${docIdOrPath}" to a doc: ${resolved.reason}`);
        process.exit(1);
      }
      docId = resolved.docId;
    }
    const { source, unexpanded, version } = await joinDocExpanded(session, docId);
    if (unexpanded.length > 0) {
      console.error(
        `Note: ${unexpanded.length} \\input/\\include reference(s) could not be resolved and were left as-is: ${unexpanded.join(", ")}`,
      );
    }
    const path = outputPath
      ? (writeFileSync(outputPath, source), outputPath)
      : snapshotDoc({ projectId, docId, lines: source.split("\n") });
    console.log(JSON.stringify({ ok: true, path, docId, version, unexpanded }, null, 2));
  } finally {
    session.close();
  }
}

interface ListProjectDocsArgs {
  projectId: string;
}

/**
 * The `ls` subcommand's I/O glue: connect, list every doc's resolved path +
 * docId from the live joinProjectResponse file tree (see project-tree.js's
 * `listDocPaths`), print as JSON, close. Mirrors writeField()'s own
 * connect/finally-close shape.
 */
async function listProjectDocs({ projectId }: ListProjectDocsArgs): Promise<void> {
  const cookie = loadSavedCookie();
  if (!cookie) {
    console.error("No saved Overleaf session -- run `meridian-latex login` first.");
    process.exit(1);
  }
  const httpBaseUrl = (status() as { baseUrl?: string }).baseUrl || "https://www.overleaf.com";
  const wsBaseUrl = httpBaseUrl.replace(/^http/, "ws");

  const session = await connectToProject({ projectId, httpBaseUrl, wsBaseUrl, cookie });
  try {
    console.log(JSON.stringify(listDocPaths(session.rootFolder), null, 2));
  } finally {
    session.close();
  }
}

async function main(): Promise<void> {
  if (command === "outline") {
    const path = rest[0];
    if (!path) {
      console.error("usage: meridian-latex outline <path-to.tex>");
      process.exit(1);
    }
    console.log(JSON.stringify(outlineFile(path), null, 2));
  } else if (command === "compile") {
    const path = rest[0];
    if (!path) {
      console.error("usage: meridian-latex compile <path-to.tex> [--engine=pdflatex|xelatex|lualatex] [--project-id=<id>]");
      process.exit(1);
    }
    const options = rest.slice(1);
    const engineArg = options.find((value) => value.startsWith("--engine="));
    const projectArg = options.find((value) => value.startsWith("--project-id="));
    if (options.some((value) => value !== engineArg && value !== projectArg)) {
      console.error("compile accepts only --engine and --project-id options");
      process.exit(1);
    }
    const engine = (engineArg?.slice("--engine=".length) ?? "pdflatex") as LatexEngine;
    try {
      const receipt = await compileLocalLatex({
        rootFile: path,
        engine,
        overleafProjectId: projectArg?.slice("--project-id=".length),
      });
      console.log(JSON.stringify(receipt, null, 2));
      if (receipt.status !== "passed") process.exitCode = 1;
    } catch (error) {
      console.error(`Compile failed: ${error instanceof Error ? error.message : String(error)}`);
      process.exitCode = 1;
    }
  } else if (command === "lint") {
    const path = rest[0];
    if (!path) {
      console.error("usage: meridian-latex lint <path-to.tex>");
      process.exit(1);
    }
    console.log(JSON.stringify(lintFile(path), null, 2));
  } else if (command === "serve") {
    // server.js starts listening as a side effect of being imported (see
    // its own header comment / bottom-of-file `server.listen(...)` call) --
    // dynamic import so that only happens when `serve` is the chosen
    // subcommand, not on every `meridian-latex` invocation.
    await import("./server.js");
  } else if (command === "login") {
    await login();
  } else if (command === "status") {
    console.log(JSON.stringify(status(), null, 2));
  } else if (command === "logout") {
    logout();
    console.log("Logged out -- saved cookie removed.");
  } else if (command === "ls") {
    const [projectId] = rest;
    if (!projectId) {
      console.error("usage: meridian-latex ls <project-id>");
      process.exit(1);
    }
    await listProjectDocs({ projectId });
  } else if (command === "write") {
    const [projectId, docId, nodeId, fieldName, ...textParts] = rest;
    if (!projectId || !docId || !nodeId || !fieldName || textParts.length === 0) {
      console.error(
        "usage: meridian-latex write <project-id> <doc-id-or-path> <node-id> <field> <new-text>\n" +
          "  field: title | key | caption | label",
      );
      process.exit(1);
    }
    await writeField({ projectId, docId, nodeId, fieldName, newText: textParts.join(" ") });
  } else if (command === "pull") {
    const [projectId, docId, outputPath] = rest;
    if (!projectId || !docId) {
      console.error("usage: meridian-latex pull <project-id> <doc-id-or-path> [output-path]");
      process.exit(1);
    }
    await pullDoc({ projectId, docId, outputPath });
  } else if (command === "style-guide") {
    const [sectionType] = rest;
    const result = getStyleGuide(sectionType);
    if ("error" in result) {
      console.error(result.error);
      process.exit(1);
    }
    console.log(JSON.stringify(result, null, 2));
  } else if (command === "style-check") {
    const [path, sectionType] = rest;
    if (!path || !sectionType) {
      console.error("usage: meridian-latex style-check <path.tex> <section-type>");
      process.exit(1);
    }
    const text = readFileSync(path, "utf-8");
    const result = checkSectionStyle({ sectionType, text });
    if ("error" in result) {
      console.error(result.error);
      process.exit(1);
    }
    console.log(JSON.stringify(result, null, 2));
  } else if (command === "style-lookup") {
    const [sectionType, ...topicParts] = rest;
    const topic = topicParts.join(" ");
    if (!sectionType || !topic) {
      console.error("usage: meridian-latex style-lookup <section-type> <topic>");
      process.exit(1);
    }
    // No researchProvider is ever wired in here -- this standalone CLI
    // process has no "calling session" with a research/paper-search
    // capability of its own (see style-guide.js's own header comment on
    // lookupPublishedFraming for why this is by design, not an oversight).
    // Even with the env var gate enabled, this will fail closed with a
    // clear NO_PROVIDER error -- exactly the documented, honest behavior for
    // running this feature from a bare terminal rather than through an
    // embedding host that injects a real provider.
    const result = await lookupPublishedFraming(sectionType, topic);
    if ("error" in result) {
      console.error(result.error);
      process.exit(1);
    }
    console.log(JSON.stringify(result, null, 2));
  } else if (command === "mcp") {
    // Unlike server.js (which starts listening as an unconditional import-time
    // side effect -- see the "serve" branch above), mcp-server.js deliberately
    // guards its own stdio-serving entry point behind an `isMainModule()`
    // check (see its own header comment) so that importing it -- e.g. from a
    // test, or from this dynamic import -- never itself opens the real store
    // or attaches a live stdio transport. Reaching the MCP server through
    // THIS subcommand is therefore an import (isMainModule() is false here,
    // since cli.js, not mcp-server.js, is process.argv[1]), so runStdioServer()
    // has to be called explicitly rather than relying on that auto-run guard.
    const { runStdioServer } = await import("./mcp-server.js");
    await runStdioServer();
  } else {
    console.error(command ? `unknown command: ${command}\n\n${USAGE}` : USAGE);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
