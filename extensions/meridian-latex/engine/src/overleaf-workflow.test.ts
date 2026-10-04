import { afterEach, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { chmod, mkdtemp, mkdir, open as openFile, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, delimiter, dirname, isAbsolute, join, relative } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { compileLocalLatex, getLatestCompileReceiptSummary, getWorkflowStatusPayload, runBoundedCommand, type CommandResult, type CommandRunner, type LatexEngine } from "./overleaf-workflow.js";

const temporaryDirectories: string[] = [];
let originalPath: string | undefined;
let fakeDistributionRoot = "";
let fakeDistributionRoots: Record<string, string> = {};

beforeEach(async () => {
  temporaryDirectories.length = 0;
  originalPath = process.env.PATH;
  const toolsDirectory = await mkdtemp(join(tmpdir(), "meridian-latex-tools-"));
  temporaryDirectories.push(toolsDirectory);
  fakeDistributionRoot = join(toolsDirectory, "texmf-dist");
  fakeDistributionRoots = {
    TEXMFDIST: fakeDistributionRoot,
    TEXMFROOT: toolsDirectory,
    TEXMFMAIN: fakeDistributionRoot,
    TEXMFSYSVAR: join(toolsDirectory, "texmf-var"),
    TEXMFSYSCONFIG: join(toolsDirectory, "texmf-config"),
    TEXMFLOCAL: join(toolsDirectory, "texmf-local"),
  };
  await Promise.all(Object.values(fakeDistributionRoots).map((path) => mkdir(path, { recursive: true })));
  const extension = process.platform === "win32" ? ".exe" : "";
  for (const name of ["pdflatex", "xelatex", "lualatex", "kpsewhich"]) {
    const executable = join(toolsDirectory, name + extension);
    await writeFile(executable, "test executable placeholder");
    if (process.platform !== "win32") await chmod(executable, 0o755);
  }
  process.env.PATH = [toolsDirectory, originalPath].filter(Boolean).join(delimiter);
});
afterEach(async () => {
  if (originalPath === undefined) delete process.env.PATH;
  else process.env.PATH = originalPath;
  await Promise.all(temporaryDirectories.map((directory) => rm(directory, { recursive: true, force: true })));
});

async function project(files: Record<string, string>): Promise<{ root: string; state: string }> {
  const root = await mkdtemp(join(tmpdir(), "meridian-latex-project-"));
  const state = join(root, "state");
  temporaryDirectories.push(root);
  for (const [path, contents] of Object.entries(files)) {
    const target = join(root, path);
    await mkdir(join(target, ".."), { recursive: true });
    await writeFile(target, contents, "utf8");
  }
  return { root, state };
}

function fakeRunner(engine: LatexEngine, extraRecordedInputs: string[] = []) {
  const calls: Array<{ executable: string; args: string[]; cwd: string }> = [];
  const rebaseProjectInput = (path: string, cwd: string): string => {
    if (!isAbsolute(path)) return path;
    let ancestor = path;
    while (true) {
      if (basename(ancestor).startsWith("meridian-latex-project-")) return join(cwd, relative(ancestor, path));
      const parent = dirname(ancestor);
      if (parent === ancestor) return path;
      ancestor = parent;
    }
  };
  const runCommand = async (executable: string, args: string[], cwd: string): Promise<CommandResult> => {
    calls.push({ executable, args, cwd });
    if (basename(executable).toLowerCase().replace(/\.exe$/, "") === "kpsewhich" && args[0]?.startsWith("--var-value=")) {
      const root = fakeDistributionRoots[args[0].slice("--var-value=".length)];
      return { exitCode: 0, stdout: (root ?? "") + "\n", stderr: "", durationMs: 1 };
    }
    if (args[0] === "--version") {
      return { exitCode: 0, stdout: `${executable} version 1.2.3\n`, stderr: "", durationMs: 2 };
    }
    if (executable === engine) {
      const outputArg = args.find((arg) => arg.startsWith("-output-directory="));
      assert.ok(outputArg, "engine receives a local output directory");
      const outDir = outputArg!.slice("-output-directory=".length);
      const rootFile = args.at(-1)!;
      const jobName = basename(rootFile).replace(/\.tex$/i, "");
      await writeFile(join(outDir, `${jobName}.pdf`), "%PDF-fake receipt test\n");
      await writeFile(join(outDir, `${jobName}.log`), "Fake compiler completed.\n");
      const recordedInputs = [rootFile, ...extraRecordedInputs.map((path) => rebaseProjectInput(path, cwd))];
      try {
        await readFile(join(cwd, "references.bib"));
        recordedInputs.push(join(cwd, "references.bib"));
      } catch {
        // Do not invent recorder inputs that the fake compiler did not read.
      }
      await writeFile(join(outDir, `${jobName}.fls`), `${recordedInputs.map((path) => `INPUT ${path}`).join("\n")}\n`);
    }
    return { exitCode: 0, stdout: `${executable} completed\n`, stderr: "", durationMs: 3 };
  };
  return { calls, runCommand };
}

test("compile receipt hashes transitive TeX and BibTeX inputs while preserving natbib source", async () => {
  const { root, state } = await project({
    "main.tex": "\\documentclass{article}\n\\usepackage{natbib}\n\\begin{document}\\input{chapters/body}\\import{chapters/}{methods}\\bibliographystyle{plainnat}\\bibliography{references}\\end{document}\n",
    "chapters/body.tex": "Prior work \\citep{known}.\n",
    "chapters/methods.tex": "Methods.\n",
    "references.bib": "@article{known, title={Example}, author={A. Writer}, year={2026}}\n",
  });
  const fake = fakeRunner("pdflatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), engine: "pdflatex", overleafProjectId: "project_123", stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.compiler.bibliography_backend, "bibtex");
  assert.equal(receipt.overleaf_sync, "not_attested");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("TeX is not sandboxed") && limitation.includes("current user's permissions")));
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "chapters/body.tex"));
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "chapters/methods.tex"), "literal import paths are added to the source manifest");
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "references.bib"));
  assert.equal(fake.calls.filter((call) => call.executable === "pdflatex" && call.args[0] !== "--version").length, 3);
  assert.ok(fake.calls.filter((call) => call.executable === "pdflatex" && call.args[0] !== "--version").every((call) => call.args.includes("-no-shell-escape")));
  assert.equal(receipt.root_file, "main.tex");
  assert.ok(receipt.source_manifest.files.every((file) => !file.path.includes(root)), "manifest paths are project-relative");
  assert.ok(receipt.output.pdf_path?.includes("build"), "local CLI receipt points to the local build artifact");
});

