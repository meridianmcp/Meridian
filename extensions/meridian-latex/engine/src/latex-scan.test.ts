import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createNewcommandScanner,
  scanBibitems,
  scanBraceArguments,
  type ScanMatch,
} from "./latex-scan.js";
import { getBibliography, parseBibitems } from "./bibliography.js";
import { expandSectionAliases } from "./section-alias.js";

// The regular expressions below are the originals these scanners replace. They
// serve only as oracles for the randomized comparisons; no production code
// uses them. Each adversarial input must finish well under this budget.
const BUDGET_MS = 1000;

type Triple = [number, string, string];

function triples(matches: Iterable<ScanMatch | RegExpMatchArray>): Triple[] {
  return [...matches].map((m) => [m.index ?? -1, m[0], m[1]] as Triple);
}

function timed<T>(fn: () => T): { value: T; ms: number } {
  const start = performance.now();
  const value = fn();
  return { value, ms: performance.now() - start };
}

/** Small deterministic PRNG so failures reproduce exactly. */
function rng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

function randomSource(next: () => number, pieces: readonly string[], maxPieces: number): string {
  const count = Math.floor(next() * maxPieces);
  let out = "";
  for (let i = 0; i < count; i++) out += pieces[Math.floor(next() * pieces.length)];
  return out;
}

test("scanBraceArguments: same matches as the brace-argument regexes it replaced", () => {
  const next = rng(11);
  const pieces = [
    "\\bibliography", "\\addbibresource", "\\input", "\\include", "{", "}", " ", "\n",
    "a,b", "[", "]", "\\", "x",
  ];
  for (let k = 0; k < 2000; k++) {
    const s = randomSource(next, pieces, 30);
    assert.deepEqual(
      triples(scanBraceArguments(s, /\\bibliography\s*\{/g)),
      triples(s.matchAll(/\\bibliography\s*\{([^}]*)\}/g)),
      JSON.stringify(s),
    );
    assert.deepEqual(
      triples(scanBraceArguments(s, /\\addbibresource\s*\{/g)),
      triples(s.matchAll(/\\addbibresource\s*\{([^}]*)\}/g)),
      JSON.stringify(s),
    );
    assert.deepEqual(
      triples(scanBraceArguments(s, /\\(?:input|include)\s*\{/g)),
      triples(s.matchAll(/\\(?:input|include)\s*\{([^}]*)\}/g)),
      JSON.stringify(s),
    );
  }
});

test("scanBibitems: same matches as the \\bibitem regex it replaced", () => {
  const next = rng(23);
  const pieces = ["\\bibitem", "[", "]", "{", "}", " ", "\n", "key", "\\", "x", "[a]", "\t"];
  for (let k = 0; k < 4000; k++) {
    const s = randomSource(next, pieces, 30);
    assert.deepEqual(
      triples(scanBibitems(s)),
      triples(s.matchAll(/\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}/g)),
      JSON.stringify(s),
    );
  }
});

test("scanBibitems: a failed label does not hide a \\bibitem inside it", () => {
  // The label "[\bibitem{x}]" has no brace after it, so its head fails, and the
  // \bibitem inside the label still matches with key "x".
  const s = "\\bibitem[\\bibitem{x}]{key";
  assert.deepEqual(
    triples(scanBibitems(s)),
    triples(s.matchAll(/\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}/g)),
  );
  assert.deepEqual(triples(scanBibitems(s)).map((t) => t[2]), ["x"]);
});

test("createNewcommandScanner: same heads as the newcommand regexes it replaced", () => {
  const next = rng(37);
  const pieces = [
    "\\newcommand", "\\renewcommand", "\\re", "*", "{", "}", "\\a", "\\sec", "[", "]",
    "[1]", "[2]", "[x]", " ", "\n", "\\", "a", "@", "9", "\t",
  ];
  const general = /\\(?:re)?newcommand\*?\s*\{\s*\\([A-Za-z@]+)\s*\}\s*(?:\[\d+\])?\s*(?:\[[^\]]*\])?\s*\{/g;
  for (let k = 0; k < 3000; k++) {
    const s = randomSource(next, pieces, 30);
    const expected: Triple[] = [];
    for (let m = general.exec(s); m !== null; m = general.exec(s)) {
      expected.push([m.index, m[0], m[1]]);
    }
    general.lastIndex = 0;
    const actual: Triple[] = [];
    const scanner = createNewcommandScanner(s);
    let from = 0;
    for (let m = scanner.next(from); m !== null; m = scanner.next(from)) {
      actual.push([m.index, m[0], m[1]]);
      from = m.index + m[0].length;
    }
    assert.deepEqual(actual, expected, JSON.stringify(s));

    for (const name of ["a", "sec", "x"]) {
      const named = new RegExp(
        `\\\\(?:re)?newcommand\\*?\\s*\\{\\s*\\\\(${name})\\s*\\}\\s*(?:\\[\\d+\\])?\\s*(?:\\[[^\\]]*\\])?\\s*\\{`,
        "g",
      );
      const namedExpected: Triple[] = [];
      for (let m = named.exec(s); m !== null; m = named.exec(s)) {
        namedExpected.push([m.index, m[0], m[1]]);
      }
      const namedActual: Triple[] = [];
      let namedFrom = 0;
      for (let m = scanner.next(namedFrom, name); m !== null; m = scanner.next(namedFrom, name)) {
        namedActual.push([m.index, m[0], m[1]]);
        namedFrom = m.index + m[0].length;
      }
      assert.deepEqual(namedActual, namedExpected, `${name} ${JSON.stringify(s)}`);
    }
  }
});

