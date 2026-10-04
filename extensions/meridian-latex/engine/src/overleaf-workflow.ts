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
import { constants } from "node:fs";
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
const MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024;
const MAX_SNAPSHOT_FILES = 20_000;
const MAX_SNAPSHOT_DIRECTORIES = 4_096;
const MAX_SNAPSHOT_ENTRIES = 100_000;
const SNAPSHOT_READ_CHUNK_BYTES = 64 * 1024;
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

interface CompileSnapshot {
  sourceRoot: string;
  files: Map<string, string>;
  directories: string[];
  complete: boolean;
  limitReason: string | null;
}

interface SnapshotProtection {
  enforced: boolean;
  reason: string | null;
  release(): Promise<boolean>;
}

interface ProcessOutput {
  exitCode: number | null;
  stdout: string;
}

interface RecorderPaths {
  inputs: string[];
  bypassedProjectInputs: string[];
  externalInputs: string[];
}

interface TeXDistributionResolution {
  roots: string[];
  reason: string | null;
  phaseResults: Array<{ tool: string; result: CommandResult }>;
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

async function readFileBounded(path: string, maximumBytes: number): Promise<Buffer> {
  const file = await fs.open(path, "r");
  const chunks: Buffer[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const remaining = maximumBytes - totalBytes;
      // Read at most one byte beyond the remaining allowance to distinguish
      // an exactly-at-limit file from a file that grew while it was being read.
      const buffer = Buffer.allocUnsafe(Math.min(SNAPSHOT_READ_CHUNK_BYTES, remaining + 1));
      const { bytesRead } = await file.read(buffer, 0, buffer.byteLength, null);
      if (bytesRead === 0) break;
      if (bytesRead > remaining) throw new Error("file exceeds bounded read limit (" + maximumBytes + " bytes)");
      chunks.push(buffer.subarray(0, bytesRead));
      totalBytes += bytesRead;
    }
    return Buffer.concat(chunks, totalBytes);
  } finally {
    await file.close();
  }
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
    const data = await readFileBounded(file, MAX_SNAPSHOT_BYTES);
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
    if (!inside(realRoot, candidate)) continue;
    const normalizedCandidate = resolve(candidate);
    const missingInput = () => {
      const path = safeRelativePath(realRoot, normalizedCandidate);
      unresolved.push({
        source: path,
        command: "recorder_input",
        reason: "missing",
        digest: sha256(`recorder-input:${path}`),
      });
    };
    try {
      const actual = await fs.realpath(candidate);
      if (!inside(realRoot, actual)) {
        const path = safeRelativePath(realRoot, normalizedCandidate);
        unresolved.push({
          source: path,
          command: "recorder_input",
          reason: "outside_project",
          digest: sha256(`recorder-input:${path}`),
        });
        continue;
      }
      const stat = await fs.stat(actual);
      if (!stat.isFile()) {
        missingInput();
        continue;
      }
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
      // A recorder-listed in-project file may have disappeared after TeX read
      // it. Keep that path in the integrity result instead of silently
      // dropping it and accidentally treating the remaining manifest complete.
      missingInput();
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

async function runProcess(executable: string, args: string[]): Promise<ProcessOutput> {
  return new Promise((resolveResult) => {
    let stdout = "";
    let settled = false;
    let timer: NodeJS.Timeout | undefined;
    const finish = (exitCode: number | null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolveResult({ exitCode, stdout });
    };
    let child;
    try {
      child = spawn(executable, args, { shell: false, windowsHide: true, stdio: ["ignore", "pipe", "ignore"] });
    } catch {
      finish(null);
      return;
    }
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout = (stdout + chunk.toString("utf8")).slice(-8_192);
    });
    timer = setTimeout(() => {
      child.kill();
      finish(null);
    }, 15_000);
    child.once("error", () => finish(null));
    child.once("close", (code) => finish(code));
  });
}

async function hashFileStream(path: string, maximumBytes = MAX_SNAPSHOT_BYTES): Promise<string> {
  const file = await fs.open(path, "r");
  const digest = createHash("sha256");
  const buffer = Buffer.allocUnsafe(SNAPSHOT_READ_CHUNK_BYTES);
  let totalBytes = 0;
  try {
    while (true) {
      const remaining = maximumBytes - totalBytes;
      const readLength = Math.min(buffer.byteLength, remaining + 1);
      const { bytesRead } = await file.read(buffer, 0, readLength, null);
      if (bytesRead === 0) break;
      if (bytesRead > remaining) throw new Error("file exceeds bounded hash limit (" + maximumBytes + " bytes)");
      digest.update(buffer.subarray(0, bytesRead));
      totalBytes += bytesRead;
    }
    return digest.digest("hex");
  } finally {
    await file.close();
  }
}