test("project search paths keep their order while pointing at the compile snapshot", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}Stable\\end{document}\n",
    "macros/local.tex": "Macro.\n",
    "styles/custom.sty": "\\ProvidesPackage{custom}\n",
    "bib/references.bib": "@book{key,title={Example}}\n",
    "bst/local.bst": "STYLE\n",
  });
  const previous = {
    tex: process.env.TEXINPUTS,
    bib: process.env.BIBINPUTS,
    bst: process.env.BSTINPUTS,
  };
  process.env.TEXINPUTS = join(root, "macros") + delimiter + join(root, "styles");
  process.env.BIBINPUTS = join(root, "bib");
  process.env.BSTINPUTS = "bst";
  let compileCwd = "";
  let compileEnv: NodeJS.ProcessEnv | undefined;
  const fake = fakeRunner("pdflatex");
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    if (executable === "pdflatex" && args[0] !== "--version") {
      compileCwd = cwd;
      compileEnv = env;
    }
    return fake.runCommand(executable, args, cwd);
  };
  try {
    const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });
    assert.equal(receipt.status, "passed");
    assert.equal(compileEnv?.TEXINPUTS, join(compileCwd, "macros") + delimiter + join(compileCwd, "styles"));
    assert.equal(compileEnv?.BIBINPUTS, compileCwd + delimiter + join(compileCwd, "bib"));
    assert.equal(compileEnv?.BSTINPUTS, join(compileCwd, "bst"));
  } finally {
    if (previous.tex === undefined) delete process.env.TEXINPUTS;
    else process.env.TEXINPUTS = previous.tex;
    if (previous.bib === undefined) delete process.env.BIBINPUTS;
    else process.env.BIBINPUTS = previous.bib;
    if (previous.bst === undefined) delete process.env.BSTINPUTS;
    else process.env.BSTINPUTS = previous.bst;
  }
});

test("biblatex addbibresource selects Biber and records a local compile receipt summary", async () => {
  const { root, state } = await project({
    "paper.tex": "\\documentclass{article}\\usepackage[backend=biber]{biblatex}\\addbibresource{references.bib}\\begin{document}\\cite{key}\\end{document}\n",
    "references.bib": "@book{key, title={A book}, author={A. Writer}, year={2026}}\n",
  });
  const fake = fakeRunner("xelatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "paper.tex"), engine: "xelatex", overleafProjectId: "project_456", stateDir: state, runCommand: fake.runCommand });
  const summary = await getLatestCompileReceiptSummary("project_456", state);

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.compiler.bibliography_backend, "biber");
  assert.ok(fake.calls.some((call) => call.executable === "biber" && call.args[0] === "--version"));
  assert.ok(fake.calls.some((call) => call.executable === "biber" && call.args[0] === "paper"));
  assert.equal(summary?.status, "passed");
  assert.equal(summary?.overleaf_sync, "not_attested");
  assert.equal(summary?.root_file, "paper.tex");
  assert.ok(!JSON.stringify(summary).includes(root), "popup summary does not expose local paths");
  const payload = await getWorkflowStatusPayload("project_456", state);
  assert.equal(payload.ok, true);
  assert.equal(payload.localCompile.command, "meridian-latex compile <main.tex> --project-id=<id>");
  assert.equal(payload.localCompile.receipt?.status, "passed");
  assert.equal(payload.localCompile.note.includes("sync is not attested"), true);
  assert.ok(!JSON.stringify(payload).includes("pdf_path"), "workflow API keeps local artifact paths private");
  await assert.rejects(getWorkflowStatusPayload("bad project id", state), /invalid Overleaf project identifier/);
});

