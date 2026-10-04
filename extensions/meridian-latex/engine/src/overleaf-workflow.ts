/**
 * Local LaTeX compile receipts for the Overleaf workstation workflow.
 *
 * This module never writes to or synchronizes an Overleaf project. It compiles
 * an explicitly selected local .tex root, records the exact local source
 * manifest and output hashes, and keeps receipts/build products under the
 * user's local Meridian state directory. A receipt is not evidence that the
 * local files were synchronized to Overleaf or that the PDF passed visual,
 * citation, or editorial review.
 */
import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { basename, delimiter, dirname, extname, isAbsolute, join, relative, resolve, sep } from "node:path";

export type LatexEngine = "pdflatex" | "xelatex" | "lualatex";
export type CompileStatus = "passed" | "failed" | "incomplete" | "unavailable";
export type BibliographyBackend = "bibtex" | "bibtex8" | "biber" | "none";

export interface CompileManifestFile {
  path: string;
  kind: "tex" | "bibliography" | "class" | "style" | "asset" | "other";
  sha256: string;
  size_bytes: number;
}

export interface CompileReceipt {
  schema_version: 1;
  receipt_id: string;
  created_at: string;
  status: CompileStatus;
  root_file: string;
  source_manifest: { complete: boolean; files: CompileManifestFile[]; unresolved_count: number; sha256: string };
  compiler: { engine: LatexEngine; version: string | null; flags: string[]; bibliography_backend: BibliographyBackend };
  environment: { platform: string; architecture: string; node_version: string };
  phases: Array<{ tool: string; exit_code: number | null; duration_ms: number; stdout_sha256: string; stderr_sha256: string }>;
  output: { pdf_path: string | null; pdf_sha256: string | null; log_sha256: string | null };
  overleaf_project_id: string | null;
  overleaf_sync: "not_attested";
  limitations: string[];
}

export interface CompileLocalLatexOptions {
  rootFile: string;
  projectRoot?: string;
  engine?: LatexEngine;
  overleafProjectId?: string;
  stateDir?: string;
  timeoutMs?: number;
  /** Test seam. Production callers should use the bounded built-in runner. */
  runCommand?: CommandRunner;
}

export interface CommandResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  durationMs: number;
  errorCode?: string;
}

export type CommandRunner = (executable: string, args: string[], cwd: string, timeoutMs: number, env?: NodeJS.ProcessEnv) => Promise<CommandResult>;

const DEFAULT_TIMEOUT_MS = 120_000;
const MAX_CAPTURED_OUTPUT = 1_000_000;
const ENGINE_NAMES: LatexEngine[] = ["pdflatex", "xelatex", "lualatex"];
const GRAPHICS_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"];
const GRAPHICS_COMMANDS = new Set(["includegraphics"]);
const FILE_COMMAND_EXTENSIONS: Record<string, { extension: string; kind: CompileManifestFile["kind"]; required: boolean }> = {
  input: { extension: ".tex", kind: "tex", required: true },
  include: { extension: ".tex", kind: "tex", required: true },
  subfile: { extension: ".tex", kind: "tex", required: true },
  bibliography: { extension: ".bib", kind: "bibliography", required: true },
  addbibresource: { extension: ".bib", kind: "bibliography", required: true },
  bibliographystyle: { extension: ".bst", kind: "other", required: false },
  documentclass: { extension: ".cls", kind: "class", required: false },
  usepackage: { extension: ".sty", kind: "style", required: false },
};

interface UnresolvedReference {
  source: string;
  command: string;
  reason: "dynamic" | "missing" | "outside_project" | "ambiguous";
  digest: string;
}

interface CollectedSources {
  rootFile: string;
  projectRoot: string;
  files: Map<string, CompileManifestFile>;
  sourceTexts: Map<string, string>;
  unresolved: UnresolvedReference[];
  bibliographyBackend: BibliographyBackend;
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function inside(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

function withoutComments(source: string): string {
  let result = "";
  for (let i = 0; i < source.length; i += 1) {
    const char = source[i];
    if (char !== "%") {
      result += char;
      continue;
    }
    let backslashes = 0;
    for (let j = i - 1; j >= 0 && source[j] === "\\"; j -= 1) backslashes += 1;
    if (backslashes % 2 === 1) result += char;
    else {
      while (i < source.length && source[i] !== "\n") i += 1;
      if (i < source.length) result += "\n";
    }
  }
  return result;
}

function extractReferences(source: string): Array<{ command: string; value: string }> {
  const code = withoutComments(source);
  const refs: Array<{ command: string; value: string }> = [];
  const commandPattern = /\\([a-zA-Z]+)\*?\s*(?:\[[^\]]*\]\s*)*\{([^{}]*)\}/g;
  for (const match of code.matchAll(commandPattern)) {
    const command = match[1].toLowerCase();
    if (FILE_COMMAND_EXTENSIONS[command] || GRAPHICS_COMMANDS.has(command)) {
      refs.push({ command, value: match[2].trim() });
    }
  }
  // TeX also accepts the common unbraced form `\\input chapter`. Hash simple
  // literal filenames during preflight so recorder reconciliation can compare
  // against a precompile baseline; a bounded tree snapshot covers recorder-only
  // inputs that this lexical scan cannot identify.
  const unbracedInputPattern = /\\input(?![a-zA-Z])\s+([^\s{}%\\]+)/g;
  for (const match of code.matchAll(unbracedInputPattern)) {
    refs.push({ command: "input", value: match[1].trim() });
  }
  // \import{directory}{file} is common in multi-file papers and is not a
  // one-argument command. Resolve only literal paths; macro-expanded paths are
  // recorded as unknown below instead of being guessed.
  const importPattern = /\\(?:sub)?import\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g;
  for (const match of code.matchAll(importPattern)) {
    refs.push({ command: "input", value: `${match[1].trim()}${match[2].trim()}` });
  }
  return refs;
}

function readBracedGroup(source: string, start: number): { value: string; end: number } | null {
  if (source[start] !== "{") return null;
  let depth = 1;
  for (let index = start + 1; index < source.length; index += 1) {
    if (source[index] === "\\") {
      // TeX control symbols can escape braces; do not treat those as groups.
      index += 1;
      continue;
    }
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return { value: source.slice(start + 1, index), end: index + 1 };
    }
  }
  return null;
}