function sameFileIdentity(left: { dev: number; ino: number }, right: { dev: number; ino: number }): boolean {
  return left.dev === right.dev && left.ino !== 0 && left.ino === right.ino;
}

async function copyFileStream(source: string, target: string, projectRoot: string, remainingBytes: number): Promise<{ hash: string; bytes: number }> {
  const linkStat = await fs.lstat(source);
  if (!linkStat.isFile() || linkStat.isSymbolicLink()) throw new Error("source path is not a regular file");
  const actualSource = await fs.realpath(source);
  if (!inside(projectRoot, actualSource)) throw new Error("source path escaped the project root");
  const sourceFile = await fs.open(actualSource, process.platform === "win32"
    ? "r"
    : constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  let targetFile;
  try {
    const openedStat = await sourceFile.stat();
    if (!openedStat.isFile() || !sameFileIdentity(linkStat, openedStat)) throw new Error("source path changed while opening");
    await fs.mkdir(dirname(target), { recursive: true, mode: 0o700 });
    targetFile = await fs.open(target, "wx", 0o600);
    const buffer = Buffer.allocUnsafe(SNAPSHOT_READ_CHUNK_BYTES);
    const digest = createHash("sha256");
    let bytes = 0;
    while (true) {
      const remaining = remainingBytes - bytes;
      const readLength = Math.min(buffer.byteLength, remaining + 1);
      const { bytesRead } = await sourceFile.read(buffer, 0, readLength, null);
      if (bytesRead === 0) break;
      if (bytesRead > remaining) throw new Error("byte limit (" + MAX_SNAPSHOT_BYTES + ")");
      const chunk = buffer.subarray(0, bytesRead);
      let written = 0;
      while (written < bytesRead) {
        const result = await targetFile.write(chunk, written, bytesRead - written, null);
        if (result.bytesWritten <= 0) throw new Error("snapshot write made no progress");
        written += result.bytesWritten;
      }
      digest.update(chunk);
      bytes += bytesRead;
    }
    return { hash: digest.digest("hex"), bytes };
  } finally {
    await sourceFile.close();
    if (targetFile) await targetFile.close();
  }
}

async function captureCompileSnapshot(
  projectRoot: string,
  rootFile: string,
  snapshotRoot: string,
  excludedBuildDir: string,
): Promise<CompileSnapshot> {
  const root = await fs.realpath(projectRoot);
  const main = await fs.realpath(rootFile);
  if (!inside(root, main)) throw new Error("root .tex file must be inside the selected project root");
  const relativeMain = relative(root, main);
  const files = new Map<string, string>();
  const directories = new Set<string>([""]);
  const pending = [root];
  const visitedDirectories = new Set<string>([root]);
  const excluded = resolve(excludedBuildDir);
  let scannedDirectories = 0;
  let scannedEntries = 0;
  let scannedBytes = 0;
  let limitReason: string | null = null;
  await fs.mkdir(snapshotRoot, { recursive: true, mode: 0o700 });

  const copyOne = async (source: string, relativePath: string): Promise<void> => {
    if (files.has(relativePath)) return;
    if (files.size >= MAX_SNAPSHOT_FILES) {
      limitReason = "file limit (" + MAX_SNAPSHOT_FILES + ")";
      return;
    }
    try {
      const copied = await copyFileStream(source, join(snapshotRoot, relativePath), root, MAX_SNAPSHOT_BYTES - scannedBytes);
      scannedBytes += copied.bytes;
      files.set(relativePath, copied.hash);
    } catch (error) {
      const message = error instanceof Error ? error.message : "source copy failed";
      limitReason = message.startsWith("byte limit") ? message : "unsafe or unreadable source file";
      await fs.rm(join(snapshotRoot, relativePath), { force: true }).catch(() => undefined);
    }
  };

  await copyOne(main, relativeMain);
  if (!files.has(relativeMain)) {
    throw new Error("root .tex file could not be copied into the bounded compile snapshot: " + (limitReason ?? "unknown error"));
  }

  while (pending.length > 0 && !limitReason) {
    if (scannedDirectories >= MAX_SNAPSHOT_DIRECTORIES) {
      limitReason = "directory limit (" + MAX_SNAPSHOT_DIRECTORIES + ")";
      break;
    }
    scannedDirectories += 1;
    const directoryPath = pending.pop()!;
    let directory;
    try {
      directory = await fs.opendir(directoryPath);
    } catch {
      limitReason = "unreadable project directory";
      break;
    }
    for await (const entry of directory) {
      if (scannedEntries >= MAX_SNAPSHOT_ENTRIES) {
        limitReason = "entry limit (" + MAX_SNAPSHOT_ENTRIES + ")";
        break;
      }
      scannedEntries += 1;
      const child = join(directoryPath, entry.name);
      if (inside(excluded, child)) continue;
      if (entry.isSymbolicLink()) {
        limitReason = "symbolic link encountered";
        break;
      }
      if (entry.isDirectory()) {
        if (entry.name === ".git" || entry.name.toLowerCase() === "node_modules") continue;
        try {
          const linkStat = await fs.lstat(child);
          if (!linkStat.isDirectory() || linkStat.isSymbolicLink()) {
            limitReason = "unsafe project directory entry";
            break;
          }
          const actualDirectory = await fs.realpath(child);
          if (!inside(root, actualDirectory)) {
            limitReason = "project directory escaped the selected root";
            break;
          }
          if (!visitedDirectories.has(actualDirectory)) {
            visitedDirectories.add(actualDirectory);
            const relativeDirectory = relative(root, actualDirectory);
            directories.add(relativeDirectory);
            await fs.mkdir(join(snapshotRoot, relativeDirectory), { recursive: true, mode: 0o700 });
            pending.push(actualDirectory);
          }
        } catch {
          limitReason = "unsafe or unreadable project directory";
          break;
        }
        continue;
      }
      if (entry.isFile()) {
        const relativePath = relative(root, child);
        await copyOne(child, relativePath);
        if (limitReason) break;
        continue;
      }
      limitReason = "unsupported filesystem entry";
      break;
    }
  }

  return { sourceRoot: snapshotRoot, files, directories: [...directories], complete: limitReason === null, limitReason };
}

async function protectCompileSnapshot(snapshot: CompileSnapshot): Promise<SnapshotProtection> {
  if (process.platform === "win32") {
    const identity = await runProcess("whoami", ["/user", "/fo", "csv", "/nh"]);
    const sid = identity.stdout.match(/S-1-(?:\d+-)+\d+/)?.[0];
    if (identity.exitCode !== 0 || !sid) {
      return { enforced: false, reason: "current Windows user identity could not be resolved", release: async () => true };
    }
    const principal = "*" + sid;
    const applied = await runProcess("icacls", [snapshot.sourceRoot, "/deny", principal + ":(OI)(CI)(WD,AD,WEA,WA)", "/t", "/c"]);
    if (applied.exitCode !== 0) {
      return {
        enforced: false,
        reason: "Windows denied-write ACL could not be applied to the compile snapshot",
        release: async () => (await runProcess("icacls", [snapshot.sourceRoot, "/remove:d", principal, "/t", "/c"])).exitCode === 0,
      };
    }
    let blocked = false;
    const probe = join(snapshot.sourceRoot, ".meridian-write-probe-" + randomUUID());
    try {
      await fs.writeFile(probe, "probe", { flag: "wx" });
      await fs.rm(probe, { force: true });
    } catch (error) {
      blocked = ["EACCES", "EPERM"].includes((error as NodeJS.ErrnoException).code ?? "");
    }
    return {
      enforced: blocked,
      reason: blocked ? null : "Windows filesystem did not enforce the compile snapshot write denial",
      release: async () => (await runProcess("icacls", [snapshot.sourceRoot, "/remove:d", principal, "/t", "/c"])).exitCode === 0,
    };
  }

  try {
    for (const path of snapshot.files.keys()) await fs.chmod(join(snapshot.sourceRoot, path), 0o444);
    for (const path of [...snapshot.directories].sort((a, b) => b.length - a.length)) {
      await fs.chmod(path ? join(snapshot.sourceRoot, path) : snapshot.sourceRoot, 0o555);
    }
  } catch {
    return { enforced: false, reason: "read-only permissions could not be applied to the compile snapshot", release: async () => true };
  }
  let blocked = false;
  const probe = join(snapshot.sourceRoot, ".meridian-write-probe-" + randomUUID());
  try {
    await fs.writeFile(probe, "probe", { flag: "wx" });
    await fs.rm(probe, { force: true });
  } catch (error) {
    blocked = ["EACCES", "EPERM"].includes((error as NodeJS.ErrnoException).code ?? "");
  }
  return {
    enforced: blocked,
    reason: blocked ? null : "filesystem did not enforce read-only permissions on the compile snapshot",
    release: async () => {
      try {
        for (const path of snapshot.directories) {
          await fs.chmod(path ? join(snapshot.sourceRoot, path) : snapshot.sourceRoot, 0o700);
        }
        for (const path of snapshot.files.keys()) await fs.chmod(join(snapshot.sourceRoot, path), 0o600);
        return true;
      } catch {
        return false;
      }
    },
  };
}

async function verifyCompileSnapshot(snapshot: CompileSnapshot): Promise<boolean> {
  for (const [relativePath, expectedHash] of snapshot.files) {
    try {
      const path = join(snapshot.sourceRoot, relativePath);
      const linkStat = await fs.lstat(path);
      if (!linkStat.isFile() || linkStat.isSymbolicLink()) return false;
      const actualPath = await fs.realpath(path);
      if (!inside(snapshot.sourceRoot, actualPath) || await hashFileStream(actualPath) !== expectedHash) return false;
    } catch {
      return false;
    }
  }
  return true;
}

async function compareOriginalProjectToSnapshot(projectRoot: string, snapshot: CompileSnapshot): Promise<boolean> {
  const root = await fs.realpath(projectRoot);
  for (const [relativePath, expectedHash] of snapshot.files) {
    try {
      const path = join(root, relativePath);
      const linkStat = await fs.lstat(path);
      if (!linkStat.isFile() || linkStat.isSymbolicLink()) return true;
      const actualPath = await fs.realpath(path);
      if (!inside(root, actualPath) || await hashFileStream(actualPath) !== expectedHash) return true;
    } catch {
      return true;
    }
  }
  return false;
}

function pathKey(path: string): string {
  const normalized = resolve(path);
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

function executableExtensions(env: NodeJS.ProcessEnv): string[] {
  if (process.platform !== "win32") return [""];
  const extensions = (env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").filter(Boolean);
  return extensions.length > 0 ? extensions : [".EXE"];
}

async function usableExecutable(path: string): Promise<string | null> {
  try {
    const realPath = await fs.realpath(path);
    const stat = await fs.stat(realPath);
    if (!stat.isFile()) return null;
    await fs.access(realPath, process.platform === "win32" ? undefined : constants.X_OK);
    return realPath;
  } catch {
    return null;
  }
}

async function resolveExecutableInPath(name: string, env: NodeJS.ProcessEnv, cwd: string): Promise<string | null> {
  const pathValue = env.PATH ?? env.Path ?? "";
  const extensions = executableExtensions(env);
  for (const entry of pathValue.split(delimiter)) {
    if (!entry) continue;
    const directory = isAbsolute(entry) ? entry : resolve(cwd, entry);
    for (const extension of extensions) {
      const candidate = join(directory, name.toLowerCase().endsWith(extension.toLowerCase()) ? name : name + extension);
      const usable = await usableExecutable(candidate);
      if (usable) return usable;
    }
  }
  return null;
}

async function resolveExecutableBeside(name: string, siblingPath: string, env: NodeJS.ProcessEnv): Promise<string | null> {
  const extensions = executableExtensions(env);
  const directory = dirname(siblingPath);
  for (const extension of extensions) {
    const usable = await usableExecutable(join(directory, name.toLowerCase().endsWith(extension.toLowerCase()) ? name : name + extension));
    if (usable) return usable;
  }
  return null;
}

async function resolveTeXDistributionRoots(
  engine: LatexEngine,
  env: NodeJS.ProcessEnv,
  cwd: string,
  timeoutMs: number,
  invoke: (executable: string, args: string[], cwd: string, commandTimeout: number, env?: NodeJS.ProcessEnv) => Promise<CommandResult>,
): Promise<TeXDistributionResolution> {
  const enginePath = await resolveExecutableInPath(engine, env, cwd);
  if (!enginePath) return { roots: [], reason: "The active TeX engine could not be resolved on PATH to verify its distribution.", phaseResults: [] };
  const kpsewhich = await resolveExecutableBeside("kpsewhich", enginePath, env);
  if (!kpsewhich) return { roots: [], reason: "The active TeX engine has no sibling kpsewhich; its TeX distribution roots cannot be verified.", phaseResults: [] };

  // Ignore user-supplied TEXMF overrides and query from the engine's own bin
  // directory so a project-local texmf.cnf cannot define trusted roots. The
  // compile still inherits the caller's actual environment; inputs outside
  // these verified system roots are treated as external.
  const kpseEnv: NodeJS.ProcessEnv = { ...env };
  for (const key of Object.keys(kpseEnv)) if (/^TEXMF/i.test(key)) delete kpseEnv[key];
  const distributionCwd = dirname(kpsewhich);
  kpseEnv.PWD = distributionCwd;
  const variables = ["TEXMFDIST", "TEXMFROOT", "TEXMFMAIN", "TEXMFSYSVAR", "TEXMFSYSCONFIG", "TEXMFLOCAL"];
  const roots = new Map<string, string>();
  const phaseResults: TeXDistributionResolution["phaseResults"] = [];
  let requiredRootError: string | null = null;
  const lookups = await Promise.all(variables.map(async (variable) => ({
    variable,
    result: await invoke(kpsewhich, ["--var-value=" + variable], distributionCwd, Math.min(timeoutMs, 15_000), kpseEnv),
  })));
  for (const { variable, result } of lookups) {
    phaseResults.push({ tool: "kpsewhich --var-value=" + variable, result });
    if (result.exitCode !== 0) {
      if (variable === "TEXMFDIST") requiredRootError = "The active TeX distribution's kpsewhich could not resolve TEXMFDIST.";
      continue;
    }
    const reported = result.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (reported.length !== 1 || !isAbsolute(reported[0])) {
      if (variable === "TEXMFDIST") requiredRootError = "The active TeX distribution's kpsewhich returned an invalid TEXMFDIST path.";
      continue;
    }
    try {
      const root = await fs.realpath(reported[0]);
      if (!(await fs.stat(root)).isDirectory()) throw new Error("not a directory");
      roots.set(pathKey(root), root);
    } catch {
      if (variable === "TEXMFDIST") requiredRootError = "The active TeX distribution's kpsewhich returned a TEXMFDIST path that is not an accessible directory.";
    }
  }
  if (requiredRootError || !roots.size) {
    return {
      roots: [],
      reason: requiredRootError ?? "The active TeX distribution's kpsewhich returned no usable system roots.",
      phaseResults,
    };
  }
  return { roots: [...roots.values()], reason: null, phaseResults };
}

async function isVerifiedTeXDistributionInput(path: string, distributionRoots: string[]): Promise<boolean> {
  try {
    const actualPath = await fs.realpath(path);
    return distributionRoots.some((root) => inside(root, actualPath));
  } catch {
    return false;
  }
}

function redirectProjectSearchPath(value: string, originalRoot: string, snapshotRoot: string): string {
  return value.split(delimiter).map((entry) => {
    const prefix = entry.startsWith("!!") ? "!!" : "";
    const rawPath = prefix ? entry.slice(2) : entry;
    if (!rawPath || /[$*{}]/.test(rawPath)) return entry;
    const originalPath = isAbsolute(rawPath) ? resolve(rawPath) : resolve(originalRoot, rawPath);
    if (inside(originalRoot, originalPath)) return prefix + join(snapshotRoot, relative(originalRoot, originalPath));
    return prefix + originalPath;
  }).join(delimiter);
}

async function externalSearchPathEntries(value: string | undefined, originalRoot: string, snapshotRoot: string, excludedBuildDir: string, distributionRoots: string[]): Promise<string[]> {
  if (!value) return [];
  const external: string[] = [];
  for (const entry of value.split(delimiter).map((part) => part.trim().replace(/^!!/, "")).filter(Boolean)) {
    if (/[$*{}]/.test(entry)) {
      external.push(entry);
      continue;
    }
    const path = isAbsolute(entry) ? resolve(entry) : resolve(originalRoot, entry);
    if (inside(originalRoot, path)) {
      const firstPart = relative(originalRoot, path).split(sep)[0]?.toLowerCase();
      if (inside(excludedBuildDir, path) || firstPart === ".git" || firstPart === "node_modules") external.push(path);
    } else if (!inside(snapshotRoot, path) && !(await isVerifiedTeXDistributionInput(path, distributionRoots))) {
      external.push(path);
    }
  }
  return external;
}

async function parseRecorderPaths(
  contents: string,
  compileRoot: string,
  originalRoot: string,
  snapshotRoot: string,
  buildDir: string,
  distributionRoots: string[],
  knownGeneratedOutputs: string[],
): Promise<RecorderPaths> {
  let recorderCwd = compileRoot;
  const lines = contents.split(/\r?\n/);
  for (const line of lines) {
    if (line.startsWith("PWD ")) {
      const reported = line.slice(4).trim();
      recorderCwd = isAbsolute(reported) ? resolve(reported) : resolve(compileRoot, reported);
      break;
    }
  }
  const result: RecorderPaths = { inputs: [], bypassedProjectInputs: [], externalInputs: [] };
  const recorderOutputs = new Set<string>();
  for (const line of lines) {
    if (!line.startsWith("OUTPUT ")) continue;
    const raw = line.slice(7).trim();
    if (!raw) continue;
    const candidates = isAbsolute(raw) ? [resolve(raw)] : [resolve(buildDir, raw), resolve(recorderCwd, raw)];
    for (const candidate of candidates) {
      if (inside(buildDir, candidate) && !inside(snapshotRoot, candidate)) recorderOutputs.add(pathKey(candidate));
    }
  }
  for (const path of knownGeneratedOutputs) {
    if (inside(buildDir, path) && !inside(snapshotRoot, path)) recorderOutputs.add(pathKey(path));
  }
  for (const line of lines) {
    if (!line.startsWith("INPUT ")) continue;
    const raw = line.slice(6).trim();
    if (!raw) continue;
    const candidate = isAbsolute(raw) ? resolve(raw) : resolve(recorderCwd, raw);
    if (inside(snapshotRoot, candidate)) {
      result.inputs.push(candidate);
    } else if (inside(buildDir, candidate)) {
      if (recorderOutputs.has(pathKey(candidate)) && await isVerifiedCurrentJobOutput(candidate, buildDir, snapshotRoot)) {
        // The current job declared this output and it still resolves to a
        // regular file in the private per-run build directory.
      } else {
        result.externalInputs.push(candidate);
      }
    } else if (inside(originalRoot, candidate)) {
      result.bypassedProjectInputs.push(candidate);
    } else if (await isVerifiedTeXDistributionInput(candidate, distributionRoots)) {
      // System distribution files are outside the local project source manifest.
    } else {
      result.externalInputs.push(candidate);
    }
  }
  return result;
}

async function isVerifiedCurrentJobOutput(path: string, buildDir: string, snapshotRoot: string): Promise<boolean> {
  try {
    const info = await fs.lstat(path);
    if (!info.isFile() || info.isSymbolicLink()) return false;
    const actualPath = await fs.realpath(path);
    return inside(buildDir, actualPath) && !inside(snapshotRoot, actualPath);
  } catch {
    return false;
  }
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
  snapshotLimitReason: string | null;
  snapshotProtectionReason: string | null;
  snapshotIntegrityVerified: boolean;
  snapshotProtectionReleased: boolean;
  unbaselinedInputs: string[];
  bypassedProjectInputs: string[];
  externalInputs: string[];
  recorderIncompleteReason: string | null;
  distributionRootsReason: string | null;
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
      "Verified TeX distribution inputs outside the project snapshot are not individually hashed.",
      ...input.prerequisiteNotes,
      ...(input.sourceChanged ? ["A local source file changed while compiling or disappeared; the source manifest is incomplete."] : []),
      ...(input.snapshotLimitReason ? ["The compile snapshot reached its " + input.snapshotLimitReason + "; source integrity cannot be confirmed."] : []),
      ...(input.snapshotProtectionReason ? [input.snapshotProtectionReason + "; source integrity cannot be confirmed."] : []),
      ...(!input.snapshotIntegrityVerified ? ["The compile snapshot changed or could not be verified after compilation; source integrity cannot be confirmed."] : []),
      ...(!input.snapshotProtectionReleased ? ["Compile snapshot write protection could not be released cleanly."] : []),
      ...(input.unbaselinedInputs.length > 0 ? ["The TeX recorder found local project input(s) absent from the compile snapshot; source integrity cannot be confirmed."] : []),
      ...(input.bypassedProjectInputs.length > 0 ? ["The TeX recorder found input(s) from the original project path outside the compile snapshot; source integrity cannot be confirmed."] : []),
      ...(input.externalInputs.length > 0 ? ["The TeX recorder or bibliography search configuration includes input(s) outside the compile snapshot and TeX distribution; source integrity cannot be confirmed."] : []),
      ...(input.recorderIncompleteReason ? [input.recorderIncompleteReason] : []),
      ...(input.distributionRootsReason ? [input.distributionRootsReason + "; source integrity cannot be confirmed."] : []),
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
  const originalRoot = await fs.realpath(projectRoot);
  const originalMain = await fs.realpath(rootFileInput);
  if (!inside(originalRoot, originalMain)) throw new Error("root .tex file must be inside the selected project root");
  const relativeMain = relative(originalRoot, originalMain);
  const stateDir = options.stateDir ?? join(homedir(), ".meridian-latex");
  const identity = options.overleafProjectId ? "overleaf:" + options.overleafProjectId : "local:" + originalRoot;
  const receiptsDir = receiptDirectory(stateDir, identity);
  // Isolate every invocation so concurrent runs of one project cannot
  // overwrite each other's .aux/.fls/PDF or reuse stale artifacts.
  const buildDir = join(stateDir, "build", sha256("local:" + originalRoot), randomUUID());
  await fs.mkdir(buildDir, { recursive: true, mode: 0o700 });
  const snapshot = await captureCompileSnapshot(originalRoot, originalMain, join(buildDir, "source"), join(stateDir, "build"));
  const snapshotMain = join(snapshot.sourceRoot, relativeMain);
  const sources = await collectSources(snapshotMain, snapshot.sourceRoot);
  const snapshotProtection = await protectCompileSnapshot(snapshot);
  const runner = options.runCommand ?? runBoundedCommand;
  const invoke = async (executable: string, commandArgs: string[], cwd: string, commandTimeout: number, env?: NodeJS.ProcessEnv): Promise<CommandResult> => {
    try {
      return await runner(executable, commandArgs, cwd, commandTimeout, env);
    } catch (error) {
      return {
        exitCode: null,
        stdout: "",
        stderr: error instanceof Error ? error.message : "command runner failed",
        durationMs: 0,
        errorCode: "RUNNER_FAILED",
      };
    }
  };
  const compileEnv: NodeJS.ProcessEnv = {
    ...process.env,
    PWD: snapshot.sourceRoot,
    BIBINPUTS: snapshot.sourceRoot + delimiter + redirectProjectSearchPath(process.env.BIBINPUTS ?? "", originalRoot, snapshot.sourceRoot),
  };
  if (process.env.TEXINPUTS !== undefined) compileEnv.TEXINPUTS = redirectProjectSearchPath(process.env.TEXINPUTS, originalRoot, snapshot.sourceRoot);
  if (process.env.BSTINPUTS !== undefined) compileEnv.BSTINPUTS = redirectProjectSearchPath(process.env.BSTINPUTS, originalRoot, snapshot.sourceRoot);
  const args = ["-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-recorder", "-output-directory=" + buildDir, sources.rootFile];
  const phases: CompileReceipt["phases"] = [];
  const prerequisiteNotes: string[] = [];
  const distribution = await resolveTeXDistributionRoots(engine, compileEnv, snapshot.sourceRoot, timeoutMs, invoke);
  for (const distributionPhase of distribution.phaseResults) phases.push(phase(distributionPhase.tool, distributionPhase.result));
  if (distribution.reason) prerequisiteNotes.push(distribution.reason);
  const compilerVersion = await invoke(engine, ["--version"], snapshot.sourceRoot, Math.min(timeoutMs, 15_000), compileEnv);
  phases.push(phase(`${engine} --version`, compilerVersion));
  let status: CompileStatus = "failed";
  let version: string | null = compilerVersion.stdout.split(/\r?\n/).find((line) => line.trim())?.trim() ?? null;
  let pdfHash: string | null = null;
  let logHash: string | null = null;
  let recorderContents: string | null = null;
  let recorderReadFailure: string | null = null;
  const knownGeneratedOutputs: string[] = [];

  if (compilerVersion.errorCode === "ENOENT") {
    status = "unavailable";
    prerequisiteNotes.push(`Required TeX engine "${engine}" was not found on PATH. Install a TeX distribution that provides it.`);
  } else if (compilerVersion.exitCode !== 0 || !version) {
    status = "unavailable";
    prerequisiteNotes.push(`Could not identify TeX engine "${engine}". Check that its installation is healthy and the executable is on PATH.`);
  }
  else {
    const initial = await invoke(engine, args, snapshot.sourceRoot, timeoutMs, compileEnv);
    phases.push(phase(engine, initial));
    if (initial.errorCode === "ENOENT") {
      status = "unavailable";
      prerequisiteNotes.push(`Required TeX engine "${engine}" could not be started. Check its installation and PATH.`);
    }
    else if (initial.exitCode === 0) {
      let bibliographyOk = true;
      if (sources.bibliographyBackend !== "none") {
        const bibtool = sources.bibliographyBackend;
        const bibVersion = await invoke(bibtool, ["--version"], buildDir, Math.min(timeoutMs, 15_000), compileEnv);
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
          const bib = await invoke(bibtool, [jobName], buildDir, timeoutMs, compileEnv);
          phases.push(phase(bibtool, bib));
          bibliographyOk = bib.exitCode === 0;
          if (bibliographyOk) {
            knownGeneratedOutputs.push(join(buildDir, jobName + ".bbl"), join(buildDir, jobName + ".blg"));
          }
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
          const result = await invoke(engine, args, snapshot.sourceRoot, timeoutMs, compileEnv);
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
  const pdfPath = join(buildDir, jobName + ".pdf");
  const logPath = join(buildDir, jobName + ".log");
  try { pdfHash = await hashFileStream(pdfPath, Number.MAX_SAFE_INTEGER); } catch { /* absent output is recorded honestly */ }
  try { logHash = await hashFileStream(logPath, Number.MAX_SAFE_INTEGER); } catch { /* absent output is recorded honestly */ }
  try {
    recorderContents = (await readFileBounded(join(buildDir, jobName + ".fls"), 8 * 1024 * 1024)).toString("utf8");
  } catch (error) {
    recorderReadFailure = error instanceof Error && error.message.includes("bounded read limit")
      ? "The TeX recorder file exceeded the 8 MiB bounded read limit."
      : "The compiler did not produce a readable TeX recorder file.";
  }
  const recorderPaths = recorderContents
    ? await parseRecorderPaths(recorderContents, snapshot.sourceRoot, originalRoot, snapshot.sourceRoot, buildDir, distribution.roots, knownGeneratedOutputs)
    : { inputs: [], bypassedProjectInputs: [], externalInputs: [] };
  const externalSearchPaths = await Promise.all([
    externalSearchPathEntries(process.env.TEXINPUTS, originalRoot, snapshot.sourceRoot, buildDir, distribution.roots),
    externalSearchPathEntries(process.env.BIBINPUTS, originalRoot, snapshot.sourceRoot, buildDir, distribution.roots),
    externalSearchPathEntries(process.env.BSTINPUTS, originalRoot, snapshot.sourceRoot, buildDir, distribution.roots),
  ]);
  recorderPaths.externalInputs.push(...externalSearchPaths.flat());
  const recorderIncompleteReason = recorderReadFailure
    ?? (recorderPaths.inputs.length === 0 ? "The TeX recorder did not list any inputs from the compile snapshot." : null);
  let finalSources = sources;
  try {
    if (recorderPaths.inputs.length > 0) {
      finalSources = await collectSources(sources.rootFile, snapshot.sourceRoot, recorderPaths.inputs);
    }
  } catch {
    finalSources.unresolved.push({
      source: safeRelativePath(snapshot.sourceRoot, sources.rootFile),
      command: "recorder_input",
      reason: "missing",
      digest: sha256("recorder-input-collection-failed"),
    });
  }
  for (const path of recorderPaths.bypassedProjectInputs) {
    const relativePath = safeRelativePath(originalRoot, path);
    finalSources.unresolved.push({
      source: relativePath,
      command: "recorder_input",
      reason: "outside_project",
      digest: sha256("snapshot-bypass:" + relativePath),
    });
  }
  for (const path of recorderPaths.externalInputs) {
    finalSources.unresolved.push({
      source: "<external>",
      command: "external_path",
      reason: "outside_project",
      digest: sha256("external-recorder-input:" + path),
    });
  }
  if (recorderIncompleteReason) {
    finalSources.unresolved.push({
      source: safeRelativePath(snapshot.sourceRoot, sources.rootFile),
      command: "recorder_input",
      reason: "missing",
      digest: sha256("recorder-incomplete:" + recorderIncompleteReason),
    });
  }
  if (distribution.reason) {
    finalSources.unresolved.push({
      source: "<unverified-tex-distribution>",
      command: "kpsewhich",
      reason: "outside_project",
      digest: sha256("distribution-roots-unverified:" + distribution.reason),
    });
  }
  const snapshotIntegrityVerified = await verifyCompileSnapshot(snapshot);
  const snapshotProtectionReleased = await snapshotProtection.release();
  let sourceChanged = true;
  try {
    sourceChanged = await compareOriginalProjectToSnapshot(originalRoot, snapshot);
  } catch {
    // Losing access to the live tree is an incomplete result, even though
    // TeX consumed only the already-hashed private snapshot.
  }
  const unbaselinedInputs = [...finalSources.files.keys()]
    .filter((path) => !snapshot.files.has(relative(snapshot.sourceRoot, path)));
  const integrityComplete = snapshot.complete
    && snapshotProtection.enforced
    && snapshotIntegrityVerified
    && snapshotProtectionReleased
    && recorderPaths.inputs.length > 0
    && distribution.reason === null
    && !sourceChanged
    && unbaselinedInputs.length === 0
    && recorderPaths.bypassedProjectInputs.length === 0
    && recorderPaths.externalInputs.length === 0;
  if (status === "passed" && !pdfHash) status = "failed";
  if (status === "passed" && !integrityComplete) status = "incomplete";
  const receipt = buildReceipt({
    status,
    rootFile: safeRelativePath(finalSources.projectRoot, finalSources.rootFile),
    files: [...finalSources.files.values()],
    complete: finalSources.unresolved.length === 0 && integrityComplete,
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
    snapshotLimitReason: snapshot.limitReason,
    snapshotProtectionReason: snapshotProtection.reason,
    snapshotIntegrityVerified,
    snapshotProtectionReleased,
    unbaselinedInputs,
    bypassedProjectInputs: recorderPaths.bypassedProjectInputs,
    externalInputs: recorderPaths.externalInputs,
    recorderIncompleteReason,
    distributionRootsReason: distribution.reason,
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
