# Drafting text model contract

This is the shared contract for the normalized block list that drafting reads from a docx, LaTeX or Google Docs source. It defines the vocabulary only. The producers in docparse, meridian-docs, meridian-latex and the Google Docs adapter are reconciled to this contract by later items; this document does not change them.

The expected output for one case per source lives in `tests/fixtures/drafting/text_model_contract.json`. `tests/test_drafting_text_model_contract.py` keeps this document, that fixture and the kind enum in agreement.

## Block kinds and source mapping

<!-- kind-table:start -->
| Kind | Source | Source construct |
|---|---|---|
| heading | docx | Paragraph with a Word heading style |
| paragraph | docx | Body paragraph |
| heading | latex | Sectioning node in the outline |
| figure | latex | Figure environment |
| table | latex | Table environment |
| equation | latex | Equation node |
| citation | latex | Citation node |
| heading | gdocs | Paragraph with a heading named style |
| paragraph | gdocs | Body paragraph |
| table | gdocs | Table element |
| table_of_contents | gdocs | Table of contents element |
| section_break | gdocs | Section break element |
| unsupported | gdocs | Any element with no mapping above |
<!-- kind-table:end -->

## Block fields

Every block is an object with exactly these fields.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Opaque and source-scoped. It always starts with the source name and a colon, for example `docx:0004`. Callers must not parse the part after the colon. |
| `kind` | string | One of the kinds in the mapping table. |
| `text` | string | Normalized text, as defined below. Empty for skip blocks. |
| `runs` | list | Ordered runs of `text`. Each run is an object with `text` (string), `bold` (boolean) and `italic` (boolean). The run texts concatenate to `text`. Empty for skip blocks. |
| `section_type` | string | One of `front_matter`, `body` or `unknown`. See Section type. |
| `skip` | boolean | `true` when the block carries no drafting text. It is `true` for the kinds `table_of_contents`, `section_break` and `unsupported`, and `false` for every other kind. |
| `source` | string | One of `docx`, `latex` or `gdocs`. |
| `order` | integer | Zero-based position in the block list. The values run 0, 1, 2 with no gaps. |

## Text per kind

- `heading` and `paragraph`: the visible text.
- `figure`: the caption. Alt text is not included.
- `table`: the text of every cell in reading order, separated by a single space.
- `equation`: the equation source as written, with whitespace normalized.
- `citation`: the citation key as written.
- `table_of_contents`, `section_break` and `unsupported`: `""`, with `runs` empty and `skip` true.

## Normalized text

Every `text` and every run is normalized in this order:

1. Unicode NFC.
2. Each run of whitespace, including tab, line break and no-break space, becomes one ASCII space.
3. Leading and trailing spaces are removed from the block.

Runs are cut from the normalized text. Adjacent runs with the same `bold` and `italic` values are merged, so no two adjacent runs share both values.

## Section type

- `front_matter`: the block comes before the first heading.
- `body`: the block is the first heading or comes after it.
- `unknown`: the source gives no section context.

## Identity across formats

Cross-format identity is not id equality. An `id` is only meaningful inside its own source. The source prefix keeps ids from different sources apart, but equal content does not share an id across formats. A caller that maps blocks between formats, for example a docx draft against a LaTeX source, matches blocks by normalized text and position, never by `id`.

## Changing the enum

To add or remove a kind, change this document's mapping table and the `kinds` list in the fixture in the same change. The test fails if the two disagree, if a golden case uses a kind the enum does not define, or if the mapping table no longer matches the source mapping this item requires.