test("biblatex backend=bibtex selects BibTeX", async () => {
  const { root, state } = await project({
    "paper.tex": "\\documentclass{article}\\usepackage[backend=bibtex]{biblatex}\\addbibresource{references.bib}\\begin{document}\\cite{key}\\end{document}\n",
    "references.bib": "@book{key, title={A book}, author={A. Writer}, year={2026}}\n",
  });
  const fake = fakeRunner("xelatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "paper.tex"), engine: "xelatex", stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.compiler.bibliography_backend, "bibtex");
  assert.ok(fake.calls.some((call) => call.executable === "bibtex" && call.args[0] === "--version"));
  assert.ok(fake.calls.some((call) => call.executable === "bibtex" && call.args[0] === "paper"));
  assert.ok(!fake.calls.some((call) => call.executable === "biber"));
});

test("biblatex backend=bibtex8 selects the 8-bit BibTeX executable", async () => {
  const { root, state } = await project({
    "paper.tex": "\\documentclass{article}\\usepackage[backend=bibtex8]{biblatex}\\addbibresource{references.bib}\\begin{document}\\cite{key}\\end{document}\n",
    "references.bib": "@book{key, title={A book}, author={A. Writer}, year={2026}}\n",
  });
  const fake = fakeRunner("xelatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "paper.tex"), engine: "xelatex", stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.compiler.bibliography_backend, "bibtex8");
  assert.ok(fake.calls.some((call) => call.executable === "bibtex8" && call.args[0] === "--version"));
  assert.ok(fake.calls.some((call) => call.executable === "bibtex8" && call.args[0] === "paper"));
  assert.ok(!fake.calls.some((call) => call.executable === "biber"));
});

test("PassOptionsToPackage selects the explicit biblatex backend", async () => {
  const { root, state } = await project({
    "paper.tex": "\\PassOptionsToPackage{backend=bibtex8}{biblatex}\\usepackage{biblatex}\\addbibresource{references.bib}\\begin{document}\\cite{key}\\end{document}\n",
    "references.bib": "@book{key, title={A book}, author={A. Writer}, year={2026}}\n",
  });
  const fake = fakeRunner("xelatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "paper.tex"), engine: "xelatex", stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.compiler.bibliography_backend, "bibtex8");
  assert.ok(fake.calls.some((call) => call.executable === "bibtex8" && call.args[0] === "paper"));
  assert.ok(!fake.calls.some((call) => call.executable === "biber"));
});

test("ExecuteBibliographyOptions selects the explicit biblatex backend", async () => {
  const { root, state } = await project({
    "paper.tex": "\\usepackage{biblatex}\\ExecuteBibliographyOptions{backend=bibtex}\\addbibresource{references.bib}\\begin{document}\\cite{key}\\end{document}\n",
    "references.bib": "@book{key, title={A book}, author={A. Writer}, year={2026}}\n",
  });
  const fake = fakeRunner("xelatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "paper.tex"), engine: "xelatex", stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.compiler.bibliography_backend, "bibtex");
  assert.ok(fake.calls.some((call) => call.executable === "bibtex" && call.args[0] === "paper"));
  assert.ok(!fake.calls.some((call) => call.executable === "biber"));
});

test("workflow status ignores malformed latest receipts instead of projecting partial fields", async () => {
  const { root, state } = await project({ "paper.tex": "\\begin{document}Hello\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  await compileLocalLatex({ rootFile: join(root, "paper.tex"), overleafProjectId: "project_malformed", stateDir: state, runCommand: fake.runCommand });
  const identityHash = createHash("sha256").update("overleaf:project_malformed").digest("hex");
  const receiptDir = join(state, "compile-receipts", identityHash);
  await writeFile(join(receiptDir, "9999-malformed.json"), JSON.stringify({
    schema_version: 1,
    overleaf_project_id: "project_malformed",
    created_at: "2099-01-01T00:00:00.000Z",
    status: "passed",
  }), "utf8");

  const summary = await getLatestCompileReceiptSummary("project_malformed", state);
  const payload = await getWorkflowStatusPayload("project_malformed", state);
  assert.equal(summary?.status, "passed");
  assert.equal(payload.localCompile.receipt?.status, "passed");
  assert.equal(payload.localCompile.receipt?.overleaf_sync, "not_attested");
});

test("workflow status rejects absolute receipt root paths before returning the summary", async () => {
  const { root, state } = await project({ "paper.tex": "\\begin{document}Hello\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  await compileLocalLatex({ rootFile: join(root, "paper.tex"), overleafProjectId: "project_path", stateDir: state, runCommand: fake.runCommand });
  const identityHash = createHash("sha256").update("overleaf:project_path").digest("hex");
  const receiptDir = join(state, "compile-receipts", identityHash);
  const receiptName = (await readdir(receiptDir)).find((name) => name.endsWith(".json"));
  assert.ok(receiptName);
  const receipt = JSON.parse(await readFile(join(receiptDir, receiptName!), "utf8"));
  receipt.root_file = "C:\\Users\\Private\\paper.tex";
  await writeFile(join(receiptDir, receiptName!), JSON.stringify(receipt), "utf8");

  const summary = await getLatestCompileReceiptSummary("project_path", state);
  const payload = await getWorkflowStatusPayload("project_path", state);
  assert.equal(summary, null);
  assert.equal(payload.localCompile.receipt, null);
  assert.ok(!JSON.stringify(payload).includes("C:\\Users\\Private"));
});

test("dynamic TeX input is compiled without claiming a complete source manifest", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}\\input{\\chapterFile}\\end{document}\n" });
  const fake = fakeRunner("lualatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), engine: "lualatex", stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.equal(receipt.source_manifest.unresolved_count, 1);
  assert.equal(receipt.output.pdf_sha256?.length, 64);
});

test("missing local input is recorded as incomplete even when the compiler exits zero", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}\\input{missing-chapter}\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.equal(receipt.source_manifest.unresolved_count, 1);
});

test("recorder entries add local TeX closure and asset files to the receipt", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}Main text.\\end{document}\n",
    "supplement.tex": "Supplement \\input{chapters/details}.\n",
    "chapters/details.tex": "Nested details.\n",
    "figure.png": "fake image bytes",
  });
  const fake = fakeRunner("pdflatex", [join(root, "supplement.tex"), join(root, "figure.png")]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "supplement.tex"));
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "chapters/details.tex"));
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "figure.png" && file.kind === "asset"));
});

