import { test } from "node:test";
import assert from "node:assert/strict";
import { parseBibitems, parseBibtexEntries, getBibliography } from "./bibliography.js";

test("parseBibitems: multiple entries in a thebibliography block, raw boundaries correct", () => {
  const source = `
\\begin{thebibliography}{99}
\\bibitem{smith2020} J. Smith, "A Paper," 2020.
\\bibitem[Doe21]{doe2021} A. Doe, "Another Paper,"
  2021.
\\end{thebibliography}
`;
  const entries = parseBibitems(source);
  assert.equal(entries.length, 2);
  assert.equal(entries[0].key, "smith2020");
  assert.equal(entries[0].type, "bibitem");
  assert.equal(entries[0].raw, 'J. Smith, "A Paper," 2020.');
  // Second entry has an optional [label] before the mandatory {key}, and its
  // raw text wraps onto a second physical line -- whitespace must collapse.
  assert.equal(entries[1].key, "doe2021");
  assert.equal(entries[1].raw, 'A. Doe, "Another Paper," 2021.');
});

test("parseBibitems: scans the whole source when no thebibliography environment is present", () => {
  const source = "\\bibitem{a} Alpha entry.\n\\bibitem{b} Beta entry.";
  const entries = parseBibitems(source);
  assert.deepEqual(
    entries.map((e) => e.key),
    ["a", "b"],
  );
  assert.equal(entries[0].raw, "Alpha entry.");
  assert.equal(entries[1].raw, "Beta entry.");
});

test("parseBibtexEntries: multiple @type entries, nested braces in a title, @comment skipped, a bare field value", () => {
  const source = `
@article{einstein1905,
  title = {On the {E}lectrodynamics of Moving Bodies},
  author = "Einstein, Albert",
  year = 1905,
  journal = {Annalen der Physik}
}

@comment{
  this whole entry should be skipped
}

@book{knuth1997,
  title = {The Art of Computer Programming},
  author = {Knuth, Donald E.},
  year = {1997}
}
`;
  const entries = parseBibtexEntries(source);
  // @comment must be skipped entirely -- only the article and book remain.
  assert.equal(entries.length, 2);

  const einstein = entries[0];
  assert.equal(einstein.key, "einstein1905");
  assert.equal(einstein.type, "article");
  // Nested braces inside the title value must not truncate it early.
  assert.equal(einstein.title, "On the {E}lectrodynamics of Moving Bodies");
  assert.equal(einstein.author, "Einstein, Albert");
  // year is a bare (unbraced/unquoted) value -- read up to the next comma.
  assert.equal(einstein.year, "1905");

  const knuth = entries[1];
  assert.equal(knuth.key, "knuth1997");
  assert.equal(knuth.type, "book");
  assert.equal(knuth.title, "The Art of Computer Programming");
  // A comma INSIDE a braced value ("Knuth, Donald E.") must not be treated
  // as ending the field early.
  assert.equal(knuth.author, "Knuth, Donald E.");
  assert.equal(knuth.year, "1997");
});

test("parseBibtexEntries: an entry with no fields at all (key only) does not throw and yields empty field strings", () => {
  const entries = parseBibtexEntries("@misc{bareKey}");
  assert.equal(entries.length, 1);
  assert.equal(entries[0].key, "bareKey");
  assert.equal(entries[0].type, "misc");
  assert.equal(entries[0].title, "");
  assert.equal(entries[0].author, "");
  assert.equal(entries[0].year, "");
});

test("getBibliography: inline bibitems only, no resolveBibText option given", () => {
  const source = "\\begin{thebibliography}{9}\n\\bibitem{x} Some entry.\n\\end{thebibliography}";
  const entries = getBibliography(source);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].key, "x");
  assert.equal(entries[0].type, "bibitem");
  assert.equal(entries[0].raw, "Some entry.");
  // bibitem entries omit title/author/year, matching parseBibitems' own shape.
  assert.equal("title" in entries[0], false);
});

test("getBibliography: external \\bibliography{} reference resolved via resolveBibText, combined with inline bibitems", () => {
  const source = `
\\begin{thebibliography}{9}
\\bibitem{inline1} An inline entry.
\\end{thebibliography}
\\bibliography{refs}
`;
  const bibtexText = "@article{ext1, title = {External Title}, author = {Ext Author}, year = {2022}}";
  const resolveBibText = (name: string) => {
    assert.equal(name, "refs.bib");
    return bibtexText;
  };
  const entries = getBibliography(source, { resolveBibText });
  // Both mechanisms' entries appear together when a source has both.
  assert.equal(entries.length, 2);
  assert.equal(entries[0].key, "inline1");
  assert.equal(entries[0].type, "bibitem");
  assert.equal(entries[1].key, "ext1");
  assert.equal(entries[1].type, "article");
  assert.equal((entries[1] as { title?: string }).title, "External Title");
});

test("getBibliography: \\addbibresource with multiple comma-separated names, .bib suffix not duplicated", () => {
  const source = "\\addbibresource{refs.bib, more}";
  const requested: string[] = [];
  const resolveBibText = (name: string) => {
    requested.push(name);
    if (name === "refs.bib") return "@misc{a, title = {A}}";
    if (name === "more.bib") return "@misc{b, title = {B}}";
    return null;
  };
  const entries = getBibliography(source, { resolveBibText });
  assert.deepEqual(requested, ["refs.bib", "more.bib"]);
  assert.deepEqual(
    entries.map((e) => e.key),
    ["a", "b"],
  );
});

test("getBibliography: resolveBibText returns null for the referenced name -> no entries from that mechanism, does not throw", () => {
  const source = "\\bibliography{missing}";
  const resolveBibText = (name: string) => {
    assert.equal(name, "missing.bib");
    return null;
  };
  assert.doesNotThrow(() => {
    const entries = getBibliography(source, { resolveBibText });
    assert.deepEqual(entries, []);
  });
});

test("getBibliography: no resolveBibText at all with an external \\bibliography{} present -> mechanism silently skipped, does not throw", () => {
  const source = "\\bibliography{refs}";
  assert.doesNotThrow(() => {
    const entries = getBibliography(source);
    assert.deepEqual(entries, []);
  });
});

test("getBibliography: a resolveBibText call that throws for one referenced name does not sink the others", () => {
  const source = "\\bibliography{bad,good}";
  const resolveBibText = (name: string) => {
    if (name === "bad.bib") throw new Error("network error");
    return "@misc{ok, title = {OK}}";
  };
  const entries = getBibliography(source, { resolveBibText });
  assert.deepEqual(
    entries.map((e) => e.key),
    ["ok"],
  );
});

test("getBibliography: non-string/empty source returns an empty array without throwing", () => {
  assert.deepEqual(getBibliography(""), []);
  assert.deepEqual(getBibliography(null), []);
  assert.deepEqual(getBibliography(undefined), []);
});