test("parseBibitems: keys match the thebibliography regex pair it replaced", () => {
  const next = rng(41);
  const pieces = [
    "\\begin{thebibliography}", "\\end{thebibliography}", "\\bibitem", "{", "}", "[", "]",
    "k", " ", "\n", "x",
  ];
  for (let k = 0; k < 2000; k++) {
    const s = randomSource(next, pieces, 25);
    const env = s.match(/\\begin\{thebibliography\}(?:\{[^}]*\})?([\s\S]*?)\\end\{thebibliography\}/);
    const body = env ? env[1] : s;
    const expected = [...body.matchAll(/\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}/g)].map((m) =>
      m[1].trim(),
    );
    assert.deepEqual(
      parseBibitems(s).map((entry) => entry.key),
      expected,
      JSON.stringify(s),
    );
  }
  // The optional argument is taken first, then the body restarts after the
  // brace when the argument-less body has no \end.
  assert.deepEqual(
    parseBibitems("\\begin{thebibliography}{\\end{thebibliography}\\bibitem{k} one\\end{thebibliography}")
      .map((entry) => entry.key),
    ["k"],
  );
  assert.deepEqual(parseBibitems("\\begin{thebibliography}{\\end{thebibliography}"), []);
});

test("getBibliography: \\bibliography and \\addbibresource names are still collected", () => {
  const requested: string[] = [];
  getBibliography("\\bibliography{refs, more}\n\\addbibresource{extra}", {
    resolveBibText: (name: string) => {
      requested.push(name);
      return "";
    },
  });
  assert.deepEqual(requested, ["refs.bib", "more.bib", "extra.bib"]);
});

test("adversarial: unclosed \\bibitem heads stay linear", () => {
  const cases = [
    "\\bibitem{".repeat(20_000),
    "\\bibitem[".repeat(20_000) + "]" + " ".repeat(100_000),
    "\\bibitem" + " ".repeat(200_000) + "x",
  ];
  for (const input of cases) {
    const { value, ms } = timed(() => scanBibitems(input));
    assert.deepEqual(value, []);
    assert.ok(ms < BUDGET_MS, `scanBibitems took ${ms.toFixed(1)}ms`);
    const parsed = timed(() => parseBibitems(input));
    assert.ok(parsed.ms < BUDGET_MS, `parseBibitems took ${parsed.ms.toFixed(1)}ms`);
  }
});

test("adversarial: unclosed \\bibliography and \\input braces stay linear", () => {
  const cases: Array<[string, RegExp]> = [
    ["\\bibliography{".repeat(20_000), /\\bibliography\s*\{/g],
    ["\\addbibresource{".repeat(20_000), /\\addbibresource\s*\{/g],
    ["\\input{".repeat(20_000), /\\(?:input|include)\s*\{/g],
  ];
  for (const [input, head] of cases) {
    const { value, ms } = timed(() => scanBraceArguments(input, head));
    assert.deepEqual(value, []);
    assert.ok(ms < BUDGET_MS, `scanBraceArguments took ${ms.toFixed(1)}ms`);
  }
  const bib = timed(() => getBibliography("\\bibliography{".repeat(20_000)));
  assert.deepEqual(bib.value, []);
  assert.ok(bib.ms < BUDGET_MS, `getBibliography took ${bib.ms.toFixed(1)}ms`);
});

test("adversarial: \\newcommand heads with long whitespace and bracket runs stay linear", () => {
  const cases = [
    "\\newcommand{\\a}" + " ".repeat(50_000) + "x",
    "\\newcommand{\\a}[".repeat(5_000),
    "\\newcommand{\\a}[1][".repeat(5_000) + "]",
    "\\newcommand{\\a}[1]" + " ".repeat(50_000) + "[" + "]",
  ];
  for (const input of cases) {
    const { value, ms } = timed(() => {
      const scanner = createNewcommandScanner(input);
      return scanner.next(0);
    });
    assert.equal(value, null);
    assert.ok(ms < BUDGET_MS, `createNewcommandScanner took ${ms.toFixed(1)}ms`);
    const alias = timed(() => expandSectionAliases(input));
    assert.ok(alias.ms < BUDGET_MS, `expandSectionAliases took ${alias.ms.toFixed(1)}ms`);
  }
});