test("literal graphicspath directories resolve local graphics into the manifest", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{figures/}{assets/}}\n\\begin{document}\\includegraphics{chart}\\end{document}\n",
    "figures/chart.png": "fake image bytes",
  });
  const imagePath = join(root, "figures", "chart.png");
  const fake = fakeRunner("pdflatex", [imagePath]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "figures/chart.png" && file.kind === "asset"));
});

test("redefined graphicspath declarations resolve duplicate basenames in declaration order", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{first/}}\\includegraphics{chart}\n\\graphicspath{{second/}}\\includegraphics{chart}\n",
    "first/chart.png": "first image bytes",
    "second/chart.png": "second image bytes",
  });
  const firstImage = join(root, "first", "chart.png");
  const secondImage = join(root, "second", "chart.png");
  const fake = fakeRunner("pdflatex", [firstImage, secondImage]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "first/chart.png"));
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "second/chart.png"));
});

test("graphicspath state follows ordered local inputs", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{first/}}\\input{chapters/first}\n\\graphicspath{{second/}}\\input{chapters/second}\n",
    "chapters/first.tex": "\\includegraphics{chart}\n",
    "chapters/second.tex": "\\includegraphics{chart}\n",
    "first/chart.png": "first image bytes",
    "second/chart.png": "second image bytes",
  });
  const fake = fakeRunner("pdflatex", [
    join(root, "chapters", "first.tex"),
    join(root, "first", "chart.png"),
    join(root, "chapters", "second.tex"),
    join(root, "second", "chart.png"),
  ]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "first/chart.png"));
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "second/chart.png"));
});

test("ambiguous duplicate basenames without recorder evidence keep the manifest incomplete", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{first/}{second/}}\\begin{document}\\includegraphics{chart}\\end{document}\n",
    "first/chart.png": "first image bytes",
    "second/chart.png": "second image bytes",
  });
  const fake = fakeRunner("pdflatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.source_manifest.unresolved_count > 0);
});

test("normalized in-project parent segments in graphicspath resolve safely", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{chapters/../figures/}}\\begin{document}\\includegraphics{chart}\\end{document}\n",
    "figures/chart.png": "fake image bytes",
  });
  const imagePath = join(root, "figures", "chart.png");
  const fake = fakeRunner("pdflatex", [imagePath]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "figures/chart.png"));
});

test("recorder-listed unique graphics reconcile TeX search paths outside literal graphicspath", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}\\includegraphics{chart}\\end{document}\n",
    "images/chart.png": "fake image bytes",
  });
  const imagePath = join(root, "images", "chart.png");
  const fake = fakeRunner("pdflatex", [imagePath]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.unresolved_count, 0);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "images/chart.png"));
});

test("path-qualified graphic references reconcile from one unique recorder input", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{not-created/}}\\begin{document}\\includegraphics{images/chart}\\end{document}\n",
    "rendered/images/chart.png": "fake image bytes",
  });
  const imagePath = join(root, "rendered", "images", "chart.png");
  const fake = fakeRunner("pdflatex", [imagePath]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.unresolved_count, 0);
  assert.ok(receipt.source_manifest.files.some((file) => file.path === "rendered/images/chart.png"));
});