function extractGraphicspathDeclarations(source: string): Array<{ start: number; paths: string[]; malformed: boolean }> {
  const code = withoutComments(source);
  const declarations: Array<{ start: number; paths: string[]; malformed: boolean }> = [];
  for (const match of code.matchAll(/\\graphicspath\b/g)) {
    let cursor = match.index! + match[0].length;
    while (/\s/.test(code[cursor] ?? "")) cursor += 1;
    const outer = readBracedGroup(code, cursor);
    if (!outer) {
      declarations.push({ start: match.index!, paths: [], malformed: true });
      continue;
    }
    const paths: string[] = [];
    let malformed = false;
    cursor = 0;
    while (cursor < outer.value.length) {
      while (/\s/.test(outer.value[cursor] ?? "")) cursor += 1;
      if (cursor >= outer.value.length) break;
      const path = readBracedGroup(outer.value, cursor);
      if (!path) {
        malformed = true;
        break;
      }
      if (path.value.trim()) paths.push(path.value.trim());
      cursor = path.end;
    }
    declarations.push({ start: match.index!, paths, malformed });
  }
  return declarations;
}

function extractGraphicReferences(source: string): Array<{ start: number; value: string; malformed: boolean }> {
  const code = withoutComments(source);
  const references: Array<{ start: number; value: string; malformed: boolean }> = [];
  for (const match of code.matchAll(/\\includegraphics\*?(?![a-zA-Z])/g)) {
    let cursor = match.index! + match[0].length;
    while (/\s/.test(code[cursor] ?? "")) cursor += 1;
    while (code[cursor] === "[") {
      let depth = 1;
      cursor += 1;
      while (cursor < code.length && depth > 0) {
        if (code[cursor] === "\\") cursor += 1;
        else if (code[cursor] === "[") depth += 1;
        else if (code[cursor] === "]") depth -= 1;
        cursor += 1;
      }
      while (/\s/.test(code[cursor] ?? "")) cursor += 1;
    }
    const group = readBracedGroup(code, cursor);
    references.push({ start: match.index!, value: group?.value.trim() ?? "", malformed: !group });
  }
  return references;
}

function extractTeXInputReferences(source: string): Array<{ start: number; value: string; command: string }> {
  const code = withoutComments(source);
  const references: Array<{ start: number; value: string; command: string }> = [];
  for (const match of code.matchAll(/\\(input|include|subfile)\*?(?![a-zA-Z])\s*/g)) {
    let cursor = match.index! + match[0].length;
    while (code[cursor] === "[") {
      const end = code.indexOf("]", cursor + 1);
      if (end < 0) break;
      cursor = end + 1;
      while (/\s/.test(code[cursor] ?? "")) cursor += 1;
    }
    const group = readBracedGroup(code, cursor);
    if (group) references.push({ start: match.index!, value: group.value.trim(), command: match[1].toLowerCase() });
  }
  for (const match of code.matchAll(/\\(?:sub)?import\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g)) {
    references.push({ start: match.index!, value: `${match[1].trim()}${match[2].trim()}`, command: "input" });
  }
  return references;
}

function safeRelativePath(root: string, file: string): string {
  return relative(root, file).split(sep).join("/");
}

