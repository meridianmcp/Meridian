---
name: prose-checks
description: >-
  Flags and questions for a draft, from the prose_check MCP tool: stock phrases,
  dash marks, three-item lists, repeated spans, duplicate or restated sentences,
  unsourced figures, quantity claims with no number, generic sentences. Use when
  the user asks to check a draft, run a prose or anti-AI check, or look for
  generic or repeated wording. Returns flags only, never a verdict.
---

# Prose checks

Use the `prose_check` MCP tool. One call takes the text and a preset, and returns
everything it found in one list.

```
prose_check(text="<the writing>", preset="general")
```

Optional: `include_custom_rules=false` ignores the owner's rule file for that call.

## Presets

| preset | use for | what it does |
| --- | --- | --- |
| `general` (default) | any plain prose, Markdown included | stock phrases, dash marks, three-item lists, plus all core checks |
| `academic` | formal writing | stock phrases only; dash and three-item-list flags are off |
| `overleaf` | LaTeX source | strips comments and commands, reads `\cite` as `[cite]`, blanks math, turns `---` and `--` into dashes |
| `docx` | text exported from Word | normalises curly quotes and non-breaking spaces |

Every preset runs the core checks: repeated spans, duplicate sentences,
restatement candidates, and questions to the author. Only the built-in rule groups
differ between presets.

## Reading the result

- `flags` is one list sorted by location. Each entry has `kind`, `status`
  (`flag` or `question`), `message`, `evidence`, and `line`/`sentence` (1-based).
- `counts_by_kind` is a plain count of what was found. It is not a score.
- `measures` holds `word_count`, `sentence_count`, `specificity` (with its
  numerator and denominator) and `lexical_diversity` (a count of distinct content
  word types). Each carries `validation_status: uncalibrated`.
- Report the flags and the questions, and say which preset ran. Do not call the
  text "AI-written", "good", "bad", "passing" or "failing". The tool never returns
  such a label, and you must not add one.
- Nothing is changed by the tool. Edits come from the author, after they answer
  the questions. Do not rewrite flagged sentences unless the user asks.

## Owner rules: `prose_rules.toml`

The owner keeps custom rules in a plain TOML file named `prose_rules.toml` in the
Meridian server's data directory. It is read on every call, so an edit takes effect
at once. A missing file means no owner rules. A broken file makes the tool return
`{"error": ...}` naming the problem. Fix the file and call again.

```toml
# Remove built-in stock phrases the owner does not want flagged.
disable_phrases = ["robust"]

# Switch a built-in group off: "stock_phrases", "dash_marks" or "triad_lists".
[groups]
triad_lists = false

[[rule]]
id = "avoid-synergy"          # lower-case letters, digits, "-" or "_", up to 64 characters
kind = "phrase"               # "phrase": case-insensitive, whole words; or "regex"
pattern = "synergy"           # up to 300 characters
label = "Team wording: avoid 'synergy'"   # shown with each match
presets = ["general", "academic"]         # optional; omit to apply to every preset

[[rule]]
id = "bare-percent"
kind = "regex"
pattern = "\\d+ ?%"           # Python regex syntax
ignore_case = true            # optional; default true
label = "Percent figure"
```

Rules are validated when the file is read. Unknown keys, duplicate ids, an unknown
preset name, a regex that does not compile, and a bad type are all reported. A rule
only ever flags a match.

## Notes

- Limit: 200,000 characters per call. The check is local and makes no network call.
- On a multi-tenant server, `prose_rules.toml` in the shared data directory applies to
  every tenant on that server. Keep owner-only rules out of a shared directory.
- Every threshold and rule is uncalibrated. Say so when you report results.