test("path-qualified graphics stay incomplete when multiple recorder inputs share their suffix", async () => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{not-created/}}\\begin{document}\\includegraphics{images/chart}\\end{document}\n",
    "first/images/chart.png": "first image bytes",
    "second/images/chart.png": "second image bytes",
  });
  const fake = fakeRunner("pdflatex", [
    join(root, "first", "images", "chart.png"),
    join(root, "second", "images", "chart.png"),
  ]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.source_manifest.unresolved_count > 0);
});

test("recorder reconciliation does not guess a missing directory from a matching basename", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}\\includegraphics{missing/chart}\\end{document}\n",
    "images/chart.png": "different image bytes",
  });
  const fake = fakeRunner("pdflatex", [join(root, "images", "chart.png")]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.source_manifest.unresolved_count > 0);
});

test("dynamic and traversal graphic paths remain unresolved", async () => {
  for (const [source, label] of [
    ["\\graphicspath{{\\assetDirectory/}}\\begin{document}\\includegraphics{chart}\\end{document}\n", "dynamic"],
    ["\\graphicspath{{../outside/}}\\begin{document}\\includegraphics{chart}\\end{document}\n", "traversal"],
  ] as const) {
    const { root, state } = await project({
      "main.tex": source,
      "images/chart.png": "fake image bytes",
    });
    const fake = fakeRunner("pdflatex", [join(root, "images", "chart.png")]);
    const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

    assert.equal(receipt.status, "incomplete", `${label} graphic paths are not claimed complete`);
    assert.equal(receipt.source_manifest.complete, false);
    assert.ok(receipt.source_manifest.unresolved_count > 0);
  }
});

test("symlinked graphicspath directories that escape the project remain unresolved", async (context) => {
  const { root, state } = await project({
    "main.tex": "\\graphicspath{{escape/}}\\begin{document}\\includegraphics{chart}\\end{document}\n",
  });
  const outside = await mkdtemp(join(tmpdir(), "meridian-latex-outside-"));
  temporaryDirectories.push(outside);
  await writeFile(join(outside, "chart.png"), "outside image bytes", "utf8");
  try {
    await symlink(outside, join(root, "escape"), process.platform === "win32" ? "junction" : "dir");
  } catch (error) {
    context.skip(`directory symlinks are unavailable: ${String(error)}`);
    return;
  }
  const fake = fakeRunner("pdflatex", [join(outside, "chart.png")]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.source_manifest.unresolved_count > 0);
  assert.ok(receipt.source_manifest.files.every((file) => !file.path.includes("chart.png")));
});

test("missing local compiler yields an unavailable receipt rather than a false pass", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Hello\\end{document}\n" });
  const runCommand = async (): Promise<CommandResult> => ({ exitCode: null, stdout: "", stderr: "", durationMs: 1, errorCode: "ENOENT" });
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), overleafProjectId: "project_789", stateDir: state, runCommand });

  assert.equal(receipt.status, "unavailable");
  assert.equal(receipt.compiler.version, null);
  assert.equal(receipt.output.pdf_sha256, null);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("not found on PATH") && limitation.includes("TeX distribution")));
  assert.equal((await getLatestCompileReceiptSummary("project_789", state))?.status, "unavailable");
});

test("missing bibliography backend gives actionable prerequisite guidance", async () => {
  const { root, state } = await project({
    "main.tex": "\\usepackage[backend=biber]{biblatex}\\addbibresource{references.bib}\\begin{document}Text.\\end{document}\n",
    "references.bib": "@book{key, title={Book}}\n",
  });
  const fake = fakeRunner("xelatex");
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => executable === "biber" && args[0] === "--version"
    ? { exitCode: null, stdout: "", stderr: "", durationMs: 1, errorCode: "ENOENT" }
    : fake.runCommand(executable, args, cwd);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "unavailable");
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("biber") && limitation.includes("not found on PATH")));
});

test("source changes during compile produce an incomplete receipt", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Before\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  let changed = false;
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version" && !changed) {
      changed = true;
      await writeFile(join(root, "main.tex"), "\\begin{document}After\\end{document}\n", "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("changed while compiling")));
});

test("unbraced input changes during compile retain the exact snapshot hash", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}\\input chapter\\end{document}\n",
    "chapter.tex": "Before compile.\n",
  });
  const chapterPath = join(root, "chapter.tex");
  const fake = fakeRunner("pdflatex", [chapterPath]);
  let changed = false;
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version" && !changed) {
      changed = true;
      await writeFile(chapterPath, "Changed during compile.\n", "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.equal(receipt.source_manifest.files.find((file) => file.path === "chapter.tex")?.sha256, createHash("sha256").update("Before compile.\n").digest("hex"));
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("changed while compiling")));
});

