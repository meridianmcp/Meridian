import { afterEach, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { compileLocalLatex, getLatestCompileReceiptSummary, getWorkflowStatusPayload, runBoundedCommand, type CommandResult, type CommandRunner, type LatexEngine } from "./overleaf-workflow.js";

const temporaryDirectories: string[] = [];

beforeEach(() => { temporaryDirectories.length = 0; });
afterEach(async () => { await Promise.all(temporaryDirectories.map((directory) => rm(directory, { recursive: true, force: true }))); });

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
  const runCommand = async (executable: string, args: string[], cwd: string): Promise<CommandResult> => {
    calls.push({ executable, args, cwd });
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
      await writeFile(join(outDir, `${jobName}.fls`), `INPUT ${rootFile}\nINPUT ${join(cwd, "references.bib")}\n${extraRecordedInputs.map((path) => `INPUT ${path}`).join("\n")}\n`);
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