async function resolveLocalReference(
  root: string,
  from: string,
  command: string,
  raw: string,
  graphicDirectories: string[] = [],
  recordedGraphicInputs?: Set<string>,
): Promise<{ file: string; kind: CompileManifestFile["kind"] } | { reason: UnresolvedReference["reason"] } | null> {
  const spec = FILE_COMMAND_EXTENSIONS[command];
  if (!spec && command !== "includegraphics") return null;
  if (!raw || /[\\$#]/.test(raw) || raw.includes("\n")) return { reason: "dynamic" };

  const base = raw.replace(/^['"]|['"]$/g, "");
  if (!base || isAbsolute(base) || /^[a-zA-Z]:[\\/]/.test(base)) return { reason: "outside_project" };
  const fromDirectory = dirname(from);
  const extensions = command === "includegraphics"
    ? (extname(base) ? [""] : GRAPHICS_EXTENSIONS)
    : (extname(base) ? [""] : [spec!.extension]);
  const searchDirectories = command === "includegraphics"
    ? [fromDirectory, ...graphicDirectories, root]
    : [fromDirectory, root];
  const candidates = searchDirectories.flatMap((directory) => [
    resolve(directory, base),
    ...extensions.map((ext) => resolve(directory, `${base}${ext}`)),
  ]);
  const recordedMatches: string[] = [];
  let recorderContainmentFailure = false;
  for (const candidate of [...new Set(candidates)]) {
    if (!inside(root, candidate)) {
      if (command === "includegraphics" && recordedGraphicInputs !== undefined) {
        recorderContainmentFailure = true;
        continue;
      }
      return { reason: "outside_project" };
    }
    try {
      const actual = await fs.realpath(candidate);
      if (!inside(root, actual)) {
        if (command === "includegraphics" && recordedGraphicInputs !== undefined) {
          recorderContainmentFailure = true;
          continue;
        }
        return { reason: "outside_project" };
      }
      const stat = await fs.stat(actual);
      if (!stat.isFile()) continue;
      if (command === "includegraphics" && recordedGraphicInputs !== undefined) {
        if (recordedGraphicInputs.has(actual)) recordedMatches.push(actual);
        continue;
      }
      return { file: actual, kind: spec?.kind ?? "asset" };
    } catch {
      // Continue through TeX's candidate extensions.
    }
  }
  if (command === "includegraphics" && recordedGraphicInputs !== undefined) {
    const uniqueMatches = [...new Set(recordedMatches)];
    if (uniqueMatches.length === 1) return { file: uniqueMatches[0], kind: "asset" };
    if (uniqueMatches.length > 1) return { reason: "ambiguous" };
    if (recorderContainmentFailure) return { reason: "outside_project" };
  }
  // documentclass/usepackage can be installed with the TeX distribution; only
  // local copies are part of this project-local source manifest.
  if (spec && !spec.required) return null;
  return { reason: "missing" };
}

async function resolveGraphicDirectory(
  root: string,
  raw: string,
): Promise<{ directory: string } | { reason: UnresolvedReference["reason"] }> {
  if (/[\\$#{}\r\n]/.test(raw)) return { reason: "dynamic" };
  if (!raw || isAbsolute(raw) || /^[a-zA-Z]:[\\/]/.test(raw)) return { reason: "outside_project" };
  const candidate = resolve(root, raw);
  if (!inside(root, candidate)) return { reason: "outside_project" };
  try {
    const actual = await fs.realpath(candidate);
    if (!inside(root, actual)) return { reason: "outside_project" };
    if ((await fs.stat(actual)).isDirectory()) return { directory: actual };
    return { reason: "missing" };
  } catch {
    return { reason: "missing" };
  }
}

function recordedGraphicMatch(
  raw: string,
  root: string,
  inputs: string[],
): { file: string } | { reason: "ambiguous" } | null {
  if (!raw || /[\\$#\r\n]/.test(raw) || isAbsolute(raw) || /^[a-zA-Z]:[\\/]/.test(raw)) return null;
  const normalizedParts: string[] = [];
  for (const part of raw.replace(/^['"]|['"]$/g, "").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (normalizedParts.length === 0) return null;
      normalizedParts.pop();
    } else normalizedParts.push(part);
  }
  if (normalizedParts.length === 0) return null;
  const requested = normalizedParts.join("/").toLowerCase();
  const requestedExtension = extname(normalizedParts.at(-1) ?? "").toLowerCase();
  const matches = [...new Set(inputs.filter((path) => {
    if (manifestKind(path) !== "asset") return false;
    const relativePath = safeRelativePath(root, path).toLowerCase();
    const actualExtension = extname(path).toLowerCase();
    if (requestedExtension && actualExtension !== requestedExtension) return false;
    const expected = requestedExtension ? requested : `${requested}${actualExtension}`;
    return relativePath === expected || relativePath.endsWith(`/${expected}`);
  }))];
  // The recorder resolves TeX's effective search path. Reconcile a literal
  // path only when its normalized suffix identifies one in-project input.
  if (matches.length === 1) return { file: matches[0] };
  return matches.length > 1 ? { reason: "ambiguous" } : null;
}

function explicitBiblatexBackend(source: string): "bibtex" | "bibtex8" | "biber" | null {
  const code = withoutComments(source);
  const optionBackend = (options: string | undefined): "bibtex" | "bibtex8" | "biber" | null => {
    const match = options?.match(/(?:^|,)\s*backend\s*=\s*(bibtex8|bibtex|biber)\s*(?:,|$)/i);
    return (match?.[1]?.toLowerCase() as "bibtex" | "bibtex8" | "biber" | undefined) ?? null;
  };
  let packageBackend: "bibtex" | "bibtex8" | "biber" | null = null;
  for (const match of code.matchAll(/\\(?:usepackage|RequirePackage)\s*(?:\[([^\]]*)\]\s*)?\{([^{}]*)\}/gi)) {
    if (match[2].split(",").some((name) => name.trim().toLowerCase() === "biblatex")) {
      packageBackend = optionBackend(match[1]) ?? packageBackend;
    }
  }
  let passedBackend: "bibtex" | "bibtex8" | "biber" | null = null;
  for (const match of code.matchAll(/\\PassOptionsToPackage\s*\{([^{}]*)\}\s*\{([^{}]*)\}/gi)) {
    if (match[2].split(",").some((name) => name.trim().toLowerCase() === "biblatex")) {
      passedBackend = optionBackend(match[1]) ?? passedBackend;
    }
  }
  let executedBackend: "bibtex" | "bibtex8" | "biber" | null = null;
  for (const match of code.matchAll(/\\ExecuteBibliographyOptions\s*\{([^{}]*)\}/gi)) {
    executedBackend = optionBackend(match[1]) ?? executedBackend;
  }
  return executedBackend ?? packageBackend ?? passedBackend;
}

async function collectSources(rootFile: string, projectRoot: string, extraInputs: string[] = []): Promise<CollectedSources> {
  const realRoot = await fs.realpath(projectRoot);
  const realMain = await fs.realpath(rootFile);
  if (!inside(realRoot, realMain)) throw new Error("root .tex file must be inside the selected project root");
  const files = new Map<string, CompileManifestFile>();
  const sourceTexts = new Map<string, string>();
  const unresolved: UnresolvedReference[] = [];
  const recorderInputFiles: string[] = [];
  const queued = [realMain];
  const queuedSet = new Set(queued);

  const addFile = async (file: string, kind: CompileManifestFile["kind"]): Promise<string> => {
    const data = await fs.readFile(file);
    const entry = { path: safeRelativePath(realRoot, file), kind, sha256: sha256(data), size_bytes: data.byteLength };
    files.set(file, entry);
    if (["tex", "class", "style"].includes(kind)) sourceTexts.set(file, data.toString("utf8"));
    return data.toString("utf8");
  };

  while (queued.length > 0) {
    const file = queued.shift()!;
    if (files.has(file)) continue;
    const content = await addFile(file, extname(file).toLowerCase() === ".cls" ? "class" : extname(file).toLowerCase() === ".sty" ? "style" : "tex");
    for (const reference of extractReferences(content)) {
      if (reference.command === "includegraphics") continue;
      const values = ["bibliography", "usepackage"].includes(reference.command)
        ? reference.value.split(",").map((value) => value.trim()).filter(Boolean)
        : [reference.value];
      for (const value of values) {
        const resolved = await resolveLocalReference(realRoot, file, reference.command, value);
        if (!resolved) continue;
        if ("reason" in resolved) {
          const raw = `${reference.command}:${value}`;
          unresolved.push({ source: safeRelativePath(realRoot, file), command: reference.command, reason: resolved.reason, digest: sha256(raw) });
          continue;
        }
        if (!files.has(resolved.file) && !queuedSet.has(resolved.file)) {
          if (["tex", "class", "style"].includes(resolved.kind)) {
            queued.push(resolved.file);
            queuedSet.add(resolved.file);
          } else {
            await addFile(resolved.file, resolved.kind);
          }
        }
      }
    }
  }
  for (const input of extraInputs) {
    const candidate = isAbsolute(input) ? input : resolve(realRoot, input);
    try {
      const actual = await fs.realpath(candidate);
      if (!inside(realRoot, actual)) continue;
      const stat = await fs.stat(actual);
      if (!stat.isFile()) continue;
      recorderInputFiles.push(actual);
      if (files.has(actual)) continue;
      const kind = manifestKind(actual);
      if (["tex", "class", "style"].includes(kind)) {
        if (!queuedSet.has(actual)) {
          queued.push(actual);
          queuedSet.add(actual);
        }
      } else {
        await addFile(actual, kind);
      }
    } catch {
      // TeX recorder entries may name deleted transient files; leave the
      // lexical completeness signal to the preflight scan.
    }
  }
  // Recorder-listed .tex/.cls/.sty inputs can contain their own inputs or
  // local macro packages. Walk those too, so the final manifest represents
  // the whole local source closure instead of only the root's direct refs.
  while (queued.length > 0) {
    const file = queued.shift()!;
    if (files.has(file)) continue;
    const content = await addFile(file, manifestKind(file) as "tex" | "class" | "style");
    for (const reference of extractReferences(content)) {
      if (reference.command === "includegraphics") continue;
      const values = ["bibliography", "usepackage"].includes(reference.command)
        ? reference.value.split(",").map((value) => value.trim()).filter(Boolean)
        : [reference.value];
      for (const value of values) {
        const resolved = await resolveLocalReference(realRoot, file, reference.command, value);
        if (!resolved) continue;
        if ("reason" in resolved) {
          unresolved.push({ source: safeRelativePath(realRoot, file), command: reference.command, reason: resolved.reason, digest: sha256(`${reference.command}:${value}`) });
          continue;
        }
        if (files.has(resolved.file) || queuedSet.has(resolved.file)) continue;
        if (["tex", "class", "style"].includes(resolved.kind)) {
          queued.push(resolved.file);
          queuedSet.add(resolved.file);
        } else {
          await addFile(resolved.file, resolved.kind);
        }
      }
    }
  }

  // Preserve declaration/reference order within each source file. A later
  // \graphicspath redefines the search list, so combining every declaration
  // globally can attach a duplicate-basename image from the wrong directory.
  const graphicReferences: Array<{ file: string; value: string; directories: string[] }> = [];
  const graphicPathIssues: UnresolvedReference[] = [];
  let sawGraphicReference = false;
  const visitedGraphicSources = new Set<string>();
  const collectGraphicEvents = async (
    file: string,
    content: string,
    initialDirectories: string[],
    ancestry: Set<string>,
  ): Promise<string[]> => {
    visitedGraphicSources.add(file);
    const events = [
      ...extractGraphicspathDeclarations(content).map((declaration) => ({ type: "paths" as const, ...declaration })),
      ...extractGraphicReferences(content).map((reference) => ({ type: "reference" as const, ...reference })),
      ...extractTeXInputReferences(content).map((reference) => ({ type: "input" as const, ...reference })),
    ].sort((left, right) => left.start - right.start);
    let activeDirectories = [...initialDirectories];
    for (const event of events) {
      if (event.type === "input") {
        const resolved = await resolveLocalReference(realRoot, file, event.command, event.value);
        if (resolved && !("reason" in resolved) && !ancestry.has(resolved.file)) {
          const childSource = sourceTexts.get(resolved.file);
          if (childSource !== undefined) {
            activeDirectories = await collectGraphicEvents(
              resolved.file,
              childSource,
              activeDirectories,
              new Set([...ancestry, file]),
            );
          }
        }
        continue;
      }
      if (event.type === "reference") {
        sawGraphicReference = true;
        if (event.malformed) {
          unresolved.push({
            source: safeRelativePath(realRoot, file),
            command: "includegraphics",
            reason: "dynamic",
            digest: sha256("includegraphics:malformed"),
          });
        } else {
          graphicReferences.push({ file, value: event.value, directories: [...activeDirectories] });
        }
        continue;
      }

      activeDirectories = [];
      if (event.malformed) {
        graphicPathIssues.push({
          source: safeRelativePath(realRoot, file),
          command: "graphicspath",
          reason: "dynamic",
          digest: sha256("graphicspath:malformed"),
        });
      }
      for (const rawPath of event.paths) {
        const resolved = await resolveGraphicDirectory(realRoot, rawPath);
        if ("reason" in resolved) {
          // Missing search directories are harmless when another declared
          // directory resolves the image; unsafe/dynamic directories remain
          // unresolved provenance instead of being traversed.
          if (resolved.reason !== "missing") {
            graphicPathIssues.push({
              source: safeRelativePath(realRoot, file),
              command: "graphicspath",
              reason: resolved.reason,
              digest: sha256(`graphicspath:${rawPath}`),
            });
          }
        } else if (!activeDirectories.includes(resolved.directory)) {
          activeDirectories.push(resolved.directory);
        }
      }
    }
    return activeDirectories;
  };

  const rootSource = sourceTexts.get(realMain);
  const rootGraphicDirectories = rootSource
    ? await collectGraphicEvents(realMain, rootSource, [], new Set())
    : [];
  for (const [file, content] of sourceTexts) {
    if (!visitedGraphicSources.has(file)) {
      await collectGraphicEvents(file, content, rootGraphicDirectories, new Set([file]));
    }
  }
  if (sawGraphicReference) unresolved.push(...graphicPathIssues);
  const recorderGraphicInputs = extraInputs.length > 0 ? new Set(recorderInputFiles) : undefined;
  for (const reference of graphicReferences) {
    const resolved = await resolveLocalReference(
      realRoot,
      reference.file,
      "includegraphics",
      reference.value,
      reference.directories,
      recorderGraphicInputs,
    );
    if (!resolved) continue;
    if ("reason" in resolved) {
      const recorded = ["missing", "ambiguous"].includes(resolved.reason)
        ? recordedGraphicMatch(reference.value, realRoot, recorderInputFiles)
        : null;
      if (recorded && "file" in recorded) {
        if (!files.has(recorded.file)) await addFile(recorded.file, "asset");
        continue;
      }
      const reason = recorded && "reason" in recorded ? recorded.reason : resolved.reason;
      unresolved.push({
        source: safeRelativePath(realRoot, reference.file),
        command: "includegraphics",
        reason,
        digest: sha256(`includegraphics:${reference.value}`),
      });
      continue;
    }
    if (!files.has(resolved.file)) await addFile(resolved.file, resolved.kind);
  }

  const combined = [...sourceTexts.values()].join("\n");
  const hasBiblatex = /\\(?:usepackage|RequirePackage)(?:\s*\[[^\]]*\])?\s*\{[^}]*\bbiblatex\b[^}]*\}/i.test(withoutComments(combined)) || /\\addbibresource\s*(?:\[[^\]]*\])?\s*\{/i.test(withoutComments(combined));
  const hasBibtex = /\\bibliography\s*\{/i.test(withoutComments(combined));
  const bibliographyBackend: BibliographyBackend = hasBiblatex ? explicitBiblatexBackend(combined) ?? "biber" : hasBibtex ? "bibtex" : "none";
  return { rootFile: realMain, projectRoot: realRoot, files, sourceTexts, unresolved, bibliographyBackend };
}

async function captureRecorderBaseline(
  projectRoot: string,
  initialFiles: Map<string, CompileManifestFile>,
  buildDir: string,
): Promise<Map<string, string>> {
  const root = await fs.realpath(projectRoot);
  const baseline = new Map([...initialFiles].map(([path, file]) => [path, file.sha256]));
  const pending = [root];
  const visitedDirectories = new Set(pending);
  const excludedBuildDir = resolve(buildDir);
  let scannedFiles = 0;
  let scannedBytes = 0;
  const maximumFiles = 20_000;
  const maximumBytes = 64 * 1024 * 1024;

  while (pending.length > 0 && scannedFiles < maximumFiles && scannedBytes < maximumBytes) {
    const directoryPath = pending.pop()!;
    let directory;
    try {
      directory = await fs.opendir(directoryPath);
    } catch {
      continue;
    }
    for await (const entry of directory) {
      const child = join(directoryPath, entry.name);
      if (entry.isSymbolicLink() || inside(excludedBuildDir, child)) continue;
      if (entry.isDirectory()) {
        if ([".git", "node_modules"].includes(entry.name)) continue;
        try {
          const actualDirectory = await fs.realpath(child);
          if (inside(root, actualDirectory) && !visitedDirectories.has(actualDirectory)) {
            visitedDirectories.add(actualDirectory);
            pending.push(actualDirectory);
          }
        } catch {
          // An unreadable directory cannot provide a baseline; a recorder
          // reference to a file inside it will be reported as unbaselined.
        }
        continue;
      }
      if (!entry.isFile()) continue;
      scannedFiles += 1;
      if (scannedFiles > maximumFiles) break;
      try {
        const actual = await fs.realpath(child);
        if (!inside(root, actual) || baseline.has(actual)) continue;
        const stat = await fs.stat(actual);
        if (!stat.isFile() || scannedBytes + stat.size > maximumBytes) {
          if (stat.size > maximumBytes) scannedBytes = maximumBytes;
          continue;
        }
        const data = await fs.readFile(actual);
        scannedBytes += data.byteLength;
        baseline.set(actual, sha256(data));
      } catch {
        // A later recorder reference without a captured hash fails closed.
      }
      if (scannedBytes >= maximumBytes) break;
    }
  }
  return baseline;
}

function manifestKind(path: string): CompileManifestFile["kind"] {
  switch (extname(path).toLowerCase()) {
    case ".tex": return "tex";
    case ".bib": return "bibliography";
    case ".cls": return "class";
    case ".sty": return "style";
    case ".pdf": case ".png": case ".jpg": case ".jpeg": case ".eps": case ".svg": return "asset";
    default: return "other";
  }
}

function manifestHash(files: CompileManifestFile[], complete: boolean, unresolved: UnresolvedReference[]): string {
  const canonical = JSON.stringify({ complete, files: [...files].sort((a, b) => a.path.localeCompare(b.path)), unresolved: unresolved.map(({ source, command, reason, digest }) => ({ source, command, reason, digest })).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))) });
  return sha256(canonical);
}

export function runBoundedCommand(executable: string, args: string[], cwd: string, timeoutMs: number, env?: NodeJS.ProcessEnv): Promise<CommandResult> {
  return new Promise((resolveResult) => {
    const started = Date.now();
    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (result: Omit<CommandResult, "durationMs">) => {
      if (settled) return;
      settled = true;
      resolveResult({ ...result, durationMs: Date.now() - started });
    };
    let child;
    try {
      child = spawn(executable, args, { cwd, env: env ?? process.env, shell: false, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      finish({ exitCode: null, stdout, stderr, errorCode: (error as NodeJS.ErrnoException).code ?? "SPAWN_FAILED" });
      return;
    }
    const append = (current: string, chunk: Buffer): string => (current + chunk.toString("utf8")).slice(-MAX_CAPTURED_OUTPUT);
    child.stdout?.on("data", (chunk: Buffer) => { stdout = append(stdout, chunk); });
    child.stderr?.on("data", (chunk: Buffer) => { stderr = append(stderr, chunk); });
    const timer = setTimeout(() => {
      child.kill();
      finish({ exitCode: null, stdout, stderr, errorCode: "TIMEOUT" });
    }, timeoutMs);
    child.once("error", (error: NodeJS.ErrnoException) => {
      clearTimeout(timer);
      finish({ exitCode: null, stdout, stderr, errorCode: error.code ?? "SPAWN_FAILED" });
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      finish({ exitCode: code, stdout, stderr });
    });
  });
}

function phase(tool: string, result: CommandResult) {
  return { tool, exit_code: result.exitCode, duration_ms: result.durationMs, stdout_sha256: sha256(result.stdout), stderr_sha256: sha256(result.stderr) };
}

function receiptDirectory(stateDir: string, identity: string): string {
  return join(stateDir, "compile-receipts", sha256(identity));
}

async function persistReceipt(receipt: CompileReceipt, directory: string): Promise<void> {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const target = join(directory, `${receipt.created_at.replace(/[:.]/g, "-")}-${receipt.receipt_id}.json`);
  const temp = `${target}.tmp`;
  await fs.writeFile(temp, `${JSON.stringify(receipt, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  await fs.rename(temp, target);
}

function buildReceipt(input: {
  status: CompileStatus;
  rootFile: string;
  files: CompileManifestFile[];
  complete: boolean;
  unresolved: UnresolvedReference[];
  engine: LatexEngine;
  version: string | null;
  flags: string[];
  bibliographyBackend: BibliographyBackend;
  phases: CompileReceipt["phases"];
  pdfHash: string | null;
  pdfPath: string | null;
  logHash: string | null;
  overleafProjectId?: string;
  sourceChanged: boolean;
  unbaselinedInputs: string[];
  prerequisiteNotes: string[];
}): CompileReceipt {
  const files = [...input.files].sort((a, b) => a.path.localeCompare(b.path));
  const complete = input.complete && input.unresolved.length === 0;
  return {
    schema_version: 1,
    receipt_id: randomUUID(),
    created_at: new Date().toISOString(),
    status: input.status === "passed" && !complete ? "incomplete" : input.status,
    root_file: input.rootFile,
    source_manifest: { complete, files, unresolved_count: input.unresolved.length, sha256: manifestHash(files, complete, input.unresolved) },
    compiler: { engine: input.engine, version: input.version, flags: input.flags, bibliography_backend: input.bibliographyBackend },
    environment: { platform: process.platform, architecture: process.arch, node_version: process.version },
    phases: input.phases,
    output: { pdf_path: input.pdfPath, pdf_sha256: input.pdfHash, log_sha256: input.logHash },
    overleaf_project_id: input.overleafProjectId ?? null,
    overleaf_sync: "not_attested",
    limitations: [
      "This receipt covers a local compile only; it does not prove the local source was synchronized to Overleaf.",
      "A successful compiler exit does not prove citation resolution, visual quality, or editorial correctness.",
      "Shell escape is disabled, but TeX is not sandboxed: it can read or write files, including source-tree files, with the current user's permissions. Compile only sources you trust.",
      "System TeX packages are identified by the compiler environment and are not individually hashed.",
      ...input.prerequisiteNotes,
      ...(input.sourceChanged ? ["A local source file changed while compiling; the source manifest is incomplete."] : []),
      ...(input.unbaselinedInputs.length > 0 ? ["The TeX recorder found local project input(s) without a precompile hash baseline; source integrity cannot be confirmed."] : []),
    ],
  };
}

/** Compile a local source tree without contacting Overleaf. This is not a TeX sandbox. */
export async function compileLocalLatex(options: CompileLocalLatexOptions): Promise<CompileReceipt> {
  const engine = options.engine ?? "pdflatex";
  if (!ENGINE_NAMES.includes(engine)) throw new Error(`unsupported TeX engine: ${String(engine)}`);
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 30 * 60_000) throw new Error("timeoutMs must be between 1 and 1800000");
  if (options.overleafProjectId !== undefined && !/^[a-zA-Z0-9_-]{1,128}$/.test(options.overleafProjectId)) throw new Error("overleafProjectId must be a simple Overleaf project identifier");

  const rootFileInput = resolve(options.rootFile);
  const projectRoot = resolve(options.projectRoot ?? dirname(rootFileInput));
  const sources = await collectSources(rootFileInput, projectRoot);
  const stateDir = options.stateDir ?? join(homedir(), ".meridian-latex");
  const identity = options.overleafProjectId ? `overleaf:${options.overleafProjectId}` : `local:${sources.projectRoot}`;
  const receiptsDir = receiptDirectory(stateDir, identity);
  // Isolate every invocation so concurrent runs of one project cannot
  // overwrite each other's .aux/.fls/PDF or reuse stale artifacts.
  const buildDir = join(stateDir, "build", sha256(`local:${sources.projectRoot}`), randomUUID());
  await fs.mkdir(buildDir, { recursive: true, mode: 0o700 });
  const recorderBaseline = await captureRecorderBaseline(sources.projectRoot, sources.files, join(stateDir, "build"));
  const runner = options.runCommand ?? runBoundedCommand;
  const args = ["-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-recorder", `-output-directory=${buildDir}`, sources.rootFile];
  const phases: CompileReceipt["phases"] = [];
  const compilerVersion = await runner(engine, ["--version"], sources.projectRoot, Math.min(timeoutMs, 15_000));
  phases.push(phase(`${engine} --version`, compilerVersion));
  let status: CompileStatus = "failed";
  const prerequisiteNotes: string[] = [];
  let version: string | null = compilerVersion.stdout.split(/\r?\n/).find((line) => line.trim())?.trim() ?? null;
  let pdfHash: string | null = null;
  let logHash: string | null = null;
  let recordedInputs: string[] = [];

  if (compilerVersion.errorCode === "ENOENT") {
    status = "unavailable";
    prerequisiteNotes.push(`Required TeX engine "${engine}" was not found on PATH. Install a TeX distribution that provides it.`);
  } else if (compilerVersion.exitCode !== 0 || !version) {
    status = "unavailable";
    prerequisiteNotes.push(`Could not identify TeX engine "${engine}". Check that its installation is healthy and the executable is on PATH.`);
  }
  else {
    const initial = await runner(engine, args, sources.projectRoot, timeoutMs);
    phases.push(phase(engine, initial));
    if (initial.errorCode === "ENOENT") {
      status = "unavailable";
      prerequisiteNotes.push(`Required TeX engine "${engine}" could not be started. Check its installation and PATH.`);
    }
    else if (initial.exitCode === 0) {
      let bibliographyOk = true;
      if (sources.bibliographyBackend !== "none") {
        const bibtool = sources.bibliographyBackend;
        const bibVersion = await runner(bibtool, ["--version"], buildDir, Math.min(timeoutMs, 15_000));
        phases.push(phase(`${bibtool} --version`, bibVersion));
        if (bibVersion.errorCode === "ENOENT") {
          bibliographyOk = false;
          status = "unavailable";
          prerequisiteNotes.push(`Bibliography backend "${bibtool}" was not found on PATH. Install the matching BibTeX/Biber tool for this document.`);
        } else if (bibVersion.exitCode !== 0) {
          bibliographyOk = false;
          status = "failed";
        } else {
          const jobName = basename(sources.rootFile, extname(sources.rootFile));
          const bibliographyEnv = { ...process.env, BIBINPUTS: `${sources.projectRoot}${delimiter}${process.env.BIBINPUTS ?? ""}` };
          const bib = await runner(bibtool, [jobName], buildDir, timeoutMs, bibliographyEnv);
          phases.push(phase(bibtool, bib));
          bibliographyOk = bib.exitCode === 0;
          if (!bibliographyOk) {
            status = bib.errorCode === "ENOENT" ? "unavailable" : "failed";
            if (bib.errorCode === "ENOENT") prerequisiteNotes.push(`Bibliography backend "${bibtool}" could not be started. Check its installation and PATH.`);
          }
        }
      }
      if (bibliographyOk) {
        const passCount = sources.bibliographyBackend === "none" ? 1 : 2;
        status = "passed";
        for (let pass = 0; pass < passCount; pass += 1) {
          const result = await runner(engine, args, sources.projectRoot, timeoutMs);
          phases.push(phase(`${engine} pass ${pass + 2}`, result));
          if (result.errorCode === "ENOENT") {
            status = "unavailable";
            prerequisiteNotes.push(`TeX engine "${engine}" could not be restarted for another compile pass.`);
          }
          else if (result.exitCode !== 0) status = "failed";
          if (status !== "passed") break;
        }
      }
    } else status = initial.errorCode === "TIMEOUT" ? "failed" : "failed";
  }

  const jobName = basename(sources.rootFile, extname(sources.rootFile));
  const pdfPath = join(buildDir, `${jobName}.pdf`);
  const logPath = join(buildDir, `${jobName}.log`);
  try { pdfHash = sha256(await fs.readFile(pdfPath)); } catch { /* absent output is recorded honestly */ }
  try { logHash = sha256(await fs.readFile(logPath)); } catch { /* absent output is recorded honestly */ }
  try {
    const fls = await fs.readFile(join(buildDir, `${jobName}.fls`), "utf8");
    recordedInputs = fls.split(/\r?\n/).flatMap((line) => line.startsWith("INPUT ") ? [line.slice(6).trim()] : []);
  } catch { /* a compiler failure can occur before it creates a recorder file */ }
  const finalSources = recordedInputs.length > 0 ? await collectSources(sources.rootFile, sources.projectRoot, recordedInputs) : sources;
  const sourceChanged = [...sources.files.entries()].some(([path, before]) => finalSources.files.get(path)?.sha256 !== before.sha256)
    || [...finalSources.files.entries()].some(([path, after]) => recorderBaseline.has(path) && recorderBaseline.get(path) !== after.sha256);
  const unbaselinedInputs = [...finalSources.files.keys()].filter((path) => !recorderBaseline.has(path));
  if (status === "passed" && !pdfHash) status = "failed";
  if (status === "passed" && (sourceChanged || unbaselinedInputs.length > 0)) status = "incomplete";
  const receipt = buildReceipt({
    status,
    rootFile: safeRelativePath(finalSources.projectRoot, finalSources.rootFile),
    files: [...finalSources.files.values()],
    complete: finalSources.unresolved.length === 0 && recordedInputs.length > 0 && !sourceChanged && unbaselinedInputs.length === 0,
    unresolved: finalSources.unresolved,
    engine,
    version,
    flags: ["-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-recorder", "-output-directory=<local-build-dir>"],
    bibliographyBackend: finalSources.bibliographyBackend,
    phases,
    pdfPath: pdfHash ? pdfPath : null,
    pdfHash,
    logHash,
    overleafProjectId: options.overleafProjectId,
    sourceChanged,
    unbaselinedInputs,
    prerequisiteNotes,
  });
  await persistReceipt(receipt, receiptsDir);
  return receipt;
}

export interface CompileReceiptSummary {
  status: CompileStatus;
  created_at: string;
  root_file: string;
  engine: LatexEngine;
  engine_version: string | null;
  manifest_sha256: string;
  source_manifest_complete: boolean;
  pdf_sha256: string | null;
  overleaf_sync: "not_attested";
}

/** Return only path-free receipt metadata for the popup's explicit project link. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/i.test(value);
}

function isSafeRelativePath(value: unknown): value is string {
  if (typeof value !== "string" || !value) return false;
  const portable = value.replace(/\\/g, "/");
  return !isAbsolute(value) && !/^(?:[a-z]:|\/)/i.test(portable) && !portable.split("/").includes("..");
}

function isCompileReceipt(value: unknown, overleafProjectId: string): value is CompileReceipt {
  if (!isRecord(value)) return false;
  const manifest = value.source_manifest;
  const compiler = value.compiler;
  const environment = value.environment;
  const output = value.output;
  const validStatuses: CompileStatus[] = ["passed", "failed", "incomplete", "unavailable"];
  const validEngines: LatexEngine[] = ["pdflatex", "xelatex", "lualatex"];
  const validBackends: BibliographyBackend[] = ["bibtex", "bibtex8", "biber", "none"];
  const validFileKinds: CompileManifestFile["kind"][] = ["tex", "bibliography", "class", "style", "asset", "other"];

  if (value.schema_version !== 1 || value.overleaf_project_id !== overleafProjectId || value.overleaf_sync !== "not_attested") return false;
  if (typeof value.receipt_id !== "string" || !value.receipt_id || typeof value.created_at !== "string" || !Number.isFinite(Date.parse(value.created_at))) return false;
  if (!validStatuses.includes(value.status as CompileStatus) || !isSafeRelativePath(value.root_file)) return false;
  if (!isRecord(manifest) || typeof manifest.complete !== "boolean" || !Number.isInteger(manifest.unresolved_count) || (manifest.unresolved_count as number) < 0 || !isSha256(manifest.sha256)) return false;
  if (!Array.isArray(manifest.files) || !manifest.files.every((file) => isRecord(file)
    && typeof file.path === "string" && !!file.path
    && validFileKinds.includes(file.kind as CompileManifestFile["kind"])
    && isSha256(file.sha256)
    && Number.isInteger(file.size_bytes) && (file.size_bytes as number) >= 0)) return false;
  if (!isRecord(compiler) || !validEngines.includes(compiler.engine as LatexEngine)
    || !(compiler.version === null || typeof compiler.version === "string")
    || !Array.isArray(compiler.flags) || !compiler.flags.every((flag) => typeof flag === "string")
    || !validBackends.includes(compiler.bibliography_backend as BibliographyBackend)) return false;
  if (!isRecord(environment) || typeof environment.platform !== "string" || typeof environment.architecture !== "string" || typeof environment.node_version !== "string") return false;
  if (!Array.isArray(value.phases) || !value.phases.every((phase) => isRecord(phase)
    && typeof phase.tool === "string"
    && (phase.exit_code === null || Number.isInteger(phase.exit_code))
    && typeof phase.duration_ms === "number" && Number.isFinite(phase.duration_ms) && phase.duration_ms >= 0
    && isSha256(phase.stdout_sha256) && isSha256(phase.stderr_sha256))) return false;
  if (!isRecord(output)
    || !(output.pdf_path === null || typeof output.pdf_path === "string")
    || !(output.pdf_sha256 === null || isSha256(output.pdf_sha256))
    || !(output.log_sha256 === null || isSha256(output.log_sha256))) return false;
  return Array.isArray(value.limitations) && value.limitations.every((limitation) => typeof limitation === "string");
}

export async function getLatestCompileReceiptSummary(
  overleafProjectId: string,
  stateDir = join(homedir(), ".meridian-latex"),
): Promise<CompileReceiptSummary | null> {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(overleafProjectId)) return null;
  const directory = receiptDirectory(stateDir, `overleaf:${overleafProjectId}`);
  let names: string[];
  try { names = await fs.readdir(directory); } catch { return null; }
  const receipts: CompileReceipt[] = [];
  for (const name of names.filter((entry) => entry.endsWith(".json")).sort().reverse()) {
    try {
      const parsed: unknown = JSON.parse(await fs.readFile(join(directory, name), "utf8"));
      if (isCompileReceipt(parsed, overleafProjectId)) receipts.push(parsed);
    } catch { /* Ignore a partial/invalid local receipt rather than showing it as evidence. */ }
  }
  const latest = receipts.sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  if (!latest) return null;
  return {
    status: latest.status,
    created_at: latest.created_at,
    root_file: latest.root_file,
    engine: latest.compiler.engine,
    engine_version: latest.compiler.version,
    manifest_sha256: latest.source_manifest.sha256,
    source_manifest_complete: latest.source_manifest.complete,
    pdf_sha256: latest.output.pdf_sha256,
    overleaf_sync: "not_attested",
  };
}

export async function getWorkflowStatusPayload(
  overleafProjectId: string,
  stateDir = join(homedir(), ".meridian-latex"),
) {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(overleafProjectId)) throw new Error("invalid Overleaf project identifier");
  return {
    ok: true as const,
    localCompile: {
      command: "meridian-latex compile <main.tex> --project-id=<id>",
      receipt: await getLatestCompileReceiptSummary(overleafProjectId, stateDir),
      note: "Compile receipts describe local files only; Overleaf sync is not attested.",
    },
  };
}