test("recorder-only local inputs changed during compile retain the exact snapshot hash", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}No lexical input reference.\\end{document}\n",
    "chapter.tex": "Recorder-only input.\n",
  });
  const chapterPath = join(root, "chapter.tex");
  const fake = fakeRunner("pdflatex", [join(root, "chapter.tex")]);
  let changed = false;
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version" && !changed) {
      changed = true;
      await writeFile(chapterPath, "Changed recorder-only input.\n", "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.equal(receipt.source_manifest.files.find((file) => file.path === "chapter.tex")?.sha256, createHash("sha256").update("Recorder-only input.\n").digest("hex"));
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("changed while compiling")));
});

test("deleted recorder-only local inputs keep the source manifest incomplete", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}No lexical input reference.\\end{document}\n",
    "chapter.tex": "Recorder-only input.\n",
  });
  const chapterPath = join(root, "chapter.tex");
  const fake = fakeRunner("pdflatex", [chapterPath]);
  let deleted = false;
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version" && !deleted) {
      deleted = true;
      await rm(chapterPath);
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.equal(receipt.source_manifest.files.find((file) => file.path === "chapter.tex")?.sha256, createHash("sha256").update("Recorder-only input.\n").digest("hex"));
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("changed while compiling")));
});

test("change then restore in the live project cannot change the compiled snapshot bytes", async () => {
  const original = "Recorder-only input.\n";
  const { root, state } = await project({
    "main.tex": "\\begin{document}No lexical input reference.\\end{document}\n",
    "chapter.tex": original,
  });
  const chapterPath = join(root, "chapter.tex");
  const fake = fakeRunner("pdflatex", [chapterPath]);
  let changed = false;
  let compiledChapter = "";
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version" && !changed) {
      changed = true;
      compiledChapter = await readFile(join(cwd, "chapter.tex"), "utf8");
      await writeFile(chapterPath, "Transient contents seen during compilation.\n", "utf8");
      await writeFile(chapterPath, original, "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(compiledChapter, original);
  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.equal(receipt.source_manifest.files.find((file) => file.path === "chapter.tex")?.sha256, createHash("sha256").update(original).digest("hex"));
});

test("compile snapshot write protection blocks edits or fails the receipt closed", async () => {
  const original = "\\begin{document}Stable\\end{document}\n";
  const { root, state } = await project({ "main.tex": original });
  const fake = fakeRunner("pdflatex");
  let snapshotWriteSucceeded = false;
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version") {
      try {
        await writeFile(join(cwd, "main.tex"), "Transient snapshot tamper.\n", "utf8");
        snapshotWriteSucceeded = true;
        await writeFile(join(cwd, "main.tex"), original, "utf8");
      } catch {
        // Expected when the filesystem enforced snapshot write protection.
      }
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  if (process.platform === "win32") assert.equal(snapshotWriteSucceeded, false, "the Windows snapshot ACL blocks TeX-side edits");
  if (snapshotWriteSucceeded) {
    assert.equal(receipt.status, "incomplete");
    assert.equal(receipt.source_manifest.complete, false);
  } else {
    assert.equal(receipt.status, "passed");
    assert.equal(receipt.source_manifest.complete, true);
  }
});

test("recorder paths that bypass the immutable project snapshot make the manifest incomplete", async () => {
  const { root, state } = await project({
    "main.tex": "\\begin{document}Stable\\end{document}\n",
    "chapter.tex": "Local recorder input.\n",
  });
  const chapterPath = join(root, "chapter.tex");
  const fake = fakeRunner("pdflatex");
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version") {
      const outputArg = args.find((arg) => arg.startsWith("-output-directory="))!;
      const flsPath = join(outputArg.slice("-output-directory=".length), "main.fls");
      const fls = await readFile(flsPath, "utf8");
      await writeFile(flsPath, fls + "INPUT " + chapterPath + "\n", "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("original project path outside the compile snapshot")));
});

test("recorder inputs outside the snapshot and TeX distribution fail closed regardless of extension", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const outside = await mkdtemp(join(tmpdir(), "meridian-latex-outside-"));
  temporaryDirectories.push(outside);
  const externalInput = join(outside, "runtime-data.bin");
  await writeFile(externalInput, "external recorder input", "utf8");
  const fake = fakeRunner("pdflatex", [externalInput]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.source_manifest.unresolved_count > 0);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("outside the compile snapshot and TeX distribution")));
});

test("the active engine's kpsewhich TEXMFDIST is accepted as the distribution root", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const systemInput = join(fakeDistributionRoot, "tex", "latex", "article.cls");
  const systemVariableInput = join(fakeDistributionRoots.TEXMFSYSVAR, "web2c", "pdftex", "pdflatex.fmt");
  await mkdir(dirname(systemInput), { recursive: true });
  await mkdir(dirname(systemVariableInput), { recursive: true });
  await writeFile(systemInput, "verified distribution input", "utf8");
  await writeFile(systemVariableInput, "verified system variable input", "utf8");
  const fake = fakeRunner("pdflatex", [systemInput, systemVariableInput]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });
  const kpsewhichCall = fake.calls.find((call) => basename(call.executable).toLowerCase().replace(/\.exe$/, "") === "kpsewhich");

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(!receipt.source_manifest.files.some((file) => file.path.includes("article.cls")));
  assert.ok(!receipt.source_manifest.files.some((file) => file.path.includes("pdflatex.fmt")));
  assert.ok(kpsewhichCall);
  assert.equal(kpsewhichCall!.cwd, dirname(kpsewhichCall!.executable), "kpsewhich runs from the active engine's binary directory");
  assert.ok(!receipt.limitations.some((limitation) => limitation.includes("distribution roots cannot be verified")));
});

test("unavailable active-engine kpsewhich roots make the receipt incomplete", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    if (basename(executable).toLowerCase().replace(/\.exe$/, "") === "kpsewhich") {
      return { exitCode: 1, stdout: "", stderr: "distribution lookup failed", durationMs: 1 };
    }
    return fake.runCommand(executable, args, cwd);
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("kpsewhich could not resolve TEXMFDIST")));
});

test("a texlive-looking external path is not trusted without kpsewhich proof", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const outside = await mkdtemp(join(tmpdir(), "meridian-latex-texlive-decoy-"));
  temporaryDirectories.push(outside);
  const untrustedDistributionPath = join(outside, "texlive", "texmf-dist", "tex", "latex", "untrusted.sty");
  await mkdir(dirname(untrustedDistributionPath), { recursive: true });
  await writeFile(untrustedDistributionPath, "untrusted external package", "utf8");
  const fake = fakeRunner("pdflatex", [untrustedDistributionPath]);
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("outside the compile snapshot and TeX distribution")));
});

test("external user TEXINPUTS entries make the source manifest incomplete", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const outside = await mkdtemp(join(tmpdir(), "meridian-latex-texinputs-decoy-"));
  temporaryDirectories.push(outside);
  const previous = process.env.TEXINPUTS;
  process.env.TEXINPUTS = join(outside, "texlive", "texmf-dist");
  try {
    const fake = fakeRunner("pdflatex");
    const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

    assert.equal(receipt.status, "incomplete");
    assert.equal(receipt.source_manifest.complete, false);
    assert.ok(receipt.limitations.some((limitation) => limitation.includes("outside the compile snapshot and TeX distribution")));
  } finally {
    if (previous === undefined) delete process.env.TEXINPUTS;
    else process.env.TEXINPUTS = previous;
  }
});

test("only recorder-declared existing build outputs are ignored as current-job inputs", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  let generatedOutput = "";
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version") {
      const outputArg = args.find((arg) => arg.startsWith("-output-directory="))!;
      const buildDir = outputArg.slice("-output-directory=".length);
      generatedOutput = join(buildDir, "main.aux");
      await writeFile(generatedOutput, "current run auxiliary output", "utf8");
      const flsPath = join(buildDir, "main.fls");
      const fls = await readFile(flsPath, "utf8");
      await writeFile(flsPath, fls + "OUTPUT " + generatedOutput + "\nINPUT " + generatedOutput + "\n", "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "passed");
  assert.equal(receipt.source_manifest.complete, true);
  assert.ok(generatedOutput);
  assert.ok(!receipt.limitations.some((limitation) => limitation.includes("outside the compile snapshot and TeX distribution")));
});

test("unverified build-directory recorder inputs are external and fail closed", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const fake = fakeRunner("pdflatex");
  let unverifiedInput = "";
  const runCommand: CommandRunner = async (executable, args, cwd, timeoutMs, env) => {
    const result = await fake.runCommand(executable, args, cwd);
    if (executable === "pdflatex" && args[0] !== "--version") {
      const outputArg = args.find((arg) => arg.startsWith("-output-directory="))!;
      const buildDir = outputArg.slice("-output-directory=".length);
      unverifiedInput = join(buildDir, "unverified.sty");
      await writeFile(unverifiedInput, "not declared as a compiler output", "utf8");
      const flsPath = join(buildDir, "main.fls");
      const fls = await readFile(flsPath, "utf8");
      await writeFile(flsPath, fls + "INPUT " + unverifiedInput + "\n", "utf8");
    }
    return result;
  };
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(unverifiedInput);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("outside the compile snapshot and TeX distribution")));
});

test("bounded snapshot streaming stops at the aggregate byte limit", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const oversized = await openFile(join(root, "large.bin"), "w");
  await oversized.truncate(64 * 1024 * 1024 + 1);
  await oversized.close();
  const fake = fakeRunner("pdflatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("byte limit (67108864)")));
});

test("compile snapshot fails closed when directory traversal reaches its limit", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  await Promise.all(Array.from({ length: 4_097 }, (_, index) => mkdir(join(root, `empty-${index}`))));
  const fake = fakeRunner("pdflatex");
  const receipt = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: fake.runCommand });

  assert.equal(receipt.status, "incomplete");
  assert.equal(receipt.source_manifest.complete, false);
  assert.ok(receipt.limitations.some((limitation) => limitation.includes("directory limit (4096)")));
});

test("unique per-run build directories prevent stale outputs and concurrent clobbering", async () => {
  const { root, state } = await project({ "main.tex": "\\begin{document}Stable\\end{document}\n" });
  const firstRunner = fakeRunner("pdflatex");
  const first = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: firstRunner.runCommand });
  const noOutputRunner: CommandRunner = async (executable, args) => executable === "pdflatex" && args[0] === "--version"
    ? { exitCode: 0, stdout: "pdflatex test\n", stderr: "", durationMs: 1 }
    : { exitCode: 0, stdout: "", stderr: "", durationMs: 1 };
  const noOutput = await compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: noOutputRunner });
  const runnerA = fakeRunner("pdflatex");
  const runnerB = fakeRunner("pdflatex");
  const [concurrentA, concurrentB] = await Promise.all([
    compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: runnerA.runCommand }),
    compileLocalLatex({ rootFile: join(root, "main.tex"), stateDir: state, runCommand: runnerB.runCommand }),
  ]);

  assert.equal(first.status, "passed");
  assert.equal(noOutput.status, "failed", "exit 0 without a new PDF cannot reuse a previous artifact");
  assert.equal(noOutput.output.pdf_path, null);
  assert.notEqual(first.output.pdf_path, concurrentA.output.pdf_path);
  assert.notEqual(concurrentA.output.pdf_path, concurrentB.output.pdf_path);
});

test("local workflow status route returns only the linked receipt summary", async () => {
  const portProbe = createNetServer();
  portProbe.listen(0, "127.0.0.1");
  await once(portProbe, "listening");
  const port = (portProbe.address() as { port: number }).port;
  await new Promise<void>((resolve, reject) => portProbe.close((error) => error ? reject(error) : resolve()));

  const child = spawn(process.execPath, [join(process.cwd(), "dist", "server.js")], {
    cwd: process.cwd(),
    env: { ...process.env, MERIDIAN_LATEX_PORT: String(port) },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const ready = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`server did not start: ${output}`)), 10_000);
    child.stdout.on("data", (chunk: Buffer) => {
      output += chunk.toString("utf8");
      if (output.includes("outline server listening")) {
        clearTimeout(timer);
        resolve();
      }
    });
    child.stderr.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("exit", (code) => {
      if (!output.includes("outline server listening")) {
        clearTimeout(timer);
        reject(new Error(`server exited before ready (${code}): ${output}`));
      }
    });
  });
  try {
    await ready;
    const projectId = `route_${randomUUID().replace(/-/g, "")}`;
    const response = await fetch(`http://127.0.0.1:${port}/workflow-status?project_id=${projectId}`);
    const payload = await response.json() as { ok: boolean; localCompile: { command: string; receipt: unknown; note: string } };
    assert.equal(response.status, 200);
    assert.equal(payload.ok, true);
    assert.equal(payload.localCompile.receipt, null);
    assert.ok(payload.localCompile.note.includes("Overleaf sync is not attested"));
    assert.equal(JSON.stringify(payload).includes("pdf_path"), false);
    const invalid = await fetch(`http://127.0.0.1:${port}/workflow-status?project_id=bad%20id`);
    assert.equal(invalid.status, 400);
  } finally {
    child.kill();
    await Promise.race([once(child, "close"), new Promise((resolve) => setTimeout(resolve, 2_000))]);
  }
});

test("project-root validation prevents compiling a root file outside the selected project", async () => {
  const { root } = await project({ "main.tex": "\\begin{document}Hello\\end{document}" });
  const outside = join(root, "..", "outside.tex");
  await writeFile(outside, "\\begin{document}Outside\\end{document}", "utf8");
  try {
    await assert.rejects(compileLocalLatex({ rootFile: outside, projectRoot: root }), /inside the selected project root/);
  } finally {
    await rm(outside, { force: true });
  }
});

test("bounded process runner captures output and returns a useful missing-executable result", async () => {
  // These processes do not need a temporary project. In particular, keeping
  // the timeout child out of a temp directory avoids Windows holding that
  // directory open while the killed process finishes closing its handles.
  const cwd = process.cwd();
  const success = await runBoundedCommand(process.execPath, ["-e", "process.stdout.write('ok'); process.stderr.write('warning')"], cwd, 5_000);
  const missing = await runBoundedCommand(join(cwd, "missing-compiler.exe"), [], cwd, 5_000);

  assert.equal(success.exitCode, 0);
  assert.equal(success.stdout, "ok");
  assert.equal(success.stderr, "warning");
  assert.equal(missing.exitCode, null);
  assert.equal(missing.errorCode, "ENOENT");
});

test("bounded process runner times out and terminates a stuck subprocess", async () => {
  const result = await runBoundedCommand(process.execPath, ["-e", "process.stdout.write('started'); setTimeout(() => {}, 5_000)"], process.cwd(), 50);

  assert.equal(result.exitCode, null);
  assert.equal(result.errorCode, "TIMEOUT");
});
